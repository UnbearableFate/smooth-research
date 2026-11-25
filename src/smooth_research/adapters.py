"""Model entries: which agent (Claude Code, Codex, pi, or a person for manual) and which model a role runs on.

Every session is an interactive agent in a herdr pane (watch.py); rl calls no model itself (design 11.2).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .util import RLError

ENTRY_RE = re.compile(r"^(?P<entry>claude|codex|pi|manual):(?P<model>[^@\s]+)(?:@(?P<effort>[a-z]+))?$")
EXECUTABLES = {"claude": "claude", "codex": "codex", "pi": "pi"}


FAMILY_NAMES = (("anthropic", r"^(claude|opus|sonnet|haiku|fable)"), ("openai", r"^(gpt|o[1-9]|codex|chatgpt)"),
                ("deepseek", r"^deepseek"), ("google", r"^(gemini|gemma)"), ("alibaba", r"^(qwen|qwq)"),
                ("moonshot", r"^(kimi|moonshot)"), ("zhipu", r"^(glm|chatglm)"), ("xai", r"^grok"),
                ("mistral", r"^(mistral|mixtral|codestral|magistral|devstral)"), ("meta", r"^llama"))
FAMILY_PROVIDERS = {"anthropic": "anthropic", "openai": "openai", "deepseek": "deepseek", "google": "google",
                    "gemini": "google", "qwen": "alibaba", "alibaba": "alibaba", "dashscope": "alibaba",
                    "moonshot": "moonshot", "kimi": "moonshot", "zhipu": "zhipu", "zai": "zhipu", "xai": "xai",
                    "mistral": "mistral", "meta": "meta"}


def family(model: str | None) -> str | None:
    """Model family by name (a provider prefix such as pi's openai/ or deepseek/ counts too); None when it
    cannot be told (then cross-model status stays unverified)."""
    if not model:
        return None
    parts = model.lower().split("/")
    for fam, pattern in FAMILY_NAMES:
        if re.match(pattern, parts[-1]):
            return fam
    return next((FAMILY_PROVIDERS[p] for p in parts[:-1] if p in FAMILY_PROVIDERS), None)


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


def available(entry: Entry) -> str | None:
    """Why this entry cannot be called here, or None."""
    if entry.entry == "manual":
        return "manual entries are submitted by a person"
    exe = EXECUTABLES[entry.entry]
    return None if shutil.which(exe) else f"{exe} CLI is not on PATH"


def codex_home() -> Path:
    """Codex's own directory (config.toml, sessions/): $CODEX_HOME when set, else ~/.codex."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
