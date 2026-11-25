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
        runs._usage(tx, {"accounting_ref": "scheduler:1", "quantity": 3, "unit": "GPU·h", "resource_type": "gpu"},
                    ["ATT-0001-1", "ATT-0002-1"])
    with project.read() as tx:
        assert usage_view.task_usage(tx, tx.require("T-0002"))["used"] == {"GPU·h": 3.0}
        assert usage_view.task_usage(tx, tx.require("T-0003"))["used"] == {"GPU·h": 3.0}


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
