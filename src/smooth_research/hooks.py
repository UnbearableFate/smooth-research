"""Host hook handlers shared by Claude Code and Codex (both pass JSON on stdin and read hookSpecificOutput).

Only a light resume hint at session start: key record checks live in the CLI, not in hooks.
"""

from __future__ import annotations

import json
from pathlib import Path

from .delegate import rl_command
from .herdr import caller_role
from .project import find_project
from .research import attention, brief
from .store import Store
from .tasks import OPEN_STATES
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
        if exc.code != "NO_PROJECT":  # e.g. a configuration file that does not load
            return _out(f"Smooth Research project found, but `{rl} status` fails: {exc.message}")
        text = f"Smooth Research is available (CLI: {rl}); use its skill to start or resume an ML research project."
        return _out(text)
    if caller_role():  # a session that `rl agent open` started: its brief, not the coordinator's start
        return _out(f"Smooth Research project at {project.root} (CLI: {rl}). You are {caller_role()}, a session "
                    f"the coordinator opened; you are not the coordinator. Your work: `{rl} agent brief "
                    f"{caller_role()}` (wait for an assignment if it names none).")
    store = Store(project, "hook")
    try:
        with store.read() as tx:
            coord = [s for s in tx.all("Session", status="active") if s["role_id"] == "coordinator"]
            tasks = [t for t in tx.all("Task") if t["state"] in OPEN_STATES]
            attempts = [a for a in tx.all("Attempt") if a["execution_state"] != "ended" or a.get("needs_check")]
            items = attention(tx)
    except RLError as exc:
        return _out(f"Smooth Research project at {project.root}, but its records could not be read: {exc.message}")
    session_id, model = event.get("session_id"), event.get("model")
    model = model.get("id") if isinstance(model, dict) else model
    register = (f"{rl} session start --role coordinator --host {host}"
                + (f" --host-session-id {session_id}" if session_id else "")
                + (f" --model-observed {model}" if model else ""))
    lines = [f"Smooth Research project at {project.root} (CLI: {rl}). Use the smooth-research skill.",
             (f"Coordinator session {coord[-1]['id']} ({coord[-1]['host']})" if coord else "No active coordinator")
             + f". To coordinate from this session: {register}",
             f"Open tasks {len(tasks)}, unfinished attempts {len(attempts)}. Read `{rl} resume` before acting."]
    lines += [f"- {brief(t)}" for t in tasks[:5]]
    lines += [f"- attention: {i}" for i in items[:5]]
    lines += [f"- configuration: {w}" for w in project.warnings]
    return _out("\n".join(lines))


def _out(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
