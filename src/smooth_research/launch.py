"""Starting a session's agent in a herdr tab, and the dialogs on its screen.

The agent runs in its own tab, with the environment that names its project, session and checkout, and with the
command line its entry and role call for. A host's question whether to trust the folder rl opened the session
in is answered for the coordinator; any other dialog is the coordinator's to decide (rl agent dialog, rl agent
answer). rl agent open and the watcher's restart start agents the same way.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

from . import adapters, herdr
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


def launch_args(store: Store, entry: adapters.Entry, kind: str, name: str, host_session: str | None) -> list[str]:
    """The agent's own command-line arguments: model, effort, session id and name; tools by role (reviewers do
    not get file edit tools, Codex advisors get web search); then agents.args.<host> from the configuration."""
    if entry.entry == "claude":
        args = ["--model", entry.model, "--session-id", host_session, "-n", name]
        args += ["--effort", entry.effort] if entry.effort else []
        args += ["--disallowedTools", "Edit,Write,NotebookEdit"] if kind == "reviewer" else []
    elif entry.entry == "codex":
        args = ["-m", entry.model] + (["-c", f'model_reasoning_effort="{entry.effort}"'] if entry.effort else [])
        args += ["--search"] if kind == "advisor" else []
    else:
        args = ["--model", entry.model, "--session-id", host_session, "--name", name]
        args += ["--thinking", entry.effort] if entry.effort else []
    return args + agent_args(store.project.config, entry.entry)


def new_tab(store: Store, s: dict) -> dict:
    """A herdr tab for a session, in its folder, with the environment that names its project, session and checkout."""
    env = {ENV_PROJECT: str(store.project.root), "SMOOTH_RESEARCH_RL": rl_command(), ENV_SESSION: s["id"],
           ENV_ROLE: s["role_id"], **({ENV_CHECKOUT: s["cwd"]} if s.get("worktree") else {})}
    return herdr.call("tab", "create", "--cwd", s["cwd"], "--label", s["role_id"], "--no-focus",
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
    return {"tab": tab, "pane": pane, "host_session": host_session, "waiting": waiting, "trusted": trusted}


def answer_trust(name: str, host: str, folder: Path, timeout: int) -> str | None:
    """Answer the host's question whether to trust the folder rl opened the session in, as the coordinator's
    choice. None once the agent is ready; otherwise why it still waits (any other dialog is the coordinator's
    to decide)."""
    ask = (f"decide it: `rl agent dialog {name}` shows it, `rl agent answer {name} \"<option>\" --reason ..` "
           "answers it (only what only the user can decide goes to the user)")
    question, accept = TRUST_QUESTIONS.get(host, (None, None))
    try:
        screen = herdr.read(name)
        if not question or question not in screen:
            return f"{name} waits at a dialog ({excerpt(screen)}): {ask}"
        if not _names(screen, folder):
            return f"{name} waits at a folder trust question that does not name {folder}: {ask}"
        problem = select(name, accept, screen)
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
    """The line of a dialog's option: given as its number, or as the start of its text (else any line holding it)."""
    lines = screen.splitlines()
    if option.strip().isdigit():
        pattern = re.compile(rf"^\s*(?:[{''.join(SELECTED)}]\s*)?{option.strip()}[.)]\s")
        return next((x for x in lines if pattern.match(x)), None)
    want = " ".join(option.split()).casefold()
    bare = [(x, re.sub(rf"^\s*(?:[{''.join(SELECTED)}]\s*)?(?:\d+[.)]\s*)?", "", x).casefold()) for x in lines]
    return next((x for x, b in bare if b.startswith(want)), None) or next((x for x, b in bare if want in b), None)


def select(target: str, option: str, screen: str) -> str | None:
    """Move a dialog's selection to the option (down, then up); None once it is selected, else why not. After every
    key the screen is read until the selection has moved: a key shows a moment after it is sent, and deciding on the
    screen from before would press one key too many (Claude Code's two trust options wrap around)."""
    if option_line(screen, option) is None:
        return f"no option '{option}' on the screen of {target}"
    for key in ["down"] * 6 + ["up"] * 12:
        if (option_line(screen, option) or "").lstrip().startswith(SELECTED):
            return None
        herdr.keys(target, key)
        screen = _moved(target, screen)
    return f"rl could not select '{option}'"


def _moved(target: str, before: str, wait: float = 2.0) -> str:
    """The screen once its selected line is not the one before the key, or as it is after wait (a key at the end of
    a list that does not wrap around moves nothing)."""
    def selected(screen: str) -> str | None:
        return next((x for x in screen.splitlines() if x.lstrip().startswith(SELECTED)), None)
    deadline = time.monotonic() + wait
    while selected(screen := herdr.read(target)) == selected(before) and time.monotonic() < deadline:
        time.sleep(0.1)
    return screen


def excerpt(screen: str, chars: int = 600) -> str:
    """The bottom of a screen, where a dialog sits, as one line."""
    shown = " / ".join(" ".join(x.split()) for x in screen.splitlines() if x.strip())
    return ("…" + shown[-chars:]) if len(shown) > chars else shown or "an empty screen"
