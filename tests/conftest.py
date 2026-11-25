import json
import subprocess
from pathlib import Path

import pytest

from smooth_research import research
from smooth_research.cli import main
from smooth_research.project import find_project
from smooth_research.store import Store


@pytest.fixture(autouse=True)
def outside_herdr(monkeypatch):
    """Tests never reach a real herdr session: the ones that need herdr use a fake one (test_watch.py)."""
    for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "SMOOTH_RESEARCH_SESSION",
                "SMOOTH_RESEARCH_CHECKOUT", "SMOOTH_RESEARCH_BUILD", "SMOOTH_RESEARCH_PROJECT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.org"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def own_homes(tmp_path_factory, monkeypatch):
    """Tests read no real subscription usage and no real Claude settings."""
    base = tmp_path_factory.mktemp("homes")
    for key in ("SMOOTH_RESEARCH_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(key, str(base / key.lower()))
    return base


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


PLAN = "# Toy plan\n\n## Averaging\nThe mean of three noisy estimates has a smaller error than one estimate.\n"


@pytest.fixture
def project(tmp_path, monkeypatch) -> Store:
    """An initialized project in its own git repository, committed; cwd is the project root."""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    git(root, "init", "-q", "-b", "main")
    (root / "plan.md").write_text(PLAN, encoding="utf-8")
    research.init_project(root, "plan.md", "toy")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return Store(find_project(), "test")


@pytest.fixture
def rl(capsys):
    """Run the CLI in-process: rl(*args, who=None) -> (exit code, stdout, stderr); who is the session (--as)."""
    def run(*args, who=None):
        argv = (["--as", who] if who else []) + [str(a) for a in args]
        code = main(argv)
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
