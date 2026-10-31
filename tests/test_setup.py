"""rl init makes a new project's code a uv project (uv init --python <version> --no-workspace)."""

import shutil
import subprocess
import tomllib

import pytest

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def init(rl, root, *extra):
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text("# Plan\nA question.\n", encoding="utf-8")
    return rl("init", "--plan", root / "plan.md", "--dir", root, *extra)


def test_a_new_project_becomes_a_uv_project_with_its_own_git_repository(rl, tmp_path):
    root = tmp_path / "noise-study"
    code, out, err = init(rl, root)
    assert code == 0, err
    meta = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert meta["name"] == "noise-study" and meta["requires-python"] == ">=3.13"
    assert (root / ".python-version").read_text().strip() == "3.13"
    assert (root / "src/noise_study/__init__.py").is_file() and (root / "src/.gitignore").is_file()
    assert (root / ".git").is_dir() and ".venv" in (root / ".gitignore").read_text().split()
    assert "python: uv project for Python 3.13 (pyproject.toml, .python-version, src/noise_study/)" in out
    assert "commit the new project in its root" in out

    code, out, err = init(rl, root)  # an existing project: nothing is initialized again
    assert code == 0 and "python:" not in out and "commit the new project" not in out


def test_a_project_inside_another_uv_project_stays_out_of_its_workspace(rl, tmp_path):
    parent = tmp_path / "tool"
    parent.mkdir()
    (parent / "pyproject.toml").write_text('[project]\nname = "tool"\nversion = "0.1.0"\n', encoding="utf-8")
    git(parent, "init", "-q")
    before = (parent / "pyproject.toml").read_text()
    root = parent / "demos" / "demo-1"
    code, out, err = init(rl, root, "--python", "3.12")
    assert code == 0, err
    assert (parent / "pyproject.toml").read_text() == before  # no [tool.uv.workspace] members added
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["requires-python"] == ">=3.12"
    # uv runs no git init inside a repository, and writes no .gitignore there: rl writes uv's
    assert not (root / ".git").exists() and ".venv" in (root / ".gitignore").read_text().split()


def test_an_existing_pyproject_and_gitignore_are_kept(rl, tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "mine"\n', encoding="utf-8")
    code, out, err = init(rl, root)
    assert code == 0 and "python: kept the existing pyproject.toml" in out
    assert (root / "pyproject.toml").read_text() == '[project]\nname = "mine"\n'

    other = tmp_path / "q"
    other.mkdir()
    git(other, "init", "-q")
    (other / ".gitignore").write_text("data/", encoding="utf-8")
    code, out, err = init(rl, other)
    assert code == 0, err
    assert (other / ".gitignore").read_text().splitlines() == ["data/", "", "# uv environment", ".venv"]


def test_no_uv_skips_it(rl, tmp_path):
    root = tmp_path / "p"
    code, out, err = init(rl, root, "--no-uv")
    assert code == 0 and "python:" not in out
    assert not (root / "pyproject.toml").exists() and not (root / ".git").exists()
