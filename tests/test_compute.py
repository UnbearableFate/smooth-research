"""Compute resources: the compute section of smooth-research.yaml, attempts registered for a resource, and the
staging steps rl prints. The steps are run here against local directories that stand in for the remote host, so
the snapshot a remote job would run is checked, not only the text."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from smooth_research import compute, context, runs, tasks
from smooth_research.project import DEFAULT_CONFIG, _merge, compute_default, compute_resources, config_warnings, \
    find_project
from smooth_research.store import Store
from smooth_research.util import RLError

from conftest import SPEC


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.org"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(key, value)


def configure(root: Path, resources: dict, default: str | None = None) -> Store:
    path = root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config["compute"] = {"resources": resources, **({"default": default} if default else {})}
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return Store(find_project(), "test")


def config(compute_section: dict) -> dict:
    return _merge(DEFAULT_CONFIG, {"compute": compute_section})


def test_the_default_is_this_machine_and_the_users_resources_replace_it():
    found, warnings = compute_resources(DEFAULT_CONFIG)
    assert list(found) == ["local"] and not found["local"].remote and warnings == []
    cfg = config({"default": "hpc", "resources": {"hpc": {"host": "miyabi-g", "workspace": "/work/g/u/p",
                                                          "connect": "pi-env-mcp:miyabi-g", "jobs": "mcp:miyabi-pbs"}}})
    found, warnings = compute_resources(cfg)
    assert list(found) == ["hpc"] and warnings == [] and config_warnings(cfg) == []
    hpc = found["hpc"]
    assert (hpc.kind, hpc.connect, hpc.server, hpc.jobs, hpc.code, hpc.git_remote) == \
        ("remote", "pi-env-mcp", "miyabi-g", "mcp:miyabi-pbs", "git", "origin")
    assert compute_default(cfg) == "hpc"


def test_resource_problems_warn_and_say_what_is_used_instead():
    cfg = config({"default": "nowhere", "resources": {
        "box": {"host": "box", "workspace": "/data/p", "code": "rsync",
                "typo": 1},
        "badconnect": {"host": "box", "workspace": "/data/p", "connect": "telnet"},
        "badjobs": {"host": "box", "workspace": "/data/p", "jobs": "slurm"},
        "relative": {"host": "h", "workspace": "work/p"},
        "nohost": {"kind": "remote", "workspace": "/w"},
        "here": {"kind": "local", "workspace": "/x"},
        "bad name!": {"kind": "local"}}})
    found, warnings = compute_resources(cfg)
    assert sorted(found) == ["box", "here"]
    box = found["box"]
    assert (box.connect, box.code, box.jobs) == ("ssh", "git", "shell")
    text = "\n".join(warnings)
    for part in ("unknown key compute.resources.box.typo", "badconnect.connect must be ssh, pi-env-mcp",
                 "box.code must be git or archive", "badjobs.jobs must be shell, mcp:<server> or skill:<name>",
                 "relative.workspace must be an absolute remote path", "nohost: a remote resource needs host",
                 "here: workspace has no effect on a local resource", "bad name!: a resource name is",
                 "compute.default 'nowhere' is not a usable resource; using box"):
        assert part in text, part
    assert compute_default(cfg) == "box"
    assert all(w.startswith("smooth-research.yaml: ") for w in config_warnings(cfg) if "compute" in w)


def _repo(project, tmp_path):
    """The project as a git repository with a bare repository standing in for GitHub, and a researcher worktree
    with one more commit on its task branch."""
    github = tmp_path.parent / f"{tmp_path.name}-github.git"
    git(tmp_path.parent, "init", "-q", "--bare", str(github))
    (tmp_path / "src/train.py").write_text("print('v1')\n")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "start")
    git(tmp_path, "remote", "add", "origin", str(github))
    wt = tmp_path.parent / f"{tmp_path.name}-worktrees" / "researcher-1"
    git(tmp_path, "worktree", "add", "-q", "-b", "sr/researcher-1/T-0001", str(wt))
    (wt / "src/train.py").write_text("print('v2')\n")
    git(wt, "commit", "-q", "-am", "v2")
    return wt, git(wt, "rev-parse", "HEAD")


def _heredoc(summary: str) -> str:
    lines = summary.splitlines()
    start = next(i for i, line in enumerate(lines) if line.endswith("sh -s <<'SR_EOF'"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "SR_EOF")
    return "\n".join(line[3:] for line in lines[start + 1:end])


def _is_read_only(path: Path) -> bool:
    return not any(p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) for p in [path, *path.rglob("*")])


def test_a_worktree_commit_is_pushed_fetched_by_the_host_and_staged_read_only(project, tmp_path, monkeypatch):
    remote_ws = tmp_path.parent / f"{tmp_path.name}-remote"
    configure(tmp_path, {"local": {"kind": "local"},
                         "box": {"host": "box", "workspace": str(remote_ws), "code": "git"}}, default="box")
    wt, sha = _repo(project, tmp_path)
    monkeypatch.chdir(wt)  # the researcher's checkout
    store = Store(find_project(), "researcher-1")
    assert store.project.in_worktree
    out = runs.register(store, "E-0001", {**SPEC, "code_ref": {"commit": sha}}, "r1", no_review="smoke",
                        resource="box")
    att = out["attempt"]
    placed = out["resource"]
    assert placed["name"] == "box" and placed["snapshot"] == f"{remote_ws}/snapshots/{sha}"
    assert placed["run_dir"] == f"{remote_ws}/runs/{att}" and f"rl compute steps --attempt {att}" in out["summary"]
    with store.read() as tx:
        assert tx.require(att)["resource"] == placed
        assert f"{att} [prepared/unknown/result:absent] on box" in runs.describe_attempt(tx, tx.require(att))

    before = compute.steps(store, attempt=att)
    assert before["commit"] == sha and before["problems"] == []
    assert "git push origin sr/researcher-1/T-0001" in before["summary"]
    assert f"SR_ATTEMPT_ID={att} SR_SPEC_DIGEST={out['spec_digest']}" in before["summary"]
    git(wt, "push", "-q", "origin", "sr/researcher-1/T-0001")
    after = compute.steps(store, attempt=att)
    assert "Already on origin/sr/researcher-1/T-0001" in after["summary"]

    script = _heredoc(after["summary"])  # what ssh would run on the host
    for _ in range(2):  # the same commit is staged once
        done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    assert "already staged" in done.stdout
    snap = Path(placed["snapshot"])
    assert (snap / "src/train.py").read_text() == "print('v2')\n" and _is_read_only(snap)
    assert (remote_ws / "repo.git").is_dir() and (remote_ws / "runs").is_dir()
    git(wt, "commit", "-q", "--allow-empty", "-m", "later")  # the branch moves on: the snapshot does not
    assert (snap / "src/train.py").read_text() == "print('v2')\n"


def test_an_archive_is_streamed_over_ssh_and_staged_once(project, tmp_path):
    remote_ws = tmp_path.parent / f"{tmp_path.name}-remote-arc"
    store = configure(tmp_path, {"box": {"host": "box", "workspace": str(remote_ws), "code": "archive"}})
    wt, sha = _repo(project, tmp_path)
    out = compute.steps(store, "box", commit=sha)
    line = next(x for x in out["summary"].splitlines() if "archive --format=tar" in x).strip()
    assert "| ssh -T -o BatchMode=yes box " in line
    local = line.replace("ssh -T -o BatchMode=yes box", "sh -c")  # sh -c parses the command line as ssh's remote shell does
    for _ in range(2):
        done = subprocess.run(["bash", "-c", local], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    assert "already staged" in done.stdout
    snap = remote_ws / "snapshots" / sha
    assert (snap / "src/train.py").read_text() == "print('v2')\n" and _is_read_only(snap)
    assert sorted(p.name for p in (remote_ws / "snapshots").iterdir()) == [sha]


def test_pi_env_mcp_steps_name_its_server_and_the_job_tool(project, tmp_path):
    store = configure(tmp_path, {"miyabi": {"host": "miyabi-g", "connect": "pi-env-mcp:miyabi-g",
                                            "jobs": "mcp:miyabi-pbs", "workspace": "/work/g/u/demo",
                                            "code": "archive"}})
    _, sha = _repo(project, tmp_path)
    text = compute.steps(store, commit=sha)["summary"]
    for part in (f"git -C {tmp_path} archive --format=tar {sha} -o",
                 f"pi-env-mcp upload (server miyabi-g, host miyabi-g): {tmp_path}/research/artifacts/snapshots/{sha}.tar "
                 f"-> /work/g/u/demo/snapshots/{sha}.tar",
                 "pi-env-mcp bash (server miyabi-g, host miyabi-g)",
                 "Submit with MCP server miyabi-pbs", "request key sr:<ATT>",
                 "Use /work/g/u/demo as the executor's project workspace if it requires one",
                 "pi-env-mcp download (server miyabi-g, host miyabi-g): /work/g/u/demo/runs/<ATT>/manifest.json"):
        assert part in text, part


def test_a_remote_attempt_without_a_commit_says_so_instead_of_staging_head(project, tmp_path):
    store = configure(tmp_path, {"box": {"host": "box", "workspace": "/w/p"}})
    _repo(project, tmp_path)
    out = runs.register(store, "E-0001", SPEC, "r1", no_review="smoke", resource="box")
    assert "--code-commit" in out["summary"] and out["resource"]["snapshot"] == "/w/p/snapshots/<commit>"
    steps = compute.steps(store, attempt=out["attempt"])
    assert steps["commit"] is None and "names no commit" in steps["problems"][0]


def test_register_refuses_an_unknown_resource_and_keys_requests_by_resource(project, tmp_path):
    store = configure(tmp_path, {"local": {"kind": "local"}, "box": {"host": "box", "workspace": "/w/p"}})
    with pytest.raises(RLError) as exc:
        runs.register(store, "E-0001", SPEC, "r1", no_review="smoke", resource="nowhere")
    assert exc.value.code == "NOT_FOUND" and "configured: local, box" in exc.value.message
    first = runs.register(store, "E-0001", SPEC, "r1", no_review="smoke", resource="local")
    assert first["resource"] == {"name": "local", "kind": "local", "jobs": "shell", "checkout": str(tmp_path),
                                 "run_dir": f"research/artifacts/{first['attempt']}"}
    assert runs.register(store, "E-0001", SPEC, "r1", no_review="smoke", resource="local")["duplicate"]
    with pytest.raises(RLError) as exc:
        runs.register(store, "E-0001", SPEC, "r1", no_review="smoke", resource="box")
    assert exc.value.code == "CONFLICT"
    plain = runs.register(store, "E-0001", SPEC, "r2", no_review="smoke")
    assert plain["resource"] is None
    with store.read() as tx:
        assert "resource" not in tx.require(plain["attempt"])
        assert " on " not in runs.describe_attempt(tx, tx.require(plain["attempt"]))


def test_work_and_resume_packs_list_the_resources(project, tmp_path):
    store = configure(tmp_path, {"miyabi": {"host": "miyabi-g", "connect": "pi-env-mcp:miyabi-g",
                                            "jobs": "mcp:miyabi-pbs", "workspace": "/work/g/u/demo"}})
    tasks.create_task(store, "run", "experiment", ["E-0001"], to_role="researcher")
    for pack in (context.work_pack(store, "T-0001")["summary"], context.resume_pack(store)["summary"]):
        assert "## Compute resources (smooth-research.yaml)" in pack
        assert "miyabi: host miyabi-g via pi-env-mcp (server miyabi-g) · jobs mcp:miyabi-pbs" in pack
        assert "Python packages: one committed pyproject.toml and uv.lock for every resource" in pack
    store = configure(tmp_path, {"local": {"kind": "local"}})  # nothing remote: no reminder
    assert "Python packages" not in context.work_pack(store, "T-0001")["summary"]


def test_rl_compute_lists_resources_with_local_checks(project, tmp_path, rl):
    configure(tmp_path, {"local": {"kind": "local"}, "box": {"host": "box", "workspace": "/w/p"}}, default="box")
    _, sha = _repo(project, tmp_path)
    code, out, _ = rl("compute", "--commit", sha)
    assert code == 0 and "default: box" in out and "git remote origin: " in out
    assert f"{sha[:12]} not pushed to origin" in out
    code, out, _ = rl("compute", "steps", "box", "--commit", sha)
    assert code == 0 and f"snapshots/{sha}" in out
    code, _, err = rl("compute", "--attempt", "ATT-0001-1")
    assert code != 0 and "rl compute steps" in err


# -- sync: push for the resources that fetch code by git, and their clone step -----------------------------------

FAKE_GH = """#!/bin/sh
# gh repo create NAME --private --source DIR --remote R, with a local bare repository standing in for GitHub
echo "$*" >> "$FAKE_GITHUB/calls"
[ "$1" = repo ] && [ "$2" = create ] || exit 2
name=$3; shift 3
while [ $# -gt 0 ]; do case "$1" in --source) src=$2; shift 2;; --remote) remote=$2; shift 2;; *) shift;; esac; done
if [ -e "$FAKE_GITHUB/$name.git" ]; then echo "GraphQL: Name already exists on this account" >&2; exit 1; fi
git init -q --bare "$FAKE_GITHUB/$name.git"
git -C "$src" remote add "$remote" "$FAKE_GITHUB/$name.git"
echo "https://github.com/me/$name"
"""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A gh on PATH that creates local bare repositories; returns their directory (with calls: its arguments)."""
    hub = tmp_path.parent / f"{tmp_path.name}-hub"
    bin_dir = tmp_path.parent / f"{tmp_path.name}-bin"
    hub.mkdir()
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GITHUB", str(hub))
    return hub


def _git_resource(root: Path, tmp_path: Path, **compute_extra) -> tuple[Store, Path]:
    ws = tmp_path.parent / f"{tmp_path.name}-remote"
    path = root / "smooth-research.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["compute"] = {"default": "box", **compute_extra, "resources": {
        "local": {"kind": "local"}, "box": {"host": "box", "workspace": str(ws), "code": "git"},
        "upload": {"host": "box", "workspace": str(ws) + "-archive", "code": "archive"}}}
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return Store(find_project(), "coordinator"), ws


def _commit(root: Path):
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "start")
    return git(root, "rev-parse", "HEAD")


def test_session_start_creates_a_private_repository_pushes_and_prints_the_clone(project, tmp_path, fake_gh, rl):
    store, ws = _git_resource(tmp_path, tmp_path)
    sha = _commit(tmp_path)
    code, out, err = rl("session", "start", "--role", "coordinator", "--host", "other")
    assert code == 0, err
    name = tmp_path.name
    assert (fake_gh / "calls").read_text().split() == ["repo", "create", name, "--private", "--source",
                                                       str(tmp_path), "--remote", "origin"]
    assert f"created the private GitHub repository {fake_gh / name}.git as git remote origin" in out
    assert f"pushed main ({sha[:12]}) to origin" in out and "problem:" not in out
    assert git(fake_gh / f"{name}.git", "rev-parse", "main") == sha
    assert git(tmp_path, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    assert "box: run now, to clone the repository" in out and "upload" not in out  # archive: nothing to sync
    script = _heredoc(out)  # what ssh would run on the host
    for _ in range(2):  # safe to repeat: clones once, then fetches
        done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert f"ready: {ws}/repo.git has {sha[:12]}" in done.stdout
    assert git(ws / "repo.git", "rev-parse", "main") == sha and (ws / "snapshots").is_dir()
    with store.read() as tx:
        kinds = [e["type"] for e in tx.all("Event")]
    assert kinds.count("compute.repository") == 1 and kinds.count("compute.push") == 1

    (tmp_path / "src/train.py").write_text("print('v1')\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "train")
    (tmp_path / "src/train.py").write_text("print('v2')\n")
    code, out, err = rl("compute", "sync")  # again: the remote exists, the new commit is pushed
    new = git(tmp_path, "rev-parse", "HEAD")
    assert code == 0 and "created" not in out and f"pushed main ({new[:12]})" in out
    assert "uncommitted changes to tracked files are not pushed" in out
    assert len((fake_gh / "calls").read_text().splitlines()) == 1
    code, out, err = rl("compute", "sync")  # nothing new: said, not recorded again
    assert code == 0 and f"main ({new[:12]}) is already on origin" in out
    with store.read() as tx:
        assert [e["type"] for e in tx.all("Event")].count("compute.push") == 2


def test_sync_reports_what_it_cannot_do_and_session_start_goes_on(project, tmp_path, fake_gh, rl):
    store, ws = _git_resource(tmp_path, tmp_path, repository="none")
    code, out, err = rl("session", "start", "--role", "coordinator", "--host", "other")
    assert code == 0 and "problem: " in out and "is in no git repository" in out

    _commit(tmp_path)
    out = compute.sync(store)
    assert "compute.repository is none: ask the user" in out["summary"] and not (fake_gh / "calls").exists()
    assert out["pushed"] == [] and "clone" not in out["summary"]

    git(tmp_path, "remote", "add", "origin", str(tmp_path.parent / "missing.git"))
    code, out, err = rl("session", "start", "--role", "coordinator", "--host", "other")
    assert code == 0 and "problem: git push origin main failed" in out and "never force" in out
    code, out, err = rl("session", "start", "--role", "coordinator", "--host", "other", "--no-sync")
    assert code == 0 and "compute sync" not in out
    code, out, err = rl("compute", "sync", "upload")
    assert code == 0 and "upload does not fetch code by git" in out


def test_no_commit_yet_or_a_project_inside_a_larger_repository_gets_no_new_repository(tmp_path, fake_gh,
                                                                                       monkeypatch):
    from smooth_research import research
    outer = tmp_path / "outer"
    root = outer / "demos" / "p"
    root.mkdir(parents=True)
    (root / "plan.md").write_text("# Plan\n", encoding="utf-8")
    research.init_project(root, str(root / "plan.md"), "t")
    monkeypatch.chdir(root)
    store, _ = _git_resource(root, tmp_path)
    git(outer, "init", "-q", "-b", "main")
    assert "no commit yet in" in compute.sync(store)["summary"]
    git(outer, "add", ".")
    git(outer, "commit", "-q", "-m", "start")
    out = compute.sync(store)
    assert f"the project is a part of the repository {outer}, so rl creates none" in out["summary"]
    assert not (fake_gh / "calls").exists()


def test_session_start_without_git_resources_says_nothing_about_sync(project, rl):
    code, out, err = rl("session", "start", "--role", "coordinator", "--host", "other")
    assert code == 0 and "compute sync" not in out and "problem" not in out


def test_an_unknown_repository_setting_warns_and_creates_nothing():
    from smooth_research.project import compute_repository
    cfg = config({"repository": "gitlab"})
    assert compute_repository(cfg) == "none" and compute_repository(DEFAULT_CONFIG) == "github-private"
    assert any("compute.repository must be github-private or none, not 'gitlab'" in w for w in config_warnings(cfg))


@pytest.mark.parametrize("coordinator", ["login", "logni", None])
def test_login_policy_never_falls_back_to_local_execution(coordinator):
    cfg = config({"coordinator": coordinator})
    found, warnings = compute_resources(cfg)
    assert found == {} and compute_default(cfg) is None
    assert any("coordinator is a login node" in w for w in warnings)
    if coordinator != "login":
        assert any("treating it as login" in w for w in warnings)


@pytest.mark.parametrize("extra", [{}, {"jobs": "shell"}, {"jobs": "mcp:"},
                                  {"jobs": "mcp:batch", "code": "git"},
                                  {"jobs": "mcp:batch", "connect": "local:batch"}])
def test_invalid_shared_cluster_resources_offer_no_shell_fallback(extra):
    cfg = config({"coordinator": "login", "resources": {
        "cluster": {"kind": "remote", "connect": "local", "workspace": "/shared/project", **extra}}})
    found, warnings = compute_resources(cfg)
    assert found == {} and warnings and compute_default(cfg) is None


def test_login_shared_snapshot_uses_worktree_commit_without_network(project, tmp_path, monkeypatch, rl):
    workspace = tmp_path.parent / f"{tmp_path.name} shared"
    configure(tmp_path, {"cluster": {"kind": "remote", "connect": "local", "jobs": "mcp:batch",
                                     "workspace": str(workspace)}}, default="cluster")
    path = tmp_path / "smooth-research.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["compute"]["coordinator"] = "login"
    path.write_text(yaml.safe_dump(cfg))
    wt, sha = _repo(project, tmp_path)
    git(tmp_path, "remote", "remove", "origin")
    monkeypatch.chdir(wt)
    store = Store(find_project(), "researcher-1")
    assert not store.project.warnings
    res = compute.resource(store.project, "cluster")
    assert res.remote and res.connect == "local" and res.code == "archive" and res.host is None
    assert compute.sync(store)["summary"] == ""
    with pytest.raises(RLError, match="register with --resource"):
        runs.register(store, "E-0001", SPEC, "missing", no_review="smoke")
    out = runs.register(store, "E-0001", {**SPEC, "code_ref": {"commit": sha}}, "shared", no_review="smoke",
                        resource="cluster")
    steps = compute.steps(store, attempt=out["attempt"])
    assert steps["problems"] == []
    assert out["resource"]["connect"] == "local" and out["resource"]["workspace"] == str(workspace)
    summary = steps["summary"]
    for part in ("Coordinator is on a login node", "Submit with MCP server batch", "--exec-kind hpc_job",
                 "no download is needed", f"SR_SPEC_DIGEST={out['spec_digest']}", f"sr:{out['attempt']}"):
        assert part in summary
    for forbidden in ("ssh -", "scp ", "git push origin", "local_process", "--exec-id <pid>"):
        assert forbidden not in summary
    script = _heredoc(summary)
    for _ in range(2):
        done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    assert "already staged" in done.stdout
    snap = Path(out["resource"]["snapshot"])
    assert (snap / "src/train.py").read_text() == "print('v2')\n" and _is_read_only(snap)
    git(wt, "commit", "-q", "--allow-empty", "-m", "later")
    assert compute.steps(store, attempt=out["attempt"])["commit"] == sha
    tasks.create_task(store, "check", "experiment", ["E-0001"], to_role="researcher")
    for pack in (context.work_pack(store, "T-0001")["summary"], context.resume_pack(store)["summary"]):
        assert "Coordinator is on a login node" in pack and "mcp:batch" in pack
    from smooth_research import hooks
    assert "Coordinator is on a login node" in hooks.session_start("codex", "")["hookSpecificOutput"]["additionalContext"]
    run_dir = Path(out["resource"]["run_dir"])
    run_dir.mkdir()
    metrics = run_dir / "metrics.json"
    metrics.write_text('{"acc": 1.0}')
    manifest = run_dir / "manifest.json"
    manifest.write_text(json.dumps({"status": "complete", "spec_digest": out["spec_digest"],
                                    "artifacts": [{"path": str(metrics)}]}))
    code, imported, err = rl("run", "import-result", "--attempt", out["attempt"], "--request-id", "shared-result",
                              "--manifest", f"@{manifest}")
    assert code == 0 and "1 read_locally" in imported, (imported, err)


def test_local_job_executor_is_not_recorded_as_a_shell_pid(project, tmp_path):
    store = configure(tmp_path, {"batch": {"kind": "local", "jobs": "mcp:batch"}})
    text = compute.steps(store)["summary"]
    assert "Submit with MCP server batch" in text and "--exec-kind hpc_job" in text
    assert "local_process" not in text and "--exec-id <pid>" not in text


def test_a_job_id_typed_by_hand_for_an_executor_resource_points_to_its_export(project, tmp_path, rl):
    store = configure(tmp_path, {"miyabi": {"host": "miyabi-g", "connect": "pi-env-mcp:miyabi-g",
                                            "jobs": "mcp:miyabi-pbs", "workspace": "/work/g/u/demo"},
                                 "box": {"kind": "local"}}, default="box")
    remote = runs.register(store, "E-0001", SPEC, "r1", no_review="smoke", resource="miyabi")["attempt"]
    here = runs.register(store, "E-0001", {**SPEC, "seed": 1}, "r2", no_review="smoke", resource="box")["attempt"]
    code, out, _ = rl("run", "record", "--attempt", remote, "--source", "agent", "--state", "queued",
                      "--exec-kind", "hpc_job", "--exec-namespace", "miyabi-g-pbs", "--exec-id", "3786622.opbs")
    assert code == 0 and f"warning: {remote} runs on miyabi, whose jobs mcp:miyabi-pbs submits: take the job id " \
        "from its export (rl run record --observation @<export.jsonl>) rather than typing it; a wrong one is " \
        "corrected with --correction-of the observation that recorded it" in out
    code, out, _ = rl("run", "record", "--attempt", here, "--source", "shell", "--state", "running",
                      "--exec-namespace", "box:boot-1", "--exec-id", "4242")
    assert code == 0 and "warning" not in out
