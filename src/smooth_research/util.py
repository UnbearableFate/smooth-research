"""Small shared helpers: errors, time, digests, canonical JSON, atomic files and token estimates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIGEST_ALGO = "blake2b"
REF_RE = re.compile(r"^(?P<id>[A-Z]+-[0-9]+(?:-[0-9]+)?)(?:@(?P<rev>[0-9]+))?$")


class RLError(Exception):
    """A refusal or failure the caller can act on; code is stable, message explains the next step."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    """ISO-8601 with optional Z; naive times are taken as UTC so ordering never mixes clocks silently."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise RLError("INVALID", f"not an ISO-8601 time: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def digest_bytes(data: bytes) -> str:
    return f"{DIGEST_ALGO}:{hashlib.blake2b(data, digest_size=32).hexdigest()}"


def digest_file(path: Path) -> str:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            h.update(chunk)
    return f"{DIGEST_ALGO}:{h.hexdigest()}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def short(digest: str, n: int = 12) -> str:
    return digest.split(":", 1)[-1][:n]


def atomic_write(path: Path, data: bytes) -> None:
    """Write-then-rename in the same directory, so readers see the old or the new file, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def estimate_tokens(text: str) -> int:
    """Rough estimate, labelled as such wherever it is shown: ASCII chars / 4 plus one per other character."""
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars)


TOKEN_METHOD = "estimate: ascii_chars/4 + non_ascii_chars"


def parse_ref(ref: str) -> tuple[str, int | None]:
    m = REF_RE.match(ref.strip())
    if not m:
        raise RLError("INVALID", f"not a record reference: {ref!r} (expected e.g. H-0001 or DOC-0003@2)")
    return m["id"], int(m["rev"]) if m["rev"] else None


def split_list(value: str | list[str] | None) -> list[str]:
    """Comma-separated CLI lists; repeated flags arrive as lists."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [part.strip() for item in items for part in item.split(",") if part.strip()]


def _json_text(value: str) -> str:
    try:
        if value == "-":
            import sys
            return sys.stdin.read()
        if value.startswith("@") or (not value.lstrip().startswith(("{", "[")) and Path(value).is_file()):
            return Path(value.removeprefix("@")).read_text(encoding="utf-8")
        return value
    except OSError as exc:
        raise RLError("INVALID", f"cannot read {value!r}: {exc}") from None


def load_json_arg(value: str | None) -> Any:
    """A JSON argument is inline JSON, @file, or - for stdin."""
    if value is None:
        return None
    try:
        return json.loads(_json_text(value))
    except json.JSONDecodeError as exc:
        raise RLError("INVALID", f"invalid JSON in {value[:60]!r}: {exc}") from None


def load_json_records(value: str) -> list[dict]:
    """Records handed over by another tool: one JSON object, a JSON array, or JSON Lines; inline, @file, path or -."""
    text = _json_text(value)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise RLError("INVALID", f"{value[:60]!r} is neither JSON nor JSON Lines: {exc}") from None
    data = data if isinstance(data, list) else [data]
    if not data or not all(isinstance(item, dict) for item in data):
        raise RLError("INVALID", f"{value[:60]!r} holds no records, or a record that is not a JSON object")
    return data
