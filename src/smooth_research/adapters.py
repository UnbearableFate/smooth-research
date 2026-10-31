"""Thin host adapters for model calls: Claude Code (`claude -p`), Codex (`codex exec`), pi (`pi -p`) and manual
submission.

They run reviewer, advisor and delegated research sessions only; experiments are never executed through them.
Each call reports the requested model, the observed model when the host exposes it (otherwise unverified),
the exact session id, usage and cost as the host reports them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import RLError

ENTRY_RE = re.compile(r"^(?P<entry>claude|codex|pi|manual):(?P<model>[^@\s]+)(?:@(?P<effort>[a-z]+))?$")
EXECUTABLES = {"claude": "claude", "codex": "codex", "pi": "pi"}
JSON_BLOCK_RE = re.compile(r"```json[ \t]*\n(.*?)```", re.S)


def family(model: str | None) -> str | None:
    """Model family by name; None when it cannot be told (then cross-model status stays unverified)."""
    if not model:
        return None
    m = model.lower().split("/", 1)[-1] if model.lower().startswith(("anthropic/", "openai/")) else model.lower()
    if model.lower().startswith("anthropic/") or m.startswith("claude") or m in ("opus", "sonnet", "haiku", "fable"):
        return "anthropic"
    if model.lower().startswith("openai/") or re.match(r"^(gpt|o[1-9]|codex|chatgpt)", m):
        return "openai"
    return None


@dataclass(frozen=True)
class Entry:
    entry: str
    model: str
    effort: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Entry":
        m = ENTRY_RE.match(text.strip())
        if not m:
            raise RLError("INVALID", f"model entry {text!r} must be <claude|codex|pi|manual>:<model>[@effort]")
        return cls(m["entry"], m["model"], m["effort"])

    @property
    def family(self) -> str | None:
        return family(self.model) or {"claude": "anthropic", "codex": "openai"}.get(self.entry)

    def __str__(self) -> str:
        return f"{self.entry}:{self.model}" + (f"@{self.effort}" if self.effort else "")


@dataclass
class Call:
    entry: str
    requested_model: str
    ok: bool = False
    text: str = ""
    observed_model: str | None = None
    provider: str | None = None
    session_id: str | None = None
    usage: dict | None = None
    cost_usd: float | None = None
    error: str | None = None
    raw_log: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = asdict(self)
        out.pop("text")
        return out


def available(entry: Entry) -> str | None:
    """Why this entry cannot be called here, or None."""
    if entry.entry == "manual":
        return "manual entries are submitted by a person"
    exe = EXECUTABLES[entry.entry]
    return None if shutil.which(exe) else f"{exe} CLI is not on PATH"


def _run(cmd: list[str], prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        return subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RLError("UNAVAILABLE", f"{cmd[0]} did not answer within {timeout}s") from None
    except OSError as exc:
        raise RLError("UNAVAILABLE", f"{cmd[0]}: {exc}") from None


def call_claude(entry: Entry, prompt: str, cwd: Path, read_only: bool, timeout: int,
                add_dirs: list[str] = (), log: Path | None = None) -> Call:
    session = str(uuid.uuid4())
    cmd = ["claude", "-p", "--output-format", "json", "--model", entry.model, "--session-id", session]
    if entry.effort:
        cmd += ["--effort", entry.effort]
    if read_only:
        # an independent reader: no user customizations, hooks, MCP servers or command tools
        cmd += ["--safe-mode", "--restricted", "--strict-mcp-config", "--tools", "Read,Grep,Glob",
                "--permission-mode", "dontAsk"]
    else:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", "Bash"]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    out = _run(cmd, prompt, cwd, timeout)
    if log:
        log.write_text(out.stdout + ("\n--- stderr ---\n" + out.stderr if out.stderr else ""), encoding="utf-8")
    result = Call("claude", entry.model, provider="anthropic", session_id=session, raw_log=str(log) if log else None)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        result.error = f"no JSON result (exit {out.returncode}): {(out.stderr or out.stdout).strip()[:300]}"
        return result
    models = list((data.get("modelUsage") or {}).keys())
    main = [m for m in models if entry.model in m] or models
    result.text = data.get("result") or ""
    result.session_id = data.get("session_id") or session
    result.usage = data.get("usage")
    result.cost_usd = data.get("total_cost_usd")
    result.observed_model = main[0] if len(main) == 1 else None
    result.extra = {"observed_models": models, "terminal_reason": data.get("terminal_reason")}
    result.ok = data.get("is_error") is False and bool(result.text)
    if not result.ok:
        result.error = f"is_error={data.get('is_error')} {str(data.get('result'))[:300]}"
    return result


def _codex_rollout_model(thread_id: str) -> tuple[str | None, str | None]:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    for path in sorted((home / "sessions").glob(f"*/*/*/rollout-*{thread_id}.jsonl"), reverse=True):
        try:
            for line in path.open(encoding="utf-8"):
                event = json.loads(line)
                if event.get("type") == "turn_context":
                    payload = event.get("payload") or {}
                    return payload.get("model"), payload.get("effort")
        except (OSError, json.JSONDecodeError):
            continue
    return None, None


def codex_write_sandbox() -> str:
    """Writable sessions keep workspace-write unless the user chose full access for Codex themselves
    (some hosts cannot run the bubblewrap sandbox at all)."""
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        with config.open("rb") as source:
            if tomllib.load(source).get("sandbox_mode") == "danger-full-access":
                return "danger-full-access"
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return "workspace-write"


def call_codex(entry: Entry, prompt: str, cwd: Path, read_only: bool, timeout: int,
               add_dirs: list[str] = (), log: Path | None = None) -> Call:
    with tempfile.TemporaryDirectory(prefix="rl-codex-") as tmp:
        last = Path(tmp) / "last.txt"
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--model", entry.model, "--cd", str(cwd),
               "--sandbox", "read-only" if read_only else codex_write_sandbox(), "-c", 'approval_policy="never"',
               "--output-last-message", str(last)]
        if entry.effort:
            cmd += ["-c", f'model_reasoning_effort="{entry.effort}"']
        for d in add_dirs:
            cmd += ["--add-dir", d]
        out = _run(cmd + ["-"], prompt, cwd, timeout)
        final = last.read_text(encoding="utf-8") if last.exists() else ""
    if log:
        log.write_text(out.stdout + ("\n--- stderr ---\n" + out.stderr if out.stderr else ""), encoding="utf-8")
    result = Call("codex", entry.model, provider="openai", raw_log=str(log) if log else None)
    texts, errors = [], []
    for line in out.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            result.session_id = event.get("thread_id")
        elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            texts.append(event["item"].get("text") or "")
        elif kind == "turn.completed":
            result.usage = event.get("usage")
        elif kind in ("turn.failed", "error"):
            errors.append(str(event.get("error") or event.get("message") or event)[:300])
    result.text = final or (texts[-1] if texts else "")
    if result.session_id:
        model, effort = _codex_rollout_model(result.session_id)
        result.observed_model = model
        result.extra = {"observed_effort": effort, "observed_from": "codex rollout turn_context" if model else None}
    result.ok = out.returncode == 0 and not errors and bool(result.text)
    if not result.ok:
        result.error = "; ".join(errors) or f"exit {out.returncode}: {out.stderr.strip()[-300:]}"
    return result


def call_pi(entry: Entry, prompt: str, cwd: Path, read_only: bool, timeout: int,
            add_dirs: list[str] = (), log: Path | None = None) -> Call:
    """`pi -p --mode json`: the first line is the session header, then agent events; the answer is the text of
    the last assistant message, and its model and provider are what pi reports for that message."""
    session = str(uuid.uuid4())
    cmd = ["pi", "-p", "--mode", "json", "--model", entry.model, "--session-id", session]
    if entry.effort:
        cmd += ["--thinking", entry.effort]
    if read_only:
        cmd += ["--tools", "read"]
    out = _run(cmd, prompt, cwd, timeout)
    if log:
        log.write_text(out.stdout + ("\n--- stderr ---\n" + out.stderr if out.stderr else ""), encoding="utf-8")
    result = Call("pi", entry.model, session_id=session, raw_log=str(log) if log else None)
    texts, models, errors, cost = [], [], [], 0.0
    for line in out.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") or {}
        if event.get("type") == "session":
            result.session_id = event.get("id") or session
        elif event.get("type") == "message_end" and message.get("role") == "assistant":
            if message.get("stopReason") in ("error", "aborted"):
                errors.append(str(message.get("errorMessage") or message["stopReason"])[:300])
            texts.append("".join(c.get("text") or "" for c in message.get("content") or [] if c.get("type") == "text"))
            models.append(message.get("responseModel") or message.get("model"))
            result.provider = message.get("provider") or result.provider
            result.usage = message.get("usage") or result.usage
            cost += float(((message.get("usage") or {}).get("cost") or {}).get("total") or 0)
    seen = sorted(set(filter(None, models)))
    result.text = next((t for t in reversed(texts) if t.strip()), "")
    result.observed_model = seen[0] if len(seen) == 1 else None
    result.cost_usd = cost or None
    result.extra = {"observed_models": seen}
    result.ok = out.returncode == 0 and not errors and bool(result.text)
    if not result.ok:
        result.error = "; ".join(errors) or f"exit {out.returncode}: {(out.stderr or out.stdout).strip()[-300:]}"
    return result


def call(entry: Entry, prompt: str, cwd: Path, read_only: bool = True, timeout: int = 1800,
         add_dirs: list[str] = (), log: Path | None = None) -> Call:
    problem = available(entry)
    if problem:
        return Call(entry.entry, entry.model, error=problem)
    fn = {"claude": call_claude, "codex": call_codex, "pi": call_pi}[entry.entry]
    try:
        return fn(entry, prompt, cwd, read_only, timeout, list(add_dirs), log)
    except RLError as exc:
        return Call(entry.entry, entry.model, error=exc.message)


def last_json_block(text: str) -> dict | None:
    blocks = JSON_BLOCK_RE.findall(text or "")
    for block in reversed(blocks):
        try:
            value = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None
