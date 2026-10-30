"""Host hook handlers shared by Claude Code and Codex (both pass JSON on stdin and read hookSpecificOutput).

Only a light resume hint at session start: key record checks live in the CLI, not in hooks.
"""

from __future__ import annotations

import json
from pathlib import Path

from .delegate import rl_command
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
    except RLError:
        text = f"Smooth Research is available (CLI: {rl}); use its skill to start or resume an ML research project."
        return _out(text)
    store = Store(project, "hook")
    try:
        with store.read() as tx:
            meta = tx.meta()
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
             f"Coordinator epoch {meta['coordinator_epoch']}"
             + (f", held by {coord[-1]['id']} ({coord[-1]['host']})" if coord else ", no active coordinator")
             + f". To coordinate from this session: {register}",
             f"Open tasks {len(tasks)}, unfinished attempts {len(attempts)}. Read `{rl} resume` before acting."]
    lines += [f"- {brief(t)}" for t in tasks[:5]]
    lines += [f"- attention: {i}" for i in items[:5]]
    return _out("\n".join(lines))


def _out(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
