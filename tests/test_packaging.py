"""The build assembles both hosts from one shared core with host-specific placeholders filled."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from smooth_research import literature
from smooth_research.util import RLError

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_plugins", REPO / "scripts/build_plugins.py")
build_plugins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_plugins)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    return out, {r["target"]: r for r in build_plugins.build(["claude", "codex"], out)}


def test_both_hosts_share_the_core_and_differ_only_in_adapters(built):
    out, records = built
    claude, codex = (set(records[t]["files"]) for t in ("claude", "codex"))
    shared = {f for f in claude & codex if records["claude"]["files"][f] == records["codex"]["files"][f]}
    assert {"bin/rl", "src/smooth_research/cli.py", "src/smooth_research/store.py", "pyproject.toml"} <= shared
    assert ".claude-plugin/plugin.json" in claude - codex and "agents/researcher.md" in claude - codex
    assert ".codex-plugin/plugin.json" in codex - claude
    for target in ("claude", "codex"):
        digests = records[target]["files"].values()
        assert all(d.startswith("blake2b:") for d in digests)


def test_placeholders_are_filled_per_host(built):
    out, _ = built
    claude = (out / "claude/plugins/smooth-research/skills/smooth-research/SKILL.md").read_text()
    codex = (out / "codex/plugins/smooth-research/skills/smooth-research/SKILL.md").read_text()
    assert "${CLAUDE_PLUGIN_ROOT}/bin/rl resume" in claude and "${CLAUDE_SESSION_ID}" in claude
    assert "rl resume" in codex and "CLAUDE_PLUGIN_ROOT" not in codex and "--host codex" in codex
    for root in (out / "claude", out / "codex"):
        for md in root.rglob("*.md"):
            assert not re.search(r"\{\{[a-z_]+\}\}", md.read_text()), md
    hooks = json.loads((out / "codex/plugins/smooth-research/hooks/hooks.json").read_text())
    assert "${PLUGIN_ROOT}/bin/rl" in hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    catalog = json.loads((out / "claude/.claude-plugin/marketplace.json").read_text())
    assert catalog["plugins"][0]["source"] == "./plugins/smooth-research"


def test_skill_entry_points_stay_short(built):
    out, _ = built
    for name in ("smooth-research", "research-review", "research-advise"):
        lines = (out / f"claude/plugins/smooth-research/skills/{name}/SKILL.md").read_text().splitlines()
        assert len(lines) <= 150, name


def test_the_build_refuses_to_overwrite_sources(tmp_path):
    with pytest.raises(ValueError):
        build_plugins.build(["claude"], REPO / "src")


def test_identifier_normalization():
    assert literature.normalize("arXiv:2106.09685v2") == {"arxiv_id": "2106.09685", "arxiv_version": "v2"}
    assert literature.normalize("https://arxiv.org/abs/2106.09685") == {"arxiv_id": "2106.09685", "arxiv_version": None}
    assert literature.normalize("https://doi.org/10.1000/XYZ.1.") == {"doi": "10.1000/xyz.1"}
    with pytest.raises(RLError):
        literature.normalize("not an id")
