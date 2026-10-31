import json
from pathlib import Path

import pytest

from smooth_research import research
from smooth_research.cli import main
from smooth_research.project import find_project
from smooth_research.store import Store


@pytest.fixture(autouse=True)
def outside_herdr(monkeypatch):
    """Tests never reach a real herdr session: the ones that need herdr use a fake one (tests/test_agents.py)."""
    for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "SMOOTH_RESEARCH_ROLE",
                "SMOOTH_RESEARCH_SESSION", "SMOOTH_RESEARCH_CHECKOUT", "SMOOTH_RESEARCH_BUILD"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def own_homes(tmp_path_factory, monkeypatch):
    """Tests read no real subscription usage and no real Claude settings: rl's data directory, Codex's home and
    Claude's configuration directory are empty ones of their own."""
    base = tmp_path_factory.mktemp("homes")
    for key in ("SMOOTH_RESEARCH_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(key, str(base / key.lower()))
    return base


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An initialized project with one hypothesis and one experiment; cwd is the project root."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMOOTH_RESEARCH_PROJECT", raising=False)
    (tmp_path / "plan.md").write_text("# Plan\nDoes weight decay help under label noise?\n", encoding="utf-8")
    research.init_project(tmp_path, "plan.md", "test")
    store = Store(find_project(), "test")
    research.add_hypothesis(store, "WD helps", "weight decay raises test accuracy under 20% noise", "toy")
    research.add_experiment(store, "WD smoke", "does wd beat no wd?", ["H-0001"], "E1", intervention="wd",
                            comparator="no wd", metric="acc", scope="toy", outcome_actions="a", budget="b", stop="c")
    return store


@pytest.fixture
def rl(capsys):
    """Run the CLI in-process: returns (exit code, stdout, stderr)."""
    def run(*args):
        code = main([str(a) for a in args])
        out = capsys.readouterr()
        return code, out.out, out.err
    return run


SPEC = {"arm": "wd", "seed": 0, "code_ref": {"content_digest": "blake2b:" + "0" * 64}, "config_digest": "unknown",
        "data_revision": "synthetic-v1", "metric": "acc", "output_spec": "metrics.json"}


def obs(attempt="ATT-0001-1", state="running", outcome=None, at="2026-10-30T00:00:00+00:00", **extra):
    o = {"attempt_id": attempt, "source": "test", "observed_at": at, "execution_state": state, **extra}
    if outcome:
        o["outcome"] = outcome
    return o


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
