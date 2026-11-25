"""What the records refuse, so that the mainline, the evidence and the verdicts rest on what was reviewed and tested:
the accepted code is the delivered code, a test pass is that of the current test at that commit, a merge test
decides only its own merge commit, a result is checked against the design its run was registered with, a known
finding blocks every new evidence attempt, and a verdict is final only for the statement it was reviewed for."""

import json

import flow
import yaml
from conftest import git
from flow import ok

from smooth_research import compute, gitops, manifest, runs, usage_view


def test_acceptance_merges_the_delivery_not_a_later_commit(project, rl):
    flow.tree(project, rl)
    wt = flow.worktree(project, "T-0001")
    (wt / "elsewhere.txt").write_text("not delivered\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "after delivery")
    top = gitops.top(project.project)
    before = git(top, "rev-parse", "HEAD")
    code, _, err = rl("task", "accept", "T-0001", who="coordinator")
    assert code == 3 and "not at its delivery" in err
    assert git(top, "rev-parse", "HEAD") == before and not (top / "elsewhere.txt").exists()
    with project.read() as tx:
        assert tx.require("T-0001")["state"] == "delivered"


def test_a_later_failing_test_or_a_changed_command_needs_a_new_pass(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", who="reviewer-RV-0002"))
    ok(rl("experiment", "set", "E-0001", "--semantic-tests", "false", who=s))
    rl("task", "test", "T-0003", who=s)  # fails
    code, _, err = rl("task", "dispatch", "T-0003", who=s)
    assert code == 1 and "did not pass" in err
    ok(rl("experiment", "set", "E-0001", "--semantic-tests", "cd src/avg && python3 test_sim.py", who=s))
    ok(rl("task", "test", "T-0003", who=s))
    code, _, err = rl("task", "dispatch", "T-0003", who=s)  # edited and restored after the review: review again
    assert code == 1 and "the draft changed since" in err
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    ok(rl("task", "dispatch", "T-0003", who=s))


def _job_tests(project) -> None:
    path = project.project.root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config.setdefault("compute", {})["tests"] = "job"
    path.write_text(yaml.safe_dump(config, sort_keys=False))


def test_a_merge_test_job_decides_only_its_own_merge_commit(project, rl, monkeypatch):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    out = ok(rl("task", "accept", "T-0003", who=s))
    assert "run its tests as a short job" in out
    top = gitops.top(project.project)
    main_before = git(top, "rev-parse", "HEAD")
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "m0", "--purpose", "merge-test",
                      "--minutes", "5", "--commit", main_before, who=s)
    assert code == 1 and "tests its merge commit" in err
    out = ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m1", "--purpose", "merge-test",
                "--minutes", "5", who=s))
    att = json.loads(out)["attempt"]
    # the end comes through an observation, not the executor's delivery: acceptance still finishes
    ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "success", who=s))
    with project.read() as tx:
        task = tx.require("T-0003")
    assert task["state"] == "accepted" and git(top, "rev-parse", "HEAD") == task["merge_commit"]
    ok(rl("run", "ended", "--attempt", att, "--outcome", "success"))  # again: nothing more happens
    with project.read() as tx:
        assert len([e for e in tx.all("Event", entity_id="T-0003") if e["type"] == "task.accepted"]) == 1


def test_a_run_that_names_only_its_arm_runs_the_arm_and_a_split_is_always_checked():
    m = {"arms": [{"name": "a", "seeds": [0], "split": "dev", "config": {"lr": 1}}]}
    assert manifest.effective(m, "a", None, None) == ({"lr": 1}, "dev")
    assert manifest.check_run(m, "a", 0, None, None) == []
    assert manifest.check_run(m, "a", 0, None, "test") == ["the split differs ('dev' → 'test')"]
    assert "lr (1 → 2)" in manifest.check_run(m, "a", 0, {"lr": 2}, None)[0]


def test_a_result_that_does_not_say_what_it_ran_is_not_evidence(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    out = ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0",
                who=r))
    with project.read() as tx:
        assert tx.require(json.loads(out)["run"])["spec"]["config"] == {"k": 1}  # the arm's, recorded
    ok(rl("run", "record", "--attempt", "ATT-0001-1", "--state", "ended", "--outcome", "success", who=r))
    out = ok(rl("--json", "run", "import-result", "--attempt", "ATT-0001-1", "--request-id", "i", "--manifest",
                json.dumps({"status": "complete", "metrics": {"abs_error": 0.1}}), who=r))
    assert "unverified" in json.loads(out)["summary"]
    code, _, err = rl("evidence", "add", "--task", "T-0003", "--summary", "s", "--level", "diagnostic", "--results",
                      json.loads(out)["result"], who=r)
    assert code == 1 and "cannot be checked against its run" in err


def test_a_result_is_checked_against_the_revision_its_run_was_registered_with(project, rl):
    flow.dispatched(project, rl)
    r, s = "researcher-T-0003", "scientist-T-0002"
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", who=r))
    ok(rl("experiment", "set", "E-0001", "--question", "a sharper question", "--reason", "revision 2", who=s))
    with project.read() as tx:
        assert tx.require("E-0001").get("manifest") is None  # revision 2 is a draft
        commit = tx.require("RUN-0001")["code_commit"]
    ok(rl("run", "record", "--attempt", "ATT-0001-1", "--state", "ended", "--outcome", "success", who=r))
    bad = {"status": "complete", "arm": "single", "seed": 0, "commit": commit, "config": {"k": 99}}
    out = ok(rl("--json", "run", "import-result", "--attempt", "ATT-0001-1", "--request-id", "i", "--manifest",
                json.dumps(bad), who=r))
    assert json.loads(out)["mismatches"][0]["expected"] == "the sealed manifest of E-0001@1"
    code, out, _ = rl("pack", who=r)
    assert "E-0001@1 (as sealed for this task)" in out and "config {'k': 1}" in out


def test_a_known_finding_blocks_a_retry_and_the_fast_lane(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", who=r))
    ok(rl("review", "request", "T-0003", who=r))
    finding = [{"category": "method", "severity": "high", "text": "the seed is reused", "loc": "src/avg/sim.py::error"}]
    ok(rl("review", "answer", "RV-0003", "--verdict", "fail", "--findings", json.dumps(finding),
          who="reviewer-RV-0003"))
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "b", "--arm", "single", "--seed", "0",
                      "--run", "RUN-0001", who=r)
    assert code == 1 and "no evidence run" in err
    with project.write() as tx:  # as if dispatched on the fast lane
        tx.update("T-0003", {"fast_lane": True})
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "c", "--arm", "mean3", "--seed", "0",
                      who=r)
    assert code == 1 and "RV-0003#F1" in err
    # the same request again returns the attempt it made, without new checks
    out = ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", who=r))
    assert "duplicate request" in out


def test_a_verdict_reviewed_for_one_statement_is_not_finalized_for_another(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    ok(rl("task", "accept", "T-0003", who=s))
    ok(rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale", "x",
          "--supports", "EV-0001", "--did", "d", "--means", "m", who=s))
    ok(rl("review", "request", "D-0003", "--kind", "conclusion", who=s))
    ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    ok(rl("hypothesis", "revise", "H-0001", "--statement", "the mean of 5 beats 1", "--reason", "broader", who=s))
    code, _, err = rl("decide", "--final", "D-0003", who=s)
    assert code == 1 and "reviewed for another statement" in err
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "untested"


def test_compute_steps_read_the_commit_the_run_was_registered_with(project, rl):
    flow.dispatched(project, rl)
    out = ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed",
                "0", who="researcher-T-0003"))
    att = json.loads(out)["attempt"]
    with project.read() as tx:
        commit = tx.require(json.loads(out)["run"])["code_commit"]
    assert compute._attempt_facts(project, att)[1] == commit and len(commit) == 40


def test_one_accounting_record_over_two_attempts_counts_once(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    for n, arm in enumerate(("single", "mean3"), 1):
        ok(rl("run", "register", "--task", "T-0003", "--request-id", f"a{n}", "--arm", arm, "--seed", "0", who=r))
    with project.write() as tx:
        for a in ("ATT-0001-1", "ATT-0002-1"):  # remote work: usage on this machine is not counted
            tx.update(a, {"resource": {"kind": "remote", "name": "box"}})
        runs._usage(tx, {"accounting_ref": "scheduler:1", "quantity": 3, "unit": "GPU·h", "resource_type": "gpu"},
                    ["ATT-0001-1", "ATT-0002-1"])
    with project.read() as tx:
        assert usage_view.task_usage(tx, tx.require("T-0002"))["used"] == {"gpu_hours": 3.0}
        assert usage_view.task_usage(tx, tx.require("T-0003"))["used"] == {"gpu_hours": 3.0}


def test_a_new_acceptance_is_not_decided_by_the_test_job_of_an_earlier_one(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research import tasks
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    ok(rl("task", "accept", "T-0003", who=s))
    att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m1", "--purpose",
                           "merge-test", "--minutes", "5", who=s)))["attempt"]
    ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "failed", who=s))
    with project.read() as tx:
        assert tx.require("T-0003")["merge_state"] == "conflict"
    ok(rl("task", "accept", "T-0003", who=s))  # a second acceptance of the same delivery
    assert tasks.reconcile_merges(project) == []  # the old failed job does not decide it
    with project.read() as tx:
        assert tx.require("T-0003")["state"] == "merging"
    att2 = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m2", "--purpose",
                            "merge-test", "--minutes", "5", who=s)))["attempt"]
    ok(rl("run", "record", "--attempt", att2, "--state", "ended", "--outcome", "success", who=s))
    with project.read() as tx:
        assert tx.require("T-0003")["state"] == "accepted"
    # the first request again returns what it made, though the task is no longer merging
    assert "duplicate request" in ok(rl("run", "register", "--task", "T-0003", "--request-id", "m1", "--purpose",
                                        "merge-test", "--minutes", "5", who=s))


def test_a_delivery_already_on_the_mainline_is_accepted_without_a_merge(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    top = gitops.top(project.project)
    with project.read() as tx:
        delivered = tx.require("T-0003")["delivered_commit"]
    git(top, "merge", "-q", "--ff-only", delivered)  # as if a move whose record was interrupted
    ok(rl("task", "accept", "T-0003", who=s))
    with project.read() as tx:
        task = tx.require("T-0003")
    assert task["state"] == "accepted" and git(top, "rev-parse", "HEAD") == delivered


def test_a_changed_design_is_handed_over_again_before_evidence(project, rl):
    flow.dispatched(project, rl)
    r, s = "researcher-T-0003", "scientist-T-0002"
    ok(rl("task", "block", "T-0003", "--reason", "design", "--note", "the arms need a third seed", who=r))
    ok(rl("experiment", "set", "E-0001", "--question", "with three seeds", "--reason", "revision 2", who=s))
    out = ok(rl("task", "answer", "T-0003", "--text", "use revision 2", who=s))
    assert "back with you" in out
    with project.read() as tx:
        task = tx.require("T-0003")
    assert task["holder"] == s and task["state"] == "active" and not task.get("sealed_design")
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0",
                      who=r)
    assert code == 1
    with project.write() as tx:  # a revision changed without a blocker: evidence still waits for the handoff
        tx.update("T-0003", {"holder": r, "sealed_design": {**_sealed_r1(), "revision": 1}})
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "b", "--arm", "single", "--seed", "0",
                      who=r)
    assert code == 1 and "evidence waits until the new revision is handed over" in err


def _sealed_r1() -> dict:
    return {"id": "E-0001", "manifest": {"arms": [{"name": "single", "seeds": [0], "config": {"k": 1}}]},
            "manifest_digest": "x", "design_commit": "0" * 40, "protected": []}


def test_a_test_job_records_the_command_it_ran_not_the_one_set_since(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", who="reviewer-RV-0002"))
    att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "t1", "--purpose",
                           "design-test", "--minutes", "5", who=s)))["attempt"]
    ok(rl("experiment", "set", "E-0001", "--semantic-tests", "false", who=s))
    ok(rl("task", "test", "T-0003", "--record", "pass", "--attempt", att, who=s))
    code, _, err = rl("task", "dispatch", "T-0003", who=s)  # the pass is of the old command
    assert code == 1 and "`false`" in err


def test_a_null_config_is_not_a_reported_config(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", who=r))
    with project.read() as tx:
        commit = tx.require("RUN-0001")["code_commit"]
    ok(rl("run", "record", "--attempt", "ATT-0001-1", "--state", "ended", "--outcome", "success", who=r))
    reported = {"status": "complete", "arm": "single", "seed": 0, "commit": commit, "config": None}
    out = ok(rl("--json", "run", "import-result", "--attempt", "ATT-0001-1", "--request-id", "i", "--manifest",
                json.dumps(reported), who=r))
    assert "unverified" in json.loads(out)["summary"]


def test_a_pending_candidate_has_a_branch_until_its_acceptance_ends(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    (top / "other.txt").write_text("main moved\n")  # so the candidate is a new merge commit
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who=s))
    with project.read() as tx:
        cand = tx.require("T-0003")["merge_commit"]
    assert gitops.candidate_refs(project.project) == {"T-0003": cand}
    att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m", "--purpose",
                           "merge-test", "--minutes", "5", who=s)))["attempt"]
    ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "success", who=s))
    assert gitops.candidate_refs(project.project) == {}


def test_an_acceptance_interrupted_after_the_mainline_moved_is_finished_as_tested(project, rl):
    from smooth_research import tasks
    flow.delivered(project, rl)
    top = gitops.top(project.project)
    with project.read() as tx:
        task = tx.require("T-0003")
    commit, _, _ = gitops.merge_candidate(project.project, "T-0003", task["delivered_commit"])
    with project.write() as tx:  # the acceptance was recorded and main moved, then the process died
        tx.update("T-0003", {"merge_commit": commit, "merge_state": "merging", "revision": task["revision"] + 1})
    git(top, "merge", "-q", "--ff-only", commit)
    (top / "later.txt").write_text("another task merged since\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "later work on main")
    assert any("T-0003" in x for x in tasks.reconcile_merges(project))
    with project.read() as tx:
        task = tx.require("T-0003")
    assert task["state"] == "accepted" and task["merge_commit"] == commit


def test_an_acceptance_on_an_old_reading_of_the_task_is_refused(project, rl):
    from smooth_research import tasks
    flow.delivered(project, rl)
    with project.read() as tx:
        seen = tx.require("T-0003")["revision"]
    ok(rl("task", "return", "T-0003", "--reason", "say more", who="scientist-T-0002"))
    ok(rl("task", "deliver", "T-0003", "--conclusion", "another claim", "--limitations", "none",
          who="researcher-T-0003"))
    import pytest
    from smooth_research.util import RLError
    with pytest.raises(RLError, match="changed since you read it"):
        tasks._merge(project, "T-0003", run_tests=True, seen=seen)


def test_one_test_job_gives_one_result(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", who="reviewer-RV-0002"))
    atts = [json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", f"t{n}", "--purpose",
                             "design-test", "--minutes", "5", who=s)))["attempt"] for n in (1, 2)]
    ok(rl("task", "test", "T-0003", "--record", "pass", "--attempt", atts[0], who=s))
    ok(rl("task", "test", "T-0003", "--record", "fail", "--attempt", atts[1], who=s))
    assert "recorded already" in ok(rl("task", "test", "T-0003", "--record", "pass", "--attempt", atts[0], who=s))
    code, _, err = rl("task", "dispatch", "T-0003", who=s)
    assert code == 1 and "did not pass" in err


def test_a_review_and_a_test_clear_one_revision_of_the_design(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", who="reviewer-RV-0002"))
    ok(rl("task", "test", "T-0003", who=s))
    ok(rl("task", "dispatch", "T-0003", who=s))
    r = "researcher-T-0003"
    ok(rl("task", "block", "T-0003", "--reason", "design", "--note", "another manifest", who=r))
    ok(rl("experiment", "set", "E-0001", "--question", "revision 2", "--reason", "new arms", who=s))
    ok(rl("task", "answer", "T-0003", "--text", "use revision 2", who=s))
    code, _, err = rl("task", "dispatch", "T-0003", who=s)  # the old test and review were of revision 1
    assert code == 1
    ok(rl("task", "test", "T-0003", who=s))
    code, _, err = rl("task", "dispatch", "T-0003", who=s)
    assert code == 1 and "E-0001@2" in err


def test_a_retry_of_another_design_revision_is_a_new_run():
    a = {"arm": "x", "design": {"revision": 1}}
    assert runs._comparable(a, {"arm": "x", "design": {"revision": 2}}) != runs._comparable(
        {"arm": "x", "design": {"revision": 2}}, a)
    assert runs._comparable({"arm": "x"}, a) == runs._comparable(a, {"arm": "x"})  # a run from before designs


def test_a_draft_changed_after_its_review_needs_a_new_review(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", who="reviewer-RV-0002"))
    wt = flow.worktree(project, "T-0003")
    (wt / "src" / "avg" / "other.yaml").write_text((wt / "src" / "avg" / "arms.yaml").read_text())
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "a second manifest")
    ok(rl("review", "request", "T-0003", who=s))
    ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    ok(rl("experiment", "set", "E-0001", "--manifest", "src/avg/other.yaml", who=s))  # same commit, same revision
    ok(rl("task", "test", "T-0003", who=s))
    code, _, err = rl("task", "dispatch", "T-0003", who=s)
    assert code == 1 and "the draft changed since" in err


def test_a_failure_in_the_same_second_as_the_pass_needs_another_pass():
    from smooth_research.tasks import passed_at
    t = {"tests": [{"commit": "c", "command": "x", "outcome": "fail", "at": "2026-11-25T01:00:00+00:00"},
                   {"commit": "c", "command": "x", "outcome": "pass", "at": "2026-11-25T01:00:00+00:00"}]}
    assert not passed_at(t, "c", "x")


def test_recovery_waits_for_a_merge_test_still_out(project, rl):
    from smooth_research import tasks
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who=s))
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "m", "--purpose", "merge-test", "--minutes", "5",
          who=s))
    with project.read() as tx:
        cand = tx.require("T-0003")["merge_commit"]
    git(top, "merge", "-q", "--ff-only", cand)  # the candidate reached main some other way
    assert tasks.reconcile_merges(project) == []
    with project.read() as tx:
        assert tx.require("T-0003")["state"] == "merging"


def test_candidate_branches_follow_the_records(project, rl):
    from smooth_research import tasks
    flow.delivered(project, rl)
    top = gitops.top(project.project)
    head = git(top, "rev-parse", "HEAD")
    git(top, "branch", gitops.candidate_branch(project.project, "T-0099"), head)  # left by a stopped process
    git(top, "branch", "sr-accept/otherproj/T-0003", head)  # another project's: never touched
    with project.write() as tx:  # an acceptance recorded, its branch never made
        tx.update("T-0003", {"state": "merging", "merge_state": "merging", "merge_commit": head})
    assert tasks.repair_candidates(project)
    assert gitops.candidate_refs(project.project) == {"T-0003": head}
    assert git(top, "rev-parse", "sr-accept/otherproj/T-0003") == head
    assert not tasks.repair_candidates(project)  # nothing to repair: nothing written


def test_a_waiting_merge_test_is_someones_item(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who=s))
    assert "merge-test" in flow.types(project, s)
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "m", "--purpose", "merge-test", "--minutes", "5",
          who=s))
    assert "merge-test" not in flow.types(project, s)


def test_a_test_credits_the_design_it_read(project, rl):
    flow.dispatched(project, rl)
    r, s = "researcher-T-0003", "scientist-T-0002"
    ok(rl("experiment", "set", "E-0001", "--question", "revision 2", "--reason", "redesign", who=s))
    ok(rl("task", "test", "T-0003", who=r))  # the researcher tests its handoff (revision 1)
    with project.read() as tx:
        task = tx.require("T-0003")
    assert task["tests"][-1]["design"].startswith("E-0001@1#")


def test_results_are_ordered_as_instants_whatever_their_offset():
    from smooth_research.tasks import passed_at
    t = {"tests": [{"commit": "c", "command": "x", "outcome": "fail", "at": "2026-11-25T10:01:00+00:00"},
                   {"commit": "c", "command": "x", "outcome": "pass", "at": "2026-11-25T19:00:00+09:00"}]}
    assert not passed_at(t, "c", "x")  # the pass was at 10:00 UTC, before the failure


def test_a_running_merge_test_can_be_overdue(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who=s))
    att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m", "--purpose",
                           "merge-test", "--minutes", "5", who=s)))["attempt"]
    ok(rl("run", "record", "--attempt", att, "--state", "running", who=s))
    with project.write() as tx:
        tx.update(att, {"expected_end": "2026-01-01T00:00:00+00:00"})
    assert "job-overdue" in flow.types(project, s)


def test_a_researchers_passed_review_wakes_the_researcher(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    wt = flow.worktree(project, "T-0003")
    (wt / "note.txt").write_text("a change\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "a change")
    ok(rl("review", "request", "T-0003", who=r))
    ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    assert "review-verdict" in flow.types(project, r)


def test_compute_sync_publishes_the_candidates_the_records_wait_on_and_retires_the_rest(project, rl, tmp_path):
    from smooth_research import tasks
    flow.delivered(project, rl)
    path = project.project.root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config["compute"] = {"default": "box", "tests": "job", "resources": {"box": {
        "kind": "remote", "connect": "ssh", "host": "box", "jobs": "shell", "workspace": "/w", "code": "git",
        "git_remote": "origin"}}}
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(top, "remote", "add", "origin", str(origin))
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who="scientist-T-0002"))
    with project.read() as tx:
        cand = tx.require("T-0003")["merge_commit"]
    gitops.git(top, "branch", gitops.candidate_branch(project.project, "T-0099"), cand)  # a stray local branch
    ok(rl("compute", "sync", who="scientist-T-0002"))
    remote = git(tmp_path, "--git-dir", str(origin), "for-each-ref", "--format=%(refname:short) %(objectname)")
    assert remote.count("sr-accept/") == 1 and f"/T-0003 {cand}" in remote
    ok(rl("task", "cancel", "T-0003", "--reason", "no longer needed", who="scientist-T-0002"))
    ok(rl("compute", "sync", who="scientist-T-0002"))
    remote = git(tmp_path, "--git-dir", str(origin), "for-each-ref", "--format=%(refname:short)")
    assert "sr-accept/" not in remote
    assert tasks.repair_candidates(project) is False


def test_candidates_go_to_the_push_destination_and_a_changed_branch_there_is_left_alone(project, rl, tmp_path):
    from smooth_research import compute
    flow.delivered(project, rl)
    path = project.project.root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config["compute"] = {"default": "box", "tests": "job", "resources": {"box": {
        "kind": "remote", "connect": "ssh", "host": "box", "jobs": "shell", "workspace": "/w", "code": "git",
        "git_remote": "origin"}}}
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    fetch, push = tmp_path / "fetch.git", tmp_path / "push.git"
    for bare in (fetch, push):
        git(tmp_path, "init", "-q", "--bare", str(bare))
    git(top, "remote", "add", "origin", str(fetch))
    git(top, "remote", "set-url", "--push", "origin", str(push))
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who="scientist-T-0002"))
    with project.read() as tx:
        cand = tx.require("T-0003")["merge_commit"]
    ok(rl("compute", "sync", who="scientist-T-0002"))
    refs = lambda bare: git(tmp_path, "--git-dir", str(bare), "for-each-ref", "--format=%(refname) %(objectname)")
    assert f"/T-0003 {cand}" in refs(push) and "sr-accept/" not in refs(fetch)
    branch = "refs/heads/" + gitops.candidate_branch(project.project, "T-0003")
    # another writer moved the branch at the destination after this sync listed it: the lease refuses the removal
    ok(rl("task", "cancel", "T-0003", "--reason", "no longer needed", who="scientist-T-0002"))
    real = compute._listed

    def moved_after_listing(checkout, url, prefix):
        found = real(checkout, url, prefix)
        git(tmp_path, "--git-dir", str(push), "update-ref", branch, git(top, "rev-parse", "HEAD"))
        return found
    lines, problems = [], []
    compute._listed = moved_after_listing
    try:
        compute._push_candidates(project, project.project.checkout, "origin", lines, problems)
    finally:
        compute._listed = real
    assert any("could not remove" in p for p in problems) and branch in refs(push)
    ok(rl("compute", "sync", who="scientist-T-0002"))  # the next sync retires it at the push destination
    assert "sr-accept/" not in refs(push)
    git(top, "remote", "set-url", "--push", "origin", str(tmp_path / "missing.git"))
    lines, problems = [], []
    compute._push_candidates(project, project.project.checkout, "origin", lines, problems)
    assert any("could not be listed" in p for p in problems)


def test_a_draft_edited_and_restored_while_its_test_runs_is_not_credited(project, rl, monkeypatch):
    from smooth_research import tasks
    flow.designed(project, rl)
    real = tasks.run_check

    def edit_meanwhile(store, tid, command, cwd=None):
        out = real(store, tid, command, cwd)
        for path in ("src/avg/other.yaml", "src/avg/arms.yaml"):  # edited, then put back
            with store.write() as tx:
                exp = tx.require("E-0001")
                tx.update("E-0001", {"manifest_path": path, "edit_seq": (exp.get("edit_seq") or 0) + 1})
        return out
    monkeypatch.setattr(tasks, "run_check", edit_meanwhile)
    code, _, err = rl("task", "test", "T-0003", who="scientist-T-0002")
    assert code == 3 and "changed while the test ran" in err


def test_an_attempt_corrected_to_running_during_the_failure_decision_does_not_reject_its_round(project, rl,
                                                                                              monkeypatch):
    from smooth_research import tasks
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    main = git(top, "rev-parse", "HEAD")
    ok(rl("task", "accept", "T-0003", who=s))
    run = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m1", "--purpose",
                           "merge-test", "--minutes", "5", who=s)))
    real = tasks._advance

    def corrected_meanwhile(*args, **kw):  # the failure was read; a correction lands before the decision
        with project.write() as tx:
            tx.update(run["attempt"], {"execution_state": "running", "outcome": "unknown", "closed_at": None})
        monkeypatch.setattr(tasks, "_advance", real)
        return real(*args, **kw)
    monkeypatch.setattr(tasks, "_advance", corrected_meanwhile)
    ok(rl("run", "record", "--attempt", run["attempt"], "--state", "ended", "--outcome", "failed", who=s))
    with project.read() as tx:
        task = tx.require("T-0003")
        version = tx.meta()["data_version"]
    assert task["state"] == "merging" and task["merge_state"] == "merging"
    assert tasks.reconcile_merges(project) == [] and tasks.reconcile_merges(project) == []
    with project.read() as tx:
        assert tx.meta()["data_version"] == version  # beats with nothing to decide write nothing
    with project.write() as tx:  # it ends well after all
        tx.update(run["attempt"], {"execution_state": "ended", "outcome": "success"})
    tasks.finish_merge(project, run["attempt"])
    with project.read() as tx:
        assert tx.require("T-0003")["state"] == "accepted"
    assert git(top, "rev-parse", "HEAD") != main


def test_one_passed_attempt_waits_for_the_rest_of_its_round(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    _job_tests(project)
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    main = git(top, "rev-parse", "HEAD")
    ok(rl("task", "accept", "T-0003", who=s))
    run = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m1", "--purpose",
                           "merge-test", "--minutes", "5", who=s)))
    retry = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m2", "--purpose",
                             "merge-test", "--minutes", "5", "--run", run["run"], who=s)))
    out = ok(rl("run", "record", "--attempt", run["attempt"], "--state", "ended", "--outcome", "success", who=s))
    assert git(top, "rev-parse", "HEAD") == main and "error" not in out.lower()
    ok(rl("run", "record", "--attempt", retry["attempt"], "--state", "ended", "--outcome", "failed", who=s))
    with project.read() as tx:
        assert tx.require("T-0003")["merge_state"] == "conflict"
    assert git(top, "rev-parse", "HEAD") == main


def test_a_manifest_with_top_level_arm_keys_is_warned_about_when_tested(project, rl):
    """Found in the all-Claude trial: a top-level config was silently not applied, and every import mismatched."""
    flow.designed(project, rl)
    wt = flow.worktree(project, "T-0003")
    (wt / "src" / "avg" / "arms.yaml").write_text(flow.ARMS + "config: {mu: 5}\nseeds: [0, 1]\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "shared config")
    out = ok(rl("task", "test", "T-0003", who="scientist-T-0002"))
    assert "WARNING" in out and "top-level config, seeds is not applied" in out


def test_an_artifact_in_the_task_worktree_is_read_and_digested(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    code, out, err = rl("--json", "run", "register", "--task", "T-0003", "--request-id", "r1", "--arm", "single",
                        "--seed", 0, "--config", json.dumps({"k": 1}), "--minutes", 1, who=r)
    att = json.loads(out)["attempt"]
    wt = flow.worktree(project, "T-0003")
    (wt / "research" / "work" / "T-0003").mkdir(parents=True, exist_ok=True)
    (wt / "research" / "work" / "T-0003" / "m.json").write_text("{}")
    ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "success", who=r))
    with project.read() as tx:
        commit = tx.require(tx.require(att, "Attempt")["run_id"])["code_commit"]
    res = {"status": "complete", "arm": "single", "seed": 0, "commit": commit, "config": {"k": 1},
           "metrics": {"abs_error": 1.0}, "artifacts": [{"path": "research/work/T-0003/m.json"}]}
    out = json.loads(ok(rl("--json", "run", "import-result", "--attempt", att, "--request-id", "i1", "--manifest",
                           json.dumps(res), who=r)))
    with project.read() as tx:
        assert tx.require(out["result"])["verification"][0]["check"].startswith("read_locally")


def test_candidates_reach_every_push_destination_and_no_credential_is_printed(project, rl, tmp_path):
    from smooth_research import compute
    flow.delivered(project, rl)
    path = project.project.root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config["compute"] = {"default": "box", "tests": "job", "resources": {"box": {
        "kind": "remote", "connect": "ssh", "host": "box", "jobs": "shell", "workspace": "/w", "code": "git",
        "git_remote": "origin"}}}
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    from smooth_research.project import find_project
    from smooth_research.store import Store
    project = Store(find_project(), "test")
    top = gitops.top(project.project)
    # long paths: the list of push URLs is longer than a shortened diagnostic
    bares = [tmp_path / ("d" * 120 + str(n)) / "repo.git" for n in range(3)]
    for bare in bares:
        bare.parent.mkdir()
        git(tmp_path, "init", "-q", "--bare", str(bare))
    git(top, "remote", "add", "origin", str(bares[0]))
    for bare in bares:
        git(top, "remote", "set-url", "--add", "--push", "origin", str(bare))
    (top / "other.txt").write_text("main moved\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    ok(rl("task", "accept", "T-0003", who="scientist-T-0002"))
    with project.read() as tx:
        cand = tx.require("T-0003")["merge_commit"]
    lines, problems = [], []
    compute._push_candidates(project, project.project.checkout, "origin", lines, problems)
    assert problems == []
    for bare in bares:
        assert cand in git(tmp_path, "--git-dir", str(bare), "for-each-ref", "--format=%(objectname)")
    secret = "https://someone:s3cr3t-token@127.0.0.1:9/repo.git"  # nothing listens: the listing fails
    git(top, "remote", "set-url", "--push", "origin", secret, str(bares[0]))
    lines, problems = [], []
    compute._push_candidates(project, project.project.checkout, "origin", lines, problems)
    assert problems and not any("s3cr3t" in x or "someone" in x for x in lines + problems)
    assert compute.redact(f"push to {secret} failed") == "push to https://127.0.0.1:9/repo.git failed"


def test_differing_copies_of_an_artifact_are_ambiguous_and_doctor_reads_the_file_imported(project, rl):
    from smooth_research.util import digest_file
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    wt = flow.worktree(project, "T-0003")
    rel = "research/work/T-0003/m.json"
    for where, text in ((wt, '{"a": 1}'), (project.project.root, '{"a": 0}')):  # the task's file, an older copy
        (where / rel).parent.mkdir(parents=True, exist_ok=True)
        (where / rel).write_text(text)

    def imported(n, artifact):
        code, out, err = rl("--json", "run", "register", "--task", "T-0003", "--request-id", f"r{n}", "--arm",
                            "single", "--seed", 0, "--config", json.dumps({"k": 1}), "--minutes", 1, who=r)
        att = json.loads(out)["attempt"]
        ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "success", who=r))
        with project.read() as tx:
            commit = tx.require(tx.require(att, "Attempt")["run_id"])["code_commit"]
        res = {"status": "complete", "arm": "single", "seed": 0, "commit": commit, "config": {"k": 1},
               "metrics": {"abs_error": 1.0}, "artifacts": [artifact]}
        out = json.loads(ok(rl("--json", "run", "import-result", "--attempt", att, "--request-id", f"i{n}",
                               "--manifest", json.dumps(res), who=r)))
        with project.read() as tx:
            return tx.require(out["result"])
    res = imported(1, {"path": rel})
    assert res["verification"][0]["check"] == "ambiguous_locally" and res["usable"] is False
    res = imported(2, {"path": rel, "digest": digest_file(wt / rel)})
    v = res["verification"][0]
    assert v["check"] == "verified_locally" and v["read_at"] in ("task", "checkout") and res["usable"]
    if v["read_at"] == "task":
        (wt / rel).write_text('{"a": 2}')  # the file it read changed; the main checkout's copy did not
        assert f"{res['id']}: {rel} changed since import" in ok(rl("doctor"))


def test_a_result_reports_the_configuration_it_ran_as_registered():
    from smooth_research.runs import _mismatches
    run = {"spec": {"arm": "a", "seed": "0", "config": {"lr": 1, "batch_size": 32}}, "category": "trial"}
    assert _mismatches(None, run, {"arm": "a", "seed": 0, "config": {"lr": 1, "batch_size": 32}})[0] == []
    found, _ = _mismatches(None, run, {"arm": "a", "seed": 0, "config": {"lr": 1, "batch_size": 8}})
    assert [f["field"] for f in found] == ["config"]


def test_no_credential_of_a_fetch_url_is_shown():
    from smooth_research import compute
    from smooth_research.project import Resource
    url = "https://someone:s3cr3t@example.org/r.git"
    assert "s3cr3t" not in compute._clone(url) and "example.org/r.git" in compute._clone(url)
    res = Resource(name="box", kind="remote", host="box", connect="ssh", jobs="shell", workspace="/w", code="git",
                   git_remote="origin", git_url=url)
    assert "s3cr3t" not in res.describe() and "someone" not in res.describe()
    assert "ssh://git@example.org/r.git" in compute._clone("ssh://git@example.org/r.git")  # a login, not a secret


def test_a_run_on_this_machine_counts_no_usage_and_is_not_unreported(project, rl):
    """11-25, the user's decision: local runs do not count usage time."""
    from smooth_research import runs
    from smooth_research.usage_view import task_usage, usage_line
    flow.delivered(project, rl)
    with project.read() as tx:
        task = tx.require("T-0003")
        u = task_usage(tx, task)
        assert u["used"] == {} and u["unreported"] == 0 and u["local"] == 2
        assert "2 attempt(s) on this machine, not counted" in usage_line(tx, task)
        assert runs.usage_summary(tx)["unreported_attempts"] == []


def test_a_session_works_in_the_worktree_of_the_project_it_runs_in(project, rl):
    """A project restored elsewhere: the worktree recorded at the claim names the original."""
    from smooth_research import watch
    flow.approved(project, rl)
    ok(rl("task", "claim", "T-0002", who="scientist-T-0002"))
    with project.write() as tx:
        tx.update("T-0002", {"worktree": "/elsewhere/original/.worktrees/T-0002"})
        assert watch.folder(project, tx, "scientist-T-0002") == str(gitops.checkout_in(project.project, "T-0002"))


def test_staging_archives_from_the_repository_top_and_ends_its_heredoc_at_column_one(tmp_path):
    from smooth_research import compute
    from smooth_research.project import Resource
    git(tmp_path, "init", "-q")
    (tmp_path / "examples" / "project").mkdir(parents=True)
    assert compute._top_of(str(tmp_path / "examples" / "project")) == str(tmp_path)
    res = Resource(name="c", kind="remote", host="h", connect="local", jobs="pbs", workspace="/w", code="archive")
    assert compute._remote(res, "echo hi").endswith("\nSR_EOF")


def test_usage_recorded_for_a_run_on_this_machine_is_not_counted_and_every_charge_is_an_event(project, rl):
    from smooth_research import runs
    from smooth_research.usage_view import task_usage
    flow.delivered(project, rl)
    with project.read() as tx:
        att = tx.all("Attempt")[0]["id"]
    ok(rl("run", "usage", "--accounting-ref", "local:box:1", "--quantity", "42", "--unit", "cpu_s",
          "--resource-type", "compute", "--basis", "measured", "--attempts", att, who="researcher-T-0003"))
    with project.write() as tx:
        assert task_usage(tx, tx.require("T-0003"))["used"] == {} and runs.usage_summary(tx)["totals"] == []
        out = runs._usage(tx, {"accounting_ref": "pbs:9", "quantity": 1, "unit": "node_h",
                               "resource_type": "compute"}, [att])  # as an executor's export writes it
        assert [e for e in tx.all("Event", type="run.usage") if e["entity_id"] == out["usage"]]


def test_a_manifest_naming_another_attempt_or_spec_is_refused(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", who=r))
    with project.read() as tx:
        run = tx.require("RUN-0001")
    good = {"status": "complete", "arm": "single", "seed": 0, "commit": run["code_commit"], "config": {"k": 1},
            "attempt_id": "ATT-0001-1", "spec_digest": run["spec_digest"]}
    for n, wrong in enumerate(({"attempt_id": "ATT-0009-1"}, {"spec_digest": "blake2b:00"})):
        code, _, err = rl("run", "import-result", "--attempt", "ATT-0001-1", "--request-id", f"w{n}", "--manifest",
                          json.dumps({**good, **wrong}), who=r)
        assert code != 0 and ("another run" in err or "not ATT-0001-1's" in err)
    out = json.loads(ok(rl("--json", "run", "import-result", "--attempt", "ATT-0001-1", "--request-id", "i",
                           "--manifest", json.dumps(good), who=r)))
    assert out["spec_match"] if "spec_match" in out else not out.get("mismatches")


def test_a_login_coordinator_runs_tests_as_jobs_and_units_compare_by_executor_name():
    from smooth_research.project import tests_mode
    assert tests_mode({"compute": {"coordinator": "login", "tests": "local"}}) == "job"
    assert tests_mode({"compute": {"coordinator": "local", "tests": "local"}}) == "local"
    assert usage_view.envelope("about 4 MIG·h, 0.2 node·h, 1 cpu·h") == {
        "mig_instance_hours": 4.0, "node_hours": 0.2, "cpu·h": 1.0}
    assert usage_view.unit_key("node_h") == usage_view.unit_key("node_hours") == "node_hours"


def test_a_higher_envelope_line_is_news_to_whoever_was_told_of_a_lower(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", who=r))

    with project.write() as tx:  # the route decision came earlier, not in the same second as the charges
        for d in tx.all("Decision", task_id="T-0002"):
            tx.update(d["id"], {"created_at": "2026-01-01T00:00:00Z"})

    def charge(ref, quantity):
        with project.write() as tx:
            tx.update("ATT-0001-1", {"resource": {"kind": "remote", "name": "box"}})
            runs._usage(tx, {"accounting_ref": ref, "quantity": quantity, "unit": "cpu·h", "resource_type": "cpu"},
                        ["ATT-0001-1"])

    def keys():
        return [i.key() for i in flow.items(project, "scientist-T-0002") if i.type == "usage-line"]

    charge("pbs:1", 0.85)
    assert keys() == ["usage-line:T-0002:scientist-T-0002@0.8"]
    charge("pbs:2", 0.2)
    assert keys() == ["usage-line:T-0002:scientist-T-0002@1"]


def test_the_scientist_is_told_again_for_the_verdict_when_its_last_task_ends(project, rl):
    """Codex trial, 11-25: the scientist accepted the execute task, ended its turn as told, and nothing woke it
    for the verdict (its hypothesis-task item had been told at the start)."""
    flow.delivered(project, rl)
    s = "scientist-T-0002"

    def item():
        return next(i for i in flow.items(project, s) if i.subject == "T-0002")

    before = item().key()
    ok(rl("task", "accept", "T-0003", who=s))
    after = item()
    assert after.key() != before and "write the verdict on H-0001" in after.ask
