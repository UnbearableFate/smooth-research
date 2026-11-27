"""Dialogue entries: review discussion, questions and their answers (design 8.7, 8.9; 2026-11-27, 3.2 and 4).

Notices are not stored: the rules table (inbox.py) computes them from record states. The Message table holds only
dialogue, which is content in itself, each entry on a subject record (a task, a review round or one of its
findings, an advice ticket). An entry waits in its addressee's to-do list until a reply to it comes, and each entry
tells its addressee's session when it is written (result wake).

- The coordinator asks a session a question; the answer goes back to whoever asked, who closes the question once it
  read the answer. A reply to an answer is a follow-up question.
- In a review round the reviewer and the owner of a finding discuss it: a method finding's owner is the scientist
  who made the task, an engineering finding's (or the round's as a whole) the round's author. What the discussion
  changes goes into the finding (rl review answer --close, rl review reject), not into the entry.
- Discussion in a round that has ended (or whose task ended) is still recorded, but goes in nobody's to-do list and
  tells nobody (4: the same condition for rl discuss, rl reply and the inbox).

Nothing here refuses who writes an entry: an entry by another session than the one the process names is recorded
with a flag on its subject record.
"""

from __future__ import annotations

from . import refs as locators
from .flags import Flags
from .reviews import finding_owner, round_author, round_ended
from .roles import Actor, need, parse, session_name
from .schema import ROLES
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
    """The coordinator's question to a session, on the record it is about; the session is told."""
    fl = Flags(actor.name if actor else "?")
    need(actor, "coordinator", what="a question to a session", flags=fl)
    if to == "user":
        raise RLError("INVALID", "the user is not a session: ask the user in the coordinator's pane, or record a "
                                 "request (rl request --kind approval|decision)")
    parse(to)  # a session name, or nothing could deliver it
    if to == actor.name:
        raise RLError("INVALID", f"{to} is the session asking: ask another session")
    with store.write() as tx:
        base = subject.split("#")[0]
        tx.require(base)
        clean = locators.check(tx, store.project, list(refs), note_cap=None)
        rec = _entry(tx, "question", subject, actor, to, text, clean)
        written = fl.write(tx, base) if fl else []
    return {"id": rec["id"], "summary": f"{rec['id']} question to {to} on {subject}", "flags": written,
            "wake": [to]}


def reply(store: Store, actor: Actor, mid: str, text: str, refs: list[str] = ()) -> dict:
    """A reply to an entry addressed to the caller: an answer to a question, a follow-up question to an answer, the
    next entry of a review discussion. In a round that has ended it is recorded and tells nobody."""
    need(actor, *ROLES, what="a reply")
    fl = Flags(actor.name)
    warnings: list[str] = []
    with store.write() as tx:
        m = tx.require(mid, "Message")
        if m["to_role"] != actor.name:
            fl.add("not-holder", f"{mid} is addressed to {m['to_role']}; {actor.name} replied to it")
        earlier = [x["id"] for x in tx.all("Message", subject=m["subject"]) if x.get("in_reply_to") == mid]
        if earlier:
            warnings.append(f"{mid} has a reply already ({earlier[-1]}): this one is recorded after it")
        clean = locators.check(tx, store.project, list(refs), note_cap=None)
        kind = {"question": "answer", "answer": "question"}.get(m["kind"], m["kind"])
        over = m["kind"] == "discussion" and _round_over(tx, m["subject"])
        if over:
            warnings.append(f"{m['subject'].split('#')[0]} has ended: the reply is recorded and goes in no to-do list")
        rec = _entry(tx, kind, m["subject"], actor, m["from_role"], text, clean, in_reply_to=mid,
                     finding=m.get("finding"))
        written = fl.write(tx, m["subject"].split("#")[0]) if fl else []
    return {"id": rec["id"], "summary": f"{rec['id']} reply to {mid} ({m['from_role']})", "flags": written,
            "warnings": warnings, "wake": [] if over else [m["from_role"]]}


def close(store: Store, actor: Actor, mid: str) -> dict:
    """Whoever asked read the answer to its question: the question and its answers leave the to-do lists."""
    need(actor, *ROLES, what="closing a question")
    fl = Flags(actor.name)
    with store.write() as tx:
        m = tx.require(mid, "Message")
        if m["kind"] != "question":
            raise RLError("INVALID", f"{mid} is a {m['kind']} entry: --close takes a question (MSG-x)")
        if m["from_role"] != actor.name:
            fl.add("not-holder", f"{mid} was asked by {m['from_role']}; {actor.name} closed it")
        stamp = now_utc()
        tx.update(mid, {"closed_at": stamp})
        for a in tx.all("Message", subject=m["subject"]):
            if a.get("in_reply_to") == mid:
                tx.update(a["id"], {"closed_at": stamp})
        written = fl.write(tx, m["subject"].split("#")[0]) if fl else []
        tx.event("dialogue.closed", m["subject"], mid)
    return {"id": mid, "summary": f"{mid} closed", "flags": written}


def _round_over(tx: Tx, subject: str) -> bool:
    rv = tx.get(subject.split("#")[0])
    return bool(rv) and rv["_label"] == "Ticket" and rv["kind"] == "review" and round_ended(tx, rv)


def discuss(store: Store, actor: Actor, subject: str, text: str, refs: list[str] = ()) -> dict:
    """An entry in a review round (RV-x) or on one of its findings (RV-x#Fn), between the reviewer and the finding's
    owner (the round's author for the round as a whole); the other side is told. In a round that has ended it is
    recorded and tells nobody."""
    need(actor, *ROLES, what="review discussion")
    fl = Flags(actor.name)
    warnings: list[str] = []
    with store.write() as tx:
        rv = tx.require(subject.split("#")[0], "Ticket")
        if rv["kind"] != "review":
            raise RLError("INVALID", f"{rv['id']} is an advice ticket: discussion is on a review round (RV-x or "
                                     "RV-x#Fn)")
        finding = None
        if "#F" in subject:
            n = subject.split("#F", 1)[1]
            finding = next((f for f in rv.get("findings") or [] if n.isdigit() and f["n"] == int(n)), None)
            if finding is None:
                raise RLError("NOT_FOUND", f"{subject} does not exist")
        owner = finding_owner(tx, rv, finding)
        reviewer = session_name("reviewer", rv["id"])
        if actor.name == reviewer:
            to = owner
        else:
            to = reviewer
            if actor.name not in (owner, round_author(tx, rv)):
                fl.add("not-holder", f"the discussion of {subject} is between {reviewer} and {owner}; {actor.name} "
                                     "wrote in it")
        over = round_ended(tx, rv)
        if over:
            warnings.append(f"{rv['id']} has ended: the entry is recorded and goes in no to-do list")
        clean = locators.check(tx, store.project, list(refs), note_cap=None)
        rec = _entry(tx, "discussion", rv["id"], actor, to, text, clean,
                     finding=f"#F{finding['n']}" if finding else None)
        written = fl.write(tx, rv["id"]) if fl else []
    return {"id": rec["id"], "summary": f"{rec['id']} discussion on {subject} to {to}", "flags": written,
            "warnings": warnings, "wake": [] if over else [to]}


def thread(tx: Tx, subject: str) -> list[dict]:
    """The dialogue on a record and on its sub-items, oldest first."""
    base = subject.split("#")[0]
    return sorted([m for m in tx.all("Message") if m["subject"].split("#")[0] == base], key=lambda m: m["id"])


def thread_lines(tx: Tx, subject: str) -> list[str]:
    return [f"{m['id']} {m['kind']} {m['from_role']} -> {m['to_role']}"
            + (f" (re {m['in_reply_to']})" if m.get("in_reply_to") else "") + f": {m['text']}"
            for m in thread(tx, subject)]
