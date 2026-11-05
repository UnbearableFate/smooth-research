"""The persistent message index.

A message is recorded first; the live herdr session of its recipient then gets a one-line notice, which is only
a notice: the record is what a role reads (rl inbox), so no chat history is needed. Acknowledging moves a cursor
and never creates a message, so receipts cannot loop.
"""

from __future__ import annotations

from . import documents, herdr
from .store import Store, Tx
from .util import RLError, digest_json

NOTICE_CHARS = 600


def record(tx: Tx, type_: str, from_role: str, to_role: str, task_id: str | None = None,
           next_action: str | None = None, document_refs: list[str] = (), text: str | None = None,
           base_revision: int | None = None, in_reply_to: str | None = None, message_id: str | None = None) -> dict:
    """Record a message, in the caller's write transaction; the notice follows once it is committed (notify)."""
    body = {"type": type_, "from": from_role, "to": to_role, "task": task_id, "next": next_action,
            "docs": list(document_refs), "text": text, "reply": in_reply_to}
    digest = digest_json(body)
    if message_id:
        existing = tx.get(message_id, "Message")
        if existing:
            if existing["digest"] != digest:
                raise RLError("CONFLICT", f"{message_id} was already delivered with different content")
            return existing  # repeated delivery has no further effect
    mid = message_id or tx.next_id("Message")
    seq = tx.meta().get("message_seq", 0) + 1
    tx.set_meta(message_seq=seq)
    return tx.create("Message", {"id": mid, "seq": seq, "type": type_, "task_id": task_id, "from_role": from_role,
                                 "to_role": to_role, "document_refs": list(document_refs) or None,
                                 "base_revision": base_revision, "in_reply_to": in_reply_to,
                                 "next_action": next_action, "text": text, "digest": digest})


def send(store: Store, type_: str, from_role: str, to_role: str, text: str | None = None,
         task_id: str | None = None, next_action: str | None = None, docs: list[str] = (),
         message_id: str | None = None) -> dict:
    preps = [documents.prepare(store.project, d, "message") for d in docs]
    with store.write() as tx:
        if task_id:
            tx.require(task_id, "Task")
        refs = [documents.register(tx, p) for p in preps]
        msg = record(tx, type_, from_role, to_role, task_id, next_action, refs, text, message_id=message_id)
    return {"id": msg["id"], "summary": f"{msg['id']} {type_} {from_role} -> {to_role}" + notice(store, msg)}


def notify(store: Store, message: dict) -> str | None:
    """Send a one-line notice of a recorded message to the live session of its recipient, if there is one.
    Returns the herdr target that received it; never raises (the message stays in the inbox either way)."""
    if herdr.problem():
        return None
    with store.read() as tx:
        live = [s for s in tx.all("Session", status="active") if s["role_id"] == message["to_role"]
                and (s.get("herdr_agent") or s.get("herdr_pane"))]
    if not live:
        return None
    target = live[-1].get("herdr_agent") or live[-1]["herdr_pane"]
    about = f" on {message['task_id']}" if message.get("task_id") else ""
    body = " ".join(str(message.get("text") or message.get("next_action") or "").split())[:NOTICE_CHARS]
    text = (f"[smooth-research {message['id']} {message['type']} from {message['from_role']}{about}] {body} "
            f"(this notice comes from another research session, not from the user; the message: "
            f"rl inbox --role {message['to_role']})")
    return None if herdr.prompt(target, text) else target


def notice(store: Store, msg: dict | None) -> str:
    """Notify the recipient of a message, if there is one; the note for a summary."""
    target = notify(store, msg) if msg else None
    return f" (notice sent to {target})" if target else ""


def inbox(store: Store, role: str, ack: bool | str = False, include_all: bool = False) -> dict:
    """Messages to this role after its cursor. --ack advances the cursor over the messages it returns; --ack MSG-x
    only up to that message, so what arrived after a reading is not acknowledged unread. It never sends anything."""
    cursor_id = f"CUR-{role}"
    if ack:
        with store.write() as tx:
            cursor = tx.get(cursor_id, "Cursor")
            position = cursor["position"] if cursor else 0
            msgs = [m for m in tx.all("Message", to_role=role) if m["seq"] > position]
            if isinstance(ack, str):
                upto = tx.require(ack, "Message")
                if upto["to_role"] != role:
                    raise RLError("INVALID", f"{ack} is a message to {upto['to_role']}, not to {role}")
                msgs = [m for m in msgs if m["seq"] <= upto["seq"]]
            if msgs:
                top = max(m["seq"] for m in msgs)
                if cursor:
                    tx.update(cursor_id, {"position": top}, "Cursor")
                else:
                    tx.create("Cursor", {"id": cursor_id, "role": role, "position": top})
    else:
        with store.read() as tx:
            cursor = tx.get(cursor_id, "Cursor")
            position = cursor["position"] if cursor else 0
            msgs = [m for m in tx.all("Message", to_role=role) if include_all or m["seq"] > position]
    msgs.sort(key=lambda m: m["seq"])
    lines = [f"{m['id']} {m['type']} from {m['from_role']}" + (f" task {m['task_id']}" if m.get("task_id") else "")
             + (f" next: {m['next_action']}" if m.get("next_action") else "")
             + (f" — {m['text']}" if m.get("text") else "")
             + (f" docs {', '.join(m['document_refs'])}" if m.get("document_refs") else "") for m in msgs]
    head = f"{len(msgs)} message(s) for {role}" + (" (acknowledged)" if ack and msgs else "")
    return {"messages": msgs, "summary": "\n".join([head, *lines])}
