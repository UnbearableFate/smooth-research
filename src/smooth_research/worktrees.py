"""Git worktrees and task branches of researcher sessions.

A researcher works in its own git worktree (one per session, reused with the session) at
<project>/../<project>-worktrees/<name>, on a new branch per task made from the branch of the main checkout. The
coordinator merges an accepted branch with a merge commit, so the commits that recorded runs name stay reachable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .project import linked_worktree, repo_top
from .store import Store
from .util import RLError


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RLError("GIT", f"git {' '.join(args)}: {exc}") from None
    if check and out.returncode != 0:
        raise RLError("GIT", f"git {' '.join(args)} in {cwd}: {(out.stderr or out.stdout).strip()[-400:]}")
    return out.stdout.strip() if out.returncode == 0 else ""


def _top(store: Store) -> Path:
    top = repo_top(store.project.root)
    if top is None or linked_worktree(store.project.root):
        raise RLError("INVALID", f"researcher worktrees need the project in its own git repository: in "
                                 f"{store.project.root} run `git init`, `git add -A` and `git commit`, or open "
                                 "the session with --no-worktree to work in the project directory")
    if not _git(top, "rev-parse", "--verify", "--quiet", "HEAD", check=False):
        raise RLError("INVALID", f"{top} has no commit yet: commit the project first (worktrees start from it)")
    return top


def base_ref(top: Path) -> str:
    """New task branches start from the branch checked out in the main checkout (its commit when detached)."""
    return _git(top, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or _git(top, "rev-parse", "HEAD")


def task_branch(name: str, task_id: str) -> str:
    return f"sr/{name}/{task_id}"


def _has_branch(top: Path, branch: str) -> bool:
    return bool(_git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False))


def _switch(path: Path, branch: str, base: str) -> None:
    """Put the worktree on the task's branch; refuse when uncommitted work would be carried along."""
    current = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current == branch:
        return
    dirty = _git(path, "status", "--porcelain")
    if dirty:
        raise RLError("CONFLICT", f"{path} has uncommitted changes on {current or 'a detached HEAD'}:\n{dirty[:800]}\n"
                                  "commit or discard them before the next task (the coordinator decides which)")
    if _has_branch(path, branch):
        _git(path, "switch", branch)
    else:
        _git(path, "switch", "-c", branch, base)


def prepare_worktree(store: Store, name: str, task_id: str | None,
                     base: str | None = None) -> tuple[Path, Path, str | None]:
    """(worktree, checkout of the project inside it, branch): created on first use, then reused."""
    top = _top(store)
    path = store.project.worktrees_dir / name
    base = base or base_ref(top)
    branch = task_branch(name, task_id) if task_id else None
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if branch and _has_branch(top, branch):
            _git(top, "worktree", "add", str(path), branch)
        elif branch:
            _git(top, "worktree", "add", "-b", branch, str(path), base)
        else:
            _git(top, "worktree", "add", "--detach", str(path), base)
    elif branch:
        _switch(path, branch, base)
    return path, path / store.project.root.relative_to(top), branch


def switch_task(store: Store, worktree: Path, name: str, task_id: str) -> str:
    """Put a session's worktree on the branch of its next task; the branch."""
    branch = task_branch(name, task_id)
    _switch(worktree, branch, base_ref(_top(store)))
    return branch


def remove(store: Store, worktree: str) -> None:
    """Remove a session's worktree; git refuses when it holds uncommitted changes."""
    _git(_top(store), "worktree", "remove", worktree)


def branch_head(store: Store, branch: str) -> tuple[str | None, int]:
    """The commit a task branch points at and how many commits it has that the base lacks; (None, 0) when git
    cannot say."""
    try:
        top = _top(store)
        head = _git(top, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        ahead = _git(top, "rev-list", "--count", f"{base_ref(top)}..{branch}") if head else "0"
    except RLError:
        return None, 0
    return head or None, int(ahead or 0)


def branch_status(store: Store, branch: str) -> str:
    """What the coordinator merges after accepting a task worked on its own branch."""
    try:
        top = _top(store)
        base = base_ref(top)
        ahead = _git(top, "rev-list", "--count", f"{base}..{branch}")
    except RLError as exc:
        return f"branch {branch}: {exc.message}"
    if ahead == "0":
        return f"branch {branch} has nothing {base} lacks (merged, or no commits)"
    return (f"branch {branch} has {ahead} commit(s) not in {base}: merge it with a merge commit, `git merge --no-ff "
            f"{branch}` (no squash or rebase: recorded runs name these commits)")
