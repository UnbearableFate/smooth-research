"""Who is calling: the session a command runs in, from the environment rl gives each pane it opens.

A session is one role on one thing: the coordinator on the project, a scientist on a hypothesis or phase task (or a
handed-over preparation task; tree tasks in older records), a researcher on a task, a reviewer on a review round, an
advisor on an advice ticket. Its name is <role>-<subject> (scientist-T-0040, reviewer-RV-0036); the coordinator's is
"coordinator". Commands outside a session (a person at a shell, tests) name the session with --as.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .schema import ROLES
from .util import RLError

ENV_SESSION = "SMOOTH_RESEARCH_SESSION"  # the session name
ENV_PROJECT_HINT = "SMOOTH_RESEARCH_PROJECT"


@dataclass(frozen=True)
class Actor:
    name: str
    role: str
    subject: str | None

    def __str__(self) -> str:
        return self.name


def session_name(role: str, subject: str | None) -> str:
    return "coordinator" if role == "coordinator" else f"{role}-{subject}"


def parse(name: str) -> Actor:
    name = name.strip()
    if name in ("coordinator", "user"):
        return Actor(name, name, None)
    role, _, subject = name.partition("-")
    if role not in ROLES or not subject:
        raise RLError("INVALID", f"{name!r} is not a session name: <role>-<subject> with role one of "
                                 f"{', '.join(ROLES)} (scientist-T-0040, reviewer-RV-0036), or coordinator")
    return Actor(name, role, subject)


def current(explicit: str | None = None) -> Actor | None:
    """The calling session: --as, else the pane's environment; None outside any session."""
    name = explicit or os.environ.get(ENV_SESSION)
    return parse(name) if name else None


def need(actor: Actor | None, *roles: str, what: str = "this", flags=None) -> Actor:
    """The calling session. Without one nothing could say who recorded the step, so the command is refused; a
    session of another role than the process names does it anyway, with a role flag (flags.Flags) on what it
    records (design 2026-11-27, 5.1). Without flags given, the role is only reported back (warnings)."""
    if actor is None:
        raise RLError("INVALID", f"{what} is recorded with the session that does it: run it in a session's pane "
                                 f"(it sets {ENV_SESSION}), or name the session with --as <name>")
    if actor.role not in roles and flags is not None:
        flags.add("role", f"{what} is the {' or '.join(roles)}'s step; {actor.name} did it")
    return actor


def is_role(actor: Actor | None, *roles: str) -> bool:
    return actor is not None and actor.role in roles
