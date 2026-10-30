"""Sealed documents: drafts stay editable; the first formal reference freezes a content-addressed revision.

The immutable object is written atomically before the database transaction registers it. A crash in between
leaves at most an unregistered object (reported by `rl doctor`), never a registered reference without content.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .project import Project
from .store import Tx
from .util import RLError, atomic_write, digest_bytes, digest_file, parse_ref, short


@dataclass(frozen=True)
class Prepared:
    source_path: str
    doc_type: str
    digest: str
    object_path: str
    size: int
    git_commit: str | None


def _git_commit(path: Path) -> str | None:
    """The full HEAD commit only when the file is tracked and unmodified there; otherwise unknown (None)."""
    try:
        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", str(path.parent), *args], capture_output=True, text=True, timeout=10)
        if git("ls-files", "--error-unmatch", path.name).returncode != 0:
            return None
        if git("status", "--porcelain", "--", path.name).stdout.strip():
            return None
        head = git("rev-parse", "HEAD")
        return head.stdout.strip() if head.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def prepare(project: Project, path: str | Path, doc_type: str) -> Prepared:
    source = project.abs(str(path))
    if not source.is_file():
        raise RLError("NOT_FOUND", f"document not found: {path}")
    data = source.read_bytes()
    digest = digest_bytes(data)
    obj = project.objects_dir / f"{short(digest, 64)}{source.suffix or '.txt'}"
    if not obj.exists() or digest_file(obj) != digest:
        atomic_write(obj, data)
        obj.chmod(0o444)
    return Prepared(project.rel(source), doc_type, digest, project.rel(obj), len(data), _git_commit(source))


def register(tx: Tx, prep: Prepared) -> str:
    """Register a prepared object; unchanged content returns the existing revision."""
    docs = tx.all("Document", path=prep.source_path)
    if docs:
        doc = docs[0]
        if doc["latest_digest"] == prep.digest:
            return f"{doc['id']}@{doc['latest_revision']}"
        revision = doc["latest_revision"] + 1
        tx.update(doc["id"], {"latest_revision": revision, "latest_digest": prep.digest})
        doc_id = doc["id"]
    else:
        doc_id, revision = tx.next_id("Document"), 1
        tx.create("Document", {"id": doc_id, "path": prep.source_path, "doc_type": prep.doc_type,
                               "latest_revision": 1, "latest_digest": prep.digest})
    ref = f"{doc_id}@{revision}"
    tx.create("DocRevision", {"id": ref, "document_id": doc_id, "revision": revision, "digest": prep.digest,
                              "object_path": prep.object_path, "source_path": prep.source_path,
                              "git_commit": prep.git_commit, "size": prep.size})
    tx.event("doc.sealed", ref, f"{prep.source_path} ({prep.doc_type}) {short(prep.digest)}")
    return ref


def resolve(tx: Tx, ref: str) -> dict:
    """DOC-0001 means its latest revision; DOC-0001@2 a specific one."""
    doc_id, rev = parse_ref(ref)
    doc = tx.require(doc_id, "Document")
    return tx.require(f"{doc_id}@{rev or doc['latest_revision']}", "DocRevision")


def read(project: Project, revision: dict) -> str:
    obj = project.abs(revision["object_path"])
    if not obj.is_file():
        raise RLError("CORRUPT", f"sealed object missing for {revision['id']}: {revision['object_path']}")
    if digest_file(obj) != revision["digest"]:
        raise RLError("CORRUPT", f"sealed object for {revision['id']} does not match its digest")
    return obj.read_text(encoding="utf-8", errors="replace")


def verify(tx: Tx, project: Project) -> dict:
    """Check every registered object and list objects no revision references (crash leftovers)."""
    problems, referenced = [], set()
    for rev in tx.all("DocRevision"):
        obj = project.abs(rev["object_path"])
        referenced.add(obj.resolve())
        if not obj.is_file():
            problems.append(f"{rev['id']}: object missing ({rev['object_path']})")
        elif digest_file(obj) != rev["digest"]:
            problems.append(f"{rev['id']}: object digest mismatch ({rev['object_path']})")
    orphans = sorted(project.rel(p) for p in project.objects_dir.glob("*")
                     if p.is_file() and p.resolve() not in referenced)
    return {"documents": len(referenced), "problems": problems, "unregistered_objects": orphans}
