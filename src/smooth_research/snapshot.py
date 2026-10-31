"""Consistent export and restore. Restore rebuilds records only: it never queries, starts or retries work."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from latticedb import Database

from . import documents
from .context import resume_pack
from .project import CONFIG_NAME, Project, load_config
from .schema import ENTITIES, SCHEMA_VERSION
from .store import Store, Tx
from .util import RLError, atomic_write, digest_file

FORMAT = "smooth-research-snapshot/1"


def export(store: Store, out: str | None = None) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(out) if out else store.project.dir("snapshots") / stamp
    if target.exists() and any(target.iterdir()):
        raise RLError("CONFLICT", f"snapshot directory {target} is not empty")
    with store.read() as tx:  # one read transaction: nodes, edges and meta from the same state
        meta = tx.meta()
        nodes = {label: [r["p"] for r in tx.q(f"MATCH (n:{label}) RETURN properties(n) AS p")] for label in ENTITIES}
        edges = [{"source": r["s"], "source_label": r["sl"][0], "edge": r["t"], "target": r["d"],
                  "target_label": r["dl"][0], "props": r["p"] or {}}
                 for label in ENTITIES
                 for r in tx.q(f"MATCH (a:{label})-[e]->(b) RETURN a.id AS s, labels(a) AS sl, type(e) AS t, "
                               "b.id AS d, labels(b) AS dl, properties(e) AS p")]
        revisions = tx.all("DocRevision")
    objects = target / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    for rev in revisions:
        source = store.project.abs(rev["object_path"])
        if source.is_file() and not (objects / source.name).exists():
            shutil.copyfile(source, objects / source.name)
    shutil.copyfile(store.project.root / CONFIG_NAME, target / CONFIG_NAME)
    payload = {"format": FORMAT, "exported_at": stamp, "schema_version": SCHEMA_VERSION, "meta": meta,
               "nodes": nodes, "edges": edges}
    atomic_write(target / "snapshot.json", json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))
    count = sum(len(v) for v in nodes.values())
    return {"path": str(target), "summary": f"snapshot {store.project.rel(target)}: {count} records, {len(edges)} "
                                             f"relations, {len(revisions)} sealed documents, data_version {meta['data_version']}"}


def restore(snapshot: str, into: str) -> dict:
    source = Path(snapshot)
    data = json.loads((source / "snapshot.json").read_text(encoding="utf-8"))
    version = data.get("schema_version")
    if data.get("format") != FORMAT or not isinstance(version, int):
        raise RLError("UNSUPPORTED", f"{snapshot} is not a smooth-research snapshot ({data.get('format')})")
    if version > SCHEMA_VERSION:
        raise RLError("UNSUPPORTED", f"{snapshot} was written by a newer smooth-research (record format {version}; "
                                     f"this CLI writes {SCHEMA_VERSION}): restore it with the newer plugin")
    root = Path(into).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not (root / CONFIG_NAME).exists():
        shutil.copyfile(source / CONFIG_NAME, root / CONFIG_NAME)
    project = Project(root, load_config(root))
    if project.db_path.exists():
        raise RLError("CONFLICT", f"{root} already has a research database; restore into a new directory")
    Store.create(project)
    project.objects_dir.mkdir(parents=True, exist_ok=True)
    for obj in (source / "objects").glob("*"):
        if not (project.objects_dir / obj.name).exists():
            shutil.copyfile(obj, project.objects_dir / obj.name)
    with Database(project.db_path) as db, db.write() as txn:
        meta_id = txn.get_nodes_by_label("Meta")[0]
        for key, value in {**data["meta"], "schema_version": version}.items():
            txn.set_property(meta_id, key, value)
        ids = {}
        for label, rows in data["nodes"].items():
            for props in rows:
                node = txn.create_node(labels=[label], properties=props)
                ids[(label, props["id"])] = node.id
        for e in data["edges"]:
            txn.create_edge(ids[(e["source_label"], e["source"])], ids[(e["target_label"], e["target"])], e["edge"],
                            properties=e["props"] or None)
        txn.commit()
    with Database(project.db_path, read_only=True) as db, db.read() as txn:  # as restored, before any upgrade
        restored = Tx(txn, False, "restore").meta()["data_version"]
    if restored != data["meta"]["data_version"]:
        raise RLError("CORRUPT", "restored data_version differs from the snapshot")
    store = Store(project, "restore")  # an older snapshot is upgraded on this first open
    with store.read() as tx:
        check = documents.verify(tx, project)
    pack = resume_pack(store)
    problems = check["problems"]
    return {"project": str(root), "problems": problems,
            "summary": f"restored into {root} (data_version {restored}); sealed documents: "
                       + ("all verified" if not problems else f"PROBLEMS: {problems}")
                       + (f"\nrecords upgraded from format {version} to {SCHEMA_VERSION}; see rl upgrade"
                          if store.upgraded else "")
                       + "\nNothing was queried or restarted. Check unknown attempts with your execution tool.\n\n"
                       + pack["summary"]}


def doctor(store: Store) -> dict:
    """Integrity check: sealed objects, unregistered objects left by an interrupted seal, and run records."""
    with store.read() as tx:
        check = documents.verify(tx, store.project)
        dangling = []
        for att in tx.all("Attempt"):
            if not tx.get(att["run_id"], "Run"):
                dangling.append(f"{att['id']} points to missing {att['run_id']}")
        for res in tx.all("Result"):
            for v in res.get("verification") or []:
                if v.get("path") and v.get("local_digest") and store.project.abs(v["path"]).is_file() \
                        and digest_file(store.project.abs(v["path"])) != v["local_digest"]:
                    dangling.append(f"{res['id']}: {v['path']} changed since import")
    lines = [f"{check['documents']} sealed object(s) checked"]
    lines += [f"problem: {p}" for p in check["problems"] + dangling]
    if check["unregistered_objects"]:
        lines.append("unregistered objects (an interrupted seal; harmless, re-run the command that sealed them): "
                     + ", ".join(check["unregistered_objects"]))
    return {**check, "record_problems": dangling, "summary": "\n".join(lines)}
