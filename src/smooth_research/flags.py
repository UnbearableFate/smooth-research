"""Process flags (design 2026-11-27, chapter 5).

The CLI refuses only what it cannot record, or what would be recorded falsely. Every other rule of the process (who
does a step, in which order, whether the user approved, whether tests and reviews were done, how long a note is) is
followed by printing a reminder and writing a flag {code, text, at, by} on the record. A flag stops no later
command: the reviewer, the coordinator and the user see it (rl show and the check lines, the receiver's pack and
to-do notes, the research page, and the "not done by the process" part of rl status) and judge it.
"""

from __future__ import annotations

from .schema import FLAGGED
from .store import Tx, label_of
from .util import now_utc

# The codes, with what each one says was skipped (the list grew out of sorting every refusal of the old CLI; the
# design's appendix A lists where each one is written).
CODES = {
    "role": "done by a session of another role than the process names",
    "not-holder": "done by a session other than the one the process names (the holder, the creator, the addressee)",
    "unapproved": "released or recorded without the user's approval, or against their answer",
    "untested": "handed on without a passing test of the commit",
    "unreviewed": "handed on, run or finalized without a review that covers it",
    "open-findings": "handed on, run or finalized with review findings still open",
    "prerequisite": "taken or handed on before the tasks it depends on were accepted",
    "no-evidence": "delivered or decided without the evidence the process asks for",
    "no-verdict": "delivered without a final verdict or stop on each of its hypotheses",
    "open-children": "delivered with tasks under it still open",
    "outside-work-dir": "a scientist's task branch changed files outside its work directory",
    "long-note": "a note longer than 300 characters (notices show its first 300)",
    "not-evidence": "evidence that cites a trial, a deviating run or another task's run",
    "unsealed": "an evidence run registered before its design was sealed, or after it was revised",
    "deviates": "an evidence run that does not match the sealed manifest, registered as deviating",
    "done-check": "delivered while its done check fails",
    "no-basis": "a stop without the upstream decision or the answered request behind it",
    "force": "the mainline moved although the tests on the merge commit failed (--force --reason)",
    "round-ended": "a review asked for again after its round ended (a new round was opened instead)",
    "category": "an engineering finding inside the protected scope, without the reason it is not a method finding",
    "no-executor": "a run registered without --resource in a project whose compute goes through an executor",
}


def entry(code: str, text: str, by: str) -> dict:
    return {"code": code, "text": text, "at": now_utc(), "by": by}


def add(tx: Tx, rec_id: str, code: str, text: str, by: str) -> dict:
    """Write one flag on a record (its own flags list; the event log names it too)."""
    rec = tx.require(rec_id)
    f = entry(code, text, by)
    if rec["_label"] in FLAGGED:
        tx.update(rec_id, {"flags": list(rec.get("flags") or []) + [f]})
    tx.event("flag", rec_id, f"[{code}] {text}"[:500])
    return f


class Flags:
    """The flags one command collects before it writes its record, then writes on that record."""

    def __init__(self, by: str):
        self.by = by
        self.items: list[dict] = []

    def add(self, code: str, text: str) -> None:
        if not any(i["code"] == code and i["text"] == text for i in self.items):
            self.items.append(entry(code, text, self.by))

    def __bool__(self) -> bool:
        return bool(self.items)

    def write(self, tx: Tx, rec_id: str) -> list[dict]:
        rec = tx.require(rec_id)
        if rec["_label"] in FLAGGED and self.items:
            tx.update(rec_id, {"flags": list(rec.get("flags") or []) + self.items})
        for f in self.items:
            tx.event("flag", rec_id, f"[{f['code']}] {f['text']}"[:500])
        return self.items


def line(f: dict) -> str:
    return f"[{f['code']}] {f['text']} ({f.get('by') or '?'}, {(f.get('at') or '')[:16]})"


def short(rec: dict) -> str | None:
    """The flags of a record in one note ('' when it has none), for to-do notes and packs."""
    found = rec.get("flags") or []
    return "flags: " + "; ".join(f"[{f['code']}] {f['text']}" for f in found) if found else None


def flagged(tx: Tx) -> list[tuple[dict, dict]]:
    """(record, flag) for every flag on every record, oldest first: what rl status shows the user."""
    out = []
    for label in FLAGGED:
        for rec in tx.all(label):
            for f in rec.get("flags") or []:
                out.append((rec, f))
    return sorted(out, key=lambda rf: rf[1].get("at") or "")


def is_flaggable(rec_id: str) -> bool:
    try:
        return label_of(rec_id) in FLAGGED
    except Exception:
        return False
