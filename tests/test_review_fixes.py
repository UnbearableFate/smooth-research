"""Regressions for the codex review of the 2026-11-27 implementation (reports/DOING/code_review/
gpt-6.2-astra_3573dc7-wt_261127-0806.md): one test per finding, named after it."""

from __future__ import annotations

import json
import re
import shlex
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import flow
import pytest
import yaml
from conftest import git
from test_watch import fake  # noqa: F401  (the fixture)

from smooth_research import adapters, gitops, reviews, sessions, tasks, watch
from smooth_research import tree as tree_draft
from smooth_research.project import find_project
from smooth_research.roles import parse
from smooth_research.store import Store
from smooth_research.util import now_utc

S, R, C = "scientist-T-0002", "researcher-T-0003", "coordinator"
ok = flow.ok


def refused(result) -> str:
    code, out, err = result
    assert code != 0, f"expected a refusal: {out}"
    return err


def task(store, tid) -> dict:
    with store.read() as tx:
        return tx.require(tid)


def opened(store) -> dict[str, dict]:
    with store.read() as tx:
        return {s["name"]: s for s in tx.all("Session") if s["status"] == "open"}


def main_at(store) -> str:
    return gitops.rev(store.project, gitops.mainline(store.project))


def to_pane(fake_herdr, pane) -> list[str]:
    return [text for p, text in fake_herdr.prompts if p == pane]


# -- 1. a watcher from before schema 6 -----------------------------------------------------------------------


def _old_watcher_beat(store, pid, **more) -> None:
    """What a watcher from before schema 6 writes, and keeps rewriting with whatever else the file held."""
    path = store.project.state_dir / watch.LEGACY_HEARTBEAT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": now_utc(), "pid": pid, "tab": "w1:t9", **more}))


def _sleeper(*argv, cwd=None):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv], cwd=cwd)


def test_a_watcher_from_before_schema_6_is_not_this_observer_and_rl_resume_stops_it(project, rl):
    proc = _sleeper("/plugin/bin/rl", "watch", "run")  # started in the project's folder, as rl started it
    try:
        _old_watcher_beat(project, proc.pid, generation=6)  # a field it kept from a file it shared once
        assert not watch.alive(project)  # its fresh heartbeat is not the read-only observer's
        assert watch.older(project)["pid"] == proc.pid
        out = ok(rl("resume"))
        assert f"stopped the watcher from before schema 6 (pid {proc.pid}" in out
        assert proc.wait(timeout=10) != 0
    finally:
        proc.kill()


def test_an_old_watcher_of_another_project_or_another_program_is_left_alone(project, tmp_path):
    other = tmp_path / "copy-of-the-project"
    other.mkdir()
    procs = [_sleeper("/plugin/bin/rl", "watch", "run", cwd=other), _sleeper("compare smooth watch run logs"),
             _sleeper()]
    try:
        _old_watcher_beat(project, procs[0].pid)  # a copied heartbeat names another project's watcher
        assert "left running" in watch.retire_older(project)
        for proc in procs[1:]:
            _old_watcher_beat(project, proc.pid)
            assert watch.older(project) is None and watch.retire_older(project) is None
        assert all(proc.poll() is None for proc in procs)
    finally:
        for proc in procs:
            proc.kill()
    watch.write_heartbeat(project)  # this observer's own beat, in its own file, names its generation
    assert watch.heartbeat(project)["generation"] == watch.GENERATION and watch.alive(project)


# -- 2. closing only an agent seen at rest ---------------------------------------------------------------------


def test_cancelling_a_task_does_not_close_its_working_agent_behind_a_stale_pane_id(project, rl, fake):
    flow.approved(project, rl)
    s = opened(project)[S]
    pane, tab = s["herdr_pane"], s["herdr_tab"]
    agent = fake.agents.pop(pane)  # herdr renumbered it: it works on in another pane and tab, rl's label kept
    fake.tabs["w1:t9"] = fake.tabs.pop(tab)
    fake.agents["w1:p9"] = {**agent, "agent_status": "working"}
    ok(rl("task", "cancel", "T-0002", "--reason", "not needed now", who=C))
    assert "w1:t9" in fake.tabs and not any(c[:2] == ("tab", "close") for c in fake.calls)
    assert opened(project)[S]["herdr_pane"] == "w1:p9"  # bound to where it lives, and left open
    assert "left open (working)" in sessions.close(project, S, "again")


def test_an_agent_herdr_cannot_classify_is_not_closed(project, rl, fake):
    flow.approved(project, rl)
    fake.set(opened(project)[S]["herdr_pane"], "unknown")
    assert "left open (unknown)" in sessions.close(project, S, "its work is finished")
    assert S in opened(project) and not any(c[:2] == ("tab", "close") for c in fake.calls)
    fake.set(opened(project)[S]["herdr_pane"], "idle")
    assert sessions.close(project, S, "its work is finished").endswith("closed (its work is finished)")


# -- 3. every blocker and every answer reaches its recipient ---------------------------------------------------


def test_two_blockers_and_their_answers_each_reach_the_session_they_go_to(project, rl, fake):
    flow.dispatched(project, rl)
    sp, rp = opened(project)[S]["herdr_pane"], opened(project)[R]["herdr_pane"]
    for n, (question, reply) in enumerate((("which seed first?", "seed 0 first"),
                                           ("and the second arm?", "mean3 after single")), 1):
        ok(rl("task", "block", "T-0003", "--reason", "question", "--note", question, who=R))
        assert question in to_pane(fake, sp)[-1], f"blocker {n} did not reach the scientist"
        ok(rl("task", "answer", "T-0003", "--text", reply, who=S))
        assert reply in to_pane(fake, rp)[-1], f"answer {n} did not reach the researcher"
    before = len(fake.prompts)
    assert sessions.wake(project, R) == f"{R}: told already"  # the same answer read again is not news
    assert sessions.wake(project, S) == f"{S}: told already" and len(fake.prompts) == before


# -- 4. a review moved to another design at the same commit ------------------------------------------------------


def test_a_review_moved_to_a_changed_design_at_the_same_commit_is_answered_for_that_design(project, rl):
    flow.designed(project, rl)
    commit = flow.head(project, "T-0003")
    ok(rl("review", "request", "T-0003", who=S))
    ok(rl("experiment", "set", "E-0001", "--semantic-tests", "false", who=S))  # no commit changes
    out = ok(rl("review", "request", "T-0003", "--recheck-of", "RV-0001", who=S))
    assert "moved" in out
    with project.read() as tx:
        p = tx.require("RV-0001")["passes"][-1]
    assert p["commit"] == commit and reviews.redesigned(p)
    now = reviews.target_text(p)
    assert now.startswith(commit[:12] + "+E-0001@") and now in out
    assert f"--at {now}" in ok(rl("pack", who="reviewer-RV-0001"))  # what its reviewer reads
    err = refused(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", commit[:12],
                     who="reviewer-RV-0001"))
    assert "moved to the design" in err and now in err
    with project.read() as tx:
        assert tx.require("RV-0001")["status"] != "passed"
    out = json.loads(ok(rl("--json", "review", "answer", "RV-0001", "--verdict", "pass", "--at", now,
                           who="reviewer-RV-0001")))
    assert out["status"] == "passed"


def test_a_design_named_in_the_answer_is_always_checked_and_names_its_digest(project, rl):
    flow.designed(project, rl)
    commit = flow.head(project, "T-0003")
    ok(rl("review", "request", "T-0003", who=S))
    err = refused(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", f"{commit[:12]}+E-0001@9#00000000",
                     who="reviewer-RV-0001"))
    assert "waits on the design" in err
    seen = []
    for tests in ("false", "true"):  # two changes of the design while the review waits, at one commit
        ok(rl("experiment", "set", "E-0001", "--semantic-tests", tests, who=S))
        ok(rl("review", "request", "T-0003", "--recheck-of", "RV-0001", who=S))
        with project.read() as tx:
            seen.append(reviews.target_text(tx.require("RV-0001")["passes"][-1]))
    assert seen[0] != seen[1] and all(re.fullmatch(r"[0-9a-f]{12}\+E-0001@\d+#[0-9a-f]{8}", t) for t in seen)
    err = refused(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", seen[0], who="reviewer-RV-0001"))
    assert seen[1] in err
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", seen[1], who="reviewer-RV-0001"))


# -- 5, 6. what a delivery records ---------------------------------------------------------------------------------


def _prep(project, rl, check: str) -> None:
    """The scientist's preparation task T-0003 (done check `check`), dispatched to researcher-T-0003, a commit."""
    flow.approved(project, rl)
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", check, who=S))
    ok(rl("task", "dispatch", "T-0003", who=S))
    wt = flow.worktree(project, "T-0003")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "work")


def test_a_delivery_check_runs_on_the_delivered_commit_not_on_files_left_uncommitted(project, rl):
    _prep(project, rl, "test -f marker")
    (flow.worktree(project, "T-0003") / "marker").write_text("only in the worktree")
    out = ok(rl("task", "deliver", "T-0003", "--conclusion", "made", "--limitations", "none", who=R))
    assert "flag: [done-check]" in out
    t = task(project, "T-0003")
    assert t["state"] == "delivered" and t["tests"][-1]["commit"] == t["delivered_commit"]
    assert t["tests"][-1]["outcome"] == "fail"


def test_a_cancellation_while_the_delivery_check_runs_is_not_overwritten(project, rl, monkeypatch):
    _prep(project, rl, "true")
    real = tasks.run_check

    def cancelled_meanwhile(*args, **kw):
        tasks.cancel(project, parse(S), "T-0003", "not needed now")
        return real(*args, **kw)
    monkeypatch.setattr(tasks, "run_check", cancelled_meanwhile)
    err = refused(rl("task", "deliver", "T-0003", "--conclusion", "made", "--limitations", "none", who=R))
    assert "changed while it was being delivered" in err
    assert task(project, "T-0003")["state"] == "cancelled"
    with project.read() as tx:
        assert not [e for e in tx.all("Event", entity_id="T-0003") if e["type"] == "task.delivered"]


# -- 7. the code a run was registered at ---------------------------------------------------------------------------


def _register(rl, *extra):
    return rl("--json", "run", "register", "--task", "T-0003", "--arm", "single", "--seed", "0", "--config",
              json.dumps({"k": 1}), "--minutes", "1", *extra, who=R)


def test_a_retry_is_checked_as_its_run_whatever_flag_it_gives(project, rl):
    flow.dispatched(project, rl)
    first = json.loads(ok(_register(rl, "--request-id", "a")))
    run = first.get("run") or task(project, first["attempt"])["run_id"]
    sim = flow.worktree(project, "T-0003") / "src" / "avg" / "sim.py"
    sim.write_text(sim.read_text() + "\n# edited after registering\n")
    err = refused(_register(rl, "--request-id", "b", "--trial", "--run", run))
    assert "uncommitted changes" in err
    head = flow.head(project, "T-0003")  # naming the commit does not change what runs here: the worktree
    for extra in (("--commit", head), ("--trial", "--run", run, "--commit", head)):
        assert "uncommitted changes" in refused(_register(rl, "--request-id", "c" + extra[-1][:3], *extra))


def test_a_commit_the_repository_does_not_have_is_refused(project, rl):
    flow.dispatched(project, rl)
    err = refused(_register(rl, "--request-id", "a", "--commit", "f" * 40))
    assert "is not a commit of the repository" in err


# -- 8. an acceptance interrupted before the mainline moved ----------------------------------------------------------


def _interrupted(project, pid: int) -> str:
    """T-0003's local acceptance recorded its candidate and process, then stopped before the mainline moved."""
    t = task(project, "T-0003")
    commit, _, _ = gitops.merge_candidate(project.project, "T-0003", t["delivered_commit"])
    with project.write() as tx:
        tx.update("T-0003", {"merge_commit": commit, "merge_state": "merging", "revision": t["revision"] + 1,
                             "merge_proc": {"host": socket.gethostname(), "pid": pid, "at": now_utc()}})
        tx.event("task.merging", "T-0003", f"candidate {commit[:12]}")
    return commit


def test_a_pid_alive_after_the_tests_could_have_run_is_not_the_acceptance(project, rl, monkeypatch):
    flow.delivered(project, rl)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # the pid, reused since
    try:
        _interrupted(project, proc.pid)
        monkeypatch.setattr(tasks, "STALE_MERGE_MINUTES", -1)  # its acceptance started longer ago than tests take
        out = ok(rl("task", "accept", "T-0003", who=S))
        assert "starts again" in out and task(project, "T-0003")["state"] == "accepted"
    finally:
        proc.kill()


def test_an_interrupted_local_acceptance_starts_again_and_a_running_one_is_left_to_its_process(project, rl):
    flow.delivered(project, rl)
    main_before = main_at(project)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _interrupted(project, proc.pid)
        out = ok(rl("task", "accept", "T-0003", who=S))
        assert f"runs in process {proc.pid}" in out and "job" not in out
        assert task(project, "T-0003")["state"] == "delivered" and main_at(project) == main_before
    finally:
        proc.kill()
        proc.wait()
    out = ok(rl("task", "accept", "T-0003", who=S))  # its process is gone now
    assert "interrupted before the mainline moved: it starts again" in out and "accepted and merged" in out
    t = task(project, "T-0003")
    assert t["state"] == "accepted" and t.get("merge_proc") is None
    assert main_at(project) == t["merge_commit"]


# -- 9. --force after failed tests only -------------------------------------------------------------------------------


def test_a_public_preparation_whose_merge_tests_fail_waits_for_the_coordinators_force(project, rl):
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check",
          "test ! -f broken.txt", who=C))
    ok(rl("task", "release", "T-0001", who=C))
    top = gitops.top(project.project)
    (top / "broken.txt").write_text("the mainline moved on\n")
    git(top, "add", "-A")
    git(top, "commit", "-qm", "main moves on")
    wt = flow.worktree(project, "T-0001")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "work")
    with project.write() as tx:  # a scientist answered its blockers
        tx.update("T-0001", {"scientist_session": "scientist-T-0009"})
    out = json.loads(ok(rl("--json", "task", "deliver", "T-0001", "--conclusion", "made", "--limitations", "none",
                           who="researcher-T-0001")))
    assert "NOT merged" in out["summary"] and "waits on the coordinator" in out["summary"]
    assert out["wake"] == ["coordinator"]
    t = task(project, "T-0001")
    assert t["state"] == "delivered" and t["merge_state"] == "conflict"
    item = next(i for i in flow.items(project, C) if i.subject == "T-0001")
    assert item.type == "merge-conflict" and "--force" in item.cmd
    assert not [i for i in flow.items(project, "scientist-T-0009") if i.subject == "T-0001"]
    out = ok(rl("task", "accept", "T-0001", "--force", "--reason", "broken.txt is removed next", who=C))
    assert "past failing tests" in out and "flag: [force]" in out
    assert task(project, "T-0001")["state"] == "accepted"


def _job_mode(project) -> Store:
    path = project.project.root / "smooth-research.yaml"
    config = yaml.safe_load(path.read_text())
    config["compute"] = {"default": "box", "tests": "job", "resources": {"box": {
        "kind": "remote", "connect": "ssh", "host": "box", "jobs": "shell", "workspace": "/w", "code": "git"}}}
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return Store(find_project(), "test")


def test_force_in_job_mode_runs_the_test_job_first_and_applies_once_it_failed(project, rl):
    flow.delivered(project, rl)
    project = _job_mode(project)
    main_before = main_at(project)
    out = ok(rl("task", "accept", "T-0003", "--force", "--reason", "slow cluster", who=S))
    assert "none has failed for this delivery yet" in out and "run its tests as a short job" in out
    assert task(project, "T-0003")["state"] == "merging" and main_at(project) == main_before
    att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0003", "--request-id", "m", "--purpose",
                           "merge-test", "--minutes", "5", who=S)))["attempt"]
    ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "failed", who=S))
    t = task(project, "T-0003")
    assert t["state"] == "delivered" and t["merge_state"] == "conflict"
    out = ok(rl("task", "accept", "T-0003", "--force", "--reason", "the job's node lacked a GPU", who=S))
    assert f"its test job {att} failed" in json.dumps(task(project, "T-0003")["flags"]) and "flag: [force]" in out
    assert task(project, "T-0003")["state"] == "accepted"


def test_a_public_preparations_failed_test_job_is_handed_to_the_coordinator(project, rl):
    project = _job_mode(project)
    r = "researcher-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "true", who=C))
    ok(rl("task", "release", "T-0001", who=C))
    wt = flow.worktree(project, "T-0001")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "work")
    ok(rl("task", "deliver", "T-0001", "--conclusion", "made", "--limitations", "none", who=r))
    ok(rl("task", "accept", "T-0001", who=C))
    att = json.loads(ok(rl("--json", "run", "register", "--task", "T-0001", "--request-id", "m", "--purpose",
                           "merge-test", "--minutes", "5", who=r)))["attempt"]
    out = json.loads(ok(rl("--json", "run", "record", "--attempt", att, "--state", "ended", "--outcome", "failed",
                           who=r)))
    assert "coordinator" in out["wake"]
    assert any(i.type == "merge-conflict" and i.subject == "T-0001" for i in flow.items(project, C))


def test_a_public_preparation_whose_done_check_fails_goes_to_the_scientist_it_is_handed_to(project, rl):
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "false", who=C))
    ok(rl("task", "release", "T-0001", who=C))
    wt = flow.worktree(project, "T-0001")
    (wt / "f.txt").write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "work")
    out = json.loads(ok(rl("--json", "task", "deliver", "T-0001", "--conclusion", "made", "--limitations", "none",
                           who="researcher-T-0001")))
    assert out["wake"] == ["coordinator"] and "hands it to a scientist" in out["summary"]
    assert [i.type for i in flow.items(project, C) if i.subject == "T-0001"] == ["task-conclusion"]
    out = json.loads(ok(rl("--json", "task", "release", "T-0001", "--to", "scientist", who=C)))
    assert "scientist-T-0001" in out["wake"]
    assert [i.type for i in flow.items(project, "scientist-T-0001") if i.subject == "T-0001"] == ["task-conclusion"]
    assert not [i for i in flow.items(project, C) if i.subject == "T-0001"]


# -- 10. a tree draft is recorded once, its questions with it ----------------------------------------------------


def test_a_tree_draft_is_recorded_once_with_its_questions(project, rl):
    draft = flow.DRAFT + "questions:\n  - Which holdout remains eligible?\n"
    path = tree_draft.draft_path(project.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft, encoding="utf-8")
    out = ok(rl("tree", "record", who=C))
    assert "Which holdout remains eligible?" in out and not path.exists()
    with project.read() as tx:
        doc = tx.require(tx.require("D-0001")["rationale_ref"])
    assert (project.project.root / doc["object_path"]).read_text() == draft  # the exact draft, its questions too
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft, encoding="utf-8")  # the same draft again (a second recorder, or a crash before cleanup)
    out = json.loads(ok(rl("--json", "tree", "record", "--approval", "approve", who=C)))
    assert out["duplicate"] and out["id"] == "D-0001" and not path.exists()
    with project.read() as tx:
        assert len(tx.all("Hypothesis")) == 1 and len(tx.all("Task")) == 1 and len(tx.all("Decision")) == 1


def test_a_draft_with_crlf_line_ends_is_recorded_as_written(project, rl):
    path = tree_draft.draft_path(project.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = flow.DRAFT.replace("\n", "\r\n").encode()
    path.write_bytes(data)
    ok(rl("tree", "check", who=C))
    ok(rl("tree", "record", "--approval", "approve", who=C))
    with project.read() as tx:
        doc = tx.require(tx.require("D-0001")["rationale_ref"])
    assert (project.project.root / doc["object_path"]).read_bytes() == data and not path.exists()


def test_a_draft_that_is_not_utf8_is_not_recorded(project, rl):
    path = tree_draft.draft_path(project.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(flow.DRAFT.replace("mean of 3 beats 1", "runtime grows as O(n²)").encode("latin-1"))
    for args in (("tree", "check"), ("tree", "record", "--approval", "approve")):
        assert "not UTF-8" in refused(rl(*args, who=C))
    with project.read() as tx:
        assert not tx.all("Hypothesis") and not tx.all("Decision")
    assert path.exists()  # left to be saved again


# -- medium: the observer's configuration, the printed command, the launcher ---------------------------------------


def test_the_observer_reads_the_configuration_again_each_beat(project, monkeypatch):
    path = project.project.root / "smooth-research.yaml"
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt
        config = yaml.safe_load(path.read_text())
        config["agents"]["beat_seconds"] = 5
        path.write_text(yaml.safe_dump(config, sort_keys=False))
    monkeypatch.setattr(watch.time, "sleep", sleep)
    assert watch.run(project)["summary"] == "stopped"
    assert slept == [30, 5]


def test_the_printed_command_has_a_real_session_id(project, monkeypatch):
    monkeypatch.setattr(adapters, "available", lambda entry: None)
    words = shlex.split(sessions.print_command(project, "coordinator").split("   #")[0])
    assert "--session-id" in words
    uuid.UUID(words[words.index("--session-id") + 1])


@pytest.mark.parametrize("event", ["stop", "user-prompt", "session-start"])
def test_the_launcher_answers_each_hook_while_its_environment_is_built(tmp_path, event):
    launcher = Path(__file__).resolve().parents[1] / "plugin" / "bin" / "rl"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SMOOTH_RESEARCH_HOME": str(tmp_path / "home")}
    out = subprocess.run(["sh", str(launcher), "hook", event, "--host", "claude"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=30, input="{}")
    assert out.returncode == 0, out.stderr
    answer = json.loads(out.stdout)
    if event == "session-start":
        assert answer["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    else:
        assert answer == {}
