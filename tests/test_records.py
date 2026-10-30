"""Ownership, sealing, crash consistency, graph rules, recovery and the CLI surface."""

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import SPEC, obs
from smooth_research import context, documents, research, runs, snapshot, tasks
from smooth_research.project import find_project
from smooth_research.store import Store
from smooth_research.util import RLError


def _claim(root, role, queue):
    os.chdir(root)
    store = Store(find_project(), role)
    try:
        queue.put(("ok", role, tasks.claim(store, "T-0001", role)["owner_epoch"]))
    except RLError as exc:
        queue.put((exc.code, role, None))


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
    assert task["owner_role"] == winner[1] and task["owner_epoch"] == 1


def test_old_owner_cannot_write_after_handoff_and_its_products_are_kept(project, tmp_path):
    tasks.create_task(project, "analysis", "analysis", ["H-0001"], to_role="researcher")
    tasks.claim(project, "T-0001", "researcher-a")
    (tmp_path / "docs/tasks/handoff.md").write_text("state of work")
    tasks.handoff(project, "T-0001", 1, "docs/tasks/handoff.md", "researcher-a")
    assert tasks.claim(project, "T-0001", "researcher-b")["owner_epoch"] == 2
    (tmp_path / "docs/notes/late.md").write_text("late result")
    with pytest.raises(RLError) as exc:
        tasks.progress(project, "T-0001", 1, "delivered", "researcher-a", "late", ["docs/notes/late.md"])
    assert exc.value.code == "STALE_EPOCH"
    with project.read() as tx:
        assert tx.require("T-0001")["state"] == "active"
    notes = tasks.inbox(project, "coordinator")["messages"]
    assert any(m["type"] == "NOTE" and m["document_refs"] for m in notes)
    tasks.progress(project, "T-0001", 2, "delivered", "researcher-b", "done")
    tasks.review_task(project, "T-0001", "accept", "checked")


def test_inbox_cursor_and_repeated_message_delivery(project):
    first = tasks.send(project, "NOTE", "coordinator", "researcher", "hello", message_id="MSG-ext-1")
    again = tasks.send(project, "NOTE", "coordinator", "researcher", "hello", message_id="MSG-ext-1")
    assert first["id"] == again["id"]
    with pytest.raises(RLError):
        tasks.send(project, "NOTE", "coordinator", "researcher", "changed", message_id="MSG-ext-1")
    assert len(tasks.inbox(project, "researcher")["messages"]) == 1
    tasks.inbox(project, "researcher", ack=True)
    assert tasks.inbox(project, "researcher")["messages"] == []
    assert len(tasks.inbox(project, "coordinator")["messages"]) == 0  # acknowledging sends nothing


def test_coordinator_takeover_makes_the_old_epoch_stale(project):
    old = tasks.start_session(project, "coordinator", "claude", "sess-a", model_observed="claude-sonnet-5-5")
    new = tasks.start_session(project, "coordinator", "codex", "thread-b", model_observed="gpt-6-astra")
    assert (old["epoch"], new["epoch"]) == (1, 2)
    with pytest.raises(RLError) as exc:
        research.decide(project, ["H-0001"], "continue", "old session", epoch=old["epoch"])
    assert exc.value.code == "STALE_EPOCH"
    research.decide(project, ["H-0001"], "continue", "current session", epoch=new["epoch"])
    with project.read() as tx:
        states = {s["id"]: s["status"] for s in tx.all("Session")}
    assert states == {old["id"]: "superseded", new["id"]: "active"}


def test_sealed_contract_does_not_change_when_the_draft_is_edited(project, tmp_path):
    research.seal_experiment(project, "E-0001")
    draft = tmp_path / "docs/experiments/E-0001.md"
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
    (tmp_path / "docs/notes/a.md").write_text("analysis")
    documents.prepare(project.project, "docs/notes/a.md", "note")  # object written, never registered
    report = snapshot.doctor(project)
    assert report["unregistered_objects"] and not report["problems"]


def test_export_restore_and_resume_without_any_session(project, tmp_path):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    runs.record(project, obs(state="running", execution_ref={"kind": "hpc_job", "executor_namespace": "pbs",
                                                             "opaque_id": "77"}))
    runs.record(project, obs(state="unknown", at="2026-10-30T05:00:00Z"))
    tasks.create_task(project, "wait for run", "experiment", ["E-0001"], to_role="researcher")
    tasks.claim(project, "T-0001", "researcher")
    tasks.progress(project, "T-0001", 1, "waiting", "researcher", attempts=["ATT-0001-1"],
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


def test_context_packs_respect_their_budget_and_list_what_they_omit(project):
    tasks.create_task(project, "seeds", "experiment", ["E-0001"], to_role="researcher")
    for i in range(30):
        research.add_evidence(project, f"evidence {i} " + "detail " * 40, "screening", "valid", supports=["H-0001"])
    pack = context.work_pack(project, "T-0001", budget=600)
    assert pack["tokens"] <= 900 and pack["omitted"]
    assert "estimate" in pack["token_method"]


def test_cli_summary_json_and_error_codes(project, rl):
    code, out, _ = rl("status")
    assert code == 0 and "H-0001" in out
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
