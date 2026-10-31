"""Compute resources: the compute section of smooth-research.yaml, attempts registered for a resource, and the
staging steps rl prints. The steps are run here against local directories that stand in for the remote host, so
the snapshot a remote job would run is checked, not only the text."""

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
        "box": {"host": "box", "workspace": "/data/p", "connect": "telnet", "code": "rsync", "jobs": "slurm",
                "typo": 1},
        "relative": {"host": "h", "workspace": "work/p"},
        "nohost": {"kind": "remote", "workspace": "/w"},
        "here": {"kind": "local", "workspace": "/x"},
        "bad name!": {"kind": "local"}}})
    found, warnings = compute_resources(cfg)
    assert sorted(found) == ["box", "here"]
    box = found["box"]
    assert (box.connect, box.code, box.jobs) == ("ssh", "git", "shell")
    text = "\n".join(warnings)
    for part in ("unknown key compute.resources.box.typo", "box.connect must be ssh, pi-env-mcp",
                 "box.code must be git or archive", "box.jobs must be shell, mcp:<server> or skill:<name>",
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
                 "Its project directory must be /work/g/u/demo",
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
