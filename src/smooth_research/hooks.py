"""Host hook handlers shared by Claude Code and Codex (both pass JSON on stdin and read hookSpecificOutput).

At session start: in a session the watcher opened, who it is and to read its pack; in a person's own session in a
project, a pointer to `rl resume` and `rl status`; elsewhere, that the plugin is there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .launch import rl_command
from .project import find_project
from .roles import ENV_SESSION
from .util import RLError


def session_start(host: str, raw: str) -> dict:
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    rl = rl_command()
    cwd = Path(event.get("cwd") or ".").resolve()
    try:
        project = find_project(start=cwd)
    except RLError as exc:
        if exc.code != "NO_PROJECT":
            return _out(f"Smooth Research project found, but `{rl} status` fails: {exc.message}")
        return _out(f"Smooth Research is available (CLI: {rl}); use its skill to start or resume an ML research "
                    "project.")
    name = os.environ.get(ENV_SESSION)
    if name:
        return _out(f"Smooth Research project at {project.root} (CLI: {rl}). You are the session {name}. Run "
                    f"`{rl} pack` first and work from what it prints; `{rl} inbox` lists what waits on you. When "
                    "nothing waits on you, end your turn: the watcher wakes you.")
    from .store import Store
    from .watch import stale_note
    note = stale_note(Store(project, "hook"))
    return _out(f"Smooth Research project at {project.root} (CLI: {rl}). Use the smooth-research skill: "
                f"`{rl} status` shows the project; `{rl} resume` runs it (the coordinator's pane and the watcher)."
                + (f" Note: {note}." if note else "")
                + "".join(f"\n- configuration: {w}" for w in project.warnings))


def _out(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
