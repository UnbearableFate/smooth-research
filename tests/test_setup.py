"""bin/rl runs rl on the research project's own Python environment. rl init makes it first (uv init --python
<version> --no-workspace, then uv add of rl's dependencies, which creates .venv) and only then creates the records
on it; later commands run there too. These tests drive the real launcher with uv."""

import json
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "plugin" / "bin" / "rl"
DEPS = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]


@pytest.fixture(autouse=True)
def plain_env(monkeypatch):
    monkeypatch.delenv("SMOOTH_RESEARCH_PROJECT", raising=False)
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.org"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(key, value)


def rl(cwd, *args):
    return subprocess.run([str(LAUNCHER), *map(str, args)], cwd=cwd, capture_output=True, text=True, timeout=600)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def init(root, *extra):
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text("# Plan\nA question.\n", encoding="utf-8")
    return rl(root, "init", "--plan", "plan.md", *extra)


def dependencies(root):
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]


def test_init_makes_the_project_environment_before_the_records(tmp_path):
    root = tmp_path / "noise-study"
    done = init(root)
    assert done.returncode == 0, done.stderr
    meta = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert meta["name"] == "noise-study" and meta["requires-python"] == ">=3.13"
    assert set(DEPS) <= set(dependencies(root)) and (root / "uv.lock").is_file()
    assert (root / ".python-version").read_text().strip() == "3.13"
    assert (root / "src/noise_study/__init__.py").is_file() and (root / "research/state/research.lattice").is_file()
    assert (root / ".venv/.smooth-research-deps").read_text() == "\n".join(DEPS)
    assert (root / ".git").is_dir() and ".venv" in (root / ".gitignore").read_text().split()
    assert "made " in done.stderr and "commit pyproject.toml and uv.lock" in done.stderr
    assert "environment: Python 3.13" in done.stdout and "on the project's .venv" in done.stdout
    assert "commit the new project in its root" in done.stdout

    for cwd in (root, root / "src"):  # later commands, also from below the root, run on the same environment
        later = rl(cwd, "upgrade")
        assert later.returncode == 0 and "on the project's .venv" in later.stdout, later.stderr
        assert later.stderr == ""  # nothing to build
    again = rl(root, "init", "--plan", "plan.md")
    assert again.returncode == 0 and "already initialized" in again.stdout and again.stderr == ""

    # a background worker (review, advice, delegation) runs rl on the same environment, which does not install the
    # package: a manual entry calls no model, so the worker only records that it waits for a pasted answer
    assert rl(root, "advise", "prepare", "--question", "which route?").returncode == 0
    started = rl(root, "advise", "run", "AD-0001", "--background", "--entry", "manual:chosen-model", "--json")
    assert started.returncode == 0, started.stderr
    log = root / json.loads(started.stdout)["log"]
    for _ in range(300):
        if "to chosen-model yourself" in log.read_text() or "Error" in log.read_text():
            break
        time.sleep(0.1)
    assert "to chosen-model yourself" in log.read_text(), log.read_text()


def test_a_project_inside_another_uv_project_gets_its_own_environment(tmp_path):
    parent = tmp_path / "tool"
    parent.mkdir()
    (parent / "pyproject.toml").write_text('[project]\nname = "tool"\nversion = "0.1.0"\n', encoding="utf-8")
    git(parent, "init", "-q")
    before = (parent / "pyproject.toml").read_text()
    root = parent / "demos" / "demo-1"
    done = init(root)
    assert done.returncode == 0, done.stderr
    assert (parent / "pyproject.toml").read_text() == before  # not made a member of the outer workspace
    assert (root / ".venv/bin/python").exists() and not (parent / ".venv").exists()
    # uv runs no git init inside a repository and writes no .gitignore there: rl writes uv's
    assert not (root / ".git").exists() and ".venv" in (root / ".gitignore").read_text().split()


def test_an_existing_pyproject_and_gitignore_are_kept_and_extended(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "mine"\nversion = "1.0"\nrequires-python = ">=3.13"\n'
                                         'dependencies = []\n', encoding="utf-8")
    git(root, "init", "-q")
    (root / ".gitignore").write_text("data/", encoding="utf-8")
    done = init(root)
    assert done.returncode == 0, done.stderr
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"] == "mine"
    assert set(DEPS) <= set(dependencies(root)) and "made " not in done.stderr
    assert (root / ".gitignore").read_text().splitlines() == ["data/", "# uv environment", ".venv"]


def test_a_python_older_than_rl_needs_is_refused(tmp_path):
    done = init(tmp_path / "p", "--python", "3.12")
    assert done.returncode == 2 and "rl needs Python 3.13 or newer" in done.stderr
    assert not (tmp_path / "p/pyproject.toml").exists() and not (tmp_path / "p/research").exists()


def test_new_dependencies_after_a_plugin_update_are_added_on_first_use(tmp_path):
    root = tmp_path / "p"
    assert init(root).returncode == 0
    stamp = root / ".venv/.smooth-research-deps"
    stamp.write_text("latticedb==0.14.0", encoding="utf-8")  # what an older plugin needed
    status = rl(root, "usage", "statusline", "--json")  # the status line never waits for a build
    assert status.returncode == 0 and status.stdout == "" and stamp.read_text() == "latticedb==0.14.0"
    done = rl(root, "status")
    assert done.returncode == 0, done.stderr
    assert "commit pyproject.toml and uv.lock" in done.stderr and stamp.read_text() == "\n".join(DEPS)


def test_a_researcher_worktree_runs_on_the_main_checkouts_environment(tmp_path):
    root = tmp_path / "p"
    assert init(root).returncode == 0
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "start")
    wt = tmp_path / "p-worktrees" / "researcher-1"
    git(root, "worktree", "add", "-q", "-b", "sr/researcher-1/T-0001", str(wt))
    done = rl(wt / "src", "upgrade")
    assert done.returncode == 0, done.stderr
    assert "on the project's .venv" in done.stdout and done.stderr == ""
    assert not (wt / ".venv").exists()


def test_a_project_made_before_its_own_environment_runs_on_the_plugins_until_rl_init(tmp_path):
    from smooth_research import research
    root = tmp_path / "old"
    root.mkdir()
    (root / "plan.md").write_text("# Plan\n", encoding="utf-8")
    research.init_project(root, str(root / "plan.md"), "t")  # an older plugin: no pyproject.toml, no .venv
    done = rl(root, "upgrade")
    assert done.returncode == 0, done.stderr
    assert "not the project's own: rl init in the project root makes one" in done.stdout
    assert Path(os.environ["SMOOTH_RESEARCH_HOME"], "env-dev", ".ready").exists()  # the plugin's, built once
    done = rl(root, "init")
    assert done.returncode == 0 and "already initialized" in done.stdout, done.stderr
    assert (root / ".venv/bin/python").exists() and set(DEPS) <= set(dependencies(root))
    assert "on the project's .venv" in rl(root, "upgrade").stdout


def test_init_help_makes_nothing(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    done = rl(root, "init", "--help")
    assert done.returncode == 0 and "--python" in done.stdout, done.stderr
    assert list(root.iterdir()) == []


def test_read_only_neither_updates_nor_makes_the_project_environment(tmp_path):
    root = tmp_path / "p"
    assert init(root).returncode == 0
    stamp = root / ".venv/.smooth-research-deps"
    stamp.write_text("latticedb==0.14.0", encoding="utf-8")  # what an older plugin needed
    kept = {name: (root / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    done = rl(root, "--read-only", "status")
    assert done.returncode == 0, done.stderr
    assert "--read-only: " in done.stderr and "is not updated for this plugin version" in done.stderr
    assert stamp.read_text() == "latticedb==0.14.0"
    assert {name: (root / name).read_bytes() for name in kept} == kept
    for args in (("init", "--dir", "elsewhere", "--plan", "p/plan.md", "--read-only"), ("setup", "--read-only")):
        refused = rl(tmp_path, *args)
        assert refused.returncode == 2 and "--read-only changes nothing" in refused.stderr
    assert not (tmp_path / "elsewhere").exists()
