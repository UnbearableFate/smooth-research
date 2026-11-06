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

ENV_SESSION, ENV_ROLE = "SMOOTH_RESEARCH_SESSION", "SMOOTH_RESEARCH_ROLE"
ENV_HEADLESS = "SMOOTH_RESEARCH_HEADLESS"  # set in the headless sessions rl starts (adapters)
SUBMIT_MS = 20_000  # how long a prompt waits for the agent to show it took it


def caller_role() -> str | None:
    """The session name of the caller when it runs in a session that `rl agent open` started."""
    return os.environ.get(ENV_ROLE) or None


def inside() -> bool:
    """Whether the caller runs in a herdr pane. A headless session that rl started from one inherits HERDR_ENV but
    has no pane of its own."""
    return os.environ.get("HERDR_ENV") == "1" and not os.environ.get(ENV_HEADLESS)


def problem() -> str | None:
    """Why herdr cannot be used from here, or None."""
    if not shutil.which("herdr"):
        return "herdr is not installed (not on PATH)"
    if os.environ.get("HERDR_ENV") != "1":
        return "this session does not run inside a herdr pane (HERDR_ENV is not 1)"
    return None


def new_tab(*args: str) -> dict:
    """herdr tab create in the caller's workspace (that of the coordinator, or of the watcher it started): without
    --workspace herdr uses the workspace the user's screen shows, which may be another project's."""
    here = os.environ.get("HERDR_WORKSPACE_ID")
    return call("tab", "create", *(["--workspace", here] if here else []), *args)


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


def submit(target: str, text: str, timeout_ms: int = SUBMIT_MS) -> tuple[str, str | None]:
    """Type a prompt (not a slash command) and wait until the agent shows it took it: ("delivered", None) when herdr
    saw it working or at a dialog afterwards; ("refused", why) when nothing was typed (a dialog was open, or no
    agent answers); ("unconfirmed", why) when it was typed but herdr saw no reaction (stalled, timed out, another
    error). An agent that was already working matches at once: whoever needs more checks its log."""
    try:
        call("agent", "prompt", target, text, "--wait", "--until", "working", "--until", "blocked", "--timeout",
             str(timeout_ms), timeout=timeout_ms // 1000 + 30)
        return "delivered", None
    except RLError as exc:
        code = exc.details.get("herdr_code")
        if code == "agent_blocked":
            return "refused", (f"{target} waits at a dialog (an approval or a question), so nothing was typed; the "
                               f"coordinator decides it (`rl agent dialog {target}`)")
        if code == "agent_not_found":
            return "refused", f"no agent answers to {target}"
        if code == "agent_prompt_stalled":
            return "unconfirmed", "herdr saw no working state after it"
        if code == "timeout":
            return "unconfirmed", f"herdr saw no working state within {timeout_ms // 1000}s"
        return "unconfirmed", exc.message


def prompt(target: str, text: str, wait_ms: int | None = None) -> str | None:
    """Type text into a live agent's input without waiting for it to react (a notice, or a slash command, which
    shows no working state); None when it was submitted, else why not."""
    try:
        call("agent", "prompt", target, text, *(["--wait", "--timeout", str(wait_ms)] if wait_ms else []),
             timeout=(wait_ms // 1000 + 30) if wait_ms else 30)
        return None
    except RLError as exc:
        code = exc.details.get("herdr_code")
        if code == "agent_prompt_stalled":
            return None  # submitted; a slash command shows no working state
        if code == "agent_blocked":
            return f"{target} waits at a dialog (an approval or a question), so nothing was typed; the coordinator " \
                   f"decides it (`rl agent dialog {target}`)"
        return exc.message


def integration(kind: str) -> bool | None:
    """Is herdr's integration for this agent kind installed (`herdr integration status`, a line such as
    `codex: current (v10) (<hook path>)`)? None when herdr does not say."""
    try:
        out = subprocess.run(["herdr", "integration", "status"], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in out.stdout.splitlines() if out.returncode == 0 else ():
        name, _, state = line.partition(":")
        if name.strip() == kind:
            return not state.strip().startswith("not installed")
    return None


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
    """A live agent as herdr sees it (agent_status, state_change_seq, agent_session: the host's session id in
    .value, pane_id, ...); None when no agent answers to target (it exited, or its tab was closed)."""
    try:
        return call("agent", "get", target, timeout=15).get("agent") or {}
    except RLError as exc:
        if exc.details.get("herdr_code") == "agent_not_found":
            return None
        raise


def session_id(target: str) -> str | None:
    """The host's session id herdr reports for a live agent, or None."""
    try:
        found = agent(target) or {}
    except RLError:
        return None
    given = found.get("agent_session")
    return str(given["value"]) if isinstance(given, dict) and given.get("value") else None


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
