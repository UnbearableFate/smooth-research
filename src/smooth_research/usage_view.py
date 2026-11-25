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
    """used, per unit; shared: the part of it from accounting records that also cover attempts of other tasks,
    counted in full in each of them (no attribution rule yet, 7.2), so the tasks' amounts do not add up."""
    used: dict[str, float] = {}
    shared: dict[str, float] = {}
    counted: set[str] = set()  # one accounting record covers several attempts: counted once
    pending_hours, pending, unreported, local = 0.0, 0, 0, 0
    mine = set(task_ids(tx, task))
    for tid in mine:
        for run in tx.all("Run", task_id=tid):
            for att in tx.all("Attempt", run_id=run["id"]):
                here = on_this_machine(tx, att)
                records = [tx.require(e["id"]) for e in tx.inc(att["id"], "covers")]
                records = [u for u in records if not u.get("superseded") and not here]  # (11-25, the user)
                for u in records:
                    if u["id"] not in counted:
                        counted.add(u["id"])
                        used[u["unit"]] = used.get(u["unit"], 0.0) + u["quantity"]
                        if any(_task_of(tx, e["id"]) not in mine for e in tx.out(u["id"], "covers")):
                            shared[u["unit"]] = shared.get(u["unit"], 0.0) + u["quantity"]
                if att["execution_state"] in ("prepared", "queued", "running", "unknown"):
                    pending += 1
                    pending_hours += 0 if here else (run.get("minutes") or 0) / 60
                elif here and not records:
                    local += 1  # a run on this machine counts no usage (11-25, the user's decision)
                elif not records:
                    unreported += 1
    return {"used": used, "shared": shared, "pending": pending, "pending_hours": pending_hours,
            "unreported": unreported, "local": local,
            "envelope": envelope(task.get("envelope") or task.get("estimate"))}


def on_this_machine(tx: Tx, att: dict) -> bool:
    """An attempt that ran on this machine: registered for no remote resource and bound to no job, container or
    remote process. It counts no usage (11-25, the user's decision)."""
    if (att.get("resource") or {}).get("kind") == "remote":
        return False
    return all(tx.require(e["id"], "ExecRef")["kind"] == "local_process" for e in tx.out(att["id"], "bound_to"))


def _task_of(tx: Tx, attempt: str) -> str | None:
    att = tx.get(attempt)
    run = tx.get(att["run_id"]) if att else None
    return run["task_id"] if run else None


def fmt(amounts: dict[str, float]) -> str:
    return ", ".join(f"{v:.3g} {k}" for k, v in sorted(amounts.items())) or "0"


def task_usage_short(tx: Tx, task: dict) -> str:
    u = task_usage(tx, task)
    env = fmt(u["envelope"]) if u["envelope"] else "—"
    return f"{fmt(u['used']) if u['used'] else '—'}{'*' if u['shared'] else ''}/{env}"


def usage_line(tx: Tx, task: dict) -> str:
    """The usage line of a check (9.2, 9.3)."""
    u = task_usage(tx, task)
    card = "envelope" if task["kind"] == "hypothesis" else "card"
    return (f"about {fmt(u['used'])} used, {u['pending']} attempt(s) queued or running"
            + (f" (about {u['pending_hours']:.3g} h asked)" if u["pending_hours"] else "")
            + (f" ({card}: about {fmt(u['envelope'])})" if u["envelope"] else "")
            + (f"; of it {fmt(u['shared'])} is shared with other tasks and counted in each (not additive)"
               if u["shared"] else "")
            + f"; {u['unreported']} attempt(s) unreported"
            + (f"; {u['local']} attempt(s) on this machine, not counted" if u["local"] else ""))


def lines_crossed(tx: Tx, task: dict, shares: list[float]) -> list[tuple[float, str]]:
    """The envelope lines (shares) the recorded usage has crossed, per unit: [(share, unit)]."""
    u = task_usage(tx, task)
    out = []
    for unit, cap in u["envelope"].items():
        spent = u["used"].get(unit, 0.0)
        out += [(share, unit) for share in shares if cap > 0 and spent >= share * cap]
    return out
