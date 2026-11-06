"""Agent sessions in herdr tabs (through a fake herdr CLI) and researcher worktrees (with real git)."""

import fcntl
import json
import os
import stat
import subprocess

import pytest

from smooth_research import agents, hooks, launch, messages, reviews, tasks, watch
from smooth_research.project import find_project
from smooth_research.store import Store
from smooth_research.util import RLError

FAKE_HERDR = r"""#!{python}
import datetime, json, os, sys, pathlib
args = sys.argv[1:]
state_path = pathlib.Path(os.environ["FAKE_HERDR_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
state.setdefault("n", 0)
# sessions: the host session id herdr reports per agent; logs: a Codex session log the fake appends a turn to when
# the agent takes a prompt; asks: a dialog the next prompt opens (a request for permission)
# threads: the conversation id Codex's /status shows (a new one after /new; herdr reports it from that
# conversation's first turn on); panel: the /status shown on the screen; fresh: a conversation herdr has not
# reported yet
for key in ("agents", "cwd", "dialog", "status", "labels", "kinds", "seq", "sessions", "logs", "asks", "starting",
            "threads", "panel", "fresh"):
    state.setdefault(key, {})
# deaf: takes no prompt (herdr sees nothing); quick: its turn is too short for herdr to see it working; fail: its
# turns end with the service's overload error
for key in ("closed", "deaf", "quick", "fail"):
    state.setdefault(key, [])
with open(os.environ["FAKE_HERDR_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
# As in the hosts, a key shows on the screen a moment later (here: from the second read on) and the options wrap
# around. DIALOGS: the options (the first one selected at the start) and the screen.
DIALOGS = {
    "claude": (["No, exit", "Yes, I trust this folder"], " Accessing workspace:\n\n {cwd}\n\n Quick safety check: "
               "Is this a project you created or one you trust?\n\n{options}\n\n Enter to confirm · Esc to cancel"),
    "codex": (["Trust and continue", "Open restricted"], "  Trust this folder?\n  {cwd}\n  Codex can read, edit and run "
              "files here.\n{options}"),
    "mcp": (["Use this MCP server", "Continue without it"], " New MCP server found in .mcp.json: codegraph\n{options}"),
    "approval": (["1. Yes", "2. Yes, and don't ask again for rm commands", "3. No, and tell Claude what to do differently"],
                 " Bash command\n   rm -rf build/\n Do you want to proceed?\n{options}"),
    "new": (["1. Use current Git worktree  Keep using the current working directory",
             "2. Create new Git worktree   Create a separate checkout of this repository in another directory"],
            "  Where should the new conversation run?\n{options}\n  enter select · esc back"),
    "hooks": (["1. Review hooks", "2. Trust all and continue", "3. Continue without trusting (hooks won't run)"],
              "  Hooks need review\n  1 hook is new or changed.\n{options}\n  enter confirm · esc skip"),
}
UNSEEN = ("new", "hooks")  # Codex's dialogs that herdr does not recognize: it sees the agent idle

def done(result=None, error=None, text=None):
    state_path.write_text(json.dumps(state))
    if error:
        print(json.dumps({"error": {"code": error[0], "message": error[1]}}), file=sys.stderr)
        sys.exit(1)
    print(text if text is not None else json.dumps({"id": "cli", "result": result}))
    sys.exit(0)

def thread():
    state["t"] = state.get("t", 0) + 1
    return f"01a1acb4-0000-7000-8000-{state['t']:012d}"

def screen(name):
    d = state["dialog"].get(name)
    if state["starting"].get(name):  # Codex still starts its MCP servers for this many more reads
        state["starting"][name] -= 1
        return "> \n  GPT-6.1 Terra medium · Waiting for startup · esc cancel"
    if not d and name in state["panel"]:  # its /status, without the id while Codex still sets up its session
        sid = state["panel"][name]
        return ("/status\n  Account:             someone@example.com (Pro)\n  Collaboration mode:  Default\n"
                + (f"  Session:             {sid}\n" if sid else "") + "> ")
    if not d:
        return "> "
    options, text = DIALOGS[d["kind"]]
    shown = d.pop("shown", d["sel"])
    lines = "\n".join((" ❯ " if i == shown else "   ") + o for i, o in enumerate(options))
    return text.replace("{cwd}", d["cwd"]).replace("{options}", lines)

def open_tab(tab):  # the coordinator's tab w1:t99 and the tabs created here, until closed
    return tab not in state["closed"] and (tab == "w1:t99" or tab in state["labels"])

def reply(name, status=None):
    out = {"agent_status": status or state["status"].get(name, "idle"), "state_change_seq": state["seq"].get(name, 1)}
    if state.get("names", {}).get(name):  # the name an agent was started with, as herdr shows it for its pane
        out["name"] = state["names"][name]
    if state["sessions"].get(name):
        kind = state["kinds"].get(name, "claude")
        out["agent_session"] = {"agent": kind, "kind": "id", "source": "herdr:" + kind, "value": state["sessions"][name]}
    return {"agent": out}

def turn(name):  # the agent's Codex log, if it has one, shows the turn the prompt started
    if not state["logs"].get(name):
        return
    at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    n = state["seq"][name]
    end = {"type": "task_complete", "turn_id": f"turn-{n}"}
    if name in state["fail"]:
        end["error"] = {"message": "Selected model is at capacity. Please try a different model.",
                        "codex_error_info": "server_overloaded"}
    events = [("event_msg", {"type": "task_started", "turn_id": f"turn-{n}"}),
              ("response_item", {"type": "message", "role": "user"}),
              ("response_item", {"type": "function_call", "call_id": f"call-{n}", "name": "exec"}),
              ("response_item", {"type": "function_call_output", "call_id": f"call-{n}"}),
              ("event_msg", end)]
    with open(state["logs"][name], "a") as log:
        for kind, payload in events:
            log.write(json.dumps({"timestamp": at, "type": kind, "payload": payload}) + "\n")

group, action = args[0], args[1]
if (group, action) == ("tab", "create"):
    state["n"] += 1
    state["cwd"][f"w1:p{state['n']}"] = args[args.index("--cwd") + 1]
    state["labels"][f"w1:t{state['n']}"] = args[args.index("--label") + 1]
    done({"tab": {"tab_id": f"w1:t{state['n']}"}, "root_pane": {"pane_id": f"w1:p{state['n']}"}})
if (group, action) == ("tab", "close"):
    state["agents"] = {k: v for k, v in state["agents"].items() if v != args[2]}
    state["closed"].append(args[2])
    done({"type": "ok"})
if (group, action) == ("integration", "status"):  # plain text, one line per agent kind
    codex = os.environ.get("FAKE_HERDR_CODEX_INTEGRATION", "not installed")
    done(text=f"pi: not installed (/h/.pi/x.ts)\ncodex: {codex} (/h/.codex/herdr-agent-state.sh)")
if (group, action) == ("tab", "get"):
    if not open_tab(args[2]):
        done(error=("tab_not_found", f"tab {args[2]} not found"))
    done({"tab": {"tab_id": args[2], "label": state["labels"].get(args[2], "1")}})
if (group, action) == ("pane", "get"):
    tab = "w1:t" + args[2].split(":p")[1]
    if not open_tab(tab):
        done(error=("pane_not_found", f"pane {args[2]} not found"))
    held = [state["kinds"].get(k, "claude") for k, v in state["agents"].items() if v == tab]
    done({"pane": {"pane_id": args[2], "tab_id": tab, "agent": held[0] if held else None,
                   "cwd": state["cwd"].get(args[2])}})
if (group, action) == ("pane", "run"):
    done({"type": "ok"})
if (group, action) == ("agent", "start"):
    name, pane = args[2], args[args.index("--pane") + 1]
    state["agents"][name] = "w1:t" + pane.split(":p")[1]
    state["kinds"][name] = args[args.index("--kind") + 1]
    state["seq"][name] = state["seq"].get(name, 0) + 1
    launched = args[args.index("--") + 1:] if "--" in args else []
    if "--session-id" in launched:  # Claude Code and pi take the id rl chose
        state["sessions"][name] = launched[launched.index("--session-id") + 1]
    elif state["kinds"][name] == "codex" and not os.environ.get("FAKE_HERDR_NO_CODEX_ID"):  # Codex chooses its own
        state["sessions"][name] = f"cx-{name}-{state['seq'][name]}"  # herdr's Codex integration reports it
    else:
        state["sessions"].pop(name, None)
    state["logs"].pop(name, None)
    state["threads"][name] = thread()
    state["starting"][name] = int(os.environ.get("FAKE_HERDR_STARTING") or 0)
    if os.environ.get("FAKE_HERDR_HOOKS") and state["kinds"][name] == "codex":  # a new hook to review
        state["dialog"][name] = {"kind": "hooks", "sel": 0, "cwd": ""}
    blocked = os.environ.get("FAKE_HERDR_BLOCKED")
    if blocked:  # 1 or mcp: another dialog; claude, codex: their trust question; claude-other: for another folder
        kind = {"1": "mcp"}.get(blocked, blocked).replace("-other", "")
        cwd = "/elsewhere/project" if blocked.endswith("-other") else state["cwd"].get(pane, "")
        state["dialog"][name] = {"kind": kind, "sel": 0, "cwd": cwd}
        done(error=("agent_not_ready", f"agent {name} is blocked during startup"))
    done({"agent": {"name": name, "agent_status": "idle"}})
if (group, action) == ("agent", "read"):
    if args[2] not in state["agents"]:
        done(error=("agent_not_found", f"agent target {args[2]} not found"))
    done(text=screen(args[2]))
if (group, action) == ("agent", "send-keys"):
    for key in args[3:]:
        d = state["dialog"].get(args[2])
        if d and key == "down":
            d["shown"], d["sel"] = d["sel"], (d["sel"] + 1) % len(DIALOGS[d["kind"]][0])
        elif d and key == "enter":
            del state["dialog"][args[2]]
            if d["kind"] == "new":  # the new conversation: herdr reports the old one's id until its first turn
                state["threads"][args[2]], state["fresh"][args[2]] = thread(), True
            if DIALOGS[d["kind"]][0][d["sel"]] == "No, exit":
                state["agents"].pop(args[2], None)
    done({"type": "ok"})
if (group, action) in (("agent", "prompt"), ("agent", "get")):
    name = args[2]
    if (name not in state["agents"] and not name.startswith("w1:p")) or name in state.get("empty", []):
        done(error=("agent_not_found", f"agent target {name} not found"))
    if state["dialog"].get(name, {}).get("kind") in UNSEEN:  # a dialog herdr does not recognize: it sees idle
        if action == "prompt":  # what is typed goes to the question, and its Enter picks the selected option: lost
            del state["dialog"][name]
            done(error=("agent_prompt_stalled", "no working state observed") if "--wait" in args else None,
                 result=reply(name))
        done(reply(name))
    if name in state["dialog"]:
        if action == "prompt":
            done(error=("agent_blocked", "the agent waits at a dialog"))
        done(reply(name, "blocked"))
    if action == "prompt":
        waited = "--wait" in args
        if args[3] == "/new" and state["kinds"].get(name) == "codex":  # Codex asks where the new conversation runs
            state["dialog"][name] = {"kind": "new", "sel": 0, "cwd": ""}
            done(reply(name))
        if args[3] == "/status" and state["kinds"].get(name) == "codex":  # a local command: no turn
            late = state.setdefault("late", {}).get(name, int(os.environ.get("FAKE_HERDR_STATUS_LATE") or 0))
            state["late"][name] = max(late - 1, 0)
            if not os.environ.get("FAKE_HERDR_NO_STATUS"):
                state["panel"][name] = None if late else state["threads"].get(name)
            done(reply(name))
        if waited and args[3].startswith("/"):
            done(error=("agent_prompt_stalled", "no working state observed"))
        if state["starting"].get(name):  # it only queues the prompt until it has started
            done(error=("agent_prompt_stalled", "no working state observed") if waited else None,
                 result=reply(name))
        if name in state["deaf"]:  # nothing happens: herdr sees no working state
            done(error=("agent_prompt_stalled", "no working state observed") if waited else None,
                 result=reply(name))
        state["seq"][name] = state["seq"].get(name, 1) + 2  # it works on the prompt and becomes idle again
        state["panel"].pop(name, None)
        if state["fresh"].pop(name, None) and not os.environ.get("FAKE_HERDR_NO_CODEX_ID"):
            state["sessions"][name] = state["threads"][name]  # the integration's hook reports the new conversation
        turn(name)
        if name in state["asks"]:  # the prompt makes it ask for permission: herdr sees it blocked
            state["dialog"][name] = {"kind": state["asks"].pop(name), "sel": 0, "cwd": ""}
        if waited and name in state["quick"]:
            done(error=("agent_prompt_stalled", "no working state observed"))
    done(reply(name))
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
                       ("HERDR_WORKSPACE_ID", "w1"),
                       ("FAKE_HERDR_STATE", str(bindir / "state.json")), ("FAKE_HERDR_LOG", str(bindir / "log.jsonl")),
                       ("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.org"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(key, value)
    for key in ("SMOOTH_RESEARCH_ROLE", "SMOOTH_RESEARCH_SESSION", "SMOOTH_RESEARCH_CHECKOUT", "FAKE_HERDR_BLOCKED",
                "FAKE_HERDR_STARTING"):
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
    assert tab[tab.index("--workspace") + 1] == "w1"  # the coordinator's workspace, not the one the screen shows
    start = next(c for c in log if c[:2] == ["agent", "start"])
    args = start[start.index("--") + 1:]
    assert args[:2] == ["--model", "claude-opus-5-5"] and args[args.index("--effort") + 1] == "high"
    assert args[-2:] == ["--permission-mode", "auto"]  # agents.args.claude; no other permission flag is added
    status = json.loads(args[args.index("--settings") + 1])["statusLine"]  # it reports Claude's usage to rl
    assert status == {"type": "command", "command": "rl usage statusline"}
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
    assert f"- {wt / 'src/model.py'}" in package  # the worktree's code, not the main checkout's
    git(wt, "commit", "-qam", "double f")
    assert "notice sent to w1:p99" in tasks.progress(store, "T-0001", "delivered", "researcher-1", "doubled")["summary"]

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    accepted = tasks.review_task(project, "T-0001", "accept", "fine")
    assert "sr/researcher-1/T-0001 has 1 commit(s) not in main: merge it with a merge commit" in accepted["summary"]
    agents.reset(project, "researcher-1", "clear")
    assert [c for c in calls(herdr_log) if c[:2] == ["agent", "prompt"]][-1] == ["agent", "prompt", "researcher-1", "/clear"]
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
    assert "the watcher in tab w1:t2 stopped" in closed["summary"]  # the last open session closed
    assert [c for c in calls(herdr_log) if c[:2] == ["tab", "close"]] == [["tab", "close", "w1:t1"],
                                                                          ["tab", "close", "w1:t2"]]
    with project.read() as tx:
        s = tx.require(out["id"])
    assert s["status"] == "ended" and s["assignment"] == "T-0002"


def test_the_branch_to_merge_is_the_claimants_when_two_sessions_were_assigned(project, herdr_log, tmp_path):
    tasks.start_session(project, "coordinator", "claude")
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    commit_all(tmp_path)
    agents.open_session(project, "researcher", "alice", task="T-0001")
    agents.open_session(project, "researcher", "bob", task="T-0001")  # before either of them claimed it
    tasks.claim(project, "T-0001", "alice")
    wt = tmp_path.parent / f"{tmp_path.name}-worktrees" / "alice"
    (wt / "src/model.py").write_text("def f(x):\n    return 2 * x\n")
    git(wt, "commit", "-qam", "double f")
    tasks.progress(project, "T-0001", "delivered", "alice", "doubled")
    accepted = tasks.review_task(project, "T-0001", "accept", "fine")
    assert "sr/alice/T-0001 has 1 commit(s) not in main" in accepted["summary"]


def test_reviewer_and_advisor_sessions_answer_and_talk_through_messages(project, herdr_log, tmp_path, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    opened = agents.open_session(project, "reviewer", ticket=rv["id"])
    assert (opened["name"], opened["entry"]) == ("reviewer-1", "codex:gpt-6-astra@medium")
    start = [c for c in calls(herdr_log) if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--") + 1:] == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="medium"',
                                             "-c", "notice.hide_rate_limit_model_nudge=true"]  # no model dialog
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


def test_of_two_advisors_answering_together_the_first_keeps_its_answer(project, herdr_log, tmp_path, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    ad = reviews.prepare_advice(project, "which route?", "H-0001")
    first = agents.open_session(project, "advisor", ticket=ad["ids"][0])
    agents.open_session(project, "advisor", ticket=ad["ids"][1])
    mine, theirs = tmp_path / "mine.md", tmp_path / "theirs.md"
    mine.write_text(PROPOSALS)
    theirs.write_text(STOP)
    real, arrived = project.write, []

    def write():  # advisor-1's answer is recorded after advisor-2's answer was checked, before it is written
        if not arrived:
            arrived.append(True)
            reviews.answer_live(project, ad["ids"][0], str(mine), "advisor-1")
        return real()
    monkeypatch.setattr(project, "write", write)
    with pytest.raises(RLError, match="only that advisor revises it"):
        reviews.answer_live(project, ad["ids"][0], str(theirs), "advisor-2")
    with project.read() as tx:
        t = tx.require(ad["ids"][0])
    assert arrived and t["answered_by"] == first["id"] and not t.get("earlier_answers")
    assert not list((tmp_path / "research/advice").glob("*.revision-*"))  # no file of the refused answer
    assert "recommends stop" in reviews.answer_live(project, ad["ids"][0], str(theirs), "advisor-1")["summary"]


def test_a_cleared_codex_session_takes_its_next_assignment(project, herdr_log):
    tasks.start_session(project, "coordinator", "claude")
    ad = reviews.prepare_advice(project, "which route?", "H-0001")
    opened = agents.open_session(project, "advisor", ticket=ad["ids"][0])
    assert opened["entry"].startswith("codex:")
    out = agents.reset(project, "advisor-1", "clear")
    assert "ready for `rl agent assign advisor-1" in out["summary"] and not out["waiting"]
    keys = [c for c in calls(herdr_log) if c[:2] == ["agent", "send-keys"]]
    assert keys == [["agent", "send-keys", "advisor-1", "enter"]]  # the new conversation runs in its own folder
    new = out["host_session"]  # read from its /status
    assert new.startswith("01a1acb4-") and f"(a new host conversation: {new})" in out["summary"]
    watch.check(project)  # herdr still reports the old conversation's id: the watcher does not go back to it
    with project.read() as tx:
        assert tx.require(opened["id"])["host_session_id"] == new
    assert agents.assign(project, "advisor-1", ticket=ad["ids"][0])["summary"].endswith("; told to read its brief")
    watch.check(project)  # from its first turn on, herdr reports the new conversation: the same id
    with project.read() as tx:
        s = tx.require(opened["id"])
        assert s["host_session_id"] == new and "stale" not in (s.get("watch") or {})
        assert not [e for e in tx.all("Event") if e["type"] == "session.bound"]


def test_a_codex_session_without_an_id_from_herdr_is_found_by_its_status(project, herdr_log, monkeypatch):
    real = launch.status_id
    monkeypatch.setattr(launch, "status_id", lambda name, host: real(name, host, wait=1.5, every=0.3))
    tasks.start_session(project, "coordinator", "claude")
    ad = reviews.prepare_advice(project, "which route?", "H-0001")
    monkeypatch.setenv("FAKE_HERDR_NO_CODEX_ID", "1")  # no integration, or no turn yet that its hook reported
    out = agents.open_session(project, "advisor", ticket=ad["ids"][0])
    log = calls(herdr_log)
    status = log.index(["agent", "prompt", "advisor-1", "/status"])
    told = next(i for i, c in enumerate(log) if c[:3] == ["agent", "prompt", "advisor-1"] and "agent brief" in c[3])
    assert status < told and "found no Codex session id" not in out["summary"]
    with project.read() as tx:
        assert tx.require(out["id"])["host_session_id"].startswith("01a1acb4-")
    monkeypatch.setenv("FAKE_HERDR_STATUS_LATE", "1")  # its first /status comes before Codex set up its session
    out = agents.open_session(project, "advisor", ticket=ad["ids"][0], entry_text="codex:gpt-6-astra@max",
                              name="advisor-3")
    assert calls(herdr_log).count(["agent", "prompt", "advisor-3", "/status"]) == 2  # asked again
    with project.read() as tx:
        assert tx.require(out["id"])["host_session_id"].startswith("01a1acb4-")
    monkeypatch.delenv("FAKE_HERDR_STATUS_LATE")
    monkeypatch.setenv("FAKE_HERDR_NO_STATUS", "1")  # and its /status shows none either
    out = agents.open_session(project, "advisor", ticket=ad["ids"][1], entry_text="codex:gpt-6-astra@max")
    assert "advisor-2: rl found no Codex session id" in out["summary"]
    assert "herdr integration install codex" in out["summary"]
    assert "(the screen: >)" in out["summary"]  # what /status left on the screen
    monkeypatch.setenv("FAKE_HERDR_CODEX_INTEGRATION", "current (v10)")  # installed: the id comes with the first turn
    out = agents.open_session(project, "advisor", ticket=ad["ids"][1], entry_text="codex:gpt-6-astra@max",
                              name="advisor-4")
    assert "advisor-4: rl has no Codex session id yet" in out["summary"]
    assert "integration install" not in out["summary"]
    assert "log unavailable (no session id yet" in agents.list_sessions(project)["summary"]


def test_a_quoted_screen_leaves_out_who_is_signed_in():
    screen = ("/status\n  Account:             someone@example.com (Pro)\n  ChatGPT workspace:   1d7d4de1\n"
              "  Session:             01a1acb4-0000-7000-8000-000000000001\n> ")
    shown = launch.excerpt(screen)
    assert "someone@example.com" not in shown and "1d7d4de1" not in shown and "Session: 01a1acb4" in shown


def test_a_dialog_herdr_does_not_see_is_left_to_the_coordinator(project, herdr_log, monkeypatch):
    tasks.start_session(project, "coordinator", "claude")
    monkeypatch.setenv("FAKE_HERDR_HOOKS", "1")  # Codex asks to review a new hook; herdr sees the agent idle
    task = tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")["id"]
    out = agents.open_session(project, "researcher", "researcher-1", entry_text="codex:m", task=task, worktree=False)
    assert "researcher-1 waits at a dialog herdr does not see (" in out["summary"] and "Hooks need review" in out["summary"]
    assert "not yet told" in out["summary"] and "rl agent answer researcher-1" in out["summary"]
    assert "researcher-1 waits at a dialog (" in watch.check(project)["summary"]  # reported, nothing typed
    assert not any(c[:3] == ["agent", "prompt", "researcher-1"] for c in calls(herdr_log))
    assert "at a dialog herdr does not see" in agents.dialog(project, "researcher-1")["summary"]
    out = agents.answer(project, "researcher-1", "Continue without trusting", "hooks not read")
    assert out["chosen"].startswith("3. Continue without trusting") and "told to read its brief" in out["summary"]
    assert any(c[:3] == ["agent", "prompt", "researcher-1"] and "agent brief researcher-1" in c[3]
               for c in calls(herdr_log))


def test_a_session_waiting_at_another_dialog_is_left_to_the_coordinator(project, herdr_log, monkeypatch):
    for n, (dialog, said) in enumerate((("mcp", "waits at a dialog (New MCP server found"),
                                        ("claude-other", "a folder trust question that does not name")), 1):
        monkeypatch.setenv("FAKE_HERDR_BLOCKED", dialog)
        task = tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")["id"]
        out = agents.open_session(project, "researcher", f"researcher-{n}", task=task, worktree=False)
        assert said in out["summary"] and 'rl agent answer researcher-' in out["summary"]
        with project.read() as tx:
            s = tx.require(out["id"])
        assert s["status"] == "active" and s["assignment"] == task
    assert not any(c[:2] in (["agent", "prompt"], ["agent", "send-keys"]) for c in calls(herdr_log))


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_the_trust_question_for_the_folder_rl_opened_the_session_in_is_answered(project, herdr_log, tmp_path,
                                                                                 monkeypatch, host):
    monkeypatch.setenv("FAKE_HERDR_BLOCKED", host)
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    commit_all(tmp_path)
    out = agents.open_session(project, "researcher", "researcher-1", entry_text=f"{host}:m", task="T-0001")
    wt = tmp_path.parent / f"{tmp_path.name}-worktrees" / "researcher-1"
    assert f"answered {host}'s folder trust question for {wt}: trusted" in out["summary"]
    log = calls(herdr_log)
    keys = [c[3:] for c in log if c[:2] == ["agent", "send-keys"]]
    assert keys == ([["down"], ["enter"]] if host == "claude" else [["enter"]])  # Claude selects "No, exit" first
    assert any(c[:2] == ["agent", "prompt"] and "agent brief researcher-1" in c[3] for c in log)
    with project.read() as tx:
        assert any(e["type"] == "session.trusted" for e in tx.all("Event"))


def test_the_first_assignment_waits_until_codex_has_started(project, herdr_log, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_STARTING", "3")  # it shows "Waiting for startup" for three more looks
    task = tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")["id"]
    out = agents.open_session(project, "researcher", "researcher-1", entry_text="codex:m", task=task, worktree=False)
    assert "assigned " + task in out["summary"] and "not confirmed" not in out["summary"]
    log = calls(herdr_log)
    reads = [i for i, c in enumerate(log) if c[:3] == ["agent", "read", "researcher-1"]]
    first = next(i for i, c in enumerate(log) if c[:3] == ["agent", "prompt", "researcher-1"])
    # typed only once Codex had started (4 looks) and showed no dialog herdr does not see (1 more)
    assert len(reads) == 5 and reads[-1] < first


def test_the_coordinator_answers_a_dialog_in_a_session_and_records_why(project, herdr_log, tmp_path, monkeypatch):
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    monkeypatch.setenv("FAKE_HERDR_BLOCKED", "mcp")
    opened = agents.open_session(project, "reviewer", ticket=rv["id"])
    assert "New MCP server found in .mcp.json" in opened["summary"]  # what it asks, for the coordinator to judge
    shown = agents.dialog(project, "reviewer-1")["summary"]
    assert shown.startswith("reviewer-1 is blocked at a dialog:") and "Continue without it" in shown
    with pytest.raises(RLError) as exc:
        agents.answer(project, "reviewer-1", "Continue without it", "")
    assert exc.value.code == "INVALID"
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    with pytest.raises(RLError) as exc:
        agents.answer(project, "reviewer-1", "Continue without it", "not needed")
    assert "the coordinator's decision, not researcher-1's" in exc.value.message
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")

    out = agents.answer(project, "reviewer-1", "continue without", "a review reads the package; no MCP server needed")
    assert out["chosen"] == "Continue without it" and out["state"] == "idle"
    assert f"{rv['id']} pending: told to read its brief" in out["summary"]  # it never got its assignment
    log = calls(herdr_log)
    assert [c[3:] for c in log if c[:2] == ["agent", "send-keys"]] == [["down"], ["enter"]]
    assert log[-1][:3] == ["agent", "prompt", "reviewer-1"] and "agent brief reviewer-1" in log[-1][3]
    with project.read() as tx:
        why = [e["summary"] for e in tx.all("Event") if e["type"] == "session.answered"]
    assert why == ["reviewer-1: chose 'Continue without it' at '"
                   + launch.excerpt(" New MCP server found in .mcp.json: codegraph\n ❯ Use this MCP server\n"
                                     "   Continue without it", chars=200)
                   + "': a review reads the package; no MCP server needed"]
    with pytest.raises(RLError) as exc:
        agents.answer(project, "reviewer-1", "1", "again")
    assert exc.value.code == "CONFLICT"  # not at a dialog now

    fake_state = herdr_log.parent / "state.json"
    state = json.loads(fake_state.read_text())
    state["dialog"]["reviewer-1"] = {"kind": "approval", "sel": 0, "cwd": ""}
    fake_state.write_text(json.dumps(state))
    out = agents.answer(project, "reviewer-1", "3", "a reviewer does not delete build outputs")
    assert out["chosen"].startswith("3. No, and tell Claude") and out["state"] == "idle"


def test_the_option_of_a_dialog_is_found_under_a_conversation_with_lines_like_it():
    screen = ("  When the notice arrives, I'll:\n  1. Check each job's log\n  2. Acknowledge the notification\n"
              "  Now I'm recording the attempts.\n❯ [smooth-research MSG-0011 NOTE from coordinator] When you resume\n"
              " Bash command\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend")
    assert launch.option_line(screen, "2") == "   2. No"  # not the numbered list of the conversation above it
    assert launch.option_line(screen, "No") == "   2. No"  # nor its line that starts with "Now"
    assert launch.option_line(screen, "1") == " ❯ 1. Yes"


def test_no_key_is_pressed_once_the_dialog_is_gone(monkeypatch):
    pressed = []  # in the input box an arrow key brings back an earlier prompt, which the next notice would join
    monkeypatch.setattr(launch.herdr, "keys", lambda target, *names: pressed.append(names))
    out = launch.select("researcher-1", "2", " Do you want to proceed?\n ❯ 1. Yes\n   2. No", lambda now: False)
    assert "no longer on the screen of researcher-1, so nothing more was pressed" in out and pressed == []


def test_a_notice_for_a_session_at_a_dialog_waits_until_it_is_at_none(project, herdr_log, tmp_path):
    (tmp_path / "src/model.py").write_text("def f(x):\n    return x\n")
    rv = reviews.request_review(project, "E-0001", ["src/model.py"], "right?", ["claude-opus-5-5"], scope="op")
    agents.open_session(project, "reviewer", ticket=rv["id"])
    fake_state = herdr_log.parent / "state.json"

    def at_dialog():
        state = json.loads(fake_state.read_text())
        state["dialog"]["reviewer-1"] = {"kind": "approval", "sel": 0, "cwd": ""}
        fake_state.write_text(json.dumps(state))

    def prompts():
        return [c[3] for c in calls(herdr_log) if c[:3] == ["agent", "prompt", "reviewer-1"]]

    at_dialog()
    before = len(prompts())
    told = agents.tell(project, "reviewer-1", "please also check the loss mask")
    assert told["delivered_to"] is None and "no notice: reviewer-1 waits at a dialog, so no notice was typed; " \
        f"it hears of {told['id']} once it is at no dialog" in told["summary"]
    assert len(prompts()) == before  # nothing typed into the dialog
    with project.read() as tx:
        sid = next(x["id"] for x in tx.all("Session") if x["role_id"] == "reviewer-1")
        assert tx.require(sid)["watch"]["notices"] == [told["id"]]

    lock = project.project.state_dir / "locks" / f"answer-{sid}.lock"  # an answer still running
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(RLError) as exc:
            agents.answer(project, "reviewer-1", "3", "a reviewer does not delete build outputs")
        assert exc.value.code == "CONFLICT" and "another `rl agent answer reviewer-1` is still running" in exc.value.message
    finally:
        os.close(fd)
    out = agents.answer(project, "reviewer-1", "3", "a reviewer does not delete build outputs")
    assert f"told of {told['id']}, held while it waited at a dialog" in out["summary"]
    assert any(told["id"] in p and "came for you while you waited at a dialog" in p for p in prompts()[before:])
    with project.read() as tx:
        assert not tx.require(sid)["watch"].get("notices")

    at_dialog()  # a dialog its host closed by itself (Claude Code denies after a while): the watcher tells it
    again = agents.tell(project, "reviewer-1", "and the data split")
    state = json.loads(fake_state.read_text())
    del state["dialog"]["reviewer-1"]
    fake_state.write_text(json.dumps(state))
    beat = watch.check(project)
    assert f"reviewer-1: told of {again['id']}, held while it waited at a dialog" in beat["summary"]
    assert again["id"] in prompts()[-1]


def test_sessions_need_herdr_but_messages_do_not(project, monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    with pytest.raises(RLError) as exc:
        agents.open_session(project, "reviewer")
    assert exc.value.code == "UNAVAILABLE" and "rl review run" in exc.value.message
    assert "recorded (no live session" in agents.tell(project, "researcher-1", "hello")["summary"]
    assert "MSG-" in messages.inbox(project, "researcher-1")["summary"]


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
