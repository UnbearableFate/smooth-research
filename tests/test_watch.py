"""The watcher over agent sessions (through the fake herdr CLI of test_agents, and Codex session logs made from the
real error turns in fixtures/codex)."""

import json
import os
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import SPEC, obs
from test_agents import calls, herdr_log  # noqa: F401 (a fixture)

from smooth_research import agents, messages, reviews, runs, tasks, watch
from smooth_research.delegate import rl_command

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
OVERLOADED = "researcher-overloaded.jsonl"  # a turn that ended with server_overloaded, then two settings events


def fake(log, change):
    """Change the fake herdr's state: an agent exits, a tab closes, a dialog opens."""
    path = log.parent / "state.json"
    state = json.loads(path.read_text())
    change(state)
    path.write_text(json.dumps(state))


def reminders(log, name):
    return [c[3] for c in calls(log) if c[:3] == ["agent", "prompt", name] and c[3].startswith("[smooth-research watch]")]


def researcher(project, n, targets=(), entry=None):
    task = tasks.create_task(project, f"task {n}", "implement", list(targets), to_role="researcher")["id"]
    agents.open_session(project, "researcher", f"researcher-{n}", entry_text=entry, task=task, worktree=False)
    tasks.claim(project, task, f"researcher-{n}")
    return task


def codex_log(herdr_log, name, *parts):
    """The Codex session log of an agent the fake herdr runs, holding these turns (fixture names or lines); the
    fake appends a turn to it when the agent takes a prompt."""
    sid = json.loads((herdr_log.parent / "state.json").read_text())["sessions"][name]
    path = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "11" / "02" / f"rollout-2026-11-02T18-23-23-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((FIXTURES / x).read_text() if x.endswith(".jsonl") else x for x in parts))
    fake(herdr_log, lambda st: st["logs"].update({name: str(path)}))
    return path


def event(minutes_ago, kind, **payload):
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return json.dumps({"timestamp": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "type": kind,
                       "payload": payload}) + "\n"


def memo(project, name):
    with project.read() as tx:
        return next(s for s in tx.all("Session", status="active") if s["role_id"] == name).get("watch") or {}


def test_the_watcher_starts_with_the_first_session_and_stops_with_the_last(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    first = agents.open_session(project, "reviewer")
    assert "the watcher runs in tab w1:t2: a beat every 5 min" in first["summary"] and "note:" not in first["summary"]
    command = f"{shlex.quote(rl_command())} agent watch --own-tab --every 5 --project {tmp_path}"
    assert [c for c in calls(herdr_log) if c[:2] == ["pane", "run"]] == [["pane", "run", "w1:p2", command]]
    second = agents.open_session(project, "reviewer")
    assert "the watcher runs" not in second["summary"]  # one watcher per project
    assert "watcher: tab w1:t2, a beat every 5" in agents.list_sessions(project)["summary"]
    assert "stopped" not in agents.close(project, "reviewer-1")["summary"]
    assert "the watcher in tab w1:t2 stopped" in agents.close(project, "reviewer-2")["summary"]

    project.project.config["agents"]["watch_minutes"] = 20  # a project made when the rounds were 20 minutes apart
    started = agents.open_session(project, "reviewer")
    assert "agents.watch_minutes is 20 in smooth-research.yaml: the watcher now beats every 5 min" in started["summary"]
    agents.close(project, "reviewer-1")
    project.project.config["agents"]["watch_minutes"] = 0
    assert "the watcher runs" not in agents.open_session(project, "reviewer")["summary"]
    assert len([c for c in calls(herdr_log) if c[:2] == ["pane", "run"]]) == 2


def test_an_agent_that_exited_is_started_again_in_its_folder(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    opened = agents.open_session(project, "researcher", "researcher-1", task="T-0001", worktree=False)
    fake(herdr_log, lambda st: st["agents"].pop("researcher-1"))  # it exited; its tab stays open
    out = watch.check(project)
    start = [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--pane") + 1] == "w1:p1" and "started again in" in out["summary"]
    told = reminders(herdr_log, "researcher-1")[-1]
    assert "started again (a new conversation)" in told and "agent brief researcher-1" in told
    assert "not from the user" in told
    notice = [c for c in calls(herdr_log) if c[:3] == ["agent", "prompt", "w1:p99"]][-1][3]
    assert out["note"] in notice and "researcher-1: its agent had exited" in notice

    fake(herdr_log, lambda st: (st["agents"].pop("researcher-1"), st["closed"].append("w1:t1")))  # tab closed too
    watch.check(project)
    create = [c for c in calls(herdr_log) if c[:2] == ["tab", "create"]][-1]
    assert create[create.index("--cwd") + 1] == str(tmp_path) and "SMOOTH_RESEARCH_ROLE=researcher-1" in create
    with project.read() as tx:
        s = tx.require(opened["id"])
        assert len([e for e in tx.all("Event") if e["type"] == "session.restarted"]) == 2
    assert (s["herdr_tab"], s["herdr_pane"]) == ("w1:t3", "w1:p3")  # w1:t2 is the watcher's

    with project.read() as tx:
        stale = tx.require(opened["id"])
    agents._end(project, opened["id"], "closed by the coordinator during the beat")
    assert watch._Beat(project).restart(stale) == ("researcher-1: closed", False)
    with project.write() as tx:
        tx.update(opened["id"], {"status": "active", "ended_at": None})

    for _ in range(2):  # the third restart within the hour is the last
        fake(herdr_log, lambda st: st["agents"].pop("researcher-1"))
        out = watch.check(project)
    assert "not started again" in out["summary"] and out["note"]
    assert not watch.check(project)["note"]  # the coordinator hears it once


def test_a_tab_that_herdr_gave_to_someone_else_is_left_alone(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    opened = agents.open_session(project, "reviewer")
    with project.write() as tx:  # herdr restarted: the recorded ids now name the coordinator's tab and pane
        tx.update(opened["id"], {"herdr_tab": "w1:t99", "herdr_pane": "w1:p99"})
    fake(herdr_log, lambda st: st["agents"].pop("reviewer-1"))
    watch.check(project)
    start = [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--pane") + 1] == "w1:p3"  # a new tab, not the coordinator's pane
    with project.write() as tx:
        tx.update(opened["id"], {"herdr_tab": "w1:t99"})
    assert "is now '1', not this session's: left open" in agents.close(project, "reviewer-1")["summary"]
    assert ["tab", "close", "w1:t99"] not in calls(herdr_log)


def test_an_idle_session_is_reminded_after_two_beats_and_a_reminder_counts_when_it_arrived(project, herdr_log,
                                                                                           tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1)
    first = watch.check(project)  # idle once: it may just have ended a turn
    assert "researcher-1: idle with T-0001 active; reminded at the next beat if it is still idle then" in first["summary"]
    assert not reminders(herdr_log, "researcher-1") and not first["note"]
    second = watch.check(project)
    assert "researcher-1: was idle with T-0001 active for two beats; reminded it to continue" in second["summary"]
    told = reminders(herdr_log, "researcher-1")
    assert len(told) == 1 and "task wait" in told[0] and "run record" in told[0]
    prompt = [c for c in calls(herdr_log) if c[:3] == ["agent", "prompt", "researcher-1"]][-1]
    assert prompt[4:] == ["--wait", "--until", "working", "--until", "blocked", "--timeout", "20000"]
    watch.check(project)  # it went on, and stopped again
    assert "reminded it to continue" in watch.check(project)["summary"]
    watch.check(project)
    stuck = watch.check(project)
    assert "did not continue after 2 reminder(s)" in stuck["summary"] and stuck["note"]
    assert not watch.check(project)["note"]
    assert len(reminders(herdr_log, "researcher-1")) == 2

    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    agents.open_session(project, "reviewer", ticket=rv["id"])
    fake(herdr_log, lambda st: st["deaf"].append("reviewer-1"))  # what is typed does not reach it
    watch.check(project)
    lost = watch.check(project)
    assert f"reviewer-1: was idle with {rv['id']} pending; a reminder was typed, but delivery not confirmed (herdr " \
           f"saw no working state after it); its screen ends:" in lost["summary"] and lost["note"]
    assert "reminders" not in memo(project, "reviewer-1")  # not counted
    held = watch.check(project)
    assert "reviewer-1: its reminder typed at" in held["summary"] and "nothing more is typed" in held["summary"]
    assert not held["note"] and len(reminders(herdr_log, "reviewer-1")) == 1  # not typed again, reported once
    fake(herdr_log, lambda st: (st["deaf"].remove("reviewer-1"), st["seq"].update({"reviewer-1": 9})))  # it moved
    watch.check(project)
    assert memo(project, "reviewer-1")["reminders"] == {"assignment": rv["id"], "count": 1}  # it arrived after all


def test_normal_pauses_are_not_woken(project, herdr_log, tmp_path, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    waiting = researcher(project, 1)
    attempt = runs.register(project, "E-0001", dict(SPEC), "r1")["attempt"]
    tasks.progress(project, waiting, "waiting", "researcher-1", next_action="analyze", attempts=[attempt])
    researcher(project, 2, ["E-0001"])  # its experiment's run is still running
    runs.record(project, obs(attempt, "running"))
    researcher(project, 3)
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-3")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    researcher(project, 4)
    agents.tell(project, "coordinator", "which seed set?", sender="researcher-4")

    first = watch.check(project)
    lines = first["summary"]
    assert "researcher-1: idle, T-0001 waiting" in lines
    assert f"researcher-2: idle, T-0002 active, waiting for runs {attempt}" in lines
    assert f"researcher-3: idle, T-0003 active, waiting for review {rv['id']}" in lines
    assert "researcher-4: idle, T-0004 active, waiting for a reply to" in lines
    assert f"{rv['id']} has no reviewer session" in lines and not first["news"]  # the coordinator gets a beat
    second = watch.check(project)
    assert second["news"] == [x.strip() for x in second["summary"].splitlines() if "has no reviewer session" in x]
    assert not any(reminders(herdr_log, f"researcher-{n}") for n in range(1, 5))
    assert not watch.check(project)["note"]  # and hears it once

    messages.inbox(project, "coordinator", ack=True)  # the coordinator read the question and gave no answer
    watch.check(project)
    assert "researcher-4: was idle with T-0004 active for two beats; reminded" in watch.check(project)["summary"]


def test_a_dialog_is_reported_once_and_a_trust_question_for_its_folder_is_answered(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    agents.open_session(project, "reviewer")
    fake(herdr_log, lambda st: st["dialog"].update({"reviewer-1": {"kind": "mcp", "sel": 0, "cwd": ""}}))
    first = watch.check(project)
    assert "reviewer-1 waits at a dialog (New MCP server found" in first["summary"] and first["note"]
    assert 'rl agent answer reviewer-1 "<option>"' in first["summary"]
    assert not watch.check(project)["note"]
    assert not [c for c in calls(herdr_log) if c[:2] == ["agent", "send-keys"]]
    fake(herdr_log, lambda st: st["dialog"].update({"reviewer-1": {"kind": "codex", "sel": 0, "cwd": str(tmp_path)}}))
    assert "reviewer-1: answered the trust question for its folder" in watch.check(project)["summary"]


def test_an_idle_coordinator_is_reminded_once_of_messages_unread_for_a_beat(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    agents.open_session(project, "reviewer")
    agents.tell(project, "coordinator", "anything for me?", sender="reviewer-1")
    assert not watch.check(project)["reminder"]  # it has not waited a beat yet
    assert "coordinator: idle with 1 message(s) unread for a beat; reminded" in watch.check(project)["summary"]
    assert not watch.check(project)["reminder"]
    assert "inbox --role coordinator" in reminders(herdr_log, "w1:p99")[-1]


def test_a_beat_with_nothing_new_writes_nothing(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    agents.open_session(project, "reviewer")
    watch.check(project)  # the first beat takes note of the coordinator's inbox
    with project.read() as tx:
        before = tx.meta()["data_version"]
    watch.check(project)
    watch.check(project)
    with project.read() as tx:
        assert tx.meta()["data_version"] == before  # an idle project keeps its data_version


def test_the_watcher_loop_beats_at_once_leaves_when_replaced_and_ends_when_no_session_is_open(project, herdr_log,
                                                                                              monkeypatch, rl,
                                                                                              capsys):
    tasks.start_session(project, "coordinator", "claude")
    opened = agents.open_session(project, "reviewer")
    code, out, _ = rl("agent", "check")
    assert code == 0 and "reviewer-1: idle, no assignment" in out
    slept = []
    monkeypatch.setattr(watch.time, "sleep", slept.append)
    with project.write() as tx:
        tab = tx.meta()["watch_tab"]
    monkeypatch.setenv("HERDR_TAB_ID", tab)
    gets = len([c for c in calls(herdr_log) if c[:3] == ["agent", "get", "reviewer-1"]])

    def replaced(seconds):  # another watcher takes over during the first sleep
        slept.append(seconds)
        with project.write() as tx:
            tx.set_meta(watch_tab="w1:t77")

    monkeypatch.setattr(watch.time, "sleep", replaced)
    assert "the watcher in tab w1:t77 took over" in watch.run(project, 1, own_tab=True)["summary"]
    assert slept == [60] and len([c for c in calls(herdr_log) if c[:3] == ["agent", "get", "reviewer-1"]]) == gets + 1
    assert calls(herdr_log)[-1] == ["tab", "close", tab]
    assert "watch beat" not in capsys.readouterr().out  # a beat that found nothing is not printed

    with project.write() as tx:  # a stale watcher is replaced; a tab that is not the watcher's is not closed
        tx.set_meta(watch_tab="w1:t99", watch_beat="2026-01-01T00:00:00+00:00")
    started = watch.start(project)
    assert started["started"] and ["tab", "close", "w1:t99"] not in calls(herdr_log)
    monkeypatch.setenv("HERDR_TAB_ID", started["tab"])
    agents._end(project, opened["id"], "ended elsewhere")
    assert watch.run(project, 1, own_tab=True)["summary"] == "no agent session is open; the watcher stopped"
    assert calls(herdr_log)[-1] == ["tab", "close", started["tab"]]
    with project.read() as tx:
        assert not tx.meta().get("watch_tab")


def test_a_session_recorded_while_a_beat_found_none_keeps_the_watcher(project, herdr_log, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    opened = agents.open_session(project, "reviewer")
    with project.read() as tx:
        tab = tx.meta()["watch_tab"]
    monkeypatch.setenv("HERDR_TAB_ID", tab)
    agents._end(project, opened["id"], "ended elsewhere")
    real = watch.check

    def opened_during_the_beat(store):  # rl agent open records a session after the beat read none
        out = real(store)
        with store.write() as tx:
            tx.update(opened["id"], {"status": "active"})
        return out

    def replaced(seconds):  # ends the loop at its next beat
        with project.write() as tx:
            tx.set_meta(watch_tab="w1:t77")
    monkeypatch.setattr(watch, "check", opened_during_the_beat)
    monkeypatch.setattr(watch.time, "sleep", replaced)
    assert "the watcher in tab w1:t77 took over" in watch.run(project, 1, own_tab=True)["summary"]


def test_a_beat_and_a_manual_check_never_act_together(project, herdr_log, rl):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1)
    watch.check(project)
    before = len(calls(herdr_log))
    with watch.beat_lock(project) as mine:  # the watcher's beat holds it
        assert mine
        out = watch.check(project)
        code, text, _ = rl("agent", "check")
    assert out["skipped"] and "watch beat skipped: another beat" in out["summary"]
    assert code == 0 and "skipped" in text and len(calls(herdr_log)) == before  # nothing looked at, nothing typed
    assert "reminded it to continue" in watch.check(project)["summary"]


# -- what the session's own log says -------------------------------------------------------------------------


def test_a_turn_cut_off_by_an_overloaded_model_is_retried_though_herdr_shows_it_working(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", OVERLOADED)  # the error, then settings events
    fake(herdr_log, lambda st: st["status"].update({"researcher-1": "working"}))  # herdr's view did not change
    out = watch.check(project)
    assert ("researcher-1: its last turn failed (server_overloaded: Selected model is at capacity. Please try a "
            "different model.); retried (1 of 3)") in out["summary"] and out["note"]
    told = reminders(herdr_log, "researcher-1")
    assert len(told) == 1 and "cut off by a model service error" in told[0]
    assert "agent brief researcher-1" in told[0] and "not from the user" in told[0]
    with project.read() as tx:
        s = next(x for x in tx.all("Session") if x["role_id"] == "researcher-1")
        assert [e["summary"] for e in tx.all("Event") if e["type"] == "session.retried"] == \
            ["researcher-1: retry 1 after server_overloaded"]
    assert s["host_session_id"] == "cx-researcher-1-1"
    # the retry's turn ended normally after some work: the streak is over, and herdr's working counts for nothing
    again = watch.check(project)
    assert "retr" not in again["summary"] and "researcher-1: idle with T-0001 active" in again["summary"]
    assert "retries" not in memo(project, "researcher-1")


def test_an_error_followed_by_a_normal_turn_is_not_retried(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", OVERLOADED, "researcher-continues.jsonl")
    out = watch.check(project)
    assert not reminders(herdr_log, "researcher-1") and "failed" not in out["summary"]


def test_a_new_host_session_leaves_the_old_log_behind(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", OVERLOADED)
    fake(herdr_log, lambda st: st["sessions"].update({"researcher-1": "cx-after-new"}))  # /new: another session
    out = watch.check(project)
    assert not reminders(herdr_log, "researcher-1") and "failed" not in out["summary"]
    with project.read() as tx:
        s = next(x for x in tx.all("Session") if x["role_id"] == "researcher-1")
        bound = [e["summary"] for e in tx.all("Event") if e["type"] == "session.bound"]
    assert s["host_session_id"] == "cx-after-new" and s["watch"]["log"] == {"id": "cx-after-new", "path": None}
    assert bound == ["researcher-1: host session cx-researcher-1-1 -> cx-after-new"]
    assert "log not found" in agents.list_sessions(project)["summary"]


def test_retries_back_off_and_the_coordinator_hears_once_when_they_are_used_up(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", OVERLOADED)
    fake(herdr_log, lambda st: (st["fail"].append("researcher-1"),  # every retry fails again
                                st["status"].update({"researcher-1": "working"})))

    def due_now():
        with project.write() as tx:
            s = next(x for x in tx.all("Session") if x["role_id"] == "researcher-1")
            retries = {**s["watch"]["retries"], "next_at": "2026-01-01T00:00:00+00:00"}
            tx.update(s["id"], {"watch": {**s["watch"], "retries": retries}})

    assert "retried (1 of 3)" in watch.check(project)["summary"]
    r = memo(project, "researcher-1")["retries"]
    gap = (datetime.fromisoformat(r["next_at"]) - datetime.fromisoformat(r["at"])).total_seconds() / 60
    assert gap == 10  # two beats of 5 minutes
    wait = watch.check(project)  # the retry failed too; herdr still shows working, which does not end the streak
    assert "retry 2 of 3 at" in wait["summary"] and len(reminders(herdr_log, "researcher-1")) == 1
    assert not wait["note"]
    due_now()
    assert "retried (2 of 3)" in watch.check(project)["summary"]
    r = memo(project, "researcher-1")["retries"]
    assert (datetime.fromisoformat(r["next_at"]) - datetime.fromisoformat(r["at"])).total_seconds() / 60 == 20
    due_now()
    assert "retried (3 of 3)" in watch.check(project)["summary"]
    out = watch.check(project)
    assert ("Please try a different model.) again after 3 retries; not retried again: continue its work on another "
            "model: rl agent switch researcher-1") in out["summary"] and out["note"]
    assert not watch.check(project)["note"] and len(reminders(herdr_log, "researcher-1")) == 3
    listed = agents.list_sessions(project)["summary"]
    assert "turn error" in listed and "retries 3 of 3" in listed and "next: report it: retries used up" in listed


def test_an_advisor_is_retried_and_only_reported_when_its_retries_are_used_up(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    agents.open_session(project, "advisor", "advisor-1", entry_text="codex:gpt-6-astra@max")
    codex_log(herdr_log, "advisor-1", "advisor-overloaded.jsonl", "advisor-overloaded-again.jsonl")  # two in a row
    fake(herdr_log, lambda st: st["fail"].append("advisor-1"))
    assert "advisor-1: its last turn failed (server_overloaded" in watch.check(project)["summary"]
    assert len(reminders(herdr_log, "advisor-1")) == 1
    with project.write() as tx:  # its retries are used up
        s = next(x for x in tx.all("Session") if x["role_id"] == "advisor-1")
        tx.update(s["id"], {"watch": {**s["watch"], "retries": {**s["watch"]["retries"], "count": 3}}})
    out = watch.check(project)
    assert "not retried again: look at it (herdr agent read advisor-1) and decide; an advisor keeps its model" \
        in out["summary"] and "switch" not in out["summary"] and out["note"]
    assert len(reminders(herdr_log, "advisor-1")) == 1


def test_a_stall_is_reported_once_with_the_screen_and_settings_do_not_count_as_progress(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    for n in (1, 2, 3):
        researcher(project, n, entry="codex:gpt-6-astra@high")
        fake(herdr_log, lambda st: st["status"].update({f"researcher-{n}": "working"}))
    codex_log(herdr_log, "researcher-1", event(45, "event_msg", type="task_started", turn_id="t1"),
              event(44, "response_item", type="reasoning"),
              event(40, "response_item", type="function_call", call_id="c1", name="exec"),
              event(40, "response_item", type="function_call_output", call_id="c1"),
              event(5, "event_msg", type="thread_settings_applied"),
              event(1, "response_item", type="message", role="user"))  # a prompt is not progress either
    codex_log(herdr_log, "researcher-2", event(200, "event_msg", type="task_started", turn_id="t2"),
              event(130, "response_item", type="function_call", call_id="c2", name="exec"))
    codex_log(herdr_log, "researcher-3", event(100, "event_msg", type="task_started", turn_id="t3"),
              event(90, "response_item", type="function_call", call_id="c3", name="exec"))

    def reads():  # of researcher-1's screen, from here on (opening it read whether Codex still starts)
        return len([c for c in calls(herdr_log) if c[:3] == ["agent", "read", "researcher-1"]]) - opened
    opened = 0
    opened = reads()
    out = watch.check(project)
    assert "researcher-1: its turn shows no progress for 40 min" in out["summary"] and out["note"]
    assert "with no tool call pending; nothing was typed; its screen ends: >" in out["summary"]
    assert ["agent", "read", "researcher-1", "--source", "visible"] in calls(herdr_log)
    assert "researcher-2: its tool call exec has been pending for 130 min" in out["summary"]
    assert "researcher-3: working" in out["summary"]  # a tool call may take a while
    assert not any(reminders(herdr_log, f"researcher-{n}") for n in (1, 2, 3))
    assert not watch.check(project)["note"]  # each is reported once
    assert reads() == 1


def test_a_session_on_a_low_host_is_not_retried(project, herdr_log):
    from test_usage import codex_left
    tasks.start_session(project, "coordinator", "claude")
    task = researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", OVERLOADED)
    codex_left(5)
    out = watch.check(project)
    assert "cut off" not in "".join(reminders(herdr_log, "researcher-1"))
    assert f"told to finish {task}; when it is idle: rl agent switch researcher-1" in out["summary"]


def test_a_used_up_subscription_is_reported_for_a_switch_not_retried(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", event(3, "event_msg", type="task_started", turn_id="t1"),
              event(2, "event_msg", type="task_complete", turn_id="t1",
                    error={"message": "You've hit your usage limit.", "codex_error_info": "usage_limit_exceeded"}))
    out = watch.check(project)
    assert ("researcher-1: its last turn failed because its subscription is used up (You've hit your usage limit.); "
            "continue its work on another model: rl agent switch researcher-1") in out["summary"] and out["note"]
    assert not reminders(herdr_log, "researcher-1")


def test_the_coordinators_own_turn_error_is_retried_after_a_full_idle_beat(project, herdr_log):
    tasks.start_session(project, "coordinator", "codex")
    agents.open_session(project, "reviewer")
    fake(herdr_log, lambda st: (st["sessions"].update({"w1:p99": "cx-coordinator"}), st["kinds"].update({"w1:p99": "codex"})))
    codex_log(herdr_log, "w1:p99", OVERLOADED)
    first = watch.check(project)
    assert "coordinator: its last turn failed (server_overloaded); retried at the next beat if it is still idle" \
        in first["summary"] and not reminders(herdr_log, "w1:p99")
    second = watch.check(project)
    assert "coordinator: its last turn failed (server_overloaded: Selected model is at capacity. Please try a " \
           "different model.); retried (1 of 3)" in second["summary"]
    told = reminders(herdr_log, "w1:p99")
    assert len(told) == 1 and "cut off by a model service error" in told[0] and "agent brief" not in told[0]


# -- delivery -------------------------------------------------------------------------------------------------


def test_a_prompt_that_herdr_saw_nothing_of_is_reported_not_delivered(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    tasks.create_task(project, "next", "implement", ["E-0001"], to_role="researcher")
    agents.open_session(project, "researcher", "researcher-1", task="T-0001", worktree=False)
    fake(herdr_log, lambda st: st["deaf"].append("researcher-1"))
    out = agents.assign(project, "researcher-1", task="T-0002")
    assert "researcher-1 assigned T-0002; told to read its brief, but delivery not confirmed (herdr saw no working " \
           "state after it); its screen ends: >" in out["summary"]
    fake(herdr_log, lambda st: st["dialog"].update({"researcher-1": {"kind": "mcp", "sel": 0, "cwd": ""}}))
    out = agents.assign(project, "researcher-1", task="T-0001")
    assert "not told (researcher-1 waits at a dialog" in out["summary"]


def test_a_turn_too_short_for_herdr_counts_when_the_log_shows_it_started(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1, entry="codex:gpt-6-astra@high")
    codex_log(herdr_log, "researcher-1", "researcher-continues.jsonl")
    fake(herdr_log, lambda st: st["quick"].append("researcher-1"))
    watch.check(project)
    out = watch.check(project)  # herdr times out on it, but the log shows the turn the reminder started
    assert "reminded it to continue" in out["summary"] and "not confirmed" not in out["summary"]
    assert memo(project, "researcher-1")["reminders"] == {"assignment": "T-0001", "count": 1}
    watch.check(project)
    assert len(reminders(herdr_log, "researcher-1")) == 1  # not typed again: it arrived


def test_a_first_turn_counts_as_delivered_when_its_new_log_shows_it(project, herdr_log, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    monkeypatch.setenv("FAKE_HERDR_NO_CODEX_ID", "1")  # the id comes from /status
    agents.open_session(project, "researcher", "researcher-1", entry_text="codex:m", worktree=False)
    with project.read() as tx:
        sid = next(s for s in tx.all("Session") if s["role_id"] == "researcher-1")["host_session_id"]
    path = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "11" / "05" / f"rollout-2026-11-05T17-17-53-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)  # Codex writes it when the first turn starts
    fake(herdr_log, lambda st: (st["quick"].append("researcher-1"), st["logs"].update({"researcher-1": str(path)})))
    task = tasks.create_task(project, "task 1", "implement", [], to_role="researcher")["id"]
    out = agents.assign(project, "researcher-1", task=task)  # herdr sees no working state: the log shows the turn
    assert out["summary"].endswith("; told to read its brief") and path.is_file()


def test_slash_commands_are_sent_as_before_and_a_dialog_after_a_prompt_counts_as_taken(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1)
    agents.reset(project, "researcher-1", "clear")
    assert [c for c in calls(herdr_log) if c[:2] == ["agent", "prompt"]][-1] == ["agent", "prompt", "researcher-1", "/clear"]
    agents.reset(project, "researcher-1", "compact")
    assert calls(herdr_log)[-1] == ["agent", "prompt", "researcher-1", "/compact", "--wait", "--timeout", "600000"]

    tasks.create_task(project, "again", "implement", ["E-0001"], to_role="researcher")
    fake(herdr_log, lambda st: st["asks"].update({"researcher-1": "approval"}))  # the prompt leads to a permission ask
    assert "; told to read its brief" in agents.assign(project, "researcher-1", task="T-0002")["summary"]
    out = watch.check(project)
    assert "researcher-1 waits at a dialog (Bash command" in out["summary"] and out["note"]


def test_the_agent_list_shows_classes_and_usage_without_sessions(project, herdr_log, rl):
    from test_usage import codex_left
    code, out, _ = rl("agent", "list")
    assert code == 0 and out.startswith("no agent sessions open\nusage:\n  claude: no reading yet")
    codex_left(40)
    assert "  codex: week 40% left (resets " in rl("agent", "list")[1]
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1)
    listed = agents.list_sessions(project)["summary"]
    assert "researcher-1 [researcher, idle] claude:claude-opus-5-5@high" in listed
    assert "\n  idle, unfinished: T-0001 active; herdr only; next: remind it after two beats in a row" in listed
    with project.read() as tx:
        before = tx.meta()["data_version"]
    agents.list_sessions(project)
    with project.read() as tx:
        assert tx.meta()["data_version"] == before  # it only looks
