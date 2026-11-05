"""What a code review saw and what a run executes.

A code review is keyed by the fingerprint of its scope: the files and dependencies under review, the configs with
the keys allowed to vary removed, and the scope text. The scope is kept on the ticket, so an answer can tell which
files changed while the review ran, and registering a run can tell whether the passing reviews still cover the
code it executes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

import yaml

from .research import passing_rechecks, review_mark, review_passes
from .store import Store, Tx, _sort_key
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


def is_test_path(path: str) -> bool:
    """A test file by the usual conventions: in a tests/ folder, or named test_*.py, *_test.py or conftest.py. Only
    the name is looked at, so the reports list such files by name: a production file named like a test shows."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else path
    return ("tests" in parts[:-1] or name == "conftest.py"
            or (name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))))


def _scope_items(t: dict):
    """(path, kind, digest, ignored keys) for each path a code review saw; a review from before code scopes were
    recorded saw none."""
    scope = t.get("code_scope") or {}
    for kind in ("files", "deps", "configs"):
        for path, digest in (scope.get(kind) or {}).items():
            yield path, kind, digest, scope.get("ignored_keys") or []


def code_coverage(store: Store, experiment: str, code_ref, named: list[str] = (), waived: str | None = None) -> dict:
    """Do answered code reviews still cover the code a run of this experiment executes?

    Every path a passing review covered must still match, in that code, the version at least one passing
    review saw (configs compared without the keys that review allowed to vary). A path that matches none has a
    reason, and only one makes the check stale:
      changed  the content differs (the passing code reviews of other experiments that saw this content are
               named: --review RV-x counts one, when it answers this experiment's question; it is not counted
               by itself)
      absent   the path is not in that code, so the run cannot execute it
      tests    a test file (is_test_path) changed: what the run computes is the same
    When the files are covered by several reviews and none of them saw all of them, the coverage is per file: the
    combination was not reviewed. Reviews of the experiment that did not pass, and that no passing re-check
    followed, are listed apart from the passing ones. When the passing reviews saw nothing that is in that code
    except tests, the run is not reviewed. The result is recorded on the attempt; nothing is blocked, because only
    the researcher knows whether the run needs a review.
    """
    if waived is not None:
        if not waived.strip():
            raise RLError("INVALID", "--no-review needs the reason this run needs no code review")
        return {"status": "waived", "reason": waived.strip()}
    with store.read() as tx:
        tickets = _code_reviews(tx, experiment, list(named))
        everyone = [t for t in tx.all("Ticket", kind="review") if t.get("subject") != "conclusion"]
    if not tickets:
        return {"status": "none"}
    passing = [t for t in tickets if review_passes(t)]
    others = [t for t in tickets if t not in passing]
    if not passing:
        return {"status": "not_passed", "open": [f"{t['id']} {review_mark(t)}" for t in others]}
    read, source = _code_reader(store, code_ref)
    if read is None:
        return {"status": "unchecked", "reviews": [f"{t['id']} {review_mark(t)}" for t in passing], "reason": source}
    contents: dict[str, bytes | None] = {}

    def current(path: str, kind: str, ignored: list[str]) -> str | None:
        if path not in contents:
            contents[path] = read(path)
        data = contents[path]
        if data is None:
            return None
        return _config_digest(path, data, ignored) if kind == "configs" else digest_bytes(data)

    seen: dict[str, list[str]] = {}  # path -> passing reviews that saw the version this run executes
    older: dict[str, list[str]] = {}  # path -> passing reviews that saw another version
    seen_by: dict[str, list[str]] = {}  # changed path -> passing reviews of other experiments that saw this version
    try:
        for t in passing:
            for path, kind, digest, ignored in _scope_items(t):
                (seen if current(path, kind, ignored) == digest else older).setdefault(path, []).append(t["id"])
        reasons: dict[str, dict[str, list[str]]] = {"changed": {}, "absent": {}, "tests": {}}
        for path, ids in sorted(older.items()):
            if path not in seen:
                reason = "absent" if contents[path] is None else "tests" if is_test_path(path) else "changed"
                reasons[reason][path] = ids
        mine = {t["id"] for t in tickets}
        for t in everyone:
            if t["id"] not in mine and review_passes(t):
                for path, kind, digest, ignored in _scope_items(t):
                    if path in reasons["changed"] and current(path, kind, ignored) == digest:
                        seen_by.setdefault(path, []).append(t["id"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unchecked", "reviews": [f"{t['id']} {review_mark(t)}" for t in passing],
                "reason": f"could not read the {source}: {exc}"}
    used = {i for ids in seen.values() for i in ids}
    if reasons["changed"]:
        status = "stale"
    elif not seen and (reasons["absent"] or reasons["tests"]):
        status = "none"
    else:
        status = "covered"
    out: dict = {"status": status, "source": source}
    if status != "none":
        out["reviews"] = [f"{t['id']} {review_mark(t)}" for t in passing if t["id"] in used]
    out.update({key: paths for key, paths in reasons.items() if paths})
    if seen_by:
        out["seen_by"] = {p: sorted(ids, key=_sort_key) for p, ids in seen_by.items()}
    if len(used) > 1 and not any({p for p, ids in seen.items() if i in ids} == set(seen) for i in used):
        out["per_file"] = True
    rechecked = passing_rechecks(everyone)
    failed = [f"{t['id']} {review_mark(t)}" for t in others if t["status"] == "answered" and t["id"] not in rechecked]
    if failed:
        out["not_passed"] = failed
    pending = [f"{t['id']} {t['status']}" for t in others if t["status"] in ("pending", "running")]
    if pending:
        out["open"] = pending
    return out
