"""The 2026-11-27 redesign: commands hand work on and tell the next session themselves; process rules are flags,
not refusals; sessions are opened and closed by commands; hooks hand over what a busy session was not told; the
observer only observes. One regression test per problem report of chapter 10 is marked with its name."""

import json

import pytest
from conftest import git
from test_watch import FakeHerdr

from smooth_research import adapters, gitops, herdr, hooks, inbox, sessions, usage, watch


def ok(result) -> str:
    code, out, err = result
    assert code == 0, f"exit {code}: {err or out}"
    return out


def flags_of(store, rid) -> list[str]:
    with store.read() as tx:
        return [f["code"] for f in tx.require(rid).get("flags") or []]


def types(store, who) -> list[str]:
    with store.read() as tx:
        return [i.type for i in inbox.for_recipient(tx, store.project.config, who)]


def released(store, rl) -> None:
    """One hypothesis, a phase, its hypothesis task T-0001 released to scientist-T-0001."""
    c = "coordinator"
    ok(rl("hypothesis", "add", "--title", "Averaging helps", "--statement", "mean of 3 beats 1", "--purpose",
          "whether averaging is worth it", who=c))
    ok(rl("phase", "create", "--title", "Phase 1", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--reason", "only one", "--phase", "PH-0001", who=c))
    ok(rl("task", "release", "T-0001", who=c))


def commit_in(store, tid, name="f.txt", text="x") -> None:
    wt = gitops.checkout_in(store.project, tid)
    (wt / name).write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", f"{tid} {name}")


# -- holders at release, prerequisites ------------------------------------------------------------------------


def test_release_gives_the_task_to_its_session_with_a_worktree(project, rl):
    released(project, rl)
    with project.read() as tx:
        t = tx.require("T-0001")
    assert t["state"] == "active" and t["holder"] == "scientist-T-0001" and t["worktree"]
    assert "hypothesis-task" in types(project, "scientist-T-0001")


def test_researcher_T_0009_active_prerequisite_inbox(project, rl):
    """researcher-T-0009-active-prerequisite-inbox: a task handed on before its prerequisite is accepted is
    flagged, its card says whom it waits on, and accepting the prerequisite tells its holder."""
    released(project, rl)
    s = "scientist-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "data", "--requirement", "data", "--done-check", "true",
          who=s))
    out = ok(rl("task", "create", "--kind", "prep", "--title", "more", "--requirement", "x", "--done-check", "true",
                "--depends-on", "T-0002", who=s))
    assert "waits on T-0002" in out
    with project.read() as tx:
        assert tx.require("T-0003")["state"] == "open" and not tx.require("T-0003").get("worktree")
    out = ok(rl("task", "dispatch", "T-0003", who=s))
    assert "flag: [prerequisite]" in out and flags_of(project, "T-0003") == ["prerequisite"]
    with project.read() as tx:
        card = next(i for i in inbox.for_recipient(tx, project.project.config, "researcher-T-0003"))
    assert "waits on T-0002" in card.note
    ok(rl("task", "dispatch", "T-0002", who=s))
    commit_in(project, "T-0002")
    ok(rl("task", "deliver", "T-0002", "--conclusion", "data made", "--limitations", "none",
          who="researcher-T-0002"))
    code, out, _ = rl("--json", "task", "accept", "T-0002", who=s)
    assert code == 0 and "researcher-T-0003" in json.loads(out)["wake"]
    with project.read() as tx:
        after = next(i for i in inbox.for_recipient(tx, project.project.config, "researcher-T-0003"))
    assert "accepted now: merge the mainline" in after.note and after.key() != card.key()


def test_a_released_task_waiting_on_its_prerequisite_is_handed_on_when_it_is_accepted(project, rl):
    c = "coordinator"
    ok(rl("task", "create", "--kind", "prep", "--title", "a", "--requirement", "a", "--done-check", "true", who=c))
    ok(rl("task", "create", "--kind", "prep", "--title", "b", "--requirement", "b", "--done-check", "true",
          "--depends-on", "T-0001", who=c))
    ok(rl("task", "release", "T-0001", who=c))
    out = ok(rl("task", "release", "T-0002", who=c))
    assert "waits on T-0001" in out
    with project.read() as tx:
        assert tx.require("T-0002")["state"] == "open" and not tx.require("T-0002").get("holder")
    commit_in(project, "T-0001")
    out = ok(rl("task", "deliver", "T-0001", "--conclusion", "a", "--limitations", "none", who="researcher-T-0001"))
    assert "accepted and merged" in out and "T-0002 goes to researcher-T-0002" in out
    with project.read() as tx:
        t2 = tx.require("T-0002")
    assert t2["state"] == "active" and t2["holder"] == "researcher-T-0002" and t2["worktree"]


# -- flags instead of refusals --------------------------------------------------------------------------------


def test_coordinator_approval_prefix(project, rl):
    """coordinator-approval-prefix: release no longer reads the answer's first word; an approval is answered with
    an explicit choice, and releasing against a change is flagged, not refused."""
    c = "coordinator"
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope", "1 h",
          "--reason", "r", who=c))
    ok(rl("request", "--kind", "approval", "--task", "T-0001", "--stuck", "over the budget", "--options", "a|b",
          "--recommend", "a", who=c))
    code, _, err = rl("request", "answer", "RQ-0001", "--by", "user", "--text", "Approved, go", who=c)
    assert code == 1 and "--approve or --change" in err
    ok(rl("request", "answer", "RQ-0001", "--by", "user", "--change", "--text", "Approved, but smaller", who=c))
    out = ok(rl("task", "release", "T-0001", who=c))
    assert "flag: [unapproved]" in out and flags_of(project, "T-0001") == ["unapproved"]
    assert "unapproved" in ok(rl("status"))


def test_coordinator_parallel_limit_fixed(project, rl):
    """coordinator-parallel-limit-fixed and the proposal to remove it: no --parallel, no scientist limit; the
    coordinator may cancel a released hypothesis task, flagged."""
    c = "coordinator"
    for args in (("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--parallel", "2"),
                 ("phase", "create", "--title", "x", "--scientists", "1")):
        with pytest.raises(SystemExit):  # argparse: the options are gone
            rl(*args, who=c)
    released(project, rl)
    out = ok(rl("task", "cancel", "T-0001", "--reason", "not now", who=c))
    assert "flag: [not-holder]" in out and "cancelled" in out


def test_scientist_T_0002_blocked_researcher_holds_slot(project, rl):
    """scientist-T-0002-blocked-researcher-holds-slot: no slots; a blocked task holds nothing back."""
    released(project, rl)
    s = "scientist-T-0001"
    for n in range(3):
        ok(rl("task", "create", "--kind", "prep", "--title", f"p{n}", "--requirement", "r", "--done-check", "true",
              who=s))
        ok(rl("task", "dispatch", f"T-000{n + 2}", who=s))
    ok(rl("task", "block", "T-0002", "--reason", "question", "--note", "which data?", who="researcher-T-0002"))
    with project.read() as tx:
        assert [tx.require(f"T-000{n}")["state"] for n in (2, 3, 4)] == ["blocked", "active", "active"]


def test_others_doing_a_step_are_flagged_and_long_notes_kept(project, rl):
    released(project, rl)
    s = "scientist-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "true", who=s))
    ok(rl("task", "dispatch", "T-0002", who=s))
    long = "n" * 400
    out = ok(rl("task", "block", "T-0002", "--reason", "question", "--note", long, who="researcher-T-0002"))
    assert "flag: [long-note]" in out
    with project.read() as tx:
        assert tx.require("T-0002")["block_note"] == long
    out = ok(rl("task", "answer", "T-0002", "--text", "use the toy data", who="scientist-T-0099"))
    assert "flag: [not-holder]" in out


def test_delivery_without_a_passing_done_check_is_flagged_and_not_accepted_automatically(project, rl):
    c = "coordinator"
    ok(rl("task", "create", "--kind", "prep", "--title", "a", "--requirement", "a", "--done-check", "false", who=c))
    ok(rl("task", "release", "T-0001", who=c))
    commit_in(project, "T-0001")
    out = ok(rl("task", "deliver", "T-0001", "--conclusion", "a", "--limitations", "none", who="researcher-T-0001"))
    assert "flag: [done-check]" in out and "not accepted on delivery" in out
    assert "task-conclusion" in types(project, "coordinator")


def test_uncommitted_changes_are_a_reminder_at_delivery(project, rl):
    released(project, rl)
    s = "scientist-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "true", who=s))
    ok(rl("task", "dispatch", "T-0002", who=s))
    commit_in(project, "T-0002")
    (gitops.checkout_in(project.project, "T-0002") / "f.txt").write_text("changed, not committed")
    out = ok(rl("task", "deliver", "T-0002", "--conclusion", "p", "--limitations", "none", who="researcher-T-0002"))
    assert "warning:" in out and "uncommitted" in out


def test_scientist_T_0002_recheck_not_retargetable_outside_work_dir(project, rl):
    """scientist-T-0002-recheck-not-retargetable (its recurrence): a scientist's branch that changed files outside
    research/work/T-x is delivered, flagged."""
    released(project, rl)
    s = "scientist-T-0001"
    commit_in(project, "T-0001", "outside.txt")
    out = ok(rl("task", "deliver", "T-0001", "--conclusion", "nothing yet", who=s))
    assert "[outside-work-dir]" in out and "[no-verdict]" in out
    assert "delivered" in out


def test_force_moves_the_mainline_past_failing_tests_with_a_reason(project, rl):
    released(project, rl)
    s = "scientist-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check",
          "test -f must-exist.txt", who=s))
    ok(rl("task", "dispatch", "T-0002", who=s))
    commit_in(project, "T-0002")
    ok(rl("task", "deliver", "T-0002", "--conclusion", "p", "--limitations", "none", who="researcher-T-0002"))
    out = ok(rl("task", "accept", "T-0002", who=s))
    assert "NOT merged" in out and "--force" in out
    with project.read() as tx:
        assert tx.require("T-0002")["state"] == "delivered"
    code, _, err = rl("task", "accept", "T-0002", "--force", who=s)
    assert code == 1 and "--reason" in err
    out = ok(rl("task", "accept", "T-0002", "--force", "--reason", "the check needs the cluster", who=s))
    assert "past failing tests" in out and "flag: [force]" in out
    with project.read() as tx:
        assert tx.require("T-0002")["state"] == "accepted"


def test_a_takeover_is_recorded_and_the_earlier_holder_is_told(project, rl):
    released(project, rl)
    s = "scientist-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "true", who=s))
    ok(rl("task", "dispatch", "T-0002", who=s))
    code, out, _ = rl("--json", "task", "claim", "T-0002", who=s)
    assert code == 0 and json.loads(out)["wake"] == ["researcher-T-0002"]
    with project.read() as tx:
        t = tx.require("T-0002")
    assert t["holder"] == s and [h["holder"] for h in t["holders"]][-2:] == ["researcher-T-0002", s]


def test_a_task_taken_before_its_release_is_flagged_not_refused(project, rl):
    c = "coordinator"
    ok(rl("hypothesis", "add", "--title", "Averaging helps", "--statement", "mean of 3 beats 1", "--purpose",
          "whether averaging is worth it", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--reason", "only one", who=c))
    out = ok(rl("task", "claim", "T-0001", who="scientist-T-0001"))
    assert "flag: [role]" in out and "not released yet" in out
    with project.read() as tx:
        task = tx.require("T-0001")
    assert task["state"] == "active" and task["holder"] == "scientist-T-0001" and task["worktree"]


def test_doctor_finds_no_items_left_on_ended_records(project, rl):
    released(project, rl)
    ok(rl("task", "cancel", "T-0001", "--reason", "stop", who="coordinator"))
    out = ok(rl("doctor"))
    assert "left over" not in out and "missed" not in out


def test_coordinator_stale_items_keep_sessions_open_closes_sessions_of_ended_work(project, rl, fake):
    """coordinator-stale-items-keep-sessions-open: accepting or cancelling a task closes its idle sessions; a busy
    one is told to stop."""
    released(project, rl)
    s = "scientist-T-0001"
    ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "true", who=s))
    ok(rl("task", "create", "--kind", "prep", "--title", "q", "--requirement", "r", "--done-check", "true", who=s))
    ok(rl("task", "dispatch", "T-0002", who=s))  # opens researcher-T-0002 (fake herdr)
    ok(rl("task", "dispatch", "T-0003", who=s))
    open_now = _open(project)
    assert {"researcher-T-0002", "researcher-T-0003"} <= set(open_now)
    fake.set(open_now["researcher-T-0003"]["herdr_pane"], "working")
    ok(rl("task", "cancel", "T-0002", "--reason", "not needed", who=s))
    out = ok(rl("task", "cancel", "T-0003", "--reason", "not needed", who=s))
    assert "researcher-T-0002" not in _open(project)
    assert "left open (working)" in out and "researcher-T-0003" in _open(project)
    assert "task-cancelled" in types(project, "researcher-T-0003")


# -- telling sessions -----------------------------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch):
    h = FakeHerdr()
    monkeypatch.setattr(herdr, "call", h.call)
    monkeypatch.setattr(herdr, "problem", lambda: None)
    monkeypatch.setattr(herdr, "read", lambda target: "")
    monkeypatch.setattr(adapters, "available", lambda entry: None)
    monkeypatch.setattr(usage, "low_hosts", lambda store: {})
    return h


def _open(store) -> dict[str, dict]:
    with store.read() as tx:
        return {s["name"]: s for s in tx.all("Session") if s["status"] == "open"}


def test_release_opens_the_scientist_and_its_first_prompt_points_to_its_pack(project, rl, fake):
    released(project, rl)
    s = _open(project)["scientist-T-0001"]
    with project.read() as tx:
        assert s["cwd"] == tx.require("T-0001")["worktree"]
    [(pane, first)] = fake.prompts
    assert pane == s["herdr_pane"] and "You are scientist-T-0001" in first and "rl pack" in first


def test_an_idle_session_is_told_once_and_a_working_one_gets_it_from_its_stop_hook(project, rl, fake, monkeypatch):
    released(project, rl)
    pane = _open(project)["scientist-T-0001"]["herdr_pane"]
    fake.set(pane, "working")
    out = ok(rl("question", "--to", "scientist-T-0001", "--on", "T-0001", "--text", "which arm first?",
                who="coordinator"))
    assert "is working" in out and len(fake.prompts) == 1
    monkeypatch.setenv("SMOOTH_RESEARCH_SESSION", "scientist-T-0001")
    raw = json.dumps({"cwd": str(project.project.root), "stop_hook_active": False})
    got = hooks.stop("claude", raw)
    assert got["decision"] == "block" and "which arm first?" in got["reason"]
    assert hooks.stop("claude", json.dumps({"cwd": str(project.project.root), "stop_hook_active": True})) == {}
    fake.set(pane, "idle")
    assert sessions.wake(project, "scientist-T-0001") == "scientist-T-0001: told already"


def test_a_dialog_is_noted_and_the_coordinator_told(project, rl, fake):
    released(project, rl)
    pane = _open(project)["scientist-T-0001"]["herdr_pane"]
    fake.set(pane, "blocked")
    line = sessions.wake(project, "scientist-T-0001")
    assert "waits at a dialog" in line
    assert "session-needs-decision" in types(project, "coordinator")


def test_coordinator_watcher_stale_config_a_session_opens_on_the_models_configured_now(project, rl, fake):
    """coordinator-watcher-stale-config: every command reads the configuration; the next session opened takes the
    models configured then."""
    cfg = project.project.root / "smooth-research.yaml"
    text = cfg.read_text()
    assert "scientist:" in text
    import yaml
    data = yaml.safe_load(text)
    data["models"]["scientist"] = ["pi:test-model@high"]
    cfg.write_text(yaml.safe_dump(data))
    released(project, rl)
    assert _open(project)["scientist-T-0001"]["entry"] == "pi:test-model@high"


def test_the_coordinators_pane_is_left_alone_while_the_user_talks(project, rl, fake, monkeypatch):
    ok(rl("session", "open", "coordinator"))
    monkeypatch.setenv("SMOOTH_RESEARCH_SESSION", "coordinator")
    hooks.user_prompt("claude", json.dumps({"cwd": str(project.project.root), "prompt": "how is it going?"}))
    released(project, rl)  # the first release is news to nobody but the scientist
    ok(rl("request", "--stuck", "s", "--options", "o", "--recommend", "r", "--task", "T-0001",
          who="scientist-T-0001"))
    n = len(fake.prompts)
    line = sessions.wake(project, "coordinator")
    assert "the user is talking to the coordinator" in line and len(fake.prompts) == n
    added = hooks.user_prompt("claude", json.dumps({"cwd": str(project.project.root), "prompt": "and now?"}))
    assert "decision-request" in added["hookSpecificOutput"]["additionalContext"]


def test_without_herdr_the_command_succeeds_and_says_how_to_open_the_session(project, rl, monkeypatch):
    monkeypatch.setattr(adapters, "available", lambda entry: None)
    released(project, rl)
    out = ok(rl("task", "create", "--kind", "prep", "--title", "p", "--requirement", "r", "--done-check", "true",
                who="scientist-T-0001"))
    out = ok(rl("task", "dispatch", "T-0002", who="scientist-T-0001"))
    assert "could not notify researcher-T-0002" in out and "rl session open researcher-T-0002" in out
    printed = ok(rl("session", "open", "researcher-T-0002", "--print"))
    assert "SMOOTH_RESEARCH_SESSION=researcher-T-0002" in printed


# -- the observer ---------------------------------------------------------------------------------------------


def test_coordinator_watch_once_kills_watcher(project, rl):
    """coordinator-watch-once-kills-watcher: a beat that finds the lock taken is skipped; the observer goes on."""
    with watch.beat_lock(project) as mine:
        assert mine
        out = watch.run(project, once=True)
    assert "skipped" in out["summary"]
    assert watch.run(project, once=True)["summary"]  # the lock is free again: a beat runs


def test_the_observer_tells_untold_items_and_reminds_once(project, rl, fake, monkeypatch):
    released(project, rl)
    pane = _open(project)["scientist-T-0001"]["herdr_pane"]
    fake.set(pane, "working")
    ok(rl("question", "--to", "scientist-T-0001", "--on", "T-0001", "--text", "why?", who="coordinator"))
    fake.set(pane, "idle")
    n = len(fake.prompts)
    watch.beat(project)
    assert len(fake.prompts) == n + 1 and "why?" in fake.prompts[-1][1]
    monkeypatch.setattr(watch, "deadline_minutes", lambda config, t: 0)
    watch.beat(project)
    watch.beat(project)
    reminders = [p for p in fake.prompts[n + 1:] if "waiting a while" in p[1]]
    assert len(reminders) == 1


def test_the_observer_opens_and_closes_nothing(project, rl, fake):
    released(project, rl)
    before = set(_open(project))
    ok(rl("task", "cancel", "T-0001", "--reason", "x", who="coordinator"))
    fake.calls.clear()
    watch.beat(project)
    assert not [c for c in fake.calls if c[:2] in (("tab", "create"), ("tab", "close"), ("agent", "start"))]
    assert set(_open(project)) <= before


# -- small fixes ----------------------------------------------------------------------------------------------


def test_scientist_T_0003_brief_path_not_resolved(project, rl, monkeypatch):
    """scientist-T-0003-brief-path-not-resolved: a relative --brief is read in the worktree the command runs in."""
    released(project, rl)
    s = "scientist-T-0001"
    wt = gitops.checkout_in(project.project, "T-0001")
    (wt / "research" / "work" / "T-0001").mkdir(parents=True, exist_ok=True)
    (wt / "research" / "work" / "T-0001" / "brief.md").write_text("# Brief\nwhy\n")
    monkeypatch.chdir(wt)
    ok(rl("experiment", "add", "--title", "Sim", "--question", "q", "--hypotheses", "H-0001", "--brief",
          "research/work/T-0001/brief.md", who=s))


def test_a_binary_file_at_a_commit_is_read_without_failing(project):
    root = project.project.root
    (root / "blob.bin").write_bytes(bytes(range(256)))
    git(root, "add", "blob.bin")
    git(root, "commit", "-qm", "blob")
    text = gitops.show(project.project, "HEAD", "blob.bin")
    assert text is not None and len(text) == 256


def test_hooks_are_registered_for_both_hosts():
    from pathlib import Path
    for host in ("claude", "codex"):
        data = json.loads((Path(__file__).parent.parent / "hosts" / host / "hooks" / "hooks.json").read_text())
        assert {"SessionStart", "Stop", "UserPromptSubmit"} <= set(data["hooks"])
    assert hooks.stop("claude", "") == {}  # not a session rl opened: the turn ends as it would
