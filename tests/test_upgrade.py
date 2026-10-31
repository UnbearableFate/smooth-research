"""Plugin updates never stop a project: older records are upgraded in place (with a copy of the old database), a
project written by a newer plugin stays readable, older snapshots restore, and the frozen projects of earlier record
formats keep working with every command."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from smooth_research import hooks, research, reviews, runs, snapshot
from smooth_research.project import find_project
from smooth_research.schema import SCHEMA_VERSION
from smooth_research.store import Store
from smooth_research.util import RLError

from conftest import SPEC

FROZEN = Path(__file__).parent / "fixtures" / "projects"


@pytest.fixture(autouse=True)
def no_plugin_build(monkeypatch):
    monkeypatch.delenv("SMOOTH_RESEARCH_BUILD", raising=False)


def _manual_advisor(root: Path) -> None:
    path = root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config["models"]["advisor"] = ["manual:advisor-a", "manual:advisor-b"]
    path.write_text(yaml.safe_dump(config, sort_keys=False))


def _make_old(store: Store, version: int = 1) -> None:
    """Make the store look written by an older plugin: drop the fields that version did not write."""
    with store.write() as tx:
        for label, field in (("Attempt", "code_review"), ("Ticket", "advice_round")):
            for rec in tx.all(label):
                tx.txn.set_property(tx._node(label, rec["id"]), field, None)
        tx.set_meta(schema_version=version)


def _older_project(project, tmp_path) -> Store:
    _manual_advisor(tmp_path)
    store = Store(find_project(), "test")
    runs.register(store, "E-0001", SPEC, "r1", no_review="smoke")
    reviews.prepare_advice(store, "which explanation?", "H-0001")
    _make_old(store)
    return Store(find_project(), "test")


def test_an_older_project_is_upgraded_in_place_with_a_copy_of_its_database(project, tmp_path, rl):
    _older_project(project, tmp_path)
    code, out, _ = rl("run", "show")  # reads code_review on every attempt
    assert code == 0 and "code review: unchecked: registered before code review checks were recorded" in out
    assert "the records were upgraded from format 1 to 2" in out and "run `rl upgrade`" in out
    backups = list((tmp_path / "research/state/backups").glob("schema-1-*"))
    assert len(backups) == 1 and (backups[0] / "research.lattice").is_file()
    store = Store(find_project(), "test")
    with store.read() as tx:
        assert tx.meta()["schema_version"] == SCHEMA_VERSION
        assert [t["advice_round"] for t in tx.all("Ticket", kind="advice")] == [1, 1]
        event = [e for e in tx.all("Event") if e["type"] == "project.upgrade"]
        assert len(event) == 1 and "schema 1 -> 2" in event[0]["summary"] and "1 attempt(s)" in event[0]["summary"]
    assert store.upgraded is None
    code, out, _ = rl("models")  # counts advice rounds
    assert code == 0 and "advice rounds used: 1 of 3" in out and "upgraded" not in out


def test_the_old_database_copy_opens_as_it_was(project, tmp_path):
    store = _older_project(project, tmp_path)
    with store.read():
        pass
    backup = next((tmp_path / "research/state/backups").glob("schema-1-*"))
    copy = tmp_path.parent / f"{tmp_path.name}-copy"
    shutil.copytree(tmp_path, copy, ignore=shutil.ignore_patterns("backups"))
    for f in (copy / "research/state").glob("research.lattice*"):
        f.unlink()
    for f in backup.iterdir():
        shutil.copy2(f, copy / "research/state" / f.name)
    from latticedb import Database
    from smooth_research.store import Tx
    with Database(copy / "research/state/research.lattice", read_only=True) as db, db.read() as txn:
        assert Tx(txn, False, "t").meta()["schema_version"] == 1


def test_a_project_from_a_newer_plugin_is_read_but_not_written(project, tmp_path, rl):
    with project.write() as tx:
        tx.set_meta(schema_version=SCHEMA_VERSION + 1)
    code, out, _ = rl("status")
    assert code == 0 and "H-0001" in out
    code, _, err = rl("hypothesis", "add", "--title", "t", "--statement", "s")
    assert code != 0 and "written by a newer smooth-research" in err and "nothing was written" in err
    text = hooks.session_start("claude", "{}")["hookSpecificOutput"]["additionalContext"]
    assert "written by a newer smooth-research" in text and "Tell the user" in text


def test_an_older_snapshot_restores_and_is_upgraded(project, tmp_path):
    store = _older_project(project, tmp_path)
    snap = tmp_path / "snap"
    # export reads through the store, which upgrades it first: write the old format back into the snapshot
    snapshot.export(store, str(snap))
    data = json.loads((snap / "snapshot.json").read_text())
    data["schema_version"] = data["meta"]["schema_version"] = 1
    for rec in data["nodes"]["Attempt"]:
        rec.pop("code_review", None)
    (snap / "snapshot.json").write_text(json.dumps(data))
    out = snapshot.restore(str(snap), str(tmp_path.parent / f"{tmp_path.name}-restored"))
    assert out["problems"] == [] and "records upgraded from format 1 to 2" in out["summary"]
    data["schema_version"] = SCHEMA_VERSION + 1
    (snap / "snapshot.json").write_text(json.dumps(data))
    with pytest.raises(RLError) as exc:
        snapshot.restore(str(snap), str(tmp_path.parent / f"{tmp_path.name}-restored-2"))
    assert exc.value.code == "UNSUPPORTED" and "newer smooth-research" in exc.value.message


def test_a_plugin_update_is_flagged_until_rl_upgrade_records_it(project, tmp_path, rl, monkeypatch):
    monkeypatch.setenv("SMOOTH_RESEARCH_BUILD", "b2")
    code, out, _ = rl("status")
    assert "the plugin changed since this project was last used (build dev -> b2): run rl upgrade" in out
    text = hooks.session_start("claude", "{}")["hookSpecificOutput"]["additionalContext"]
    assert "attention: the plugin changed since this project was last used" in text
    shutil.rmtree(tmp_path / "research/reports")
    code, out, _ = rl("upgrade")
    assert code == 0 and f"schema {SCHEMA_VERSION} (this CLI: {SCHEMA_VERSION})" in out
    assert "plugin build b2 (last used with dev)" in out and "layout: created research/reports/" in out
    assert "adjust the old research assets to this version yourself" in out and "Never rewrite recorded facts" in out
    code, out, _ = rl("status")
    assert "plugin changed" not in out
    code, out, _ = rl("upgrade", "--note", "moved old notes into research/notes/")
    assert "recorded: project.adjusted: moved old notes into research/notes/" in out
    with project.read() as tx:
        assert [e["summary"] for e in tx.all("Event") if e["type"] == "project.adjusted"] == \
            ["by build b2: moved old notes into research/notes/"]


def test_the_hook_says_the_records_were_upgraded(project, tmp_path):
    _older_project(project, tmp_path)
    text = hooks.session_start("claude", "{}")["hookSpecificOutput"]["additionalContext"]
    assert "Its records were upgraded from format 1 to 2" in text


def test_layout_repair_creates_only_what_is_missing(project, tmp_path):
    (tmp_path / "docs/constraints.md").write_text("mine\n")
    shutil.rmtree(tmp_path / "research/notes")
    assert research.ensure_layout(project.project) == ["research/notes/"]
    assert (tmp_path / "docs/constraints.md").read_text() == "mine\n"


FROZEN_COMMANDS = [
    ("status",), ("resume", "--role", "coordinator"), ("history",), ("graph",), ("view",), ("report",),
    ("manuscript",), ("run", "show"), ("run", "show", "ATT-0001-1"), ("inbox", "--role", "researcher-1"),
    ("models",), ("compute",), ("doctor",), ("upgrade",), ("context", "--purpose", "work", "--task", "T-0001"),
    ("context", "--purpose", "advice", "--question", "next?"), ("task", "list"), ("paper", "list"),
    ("review", "show", "RV-0001"), ("review", "show", "RV-0002"), ("advise", "show", "AD-0001"),
]


@pytest.mark.parametrize("frozen", sorted(p.name for p in FROZEN.iterdir() if p.is_dir()))
def test_frozen_projects_of_earlier_formats_keep_working(frozen, tmp_path, rl, monkeypatch):
    target = tmp_path / "project"
    out = snapshot.restore(str(FROZEN / frozen), str(target))
    assert out["problems"] == [], out["problems"]
    monkeypatch.chdir(target)
    data = json.loads((FROZEN / frozen / "snapshot.json").read_text())
    ids = [rec["id"] for label, rows in data["nodes"].items() if label not in ("Event", "Request", "Meta")
           for rec in rows]
    for args in [*FROZEN_COMMANDS, *[("show", i) for i in ids]]:
        code, _, err = rl(*args)
        assert code == 0, (args, err)
    spec = data["nodes"]["Run"][0]["spec"]
    spec = {k: v for k, v in (json.loads(spec) if isinstance(spec, str) else spec).items()
            if k not in ("experiment_ref", "protocol_ref")}
    writes = [("hypothesis", "add", "--title", "new", "--statement", "a new claim", "--derived-from", "H-0001"),
              ("run", "register", "--experiment", "E-0001", "--run", "RUN-0001", "--request-id", "again",
               "--spec", json.dumps(spec), "--no-review", "re-execution"),
              ("run", "record", "--attempt", "ATT-0001-2", "--source", "test", "--state", "ended", "--outcome",
               "failed"),
              ("decide", "--targets", "H-0002", "--action", "pause", "--rationale", "later",
               "--reopen-condition", "a clean-label control"),
              ("task", "create", "--title", "next", "--kind", "analysis", "--targets", "H-0001")]
    for args in writes:
        code, _, err = rl(*args)
        assert code == 0, (args, err)


def _older_layout(project, tmp_path):
    """Files where an earlier layout kept them, with the records pointing there."""
    old = tmp_path / "docs/experiments"
    old.mkdir(parents=True)
    contract = tmp_path / "research/experiments/E-0001.md"
    contract.rename(old / "E-0001.md")
    with project.write() as tx:
        tx.update("E-0001", {"contract_path": "docs/experiments/E-0001.md"})
    for name in ("reviews/RV-0009.md", "report-1.md"):
        (tmp_path / "reports" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "reports" / name).write_text("old\n")
    (tmp_path / "docs/plan").mkdir()
    (tmp_path / "docs/constraints.md").rename(tmp_path / "docs/plan/constraints.md")


def test_older_layout_is_listed_and_moved_with_its_records(project, tmp_path, rl):
    _older_layout(project, tmp_path)
    code, out, _ = rl("upgrade")
    for old, new in (("docs/plan/constraints.md", "docs/constraints.md"), ("docs/experiments", "research/experiments"),
                     ("reports/reviews", "research/reviews"), ("reports", "research/reports")):
        assert f"older layout: {old} " in out and f"rl upgrade --move {old} {new}" in out
    assert not (tmp_path / "docs/constraints.md").exists()  # no empty template over the user's constraints
    code, _, err = rl("upgrade", "--move", "reports", "research/reports")
    assert code != 0 and "move that first (rl upgrade --move reports/reviews research/reviews)" in err
    code, out, _ = rl("upgrade", "--move", "docs/experiments", "research/experiments")
    assert code == 0 and "E-0001 contract" in out
    with project.read() as tx:
        assert tx.require("E-0001")["contract_path"] == "research/experiments/E-0001.md"
        assert [e for e in tx.all("Event") if e["type"] == "project.moved"]
    assert (tmp_path / "research/experiments/E-0001.md").is_file() and not (tmp_path / "docs/experiments").exists()
    runs.register(project, "E-0001", SPEC, "r1", no_review="smoke")  # seals the contract from its new place
    with project.read() as tx:
        assert tx.require("E-0001")["contract_ref"]
    for old, new in (("reports/reviews", "research/reviews"), ("reports", "research/reports"),
                     ("docs/plan/constraints.md", "docs/constraints.md")):
        assert rl("upgrade", "--move", old, new)[0] == 0
    code, out, _ = rl("upgrade")
    assert "older layout: " not in out and "is missing" not in out


def test_a_move_refuses_clashes_and_protected_paths_and_rolls_back(project, tmp_path, rl, monkeypatch):
    _older_layout(project, tmp_path)
    (tmp_path / "research/experiments/E-0001.md").write_text("new\n")
    code, _, err = rl("upgrade", "--move", "docs/experiments", "research/experiments")
    assert code != 0 and "already there: research/experiments/E-0001.md" in err
    (tmp_path / "research/experiments/E-0001.md").unlink()
    code, _, err = rl("upgrade", "--move", "research/state", "elsewhere")
    assert code != 0 and "not a movable path" in err
    from smooth_research.store import Tx

    def broken(self, *args, **kwargs):
        raise RLError("CORRUPT", "simulated failure")
    monkeypatch.setattr(Tx, "event", broken)
    code, _, err = rl("upgrade", "--move", "docs/experiments", "research/experiments")
    assert code != 0 and "simulated failure" in err
    assert (tmp_path / "docs/experiments/E-0001.md").is_file()
    assert not (tmp_path / "research/experiments/E-0001.md").exists()
    monkeypatch.undo()
    with project.read() as tx:
        assert tx.require("E-0001")["contract_path"] == "docs/experiments/E-0001.md"
