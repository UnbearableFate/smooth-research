"""The single store entry point: short serialized write transactions over an embedded LatticeDB graph.

Every write runs under an exclusive process lock and one database transaction that checks revisions, references
and required fields, updates records, relations and the event log, bumps data_version and commits.
An exception anywhere rolls the whole transaction back.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

from latticedb import Database
from latticedb import LatticeDatabaseLockedError

from .project import Project
from .schema import ENTITIES, ENUMS, PREFIX_LABEL, RELATIONS, SCHEMA_VERSION
from .util import RLError, now_utc

LOCK_TIMEOUT_S = 60.0
OPEN_RETRY_S = 10.0
EXTRA_INDEXES = [("Observation", "attempt_id"), ("Attempt", "run_id"), ("Message", "to_role"),
                 ("Ticket", "dedupe_key"), ("Usage", "accounting_ref"), ("ExecRef", "opaque_id"),
                 ("Document", "path"), ("Run", "experiment_id"), ("Result", "attempt_id")]


def label_of(entity_id: str) -> str:
    if "@" in entity_id:
        return "DocRevision"
    prefix = entity_id.split("-", 1)[0]
    if prefix not in PREFIX_LABEL or "-" not in entity_id:
        raise RLError("INVALID", f"unknown record id {entity_id!r}")
    return PREFIX_LABEL[prefix]


def _encode(label: str, key: str, value: Any) -> Any:
    spec = ENTITIES[label].fields.get(key)
    if spec is None:
        raise RLError("INVALID", f"{label} has no field {key!r}")
    if value is None:
        return None
    if spec.type == "json":
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    ok = {"str": isinstance(value, str), "int": isinstance(value, int) and not isinstance(value, bool),
          "float": isinstance(value, (int, float)) and not isinstance(value, bool), "bool": isinstance(value, bool)}
    if not ok[spec.type]:
        raise RLError("INVALID", f"{label}.{key} must be {spec.type}, got {value!r}")
    if spec.enum and value not in ENUMS[spec.enum]:
        raise RLError("INVALID", f"{label}.{key} must be one of {', '.join(ENUMS[spec.enum])}; got {value!r}")
    return float(value) if spec.type == "float" else value


def _decode(label: str, props: dict) -> dict:
    out = {}
    for key, value in props.items():
        if value is None:
            continue
        spec = ENTITIES[label].fields.get(key)
        out[key] = json.loads(value) if spec is not None and spec.type == "json" and isinstance(value, str) else value
    out["_label"] = label
    return out


class Tx:
    def __init__(self, txn, writable: bool, actor: str):
        self.txn = txn
        self.writable = writable
        self.actor = actor

    # -- reads -----------------------------------------------------------------------------------------------

    def q(self, cypher: str, **params: Any) -> list[dict]:
        return list(self.txn.query(cypher, parameters=params or None))

    def _node(self, label: str, entity_id: str) -> int | None:
        ids = self.txn.find_nodes_by_label_property(label, "id", entity_id, limit=2)
        if len(ids) > 1:
            raise RLError("CORRUPT", f"duplicate records for {entity_id}")
        return ids[0] if ids else None

    def get(self, entity_id: str, label: str | None = None) -> dict | None:
        label = label or label_of(entity_id)
        rows = self.q(f"MATCH (n:{label} {{id: $id}}) RETURN properties(n) AS p", id=entity_id)
        return _decode(label, rows[0]["p"]) if rows else None

    def require(self, entity_id: str, *labels: str) -> dict:
        try:
            label = label_of(entity_id)
        except RLError:
            label = labels[0] if len(labels) == 1 else None  # caller-chosen ids (observations, requests)
        record = self.get(entity_id, label) if label else None
        if record is None:
            raise RLError("NOT_FOUND", f"{entity_id} does not exist")
        if labels and record["_label"] not in labels:
            raise RLError("INVALID", f"{entity_id} is a {record['_label']}, expected {' or '.join(labels)}")
        return record

    def all(self, label: str, **equals: Any) -> list[dict]:
        where = " AND ".join(f"n.{k} = ${k}" for k in equals)
        rows = self.q(f"MATCH (n:{label}) {'WHERE ' + where if where else ''} RETURN properties(n) AS p", **equals)
        return sorted((_decode(label, r["p"]) for r in rows), key=lambda r: _sort_key(r["id"]))

    def out(self, entity_id: str, rel: str | None = None, label: str | None = None) -> list[dict]:
        return self._edges(entity_id, rel, label, outgoing=True)

    def inc(self, entity_id: str, rel: str | None = None, label: str | None = None) -> list[dict]:
        return self._edges(entity_id, rel, label, outgoing=False)

    def _edges(self, entity_id: str, rel: str | None, label: str | None, outgoing: bool) -> list[dict]:
        label = label or label_of(entity_id)
        edge = f":{RELATIONS[rel].edge}" if rel else ""
        pattern = f"(a:{label} {{id: $id}})-[e{edge}]->(b)" if outgoing else f"(b)-[e{edge}]->(a:{label} {{id: $id}})"
        rows = self.q(f"MATCH {pattern} RETURN type(e) AS t, b.id AS other, labels(b) AS l, properties(e) AS p",
                      id=entity_id)
        by_edge = {r.edge: name for name, r in RELATIONS.items()}
        out = [{"rel": by_edge.get(r["t"], r["t"].lower()), "id": r["other"], "label": (r["l"] or ["?"])[0],
                **(r["p"] or {})} for r in rows]
        return sorted(out, key=lambda e: (e["rel"], _sort_key(e["id"])))

    def meta(self) -> dict:
        ids = self.txn.get_nodes_by_label("Meta")
        if len(ids) != 1:
            raise RLError("CORRUPT", f"expected one Meta record, found {len(ids)}")
        rows = self.q("MATCH (m:Meta) RETURN properties(m) AS p")
        return dict(rows[0]["p"])

    # -- writes ----------------------------------------------------------------------------------------------

    def _need_write(self) -> None:
        if not self.writable:
            raise RLError("INTERNAL", "write attempted in a read transaction")

    def _meta_node(self) -> int:
        return self.txn.get_nodes_by_label("Meta")[0]

    def set_meta(self, **values: Any) -> None:
        self._need_write()
        nid = self._meta_node()
        for key, value in values.items():
            self.txn.set_property(nid, key, value)

    def next_id(self, label: str, prefix: str | None = None) -> str:
        """Counters live in Meta, so ids are never reused even when records are rejected."""
        self._need_write()
        entity = ENTITIES[label]
        prefix = prefix or entity.prefix
        key = f"seq_{prefix}"
        nid = self._meta_node()
        value = (self.txn.get_property(nid, key) or 0) + 1
        self.txn.set_property(nid, key, value)
        return f"{prefix}-{value:0{entity.width}d}"

    def create(self, label: str, props: dict) -> dict:
        self._need_write()
        entity = ENTITIES[label]
        stamp = now_utc()
        record = {"created_at": stamp, "updated_at": stamp, **props}
        missing = [k for k, f in entity.fields.items() if f.required and record.get(k) is None]
        if missing:
            raise RLError("INVALID", f"{label} requires {', '.join(missing)}")
        if self._node(label, record["id"]) is not None:
            raise RLError("CONFLICT", f"{record['id']} already exists")
        encoded = {k: _encode(label, k, v) for k, v in record.items()}
        self.txn.create_node(labels=[label], properties={k: v for k, v in encoded.items() if v is not None})
        return _decode(label, {k: v for k, v in encoded.items() if v is not None})

    def update(self, entity_id: str, changes: dict, label: str | None = None) -> dict:
        self._need_write()
        label = label or label_of(entity_id)
        nid = self._node(label, entity_id)
        if nid is None:
            raise RLError("NOT_FOUND", f"{entity_id} does not exist")
        entity = ENTITIES[label]
        for key, value in {**changes, "updated_at": now_utc()}.items():
            if key == "id":
                raise RLError("INVALID", "record ids are immutable")
            if value is None and entity.fields.get(key, None) is not None and entity.fields[key].required:
                raise RLError("INVALID", f"{label}.{key} is required")
            self.txn.set_property(nid, key, _encode(label, key, value))
        return self.get(entity_id, label)

    def link(self, rel: str, source: str, target: str, source_label: str | None = None,
             target_label: str | None = None, **props: Any) -> None:
        self._need_write()
        spec = RELATIONS[rel]
        s_label, t_label = source_label or label_of(source), target_label or label_of(target)
        if s_label not in spec.sources or t_label not in spec.targets:
            raise RLError("INVALID", f"{rel} links {'/'.join(spec.sources)} to {'/'.join(spec.targets)}; "
                                     f"got {source} ({s_label}) -> {target} ({t_label})")
        s, t = self._node(s_label, source), self._node(t_label, target)
        if s is None or t is None:
            raise RLError("NOT_FOUND", f"{source if s is None else target} does not exist")
        if source == target:
            raise RLError("INVALID", f"{rel} cannot link {source} to itself")
        existing = [e for e in self.out(source, rel, s_label) if e["id"] == target]
        bound = {k: props.get(k) for k in ("revision", "source_revision")} if spec.revision_bound else {}
        same = [e for e in existing if all(e.get(k) == v for k, v in bound.items())]
        # linked again (a revision-bound relation: at the same revisions): the edge takes the new description
        # (assessment, locator, note)
        if same:
            changed = {k: v for k, v in props.items() if k not in bound and v is not None and same[0].get(k) != v}
            if changed:
                where = " AND ".join(f"e.{k} = ${k}" if v is not None else f"e.{k} IS NULL" for k, v in bound.items())
                self.q(f"MATCH (a:{s_label} {{id: $s}})-[e:{spec.edge}]->(b:{t_label} {{id: $t}})"
                       + (f" WHERE {where}" if where else "")
                       + f" SET {', '.join(f'e.{k} = $new_{k}' for k in changed)}", s=source, t=target,
                       **{k: v for k, v in bound.items() if v is not None},
                       **{f"new_{k}": v for k, v in changed.items()})
            return
        if spec.acyclic and self.reachable(target, t_label, rel, source):
            raise RLError("CONFLICT", f"{rel} {source} -> {target} would create a cycle")
        clean = {k: v for k, v in props.items() if v is not None}
        self.txn.create_edge(s, t, spec.edge, properties=clean or None)

    def reachable(self, start: str, start_label: str, rel: str, goal: str) -> bool:
        seen, frontier = {start}, [(start, start_label)]
        while frontier:
            node, label = frontier.pop()
            if node == goal:
                return True
            for e in self.out(node, rel, label):
                if e["id"] not in seen:
                    seen.add(e["id"])
                    frontier.append((e["id"], e["label"]))
        return False

    def event(self, type_: str, entity_id: str | None = None, summary: str = "") -> None:
        eid = self.next_id("Event")
        self.create("Event", {"id": eid, "seq": int(eid.split("-")[1]), "type": type_, "entity_id": entity_id,
                              "actor": self.actor, "summary": summary[:500]})

    def request(self, op: str, request_id: str, digest: str) -> dict | None:
        """Return the stored result of a repeated request; refuse the same id with different content."""
        found = self.get(f"{op}:{request_id}", "Request")
        if found is None:
            return None
        if found["digest"] != digest:
            raise RLError("CONFLICT", f"request_id {request_id!r} was already used for different {op} content; "
                                      "use a new request_id for a new, legitimate record", previous=found["result"])
        return found["result"]

    def remember(self, op: str, request_id: str, digest: str, result: dict) -> None:
        self.create("Request", {"id": f"{op}:{request_id}", "op": op, "request_id": request_id, "digest": digest,
                                "result": result})


def _sort_key(entity_id: str) -> tuple:
    parts = entity_id.replace("@", "-").split("-")
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


class Store:
    def __init__(self, project: Project, actor: str = "agent"):
        self.project = project
        self.path = project.db_path
        self.actor = actor

    @staticmethod
    def create(project: Project, title: str | None = None) -> None:
        path = project.db_path
        if path.exists() or path.with_name(path.name + "-wal").exists():
            raise RLError("CONFLICT", f"database already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with Database(path, create=True) as db:
            for label in ENTITIES:
                db.create_node_property_index(label, "id")
            for label, prop in EXTRA_INDEXES:
                db.create_node_property_index(label, prop)
            with db.write() as txn:
                stamp = now_utc()
                txn.create_node(labels=["Meta"], properties={
                    "schema_version": SCHEMA_VERSION, "data_version": 0,
                    "created_at": stamp, "updated_at": stamp, "title": title or project.root.name})
                txn.commit()

    @contextmanager
    def _lock(self, exclusive: bool) -> Iterator[None]:
        self.project.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.project.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise RLError("LOCKED", f"research store busy for {LOCK_TIMEOUT_S:.0f}s: "
                                                f"{self.project.lock_path}") from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    @contextmanager
    def _open(self, write: bool) -> Iterator[Database]:
        if not self.path.exists():
            raise RLError("NO_PROJECT", f"no research database at {self.path}; run `rl init`")
        deadline = time.monotonic() + OPEN_RETRY_S
        while True:
            db = Database(self.path, read_only=not write)
            try:
                db.open()
                break
            except LatticeDatabaseLockedError:
                if time.monotonic() > deadline:
                    raise RLError("LOCKED", f"database is held by another process: {self.path}") from None
                time.sleep(0.05)
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _check_schema(tx: Tx) -> dict:
        meta = tx.meta()
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise RLError("UNSUPPORTED", f"database schema_version {meta.get('schema_version')!r}; this tool "
                                         f"supports {SCHEMA_VERSION}. Keep the old store unchanged.")
        return meta

    @contextmanager
    def read(self) -> Iterator[Tx]:
        with self._lock(exclusive=False), self._open(write=False) as db, db.read() as txn:
            tx = Tx(txn, False, self.actor)
            self._check_schema(tx)
            yield tx

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """One atomic write: everything inside commits together or not at all."""
        with self._lock(exclusive=True), self._open(write=True) as db, db.write() as txn:
            tx = Tx(txn, True, self.actor)
            meta = self._check_schema(tx)
            yield tx
            tx.set_meta(data_version=meta["data_version"] + 1, updated_at=now_utc())
            txn.commit()
