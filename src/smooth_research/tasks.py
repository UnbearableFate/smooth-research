"""Sessions, tasks and the persistent message index.

Chat notifications are only notices; the versioned task package is what a session resumes from. Every task
transition writes one message (TASK, RESULT, WAIT, BLOCKED, HANDOFF, CANCEL) so a role can read its inbox
without chat history. Acknowledging moves a cursor and never creates a message, so receipts cannot loop.
"""

from __future__ import annotations

from . import documents
from .store import Store, Tx
from .util import RLError, digest_json

COORDINATOR = "coordinator"
OPEN_STATES = ("ready", "active", "waiting", "blocked", "delivered")


# -- sessions -------------------------------------------------------------------------------------------------


def start_session(store: Store, role: str, host: str, host_session_id: str | None = None,
                  model_requested: str | None = None, model_observed: str | None = None,
                  effort: str | None = None, worktree: str | None = None) -> dict:
    """Register a session. A new coordinator session marks the earlier active ones superseded."""
    with store.write() as tx:
        if host_session_id:
            for s in tx.all("Session", host_session_id=host_session_id, status="active"):
                if s["role_id"] == role:
                    return {"id": s["id"], "reused": True, "summary": f"{s['id']} already registered as {role}"}
        sid = tx.next_id("Session")
        if role == COORDINATOR:
            for s in tx.all("Session", status="active"):
                if s["role_id"] == COORDINATOR:
                    tx.update(s["id"], {"status": "superseded", "ended_at": tx.meta()["updated_at"]})
        tx.create("Session", {"id": sid, "role_id": role, "host": host, "host_session_id": host_session_id,
                              "model_requested": model_requested, "model_observed": model_observed,
                              "effort": effort, "worktree": worktree, "status": "active"})
        tx.event("session.start", sid, f"{role} on {host} {model_observed or model_requested or 'model unverified'}")
    return {"id": sid, "summary": f"{sid} {role} on {host}"}


def end_session(store: Store, sid: str) -> dict:
    with store.write() as tx:
        s = tx.require(sid, "Session")
        if s["status"] == "active":
            tx.update(sid, {"status": "ended", "ended_at": tx.meta()["updated_at"]})
        tx.event("session.end", sid, s["role_id"])
    return {"id": sid, "summary": f"{sid} ended (running external work is not affected)"}


# -- messages -------------------------------------------------------------------------------------------------


def _message(tx: Tx, type_: str, from_role: str, to_role: str, task_id: str | None = None,
             next_action: str | None = None, document_refs: list[str] = (), text: str | None = None,
             base_revision: int | None = None, in_reply_to: str | None = None, message_id: str | None = None) -> dict:
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
        msg = _message(tx, type_, from_role, to_role, task_id, next_action, refs, text, message_id=message_id)
    return {"id": msg["id"], "summary": f"{msg['id']} {type_} {from_role} -> {to_role}"}


def inbox(store: Store, role: str, ack: bool = False, include_all: bool = False) -> dict:
    """Messages to this role after its cursor. --ack advances the cursor; it never sends anything."""
    cursor_id = f"CUR-{role}"
    if ack:
        with store.write() as tx:
            cursor = tx.get(cursor_id, "Cursor")
            position = cursor["position"] if cursor else 0
            msgs = [m for m in tx.all("Message", to_role=role) if m["seq"] > position]
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


# -- tasks ----------------------------------------------------------------------------------------------------


def create_task(store: Store, title: str, kind: str, targets: list[str] = (), request: str | None = None,
                to_role: str | None = None, next_action: str | None = None, depends_on: list[str] = (),
                from_role: str = COORDINATOR) -> dict:
    prep = documents.prepare(store.project, request, "task_request") if request else None
    with store.write() as tx:
        tid = tx.next_id("Task")
        ref = documents.register(tx, prep) if prep else None
        tx.create("Task", {"id": tid, "title": title, "kind": kind, "state": "ready", "to_role": to_role,
                           "request_ref": ref, "next_action": next_action, "revision": 1})
        for target in targets:
            tx.link("targets", tid, target)
        for dep in depends_on:
            tx.link("depends_on", tid, dep)
        if to_role:
            _message(tx, "TASK", from_role, to_role, tid, next_action, [ref] if ref else [], title, base_revision=1)
        tx.event("task.created", tid, title)
    return {"id": tid, "request_ref": ref,
            "summary": f"{tid} ready: {title}" + (f" (request {ref})" if ref else "") + (f" -> {to_role}" if to_role else "")}


def claim(store: Store, tid: str, role: str, session: str | None = None) -> dict:
    """Exactly one claimant wins."""
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "ready" or task.get("owner_role"):
            raise RLError("CONFLICT", f"{tid} is {task['state']} (owner {task.get('owner_role') or 'none'}); "
                                      "it cannot be claimed now",
                          current={k: task.get(k) for k in ("state", "owner_role", "owner_session", "next_action")})
        open_deps = [d["id"] for d in tx.out(tid, "depends_on")
                     if d["label"] == "Task" and tx.require(d["id"])["state"] not in ("delivered", "accepted")]
        tx.update(tid, {"state": "active", "owner_role": role, "owner_session": session,
                        "revision": task["revision"] + 1})
        tx.event("task.claimed", tid, role)
    warn = f"; waiting dependencies: {', '.join(open_deps)}" if open_deps else ""
    return {"id": tid, "summary": f"{tid} claimed by {role}{warn}"}


def _owner_write(store: Store, tid: str, docs: list[str], doc_type: str, fn) -> dict:
    """Seal the documents and apply fn in one write; only a claimed task takes owner updates."""
    preps = [documents.prepare(store.project, d, doc_type) for d in docs]
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if not task.get("owner_role"):
            raise RLError("CONFLICT", f"{tid} is {task['state']} and has no owner; claim it first "
                                      f"(rl task claim {tid} --role <role>)", current_state=task["state"])
        return fn(tx, task, [documents.register(tx, p) for p in preps])


def progress(store: Store, tid: str, state: str, from_role: str, summary: str | None = None,
             outputs: list[str] = (), next_action: str | None = None, attempts: list[str] = (),
             reason: str | None = None) -> dict:
    """deliver (RESULT), wait (WAIT on attempts), block (BLOCKED with a reason) or resume work (active)."""
    if state == "blocked" and not reason:
        raise RLError("INVALID", "blocked needs --reason engineering|model_unavailable|budget|science")
    if state == "waiting" and not next_action:
        raise RLError("INVALID", "waiting needs --next-action: what to do when the awaited facts arrive")

    def apply(tx: Tx, task: dict, refs: list[str]) -> dict:
        changes = {"state": state, "revision": task["revision"] + 1, "note": summary or task.get("note")}
        if next_action:
            changes["next_action"] = next_action
        if refs:
            changes["output_refs"] = (task.get("output_refs") or []) + refs
        if state == "waiting":
            for a in attempts:
                tx.require(a, "Attempt")
            changes["wait_attempts"] = list(attempts)
        changes["blocked_reason"] = reason if state == "blocked" else None
        tx.update(tid, changes)
        mtype = {"delivered": "RESULT", "waiting": "WAIT", "blocked": "BLOCKED"}.get(state)
        if mtype:
            _message(tx, mtype, from_role, COORDINATOR, tid, next_action, refs,
                     summary or (f"waiting on {', '.join(attempts)}" if attempts else reason),
                     base_revision=changes["revision"])
        tx.event(f"task.{state}", tid, summary or next_action or reason or "")
        return {"id": tid, "outputs": refs, "summary": f"{tid} -> {state}" + (f" outputs {', '.join(refs)}" if refs else "")}

    return _owner_write(store, tid, list(outputs), "task_output", apply)


def handoff(store: Store, tid: str, doc: str, from_role: str, to_role: str | None = None) -> dict:
    """Seal the handoff and release ownership; the task is ready to claim again."""
    def apply(tx: Tx, task: dict, refs: list[str]) -> dict:
        tx.update(tid, {"state": "ready", "owner_role": None, "owner_session": None, "handoff_ref": refs[0],
                        "revision": task["revision"] + 1, "to_role": to_role or task.get("to_role")})
        _message(tx, "HANDOFF", from_role, to_role or task.get("to_role") or COORDINATOR, tid,
                 "read the handoff, verify code and run records, then continue", refs)
        tx.event("task.handoff", tid, refs[0])
        return {"id": tid, "handoff_ref": refs[0], "summary": f"{tid} handed off ({refs[0]}); ready to claim"}

    return _owner_write(store, tid, [doc], "handoff", apply)


def review_task(store: Store, tid: str, verdict: str, reason: str) -> dict:
    """Coordinator acceptance or stop. Stopping a research task does not cancel any external execution."""
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if verdict == "accept" and task["state"] != "delivered":
            raise RLError("INVALID", f"{tid} is {task['state']}; only delivered work can be accepted")
        state = "accepted" if verdict == "accept" else "stopped" if verdict == "stop" else "active"
        if verdict == "reopen" and not task.get("owner_role"):
            state = "ready"
        tx.update(tid, {"state": state, "note": reason, "revision": task["revision"] + 1})
        if verdict == "stop" and task.get("owner_role"):
            _message(tx, "CANCEL", COORDINATOR, task["owner_role"], tid, None, [],
                     f"{reason} (external runs are not cancelled by this)")
        tx.event(f"task.{verdict}", tid, reason)
    return {"id": tid, "summary": f"{tid} -> {state}"}


def open_tasks(tx: Tx) -> list[dict]:
    return [t for t in tx.all("Task") if t["state"] in OPEN_STATES]
