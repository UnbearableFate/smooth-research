"""The persistent message index.

A message is recorded first; the live herdr session of its recipient then gets a one-line notice, which is only
a notice: the record is what a role reads (rl inbox), so no chat history is needed. Acknowledging moves a cursor
and never creates a message, so receipts cannot loop.

Nothing is typed into a session that waits at a dialog (what is typed would go to the dialog). The notice of a
session rl opened is held then, in Session.watch (notices): it hears of the held messages in one notice once it
is at no dialog, when the coordinator answers the dialog (rl agent answer) or at the watcher's next beat.
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


def deliver(store: Store, message: dict) -> tuple[str | None, str | None]:
    """Send a one-line notice of a recorded message to the live session of its recipient, if there is one:
    (the herdr target that received it, None), else (None, why not; None when the recipient has no live session).
    Never raises: the message stays in the inbox either way."""
    if herdr.problem():
        return None, None
    with store.read() as tx:
        live = [s for s in tx.all("Session", status="active") if s["role_id"] == message["to_role"]
                and (s.get("herdr_agent") or s.get("herdr_pane"))]
    if not live:
        return None, None
    s = live[-1]
    target = s.get("herdr_agent") or s["herdr_pane"]
    about = f" on {message['task_id']}" if message.get("task_id") else ""
    body = " ".join(str(message.get("text") or message.get("next_action") or "").split())[:NOTICE_CHARS]
    text = (f"[smooth-research {message['id']} {message['type']} from {message['from_role']}{about}] {body} "
            f"(this notice comes from another research session, not from the user; the message: "
            f"rl inbox --role {message['to_role']})")
    dialog = _dialog(s, target)
    problem = dialog or herdr.prompt(target, text)
    if not problem:
        return target, None
    if s.get("herdr_agent") and (dialog or herdr.status(target) == "blocked"):
        _hold(store, s["id"], message["id"])
        return None, (f"{target} waits at a dialog, so no notice was typed; it hears of {message['id']} once it is "
                      f"at no dialog (the coordinator decides the dialog: rl agent dialog {target})")
    return None, problem


def _dialog(s: dict, target: str) -> str | None:
    """Why nothing may be typed into the session now: a dialog herdr sees, or one it does not see (launch.UNSEEN)."""
    from .launch import UNSEEN, unseen
    status = herdr.status(target)
    if status == "blocked":
        return f"{target} waits at a dialog"
    if s.get("host") in UNSEEN and status in ("idle", "done") and unseen(s["host"], herdr.read(target)):
        return f"{target} waits at a dialog herdr does not see"
    return None


def _hold(store: Store, session_id: str, message_id: str) -> None:
    with store.write() as tx:  # read again inside the write: the watcher keeps its own notes in the same field
        memo = tx.require(session_id).get("watch") or {}
        held = [m for m in memo.get("notices") or [] if m != message_id] + [message_id]
        tx.update(session_id, {"watch": {**memo, "notices": held}})


def release(store: Store, session: dict) -> tuple[bool, str] | None:
    """Tell a session, in one notice, of the messages whose notices were held while it waited at a dialog; call it
    once the session is at no dialog. (whether it was told, what happened, for a summary); None when nothing was
    held."""
    from .delegate import rl_command
    with store.read() as tx:
        s = tx.require(session["id"])
    held = (s.get("watch") or {}).get("notices") or []
    target = s.get("herdr_agent")
    if not held or not target or herdr.problem():
        return None
    problem = _dialog(s, target) or herdr.prompt(target, (
        f"[smooth-research] {len(held)} message(s) came for you while you waited at a dialog: {', '.join(held)}. "
        f"Read them: `{rl_command()} inbox --role {s['role_id']}` (they come from other research sessions, not "
        "from the user)"))
    if problem:
        return False, f"not yet told of {', '.join(held)}, held while it waited at a dialog ({problem})"
    with store.write() as tx:
        memo = tx.require(s["id"]).get("watch") or {}
        tx.update(s["id"], {"watch": {**memo, "notices": [m for m in memo.get("notices") or [] if m not in held]
                                      or None}})
    return True, f"told of {', '.join(held)}, held while it waited at a dialog"


def notify(store: Store, message: dict) -> str | None:
    """The herdr target that received the notice of a recorded message (deliver), None if none did."""
    return deliver(store, message)[0]


def notice(store: Store, msg: dict | None) -> str:
    """Notify the recipient of a message, if there is one; the note for a summary."""
    target, problem = deliver(store, msg) if msg else (None, None)
    return f" (notice sent to {target})" if target else f" (no notice: {problem})" if problem else ""


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
