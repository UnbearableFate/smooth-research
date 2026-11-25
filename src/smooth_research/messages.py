"""Dialogue entries: review discussion, the coordinator's questions and their answers (design 8.7, 8.9).

Notices are not stored: the rules table (inbox.py) computes them from record states. The Message table holds only
dialogue, which is content in itself, each entry on a subject record (a task, a review round or one of its
findings, an advice ticket). An entry waits in its addressee's inbox until a reply to it comes.

- The coordinator asks any session a question; the answer is the only entry anyone writes to the coordinator, one
  per question (9.1). The coordinator closes the question once it read the answer.
- In an open review round the reviewer and the owner of a finding discuss it: a method finding's owner is the
  scientist, an engineering finding's the task's holder (5.3); an entry on the round itself goes to its author.
  What the discussion changes goes into the finding's status (rl review answer --close), not into the entry.
- A consultation is the AD ticket's question; the advisor's answer is a document on the ticket.
"""

from __future__ import annotations

from . import refs as locators
from .roles import Actor
from .store import Store, Tx
from .util import RLError, now_utc


def _entry(tx: Tx, kind: str, subject: str, actor: Actor, to: str, text: str, refs: list[str],
           in_reply_to: str | None = None, finding: str | None = None) -> dict:
    mid = tx.next_id("Message")
    rec = tx.create("Message", {"id": mid, "kind": kind, "subject": subject, "from_role": actor.name, "to_role": to,
                                "text": text, "refs": refs or None, "in_reply_to": in_reply_to, "finding": finding})
    tx.event(f"dialogue.{kind}", subject, f"{mid} {actor.name} -> {to}")
    return rec


def ask(store: Store, actor: Actor, to: str, subject: str, text: str, refs: list[str] = ()) -> dict:
    """The coordinator's question to a session, on the record it is about."""
    if actor.role != "coordinator":
        raise RLError("INVALID", "only the coordinator asks questions; other roles use their records (a decision "
                                 "request, a review discussion, a consultation)")
    if to in ("coordinator", "user"):
        raise RLError("INVALID", "ask a session: scientist-T-x, researcher-T-x, reviewer-RV-x or advisor-AD-x")
    with store.write() as tx:
        tx.require(subject.split("#")[0])
        clean = locators.check(tx, store.project, list(refs), note_cap=None)
        rec = _entry(tx, "question", subject, actor, to, text, clean)
    return {"id": rec["id"], "summary": f"{rec['id']} question to {to} on {subject}"}


def reply(store: Store, actor: Actor, mid: str, text: str, refs: list[str] = ()) -> dict:
    with store.write() as tx:
        m = tx.require(mid, "Message")
        if m["to_role"] != actor.name:
            raise RLError("INVALID", f"{mid} is addressed to {m['to_role']}, not to {actor.name}")
        if m["kind"] == "answer":
            raise RLError("INVALID", f"{mid} is an answer; the question it answers is closed by the coordinator")
        if any(x.get("in_reply_to") == mid for x in tx.all("Message", subject=m["subject"])):
            raise RLError("CONFLICT", f"{mid} already has a reply (each entry is answered once)")
        clean = locators.check(tx, store.project, list(refs), note_cap=None)
        kind = "answer" if m["kind"] == "question" else m["kind"]
        if m["kind"] == "discussion":
            _open_round(tx, m["subject"])
        rec = _entry(tx, kind, m["subject"], actor, m["from_role"], text, clean, in_reply_to=mid,
                     finding=m.get("finding"))
    return {"id": rec["id"], "summary": f"{rec['id']} reply to {mid} ({m['from_role']})"}


def close(store: Store, actor: Actor, mid: str) -> dict:
    """The coordinator read the answer to its question: the question and its answer leave the inbox."""
    if actor.role != "coordinator":
        raise RLError("INVALID", "the coordinator closes its questions")
    with store.write() as tx:
        m = tx.require(mid, "Message")
        if m["kind"] != "question" or m["from_role"] != "coordinator":
            raise RLError("INVALID", f"{mid} is not a question of the coordinator")
        stamp = now_utc()
        tx.update(mid, {"closed_at": stamp})
        for a in tx.all("Message", subject=m["subject"]):
            if a.get("in_reply_to") == mid:
                tx.update(a["id"], {"closed_at": stamp})
        tx.event("dialogue.closed", m["subject"], mid)
    return {"id": mid, "summary": f"{mid} closed"}


def _open_round(tx: Tx, subject: str) -> dict:
    rv = tx.require(subject.split("#")[0], "Ticket")
    if rv["kind"] != "review" or rv["status"] not in ("waiting", "answered"):
        raise RLError("INVALID", f"review discussion happens in an open review round; {rv['id']} is {rv['status']}")
    return rv


def discuss(store: Store, actor: Actor, subject: str, text: str, refs: list[str] = ()) -> dict:
    """An entry in an open review round (RV-x) or on one of its findings (RV-x#Fn), between the reviewer and the
    finding's owner."""
    with store.write() as tx:
        rv = _open_round(tx, subject)
        finding = None
        if "#F" in subject:
            n = int(subject.split("#F")[1])
            finding = next((f for f in rv.get("findings") or [] if f["n"] == n), None)
            if finding is None:
                raise RLError("NOT_FOUND", f"{subject} does not exist")
        owner = _owner(tx, rv, finding)
        reviewer = f"reviewer-{rv['id']}"
        if actor.name == reviewer:
            to = owner
        elif actor.name in (owner, _author(tx, rv)):
            to = reviewer
        else:
            raise RLError("INVALID", f"the discussion of {subject} is between {reviewer} and {owner}")
        clean = locators.check(tx, store.project, list(refs), note_cap=None)
        rec = _entry(tx, "discussion", rv["id"], actor, to, text, clean,
                     finding=f"#F{finding['n']}" if finding else None)
    return {"id": rec["id"], "summary": f"{rec['id']} discussion on {subject} to {to}"}


def _author(tx: Tx, rv: dict) -> str:
    task = tx.get(rv.get("task_id") or "")
    if rv.get("review_kind") == "design" and task and task.get("holder"):
        return task["holder"]
    return (rv.get("passes") or [{}])[-1].get("requested_by") or rv["requested_by"]


def _owner(tx: Tx, rv: dict, finding: dict | None) -> str:
    """A method finding belongs to the scientist, an engineering one to the task's holder (5.3)."""
    task = tx.get(rv.get("task_id") or "")
    if finding and finding.get("category") == "method" and task and task.get("kind") in ("execute", "prep"):
        return task.get("scientist_session") if task["creator"] == "coordinator" and task.get("scientist_session") \
            else task["creator"]
    return _author(tx, rv)


def thread(tx: Tx, subject: str) -> list[dict]:
    """The dialogue on a record and on its sub-items, oldest first."""
    base = subject.split("#")[0]
    return sorted([m for m in tx.all("Message") if m["subject"].split("#")[0] == base], key=lambda m: m["id"])


def thread_lines(tx: Tx, subject: str) -> list[str]:
    return [f"{m['id']} {m['kind']} {m['from_role']} -> {m['to_role']}"
            + (f" (re {m['in_reply_to']})" if m.get("in_reply_to") else "") + f": {m['text']}"
            for m in thread(tx, subject)]
