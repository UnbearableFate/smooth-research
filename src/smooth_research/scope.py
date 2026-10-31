"""What a code review saw and what a run executes.

A code review is keyed by the fingerprint of its scope: the files and dependencies under review, the configs with
the keys allowed to vary removed, and the scope text. The scope is kept on the ticket, so an answer can tell which
files changed while the review ran, and registering a run can tell whether the passing reviews still cover the
code it executes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from .research import review_mark, review_passes
from .store import Store, Tx
from .util import RLError, digest_bytes, digest_file, digest_json


def _strip(value, keys: set):
    if isinstance(value, dict):
        return {k: _strip(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_strip(v, keys) for v in value]
    return value


def _plain(value):
    """A parsed config as JSON holds it: dates, binary values and keys that are not strings become strings."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def _config_digest(path: str, data: bytes, ignore_keys: list[str]) -> str:
    try:
        loaded = (json.loads if Path(path).suffix == ".json" else yaml.safe_load)(data.decode("utf-8"))
        return digest_json(_strip(_plain(loaded), set(ignore_keys)))
    except (ValueError, yaml.YAMLError):
        return digest_bytes(data)  # not structured: the whole file counts


def fingerprint(store: Store, files: list[str], configs: list[str], deps: list[str], ignore_keys: list[str],
                scope: str) -> tuple[str, dict]:
    def digest(p: str) -> str:
        path = store.project.src(p)
        if not path.is_file():
            raise RLError("NOT_FOUND", f"review scope file not found: {p}")
        return digest_file(path)

    def rel(p: str) -> str:  # recorded relative to the checkout: the same path names the file after a merge
        return _checkout_rel(store, store.project.src(p))

    detail = {"files": {rel(f): digest(f) for f in sorted(files)},
              "deps": {rel(d): digest(d) for d in sorted(deps)},
              "configs": {}, "ignored_keys": sorted(ignore_keys), "scope": scope.strip()}
    for c in sorted(configs):
        path = store.project.src(c)
        digest(c)
        detail["configs"][rel(c)] = _config_digest(c, path.read_bytes(), ignore_keys)
    return digest_json(detail), detail


def _checkout_rel(store: Store, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(store.project.checkout))
    except ValueError:
        return store.project.rel(path)


def changed_since_request(t: dict) -> list[str]:
    """The paths of a code review whose content in the requester's checkout differs from the version the
    request recorded (configs compared without the keys allowed to vary)."""
    scope, checkout = t.get("code_scope"), t.get("code_checkout")
    if not scope or not checkout:
        return []
    changed = []
    for kind in ("files", "deps", "configs"):
        for path, digest in (scope.get(kind) or {}).items():
            try:
                data = (Path(checkout) / path).read_bytes()
            except OSError:
                changed.append(path)
                continue
            now = _config_digest(path, data, scope.get("ignored_keys") or []) if kind == "configs" \
                else digest_bytes(data)
            if now != digest:
                changed.append(path)
    return changed


# -- code review coverage of a run ----------------------------------------------------------------------------


def _code_reviews(tx: Tx, experiment: str, named: list[str]) -> list[dict]:
    """Code reviews of the experiment and of the experiments its code comes from, plus the ones named."""
    exps, todo = set(), [experiment]
    while todo:
        eid = todo.pop()
        if eid not in exps:
            exps.add(eid)
            todo += [e["id"] for e in tx.out(eid, "code_from")]
    ids = {e["id"] for eid in exps for e in tx.inc(eid, "targets") if e["label"] == "Ticket"}
    for rid in named:
        t = tx.require(rid, "Ticket")
        if t["kind"] != "review" or t.get("subject") == "conclusion":
            raise RLError("INVALID", f"{rid} is not a code review")
        ids.add(rid)
    tickets = [tx.require(i) for i in sorted(ids)]
    return [t for t in tickets if t["kind"] == "review" and t.get("subject") != "conclusion"]


def _code_reader(store: Store, code_ref) -> tuple:
    """Files as the run executes them: from the commit its RunSpec names, otherwise from the working tree."""
    commit = code_ref.get("commit") if isinstance(code_ref, dict) else None
    if not commit:
        def read_tree(p: str) -> bytes | None:
            path = store.project.src(p)
            return path.read_bytes() if path.is_file() else None
        return read_tree, "working tree" + (f" of {store.project.checkout}" if store.project.in_worktree else "")

    def git(*args: str) -> subprocess.CompletedProcess:  # worktrees share the repository's commits
        return subprocess.run(["git", *args], cwd=store.project.checkout, capture_output=True, timeout=30)

    try:
        found = git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        found = False
    if not found:
        return None, f"commit {commit[:12]} is not in this repository"

    def read_commit(p: str) -> bytes | None:
        out = git("cat-file", "blob", f"{commit}:./{p}")
        return out.stdout if out.returncode == 0 else None
    return read_commit, f"commit {commit[:12]}"


def code_coverage(store: Store, experiment: str, code_ref, named: list[str] = (), waived: str | None = None) -> dict:
    """Do answered code reviews still cover the code a run of this experiment executes?

    Every path a passing review covered must still match, in that code, the version at least one passing
    review saw (configs compared without the keys that review allowed to vary). The result is recorded on the
    attempt; nothing is blocked, because only the researcher knows whether the run needs a review.
    """
    if waived is not None:
        if not waived.strip():
            raise RLError("INVALID", "--no-review needs the reason this run needs no code review")
        return {"status": "waived", "reason": waived.strip()}
    with store.read() as tx:
        tickets = _code_reviews(tx, experiment, list(named))
    if not tickets:
        return {"status": "none"}
    passing = [t for t in tickets if review_passes(t)]
    others = [t for t in tickets if t not in passing]
    if not passing:
        return {"status": "not_passed", "open": [f"{t['id']} {review_mark(t)}" for t in others]}
    read, source = _code_reader(store, code_ref)
    if read is None:
        return {"status": "unchecked", "reviews": [f"{t['id']} {review_mark(t)}" for t in passing], "reason": source}

    def current(path: str, kind: str, ignored: list[str]) -> str | None:
        data = read(path)
        if data is None:
            return None
        return _config_digest(path, data, ignored) if kind == "configs" else digest_bytes(data)

    seen: dict[str, list[str]] = {}  # path -> passing reviews that saw the version this run executes
    older: dict[str, list[str]] = {}  # path -> passing reviews that saw another version
    try:
        for t in passing:
            scope = t["code_scope"]
            for kind in ("files", "deps", "configs"):
                for path, digest in (scope.get(kind) or {}).items():
                    now = current(path, kind, scope.get("ignored_keys") or [])
                    (seen if now == digest else older).setdefault(path, []).append(t["id"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unchecked", "reviews": [f"{t['id']} {review_mark(t)}" for t in passing],
                "reason": f"could not read the {source}: {exc}"}
    changed = {p: ids for p, ids in sorted(older.items()) if p not in seen}
    used = {i for ids in seen.values() for i in ids}
    out = {"status": "stale" if changed else "covered", "source": source,
           "reviews": [f"{t['id']} {review_mark(t)}" for t in passing if t["id"] in used]}
    if changed:
        out["changed"] = changed
    pending = [f"{t['id']} {t['status']}" for t in others if t["status"] in ("pending", "running")]
    if pending:
        out["open"] = pending
    return out
