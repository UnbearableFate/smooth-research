"""Git for task branches and worktrees, design commits, protected scope and the merge on acceptance (design 6.5).

Every task that is claimed gets the branch sr/T-xxxx and the worktree <project>-worktrees/T-xxxx, made from the
mainline at that moment. Acceptance merges: a merge commit of the delivered commit and the mainline is made
without touching any checkout, the task's tests run on it in a temporary checkout, and only when they pass does
the mainline move to it (a fast-forward of the main checkout); the task branch then follows. Only the CLI moves
the mainline.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .project import Project, linked_worktree, repo_top
from .util import RLError


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RLError("GIT", f"git {' '.join(args)}: {exc}") from None
    if check and out.returncode != 0:
        raise RLError("GIT", f"git {' '.join(args)} in {cwd}: {(out.stderr or out.stdout).strip()[-600:]}")
    return out.stdout.strip() if out.returncode == 0 else ""


def top(project: Project) -> Path:
    """The main checkout of the project's repository."""
    found = repo_top(project.root)
    if found is None or linked_worktree(project.root):
        raise RLError("INVALID", f"task branches need the project in its own git repository: in {project.root} run "
                                 "`git init`, `git add -A` and `git commit`")
    if not git(found, "rev-parse", "--verify", "--quiet", "HEAD", check=False):
        raise RLError("INVALID", f"{found} has no commit yet: commit the project first (task branches start from it)")
    return found


def mainline(project: Project) -> str:
    """The branch checked out in the main checkout: the mainline."""
    name = git(top(project), "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not name:
        raise RLError("INVALID", "the main checkout is on a detached HEAD: check out the mainline branch there")
    return name


def rev(project: Project, ref: str) -> str | None:
    return git(top(project), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) or None


def branch_name(task_id: str) -> str:
    return f"sr/{task_id}"


def prefix(project: Project) -> str:
    """The project's directory inside the repository ('' at its top, else 'sub/dir/')."""
    rel = project.root.resolve().relative_to(top(project).resolve()).as_posix()
    return "" if rel == "." else rel + "/"


def worktree_path(project: Project, task_id: str) -> Path:
    return project.worktrees_dir / task_id


def checkout_in(project: Project, task_id: str) -> Path:
    """The project's directory inside a task's worktree (where its sessions work)."""
    return worktree_path(project, task_id) / prefix(project)


def make_worktree(project: Project, task_id: str) -> tuple[str, Path]:
    """The task's branch and worktree, made from the mainline on first use, then reused."""
    root, branch = top(project), branch_name(task_id)
    path = worktree_path(project, task_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
            git(root, "worktree", "add", str(path), branch)
        else:
            git(root, "worktree", "add", "-b", branch, str(path), mainline(project))
    return branch, path


def head(project: Project, task_id: str) -> str:
    path = worktree_path(project, task_id)
    if not path.exists():
        raise RLError("INVALID", f"{task_id} has no worktree ({path})")
    return git(path, "rev-parse", "HEAD")


def dirty(project: Project, task_id: str) -> list[str]:
    """Tracked files changed and not committed in a task's worktree. Untracked files (run outputs not yet kept,
    scratch) do not count: what a review, a test or a run is of is the committed code; outputs a delivery names are
    checked to be committed on their own (tracked_and_committed)."""
    path = worktree_path(project, task_id)
    out = git(path, "status", "--porcelain", "--untracked-files=no")
    return [line[3:] for line in out.splitlines() if line.strip()]


def show(project: Project, commit: str, path: str) -> str | None:
    """A file of the project (path relative to the project) at a commit, or None when it is not there."""
    try:
        out = subprocess.run(["git", "show", f"{commit}:{prefix(project)}{path}"], cwd=top(project),
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def exists(project: Project, commit: str) -> bool:
    return rev(project, commit) is not None


def changed_paths(project: Project, base: str, tip: str) -> list[str]:
    """Paths (relative to the repository) that differ between two commits."""
    out = git(top(project), "diff", "--name-only", base, tip)
    return [p for p in out.splitlines() if p]


def symbol_source(text: str, symbol: str) -> str | None:
    """The source of a top-level or dotted (Class.method) symbol in Python text, or None when absent."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    body = tree.body
    node = None
    for part in symbol.split("."):
        node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and n.name == part), None)
        if node is None:
            assign = next((n for n in body if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
                isinstance(t, ast.Name) and t.id == part for t in (n.targets if isinstance(n, ast.Assign)
                                                                   else [n.target]))), None)
            return ast.get_source_segment(text, assign) if assign is not None else None
        body = getattr(node, "body", [])
    return ast.get_source_segment(text, node) if node is not None else None


def protected_unit(project: Project, commit: str, unit: str) -> str | None:
    """What a protected unit (path or path::symbol) holds at a commit; None when absent."""
    path, _, symbol = unit.partition("::")
    text = show(project, commit, path)
    if text is None or not symbol:
        return text
    return symbol_source(text, symbol) if path.endswith(".py") else text


def protected_changes(project: Project, base: str, tip: str, units: list[str]) -> list[str]:
    """The protected units that differ between two commits."""
    if base == tip:
        return []
    return [u for u in units if protected_unit(project, base, u) != protected_unit(project, tip, u)]


def diffstat(project: Project, base: str, tip: str, unit: str) -> str:
    path = unit.partition("::")[0]
    out = git(top(project), "diff", "--numstat", base, tip, "--", prefix(project) + path, check=False)
    added = removed = 0
    for line in out.splitlines():
        a, r, _ = (line.split("\t") + ["", ""])[:3]
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    return f"+{added} −{removed}"


def commits_after(project: Project, base: str, tip: str) -> int:
    out = git(top(project), "rev-list", "--count", f"{base}..{tip}", check=False)
    return int(out or 0)


def merge_candidate(project: Project, task_id: str, delivered: str) -> tuple[str | None, list[str], bool]:
    """The commit an acceptance tests and moves the mainline to, made from the delivered commit itself (never from
    whatever the task branch holds now) without touching any checkout: a merge commit of the delivery (first
    parent) and the mainline. (commit, [], contained) or (None, conflicting files, False). A mainline that already
    holds the delivery is its own candidate (contained: decided here, from the one reading of the mainline)."""
    root = top(project)
    main = rev(project, mainline(project))
    if is_ancestor(project, delivered, main):
        return main, [], True
    out = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", delivered, main],
                         cwd=root, capture_output=True, text=True, timeout=120)
    lines = out.stdout.splitlines()
    if out.returncode != 0:
        return None, [x for x in lines[1:] if x.strip()][:20] or [(out.stderr or out.stdout).strip()[-300:]], False
    tree = lines[0].strip()
    commit = git(root, "commit-tree", tree, "-p", delivered, "-p", main, "-m",
                 f"Merge {mainline(project)} into {branch_name(task_id)} for acceptance of {task_id}")
    return commit, [], False


CANDIDATES = "sr-accept/"


def candidate_branch(task_id: str) -> str:
    return CANDIDATES + task_id


def set_candidates(project: Project, wanted: dict[str, str]) -> None:
    """Make the candidate branches exactly wanted (task id -> the candidate its acceptance waits on with a test
    job): a remote job fetches its candidate through it (compute sync pushes these branches). The caller holds the
    records' write lock and derives wanted from them, so the branches follow the records, never a stale process."""
    root = top(project)
    have = {b[len(CANDIDATES):]: rev(project, b) for b in candidate_branches(root)}
    for tid, commit in wanted.items():
        if have.get(tid) != commit:
            git(root, "update-ref", f"refs/heads/{candidate_branch(tid)}", commit)
    for tid in set(have) - set(wanted):
        git(root, "update-ref", "-d", f"refs/heads/{candidate_branch(tid)}", check=False)


def candidate_branches(checkout: Path) -> list[str]:
    return git(checkout, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{CANDIDATES}",
               check=False).split()


@contextmanager
def temp_checkout(project: Project, commit: str) -> Iterator[Path]:
    """A detached checkout of commit for a test run, removed afterwards; yields the project's directory in it."""
    root = top(project)
    base = Path(tempfile.mkdtemp(prefix="sr-accept-", dir=project.worktrees_dir.parent))
    path = base / "co"
    git(root, "worktree", "add", "--detach", str(path), commit)
    try:
        yield path / prefix(project)
    finally:
        git(root, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(base, ignore_errors=True)


def follow(project: Project, task_id: str, delivered: str, commit: str) -> None:
    """After acceptance, bring the task branch to the merge commit when its worktree still stands at the
    delivery (best effort: a branch that moved on is left as it is)."""
    path = worktree_path(project, task_id)
    if path.exists() and git(path, "rev-parse", "HEAD", check=False) == delivered and not dirty(project, task_id):
        git(path, "merge", "--ff-only", commit, check=False)


def move_mainline(project: Project, commit: str) -> str | None:
    """Fast-forward the mainline (in the main checkout) to commit; why not, or None when it moved."""
    root = top(project)
    out = subprocess.run(["git", "merge", "--ff-only", commit], cwd=root, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return (out.stderr or out.stdout).strip()[-400:]
    return None


def only_under(project: Project, base: str, tip: str, allowed: str) -> list[str]:
    """Paths changed between base and tip outside the project-relative directory allowed."""
    want = prefix(project) + allowed.rstrip("/") + "/"
    return [p for p in changed_paths(project, base, tip) if not p.startswith(want)]


def fork_point(project: Project, task_id: str) -> str | None:
    """Where the task branch left the mainline."""
    return git(top(project), "merge-base", mainline(project), branch_name(task_id), check=False) or None


def remove_worktree(project: Project, task_id: str) -> None:
    path = worktree_path(project, task_id)
    if path.exists():
        git(top(project), "worktree", "remove", str(path), check=False)


def tracked_and_committed(project: Project, task_id: str, paths: list[str]) -> list[str]:
    """Of the project-relative paths, those not committed unchanged at the task's head."""
    wt = checkout_in(project, task_id)
    bad = []
    for p in paths:
        listed = subprocess.run(["git", "ls-files", "--error-unmatch", p], cwd=wt, capture_output=True, text=True)
        changed = subprocess.run(["git", "status", "--porcelain", "--", p], cwd=wt, capture_output=True, text=True)
        if listed.returncode != 0 or changed.stdout.strip():
            bad.append(p)
    return bad


def commit_paths(project: Project, paths: list[str], message: str) -> str:
    """Commit files of the main checkout on the mainline (the coordinator's explanations and glossary, 7.7)."""
    root = top(project)
    rels = [str((project.root / p).resolve().relative_to(root.resolve())) for p in paths]
    git(root, "add", "--", *rels)
    if not git(root, "diff", "--cached", "--name-only", "--", *rels):
        return git(root, "rev-parse", "HEAD")
    git(root, "commit", "-m", message, "--", *rels)
    return git(root, "rev-parse", "HEAD")


def is_ancestor(project: Project, commit: str, tip: str) -> bool:
    out = subprocess.run(["git", "merge-base", "--is-ancestor", commit, tip], cwd=top(project), capture_output=True)
    return out.returncode == 0
