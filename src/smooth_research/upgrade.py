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


def _v6(tx: "Tx") -> list[str]:
    """Schema 6 (design 2026-11-27, chapter 9): the plugin guides and records, nothing in it stops the agents.
    Limits that no longer exist leave the records; review rounds end after two reviews and no longer escalate, so
    escalated rounds, rounds answered twice and the rulings that waited on them are closed (their findings and any
    ruling already given stay as they were); answered approvals get the choice their answer began with."""
    out = []
    limited = [t["id"] for t in tx.all("Task") if t.get("parallel_limit") is not None]
    for tid in limited:
        tx.update(tid, {"parallel_limit": None})
    if limited:
        out.append(f"parallel limit removed from {', '.join(limited)}")
    phases = [p["id"] for p in tx.all("Phase") if p.get("scientist_limit") is not None]
    for ph in phases:
        tx.update(ph, {"scientist_limit": None})
    if phases:
        out.append(f"scientist limit removed from {', '.join(phases)}")
    closed = []
    for t in tx.all("Ticket"):
        if t["kind"] == "review" and (t["status"] == "escalated"
                                      or (t["status"] == "answered" and len(t.get("passes") or []) >= 2)):
            tx.update(t["id"], {"status": "closed"})
            closed.append(f"{t['id']} ({t['status']})")
        elif t["kind"] == "advice" and t.get("advice_kind") == "ruling" and t["status"] == "waiting":
            tx.update(t["id"], {"status": "closed"})
            closed.append(f"{t['id']} (a ruling that waited)")
    if closed:
        out.append(f"closed: {', '.join(closed)}")
    chose = []
    for a in tx.all("Ask", kind="approval"):
        if a["state"] == "answered" and not a.get("choice"):
            text = (a.get("answer") or "").strip().lower()
            choice = "approve" if text.startswith("approve") else "change" if text.startswith("change") else None
            if choice:
                tx.update(a["id"], {"choice": choice})
                chose.append(f"{a['id']} {choice}")
    if chose:
        out.append(f"approval choices: {', '.join(chose)}")
    return out or ["nothing to change"]


# The migrations from schema 5 on, in order (schema 5 is the oldest format opened: design 10.4). A change of the
# record format adds its migration here and freezes a fixture of the format it upgrades from
# (tests/fixtures/projects/schema-<n>).
MIGRATIONS: list[Migration] = [
    Migration(6, "the plugin guides and records: limits removed, review rounds of two reviews, approval choices",
              _v6),
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


def same_source(last: str | None, current: str) -> bool:
    """Whether two build ids come from one source tree. An id is <source>-<build> (scripts/build_plugins.py):
    the Claude and Codex builds of one tree differ only after the dash, so a project that a Codex coordinator
    and Claude researchers share is not changed by either. An id without a dash (older builds, dev) is
    compared whole."""
    return bool(last) and last.split("-", 1)[0] == current.split("-", 1)[0]


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
    if version < 6:  # a watcher of the old rules still running would go on scheduling (2026-11-27, 3.4)
        from .watch import retire_older
        stopped = retire_older(store)
        if stopped:
            changes.append(stopped)
    return {"from": version, "to": SCHEMA_VERSION, "backup": str(backup), "changes": changes}


# Where earlier versions kept things, and where this one does. Files there keep working (records point to them);
# rl upgrade lists them and rl upgrade --move moves them together with the paths recorded for them. Nested
# entries come first, so a parent directory is moved after its children went elsewhere.
# Where earlier versions kept things, and where this one does: (old, new, what). Files there keep working (records
# point to them); rl upgrade lists them and rl upgrade --move moves them together with the paths recorded for them.
# Emptied with the 2026-11 redesign; later versions add to it.
OLDER_LAYOUT: tuple[tuple[str, str, str], ...] = ()
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
        inside = rel and any(rel == x or rel.startswith(x + "/") for x in PROTECTED)
        holding = rel and path == src and any(x.startswith(rel + "/") for x in PROTECTED)  # it would take them along
        if not rel or rel == "." or inside or holding:
            raise RLError("INVALID", f"{path} is not a movable path inside the project (the database and the sealed "
                                     "objects stay where they are)")
    if not src.exists():
        raise RLError("NOT_FOUND", f"{old} does not exist")
    pairs = [(src, dst)] if src.is_file() else \
        [(f, dst / f.relative_to(src)) for f in sorted(src.rglob("*")) if f.is_file()]

    def lands(d: Path) -> str | None:
        """Where a file renamed to d really goes, relative to the project: its folder with every link in it
        resolved (a link inside the destination may lead elsewhere); None outside the project."""
        at = d.parent.resolve() / d.name
        return at.relative_to(root).as_posix() if at.is_relative_to(root) else None
    into = [project.rel(d) for _, d in pairs
            if (at := lands(d)) is None or any(at.startswith(x + "/") for x in PROTECTED)]
    if into:
        raise RLError("INVALID", f"{', '.join(into[:5])} would land among the database and the sealed objects, which "
                                 "hold only what rl writes there, or outside the project")
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
  drafts in task work directories, worktrees and their branches, and you may add records that this version
  expects (rl commands only; never edit research/state/ by hand).
- Ask the user only when an adjustment would change the goal, the claims in scope, the data or the budget.
- Never rewrite recorded facts (observations, results, evidence, decisions, reviews) or sealed documents: when
  this version wants them in another form, add a new record or revision that cites the old one.
- Say what you changed: rl upgrade --note "<what and why>", and commit it."""


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
    last = meta.get("last_build")
    with store.write() as tx:
        tx.set_meta(last_build=build())
        if note and note.strip():
            tx.event("project.adjusted", None, f"by build {build()}: {note.strip()}")
    lines = [f"project format: schema {meta.get('schema_version')} (this CLI: {SCHEMA_VERSION}) · plugin build "
             f"{build()} (last used with {last or 'unknown'})", environment(project)[0]]
    lines += [f"history: {e['created_at'][:16]} {e['type']}: {e['summary']}" for e in upgrades[-5:]]
    if note and note.strip():
        lines.append(f"recorded: project.adjusted: {note.strip()}")
    lines += [f"layout: created {', '.join(created)}" for _ in [0] if created]
    lines += [f"older layout: {old} ({what}) works where it is, since the records point there; this version keeps "
              f"them in {new}. To move them with their records: rl upgrade --move {old} {new}"
              for old, new, what in older]
    with store.read() as tx:
        lost = [f"{d['id']} {d['path']}" for d in tx.all("Document") if not project.abs(d["path"]).is_file()]
    lines += [f"records: {item} is missing (moved by hand? move it back, then use rl upgrade --move)" for item in lost]
    warnings = project.warnings  # printed after the summary as warning: lines
    lines.append(f"configuration: {len(warnings)} warning(s) below: fix them in smooth-research.yaml" if warnings
                 else "configuration: read as written")
    check = snapshot.doctor(store)
    problems = check["problems"] + check["record_problems"]
    lines += [f"records: {p}" for p in problems] or [f"records: {check['documents']} sealed object(s) verified"]
    return {"schema_version": meta.get("schema_version"), "build": build(), "last_build": last, "created": created,
            "older_layout": [old for old, _, _ in older], "missing": lost, "problems": problems,
            "summary": "\n".join(lines) + "\n\n" + ADJUST}
