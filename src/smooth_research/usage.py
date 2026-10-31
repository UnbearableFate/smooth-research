"""Subscription usage of the Claude Code and Codex hosts, and when a host is low.

Both hosts run on subscriptions with a 5-hour and a weekly window. Codex writes its windows into its session
logs ($CODEX_HOME/sessions/**/rollout-*.jsonl, token_count events); Claude Code gives them only to a status line
command, so the Claude sessions rl starts (and the coordinator's, through the project's
.claude/settings.local.json) run `rl usage statusline`, which keeps the latest reading in
<rl home>/usage/claude.json. A reading is as fresh as the host's last turn on this machine; a window whose reset
time has passed counts as unused.

A host is low when one of its windows has usage.switch_left_percent (10) or less left, or
usage.coordinator_host_left_percent (20) on the host the coordinator runs on, which keeps working on it. Then
its researcher and reviewer sessions finish and another model takes over (the watcher tells them and the
coordinator, which runs rl agent switch), and new ones start on another entry of their role; advisors keep what
is left. pi reports no windows.
"""

from __future__ import annotations

import json
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

from .project import usage_percent
from .util import RLError, atomic_write, now_utc, parse_time

HOSTS = ("claude", "codex")
WINDOW_NAMES = {300: "5h", 10080: "week"}
TAIL_BYTES = 2 << 20  # the end of a Codex session log that is searched for its last reading
LOGS_SEARCHED = 5  # Codex session logs, newest first
OURS = "usage statusline"  # how rl's status line command is recognized in a settings file


def home() -> Path:
    """rl's own data directory, as the plugin's bin/rl names it."""
    if os.environ.get("SMOOTH_RESEARCH_HOME"):
        return Path(os.environ["SMOOTH_RESEARCH_HOME"]).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "smooth-research"


def claude_cache() -> Path:
    return home() / "usage" / "claude.json"


def codex_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _window(name: str, used, resets_at) -> dict | None:
    if not _number(used):
        return None
    return {"name": name, "used": float(used), "resets_at": resets_at if _number(resets_at) else None}


def claude_windows(rate_limits) -> list[dict]:
    """The windows in Claude Code's status line input: rate_limits.five_hour and .seven_day."""
    out = []
    for key, name in (("five_hour", "5h"), ("seven_day", "week")):
        w = rate_limits.get(key) if isinstance(rate_limits, dict) else None
        found = _window(name, w.get("used_percentage"), w.get("resets_at")) if isinstance(w, dict) else None
        out += [found] if found else []
    return out


def codex_windows(rate_limits) -> list[dict]:
    """The windows of a Codex token_count event: rate_limits.primary and .secondary."""
    out = []
    for key in ("primary", "secondary"):
        w = rate_limits.get(key) if isinstance(rate_limits, dict) else None
        if not isinstance(w, dict):
            continue
        minutes = w.get("window_minutes")
        name = WINDOW_NAMES.get(minutes) or (f"{minutes / 60:g}h" if _number(minutes) else key)
        found = _window(name, w.get("used_percent"), w.get("resets_at"))
        out += [found] if found else []
    return out


def left(window: dict, now: float | None = None) -> float:
    """Percent left in a window; one whose reset time has passed is unused again."""
    reset = window.get("resets_at")
    if reset and reset <= (time.time() if now is None else now):
        return 100.0
    return max(0.0, 100.0 - window["used"])


# -- readings -------------------------------------------------------------------------------------------------


def statusline(raw: str) -> str:
    """Claude Code's status line: keep the reading of its windows for rl and show the model and what is left.
    It never fails (a status line that errors shows nothing)."""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    windows = claude_windows(data.get("rate_limits"))
    held = reading("claude")
    if windows and not (held and held["windows"] == windows and _minutes_since(held["at"]) < 1):
        try:  # the status line runs after every message: an unchanged reading is written once a minute
            atomic_write(claude_cache(), json.dumps({"at": now_utc(), "windows": windows}).encode("utf-8"))
        except OSError:
            pass
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    parts = [str(model.get("display_name") or "")] + [f"{w['name']} {left(w):.0f}% left" for w in windows]
    return " · ".join(p for p in parts if p)


def statusline_setting(rl: str) -> dict:
    """The statusLine setting that runs rl's status line command."""
    return {"type": "command", "command": f"{shlex.quote(rl)} {OURS}"}


def reading(host: str) -> dict | None:
    """The latest reading of a host's windows, {"at", "windows", "source"}, or None."""
    if host == "codex":
        return _codex_reading()
    if host != "claude":
        return None
    try:
        data = json.loads(claude_cache().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    given = data.get("windows") if isinstance(data, dict) else None
    windows = [w for w in given or [] if isinstance(w, dict) and _number(w.get("used")) and w.get("name")]
    return {"at": data.get("at"), "windows": windows, "source": "the Claude status line"} if windows else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _codex_reading() -> dict | None:
    """The last rate limits Codex logged: in the session log written last (a session that runs across midnight
    keeps writing to the day it started in)."""
    logs = sorted(codex_sessions().glob("*/*/*/rollout-*.jsonl"), key=_mtime, reverse=True)
    for path in logs[:LOGS_SEARCHED]:
        try:
            with open(path, "rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(0, size - TAIL_BYTES))
                lines = log.read().split(b"\n")[1 if size > TAIL_BYTES else 0:]  # the first may be cut
        except OSError:
            continue
        for line in reversed(lines):
            if b'"token_count"' not in line or b'"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            windows = codex_windows(payload.get("rate_limits")) if isinstance(payload, dict) else []
            if windows:
                return {"at": event.get("timestamp"), "windows": windows, "source": str(path)}
    return None


# -- low hosts ------------------------------------------------------------------------------------------------


def hosts(store) -> dict[str, dict]:
    """Every host's reading, the percent left at which its sessions hand over, and why it is low (None when it
    is not): the narrowest window at or below that percent."""
    with store.read() as tx:
        coordinator = next((s["host"] for s in tx.all("Session", status="active") if s["role_id"] == "coordinator"),
                           None)
    now, out = time.time(), {}
    for host in HOSTS:
        found = reading(host)
        mine = host == coordinator
        limit = usage_percent(store.project.config, "coordinator_host_left_percent" if mine else "switch_left_percent")
        short = sorted((left(w, now), w) for w in (found or {}).get("windows", []) if left(w, now) <= limit)
        low = None
        if short:
            rest, w = short[0][0], short[0][1]
            low = {"host": host, "window": w["name"], "left": rest, "resets_at": w.get("resets_at"),
                   "key": f"{host} {w['name']} {w.get('resets_at')}",
                   "reason": f"the {host} subscription has {rest:.0f}% of its {w['name']} window left (its sessions "
                             f"hand over at {limit:g}%" + (", the coordinator's host" if mine else "") + ")"}
        out[host] = {"reading": found, "limit": limit, "coordinator": mine, "low": low}
    return out


def low_hosts(store) -> dict[str, dict]:
    """The hosts whose researcher and reviewer sessions hand over now, with why."""
    return {host: info["low"] for host, info in hosts(store).items() if info["low"]}


def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%m-%d %H:%M UTC")


def _minutes_since(at: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - parse_time(at)).total_seconds() / 60
    except RLError:
        return float("inf")


def _age(at: str | None) -> str:
    minutes = _minutes_since(at)
    if minutes == float("inf"):
        return "at an unknown time"
    return f"{minutes:.0f} min ago" if minutes < 120 else f"{minutes / 60:.1f} h ago"


def show(store) -> dict:
    """rl usage: each host's windows, how old the reading is, when its sessions hand over, and its sessions."""
    found = hosts(store)
    with store.read() as tx:
        sessions = [s for s in tx.all("Session", status="active") if s.get("role_kind")]
    lines = []
    for host, info in found.items():
        r, low = info["reading"], info["low"]
        if not r:
            where = ("Claude sessions that rl starts report it through their status line"
                     if host == "claude" else f"Codex has logged none in {codex_sessions()}")
            lines.append(f"{host}: no reading yet ({where})")
        else:
            windows = " · ".join(f"{w['name']} {left(w):.0f}% left"
                                 + (f" (resets {_when(w['resets_at'])})" if w.get("resets_at") else "")
                                 for w in r["windows"])
            lines.append(f"{host}: {windows}; read {_age(r['at'])}; sessions hand over at {info['limit']:g}% left"
                         + (" (the coordinator's host)" if info["coordinator"] else "")
                         + (": LOW, its researchers and reviewers hand over; advisors keep the rest" if low else ""))
        on = [f"{s['role_id']} ({s['role_kind']})" for s in sessions if s.get("host") == host]
        lines += [f"  sessions: {', '.join(on)}"] if on else []
    lines.append("pi: reports no windows")
    return {"hosts": found, "summary": "\n".join(lines)}


# -- the coordinator's own Claude session ---------------------------------------------------------------------


def _settings(path: Path) -> dict | None:
    """A Claude Code settings file ({} when there is none), or None when it does not parse."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def install_statusline(root: Path, rl: str) -> str | None:
    """Let the coordinator's Claude Code session in the project report its usage: the project's
    .claude/settings.local.json runs rl's status line, unless a status line of the user's own is set (it is
    left as it is). A note on what was done, or None when it was already in place."""
    user = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"
    local = root / ".claude" / "settings.local.json"
    for path in (user, root / ".claude" / "settings.json", local):
        data = _settings(path)
        if data is None:
            return f"{path} does not parse, so no status line was added there"
        line = data.get("statusLine")
        command = line.get("command") if isinstance(line, dict) else None
        if line and not (isinstance(command, str) and OURS in command):
            return (f"the status line set in {path} is the user's own, so this session does not report Claude's usage "
                    f"(it may run `{rl} {OURS}` with its input); the sessions rl starts report it")
    data = _settings(local)
    if data.get("statusLine") == statusline_setting(rl):
        return None
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        if not local.exists() and not (local.parent / ".gitignore").exists():
            (local.parent / ".gitignore").write_text("settings.local.json\n", encoding="utf-8")
        atomic_write(local, (json.dumps({**data, "statusLine": statusline_setting(rl)}, indent=2) + "\n").encode())
    except OSError as exc:
        return f"no status line was added to {local}: {exc}"
    return f"{local} runs rl's status line, which reports Claude's usage (rl usage)"
