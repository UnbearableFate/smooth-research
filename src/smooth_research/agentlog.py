"""What an agent session's own log says about its current turn.

herdr reports the host's session id of each agent (agent get: agent_session.value). For Codex that id names the
session log, $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl: one JSON event per line, appended while
the session runs. The watcher reads only the bytes appended since its last look (the path and the offset are kept
in Session.watch) and keeps the lifecycle of the current turn: when it started (task_started), whether it ended
(task_complete, or turn_aborted when the user interrupted it) and with which error (task_complete.error,
codex_error_info), which tool calls are still pending (a function or custom tool call without its output yet),
and when the session last made progress. Progress is a turn event, a tool call or its output, a reply or a
reasoning step; settings, token counts, context and the prompts typed into the session are not (a prompt the
watcher typed must not look like work). Retries inside a turn are not errors: only the turn's end counts.

A Claude Code session's log (<claude config>/projects/<folder>/<id>.jsonl) is not read: there is no sample of
its turn errors yet, so no rule for them. Claude Code and pi sessions are seen through herdr only, and so is a
session without a known id: its log is not guessed (from the folder, say).
"""

from __future__ import annotations

import json
from pathlib import Path

from .usage import codex_sessions

TRANSIENT = ("server_overloaded",)  # the service failed the turn; the same turn may succeed later
QUOTA = ("usage_limit_exceeded",)  # the subscription is used up: another model continues the work
TOOL_CALLS = {"function_call": "function_call_output", "custom_tool_call": "custom_tool_call_output"}
NOT_PROGRESS_TYPES = {"session_meta", "turn_context", "world_state", "token_usage_record",
                      "inter_agent_communication_metadata"}
NOT_PROGRESS_EVENTS = {"token_count", "thread_settings_applied", "thread_goal_updated"}
NOT_PROGRESS_ITEMS = {"additional_tools", "configuration_update"}
PROMPT_ROLES = ("user", "developer", "system")


def locate(sid: str | None) -> Path | None:
    """The Codex session log of a session id, or None (no id, or not found)."""
    if not sid or "/" in sid or "*" in sid:
        return None
    found = sorted(codex_sessions().glob(f"*/*/*/rollout-*-{sid}.jsonl"))
    return found[-1] if found else None


def follow(host: str, sid: str | None, memo: dict | None) -> dict | None:
    """The log of the session id, brought up to date from what was kept (memo, from an earlier look): {"id",
    "path", "inode", "offset", "turn", "calls", "progress_at"}, or None for a host whose log rl does not read or
    when no id is known. A log that is not found (yet) gives "path" None. A new id starts over: what the old log
    said is not carried into the new one."""
    if host != "codex" or not sid:
        return None
    memo = dict(memo) if memo and memo.get("id") == sid else {"id": sid}
    path = Path(memo["path"]) if memo.get("path") else None
    try:
        st = path.stat() if path else None
    except OSError:
        st = None
    if st is None:  # not looked for yet, or moved
        path = locate(sid)
        st = path.stat() if path else None
    if path is None or st is None:
        return {"id": sid, "path": None}
    if str(path) != memo.get("path") or st.st_ino != memo.get("inode") or st.st_size < memo.get("offset", 0):
        memo = {"id": sid}  # replaced or cut: read it again from the start
    memo.update(path=str(path), inode=st.st_ino)
    offset = memo.get("offset", 0)
    if st.st_size > offset:
        try:
            with open(path, "rb") as log:
                log.seek(offset)
                data = log.read()
        except OSError:
            return memo
        end = data.rfind(b"\n")  # a line still being written is read at the next look
        if end >= 0:
            for line in data[:end].split(b"\n"):
                _apply(memo, line)
            memo["offset"] = offset + end + 1
    return memo


def _apply(memo: dict, line: bytes) -> None:
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    kind, at = event.get("type"), event.get("timestamp")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    what = payload.get("type")
    if kind == "event_msg" and what == "task_started":
        memo["turn"] = {"id": payload.get("turn_id"), "started": at, "ended": None, "error": None,
                        "aborted": None, "progress": 0}
        memo["calls"], memo["progress_at"] = {}, at
        return
    turn = memo.get("turn")
    if kind == "event_msg" and what in ("task_complete", "turn_aborted"):
        if turn and turn.get("id") not in (None, payload.get("turn_id")):
            return  # the end of another turn
        turn = turn or {"id": payload.get("turn_id"), "started": None, "progress": 0}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else None
        memo["turn"] = {**turn, "ended": at,
                        "aborted": (payload.get("reason") or "interrupted") if what == "turn_aborted" else None,
                        "error": {"kind": str(error.get("codex_error_info") or "unknown"),
                                  "message": str(error.get("message") or "")[:300]} if error else None}
        memo["calls"] = {}
        return
    if not _progress(kind, what, payload):
        return
    memo["progress_at"] = at
    if turn and not turn.get("ended"):
        turn["progress"] = turn.get("progress", 0) + 1
    calls = memo.setdefault("calls", {})
    if kind == "response_item" and what in TOOL_CALLS and payload.get("call_id"):
        calls[payload["call_id"]] = {"at": at, "name": str(payload.get("name") or what)}
    elif kind == "response_item" and what in TOOL_CALLS.values():
        calls.pop(payload.get("call_id"), None)


def _progress(kind, what, payload: dict) -> bool:
    if kind in NOT_PROGRESS_TYPES:
        return False
    if kind == "event_msg" and what in NOT_PROGRESS_EVENTS:
        return False
    if kind == "response_item" and (what in NOT_PROGRESS_ITEMS
                                    or (what == "message" and payload.get("role") in PROMPT_ROLES)):
        return False
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    return not (kind == "event_msg" and what == "item_completed" and item.get("type") == "UserMessage")


def turn_view(log: dict | None) -> dict | None:
    """The current turn as the log tells it: {"state": open|ended|error|aborted, "error", "pending", "progress_at",
    "turn", "started"}; None when the log tells nothing (no Codex log, or no turn in it yet)."""
    if not log or not log.get("turn"):
        return None
    turn = log["turn"]
    state = ("error" if turn.get("error") else "aborted" if turn.get("aborted") else "ended") if turn.get("ended") \
        else "open"
    return {"state": state, "error": turn.get("error"), "pending": dict(log.get("calls") or {}),
            "progress_at": log.get("progress_at"), "turn": turn.get("id"), "started": turn.get("started"),
            "worked": turn.get("progress", 0) > 0}
