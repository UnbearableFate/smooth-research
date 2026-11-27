"""Starting a session's agent in a herdr tab, and the dialogs on its screen.

The agent runs in its own tab, with the environment that names its project, session and checkout, and with the
command line its entry and role call for. A host's question whether to trust the folder rl opened the session
in is answered by rl; any other dialog is recorded as the session needing a decision, which the coordinator
answers (rl agent answer), or the user for the coordinator's own session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from . import adapters, herdr, usage
from .project import ENV_CHECKOUT, ENV_PROJECT, agent_args
from .roles import ENV_SESSION
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
# The footer a host shows under its prompt box when it takes prompts: no dialog is open then, whatever the
# transcript above quotes (an agent that read another's dialog shows its text).
TAKES_PROMPTS = {"codex": "? for shortcuts"}


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
    """The dialog at the bottom of the screen that herdr does not recognize as one, if any: never while the host
    shows its prompt box, and not text quoted higher up in the transcript (Codex trial, 11-25)."""
    lines = [x for x in screen.splitlines() if x.strip()]
    footer = TAKES_PROMPTS.get(host or "")
    if footer and any(footer in x for x in lines[-3:]):
        return None
    bottom = "\n".join(lines[-15:])
    return next((d for d in UNSEEN.get(host or "", ()) if d in bottom), None)


def rl_command() -> str:
    """How sessions call rl: the plugin's bin/rl when the plugin started this process, else rl on PATH."""
    return os.environ.get("SMOOTH_RESEARCH_RL") or "rl"


def decide(name: str) -> str:
    """How the coordinator decides a dialog in a session."""
    return (f"decide it: `rl agent dialog {name}` shows it, `rl agent answer {name} \"<option>\" --reason ..` "
            "answers it (only what only the user can decide goes to the user)")


def launch_args(store: Store, entry: adapters.Entry, kind: str, name: str, host_session: str | None,
                resume: bool = False) -> list[str]:
    """The agent's own command-line arguments: model, effort, session id and name (resume: go on with the host's
    conversation host_session instead of starting one, design 2026-11-27 3.2); tools by role (reviewers do not get
    file edit tools, Codex advisors get web search); the subscription usage (usage.py): Claude Code reports it
    through rl's status line, and Codex does not offer a cheaper model in a dialog when it runs low; then
    agents.args.<host> from the configuration."""
    if entry.entry == "claude":
        args = ["--model", entry.model, "--resume" if resume else "--session-id", host_session, "-n", name]
        args += ["--effort", entry.effort] if entry.effort else []
        args += ["--disallowedTools", "Edit,Write,NotebookEdit"] if kind == "reviewer" else []
        args += ["--settings", json.dumps({"statusLine": usage.statusline_setting(rl_command())})]
    elif entry.entry == "codex":
        args = (["resume", host_session] if resume and host_session else []) + ["-m", entry.model]
        args += ["-c", f'model_reasoning_effort="{entry.effort}"'] if entry.effort else []
        args += ["-c", "notice.hide_rate_limit_model_nudge=true"]
        args += ["--search"] if kind == "advisor" else []
    else:
        args = ["--model", entry.model, "--session" if resume else "--session-id", host_session, "--name", name]
        args += ["--thinking", entry.effort] if entry.effort else []
    return args + agent_args(store.project.config, entry.entry)


def herdr_name(store: Store, name: str) -> str:
    """The herdr agent name (and tab label) of a session: herdr takes [a-z][a-z0-9_-]{0,31}, unique on its server,
    so the session name is lowercased behind a tag of the project (four letters of its folder and eight hex digits
    of a digest of its path), and two projects with the same record ids keep apart."""
    root = str(store.project.root.resolve())
    slug = re.sub(r"[^a-z0-9]", "", store.project.root.name.lower())[:4]
    slug = slug if slug[:1].isalpha() else "p" + slug[:3]
    tag = hashlib.blake2b(root.encode(), digest_size=4).hexdigest()
    return f"{slug}{tag}-{name.lower()}"[:32]


def bound(store: Store, s: dict) -> tuple[str | None, dict | None]:
    """The session's pane and its live agent, when the agent there is still the session's own: herdr reuses pane
    and tab ids after a restart, so an id alone may name another project's agent. (None, None) when the pane holds
    no agent or another one; such a binding is stale, and nothing is done to whatever lives there."""
    pane = s.get("herdr_pane")
    if not pane:
        return None, None
    info = herdr.agent(pane)
    if info is None or (info.get("name") or "") != herdr_name(store, s["name"]):
        return None, None
    return pane, info


def own_tab(store: Store, s: dict) -> str | None:
    """The session's tab when it is still the one rl made for it (by its label), else None. A lookup herdr could not
    answer (a timeout, say) raises: it does not show the tab is gone, so nothing is closed or rebound on it."""
    tab = s.get("herdr_tab")
    if not tab:
        return None
    found = herdr.get("tab", tab)  # None when herdr says it is closed
    mine = herdr_name(store, s["name"])
    if not found or found.get("label") != mine:
        return None
    # and nothing else lives there: every agent in the tab is the session's own (a user's shell may stay)
    panes = herdr.call("pane", "list", "--workspace", found.get("workspace_id") or tab.split(":")[0],
                       timeout=15).get("panes") or []
    for pane in panes:
        if pane.get("tab_id") == tab and pane.get("agent"):
            info = herdr.agent(pane["pane_id"])
            if info is not None and (info.get("name") or "") != mine:
                return None
    return tab


def new_tab(store: Store, s: dict) -> dict:
    """A herdr tab for a session, in its folder, with the environment that names its project, session and checkout."""
    env = {ENV_PROJECT: str(store.project.root), "SMOOTH_RESEARCH_RL": rl_command(), ENV_SESSION: s["name"],
           **({ENV_CHECKOUT: s["cwd"]} if s.get("cwd") and Path(s["cwd"]) != store.project.root else {})}
    return herdr.new_tab("--cwd", s["cwd"], "--label", herdr_name(store, s["name"]), "--no-focus",
                         *[a for k, v in env.items() for a in ("--env", f"{k}={v}")])


def start_agent(store: Store, s: dict, timeout: int = 90, tab: str | None = None, pane: str | None = None,
                resume: str | None = None) -> dict:
    """Start the session's agent: in the pane given (of the tab given), else in a new tab; in a new conversation,
    or going on with the host's conversation resume (the session's earlier one); answer the host's question
    whether to trust the session's folder. Returns the tab, the pane, the host session id, and why the agent still
    waits when it is not ready (a dialog someone decides). When the agent does not start, a tab made here is closed
    and the error raised."""
    name, entry = s["name"], adapters.Entry.parse(s["entry"])
    agent_name = herdr_name(store, name)
    made = not pane
    if made:
        created = new_tab(store, s)
        tab, pane = created["tab"]["tab_id"], created["root_pane"]["pane_id"]
    host_session = resume or (str(uuid.uuid4()) if entry.entry in ("claude", "pi") else None)
    waiting = None
    try:
        herdr.call("agent", "start", agent_name, "--kind", entry.entry, "--pane", pane, "--timeout", str(timeout * 1000),
                   "--", *launch_args(store, entry, s["role"], name, host_session, resume=bool(resume)),
                   timeout=timeout + 30)
    except RLError as exc:
        if exc.details.get("herdr_code") != "agent_not_ready":
            if made:
                herdr.call("tab", "close", tab, timeout=30)
            raise
        waiting = answer_trust(pane, entry.entry, Path(s["cwd"]), timeout, label=name)
        if not waiting:
            with store.write() as tx:
                tx.event("session.trusted", name, f"answered {entry.entry}'s folder trust question for {s['cwd']}")
    if not waiting:
        screen = _until_started(pane, entry.entry, timeout)
        if unseen(entry.entry, screen):
            waiting = f"{name} waits at a dialog herdr does not see ({excerpt(screen)})"
    host_session = herdr.session_id(pane) or host_session
    if not host_session and not waiting:
        host_session = status_id(pane, entry.entry)
    return {"tab": tab, "pane": pane, "host_session": host_session, "waiting": waiting}


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


def answer_trust(name: str, host: str, folder: Path, timeout: int, label: str | None = None) -> str | None:
    """Answer the host's question whether to trust the folder rl opened the session in, as the coordinator's
    choice. None once the agent is ready; otherwise why it still waits (any other dialog is the coordinator's
    to decide). name: the pane to act on; label: the session it is, for the messages."""
    ask = decide(label or name)
    pane, name = name, label or name
    question, accept = TRUST_QUESTIONS.get(host, (None, None))
    try:
        screen = herdr.read(pane)
        if not question or question not in screen:
            return f"{name} waits at a dialog ({excerpt(screen)}): {ask}"
        if not _names(screen, folder):
            return f"{name} waits at a folder trust question that does not name {folder}: {ask}"
        problem = select(pane, accept, screen, lambda now: question in now)
        if problem:
            return f"{name} waits at the folder trust question for {folder}; {problem}: {ask}"
        herdr.keys(pane, "enter")
        deadline, other = time.monotonic() + timeout, 0
        while time.monotonic() < deadline:
            state = herdr.status(pane)
            if state in ("idle", "done"):
                return None
            if state == "gone":
                return f"{name} exited at the folder trust question; rl session open {name} opens it again"
            # blocked on something else for a few looks in a row (not the moment the trust dialog goes away)
            other = other + 1 if state == "blocked" and question not in herdr.read(pane) else 0
            if other >= 6:
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
