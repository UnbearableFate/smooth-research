"""Starting a session's agent in a herdr tab, and the dialogs on its screen.

The agent runs in its own tab, with the environment that names its project, session and checkout, and with the
command line its entry and role call for. A host's question whether to trust the folder rl opened the session
in is answered for the coordinator; any other dialog is the coordinator's to decide (rl agent dialog, rl agent
answer). rl agent open and the watcher's restart start agents the same way.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from . import adapters, herdr, usage
from .delegate import rl_command
from .herdr import ENV_ROLE, ENV_SESSION
from .project import ENV_CHECKOUT, ENV_PROJECT, agent_args
from .store import Store
from .util import RLError

# The folder trust question of each host: the text that marks it and the option that trusts the folder. rl answers
# it only for the folder it opened the session in: the session's worktree, or the project root, which the user
# trusted when starting the coordinator there.
TRUST_QUESTIONS = {"claude": ("Quick safety check", "Yes, I trust this folder"),
                   "codex": ("Trust this folder?", "Trust and continue")}
SELECTED = ("❯", "›", "▶", ">")
# What a host shows while it still starts (Codex: its MCP servers) though herdr already sees it ready: a prompt typed
# then only waits in its queue, and shows no working state herdr could confirm
STARTING = {"codex": "Waiting for startup"}
# The question a host asks after /new in a git checkout before it starts the new conversation, and the option that
# keeps the folder rl opened the session in. herdr sees the agent idle there: a prompt typed then only answers the
# question, and is lost
NEW_CONVERSATION = {"codex": ("Where should the new conversation run?", "Use current Git worktree")}
# Dialogs a host shows that herdr does not recognize (it sees the agent idle, and what is typed then goes to the
# dialog), by the text that marks them: Codex's review of new or changed hooks, and its question after /new
UNSEEN = {"codex": ("Hooks need review", NEW_CONVERSATION["codex"][0])}


# The command that shows a host's session id on its screen without calling its model, and the line that holds it:
# for a Codex session herdr reports no id for (without herdr's Codex integration, or before the first turn of a
# conversation, when the integration's hook reports it)
STATUS = {"codex": ("/status", re.compile(r"^\s*Session:\s+([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\s*$", re.MULTILINE))}
# Lines of Codex's /status that name the signed-in user: not quoted from a screen into notes and records
PRIVATE = re.compile(r"^(\s*(?:Account|ChatGPT workspace):).*$", re.MULTILINE)


def status_id(name: str, host: str, wait: float = 10.0, every: float = 2.0) -> str | None:
    """The session id the host's status command shows (Codex: /status), typed into an agent that is ready and at no
    dialog; None for a host without one, or when it showed none within wait seconds. A Codex that herdr already
    sees ready may not have set up its session yet: its status then shows no id, and is asked again every few
    seconds."""
    command, pattern = STATUS.get(host, (None, None))
    if not command:
        return None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        before = len(pattern.findall(herdr.read(name)))  # an earlier status on the screen does not count
        if herdr.prompt(name, command):
            return None
        again = min(deadline, time.monotonic() + every)
        while time.monotonic() < again:
            found = pattern.findall(herdr.read(name))
            if len(found) > before:
                return found[-1]
            time.sleep(0.25)
    return None


def redact(screen: str) -> str:
    """A screen without the lines that name the signed-in user."""
    return PRIVATE.sub(r"\1 (not quoted)", screen)


def unseen(host: str | None, screen: str) -> str | None:
    """The dialog on the screen that herdr does not recognize as one, if any."""
    return next((d for d in UNSEEN.get(host or "", ()) if d in screen), None)


def decide(name: str) -> str:
    """How the coordinator decides a dialog in a session."""
    return (f"decide it: `rl agent dialog {name}` shows it, `rl agent answer {name} \"<option>\" --reason ..` "
            "answers it (only what only the user can decide goes to the user)")


def launch_args(store: Store, entry: adapters.Entry, kind: str, name: str, host_session: str | None) -> list[str]:
    """The agent's own command-line arguments: model, effort, session id and name; tools by role (reviewers do
    not get file edit tools, Codex advisors get web search); the subscription usage (usage.py): Claude Code
    reports it through rl's status line, and Codex does not offer a cheaper model in a dialog when it runs low
    (rl hands the work over instead); then agents.args.<host> from the configuration."""
    if entry.entry == "claude":
        args = ["--model", entry.model, "--session-id", host_session, "-n", name]
        args += ["--effort", entry.effort] if entry.effort else []
        args += ["--disallowedTools", "Edit,Write,NotebookEdit"] if kind == "reviewer" else []
        args += ["--settings", json.dumps({"statusLine": usage.statusline_setting(rl_command())})]
    elif entry.entry == "codex":
        args = ["-m", entry.model] + (["-c", f'model_reasoning_effort="{entry.effort}"'] if entry.effort else [])
        args += ["-c", "notice.hide_rate_limit_model_nudge=true"]
        args += ["--search"] if kind == "advisor" else []
    else:
        args = ["--model", entry.model, "--session-id", host_session, "--name", name]
        args += ["--thinking", entry.effort] if entry.effort else []
    return args + agent_args(store.project.config, entry.entry)


def new_tab(store: Store, s: dict) -> dict:
    """A herdr tab for a session, in its folder, with the environment that names its project, session and checkout."""
    env = {ENV_PROJECT: str(store.project.root), "SMOOTH_RESEARCH_RL": rl_command(), ENV_SESSION: s["id"],
           ENV_ROLE: s["role_id"], **({ENV_CHECKOUT: s["cwd"]} if s.get("worktree") else {})}
    return herdr.new_tab("--cwd", s["cwd"], "--label", s["role_id"], "--no-focus",
                         *[a for k, v in env.items() for a in ("--env", f"{k}={v}")])


def start_agent(store: Store, s: dict, timeout: int = 90, tab: str | None = None, pane: str | None = None) -> dict:
    """Start the session's agent in a new conversation: in the pane given (of the tab given), else in a new tab;
    answer the host's question whether to trust the session's folder. Returns the tab, the pane, the host session
    id, why the agent still waits when it is not ready (a dialog for the coordinator), and the note when the
    trust question was answered. When the agent does not start, a tab made here is closed and the error raised."""
    name, entry = s["role_id"], adapters.Entry.parse(s["entry"])
    made = not pane
    if made:
        created = new_tab(store, s)
        tab, pane = created["tab"]["tab_id"], created["root_pane"]["pane_id"]
    host_session = str(uuid.uuid4()) if entry.entry in ("claude", "pi") else None
    waiting = trusted = None
    try:
        herdr.call("agent", "start", name, "--kind", entry.entry, "--pane", pane, "--timeout", str(timeout * 1000),
                   "--", *launch_args(store, entry, s["role_kind"], name, host_session), timeout=timeout + 30)
    except RLError as exc:
        if exc.details.get("herdr_code") != "agent_not_ready":
            if made:
                herdr.call("tab", "close", tab, timeout=30)
            raise
        waiting = answer_trust(name, entry.entry, Path(s["cwd"]), timeout)
        if not waiting:
            trusted = f"answered {entry.entry}'s folder trust question for {s['cwd']}: trusted"
            with store.write() as tx:
                tx.event("session.trusted", s["id"], trusted)
    if not waiting:
        screen = _until_started(name, entry.entry, timeout)
        if unseen(entry.entry, screen):
            waiting = f"{name} waits at a dialog herdr does not see ({excerpt(screen)}): {decide(name)}"
    # the id herdr reports (Codex chooses its own), else the one its /status shows: the watcher reads the session's
    # log by it, and keeps it current
    host_session = herdr.session_id(name) or host_session
    if not host_session and not waiting:
        host_session = status_id(name, entry.entry)
    return {"tab": tab, "pane": pane, "host_session": host_session, "waiting": waiting, "trusted": trusted}


def _until_started(name: str, host: str, timeout: int) -> str:
    """Wait, at most timeout seconds, while the host shows that it still starts, so the first prompt starts a turn.
    The screen then ('' for a host whose screen rl need not read)."""
    marker, deadline = STARTING.get(host), time.monotonic() + timeout
    screen = herdr.read(name) if marker or host in UNSEEN else ""
    while marker and marker in screen and time.monotonic() < deadline:
        time.sleep(0.5)
        screen = herdr.read(name)
    return screen


def answer_new(name: str, host: str, timeout: int = 30, asked: float = 5.0) -> str | None:
    """After /new: answer the host's question where the new conversation runs with the session's own folder, and
    wait until the new conversation takes prompts. None once it does, else why it still waits (the coordinator's to
    decide). A host that does not ask within `asked` seconds (another version, a folder outside git) has started it."""
    question, keep = NEW_CONVERSATION.get(host, (None, None))
    if not question:
        return None
    ask = decide(name)
    deadline, until = time.monotonic() + timeout, time.monotonic() + asked
    try:
        while question not in (screen := herdr.read(name)):
            if time.monotonic() >= until:
                break
            time.sleep(0.25)
        else:
            problem = select(name, keep, screen, lambda now: question in now)
            if problem:
                return f"{name} asks where its new conversation runs; {problem}: {ask}"
            herdr.keys(name, "enter")
            while question in herdr.read(name):
                if time.monotonic() >= deadline:
                    return f"{name} still asks where its new conversation runs: {ask}"
                time.sleep(0.25)
    except RLError as exc:
        return f"{name} may still ask where its new conversation runs ({exc.message}): {ask}"
    _until_started(name, host, max(1, int(deadline - time.monotonic())))
    return None


def answer_trust(name: str, host: str, folder: Path, timeout: int) -> str | None:
    """Answer the host's question whether to trust the folder rl opened the session in, as the coordinator's
    choice. None once the agent is ready; otherwise why it still waits (any other dialog is the coordinator's
    to decide)."""
    ask = decide(name)
    question, accept = TRUST_QUESTIONS.get(host, (None, None))
    try:
        screen = herdr.read(name)
        if not question or question not in screen:
            return f"{name} waits at a dialog ({excerpt(screen)}): {ask}"
        if not _names(screen, folder):
            return f"{name} waits at a folder trust question that does not name {folder}: {ask}"
        problem = select(name, accept, screen, lambda now: question in now)
        if problem:
            return f"{name} waits at the folder trust question for {folder}; {problem}: {ask}"
        herdr.keys(name, "enter")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = herdr.status(name)
            if state in ("idle", "done"):
                return None
            if state == "gone":
                return f"{name} exited at the folder trust question; close it with `rl agent close {name}`"
            if state == "blocked" and question not in herdr.read(name):
                return f"{name} trusted {folder} and now waits at another dialog: {ask}"
            time.sleep(0.5)
        return f"{name} trusted {folder} but was not ready within {timeout}s: {ask}"
    except RLError as exc:
        return f"{name} waits at a dialog ({exc.message}): {ask}"


def _names(screen: str, folder: Path) -> bool:
    """Does the dialog name exactly this folder (absolute or under ~), not a longer path that starts with it?"""
    home = str(Path.home())
    names = [str(folder)] + (["~" + str(folder)[len(home):]] if str(folder).startswith(home + os.sep) else [])
    ends = r"(?=$|[\s,;:)'\"]|\.(?:\s|$))"
    return any(re.search(re.escape(n) + ends, line) for n in names for line in screen.splitlines())


def option_line(screen: str, option: str) -> str | None:
    """The line of a dialog's option: given as its number, or as the start of its text (else any line holding it).
    The lowest such line: the dialog sits at the bottom of the screen, under the conversation, whose text can have
    lines like it (a numbered list, a line that starts with the option's word)."""
    lines = screen.splitlines()[::-1]
    if option.strip().isdigit():
        pattern = re.compile(rf"^\s*(?:[{''.join(SELECTED)}]\s*)?{option.strip()}[.)]\s")
        return next((x for x in lines if pattern.match(x)), None)
    want = " ".join(option.split()).casefold()
    bare = [(x, re.sub(rf"^\s*(?:[{''.join(SELECTED)}]\s*)?(?:\d+[.)]\s*)?", "", x).casefold()) for x in lines]
    return next((x for x, b in bare if b.startswith(want)), None) or next((x for x, b in bare if want in b), None)


def select(target: str, option: str, screen: str, present: Callable[[str], bool]) -> str | None:
    """Move a dialog's selection to the option (down, then up); None once it is selected, else why not. After every
    key the screen is read until the selection has moved: a key shows a moment after it is sent, and deciding on the
    screen from before would press one key too many (Claude Code's two trust options wrap around). present(screen)
    says whether the dialog is still there: once it is gone (answered meanwhile, or closed by its host), no key is
    pressed, since in the input box an arrow key brings back an earlier prompt that the next notice would join."""
    if option_line(screen, option) is None:
        return f"no option '{option}' on the screen of {target}"
    for key in ["down"] * 6 + ["up"] * 12:
        if (option_line(screen, option) or "").lstrip().startswith(SELECTED):
            return None
        if not present(screen):
            return f"the dialog is no longer on the screen of {target}, so nothing more was pressed"
        herdr.keys(target, key)
        screen = _moved(target, screen)
    return f"rl could not select '{option}'"


def _moved(target: str, before: str, wait: float = 2.0) -> str:
    """The screen once its selected line is not the one before the key, or as it is after wait (a key at the end of
    a list that does not wrap around moves nothing). The lowest marked line: the conversation above the dialog
    marks earlier prompts the same way."""
    def selected(screen: str) -> str | None:
        return next((x for x in reversed(screen.splitlines()) if x.lstrip().startswith(SELECTED)), None)
    deadline = time.monotonic() + wait
    while selected(screen := herdr.read(target)) == selected(before) and time.monotonic() < deadline:
        time.sleep(0.1)
    return screen


def excerpt(screen: str, chars: int = 600) -> str:
    """The bottom of a screen, where a dialog sits, as one line (without the lines that name the signed-in user)."""
    shown = " / ".join(" ".join(x.split()) for x in redact(screen).splitlines() if x.strip())
    return ("…" + shown[-chars:]) if len(shown) > chars else shown or "an empty screen"
