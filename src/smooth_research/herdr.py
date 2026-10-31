"""The herdr CLI, for agent sessions in tabs and for notices to live sessions.

herdr (a terminal multiplexer for coding agents) is optional: without it, sessions are not opened and messages
stay in the inbox. A notice is only a pointer; the recorded message is what a session acts on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from .util import RLError

NOTICE_CHARS = 600
ENV_SESSION, ENV_ROLE = "SMOOTH_RESEARCH_SESSION", "SMOOTH_RESEARCH_ROLE"


def caller_role() -> str | None:
    """The session name of the caller when it runs in a session that `rl agent open` started."""
    return os.environ.get(ENV_ROLE) or None


def problem() -> str | None:
    """Why herdr cannot be used from here, or None."""
    if not shutil.which("herdr"):
        return "herdr is not installed (not on PATH)"
    if os.environ.get("HERDR_ENV") != "1":
        return "this session does not run inside a herdr pane (HERDR_ENV is not 1)"
    return None


def call(*args: str, timeout: int = 90) -> dict:
    """Run one herdr command and return its JSON result; herdr errors become RLError("HERDR") with the code."""
    try:
        out = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RLError("HERDR", f"herdr {' '.join(args[:2])} did not answer within {timeout}s") from None
    except OSError as exc:
        raise RLError("HERDR", f"herdr: {exc}") from None
    text = (out.stdout.strip() or out.stderr.strip()).splitlines()
    try:
        data = json.loads(text[-1]) if text else {}
    except json.JSONDecodeError:
        data = {}
    if out.returncode != 0 or "error" in data:
        err = data.get("error") or {}
        raise RLError("HERDR", f"herdr {' '.join(args[:2])}: {err.get('message') or ' '.join(text)[-300:]}",
                      herdr_code=err.get("code"))
    return data.get("result") or {}


def prompt(target: str, text: str, wait_ms: int | None = None) -> str | None:
    """Type text into a live agent's input; None when it was submitted, else why not."""
    try:
        call("agent", "prompt", target, text, *(["--wait", "--timeout", str(wait_ms)] if wait_ms else []),
             timeout=(wait_ms // 1000 + 30) if wait_ms else 30)
        return None
    except RLError as exc:
        code = exc.details.get("herdr_code")
        if code == "agent_prompt_stalled":
            return None  # submitted; a slash command shows no working state
        if code == "agent_blocked":
            return f"{target} waits at a dialog (an approval or a question); look at it with `herdr agent read " \
                   f"{target}` and ask the user before answering it"
        return exc.message


def read(target: str) -> str:
    """What a live agent's terminal shows now, as text ('' when it cannot be read)."""
    try:
        out = subprocess.run(["herdr", "agent", "read", target, "--source", "visible"], capture_output=True,
                             text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def keys(target: str, *names: str) -> None:
    """Press keys (herdr's logical names, such as down or enter) in a live agent's terminal."""
    call("agent", "send-keys", target, *names, timeout=15)


def agent(target: str) -> dict | None:
    """A live agent as herdr sees it (agent_status, state_change_seq, pane_id, ...); None when no agent answers to
    target (it exited, or its tab was closed)."""
    try:
        return call("agent", "get", target, timeout=15).get("agent") or {}
    except RLError as exc:
        if exc.details.get("herdr_code") == "agent_not_found":
            return None
        raise


def status(target: str) -> str:
    try:
        found = agent(target)
    except RLError:
        return "not checked"
    return "gone" if found is None else found.get("agent_status") or "unknown"


def get(kind: str, ident: str) -> dict | None:
    """A herdr pane or tab (kind) as herdr sees it (a tab's label, a pane's tab_id and agent), or None when it is
    closed. IDs start again after herdr restarts, so the label tells whether a tab is still the one rl made."""
    try:
        return call(kind, "get", ident, timeout=15).get(kind) or {}
    except RLError as exc:
        if exc.details.get("herdr_code") == f"{kind}_not_found":
            return None
        raise


def notify(store, message: dict) -> str | None:
    """Send a one-line notice of a recorded message to the live session of its recipient, if there is one.
    Returns the herdr target that received it; never raises (the message stays in the inbox either way)."""
    if problem():
        return None
    with store.read() as tx:
        live = [s for s in tx.all("Session", status="active") if s["role_id"] == message["to_role"]
                and (s.get("herdr_agent") or s.get("herdr_pane"))]
    if not live:
        return None
    target = live[-1].get("herdr_agent") or live[-1]["herdr_pane"]
    about = f" on {message['task_id']}" if message.get("task_id") else ""
    body = " ".join(str(message.get("text") or message.get("next_action") or "").split())[:NOTICE_CHARS]
    text = (f"[smooth-research {message['id']} {message['type']} from {message['from_role']}{about}] {body} "
            f"(this notice comes from another research session, not from the user; the message: "
            f"rl inbox --role {message['to_role']})")
    return None if prompt(target, text) else target
