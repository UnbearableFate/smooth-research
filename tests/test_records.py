"""Ownership, sealing, crash consistency, graph rules, recovery and the CLI surface."""

import json
import multiprocessing
import os
import signal
import subprocess
import sys

import pytest
import yaml

from conftest import SPEC, obs
from smooth_research import context, documents, messages, research, runs, snapshot, tasks
from smooth_research.project import find_project
from smooth_research.store import Store
from smooth_research.util import RLError


def _claim(root, role, queue):
    os.chdir(root)
    store = Store(find_project(), role)
    try:
        tasks.claim(store, "T-0001", role)
        queue.put(("ok", role))
    except RLError as exc:
        queue.put((exc.code, role))


def test_two_sessions_claiming_at_once_get_one_owner(project, tmp_path):
    tasks.create_task(project, "seeds", "experiment", ["E-0001"], to_role="researcher")
    queue = multiprocessing.get_context("fork").Queue()
    procs = [multiprocessing.get_context("fork").Process(target=_claim, args=(tmp_path, f"researcher-{i}", queue))
             for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    outcomes = [queue.get(timeout=5) for _ in procs]
    assert sorted(o[0] for o in outcomes) == ["CONFLICT", "CONFLICT", "CONFLICT", "ok"]
    with project.read() as tx:
        task = tx.require("T-0001")
    winner = next(o for o in outcomes if o[0] == "ok")
    assert task["owner_role"] == winner[1]


def test_a_task_seals_the_contract_it_targets_and_its_pack_shows_a_later_revision(project, tmp_path):
    out = tasks.create_task(project, "seeds", "experiment", ["E-0001"], to_role="researcher")
    with project.read() as tx:
        exp, edge = tx.require("E-0001"), tx.out("T-0001", "targets")[0]
    assert (exp["revision"], edge["revision"], edge["contract_ref"]) == (1, 1, exp["contract_ref"])
    assert f"; contract E-0001@1 ({exp['contract_ref']}) sealed now" in out["summary"]
    assert f"This task is bound to contract EH1/1@1 ({exp['contract_ref']}), the current revision." in \
        context.work_pack(project, "T-0001")["summary"]
    contract = tmp_path / "research/experiments/E-0001.md"
    contract.write_text(contract.read_text().replace("budget: b\n", "budget: two GPU-hours\n"))
    research.seal_experiment(project, "E-0001")  # revision 2: the task goes on, its pack shows what changed
    pack = context.work_pack(project, "T-0001")["summary"]
    assert f"This task was created on contract EH1/1@1 ({exp['contract_ref']}); the contract has been revised " \
           "since, to EH1/1@2" in pack and "; draft research/experiments/E-0001.md)" in pack
    assert "-budget: b\n+budget: two GPU-hours" in pack
    again = tasks.create_task(project, "more seeds", "experiment", ["E-0001"], to_role="researcher")
    assert "; contract E-0001@2 (" in again["summary"] and "sealed now" not in again["summary"]


def test_a_task_on_an_incomplete_contract_says_what_to_fill_in_and_one_on_a_hypothesis_seals_nothing(project,
                                                                                                    tmp_path):
    research.add_experiment(project, "draft", "q?", ["H-0001"], metric="acc")  # E-0002
    objects = sorted(project.project.objects_dir.glob("*"))
    with pytest.raises(RLError) as exc:
        tasks.create_task(project, "both", "implement", ["E-0001", "E-0002"], to_role="researcher")
    assert exc.value.code == "INVALID" and "the contract of EH1/2 (research/experiments/E-0002.md) is not " \
        "complete: fill in intervention, comparator, scope, outcome_actions, budget, stop." in exc.value.message
    assert sorted(project.project.objects_dir.glob("*")) == objects  # nothing sealed, not even E-0001
    tasks.create_task(project, "analysis", "analysis", ["H-0001"], to_role="researcher")
    with project.read() as tx:
        assert [e["revision"] for e in tx.all("Experiment")] == [0, 0]
        assert [t["id"] for t in tx.all("Task")] == ["T-0001"] and "revision" not in tx.out("T-0001", "targets")[0]


def test_handoff_releases_the_task_for_the_next_owner(project, tmp_path):
    tasks.create_task(project, "analysis", "analysis", ["H-0001"], to_role="researcher")
    tasks.claim(project, "T-0001", "researcher-a")
    (tmp_path / "research/tasks/handoff.md").write_text("state of work")
    tasks.handoff(project, "T-0001", "research/tasks/handoff.md", "researcher-a")
    with pytest.raises(RLError) as exc:  # a released task takes no owner updates until it is claimed again
        tasks.progress(project, "T-0001", "delivered", "researcher-a", "done")
    assert exc.value.code == "CONFLICT"
    tasks.claim(project, "T-0001", "researcher-b")
    (tmp_path / "research/notes/result.md").write_text("result")
    tasks.progress(project, "T-0001", "delivered", "researcher-b", "done", ["research/notes/result.md"])
    tasks.review_task(project, "T-0001", "accept", "checked")
    with project.read() as tx:
        task = tx.require("T-0001")
    assert (task["state"], task["owner_role"], len(task["output_refs"])) == ("accepted", "researcher-b", 1)
    assert [m["type"] for m in messages.inbox(project, "coordinator")["messages"]] == ["RESULT"]


def test_only_the_owner_updates_a_task_and_not_after_the_coordinator_closed_it(project, tmp_path):
    tasks.create_task(project, "analysis", "analysis", ["H-0001"], to_role="researcher")
    tasks.claim(project, "T-0001", "researcher-a")
    (tmp_path / "research/tasks/handoff.md").write_text("state of work")

    def refused(fn, *args):
        with project.read() as tx:
            before = tx.require("T-0001")
        with pytest.raises(RLError) as exc:
            fn(project, "T-0001", *args)
        with project.read() as tx:
            assert tx.require("T-0001") == before
        assert exc.value.code == "CONFLICT"
        return exc.value.message

    assert "owned by researcher-a, not researcher-b" in refused(tasks.progress, "delivered", "researcher-b", "mine")
    assert "only its owner" in refused(tasks.handoff, "research/tasks/handoff.md", "researcher-b")
    tasks.review_task(project, "T-0001", "stop", "not needed")
    assert "rl task reopen T-0001" in refused(tasks.progress, "delivered", "researcher-a", "late")
    tasks.review_task(project, "T-0001", "reopen", "needed after all")
    tasks.progress(project, "T-0001", "delivered", "researcher-a", "done")
    tasks.review_task(project, "T-0001", "accept", "fine")
    assert "is accepted" in refused(tasks.progress, "active", "researcher-a")
    assert [m["type"] for m in messages.inbox(project, "coordinator")["messages"]] == ["RESULT"]


def test_inbox_cursor_and_repeated_message_delivery(project):
    first = messages.send(project, "NOTE", "coordinator", "researcher", "hello", message_id="MSG-ext-1")
    again = messages.send(project, "NOTE", "coordinator", "researcher", "hello", message_id="MSG-ext-1")
    assert first["id"] == again["id"]
    with pytest.raises(RLError):
        messages.send(project, "NOTE", "coordinator", "researcher", "changed", message_id="MSG-ext-1")
    assert len(messages.inbox(project, "researcher")["messages"]) == 1
    messages.inbox(project, "researcher", ack=True)
    assert messages.inbox(project, "researcher")["messages"] == []
    assert len(messages.inbox(project, "coordinator")["messages"]) == 0  # acknowledging sends nothing


def test_acknowledging_up_to_a_message_leaves_the_later_ones_unread(project, rl):
    ids = [messages.send(project, "NOTE", "coordinator", "researcher", f"note {i}")["id"] for i in range(3)]
    elsewhere = messages.send(project, "NOTE", "researcher", "coordinator", "for the coordinator")["id"]
    code, out, _ = rl("inbox", "--role", "researcher", "--ack", ids[1])  # read two, a third arrived meanwhile
    assert code == 0 and out.startswith("2 message(s) for researcher (acknowledged)") and "note 2" not in out
    assert [m["id"] for m in messages.inbox(project, "researcher")["messages"]] == [ids[2]]
    with pytest.raises(RLError, match=f"{elsewhere} is a message to coordinator, not to researcher"):
        messages.inbox(project, "researcher", ack=elsewhere)
    assert messages.inbox(project, "researcher", ack=ids[0])["messages"] == []  # already acknowledged: no change
    code, out, _ = rl("inbox", "--ack", "--role", "researcher")  # without an id: what it returns, as before
    assert code == 0 and out.startswith("1 message(s) for researcher (acknowledged)")
    assert messages.inbox(project, "researcher")["messages"] == []


def test_a_message_told_from_a_file_keeps_its_text_as_written(project, rl, tmp_path):
    text = "RV-0001 answered: 1 medium finding.\nIt says `loss.mean()` should be \"loss.sum() / n\"; check $HOME.\n"
    (tmp_path / "msg.md").write_text(text, encoding="utf-8")
    code, out, _ = rl("agent", "tell", "researcher-1", "--file", "msg.md", "--from", "coordinator")
    assert code == 0 and "NOTE coordinator -> researcher-1" in out
    assert messages.inbox(project, "researcher-1")["messages"][0]["text"] == text.strip()
    code, _, err = rl("agent", "tell", "researcher-1", "hello", "--file", "msg.md")
    assert code == 1 and "as text or with --file, not both" in err
    code, _, err = rl("agent", "tell", "researcher-1")
    assert code == 1 and "the message text is empty" in err


def test_a_new_coordinator_session_supersedes_the_old_one(project):
    old = tasks.start_session(project, "coordinator", "claude", "sess-a", model_observed="claude-sonnet-5-5")
    new = tasks.start_session(project, "coordinator", "codex", "thread-b", model_observed="gpt-6-astra")
    again = tasks.start_session(project, "coordinator", "codex", "thread-b", model_observed="gpt-6-astra")
    assert again["id"] == new["id"] and again["reused"]
    with project.read() as tx:
        states = {s["id"]: s["status"] for s in tx.all("Session")}
    assert states == {old["id"]: "superseded", new["id"]: "active"}


def test_a_session_ends_when_it_is_ended(project, monkeypatch):
    researcher = tasks.start_session(project, "researcher-1", "claude", "sess-r")
    old = tasks.start_session(project, "coordinator", "claude", "sess-a")
    monkeypatch.setattr(tasks, "now_utc", lambda: "2026-10-31T12:00:00+00:00")
    tasks.end_session(project, researcher["id"])
    tasks.start_session(project, "coordinator", "codex", "thread-b")  # supersedes the old coordinator
    with project.read() as tx:
        assert {tx.require(s["id"])["ended_at"] for s in (researcher, old)} == {"2026-10-31T12:00:00+00:00"}


def test_sealed_contract_does_not_change_when_the_draft_is_edited(project, tmp_path):
    research.seal_experiment(project, "E-0001")
    draft = tmp_path / "research/experiments/E-0001.md"
    draft.write_text(draft.read_text().replace("comparator: no wd", "comparator: tuned wd"))
    with project.read() as tx:
        rev = documents.resolve(tx, tx.require("E-0001")["contract_ref"])
    assert "comparator: no wd" in documents.read(project.project, rev)
    assert research.seal_experiment(project, "E-0001")["contract_ref"] == "DOC-0002@2"


def test_revising_a_hypothesis_keeps_old_evidence_on_the_old_revision(project):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    ev = research.add_evidence(project, "acc up", "screening", "valid", supports=["H-0001"], attempts=["ATT-0001-1"])
    out = research.revise_hypothesis(project, "H-0001", "narrow", scope="toy, 20% noise only", base_revision=1)
    assert "earlier revisions" in out["summary"]
    with pytest.raises(RLError) as exc:
        research.revise_hypothesis(project, "H-0001", "again", scope="x", base_revision=1)
    assert exc.value.code == "STALE_REVISION"
    with project.read() as tx:
        h = tx.require("H-0001")
        edge = [e for e in tx.inc("H-0001") if e["id"] == ev["id"]][0]
    assert h["revision"] == 2 and h["evidence_status"] == "untested" and edge["revision"] == 1
    research.link(project, ev["id"], "supports", "H-0001")
    with project.read() as tx:
        assert {e["revision"] for e in tx.inc("H-0001") if e["id"] == ev["id"]} == {1, 2}


def test_invalid_evidence_flags_only_its_dependents(project):
    research.add_hypothesis(project, "other", "unrelated claim")
    ev = research.add_evidence(project, "acc up", "screening", "valid", supports=["H-0001"])
    other = research.add_evidence(project, "other", "screening", "valid", supports=["H-0002"])
    d = research.decide(project, ["H-0001"], "continue", "signal", based_on=[ev["id"]])
    research.set_evidence(project, ev["id"], "invalid", "noise was never injected")
    with project.read() as tx:
        assert tx.require("H-0001")["needs_recheck"] and tx.require(d["id"])["needs_recheck"]
        assert not tx.require("H-0002").get("needs_recheck") and tx.require(other["id"])["validity"] == "valid"


def test_rejecting_a_result_flags_what_rests_on_it(project):
    research.add_hypothesis(project, "other", "unrelated claim")
    att = runs.register(project, "E-0001", dict(SPEC), "r1")["attempt"]
    runs.record(project, obs(state="ended", outcome="success"))
    res = runs.import_result(project, att, {"status": "complete", "metrics": {"acc": 0.8}}, "res1")["result"]
    ev = research.add_evidence(project, "acc up", "screening", "valid", supports=["H-0001"], attempts=[att],
                               results=[res], experiment="E-0001")["id"]
    other = research.add_evidence(project, "other", "screening", "valid", supports=["H-0002"])["id"]
    on_ev = research.decide(project, ["H-0001"], "continue", "signal", based_on=[ev])["id"]
    on_res = research.decide(project, ["E-0001"], "continue", "the number itself", based_on=[res])["id"]
    unrelated = research.decide(project, ["H-0002"], "continue", "x", based_on=[other])["id"]
    out = runs.reject_result(project, res, "wrong labels")
    assert sorted(out["flagged"]) == sorted(["H-0001", on_ev, on_res]) and "re-check" in out["summary"]
    with project.read() as tx:
        assert tx.require(on_ev)["recheck_reason"] == f"{res} rejected: wrong labels"
        assert not tx.require("H-0002").get("needs_recheck") and not tx.require(unrelated).get("needs_recheck")
        assert tx.require(ev)["validity"] == "valid"  # whether it still holds is the researcher's judgment


@pytest.mark.parametrize("relation", ["nearest_work", "cites", "motivated_by"])
def test_a_relinked_paper_takes_the_new_assessment(project, relation):
    """A revision-bound link (nearest_work, cites) is kept per revision of the hypothesis; motivated_by is one."""
    from smooth_research import literature
    paper = literature.add_paper(project, title="Noise and decay", authors=["A. Author"], year=2024)["id"]
    literature.link_paper(project, "H-0001", paper, relation, "search_hit")
    literature.link_paper(project, "H-0001", paper, relation, "checked", locator="section 3")
    literature.link_paper(project, "H-0001", paper, relation, "checked", locator="section 3")
    research.revise_hypothesis(project, "H-0001", "narrow", scope="toy, 20% noise only", base_revision=1)
    literature.link_paper(project, "H-0001", paper, relation, "checked", locator="section 4")
    with project.read() as tx:
        edges = sorted((e["source_revision"], e["assessment"], e["locator"]) for e in tx.out("H-0001", relation))
    kept = [] if relation == "motivated_by" else [(1, "checked", "section 3")]
    assert edges == kept + [(2, "checked", "section 4")]


def test_failed_positive_control_is_a_diagnostic_not_a_refutation(project):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    runs.record(project, obs(state="ended", outcome="failed"))
    out = research.add_evidence(project, "positive control did not learn", "diagnostic", "valid",
                                attempts=["ATT-0001-1"], experiment="E-0001")
    assert any("diagnose" in w for w in out["warnings"])
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "untested"


def test_decisions_need_reopen_conditions_and_explicit_reopening(project):
    with pytest.raises(RLError):
        research.decide(project, ["H-0001"], "pause", "no signal")
    research.decide(project, ["H-0001"], "pause", "no signal", reopen_condition="a better writer exists")
    with pytest.raises(RLError):
        research.decide(project, ["H-0001"], "continue", "try again")
    research.decide(project, ["H-0001"], "reopen", "new evidence")
    with pytest.raises(RLError):
        research.decide(project, ["H-0001"], "recommend_formal", "x", kind="stage")
    research.decide(project, ["H-0001"], "recommend_formal", "ready", kind="stage", implementation_ready="yes",
                    evidence_scope="exploratory signal on toy data")


def test_a_decision_takes_its_reason_from_its_file_and_may_rest_on_an_earlier_one(project, tmp_path, rl):
    (tmp_path / "why.md").write_text("\n## Pause: no signal at three seeds\n\nThe details.\n", encoding="utf-8")
    first = research.decide(project, ["H-0001"], "pause", None, reopen_condition="a better writer exists",
                            rationale_file="why.md")
    with pytest.raises(RLError, match="give the decision's reason"):
        research.decide(project, ["H-0001"], "reopen", "  ")
    (tmp_path / "blank.md").write_text("#\n\n", encoding="utf-8")
    with pytest.raises(RLError, match="give the decision's reason"):
        research.decide(project, ["H-0001"], "reopen", "", rationale_file="blank.md")
    ev = research.add_evidence(project, "wd wins at three seeds", "screening", "valid", supports=["H-0001"])["id"]
    code, out, _ = rl("decide", "--targets", "H-0001", "--action", "reopen", "--rationale", "seeds agree",
                      "--rationale-file", "why.md", "--based-on", first["id"], "--based-on", ev)
    assert code == 0
    second = out.split()[0]
    with project.read() as tx:
        d1, d2 = tx.require(first["id"]), tx.require(second)
        assert (d1["rationale"], d2["rationale"]) == ("Pause: no signal at three seeds", "seeds agree")
        assert d1["rationale_ref"] and sorted(e["id"] for e in tx.out(second, "based_on")) == sorted([first["id"], ev])
    with pytest.raises(RLError, match="cycle"):  # a reference, never a loop
        research.link(project, first["id"], "based_on", second)
    research.link(project, first["id"], "based_on", ev)
    third = research.decide(project, ["H-0001"], "continue", "go on", based_on=[second])["id"]
    flagged = research.set_evidence(project, ev, "invalid", "wrong split")
    with project.read() as tx:
        assert [bool(tx.require(d).get("needs_recheck")) for d in (first["id"], second, third)] == [True, True, False]
    assert sorted(flagged["flagged"]) == sorted(["H-0001", first["id"], second])


def test_dependency_and_derivation_cycles_are_refused(project):
    research.add_hypothesis(project, "child", "x", derived_from=["H-0001"])
    with pytest.raises(RLError, match="cycle"):
        research.link(project, "H-0001", "derived_from", "H-0002")
    tasks.create_task(project, "a", "other")
    tasks.create_task(project, "b", "other", depends_on=["T-0001"])
    with pytest.raises(RLError, match="cycle"):
        research.link(project, "T-0001", "depends_on", "T-0002")


def test_an_exception_inside_a_write_leaves_no_partial_records(project):
    with pytest.raises(RuntimeError):
        with project.write() as tx:
            tx.create("Evidence", {"id": tx.next_id("Evidence"), "summary": "half", "validity": "valid",
                                   "evidence_level": "screening"})
            raise RuntimeError("crash inside the transaction")
    with project.read() as tx:
        assert tx.all("Evidence") == [] and "seq_EV" not in tx.meta()


CRASH = """
import os, sys
sys.path.insert(0, {src!r})
from smooth_research.project import find_project
from smooth_research.store import Store
os.chdir({root!r})
store = Store(find_project(), "crash")
with store.write() as tx:
    for i in range(200):
        tx.create("Evidence", {{"id": tx.next_id("Evidence"), "summary": "x", "validity": "valid",
                               "evidence_level": "screening"}})
    print("ready", flush=True)
    import time; time.sleep(30)
"""


def test_a_killed_writer_leaves_a_consistent_store(project, tmp_path):
    src = str(__import__("pathlib").Path(research.__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, "-c", CRASH.format(src=src, root=str(tmp_path))],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    with project.read() as tx:
        assert tx.all("Evidence") == []
    research.add_evidence(project, "after crash", "screening", "pending")
    with project.read() as tx:
        assert [e["id"] for e in tx.all("Evidence")] == ["EV-0001"]


def test_an_interrupted_seal_leaves_only_an_unregistered_object(project, tmp_path):
    (tmp_path / "research/notes/a.md").write_text("analysis")
    documents.prepare(project.project, "research/notes/a.md", "note")  # object written, never registered
    report = snapshot.doctor(project)
    assert report["unregistered_objects"] and not report["problems"]


def test_export_restore_and_resume_without_any_session(project, tmp_path):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    runs.record(project, obs(state="running", execution_ref={"kind": "hpc_job", "executor_namespace": "pbs",
                                                             "opaque_id": "77"}))
    runs.record(project, obs(state="unknown", at="2026-10-30T05:00:00Z"))
    tasks.create_task(project, "wait for run", "experiment", ["E-0001"], to_role="researcher")
    tasks.claim(project, "T-0001", "researcher")
    tasks.progress(project, "T-0001", "waiting", "researcher", attempts=["ATT-0001-1"],
                   next_action="check job 77 with qstat")
    out = snapshot.export(project, str(tmp_path / "snap"))
    restored = snapshot.restore(out["path"], str(tmp_path / "restored"))
    assert restored["problems"] == []
    text = restored["summary"]
    assert "ATT-0001-1" in text and "pbs/77" in text and "check job 77" in text and "last trusted running" in text
    store2 = Store(find_project(str(tmp_path / "restored")))
    with store2.read() as tx, project.read() as tx0:
        assert tx.meta()["data_version"] == tx0.meta()["data_version"]
        assert len(tx.all("Observation")) == len(tx0.all("Observation"))
    with pytest.raises(RLError):
        snapshot.restore(out["path"], str(tmp_path / "restored"))


def test_export_of_the_records_only_prints_them_and_writes_nothing(project, rl, tmp_path):
    def files():  # the database and its lock live in research/state
        return sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if "state" not in p.parts)

    before = files()
    code, out, _ = rl("export", "--records-only")
    assert code == 0
    payload = json.loads(out)
    assert payload["format"] == snapshot.FORMAT and [n["id"] for n in payload["nodes"]["Hypothesis"]] == ["H-0001"]
    code, out, _ = rl("--json", "export", "--records-only")
    assert code == 0 and json.loads(out)["nodes"] == payload["nodes"]  # the records themselves, not a string of them
    assert files() == before
    code, _, err = rl("export", "--records-only", "--out", "snap")
    assert code == 1 and "writes no directory" in err and not (tmp_path / "snap").exists()


def test_hypotheses_and_evidence_are_listed_by_label_and_filtered(project, rl):
    research.add_hypothesis(project, "LN helps", "layer norm raises accuracy", "toy")
    research.add_hypothesis(project, "WD on wide nets", "the gain grows with width", "toy", derived_from=["H-0001"])
    research.decide(project, ["H-0002"], "pause", "later", reopen_condition="H1 settles")
    listed = research.list_hypotheses(project)
    assert [h["label"] for h in listed["hypotheses"]] == ["H1", "H1.1", "H2"]
    assert listed["summary"].splitlines()[1] == "H1.1@1 [active/untested] WD on wide nets"
    assert research.list_hypotheses(project, disposition="paused")["summary"] == "H2@1 [paused/untested] LN helps"
    assert research.list_hypotheses(project, status="supported")["summary"] == "no supported hypotheses"
    good = research.add_evidence(project, "wd wins at three seeds", "screening", "valid", supports=["H-0001"])["id"]
    bad = research.add_evidence(project, "ln does nothing", "screening", "valid", challenges=["H-0002"])["id"]
    research.set_evidence(project, bad, "invalid", "wrong split")
    code, out, _ = rl("evidence", "list")
    assert code == 0 and out.splitlines()[:2] == [f"{good} [screening/valid] wd wins at three seeds (supports H1@1)",
                                                  f"{bad} [screening/invalid] ln does nothing (challenges H2@1)"]
    assert research.list_evidence(project, validity="invalid")["evidence"][0]["id"] == bad
    assert research.list_evidence(project, level="confirmatory")["summary"] == "no confirmatory evidence"
    code, out, _ = rl("--json", "hypothesis", "list", "--disposition", "active")
    assert code == 0 and [h["id"] for h in json.loads(out)["hypotheses"]] == ["H-0001", "H-0003"]


def test_a_command_that_waits_in_vain_for_the_store_says_who_holds_it(project, monkeypatch):
    import socket
    from smooth_research import store as store_module
    monkeypatch.setattr(store_module, "LOCK_TIMEOUT_S", 0.2)
    with project._lock(exclusive=True):  # a writer: this process, noted in the lock file
        with pytest.raises(RLError) as exc:
            research.add_hypothesis(project, "x", "y", "toy")
        with pytest.raises(RLError) as read_exc:
            research.list_hypotheses(project)
    assert exc.value.code == "LOCKED" and "the research store stayed busy: waited " in exc.value.message
    assert f"s to write; held by process {os.getpid()} on {socket.gethostname()} (rl " in exc.value.message
    assert "Nothing was written; run the command again (it is not retried" in exc.value.message
    assert f"s to read; held by process {os.getpid()} on" in read_exc.value.message
    research.add_hypothesis(project, "x", "y", "toy")  # the lock is free again, and its writer note is cleared
    with project._lock(exclusive=False):  # readers only
        with pytest.raises(RLError) as exc:
            research.add_hypothesis(project, "z", "y", "toy")
    assert "no writer holds it, so readers do" in exc.value.message
    with project.read() as tx:
        assert [h["title"] for h in tx.all("Hypothesis")][-1] == "x"


def test_context_packs_respect_their_budget_and_list_what_they_omit(project):
    tasks.create_task(project, "seeds", "experiment", ["E-0001"], to_role="researcher")
    for i in range(30):
        research.add_evidence(project, f"evidence {i} " + "detail " * 40, "screening", "valid", supports=["H-0001"])
    pack = context.work_pack(project, "T-0001", budget=600)
    assert pack["tokens"] <= 900 and pack["omitted"]
    assert "estimate" in pack["token_method"]


def test_cli_summary_json_and_error_codes(project, rl):
    code, out, _ = rl("status")
    assert code == 0 and "H1@1 [active/untested] WD helps" in out
    code, out, _ = rl("--json", "show", "H-0001")
    assert code == 0 and json.loads(out)["record"]["id"] == "H-0001"
    code, _, err = rl("show", "H-9999")
    assert code == 1 and "error[NOT_FOUND]" in err
    rl("task", "create", "--title", "t", "--kind", "other")
    rl("task", "claim", "T-0001", "--role", "a")
    code, _, err = rl("task", "claim", "T-0001", "--role", "b")
    assert code == 3 and "CONFLICT" in err
    code, out, _ = rl("run", "register", "--experiment", "E-0001", "--request-id", "x", "--arm", "a",
                      "--code-unknown", "--config-digest", "unknown", "--data-revision", "d", "--metric", "m",
                      "--output-spec", "o", "--param", "lr=0.1")
    assert code == 0 and "ATT-0001-1 prepared" in out
    code, _, err = rl("run", "record", "--attempt", "ATT-0001-1", "--source", "shell", "--state", "running",
                      "--exec-id", "12")
    assert code == 1 and "error[INVALID]: execution_ref needs executor_namespace" in err
    code, out, _ = rl("run", "record", "--attempt", "ATT-0001-1", "--source", "shell", "--state", "ended",
                      "--outcome", "success", "--exec-namespace", "host:x:boot", "--exec-id", "12")
    assert code == 0 and "ended/success" in out


def test_session_start_hook_reports_project_state(project, monkeypatch, tmp_path):
    from smooth_research import hooks
    monkeypatch.setenv("SMOOTH_RESEARCH_RL", "/opt/plugin/bin/rl")
    out = hooks.session_start("claude", json.dumps({"cwd": str(tmp_path), "session_id": "s1", "model": "claude-x"}))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "/opt/plugin/bin/rl session start --role coordinator --host claude --host-session-id s1" in text
    elsewhere = hooks.session_start("codex", json.dumps({"cwd": "/"}))
    assert "available" in elsewhere["hookSpecificOutput"]["additionalContext"]


def test_configuration_keys_the_cli_does_not_read_are_reported(project, rl, tmp_path):
    import yaml
    from smooth_research import hooks
    from smooth_research.project import DEFAULT_CONFIG
    cfg = tmp_path / "smooth-research.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data == DEFAULT_CONFIG and project.project.warnings == []
    code, out, _ = rl("status")
    assert code == 0 and "warning" not in out

    # a typo: the commands still run and say what they do not read
    data["contxt"] = {"work_target_tokens": 100}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    code, out, _ = rl("status")
    assert code == 0 and "H1@1" in out
    assert "warning: smooth-research.yaml: unknown key contxt has no effect (a typo?)" in out
    code, out, _ = rl("--json", "status")
    assert len(json.loads(out)["warnings"]) == 1
    hook = hooks.session_start("claude", json.dumps({"cwd": str(tmp_path)}))["hookSpecificOutput"]
    assert "- configuration: smooth-research.yaml: unknown key contxt" in hook["additionalContext"]

    # an entry that does not parse and an advice limit that is not a count are named too
    data = {**DEFAULT_CONFIG, "models": {**DEFAULT_CONFIG["models"], "reviewer": ["codex-gpt-6-astra"]},
            "advice": {"max_rounds": "three"}}
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    code, out, _ = rl("models")
    assert code == 0 and "models.reviewer: model entry 'codex-gpt-6-astra' must be" in out
    assert "advice.max_rounds must be a whole number of rounds, not 'three'; using 3" in out
    assert "codex-gpt-6-astra: invalid" in out and "advice rounds used: 0 of 3" in out


def test_nothing_in_the_configuration_stops_a_command(project, rl, tmp_path):
    from smooth_research import hooks
    from smooth_research.adapters import Entry
    from smooth_research.project import DEFAULT_CONFIG, agent_args, model_entries, usable_entries
    cfg = tmp_path / "smooth-research.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data.update(version=2, context=None, advice={"max_rounds": 5},
                models={**data["models"], "reviewer": ["codex-gpt-6-astra", "claude:claude-opus-5-5"], "advisor": 5},
                agents={"args": {"claude": "--permission-mode auto --verbose", "codex": 7}})
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    p = find_project()
    assert usable_entries(p.config, "reviewer") == [Entry.parse("claude:claude-opus-5-5")]  # the typo is skipped
    assert model_entries(p.config, "advisor") == DEFAULT_CONFIG["models"]["advisor"]
    assert agent_args(p.config, "claude") == ["--permission-mode", "auto", "--verbose"] and agent_args(p.config, "codex") == []
    code, out, _ = rl("resume", "--role", "coordinator")
    assert code == 0 and "# Resume pack" in out and "## Identity" in out
    for text in ("version 2 is not known; read as version 1", "context must hold keys like the default, not None",
                 "models.reviewer: model entry 'codex-gpt-6-astra' must be", "it is skipped",
                 "models.advisor must be a list of model entries, not 5; using the defaults",
                 "agents.args.codex must be a list of arguments, not 7; using []"):
        assert text in out, text

    # a file that does not parse: the settings read the last time it did stay in use
    cfg.write_text("models: [unclosed\n", encoding="utf-8")
    code, out, _ = rl("models")
    assert code == 0 and "advice rounds used: 0 of 5" in out
    assert "smooth-research.yaml: the file could not be read (" in out
    assert "the settings read the last time it could be are used until it is fixed" in out
    hook = hooks.session_start("claude", json.dumps({"cwd": str(tmp_path)}))["hookSpecificOutput"]["additionalContext"]
    assert "- configuration: smooth-research.yaml: the file could not be read" in hook and "Use the smooth-research" in hook


def test_the_layout_is_fixed_with_documents_for_people_in_docs_and_the_rest_in_research(project, tmp_path, rl):
    for path in ("docs/plan.md", "docs/constraints.md", "docs/explanations", "research/experiments/E-0001.md",
                 "research/tasks", "research/notes", "research/reviews", "research/advice", "research/reports",
                 "src/.gitignore"):
        assert (tmp_path / path).exists(), path
    assert sorted(p.name for p in (tmp_path / "docs").iterdir()) == ["constraints.md", "explanations", "plan.md"]
    assert not (tmp_path / "reports").exists()
    cfg = yaml.safe_load((tmp_path / "smooth-research.yaml").read_text(encoding="utf-8"))
    assert cfg["manuscript"] == {"latex": None}
    assert not {"plan", "constraints_ref", "code_root", "docs_root", "reports_root", "research_root"} & set(cfg)
    tasks.create_task(project, "adapter", "implement", [], to_role="researcher")
    pack = context.work_pack(project, "T-0001")
    assert "## Locations\ncode src/ · for people docs/ (plan, constraints" in pack["summary"]
    assert "see docs/constraints.md" in pack["summary"] and "Locations" not in pack["omitted"]
    (tmp_path / "plan2.md").write_text("# Plan\nv2\n", encoding="utf-8")
    code, _, err = rl("init", "--plan", "plan2.md")  # the imported plan is not replaced
    assert code == 3 and "already holds a different imported plan" in err
    assert "v2" not in (tmp_path / "docs/plan.md").read_text(encoding="utf-8")


def test_report_summarizes_arms_and_lives_in_research_reports(project, tmp_path):
    from smooth_research import view
    for seed, acc in ((0, 0.8), (1, 0.9)):
        out = runs.register(project, "E-0001", {**SPEC, "seed": seed}, f"r{seed}")
        runs.record(project, obs(attempt=out["attempt"], state="ended", outcome="success"))
        runs.import_result(project, out["attempt"], {"status": "complete", "metrics": {"acc": acc}}, f"res{seed}")
    path = view.report(project)["path"]
    text = open(path, encoding="utf-8").read()
    assert path.startswith(str(tmp_path / "research/reports"))
    assert "| wd | @1 | 0,1 | 2 ended/success | 0.85 (n=2) | - |" in text
