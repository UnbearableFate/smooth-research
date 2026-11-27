"""Host hook handlers shared by Claude Code and Codex (both pass JSON on stdin; design 2026-11-27, 3.1, 3.2).

- SessionStart: in a session rl opened, who it is and to read its pack; in a person's own session in a project, a
  pointer to `rl resume` and `rl status`; elsewhere, that the plugin is there.
- Stop: in a session rl opened, the to-do items it was not told of yet are handed to it as it ends its turn
  (decision: block, with them as the reason), and noted as told, so the same item never stops it twice and nothing
  loops. While it goes on because of this (stop_hook_active), only items that came meanwhile stop it again.
- UserPromptSubmit (the coordinator): when the user speaks, the items not told yet are added to the prompt, and the
  time is noted, so that commands do not type into the pane while the user talks to it (3.2, case 5).

A hook never refuses the agent anything and never fails: whatever goes wrong, it lets the turn end as it would.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .launch import rl_command
from .project import find_project
from .roles import ENV_SESSION
from .util import RLError, now_utc

NOTICE = "[smooth-research]"
SHOWN = 4  # items handed over at once (13: at most four, one line each); the rest are in rl inbox


def _event(raw: str) -> dict:
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    return event if isinstance(event, dict) else {}


def session_start(host: str, raw: str) -> dict:
    event = _event(raw)
    rl = rl_command()
    cwd = Path(event.get("cwd") or ".").resolve()
    try:
        project = find_project(start=cwd)
    except RLError as exc:
        if exc.code != "NO_PROJECT":
            return _out("SessionStart", f"Smooth Research project found, but `{rl} status` fails: {exc.message}")
        return _out("SessionStart", f"Smooth Research is available (CLI: {rl}); use its skill to start or resume an "
                                    "ML research project.")
    name = os.environ.get(ENV_SESSION)
    if name:
        return _out("SessionStart", f"Smooth Research project at {project.root} (CLI: {rl}). You are the session "
                                    f"{name}. Run `{rl} pack` first and work from what it prints; `{rl} inbox` lists "
                                    "what waits on you. When nothing waits on you, end your turn: the command that "
                                    "hands you work tells you.")
    return _out("SessionStart", f"Smooth Research project at {project.root} (CLI: {rl}). Use the smooth-research "
                                f"skill: `{rl} status` shows the project; `{rl} resume` runs it (the coordinator's "
                                "pane, the sessions with work, the observer)."
                + "".join(f"\n- configuration: {w}" for w in project.warnings))


def _out(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _untold(event: dict):
    """(store, session record, items not told yet) for the session rl opened here, or None."""
    name = os.environ.get(ENV_SESSION)
    if not name:
        return None
    from . import sessions
    from .store import Store
    cwd = Path(event.get("cwd") or ".").resolve()
    project = find_project(start=cwd)
    store = Store(project, name)
    with store.read() as tx:
        s = sessions.open_record(tx, name)
        if s is None:  # opened by hand (rl session open --print): no record of what it was told
            return None
        items = sessions.untold(tx, project.config, name, s)
    return store, s, items


def _lines(items) -> str:
    from .sessions import notice_line
    text = "\n".join(notice_line(i) for i in items[:SHOWN])
    if len(items) > SHOWN:
        text += f"\n{NOTICE} +{len(items) - SHOWN} more: rl inbox"
    return text


def stop(host: str, raw: str) -> dict:
    """Stop: hand the session the items it was not told of yet, once each (3.2, case 2)."""
    try:
        found = _untold(_event(raw))
        if not found:
            return {}
        store, s, items = found
        if not items:
            return {}
        from .sessions import mark_told
        mark_told(store, s["id"], items, "handed over by the Stop hook")
        return {"decision": "block",
                "reason": f"{NOTICE} New since you last looked (rl inbox lists all):\n{_lines(items)}\n"
                          "Handle these, then end your turn."}
    except Exception:  # a hook lets the turn end as it would
        return {}


def user_prompt(host: str, raw: str) -> dict:
    """UserPromptSubmit (the coordinator): note that the user speaks, and add what is new (3.2, case 5)."""
    event = _event(raw)
    if os.environ.get(ENV_SESSION) != "coordinator":
        return {}
    try:
        prompt = str(event.get("prompt") or "")
        found = _untold(event)
        if not found:
            return {}
        store, s, items = found
        if prompt.lstrip().startswith(NOTICE):  # rl's own notice, typed by a command: not the user, told already
            return {}
        with store.write() as tx:
            tx.update(s["id"], {"last_user_at": now_utc()})
        if not items:
            return {}
        from .sessions import mark_told
        mark_told(store, s["id"], items, "added to the user's prompt")
        return _out("UserPromptSubmit", f"{NOTICE} Also new for you since you last looked (rl inbox lists all):\n"
                                        f"{_lines(items)}")
    except Exception:
        return {}
