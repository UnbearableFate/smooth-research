"""Hierarchical labels: H1.2 sits under H1, EH1.2/3 is the third experiment anchored at H1.2. The ids stay the keys;
the labels are given once at creation, every command takes either form, and people read the labels."""

import pytest

from smooth_research import delegate, literature, research, tasks, view
from smooth_research.project import find_project
from smooth_research.store import Store, _sort_key
from smooth_research.util import RLError


def hyp(store, title, *sources, **kw):
    return research.add_hypothesis(store, title, f"{title} holds", derived_from=list(sources), **kw)


def exp(store, title, *hyps, **kw):
    return research.add_experiment(store, title, "q?", list(hyps), **kw)


def label(store, rid):
    with store.read() as tx:
        return tx.require(rid)["label"]


def test_a_hypothesis_goes_under_its_latest_hypothesis_source_unless_told_otherwise(project):
    assert label(project, "H-0001") == "H1"
    assert hyp(project, "b")["label"] == "H2"  # no source: a new line
    assert hyp(project, "c", "H-0001")["label"] == "H1.1"
    assert hyp(project, "d", "H-0001", "H-0003")["label"] == "H1.1.1"  # the latest created source
    assert hyp(project, "e", "H-0001", "H-0003", parent="H-0001")["label"] == "H1.2"
    out = hyp(project, "f", "H-0003", root=True)
    assert out["label"] == "H3" and out["summary"] == "H3@1 active: f (id H-0006)"
    with project.read() as tx:  # --root changes the label only: the source is recorded as usual
        assert [e["id"] for e in tx.out("H-0006", "derived_from")] == ["H-0003"]
    paper = literature.add_paper(project, title="a paper")["id"]
    assert hyp(project, "g", paper)["label"] == "H4"  # papers and advice answers do not place a hypothesis
    with pytest.raises(RLError, match="--parent H-0002 is not one of the hypotheses it is derived from"):
        hyp(project, "h", "H-0001", parent="H-0002")
    with pytest.raises(RLError, match="give one of them"):
        hyp(project, "h", "H-0001", parent="H-0001", root=True)
    research.decide(project, ["H-0005"], "stop", "a dead end")  # H1.2 retired: its number is not given again
    assert hyp(project, "i", "H-0001")["label"] == "H1.3"


def test_an_experiment_goes_under_the_deepest_common_ancestor_of_what_it_tests(project):
    hyp(project, "a", "H-0001")  # H-0002 H1.1
    hyp(project, "b", "H-0001")  # H-0003 H1.2
    hyp(project, "c")  # H-0004 H2
    assert exp(project, "both", "H-0002", "H-0003")["label"] == "EH1/2"  # E-0001 is EH1/1
    out = exp(project, "one", "H-0002")
    assert out["label"] == "EH1.1/1" and out["summary"].startswith("EH1.1/1 (id E-0003) draft contract "
                                                                   "research/experiments/E-0003.md")
    assert exp(project, "apart", "H-0004", "H-0002")["label"] == "EH2/1"  # no common ancestor: the first
    assert exp(project, "apart again", "H-0002", "H-0004")["label"] == "EH1.1/2"
    assert exp(project, "anchored", "H-0002", anchor="H-0004")["label"] == "EH2/2"
    assert "label: EH2/2" in (project.project.root / "research/experiments/E-0006.md").read_text()


def test_every_command_takes_the_label_or_the_id(project, rl):
    code, out, _ = rl("hypothesis", "add", "--title", "deeper", "--statement", "s", "--derived-from", "H1")
    assert code == 0 and out.startswith("H1.1@1 active: deeper (id H-0002)")
    code, out, _ = rl("show", "H1.1")
    assert code == 0 and out.startswith("H1.1@1 [active/untested] deeper\n  id: H-0002\n")
    assert "  -> derived_from H1 (H-0001)" in out
    code, out, _ = rl("link", "EH1/1", "tests", "H1.1")
    assert code == 0 and "E-0001 tests H-0002@1" in out
    code, out, _ = rl("experiment", "add", "--title", "x", "--question", "q", "--hypotheses", "H1,H-0002")
    assert code == 0 and out.startswith("EH1/2 (id E-0002)")
    code, out, _ = rl("evidence", "add", "--summary", "s", "--level", "screening", "--supports", "H1.1",
                      "--experiment", "EH1/2")
    assert code == 0 and "supports H-0002" in out
    code, out, _ = rl("decide", "--targets", "H1.1", "--targets", "EH1/1", "--action", "continue", "--rationale", "r")
    assert code == 0 and "on H-0002, E-0001" in out
    code, out, _ = rl("hypothesis", "add", "--title", "other", "--statement", "s", "--derived-from", "H1,H1.1",
                      "--parent", "H1")
    assert code == 0 and out.startswith("H1.2@1")
    code, _, err = rl("show", "H9")
    assert code == 1 and "error[NOT_FOUND]: no hypothesis is labelled H9" in err
    code, _, err = rl("run", "register", "--experiment", "EH7/1", "--request-id", "r", "--code-unknown")
    assert code == 1 and "no experiment is labelled EH7/1" in err
    code, _, err = rl("run", "record", "--attempt", "ATT-0009-1", "--source", "H100", "--state", "running")
    assert "ATT-0009-1" in err and "labelled" not in err  # --source says who observed it here: not a record
    code, out, _ = rl("show", "H-0001")  # the ids work as before
    assert code == 0 and out.startswith("H1@1")


def test_labels_sort_by_the_number_at_each_level(project, rl):
    for n in range(10):
        hyp(project, f"child {n}", "H-0001")
    code, out, _ = rl("status")
    shown = [line.split("@")[0].strip() for line in out.splitlines() if "@1 [active" in line]
    assert shown == ["H1"] + [f"H1.{n}" for n in range(1, 11)]
    assert sorted(["EH1.1/1", "EH1/10", "EH1/3", "H1.10", "H1.9", "H2", "H1"], key=_sort_key) == \
        ["EH1/3", "EH1/10", "EH1.1/1", "H1", "H1.9", "H1.10", "H2"]


def test_the_upgrade_labels_older_records_as_if_they_had_been_created_now(project, tmp_path, rl):
    hyp(project, "b")  # H-0002
    hyp(project, "c", "H-0001")  # H-0003
    hyp(project, "d", "H-0001", "H-0003")  # H-0004
    hyp(project, "e", literature.add_paper(project, title="p")["id"])  # H-0005
    exp(project, "x", "H-0003", "H-0004")
    exp(project, "y", "H-0002", "H-0003")
    research.link(project, "H-0002", "derived_from", "H-0005")  # added later: it does not move H-0002
    research.decide(project, ["H-0004"], "stop", "dead end")
    with project.read() as tx:
        fresh = {r["id"]: r["label"] for r in tx.all("Hypothesis") + tx.all("Experiment")}
    assert fresh == {"H-0001": "H1", "H-0002": "H2", "H-0003": "H1.1", "H-0004": "H1.1.1", "H-0005": "H3",
                     "E-0001": "EH1/1", "E-0002": "EH1.1/1", "E-0003": "EH2/1"}
    with project.write() as tx:  # as a plugin before labels wrote them
        for rid in fresh:
            tx.txn.set_property(tx._node(tx.require(rid)["_label"], rid), "label", None)
        tx.set_meta(schema_version=2)
    store = Store(find_project(), "test")
    with store.read() as tx:
        assert {r["id"]: r["label"] for r in tx.all("Hypothesis") + tx.all("Experiment")} == fresh
    assert "labelled in the order they were created" in store.upgraded["changes"][1]
    code, out, _ = rl("upgrade")
    assert code == 0 and "labels the schema 3 upgrade gave the records" in out
    assert "  H-0001 H1 · H-0002 H2 · H-0003 H1.1 · H-0004 H1.1.1 · H-0005 H3 · E-0001 EH1/1\n" in out


def test_people_read_labels_also_in_the_text_of_older_records(project, tmp_path):
    hyp(project, "b", "H-0001")
    research.decide(project, ["H-0001"], "continue", "E-0001 shows H-0001 and H-0002@1 hold; see "
                                                     "research/experiments/E-0001.md. Next: H-0002.")
    text = (tmp_path / view.report(project)["path"]).read_text()
    assert "EH1/1 shows H1 and H1.1@1 hold; see research/experiments/E-0001.md. Next: H1.1." in text
    assert "### EH1/1 WD smoke" in text


def test_the_brief_names_its_records_once_by_label_and_id(project):
    tasks.create_task(project, "seeds", "experiment", ["E-0001"], to_role="researcher")
    text = delegate.prompt_for(project, "T-0001", "researcher-1")
    assert "names them so: EH1/1 is E-0001; H1 is H-0001. Use the ids in paths, job names" in text
    with project.read() as tx:
        ref = tx.require("E-0001")["contract_ref"]  # the task sealed it
    assert f"## Experiment EH1/1\nEH1/1@1 [E1] WD smoke (contract {ref}: research/experiments/E-0001.md)" in text
