"""Host hook handlers shared by Claude Code and Codex (both pass JSON on stdin and read hookSpecificOutput).

Only a light resume hint at session start: key record checks live in the CLI, not in hooks.
"""

from __future__ import annotations

import json
from pathlib import Path

from .compute import LOGIN_POLICY
from .context import open_work
from .delegate import rl_command
from .herdr import caller_role
from .project import coordinator_is_login, find_project
from .research import brief
from .store import Store
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
    policy = f"\n{LOGIN_POLICY}" if coordinator_is_login(project.config) else ""
    if caller_role():  # a session that `rl agent open` started: its brief, not the coordinator's start
        return _out(f"Smooth Research project at {project.root} (CLI: {rl}). You are {caller_role()}, a session "
                    f"the coordinator opened; you are not the coordinator. Your work: `{rl} agent brief "
                    f"{caller_role()}` (wait for an assignment if it names none).{policy}")
    store = Store(project, "hook")
    try:
        with store.read() as tx:  # the first open by a newer plugin upgrades the records (upgrade.py)
            work = open_work(tx, project)
    except RLError as exc:
        return _out(f"Smooth Research project at {project.root}, but its records could not be read: {exc.message}")
    session_id, model = event.get("session_id"), event.get("model")
    model = model.get("id") if isinstance(model, dict) else model
    register = (f"{rl} session start --role coordinator --host {host}"
                + (f" --host-session-id {session_id}" if session_id else "")
                + (f" --model-observed {model}" if model else ""))
    c = work["coordinator"]
    lines = [f"Smooth Research project at {project.root} (CLI: {rl}). Use the smooth-research skill.",
             (f"Coordinator session {c['id']} ({c['host']})" if c else "No active coordinator")
             + f". To coordinate from this session: {register}",
             f"Open tasks {len(work['tasks'])}, unfinished attempts {len(work['attempts'])}. Read `{rl} resume` "
             "before acting."]
    if store.newer:
        lines.append(f"This project was written by a newer smooth-research (record format {store.newer}): it can "
                     "be read, but nothing can be recorded until the plugin is updated. Tell the user.")
    elif store.upgraded:
        lines.append(f"Its records were upgraded from format {store.upgraded['from']} to {store.upgraded['to']} for "
                     f"this plugin version (a copy of the old database: {store.upgraded['backup']}).")
    lines += [f"- {brief(t)}" for t in work["tasks"][:5]]
    lines += [f"- attention: {i}" for i in work["attention"][:5]]
    lines += [f"- configuration: {w}" for w in project.warnings]
    return _out("\n".join(lines) + policy)


def _out(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
