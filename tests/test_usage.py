"""Subscription usage (usage.py): readings, low hosts, where new work goes, the watcher's notices and the switch
to another model (through the fake herdr of test_agents and the fake CLIs of test_reviews)."""

import io
import itertools
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_agents import PASS, PROPOSALS, calls, commit_all, herdr_log  # noqa: F401 (a fixture)
from test_reviews import fakes  # noqa: F401 (a fixture)
from test_watch import fake, reminders

from smooth_research import agents, delegate, reviews, tasks, usage, watch
from smooth_research.project import DEFAULT_CONFIG, config_warnings
from smooth_research.util import RLError

LOGS = itertools.count(1)


def stamp(seconds=0.0):
    at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def codex_left(percent, minutes=10080, resets_in=3600, day="31", age_minutes=0.0):
    """Codex logs a reading with this percent left of one window, in a session log written after the others (and
    taken after them, unless it is age_minutes old)."""
    n = next(LOGS)
    folder = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / day
    folder.mkdir(parents=True, exist_ok=True)
    window = {"used_percent": 100 - percent, "window_minutes": minutes, "resets_at": int(time.time() + resets_in)}
    event = {"timestamp": stamp(n / 1000 - age_minutes * 60), "type": "event_msg",
             "payload": {"type": "token_count", "info": None, "rate_limits": {"limit_id": "codex", "primary": window,
                                                                              "secondary": None}}}
    path = folder / f"rollout-2026-10-{day}T00-00-{n:02d}-x.jsonl"
    path.write_text(json.dumps(event) + "\n")
    os.utime(path, (time.time() + n, time.time() + n))
    return path


def claude_left(percent, window="seven_day", resets_in=3600):
    """A Claude Code session's status line shows this percent left of one window."""
    usage.statusline(json.dumps({"rate_limits": {window: {"used_percentage": 100 - percent,
                                                          "resets_at": int(time.time() + resets_in)}}}))


def test_codex_usage_is_the_newest_reading_by_the_time_it_was_taken():
    first = codex_left(50, day="30")
    folder = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "29"  # a session across midnight
    folder.mkdir(parents=True)
    limits = {"primary": {"used_percent": 70.0, "window_minutes": 300, "resets_at": 1793811568},
              "secondary": {"used_percent": 20.0, "window_minutes": 10080, "resets_at": 1794000000}}
    lines = [{"timestamp": stamp(-60), "type": "event_msg",
              "payload": {"type": "token_count", "info": None, "rate_limits": limits}},
             {"timestamp": stamp(-50), "type": "event_msg",
              "payload": {"type": "token_count", "info": None, "rate_limits": None}},
             {"timestamp": stamp(-40), "type": "response_item", "payload": {"type": "message"}}]
    log = folder / "rollout-2026-10-29T23-59-00-y.jsonl"
    log.write_text("".join(json.dumps(x) + "\n" for x in lines))
    os.utime(log, (time.time() + 1000, time.time() + 1000))  # written last, but its reading is older
    found = usage.reading("codex")
    assert (found["source"], found["windows"][0]["used"]) == (str(first), 50.0)
    lines[0]["timestamp"] = stamp(60)  # now its reading is the newer one
    log.write_text("".join(json.dumps(x) + "\n" for x in lines))
    found = usage.reading("codex")
    assert found["windows"] == [{"name": "5h", "used": 70.0, "resets_at": 1793811568},
                                {"name": "week", "used": 20.0, "resets_at": 1794000000}]
    assert (found["source"], found["at"]) == (str(log), lines[0]["timestamp"])
    assert usage.reading("pi") is None


def test_claude_usage_comes_from_rls_status_line_which_never_fails(rl, monkeypatch):
    later = int(time.time() + 3600)
    given = {"model": {"display_name": "Opus 5.5"},
             "rate_limits": {"five_hour": {"used_percentage": 34, "resets_at": later},
                             "seven_day": {"used_percentage": 61, "resets_at": later}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(given)))
    assert rl("usage", "statusline")[:2] == (0, "Opus 5.5 · 5h 66% left · week 39% left\n")
    assert [(w["name"], w["used"]) for w in usage.reading("claude")["windows"]] == [("5h", 34.0), ("week", 61.0)]
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert rl("usage", "statusline")[:2] == (0, "\n")
    assert [w["used"] for w in usage.reading("claude")["windows"]] == [34.0, 61.0]  # the reading is kept


def test_a_host_is_low_at_its_threshold_and_earlier_on_the_coordinators_host(project, rl):
    codex_left(15)
    claude_left(15)
    assert usage.low_hosts(project) == {}
    tasks.start_session(project, "coordinator", "claude")  # it keeps working on its own host
    low = usage.low_hosts(project)
    assert list(low) == ["claude"] and low["claude"]["reason"] == (
        "the claude subscription has 15% of its week window left (its sessions hand over at 20%, the "
        "coordinator's host)")
    code, out, _ = rl("agent", "list")
    assert code == 0 and "  claude: week 15% left (resets " in out
    assert "sessions hand over at 20% left (the coordinator's host); LOW: its researchers and reviewers hand over" in out
    assert "  codex: week 15% left" in out and "sessions hand over at 10% left; not low\n" in out
    assert "  pi: reports no windows" in out
    code, out, err = rl("usage")
    assert code != 0 and "rl agent list" in err
    codex_left(9)
    assert set(usage.low_hosts(project)) == {"claude", "codex"}
    codex_left(5, resets_in=-60)  # that window has reset since
    claude_left(15, window="five_hour", resets_in=-60)
    assert set(usage.low_hosts(project)) == set()
    assert "  codex: week reset " in rl("agent", "list")[1] and ", no reading since; read 0 min ago" in rl("agent", "list")[1]
    codex_left(1, age_minutes=10)  # a log written later, with a reading older than the last one
    assert set(usage.low_hosts(project)) == set()
    project.project.config["usage"] = {"switch_left_percent": 50, "coordinator_host_left_percent": 5}
    codex_left(40, minutes=300, age_minutes=-2)
    assert usage.low_hosts(project)["codex"]["window"] == "5h"
    warned = config_warnings({**DEFAULT_CONFIG, "usage": {"switch_left_percent": "ten"}})
    assert warned == ["smooth-research.yaml: usage.switch_left_percent must be a percentage from 0 to 100, not "
                      "'ten'; using 10"]


def test_a_stale_reading_is_shown_but_does_not_make_a_host_low(project, rl):
    codex_left(5, age_minutes=20)  # Codex has not run a turn here for 20 minutes
    assert usage.reading("codex")["windows"][0]["used"] == 95 and usage.low_hosts(project) == {}
    out = rl("agent", "list")[1]
    assert "  codex: week 5% left (resets " in out and "; read 20 min ago; sessions hand over at 10% left; stale: not " \
                                                      "counted as low until a new reading" in out
    codex_left(5)
    assert set(usage.low_hosts(project)) == {"codex"}


def test_new_researchers_and_reviewers_avoid_a_low_host_but_advisors_do_not(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    codex_left(5)
    assert agents.open_session(project, "reviewer")["entry"] == "claude:claude-opus-5-5@medium"
    assert agents.open_session(project, "advisor")["entry"] == "codex:gpt-6-astra@max"  # the rest is theirs
    named = agents.open_session(project, "researcher", entry_text="codex:gpt-6-astra@high", worktree=False)
    assert named["entry"] == "codex:gpt-6-astra@high"  # an entry the coordinator names is used as it is
    claude_left(15)
    with pytest.raises(RLError) as err:
        agents.open_session(project, "researcher", worktree=False)
    assert err.value.message == (
        "no researcher entry can run a session here: claude:claude-opus-5-5@high: the claude subscription has 15% "
        "of its week window left (its sessions hand over at 20%, the coordinator's host); codex:gpt-6-astra@high: "
        "the codex subscription has 5% of its week window left (its sessions hand over at 10%); name one with "
        "--entry to use it anyway")


def test_headless_reviews_and_delegation_skip_a_low_host_but_advice_does_not(project, fakes, tmp_path, monkeypatch):
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    codex_left(5)
    monkeypatch.setenv("FAKE_ANSWER", PASS)
    assert "claude-opus-5-5" in reviews.run_ticket(project, rv["id"])["summary"]
    with project.read() as tx:
        skipped = tx.require(rv["id"])["calls"][0]
    assert skipped["entry"] == "codex:gpt-6-astra@medium" and not skipped["ok"]
    assert skipped["error"] == ("not called: the codex subscription has 5% of its week window left (its sessions "
                                "hand over at 10%)")
    advice = reviews.prepare_advice(project, "why no gain?", "H-0001")
    monkeypatch.setenv("FAKE_ANSWER", PROPOSALS)
    reviews.run_ticket(project, advice["ids"][0])
    with project.read() as tx:
        assert tx.require(advice["ids"][0])["observed_model"] == "gpt-6-astra"  # advisors keep the rest
    codex_left(50)
    claude_left(5)
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    out = delegate.delegate(project, "T-0001")
    assert out["entry"] == "codex:gpt-6-astra@high" and "not used: claude:claude-opus-5-5@high: the claude " \
                                                        "subscription has 5% of its week window left" in out["summary"]


def test_sessions_on_a_low_host_are_told_to_finish_and_the_coordinator_to_switch_them(project, herdr_log):
    tasks.start_session(project, "coordinator", "codex")
    task = tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")["id"]
    agents.open_session(project, "researcher", "researcher-1", task=task, worktree=False)
    tasks.claim(project, task, "researcher-1")
    agents.open_session(project, "advisor", "advisor-1", entry_text="claude:claude-fable-5-1@high")
    agents.open_session(project, "reviewer", "reviewer-1")  # on codex, which is not low
    claude_left(8)
    out = watch.check(project)
    told = reminders(herdr_log, "researcher-1")
    assert len(told) == 1 and "the claude subscription has 8% of its week window left" in told[0]
    assert f"`rl task handoff {task} --role researcher-1 --doc <note>`. Then end your turn." in told[0]
    assert not reminders(herdr_log, "advisor-1") and not reminders(herdr_log, "reviewer-1")
    with project.read() as tx:
        note = tx.require(out["note"])["text"]
    assert note == ("watch beat: researcher-1 (claude:claude-opus-5-5@high): the claude subscription has 8% of its "
                    f"week window left (its sessions hand over at 10%); told to finish {task}; when it is idle: rl "
                    "agent switch researcher-1")
    again = watch.check(project)  # idle with its task unfinished, but not reminded to go on with it
    assert (again["note"], len(reminders(herdr_log, "researcher-1"))) == (None, 1)
    assert f"told to finish {task}; when it is idle: rl agent switch researcher-1" in again["summary"]
    watch.check(project)
    assert len(reminders(herdr_log, "researcher-1")) == 1
    starts = len([c for c in calls(herdr_log) if c[:2] == ["agent", "start"]])
    fake(herdr_log, lambda st: st["agents"].pop("researcher-1"))
    gone = watch.check(project)
    assert len([c for c in calls(herdr_log) if c[:2] == ["agent", "start"]]) == starts  # not on the low host
    assert "researcher-1: its agent exited, and the claude subscription" in gone["summary"] and gone["note"]
    assert "not started again there; continue its work on another model: rl agent switch researcher-1" \
        in gone["summary"]


def test_a_session_at_a_dialog_is_told_to_finish_once_the_dialog_is_gone(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")["id"]
    agents.open_session(project, "reviewer", "reviewer-1", ticket=rv)  # on codex
    fake(herdr_log, lambda st: st["dialog"].update({"reviewer-1": {"kind": "approval", "sel": 0, "cwd": ""}}))
    codex_left(5)
    first = watch.check(project)
    assert "reviewer-1 waits at a dialog (Bash command" in first["summary"] and first["note"]
    assert not reminders(herdr_log, "reviewer-1")  # nothing is typed at a dialog
    fake(herdr_log, lambda st: st["dialog"].clear())
    second = watch.check(project)
    told = reminders(herdr_log, "reviewer-1")
    assert second["note"] and len(told) == 1 and f"Stop now without answering {rv}" in told[-1]
    watch.check(project)
    assert len(reminders(herdr_log, "reviewer-1")) == 1  # delivered once


def test_switch_continues_a_researchers_task_on_the_next_model_in_its_worktree(project, herdr_log, tmp_path,
                                                                             monkeypatch):
    tasks.start_session(project, "coordinator", "codex")
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    commit_all(tmp_path)
    first = agents.open_session(project, "researcher", "researcher-1", task="T-0001")
    assert first["entry"] == "claude:claude-opus-5-5@high"
    tasks.claim(project, "T-0001", "researcher-1")
    claude_left(8)
    fake(herdr_log, lambda st: st["status"].update({"researcher-1": "working"}))
    with pytest.raises(RLError, match="still working"):
        agents.switch(project, "researcher-1")
    note = tmp_path / "research/notes/handoff.md"
    note.write_text("done: the operator; next: the smoke run\n")
    tasks.handoff(project, "T-0001", str(note), "researcher-1")
    fake(herdr_log, lambda st: st["status"].pop("researcher-1"))
    out = agents.switch(project, "researcher-1")
    assert (out["name"], out["entry"]) == ("researcher-1", "codex:gpt-6-astra@high")
    assert (out["worktree"], out["branch"]) == (first["worktree"], "sr/researcher-1/T-0001")
    assert out["summary"].startswith("researcher-1: claude:claude-opus-5-5@high -> codex:gpt-6-astra@high (the "
                                     "claude subscription has 8% of its week window left")
    with project.read() as tx:
        old, new = tx.require(first["id"]), tx.require(out["id"])
    assert old["status"] == "ended" and (new["role_id"], new["assignment"]) == ("researcher-1", "T-0001")
    start = [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--") + 1:][:2] == ["-m", "gpt-6-astra"]
    brief = agents.brief(project, "researcher-1")["summary"]
    assert "Claim the task" in brief and "done: the operator; next: the smoke run" in brief  # the handoff

    agents.open_session(project, "advisor", "advisor-1", entry_text="claude:claude-fable-5-1@high")
    with pytest.raises(RLError, match="advisors keep their model"):
        agents.switch(project, "advisor-1")
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    with pytest.raises(RLError, match="the coordinator's decision"):
        agents.switch(project, "researcher-1")


def test_switch_gives_an_open_review_to_the_next_reviewer_of_its_ticket(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    first = agents.open_session(project, "reviewer", "reviewer-1", ticket=rv["id"])
    assert first["entry"] == "codex:gpt-6-astra@medium"
    codex_left(5)
    out = agents.switch(project, "reviewer-1")
    assert out["entry"] == "claude:claude-opus-5-5@medium"
    with project.read() as tx:
        assert tx.require(rv["id"])["assigned_session"] == out["id"]
        assert tx.require(out["id"])["assignment"] == rv["id"]
    (tmp_path / "answer.md").write_text(PASS)
    reviews.answer_live(project, rv["id"], str(tmp_path / "answer.md"), "reviewer-1")
    codex_left(50)
    claude_left(5)
    third = agents.switch(project, "reviewer-1")  # its review is answered: it waits for the re-check
    assert third["entry"] == "codex:gpt-6-astra@medium" and "assigned" not in third["summary"]
    recheck = reviews.request_review(project, "E-0001", ["src/model.py"], "fixed?", ["claude-opus-5-5"], scope="op",
                                     recheck_of=rv["id"])
    assert f"reviewer-1 assigned {recheck['id']}" in recheck["summary"]  # the session that replaced its reviewer


def test_a_claude_coordinator_reports_usage_through_the_projects_local_settings(project, rl, own_homes):
    start = ("session", "start", "--role", "coordinator", "--host", "claude")
    out = rl(*start)[1]
    local = project.project.root / ".claude" / "settings.local.json"
    ours = {"type": "command", "command": "rl usage statusline"}
    assert json.loads(local.read_text()) == {"statusLine": ours} and "reports Claude's usage" in out
    assert (local.parent / ".gitignore").read_text() == "settings.local.json\n"
    assert "Claude usage" not in rl(*start)[1]  # already in place
    local.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    user = own_homes / "claude_config_dir" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line"}}))
    assert "is the user's own" in rl(*start)[1]
    assert json.loads(local.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}}  # left as it was
    user.unlink()
    rl(*start)
    assert json.loads(local.read_text()) == {"permissions": {"allow": ["Bash(ls)"]}, "statusLine": ours}
    local.unlink()
    assert "Claude usage" not in rl("session", "start", "--role", "coordinator", "--host", "codex")[1]
    assert not local.exists()
