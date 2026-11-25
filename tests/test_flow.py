"""One hypothesis from the tree to the phase report, through the CLI as each role, with what waits on whom at
each step (design 2, 8.7, 8.8, 9.1)."""

import json

import flow
from conftest import git
from flow import ok, types

from smooth_research import gitops


def test_the_tree_goes_from_the_coordinator_to_a_scientist_and_back(project, rl):
    ok(rl("task", "create", "--kind", "tree", "--title", "Tree", who="coordinator"))
    assert types(project, "scientist-T-0001") == []  # queued: on the coordinator's schedule, not anyone's inbox
    ok(rl("task", "release", "T-0001", who="coordinator"))
    assert types(project, "scientist-T-0001") == ["tree-task"]


def test_a_tree_is_not_delivered_before_its_review_passes(project, rl):
    ok(rl("task", "create", "--kind", "tree", "--title", "Tree", who="coordinator"))
    ok(rl("task", "release", "T-0001", who="coordinator"))
    s = "scientist-T-0001"
    ok(rl("task", "claim", "T-0001", who=s))
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "a", "--purpose", "p", "--scope", "s", "--source",
          "Averaging", who=s))
    code, _, err = rl("task", "deliver", "T-0001", "--conclusion", "x", who=s)
    assert code == 1 and "tree review has not passed" in err and "route decision" in err


def test_plan_headings_must_be_covered_by_a_source_or_left_out(project, rl):
    ok(rl("task", "create", "--kind", "tree", "--title", "Tree", who="coordinator"))
    ok(rl("task", "release", "T-0001", who="coordinator"))
    s = "scientist-T-0001"
    ok(rl("task", "claim", "T-0001", who=s))
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "a", "--purpose", "p", "--scope", "s", "--source",
          "elsewhere", who=s))
    code, _, err = rl("task", "deliver", "T-0001", who=s)
    assert code == 1 and "plan sections with no hypothesis" in err and "Averaging" in err


def test_hypothesis_tasks_need_the_users_approval_of_the_tree(project, rl):
    flow.tree(project, rl)
    c = "coordinator"
    assert types(project, c) == ["tree"]
    ok(rl("task", "accept", "T-0001", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--reason", "only one", who=c))
    code, _, err = rl("task", "release", "T-0002", who=c)
    assert code == 1 and "not approved" in err
    ok(rl("request", "--kind", "approval", "--task", "T-0001", "--stuck", "approve", "--options", "a|b",
          "--recommend", "a", who=c))
    assert types(project, "user") == ["tree-approval"]
    code, _, err = rl("request", "answer", "RQ-0001", "--by", "coordinator", "--text", "approve", who=c)
    assert code == 1 and "the user's to answer" in err
    ok(rl("request", "answer", "RQ-0001", "--by", "user", "--text", "approve", who=c))
    assert types(project, c) == ["tree-approved"]
    ok(rl("task", "release", "T-0002", who=c))
    assert types(project, c) == []
    assert types(project, "scientist-T-0002") == ["hypothesis-task"]


def test_a_dispatch_needs_tests_and_a_review_covering_the_commit(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    code, _, err = rl("task", "dispatch", "T-0003", who=s)
    assert code == 1 and "review" in err
    ok(rl("review", "request", "T-0003", who=s))
    assert types(project, "reviewer-RV-0002") == ["review-request"]
    assert "review-request" not in types(project, s)
    ok(rl("review", "answer", "RV-0002", "--verdict", "fail", "--findings",
          json.dumps([{"category": "method", "severity": "medium", "text": "seed count", "loc": None}]),
          who="reviewer-RV-0002"))
    assert "review-verdict" in types(project, s)
    code, _, err = rl("task", "dispatch", "T-0003", who=s)
    assert code == 1
    ok(rl("review", "request", "T-0003", "--recheck-of", "RV-0002", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--close", json.dumps([{"n": 1, "status": "fixed"}]),
          who="reviewer-RV-0002"))
    ok(rl("task", "dispatch", "T-0003", who=s))
    with project.read() as tx:
        exp = tx.require("E-0001")
        task = tx.require("T-0003")
    assert exp["design_commit"] == task["design_commit"] and exp["manifest"]["arms"][0]["name"] == "single"
    assert types(project, "researcher-T-0003") == ["task-card"]
    assert "hypothesis-task" not in types(project, s)  # it waits on the researcher now


def test_a_finding_in_the_protected_scope_is_method_unless_said_otherwise(project, rl):
    flow.designed(project, rl)
    ok(rl("review", "request", "T-0003", who="scientist-T-0002"))
    code, _, err = rl("review", "answer", "RV-0002", "--verdict", "fail", "--findings", json.dumps(
        [{"category": "engineering", "severity": "low", "text": "x", "loc": "HEAD:src/avg/sim.py::error"}]),
        who="reviewer-RV-0002")
    assert code == 1 and "protected scope" in err


def test_evidence_runs_follow_the_manifest_and_deviations_are_not_evidence(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "x", "--arm", "single", "--seed", "0",
                      "--config", '{"k": 2}', who=r)
    assert code == 1 and "does not match the sealed manifest" in err
    code, out, _ = rl("--json", "run", "register", "--task", "T-0003", "--request-id", "x", "--arm", "single",
                      "--seed", "0", "--config", '{"k": 2}', "--deviation", "trying k=2", who=r)
    assert code == 0 and json.loads(out)["deviates"]
    ok(rl("run", "record", "--attempt", "ATT-0001-1", "--state", "ended", "--outcome", "success", who=r))
    out = ok(rl("--json", "run", "import-result", "--attempt", "ATT-0001-1", "--request-id", "i", "--manifest",
                json.dumps({"status": "complete", "arm": "single", "seed": 0, "config": {"k": 2}}), who=r))
    code, _, err = rl("evidence", "add", "--task", "T-0003", "--summary", "s", "--level", "diagnostic", "--results",
                      json.loads(out)["result"], who=r)
    assert code == 1 and "cannot be evidence" in err or "deviates" in err


def test_a_trial_run_is_capped(project, rl):
    flow.dispatched(project, rl)
    code, _, err = rl("run", "register", "--task", "T-0003", "--request-id", "t", "--trial", "--minutes", "600",
                      who="researcher-T-0003")
    assert code == 1 and "trial_minutes" in err


def test_a_running_job_hides_the_task_until_it_ends(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "a", "--arm", "single", "--seed", "0", "--config",
          '{"k": 1}', "--minutes", "60", who=r))
    ok(rl("run", "record", "--attempt", "ATT-0001-1", "--state", "running", who=r))
    assert types(project, r) == []  # waits on the job, not on the researcher (49)
    ok(rl("run", "ended", "--attempt", "ATT-0001-1", "--outcome", "failed", "--note", "OOM"))
    assert "job-ended" in types(project, r)
    ok(rl("run", "handled", "ATT-0001-1", "--note", "retrying with a smaller batch", who=r))
    assert types(project, r) == ["task-card"]


def miyabi(head: str, rows: list[str], final: bool = True) -> str:
    """A message as miyabi-mcp renders it (notify.render): a head line, one row per job, its footer."""
    lines = [f"[miyabi-mcp] {head} N-1 (arms): all ended (12:00 JST)", *rows,
             "PBS exit status is not application success: check the outputs before continuing.",
             "Details: job_get(job_ids=[...], tail_lines=50)."]
    if final:
        lines.append('Acknowledge: notify_ack(id="N-1", ack_token="t")  (success is not claimed here)')
    return "\n".join(lines)


def test_the_executor_command_delivery_ends_each_job_by_its_own_row(project, rl, monkeypatch):
    import io
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    for n, (arm, job) in enumerate((("single", "123456.opbs"), ("mean3", "123457.opbs")), 1):
        k = 3 if arm == "mean3" else 1
        ok(rl("run", "register", "--task", "T-0003", "--request-id", f"a{n}", "--arm", arm, "--seed", "0",
              "--config", json.dumps({"k": k}), "--minutes", "60", who=r))
        ok(rl("run", "record", "--attempt", f"ATT-000{n}-1", "--state", "queued", "--exec-kind", "hpc_job",
              "--exec-namespace", "miyabi-pbs", "--exec-id", job, who=r))

    def deliver(message: str) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"notification": "N-1", "msg_id": "m",
                                                                 "message": message})))
        return ok(rl("run", "ended"))

    def state(att: str) -> tuple:
        with project.read() as tx:
            a = tx.require(att)
        return a["execution_state"], a["outcome"]

    # a state-unknown notice names the job and its footer says "success": nothing ends
    deliver(miyabi("job state unknown in", ["- 123456.opbs single  - (unknown)  walltime -/1:00:00  log o"],
                   final=False))
    assert state("ATT-0001-1") == ("queued", "unknown")
    # a batch: each job by its own row; one still running stays pending
    out = deliver(miyabi("notification", ["- 123456.opbs single  F (success)  walltime 0:10/1:00:00  log o",
                                          "- 123457.opbs mean3  R (unknown)  walltime 0:10/1:00:00  log o"]))
    assert "ATT-0001-1" in out and "still running or unknown: ATT-0002-1" in out
    assert state("ATT-0001-1") == ("ended", "success") and state("ATT-0002-1") == ("queued", "unknown")
    deliver(miyabi("notification", ["- 123457.opbs mean3  F (failed)  walltime 0:20/1:00:00  log o"]))
    assert state("ATT-0002-1") == ("ended", "failed")


def test_acceptance_merges_into_the_mainline_after_tests(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    assert types(project, s) == ["hypothesis-task", "task-conclusion"]
    out = ok(rl("task", "accept", "T-0003", who=s))
    assert "merged" in out
    top = gitops.top(project.project)
    assert (top / "src" / "avg" / "sim.py").is_file()  # the mainline moved to the merge
    assert (top / "research" / "work" / "T-0003" / "mean3-0.json").is_file()


def test_a_merge_conflict_leaves_the_mainline_and_goes_to_the_scientist(project, rl):
    flow.delivered(project, rl)
    top = gitops.top(project.project)
    (top / "src" / "avg").mkdir(parents=True, exist_ok=True)
    (top / "src" / "avg" / "sim.py").write_text("x = 1\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "conflicting change")
    before = git(top, "rev-parse", "HEAD")
    rl("task", "accept", "T-0003", who="scientist-T-0002")
    assert git(top, "rev-parse", "HEAD") == before
    assert "merge-conflict" in types(project, "scientist-T-0002")


def test_a_verdict_is_a_draft_until_its_conclusion_review_passes(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    ok(rl("task", "accept", "T-0003", who=s))
    code, _, err = rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale",
                      "x", "--supports", "EV-0001", who=s)
    assert code == 1 and "--did" in err
    ok(rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale", "x",
          "--supports", "EV-0001", "--did", "d", "--means", "m", who=s))
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "untested"
    code, _, err = rl("decide", "--final", "D-0003", who=s)
    assert code == 1 and "conclusion review" in err
    code, _, err = rl("task", "deliver", "T-0002", "--conclusion", "x", who=s)
    assert code == 1 and "no final verdict" in err
    ok(rl("review", "request", "D-0003", "--kind", "conclusion", who=s))
    ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    assert "review-verdict" in types(project, s)
    ok(rl("decide", "--final", "D-0003", who=s))
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "supported"
        assert [e["id"] for e in tx.inc("H-0001", "supports")] == ["EV-0001"]


def test_the_whole_way_to_the_phase_report(project, rl):
    flow.concluded(project, rl)
    c = "coordinator"
    assert types(project, c) == ["hypothesis-conclusion"]
    out = ok(rl("inbox", "--item", "1", who=c))
    assert "decision D-0003 (final): supported" in out and "conclusion review RV-0003 passed" in out
    ok(rl("task", "accept", "T-0002", who=c))
    assert types(project, c) == ["phase-ended"]
    ok(rl("task", "create", "--kind", "phase", "--phase", "PH-0001", "--title", "Phase 1 conclusion", who=c))
    ok(rl("task", "release", "T-0004", who=c))
    s = "scientist-T-0004"
    ok(rl("task", "claim", "T-0004", who=s))
    code, _, err = rl("task", "deliver", "T-0004", who=s)
    assert code == 1 and "no phase decision" in err
    ok(rl("decide", "--kind", "phase", "--targets", "PH-0001", "--rationale", "done", "--highlights",
          json.dumps([{"decision": "D-0003", "why": "the plan's claim holds"}]), who=s))
    ok(rl("task", "deliver", "T-0004", "--conclusion", "phase done", who=s))
    assert types(project, "user") == ["phase-report"]
    assert types(project, c) == ["phase-conclusion"]
    ok(rl("task", "accept", "T-0004", who=c))
    out = ok(rl("doctor"))
    assert "missed" not in out
    ok(rl("view"))
    page = (project.project.root / "docs" / "research.html").read_text()
    assert "PH-0001" in page and "averaging helps here" in page


def test_a_scientist_cannot_write_for_another_role(project, rl):
    flow.tree(project, rl)
    code, _, err = rl("task", "accept", "T-0001", who="scientist-T-0001")
    assert code == 1 and "coordinator" in err
    code, _, err = rl("task", "create", "--kind", "tree", "--title", "x")
    assert code == 1 and "--as" in err
