"""The watcher over agent sessions (through the fake herdr CLI of test_agents)."""

import json
import shlex

from conftest import SPEC, obs
from test_agents import calls, herdr_log  # noqa: F401 (a fixture)

from smooth_research import agents, messages, reviews, runs, tasks, watch
from smooth_research.delegate import rl_command


def fake(log, change):
    """Change the fake herdr's state: an agent exits, a tab closes, a dialog opens."""
    path = log.parent / "state.json"
    state = json.loads(path.read_text())
    change(state)
    path.write_text(json.dumps(state))


def reminders(log, name):
    return [c[3] for c in calls(log) if c[:3] == ["agent", "prompt", name] and c[3].startswith("[smooth-research watch]")]


def researcher(project, n, targets=()):
    task = tasks.create_task(project, f"task {n}", "implement", list(targets), to_role="researcher")["id"]
    agents.open_session(project, "researcher", f"researcher-{n}", task=task, worktree=False)
    tasks.claim(project, task, f"researcher-{n}")
    return task


def test_the_watcher_starts_with_the_first_session_and_stops_with_the_last(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    first = agents.open_session(project, "reviewer")
    assert "the watcher runs in tab w1:t2: a round every 20 min" in first["summary"]
    command = f"{shlex.quote(rl_command())} agent watch --own-tab --every 20 --project {tmp_path}"
    assert [c for c in calls(herdr_log) if c[:2] == ["pane", "run"]] == [["pane", "run", "w1:p2", command]]
    second = agents.open_session(project, "reviewer")
    assert "the watcher runs" not in second["summary"]  # one watcher per project
    assert "watcher: tab w1:t2, a round every 20" in agents.list_sessions(project)["summary"]
    assert "stopped" not in agents.close(project, "reviewer-1")["summary"]
    assert "the watcher in tab w1:t2 stopped" in agents.close(project, "reviewer-2")["summary"]

    project.project.config["agents"]["watch_minutes"] = 0
    assert "the watcher runs" not in agents.open_session(project, "reviewer")["summary"]
    assert len([c for c in calls(herdr_log) if c[:2] == ["pane", "run"]]) == 1


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
    agents._end(project, opened["id"], "closed by the coordinator during the round")
    assert watch._restart(project, stale) == ("researcher-1: closed", False)
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


def test_an_idle_session_with_unfinished_work_is_reminded_then_reported(project, herdr_log, tmp_path, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    researcher(project, 1)
    first = watch.check(project)
    assert "researcher-1: was idle with T-0001 active; reminded it to continue" in first["summary"]
    assert "task wait" in reminders(herdr_log, "researcher-1")[-1] and "run record" in reminders(herdr_log, "researcher-1")[-1]
    assert "reminded it to continue" in watch.check(project)["summary"]  # it went on, then stopped again
    third = watch.check(project)
    assert "did not continue after 2 reminder(s)" in third["summary"] and third["note"]
    assert not watch.check(project)["note"]
    assert len(reminders(herdr_log, "researcher-1")) == 2

    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    agents.open_session(project, "reviewer", ticket=rv["id"])
    fake(herdr_log, lambda st: st["deaf"].append("reviewer-1"))  # it does not react to what it is told
    assert f"reviewer-1: was idle with {rv['id']} pending; reminded" in watch.check(project)["summary"]
    assert "reviewer-1: idle with RV-0001 pending; it did not continue after 1 reminder(s)" in \
        watch.check(project)["summary"]


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
    assert f"{rv['id']} has no reviewer session" in lines and not first["news"]  # the coordinator gets a round
    second = watch.check(project)
    assert second["news"] == [x.strip() for x in second["summary"].splitlines() if "has no reviewer session" in x]
    assert not any(reminders(herdr_log, f"researcher-{n}") for n in range(1, 5))
    assert not watch.check(project)["note"]  # and hears it once

    messages.inbox(project, "coordinator", ack=True)  # the coordinator read the question and gave no answer
    assert "researcher-4: was idle with T-0004 active; reminded" in watch.check(project)["summary"]


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


def test_an_idle_coordinator_is_reminded_once_of_messages_unread_for_a_round(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    agents.open_session(project, "reviewer")
    agents.tell(project, "coordinator", "anything for me?", sender="reviewer-1")
    assert not watch.check(project)["reminder"]  # it has not waited a round yet
    assert "coordinator: idle with 1 message(s) unread for a round; reminded" in watch.check(project)["summary"]
    assert not watch.check(project)["reminder"]
    assert "inbox --role coordinator" in reminders(herdr_log, "w1:p99")[-1]


def test_a_round_with_nothing_new_writes_nothing(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    agents.open_session(project, "reviewer")
    watch.check(project)  # the first round takes note of the coordinator's inbox
    with project.read() as tx:
        before = tx.meta()["data_version"]
    watch.check(project)
    watch.check(project)
    with project.read() as tx:
        assert tx.meta()["data_version"] == before  # an idle project keeps its data_version


def test_the_watcher_loop_leaves_when_replaced_and_ends_when_no_session_is_open(project, herdr_log, monkeypatch, rl):
    tasks.start_session(project, "coordinator", "claude")
    opened = agents.open_session(project, "reviewer")
    code, out, _ = rl("agent", "check")
    assert code == 0 and "reviewer-1: idle, no assignment" in out
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    with project.write() as tx:
        tab = tx.meta()["watch_tab"]
        tx.set_meta(watch_tab="w1:t77")
    monkeypatch.setenv("HERDR_TAB_ID", tab)
    assert "the watcher in tab w1:t77 took over" in watch.run(project, 1, own_tab=True)["summary"]
    assert calls(herdr_log)[-1] == ["tab", "close", tab]

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
