"""Sessions and tasks.

The versioned task package is what a session resumes from. Every task transition writes one message (TASK,
RESULT, WAIT, BLOCKED, HANDOFF, CANCEL; see messages.py), so a role can read its inbox without chat history.
"""

from __future__ import annotations

import os
import re

from . import documents, messages
from .research import prepare_contracts, seal_referenced
from .store import Store, Tx
from .util import RLError, now_utc
from .worktrees import branch_head, branch_status

COORDINATOR = "coordinator"
OPEN_STATES = ("ready", "active", "waiting", "blocked", "delivered")


# -- sessions -------------------------------------------------------------------------------------------------


def start_session(store: Store, role: str, host: str, host_session_id: str | None = None,
                  model_requested: str | None = None, model_observed: str | None = None,
                  effort: str | None = None, worktree: str | None = None) -> dict:
    """Register a session. A new coordinator session marks the earlier active ones superseded. A coordinator
    inside a herdr pane records that pane, so notices from other sessions reach it."""
    pane = os.environ.get("HERDR_PANE_ID") if role == COORDINATOR and os.environ.get("HERDR_ENV") == "1" else None
    with store.write() as tx:
        if host_session_id:
            for s in tx.all("Session", host_session_id=host_session_id, status="active"):
                if s["role_id"] == role:
                    return {"id": s["id"], "reused": True, "summary": f"{s['id']} already registered as {role}"}
        sid = tx.next_id("Session")
        if role == COORDINATOR:
            for s in tx.all("Session", status="active"):
                if s["role_id"] == COORDINATOR:
                    tx.update(s["id"], {"status": "superseded", "ended_at": now_utc()})
        tx.create("Session", {"id": sid, "role_id": role, "host": host, "host_session_id": host_session_id,
                              "model_requested": model_requested, "model_observed": model_observed,
                              "effort": effort, "worktree": worktree, "status": "active", "herdr_pane": pane,
                              "herdr_tab": os.environ.get("HERDR_TAB_ID") if pane else None})
        tx.event("session.start", sid, f"{role} on {host} {model_observed or model_requested or 'model unverified'}")
    return {"id": sid, "summary": f"{sid} {role} on {host}" + (f" (herdr pane {pane})" if pane else "")}


def end_session(store: Store, sid: str) -> dict:
    with store.write() as tx:
        s = tx.require(sid, "Session")
        if s["status"] == "active":
            tx.update(sid, {"status": "ended", "ended_at": now_utc()})
        tx.event("session.end", sid, s["role_id"])
    return {"id": sid, "summary": f"{sid} ended (running external work is not affected)"}


# -- tasks ----------------------------------------------------------------------------------------------------


def create_task(store: Store, title: str, kind: str, targets: list[str] = (), request: str | None = None,
                to_role: str | None = None, next_action: str | None = None, depends_on: list[str] = (),
                from_role: str = COORDINATOR) -> dict:
    """A new task. The contract of each experiment it targets is sealed now (a complete one), and the task is bound
    to that revision: its work pack shows what changed when the contract is revised later. A task on a hypothesis
    seals nothing below it."""
    prep = documents.prepare(store.project, request, "task_request") if request else None
    contracts = prepare_contracts(store, list(targets))
    with store.write() as tx:
        tid = tx.next_id("Task")
        ref = documents.register(tx, prep) if prep else None
        tx.create("Task", {"id": tid, "title": title, "kind": kind, "state": "ready", "to_role": to_role,
                           "request_ref": ref, "next_action": next_action, "revision": 1})
        bound, sealed = seal_referenced(tx, contracts, store.project)
        for target in targets:
            tx.link("targets", tid, target, **bound.get(target, {}))
        for dep in depends_on:
            tx.link("depends_on", tid, dep)
        msg = messages.record(tx, "TASK", from_role, to_role, tid, next_action, [ref] if ref else [], title,
                              base_revision=1) if to_role else None
        tx.event("task.created", tid, title)
    return {"id": tid, "request_ref": ref,
            "summary": f"{tid} ready: {title}" + (f" (request {ref})" if ref else "") + (f" -> {to_role}" if to_role else "")
                       + "".join(f"; {note}" for note in sealed) + messages.notice(store, msg)}


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
        # the branch to merge is the claimant's: another session may have been assigned the task before
        mine = [s for s in tx.all("Session", status="active")
                if s.get("role_id") == role and s.get("assignment") == tid]
        tx.update(tid, {"state": "active", "owner_role": role, "owner_session": session,
                        "branch": mine[-1].get("branch") if mine else None, "revision": task["revision"] + 1})
        tx.event("task.claimed", tid, role)
    warn = f"; waiting dependencies: {', '.join(open_deps)}" if open_deps else ""
    return {"id": tid, "summary": f"{tid} claimed by {role}{warn}"}


def _owner_write(store: Store, tid: str, docs: list[str], doc_type: str, from_role: str, fn) -> dict:
    """Seal the documents and apply fn in one write. Only the task's owner updates it, and not after the
    coordinator accepted or stopped it."""
    preps = [documents.prepare(store.project, d, doc_type) for d in docs]
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if not task.get("owner_role"):
            raise RLError("CONFLICT", f"{tid} is {task['state']} and has no owner; claim it first "
                                      f"(rl task claim {tid} --role <role>)", current_state=task["state"])
        if task["owner_role"] != from_role:
            raise RLError("CONFLICT", f"{tid} is owned by {task['owner_role']}, not {from_role}: only its owner "
                                      "updates it", current_state=task["state"])
        if task["state"] in ("accepted", "stopped"):
            raise RLError("CONFLICT", f"{tid} is {task['state']}: the coordinator closed it, and reopens it if it "
                                      f"needs more work (rl task reopen {tid})", current_state=task["state"])
        return fn(tx, task, [documents.register(tx, p) for p in preps])


VERIFIED = re.compile(r"^(?P<command>.+?) @(?P<commit>[0-9a-fA-F]{7,40}): (?P<outcome>pass|fail)(?:,? log (?P<log>\S.*))?$")


def parse_verified(text: str) -> dict:
    """A test run that a delivery records: "<command> @<commit>: pass|fail, log <path>"."""
    m = VERIFIED.match(text.strip())
    if not m:
        raise RLError("INVALID", f"--verified is \"<command> @<commit>: pass|fail, log <path>\", for example "
                                 f"\"uv run pytest -q tests @3f2c9a1: pass, log research/notes/T-0035-tests.log\"; "
                                 f"got {text!r}")
    return {"command": m["command"].strip(), "commit": m["commit"].lower(), "outcome": m["outcome"],
            "log": m["log"].strip() if m["log"] else None}


def progress(store: Store, tid: str, state: str, from_role: str, summary: str | None = None,
             outputs: list[str] = (), next_action: str | None = None, attempts: list[str] = (),
             reason: str | None = None, verified: list[str] = ()) -> dict:
    """deliver (RESULT, with the test runs that verified changed code), wait (WAIT on attempts), block (BLOCKED
    with a reason) or resume work (active)."""
    if state == "blocked" and not reason:
        raise RLError("INVALID", "blocked needs --reason engineering|model_unavailable|budget|science")
    if state == "waiting" and not next_action:
        raise RLError("INVALID", "waiting needs --next-action: what to do when the awaited facts arrive")
    checks = [parse_verified(v) for v in verified]

    def apply(tx: Tx, task: dict, refs: list[str]) -> dict:
        changes = {"state": state, "revision": task["revision"] + 1, "note": summary or task.get("note")}
        if next_action:
            changes["next_action"] = next_action
        if refs:
            changes["output_refs"] = (task.get("output_refs") or []) + refs
        if checks:
            changes["verified"] = (task.get("verified") or []) + checks
        if state == "waiting":
            for a in attempts:
                tx.require(a, "Attempt")
            changes["wait_attempts"] = list(attempts)
        changes["blocked_reason"] = reason if state == "blocked" else None
        tx.update(tid, changes)
        mtype = {"delivered": "RESULT", "waiting": "WAIT", "blocked": "BLOCKED"}.get(state)
        msg = messages.record(tx, mtype, from_role, COORDINATOR, tid, next_action, refs,
                              summary or (f"waiting on {', '.join(attempts)}" if attempts else reason),
                              base_revision=changes["revision"]) if mtype else None
        tx.event(f"task.{state}", tid, summary or next_action or reason or "")
        return {"id": tid, "outputs": refs, "message": msg,
                "summary": f"{tid} -> {state}" + (f" outputs {', '.join(refs)}" if refs else "")
                           + "".join(f"\nverified: {_check_line(c)}" for c in checks)}

    out = _owner_write(store, tid, list(outputs), "task_output", from_role, apply)
    out["summary"] += messages.notice(store, out.pop("message"))
    return out


def handoff(store: Store, tid: str, doc: str, from_role: str, to_role: str | None = None) -> dict:
    """Seal the handoff and release ownership; the task is ready to claim again."""
    def apply(tx: Tx, task: dict, refs: list[str]) -> dict:
        tx.update(tid, {"state": "ready", "owner_role": None, "owner_session": None, "handoff_ref": refs[0],
                        "revision": task["revision"] + 1, "to_role": to_role or task.get("to_role")})
        msg = messages.record(tx, "HANDOFF", from_role, to_role or task.get("to_role") or COORDINATOR, tid,
                              "read the handoff, verify code and run records, then continue", refs)
        tx.event("task.handoff", tid, refs[0])
        return {"id": tid, "handoff_ref": refs[0], "message": msg,
                "summary": f"{tid} handed off ({refs[0]}); ready to claim"}

    out = _owner_write(store, tid, [doc], "handoff", from_role, apply)
    out["summary"] += messages.notice(store, out.pop("message"))
    return out


def review_task(store: Store, tid: str, verdict: str, reason: str) -> dict:
    """Coordinator acceptance or stop. Stopping a research task does not cancel any external execution. A task
    worked on its own branch names the branch to merge."""
    msg = None
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if verdict == "accept" and task["state"] != "delivered":
            raise RLError("INVALID", f"{tid} is {task['state']}; only delivered work can be accepted")
        state = "accepted" if verdict == "accept" else "stopped" if verdict == "stop" else "active"
        if verdict == "reopen" and not task.get("owner_role"):
            state = "ready"
        tx.update(tid, {"state": state, "note": reason, "revision": task["revision"] + 1})
        if verdict == "stop" and task.get("owner_role"):
            msg = messages.record(tx, "CANCEL", COORDINATOR, task["owner_role"], tid, None, [],
                                  f"{reason} (external runs are not cancelled by this)")
        tx.event(f"task.{verdict}", tid, reason)
    merge = ""
    if verdict == "accept" and task.get("branch"):
        merge = "; " + branch_status(store, task["branch"])
    checked = verification_lines(store, task) if verdict == "accept" else []
    return {"id": tid, "summary": f"{tid} -> {state}{merge}" + messages.notice(store, msg)
                                  + "".join(f"\n{line}" for line in checked)}


def _check_line(c: dict) -> str:
    return f"{c['command']} @{c['commit'][:12]}: {c['outcome']}" + (f", log {c['log']}" if c.get("log") else "")


def verification_lines(store: Store, task: dict) -> list[str]:
    """What task accept says about the tests the delivery ran: the last recorded runs, and a note (never a
    refusal) when the task's branch has commits but none of its passing runs was of the branch's head."""
    checks = task.get("verified") or []
    lines = [f"verified: {_check_line(c)}" for c in checks[-3:]]
    head, ahead = branch_head(store, task["branch"]) if task.get("branch") else (None, 0)
    if head and ahead:
        passed = [c["commit"] for c in checks if c["outcome"] == "pass"]
        if not checks:
            lines.append(f"note: {task['branch']} has {ahead} commit(s) and the delivery recorded no test run "
                         "(rl task deliver --verified \"<command> @<commit>: pass|fail, log <path>\"); ask for one "
                         "before merging, or merge knowing it")
        elif not any(head.startswith(c) for c in passed):
            lines.append(f"note: the head of {task['branch']} ({head[:12]}) is not a commit whose recorded tests "
                         "passed: the code changed after the run, or the run failed")
    return lines


def open_tasks(tx: Tx) -> list[dict]:
    return [t for t in tx.all("Task") if t["state"] in OPEN_STATES]
