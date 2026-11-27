"""Plugin updates never stop a project: the frozen projects of earlier record formats restore and keep working
with every command, an older store is upgraded in place with a copy of its database, a project written by a
newer plugin stays readable, and a project from before the 2026-11 redesign is refused with what to do."""

import json
from pathlib import Path

import pytest

from smooth_research import schema, snapshot, store as store_mod, upgrade
from smooth_research.project import find_project
from smooth_research.store import Store
from smooth_research.upgrade import Migration

FROZEN = Path(__file__).parent / "fixtures" / "projects"

READS = [("status",), ("tree",), ("history",), ("doctor",), ("upgrade",), ("task", "list"), ("hypothesis", "list"),
         ("run", "show"), ("inbox", "--for", "coordinator"), ("inbox", "--for", "user"), ("pack", "--for", "coordinator"),
         ("pack", "--for", "scientist-T-0002"), ("view",)]


@pytest.fixture(autouse=True)
def no_plugin_build(monkeypatch):
    monkeypatch.delenv("SMOOTH_RESEARCH_BUILD", raising=False)


def restored(frozen: str, tmp_path, monkeypatch) -> tuple[Path, dict]:
    target = tmp_path / "project"
    out = snapshot.restore(str(FROZEN / frozen), str(target))
    assert out["problems"] == [], out["problems"]
    monkeypatch.chdir(target)
    return target, json.loads((FROZEN / frozen / "snapshot.json").read_text())


@pytest.mark.parametrize("frozen", sorted(p.name for p in FROZEN.iterdir() if p.is_dir()))
def test_frozen_projects_of_earlier_formats_keep_working(frozen, tmp_path, rl, monkeypatch):
    _, data = restored(frozen, tmp_path, monkeypatch)
    ids = [rec["id"] for label, rows in data["nodes"].items() if label not in ("Event", "Request", "Meta")
           for rec in rows]
    for args in [*READS, *[("show", i) for i in ids]]:
        code, _, err = rl(*args)
        assert code == 0, (args, err)
    writes = [(("hypothesis", "add", "--title", "Another", "--statement", "s", "--purpose", "p"), "coordinator"),
              (("phase", "create", "--title", "Phase 2"), "coordinator"),
              (("question", "--close", "MSG-0001"), "coordinator")]
    for args, who in writes:
        code, _, err = rl(*args, who=who)
        assert code == 0, (args, err)


def _set_version(root: Path, version: int) -> None:
    with Store(find_project(str(root)), "test").write() as tx:
        tx.set_meta(schema_version=version)


LATEST = f"schema-{schema.SCHEMA_VERSION}"


def test_the_latest_format_has_a_frozen_project():
    assert (FROZEN / LATEST / "snapshot.json").is_file()


def test_a_project_from_before_the_redesign_is_refused_with_what_to_do(tmp_path, rl, monkeypatch):
    root, _ = restored("schema-5", tmp_path, monkeypatch)
    _set_version(root, 4)
    code, _, err = rl("status")
    assert code == 1 and "before the 2026-11 redesign" in err and "rl init --plan" in err


def test_a_project_written_by_a_newer_plugin_is_read_but_not_written(tmp_path, rl, monkeypatch):
    root, _ = restored(LATEST, tmp_path, monkeypatch)
    _set_version(root, schema.SCHEMA_VERSION + 1)
    assert rl("status")[0] == 0 and rl("tree")[0] == 0
    code, _, err = rl("phase", "create", "--title", "x", who="coordinator")
    assert code == 1 and "newer smooth-research" in err and "nothing was written" in err


def test_a_later_format_upgrades_the_frozen_project_in_place_with_a_copy(tmp_path, rl, monkeypatch):
    """A stand-in for the next record format: its migration runs once, under the lock, with a backup."""
    root, _ = restored(LATEST, tmp_path, monkeypatch)
    nxt = schema.SCHEMA_VERSION + 1
    for module in (schema, store_mod, upgrade, snapshot):
        monkeypatch.setattr(module, "SCHEMA_VERSION", nxt, raising=False)

    def fill(tx) -> list[str]:
        hyps = tx.all("Hypothesis")
        return [f"{len(hyps)} hypothesis(es) looked at"]
    monkeypatch.setattr(upgrade, "MIGRATIONS", [*upgrade.MIGRATIONS, Migration(nxt, "a test migration", fill)])
    code, out, err = rl("upgrade")
    assert code == 0, err
    assert f"schema {nxt}" in out and "project.upgrade" in out
    backups = list((root / "research/state/backups").iterdir())
    assert [b.name.split("-")[1] for b in backups] == [str(nxt - 1)]
    with Store(find_project(), "test").read() as tx:
        assert tx.meta()["schema_version"] == nxt
        events = [e["summary"] for e in tx.all("Event") if e["type"] == "project.upgrade"]
    assert len(events) == 1 and "1 hypothesis(es) looked at" in events[0]
    assert rl("upgrade")[0] == 0  # nothing more to do the second time
    with Store(find_project(), "test").read() as tx:
        assert len([e for e in tx.all("Event") if e["type"] == "project.upgrade"]) == 1
