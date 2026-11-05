"""Keeping a research project usable across plugin updates.

A plugin update never stops a project. The records are upgraded in place the first time a newer CLI opens them:
under the write lock, the database files are copied to research/state/backups/ first, then every migration
between the stored schema and this one runs in one transaction, and a project.upgrade event says what changed.
Migrations only add or fill in: they never remove a record, change a recorded fact or touch a sealed document.
A project written by a newer plugin stays readable, but this CLI writes nothing to it.

What a migration cannot do (old configuration keys, files where an older layout put them, drafts and notes in an
older shape) the agent adjusts itself; `rl upgrade` lists it and `rl upgrade --note` records what was changed.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .schema import SCHEMA_VERSION
from .util import now_utc

if TYPE_CHECKING:
    from .store import Store, Tx

ENV_BUILD = "SMOOTH_RESEARCH_BUILD"  # set by bin/rl from the plugin's .build-id


@dataclass(frozen=True)
class Migration:
    version: int  # the schema version it produces
    summary: str
    apply: Callable[["Tx"], list[str]]  # returns what it changed, one line each


def _v2(tx: "Tx") -> list[str]:
    """Fields that later versions read on every record of a type, filled in on records written before them."""
    out = []
    old = [a for a in tx.all("Attempt") if not a.get("code_review")]
    for att in old:
        tx.update(att["id"], {"code_review": {"status": "unchecked",
                                              "reason": "registered before code review checks were recorded"}})
    if old:
        out.append(f"{len(old)} attempt(s) registered before code review checks: code review 'unchecked'")
    advice = tx.all("Ticket", kind="advice")
    rounds: dict[str, list[dict]] = {}  # one request package = one round
    for t in advice:
        if t.get("advice_round") is None:
            rounds.setdefault(t.get("request_ref") or t["id"], []).append(t)
    number = max([t["advice_round"] for t in advice if t.get("advice_round") is not None], default=0)
    for tickets in sorted(rounds.values(), key=lambda ts: min(t["id"] for t in ts)):
        number += 1
        for t in tickets:
            tx.update(t["id"], {"advice_round": number})
    if rounds:
        out.append(f"{sum(len(ts) for ts in rounds.values())} advice ticket(s) from before advice rounds: grouped "
                   f"into {len(rounds)} round(s) by request package")
    return out


def _v3(tx: "Tx") -> list[str]:
    """Labels (H2.1, EH2.1/3) for the hypotheses and experiments written before them, by the rules new records
    follow (labels), in the order they were created: the hypotheses by id, each placed by the hypotheses it is
    derived from that were created before it; then the experiments by id, each by the hypotheses it tests that
    existed when it was created, the first by id standing in for the first one named. Which hypotheses opened a
    new line of research (--root) was never recorded, so the labels of an older project run deeper than new ones."""
    from .labels import for_experiment, for_hypothesis
    from .store import _sort_key
    hyps = [h for h in tx.all("Hypothesis") if not h.get("label")]
    for h in hyps:
        sources = [e["id"] for e in tx.out(h["id"], "derived_from")
                   if e["label"] == "Hypothesis" and _sort_key(e["id"]) < _sort_key(h["id"])]
        tx.update(h["id"], {"label": for_hypothesis(tx, list(dict.fromkeys(sources)))})
    exps, untested = [e for e in tx.all("Experiment") if not e.get("label")], []
    for exp in exps:
        tested = [tx.require(e["id"]) for e in tx.out(exp["id"], "tests")]
        before = [h["id"] for h in tested if h["created_at"] <= exp["created_at"]] or [h["id"] for h in tested]
        if not before:
            untested.append(exp["id"])
            continue
        tx.update(exp["id"], {"label": for_experiment(tx, before)})
    out = [f"{len(hyps)} hypothes{'i' if len(hyps) == 1 else 'e'}s and {len(exps) - len(untested)} experiment(s) "
           "labelled in the order they were created (rl upgrade lists them)"] if hyps or exps else []
    out += [f"{', '.join(untested)} test(s) no hypothesis: no label, shown by id"] if untested else []
    return out


MIGRATIONS = [
    Migration(2, "fill in fields that records written before them lack (attempt code review check, advice round)",
              _v2),
    Migration(3, "labels for hypotheses and experiments (H2.1, EH2.1/3)", _v3),
]


def environment(project) -> tuple[str, bool]:
    """(one line on the Python environment rl runs on, whether it is the project's own <root>/.venv). bin/rl
    runs rl on the project's environment once rl init has made it; before that, on the plugin's."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        lattice = version("latticedb")
    except PackageNotFoundError:
        lattice = "unknown"
    venv = project.root / ".venv"
    own = venv.exists() and Path(sys.prefix).resolve() == venv.resolve()
    where = (f"the project's {project.rel(venv)}" if own else
             f"{sys.prefix}, not the project's own: rl init in the project root makes one (uv init, uv add of rl's "
             "dependencies)")
    return f"environment: Python {platform.python_version()}, latticedb {lattice} on {where}", own


def build() -> str:
    """The plugin build this CLI comes from ('dev' in a source checkout)."""
    return os.environ.get(ENV_BUILD) or "dev"


def migrate(store: "Store") -> dict | None:
    """Bring an older store up to SCHEMA_VERSION; None when it already is (or is newer). Holds the write lock
    throughout, so concurrent CLIs wait and then find it upgraded."""
    from .store import Tx
    with store._lock(exclusive=True):
        with store._open(write=False) as db, db.read() as txn:
            version = Tx(txn, False, "upgrade").meta().get("schema_version")
        if not isinstance(version, int) or version >= SCHEMA_VERSION:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = store.project.state_dir / "backups" / f"schema-{version}-{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        for path in store.path.parent.glob(store.path.name + "*"):
            shutil.copy2(path, backup / path.name)
        changes = []
        with store._open(write=True) as db, db.write() as txn:
            tx = Tx(txn, True, "upgrade")
            meta = tx.meta()
            for m in MIGRATIONS:
                if version < m.version <= SCHEMA_VERSION:
                    changes += [f"schema {m.version}: {m.summary}"] + [f"  {c}" for c in m.apply(tx)]
            tx.set_meta(schema_version=SCHEMA_VERSION, data_version=meta["data_version"] + 1, updated_at=now_utc())
            rel = store.project.rel(backup)
            tx.event("project.upgrade", None, f"schema {version} -> {SCHEMA_VERSION} by build {build()}; copy of the "
                                              f"old database: {rel}; " + "; ".join(c.strip() for c in changes))
            txn.commit()
    return {"from": version, "to": SCHEMA_VERSION, "backup": str(backup), "changes": changes}


# Where earlier versions kept things, and where this one does. Files there keep working (records point to them);
# rl upgrade lists them and rl upgrade --move moves them together with the paths recorded for them. Nested
# entries come first, so a parent directory is moved after its children went elsewhere.
OLDER_LAYOUT = (
    ("docs/plan/original.md", "docs/plan.md", "the imported plan"),
    ("docs/plan/constraints.md", "docs/constraints.md", "the constraints document"),
    ("docs/experiments", "research/experiments", "experiment contracts"),
    ("docs/tasks", "research/tasks", "task notes"),
    ("docs/notes", "research/notes", "researcher notes"),
    ("reports/reviews", "research/reviews", "review answers"),
    ("reports/advice", "research/advice", "advice answers"),
    ("reports", "research/reports", "stage reports"),
)
PROTECTED = ("research/state", "research/documents")  # the database and the sealed objects


def older_layout(project) -> list[tuple[str, str, str]]:
    """(old, new, what) for each earlier location that still holds something."""
    out = []
    for old, new, what in OLDER_LAYOUT:
        path = project.root / old
        if path.is_file() or (path.is_dir() and any(f.is_file() for f in path.rglob("*"))):
            out.append((old, new, what))
    return out


def move(store: "Store", old: str, new: str) -> dict:
    """Move a file or directory inside the project and rewrite the paths the records keep for it (experiment
    contracts, document locations) in the same transaction; the files go back if the transaction fails. Where a
    file was sealed from (revisions, events) stays as recorded."""
    from .util import RLError
    project = store.project
    root = project.root.resolve()
    src, dst = (project.abs(p).resolve() for p in (old, new))
    for path in (src, dst):
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
        if not rel or rel == "." or any(rel == x or rel.startswith(x + "/") for x in PROTECTED):
            raise RLError("INVALID", f"{path} is not a movable path inside the project (the database and the sealed "
                                     "objects stay where they are)")
    if not src.exists():
        raise RLError("NOT_FOUND", f"{old} does not exist")
    pairs = [(src, dst)] if src.is_file() else \
        [(f, dst / f.relative_to(src)) for f in sorted(src.rglob("*")) if f.is_file()]
    clashes = [project.rel(d) for _, d in pairs if d.exists()]
    if clashes:
        raise RLError("CONFLICT", f"already there: {', '.join(clashes[:5])}; move or merge those first")
    rel_old, rel_new = src.relative_to(root).as_posix(), dst.relative_to(root).as_posix()
    first = [(o, n) for o, n, _ in older_layout(project)
             if o.startswith(rel_old + "/") and not n.startswith(rel_new + "/")]
    if first:
        raise RLError("CONFLICT", f"{rel_old} still holds {first[0][0]}, which this version keeps in {first[0][1]}: "
                                  f"move that first (rl upgrade --move {first[0][0]} {first[0][1]})")

    def moved(path: str | None) -> str | None:
        if path and (path == rel_old or path.startswith(rel_old + "/")):
            return rel_new + path[len(rel_old):]
        return None

    done: list[tuple] = []
    changed: list[str] = []
    try:
        with store.write() as tx:
            for exp in tx.all("Experiment"):
                if to := moved(exp.get("contract_path")):
                    tx.update(exp["id"], {"contract_path": to})
                    changed.append(f"{exp['id']} contract")
            for doc in tx.all("Document"):
                if to := moved(doc.get("path")):
                    tx.update(doc["id"], {"path": to})
                    changed.append(doc["id"])
            for s, d in pairs:
                d.parent.mkdir(parents=True, exist_ok=True)
                s.rename(d)
                done.append((s, d))
            if src.is_dir():
                for sub in sorted((p for p in src.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
                    sub.rmdir()
                src.rmdir()
            tx.event("project.moved", None, f"{rel_old} -> {rel_new}: {len(pairs)} file(s); records: "
                                            f"{', '.join(changed) or 'none'}")
    except BaseException:
        for s, d in reversed(done):
            s.parent.mkdir(parents=True, exist_ok=True)
            d.rename(s)
        raise
    return {"moved": len(pairs), "records": changed,
            "summary": f"moved {rel_old} -> {rel_new}: {len(pairs)} file(s); recorded paths rewritten: "
                       f"{', '.join(changed) or 'none'}. Commit the move."}


ADJUST = """A plugin update does not stop the research: adjust the old research assets to this version yourself.
- You may change: smooth-research.yaml (the warnings say what this version reads differently), the wording of
  docs/constraints.md where only the format changed, files an older layout put elsewhere (move them with
  rl upgrade --move, which rewrites the paths the records keep; a plain mv breaks those links), unsealed
  contract drafts, task notes, notes, worktrees and their branches, and you may add records that this version
  expects (rl commands only; never edit research/state/ by hand).
- Ask the user only when an adjustment would change the goal, the claims in scope, the data or the budget.
- Never rewrite recorded facts (observations, results, evidence, decisions, reviews) or sealed documents: when
  this version wants them in another form, add a new record or revision that cites the old one.
- Say what you changed: rl upgrade --note "<what and why>", and commit it."""


def _labels_given(tx: "Tx", upgrades: list[dict]) -> list[str]:
    """The labels the upgrade to schema 3 gave the hypotheses and experiments that existed then, a few per row;
    shown while that upgrade is in the history rl upgrade lists."""
    import re
    done = [e for e in upgrades if (m := re.match(r"schema (\d+) -> (\d+)", e.get("summary") or ""))
            and int(m[1]) < 3 <= int(m[2])]
    if not done:
        return []
    pairs = [f"{r['id']} {r['label']}" for entity in ("Hypothesis", "Experiment") for r in tx.all(entity)
             if r.get("label") and r["created_at"] <= done[-1]["created_at"]]
    return [" · ".join(pairs[i:i + 6]) for i in range(0, len(pairs), 6)]


def report(store: "Store", note: str | None = None) -> dict:
    """rl upgrade: the format and plugin build of the project, upgrades done, what this version reads differently,
    and what the agent may adjust; with note, records the agent's own adjustments."""
    from . import research, snapshot
    project = store.project
    older = older_layout(project)
    created = research.ensure_layout(project, skip=[project.root / new for _, new, _ in older])
    with store.read() as tx:
        meta = tx.meta()
        upgrades = [e for e in tx.all("Event") if e["type"] in ("project.upgrade", "project.adjusted")]
        given = _labels_given(tx, upgrades[-5:])
    last = meta.get("last_build")
    with store.write() as tx:
        tx.set_meta(last_build=build())
        if note and note.strip():
            tx.event("project.adjusted", None, f"by build {build()}: {note.strip()}")
    lines = [f"project format: schema {meta.get('schema_version')} (this CLI: {SCHEMA_VERSION}) · plugin build "
             f"{build()} (last used with {last or 'unknown'})", environment(project)[0]]
    lines += [f"history: {e['created_at'][:16]} {e['type']}: {e['summary']}" for e in upgrades[-5:]]
    if given:
        lines += ["labels the schema 3 upgrade gave the records (id label; commands take either, paths, job names "
                  "and commits keep the ids):"] + [f"  {row}" for row in given]
    if note and note.strip():
        lines.append(f"recorded: project.adjusted: {note.strip()}")
    lines += [f"layout: created {', '.join(created)}" for _ in [0] if created]
    lines += [f"older layout: {old} ({what}) works where it is, since the records point there; this version keeps "
              f"them in {new}. To move them with their records: rl upgrade --move {old} {new}"
              for old, new, what in older]
    if (project.docs_root / "research-graph.html").exists():
        lines.append("docs/research-graph.html is no longer updated: rl view writes docs/research.html, the records "
                     "with the explanations in research/notes/explanation.yaml (rl view --todo lists what to write). "
                     "Delete the old page when nobody needs it")
    with store.read() as tx:
        lost = [f"{e['id']} contract {e['contract_path']}" for e in tx.all("Experiment")
                if e.get("contract_path") and not project.abs(e["contract_path"]).is_file()]
    lines += [f"records: {item} is missing (moved by hand? move it back, then use rl upgrade --move)" for item in lost]
    warnings = project.warnings  # printed after the summary as warning: lines
    lines.append(f"configuration: {len(warnings)} warning(s) below: fix them in smooth-research.yaml" if warnings
                 else "configuration: read as written")
    from .watch import config_hint
    lines += [f"configuration: {hint}" for hint in [config_hint(store)] if hint]
    check = snapshot.doctor(store)
    problems = check["problems"] + check["record_problems"]
    lines += [f"records: {p}" for p in problems] or [f"records: {check['documents']} sealed object(s) verified"]
    return {"schema_version": meta.get("schema_version"), "build": build(), "last_build": last, "created": created,
            "older_layout": [old for old, _, _ in older], "missing": lost, "problems": problems,
            "summary": "\n".join(lines) + "\n\n" + ADJUST}
