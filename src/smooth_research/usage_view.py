"""Compute usage per task, read from Usage records at reading time (design 7.2): never summed across units.

A hypothesis task's usage is that of the runs of its own tasks (PART_OF); the queued or running part is shown
apart (attempts not ended, with the hours they asked for), and attempts that ended without a usage record are
counted, since "not reported" is not "not spent".
"""

from __future__ import annotations

import re

from .store import Tx

ENVELOPE = re.compile(r"(?:about\s+)?([0-9]+(?:\.[0-9]+)?)\s*([^\s,;]+)")


def envelope(text: str | None) -> dict[str, float]:
    """'about 4 MIG·h, 0.5 node·h' -> {'MIG·h': 4.0, 'node·h': 0.5}."""
    return {m[2]: float(m[1]) for m in ENVELOPE.finditer(text or "")}


def task_ids(tx: Tx, task: dict) -> list[str]:
    ids = [task["id"]]
    if task["kind"] == "hypothesis":
        ids += [e["id"] for e in tx.inc(task["id"], "part_of")]
    return ids


def task_usage(tx: Tx, task: dict) -> dict:
    used: dict[str, float] = {}
    counted: set[str] = set()  # one accounting record covers several attempts: counted once
    pending_hours, pending, unreported = 0.0, 0, 0
    for tid in task_ids(tx, task):
        for run in tx.all("Run", task_id=tid):
            for att in tx.all("Attempt", run_id=run["id"]):
                records = [tx.require(e["id"]) for e in tx.inc(att["id"], "covers")]
                records = [u for u in records if not u.get("superseded")]
                for u in records:
                    if u["id"] not in counted:
                        counted.add(u["id"])
                        used[u["unit"]] = used.get(u["unit"], 0.0) + u["quantity"]
                if att["execution_state"] in ("prepared", "queued", "running", "unknown"):
                    pending += 1
                    pending_hours += (run.get("minutes") or 0) / 60
                elif not records:
                    unreported += 1
    return {"used": used, "pending": pending, "pending_hours": pending_hours, "unreported": unreported,
            "envelope": envelope(task.get("envelope") or task.get("estimate"))}


def fmt(amounts: dict[str, float]) -> str:
    return ", ".join(f"{v:.3g} {k}" for k, v in sorted(amounts.items())) or "0"


def task_usage_short(tx: Tx, task: dict) -> str:
    u = task_usage(tx, task)
    env = fmt(u["envelope"]) if u["envelope"] else "—"
    return f"{fmt(u['used']) if u['used'] else '—'}/{env}"


def usage_line(tx: Tx, task: dict) -> str:
    """The usage line of a check (9.2, 9.3)."""
    u = task_usage(tx, task)
    card = "envelope" if task["kind"] == "hypothesis" else "card"
    return (f"about {fmt(u['used'])} used, {u['pending']} attempt(s) queued or running"
            + (f" (about {u['pending_hours']:.3g} h asked)" if u["pending_hours"] else "")
            + (f" ({card}: about {fmt(u['envelope'])})" if u["envelope"] else "")
            + f"; {u['unreported']} attempt(s) unreported")


def lines_crossed(tx: Tx, task: dict, shares: list[float]) -> list[tuple[float, str]]:
    """The envelope lines (shares) the recorded usage has crossed, per unit: [(share, unit)]."""
    u = task_usage(tx, task)
    out = []
    for unit, cap in u["envelope"].items():
        spent = u["used"].get(unit, 0.0)
        out += [(share, unit) for share in shares if cap > 0 and spent >= share * cap]
    return out
