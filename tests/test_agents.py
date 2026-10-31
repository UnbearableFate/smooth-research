"""Agent sessions in herdr tabs (through a fake herdr CLI) and researcher worktrees (with real git)."""

import json
import os
import stat
import subprocess

import pytest

from smooth_research import agents, hooks, reviews, tasks
from smooth_research.project import find_project
from smooth_research.store import Store
from smooth_research.util import RLError

FAKE_HERDR = r"""#!{python}
import json, os, sys, pathlib
args = sys.argv[1:]
state_path = pathlib.Path(os.environ["FAKE_HERDR_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"n": 0, "agents": {}}
with open(os.environ["FAKE_HERDR_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")

def done(result=None, error=None):
    state_path.write_text(json.dumps(state))
    if error:
        print(json.dumps({"error": {"code": error[0], "message": error[1]}}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"id": "cli", "result": result}))
    sys.exit(0)

group, action = args[0], args[1]
if (group, action) == ("tab", "create"):
    state["n"] += 1
    done({"tab": {"tab_id": f"w1:t{state['n']}"}, "root_pane": {"pane_id": f"w1:p{state['n']}"}})
if (group, action) == ("tab", "close"):
    state["agents"] = {k: v for k, v in state["agents"].items() if v != args[2]}
    done({"type": "ok"})
if (group, action) == ("agent", "start"):
    state["agents"][args[2]] = "w1:t" + args[args.index("--pane") + 1].split(":p")[1]
    if os.environ.get("FAKE_HERDR_BLOCKED"):
        done(error=("agent_not_ready", f"agent {args[2]} is blocked during startup"))
    done({"agent": {"name": args[2], "agent_status": "idle"}})
if (group, action) in (("agent", "prompt"), ("agent", "get")):
    if args[2] not in state["agents"] and not args[2].startswith("w1:p"):
        done(error=("agent_not_found", f"agent target {args[2]} not found"))
    if action == "prompt" and "--wait" in args and args[3].startswith("/"):
        done(error=("agent_prompt_stalled", "no working state observed"))
    done({"agent": {"agent_status": "idle"}})
done(error=("unknown", " ".join(args)))
"""

MEDIUM = ('Problems.\n```json\n{"verdict": "issues", "findings": [{"location": "src/model.py:2", "evidence": "e", '
          '"impact": "result_error", "severity": "medium", "fix": "f", "verify": "v"}], "checked": ["all"], '
          '"unchecked": []}\n```')
PASS = 'Fine.\n```json\n{"verdict": "pass", "findings": [], "checked": ["all"], "unchecked": []}\n```'
PROPOSALS = 'Advice.\n```json\n{"proposals": [{"title": "x"}], "recommendation": "continue"}\n```'
STOP = 'Advice.\n```json\n{"proposals": [], "recommendation": "stop", "bottleneck": "no signal"}\n```'


@pytest.fixture
def herdr_log(tmp_path_factory, monkeypatch):
    """A fake herdr and stub agent CLIs on PATH, this session inside a herdr pane, git identity set."""
    bindir = tmp_path_factory.mktemp("bin")
    for name, body in (("herdr", FAKE_HERDR.replace("{python}", os.sys.executable)),
                       ("claude", "#!/bin/sh\nexit 0\n"), ("codex", "#!/bin/sh\nexit 0\n"), ("pi", "#!/bin/sh\nexit 0\n")):
        (bindir / name).write_text(body)
        (bindir / name).chmod((bindir / name).stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    for key, value in (("HERDR_ENV", "1"), ("HERDR_PANE_ID", "w1:p99"), ("HERDR_TAB_ID", "w1:t99"),
                       ("FAKE_HERDR_STATE", str(bindir / "state.json")), ("FAKE_HERDR_LOG", str(bindir / "log.jsonl")),
                       ("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.org"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(key, value)
    for key in ("SMOOTH_RESEARCH_ROLE", "SMOOTH_RESEARCH_SESSION", "SMOOTH_RESEARCH_CHECKOUT", "FAKE_HERDR_BLOCKED"):
        monkeypatch.delenv(key, raising=False)
    return bindir / "log.jsonl"


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_all(root):
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")


def test_a_researcher_works_in_its_own_worktree_with_a_branch_per_task(project, herdr_log, tmp_path, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")  # inside herdr pane w1:p99
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    tasks.create_task(project, "next", "implement", ["E-0001"], to_role="researcher")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    commit_all(tmp_path)
    out = agents.open_session(project, "researcher", "researcher-1", task="T-0001")
    wt = tmp_path.parent / f"{tmp_path.name}-worktrees" / "researcher-1"
    assert out["worktree"] == str(wt) and git(wt, "branch", "--show-current") == "sr/researcher-1/T-0001"
    log = calls(herdr_log)
    tab = next(c for c in log if c[:2] == ["tab", "create"])
    assert tab[tab.index("--cwd") + 1] == str(wt) and f"SMOOTH_RESEARCH_PROJECT={tmp_path}" in tab
    start = next(c for c in log if c[:2] == ["agent", "start"])
    args = start[start.index("--") + 1:]
    assert args[:2] == ["--model", "claude-opus-5-5"] and args[args.index("--effort") + 1] == "high"
    assert args[-2:] == ["--permission-mode", "auto"]  # agents.args.claude; no other permission flag is added
    prompt = next(c for c in log if c[:2] == ["agent", "prompt"])
    assert prompt[2] == "researcher-1" and "agent brief researcher-1" in prompt[3]

    # the session's own view: records in the project, code in its worktree
    monkeypatch.chdir(wt)
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    store = Store(find_project(), "test")
    assert (store.project.root, store.project.checkout) == (tmp_path, wt)
    text = agents.brief(store)["summary"]
    assert "git worktree of the project on branch sr/researcher-1/T-0001" in text and "Subagents:" in text
    assert "Work pack for T-0001" in text and str(tmp_path / "research" / "artifacts") in text
    tasks.claim(store, "T-0001", "researcher-1")
    (wt / "src/model.py").write_text("def f(x):\n    return 2 * x\n")
    rv = reviews.request_review(store, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="new op")
    assert "the coordinator is asked to assign a reviewer" in rv["summary"] and "notice sent to w1:p99" in rv["summary"]
    with store.read() as tx:
        package = store.project.abs(tx.require(tx.require(rv["id"])["request_ref"])["object_path"]).read_text()
    assert "return 2 * x" in package  # the worktree's code, not the main checkout's
    git(wt, "commit", "-qam", "double f")
    assert "notice sent to w1:p99" in tasks.progress(store, "T-0001", "delivered", "researcher-1", "doubled")["summary"]

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    accepted = tasks.review_task(project, "T-0001", "accept", "fine")
    assert "sr/researcher-1/T-0001 has 1 commit(s) not in main: merge it with a merge commit" in accepted["summary"]
    agents.reset(project, "researcher-1", "clear")
    assert calls(herdr_log)[-1][:4] == ["agent", "prompt", "researcher-1", "/clear"]
    (wt / "scratch.py").write_text("x = 1\n")
    with pytest.raises(RLError) as exc:
        agents.assign(project, "researcher-1", task="T-0002")
    assert exc.value.code == "CONFLICT" and "scratch.py" in exc.value.message
    (wt / "scratch.py").unlink()
    agents.assign(project, "researcher-1", task="T-0002")
    assert git(wt, "branch", "--show-current") == "sr/researcher-1/T-0002"
    assert git(wt, "log", "-1", "--format=%s") == "start"  # a new branch from main, not from the last task
    closed = agents.close(project, "researcher-1")
    assert "kept (a new session named researcher-1 reuses it)" in closed["summary"]
    assert calls(herdr_log)[-1] == ["tab", "close", "w1:t1"]
    with project.read() as tx:
        s = tx.require(out["id"])
    assert s["status"] == "ended" and s["assignment"] == "T-0002"


def test_reviewer_and_advisor_sessions_answer_and_talk_through_messages(project, herdr_log, tmp_path, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    opened = agents.open_session(project, "reviewer", ticket=rv["id"])
    assert (opened["name"], opened["entry"]) == ("reviewer-1", "codex:gpt-6-astra@medium")
    start = [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--") + 1:] == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="medium"']
    text = agents.brief(project, "reviewer-1")["summary"]
    assert "review 1 of at most 2" in text and "requested by researcher-1" in text
    assert f"rl review answer {rv['id']}" in text and "read-only" in text

    answer = tmp_path / "answer.md"
    answer.write_text(MEDIUM)
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "reviewer-1")
    out = reviews.answer_live(project, rv["id"], str(answer))
    assert out["relation"] == "cross_model_unverified" and "medium open" in out["summary"] and not out["escalated"]
    assert "recorded (no live session" in agents.tell(project, "researcher-1", "RV-0001: 1 medium finding")["summary"]
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return 2 * x\n")
    recheck = reviews.request_review(project, "E-0001", ["src/model.py"], "fixed?", ["claude-opus-5-5"], scope="op",
                                     recheck_of=rv["id"])
    assert f"reviewer-1 assigned {recheck['id']}" in recheck["summary"]  # the same reviewer takes the re-check
    assert calls(herdr_log)[-1][:3] == ["agent", "prompt", "reviewer-1"]
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "reviewer-1")
    answer.write_text(PASS)
    reviews.answer_live(project, recheck["id"], str(answer))
    note = agents.tell(project, "coordinator", "round RV-0001 done: pass")
    assert "notice sent to w1:p99" in note["summary"]
    assert "not from the user" in calls(herdr_log)[-1][3]
    with pytest.raises(RLError):
        reviews.answer_live(project, recheck["id"], str(answer))  # a review answer is final
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")

    ad = reviews.prepare_advice(project, "which route?", "H-0001")
    first = agents.open_session(project, "advisor", ticket=ad["ids"][0])
    start = [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]][-1]
    assert first["entry"] == "codex:gpt-6-astra@max" and "--search" in start and 'model_reasoning_effort="max"' in start
    second = agents.open_session(project, "advisor", ticket=ad["ids"][1])
    assert (second["name"], second["entry"]) == ("advisor-2", "claude:claude-fable-5-1@high")
    assert f"rl advise answer {ad['ids'][0]}" in agents.brief(project, "advisor-1")["summary"]
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "advisor-1")
    answer.write_text(PROPOSALS)
    reviews.answer_live(project, ad["ids"][0], str(answer))
    answer.write_text(STOP)
    assert "recommends stop" in reviews.answer_live(project, ad["ids"][0], str(answer))["summary"]  # revised
    with project.read() as tx:
        t = tx.require(ad["ids"][0])
    assert len(t["earlier_answers"]) == 1 and t["answered_by"] == first["id"]
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "advisor-2")
    with pytest.raises(RLError):
        reviews.answer_live(project, ad["ids"][0], str(answer))  # only the advisor that answered revises it
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    agents.close(project, "advisor-1")
    agents.close(project, "advisor-2")
    assert agents.list_sessions(project)["summary"].startswith(f"{opened['id']} reviewer-1 [reviewer, idle]")


def test_a_session_waiting_at_a_startup_dialog_is_reported_not_answered(project, herdr_log, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_BLOCKED", "1")
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    out = agents.open_session(project, "researcher", "researcher-1", task="T-0001", worktree=False)
    assert "waits at a dialog" in out["summary"] and "ask the user before answering it" in out["summary"]
    assert not any(c[:2] == ["agent", "prompt"] for c in calls(herdr_log))
    with project.read() as tx:
        s = tx.require(out["id"])
    assert s["status"] == "active" and s["assignment"] == "T-0001"


def test_sessions_need_herdr_but_messages_do_not(project, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    with pytest.raises(RLError) as exc:
        agents.open_session(project, "reviewer")
    assert exc.value.code == "UNAVAILABLE" and "rl review run" in exc.value.message
    assert "recorded (no live session" in agents.tell(project, "researcher-1", "hello")["summary"]
    assert "MSG-" in tasks.inbox(project, "researcher-1")["summary"]


def test_a_worktree_without_the_configuration_belongs_to_the_main_project(project, herdr_log, tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "src", "docs")  # the configuration is not committed
    git(tmp_path, "commit", "-q", "-m", "code")
    wt = tmp_path.parent / f"{tmp_path.name}-wt"
    git(tmp_path, "worktree", "add", "-q", str(wt))
    found = find_project(start=wt / "src")
    assert (found.root, found.checkout) == (tmp_path, wt)
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-2")
    note = hooks.session_start("claude", json.dumps({"cwd": str(wt)}))["hookSpecificOutput"]["additionalContext"]
    assert "You are researcher-2" in note and "not the coordinator" in note and "agent brief researcher-2" in note
