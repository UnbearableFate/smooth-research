"""One hypothesis from the tree to the phase report, through the CLI as each role, with what waits on whom at
each step; process rules are flags on the records, refusals only for what cannot be recorded or would be false
(design 2026-11-27, 3, 5)."""

import json

import flow
from conftest import git
from flow import ok, types

from smooth_research import gitops


def flag_codes(store, rid) -> list[str]:
    with store.read() as tx:
        return [f["code"] for f in tx.require(rid).get("flags") or []]


def test_the_tree_is_built_first_and_its_hypothesis_task_goes_to_its_scientist_at_release(project, rl):
    c = "coordinator"
    assert types(project, c) == ["build-tree"]
    flow.tree(project, rl)
    assert types(project, c) == []  # the tree is recorded; the coordinator makes hypothesis tasks from its pack
    ok(rl("phase", "create", "--title", "Phase 1", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--reason", "only one", "--phase", "PH-0001", who=c))
    assert types(project, "scientist-T-0002") == []  # queued: on the coordinator's schedule, not anyone's inbox
    ok(rl("task", "release", "T-0002", who=c))
    assert types(project, "scientist-T-0002") == ["hypothesis-task"]
    with project.read() as tx:
        task = tx.require("T-0002")
    assert task["holder"] == "scientist-T-0002" and task["state"] == "active" and task["worktree"]


def test_an_approval_the_user_did_not_give_is_recorded_and_flagged(project, rl):
    flow.tree(project, rl)
    c = "coordinator"
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--reason", "over the budget", who=c))
    ok(rl("request", "--kind", "approval", "--task", "T-0002", "--stuck", "over the budget", "--options",
          "approve | change", "--recommend", "approve", who=c))
    assert "approval" in types(project, "user")[0]
    out = ok(rl("task", "release", "T-0002", who=c))  # released all the same
    assert "flag: [unapproved]" in out and "unapproved" in flag_codes(project, "T-0002")
    code, _, err = rl("request", "answer", "RQ-0002", "--by", "user", "--text", "fine", who=c)
    assert code == 1 and "--approve" in err  # an approval's answer says which: it cannot be recorded without
    out = ok(rl("request", "answer", "RQ-0002", "--by", "coordinator", "--approve", "--text", "approve", who=c))
    assert "flag: [unapproved]" in out and "the user's to answer" in out
    assert types(project, "user") == []


def test_a_dispatch_without_a_passed_review_is_flagged_not_refused(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    ok(rl("review", "request", "T-0003", who=s))
    assert types(project, "reviewer-RV-0001") == ["review-request"]
    assert "review-request" not in types(project, s)
    at = flow.head(project, "T-0003")[:12]
    ok(rl("review", "answer", "RV-0001", "--verdict", "fail", "--at", at, "--findings",
          json.dumps([{"category": "method", "severity": "medium", "text": "seed count", "loc": None}]),
          who="reviewer-RV-0001"))
    assert "review-verdict" in types(project, s)
    out = ok(rl("task", "dispatch", "T-0003", who=s))
    assert "flag: [" in out and "RV-0001#F1" in out
    assert types(project, "researcher-T-0003") == ["task-card"]


def test_a_recheck_that_passes_clears_the_dispatch(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    ok(rl("review", "request", "T-0003", who=s))
    at = flow.head(project, "T-0003")[:12]
    ok(rl("review", "answer", "RV-0001", "--verdict", "fail", "--at", at, "--findings",
          json.dumps([{"category": "method", "severity": "medium", "text": "seed count", "loc": None}]),
          who="reviewer-RV-0001"))
    ok(rl("review", "request", "T-0003", "--recheck-of", "RV-0001", who=s))
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", at, "--close",
          json.dumps([{"n": 1, "status": "fixed"}]), who="reviewer-RV-0001"))
    out = ok(rl("task", "dispatch", "T-0003", who=s))
    assert "flag:" not in out
    with project.read() as tx:
        exp = tx.require("E-0001")
        task = tx.require("T-0003")
    assert exp["design_commit"] == task["design_commit"] and exp["manifest"]["arms"][0]["name"] == "single"
    assert types(project, "researcher-T-0003") == ["task-card"]
    assert "hypothesis-task" not in types(project, s)  # it waits on the researcher now


def test_an_answer_on_another_target_than_the_rounds_is_refused(project, rl):
    flow.designed(project, rl)
    ok(rl("review", "request", "T-0003", who="scientist-T-0002"))
    code, _, err = rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", "0000000", who="reviewer-RV-0001")
    assert code == 1 and "--at" in err


def test_evidence_runs_off_the_manifest_are_recorded_as_deviations_and_flagged_as_evidence(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    code, out, err = rl("--json", "run", "register", "--task", "T-0003", "--request-id", "x", "--arm", "single",
                        "--seed", "0", "--config", '{"k": 2}', who=r)
    assert code == 0, err
    assert json.loads(out)["deviates"]
    ok(rl("run", "record", "--attempt", "ATT-0001-1", "--state", "ended", "--outcome", "success", who=r))
    out = ok(rl("--json", "run", "import-result", "--attempt", "ATT-0001-1", "--request-id", "i", "--manifest",
                json.dumps({"status": "complete", "arm": "single", "seed": 0, "config": {"k": 2}}), who=r))
    out = ok(rl("evidence", "add", "--task", "T-0003", "--summary", "s", "--level", "diagnostic", "--results",
                json.loads(out)["result"], who=r))
    assert "flag: [not-evidence]" in out and "deviates" in out


def test_a_trial_run_has_no_time_limit(project, rl):
    flow.dispatched(project, rl)
    ok(rl("run", "register", "--task", "T-0003", "--request-id", "t", "--trial", "--minutes", "600",
          who="researcher-T-0003"))


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


def test_a_note_does_not_hide_an_end_and_a_job_id_of_two_executors_is_not_guessed(project, rl, monkeypatch):
    import io
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    for n, (arm, space) in enumerate((("single", "miyabi-pbs"), ("mean3", "other-pbs")), 1):
        ok(rl("run", "register", "--task", "T-0003", "--request-id", f"a{n}", "--arm", arm, "--seed", "0",
              "--minutes", "60", who=r))
        ok(rl("run", "record", "--attempt", f"ATT-000{n}-1", "--state", "queued", "--exec-kind", "hpc_job",
              "--exec-namespace", space, "--exec-id", "42.opbs", who=r))
    message = miyabi("notification", ["- 42.opbs single  F (success)  walltime 0:10/1:00:00  log o"]) + (
        "\nYour note: compare with the job state unknown in the previous batch\n"
        "- 42.opbs single  F (failed)  walltime 0:10/1:00:00  log o (the row of the batch before)")

    def deliver(*flags) -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"message": message})))
        return ok(rl("run", "ended", *flags))

    assert "more than one executor namespace" in deliver()
    with project.read() as tx:
        assert tx.require("ATT-0001-1")["execution_state"] == "queued"
    assert "ATT-0001-1" in deliver("--namespace", "miyabi-pbs")
    with project.read() as tx:
        assert tx.require("ATT-0001-1")["outcome"] == "success"
        assert tx.require("ATT-0002-1")["execution_state"] == "queued"


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


def test_a_verdict_finalized_before_its_review_is_final_and_flagged(project, rl):
    flow.delivered(project, rl)
    s = "scientist-T-0002"
    ok(rl("task", "accept", "T-0003", who=s))
    code, _, err = rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale",
                      "x", "--supports", "EV-0001", who=s)
    assert code == 1 and "--did" in err  # a verdict says what was done: it cannot be recorded without
    ok(rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale", "x",
          "--supports", "EV-0001", "--did", "d", "--means", "m", who=s))
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "untested"
    out = ok(rl("task", "deliver", "T-0002", "--conclusion", "x", who=s))
    assert "flag: [no-verdict]" in out
    ok(rl("task", "return", "T-0002", "--reason", "finalize the verdict first", who="coordinator"))
    out = ok(rl("decide", "--final", "D-0003", who=s))
    assert "flag: [unreviewed]" in out
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "supported"
        assert [e["id"] for e in tx.inc("H-0001", "supports")] == ["EV-0001"]


def test_the_whole_way_to_the_phase_report(project, rl):
    flow.concluded(project, rl)
    c = "coordinator"
    assert types(project, c) == ["hypothesis-conclusion"]
    out = ok(rl("inbox", "--item", "1", who=c))
    assert "decision D-0003 (final): supported" in out and "conclusion review RV-0002 passed" in out
    ok(rl("task", "accept", "T-0002", who=c))
    assert types(project, c) == ["phase-ended"]
    ok(rl("task", "create", "--kind", "phase", "--phase", "PH-0001", "--title", "Phase 1 conclusion", who=c))
    ok(rl("task", "release", "T-0004", who=c))
    s = "scientist-T-0004"
    assert types(project, s) == ["phase-task"]
    ok(rl("decide", "--kind", "phase", "--targets", "PH-0001", "--rationale", "done", "--highlights",
          json.dumps([{"decision": "D-0003", "why": "the plan's claim holds"}]), who=s))
    ok(rl("task", "deliver", "T-0004", "--conclusion", "phase done", who=s))
    assert types(project, "user") == ["phase-report"]
    assert types(project, c) == ["phase-conclusion"]
    [report] = flow.items(project, "user")
    assert report.subject.startswith("RQ-") and report.subject in flow.items(project, c)[0].note
    ok(rl("task", "accept", "T-0004", who=c))
    ok(rl("request", "answer", report.subject, "--by", "user", "--text", "read it", who=c))
    assert types(project, "user") == []
    out = ok(rl("doctor"))
    assert "missed" not in out and "left over" not in out
    ok(rl("view"))
    page = (project.project.root / "docs" / "research.html").read_text()
    assert "PH-0001" in page and "averaging helps here" in page


def test_a_step_of_another_role_is_flagged_and_a_command_without_a_session_refused(project, rl):
    flow.tree(project, rl)
    out = ok(rl("task", "release", "T-0001", who="scientist-T-0002"))
    assert "flag: [role]" in out and "role" in flag_codes(project, "T-0001")
    code, _, err = rl("task", "create", "--kind", "hypothesis", "--title", "x")
    assert code == 1 and "--as" in err


def test_end_to_end_without_the_observer_prep_and_execute_at_once(project, rl):
    """Design 2026-11-27, 12 (9): from the tree draft to the final verdict and its acceptance, with no observer
    running; every handoff is made and told by the command that does it. The scientist makes a prep task and an
    execute task that waits on it (--depends-on): the prep task runs while the execute task waits, unassigned, and
    accepting the prep hands the execute task on, its worktree made from the mainline with the prep's output."""
    from smooth_research import watch
    flow.approved(project, rl)
    c, s = "coordinator", "scientist-T-0002"
    ok(rl("task", "create", "--kind", "prep", "--title", "Data", "--requirement", "data.txt with the seeds",
          "--done-check", "test -f data.txt", who=s))  # T-0003, held by the scientist that writes its card
    ok(rl("decide", "--kind", "route", "--targets", "H-0001", "--rationale", "simulate", who=s))
    ok(rl("experiment", "add", "--title", "Sim", "--question", "is mean3 better?", "--hypotheses", "H-0001", who=s))
    out = ok(rl("task", "create", "--kind", "execute", "--title", "Run sim", "--experiment", "E-0001", "--estimate",
                "0.01 cpu·h", "--depends-on", "T-0003", who=s))
    assert "waits on T-0003" in out
    with project.read() as tx:
        assert tx.require("T-0004")["state"] == "open" and not tx.require("T-0004").get("holder")
    ok(rl("task", "dispatch", "T-0003", "--note", "one file", who=s))
    assert types(project, "researcher-T-0003") == ["task-card"]
    prep = flow.worktree(project, "T-0003")
    (prep / "data.txt").write_text("0\n")
    git(prep, "add", "-A")
    git(prep, "commit", "-qm", "data")
    ok(rl("task", "deliver", "T-0003", "--conclusion", "data.txt written", "--limitations", "one seed", "--outputs",
          "data.txt", who="researcher-T-0003"))
    assert "task-conclusion" in types(project, s)
    out = ok(rl("task", "accept", "T-0003", who=s))
    assert "T-0004 waits on nothing now" in out  # the creator is told to claim it (design 3.3)
    assert "T-0004" in [i.subject for i in flow.items(project, s)]
    assert "flag:" not in ok(rl("task", "claim", "T-0004", who=s))
    with project.read() as tx:
        execute = tx.require("T-0004")
    assert execute["holder"] == s and execute["state"] == "active"
    wt = flow.worktree(project, "T-0004")
    assert (wt / "data.txt").is_file()  # made from the mainline after the prep was merged

    (wt / "src" / "avg").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "avg" / "sim.py").write_text(flow.SIM)
    (wt / "src" / "avg" / "test_sim.py").write_text(flow.TEST)
    (wt / "src" / "avg" / "arms.yaml").write_text(flow.ARMS)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "design")
    ok(rl("experiment", "set", "E-0001", "--manifest", "src/avg/arms.yaml", "--protected", "src/avg/sim.py::error",
          "--semantic-tests", "cd src/avg && python3 test_sim.py", who=s))
    ok(rl("task", "test", "T-0004", who=s))
    ok(rl("review", "request", "T-0004", who=s))
    assert types(project, "reviewer-RV-0001") == ["review-request"]
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", flow.head(project, "T-0004")[:12],
          who="reviewer-RV-0001"))
    assert "flag:" not in ok(rl("task", "dispatch", "T-0004", who=s))

    r = "researcher-T-0004"
    assert types(project, r) == ["task-card"]
    results = []
    for arm, k in (("single", 1), ("mean3", 3)):
        att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0004", "--request-id", f"r-{arm}", "--arm",
                               arm, "--seed", "0", "--config", json.dumps({"k": k}), "--minutes", "1",
                               who=r)))["attempt"]
        out_file = wt / "research" / "work" / "T-0004" / f"{arm}-0.json"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["python3", "sim.py", "--arm", arm, "--seed", "0", "--out", str(out_file)],
                       cwd=wt / "src" / "avg", check=True)
        ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "success", who=r))
        results.append(json.loads(ok(rl("--json", "run", "import-result", "--attempt", att, "--request-id",
                                        f"i-{arm}", "--manifest", out_file.read_text(), who=r)))["result"])
    ok(rl("evidence", "add", "--task", "T-0004", "--summary", "mean3 error lower", "--level", "diagnostic",
          "--results", ",".join(results), who=r))
    git(wt, "add", "research/work/T-0004")
    git(wt, "commit", "-qm", "results")
    ok(rl("task", "deliver", "T-0004", "--conclusion", "mean3 lowers the error (EV-0001)", "--limitations",
          "one seed", "--outputs", "research/work/T-0004/mean3-0.json", who=r))
    ok(rl("task", "accept", "T-0004", who=s))
    assert types(project, s) == ["hypothesis-task"]  # all its tasks are in: the verdict is next

    ok(rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale", "lower",
          "--supports", "EV-0001", "--did", "simulated both arms", "--means", "averaging helps here", who=s))
    ok(rl("review", "request", "D-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--at", "D-0003", who="reviewer-RV-0002"))
    assert "flag:" not in ok(rl("decide", "--final", "D-0003", who=s))
    assert "flag:" not in ok(rl("task", "deliver", "T-0002", "--conclusion", "averaging helps", who=s))
    assert types(project, c) == ["hypothesis-conclusion"]
    ok(rl("task", "accept", "T-0002", who=c))
    with project.read() as tx:
        assert tx.require("H-0001")["evidence_status"] == "supported"
        assert not [t["id"] for t in tx.all("Task") if t.get("flags")]
    assert watch.heartbeat(project) is None  # no observer ran
    assert "left over" not in ok(rl("doctor"))


def test_a_run_without_a_resource_in_a_login_project_is_flagged_not_refused(project, rl, monkeypatch):
    from smooth_research import runs
    flow.dispatched(project, rl)
    monkeypatch.setattr(runs, "coordinator_is_login", lambda config: True)
    out = ok(rl("run", "register", "--task", "T-0003", "--request-id", "t", "--trial", "--minutes", "1",
                who="researcher-T-0003"))
    assert "flag: [no-executor]" in out
