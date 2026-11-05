"""Agent sessions in herdr tabs: researchers, reviewers and advisors that the coordinator opens and closes.

Each session is a Claude Code, Codex or pi agent in its own herdr tab, recorded as a Session (entry, herdr agent and
tab, working directory, current assignment). The agent learns its work from `rl agent brief`, so the typed prompt
stays one line. A researcher works in its own git worktree (one per session, reused with the session) at
<project>/../<project>-worktrees/<name>, on a new branch per task made from the branch of the main checkout
(worktrees.py); the coordinator merges accepted branches. Sessions talk through recorded messages; a live recipient
gets a one-line notice. After an assignment the coordinator resets a session for reuse (compact or clear) or closes
it with its tab. A host's question whether to trust the folder rl opened the session in is answered for the
coordinator (launch.py); the sessions' other dialogs, questions and requests for permission go to the coordinator,
which decides what it can judge (rl agent dialog, rl agent answer). Without herdr the headless commands remain: rl
delegate, rl review run, rl advise run.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import adapters, herdr, messages, usage, watch, worktrees
from .delegate import SUBAGENTS, WAITING, prompt_for, rl_command
from .herdr import caller_role
from .launch import SELECTED, answer_new, excerpt, option_line, select, start_agent, unseen
from .project import stall_minutes, usable_entries, watch_minutes
from .store import Store
from .util import RLError, now_utc
from .worktrees import branch_status, prepare_worktree, switch_task

ROLE_KINDS = ("researcher", "reviewer", "advisor")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SLASH = {"claude": {"compact": "/compact", "clear": "/clear"}, "codex": {"compact": "/compact", "clear": "/new"},
         "pi": {"compact": "/compact", "clear": "/new"}}


# -- sessions -------------------------------------------------------------------------------------------------


def _sessions(store: Store, name: str | None = None, active: bool = True) -> list[dict]:
    with store.read() as tx:
        found = tx.all("Session", status="active") if active else tx.all("Session")
    return [s for s in found if s.get("role_kind") and (name is None or s["role_id"] == name)]


def _session(store: Store, name: str | None) -> dict:
    name = name or caller_role()
    if not name:
        raise RLError("INVALID", "name the session (its name is the one `rl agent open` printed)")
    found = _sessions(store, name)
    if not found:
        raise RLError("NOT_FOUND", f"no open agent session named {name}; `rl agent list` shows them")
    return found[-1]


def _free_name(store: Store, kind: str) -> str:
    used = {s["role_id"] for s in _sessions(store)}
    n = 1
    while f"{kind}-{n}" in used:
        n += 1
    return f"{kind}-{n}"


def _choose_entry(store: Store, kind: str, entry_text: str | None, ticket: dict | None,
                  instead_of: str | None = None) -> adapters.Entry:
    """The entry named, else the first one of the ticket or of models.<kind> that can run a session here: a
    researcher or reviewer not on a host whose subscription is low (advisors keep the rest), nor on the entry
    it replaces."""
    if entry_text:
        entries = [adapters.Entry.parse(entry_text)]
    elif ticket and ticket.get("candidates"):
        entries = [adapters.Entry.parse(text) for text in ticket["candidates"]]
    else:
        entries = usable_entries(store.project.config, kind)
    low = usage.low_hosts(store) if kind != "advisor" and not entry_text else {}
    tried = []
    for entry in entries:
        problem = ("a manual entry is answered by a person, not a session" if entry.entry == "manual"
                   else "the entry it replaces" if str(entry) == instead_of
                   else low[entry.entry]["reason"] if entry.entry in low
                   else adapters.available(entry))
        if not problem:
            return entry
        tried.append(f"{entry}: {problem}")
    raise RLError("UNAVAILABLE", f"no {kind} entry can run a session here: "
                                 + ("; ".join(tried) or f"models.{kind} lists none that parses")
                                 + ("; name one with --entry to use it anyway" if low else ""))


def open_session(store: Store, kind: str, name: str | None = None, entry_text: str | None = None,
                 task: str | None = None, ticket: str | None = None, worktree: bool = True,
                 base: str | None = None, timeout: int = 90) -> dict:
    """Open a session in a new herdr tab, record it, and give it its first assignment if one is named."""
    if kind not in ROLE_KINDS:
        raise RLError("INVALID", f"--role must be one of {', '.join(ROLE_KINDS)}")
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"agent sessions need herdr: {problem}. Without it use the headless "
                                     "commands: rl delegate T-x, rl review run RV-x, rl advise run AD-x ...")
    with store.read() as tx:
        if task:
            tx.require(task, "Task")
        tk = tx.require(ticket, "Ticket") if ticket else None
    name = name or _free_name(store, kind)
    if not NAME_RE.match(name):
        raise RLError("INVALID", f"session name {name!r}: lowercase letters, digits, - and _, starting with a letter")
    if _sessions(store, name) or name == "coordinator":
        raise RLError("CONFLICT", f"a session named {name} is already open; pick another name or close it first")
    entry = _choose_entry(store, kind, entry_text, tk)
    wt, cwd, branch = None, store.project.root, None
    if kind == "researcher" and worktree:
        wt, cwd, branch = prepare_worktree(store, name, task, base)
    with store.write() as tx:
        sid = tx.next_id("Session")
        record = tx.create("Session", {"id": sid, "role_id": name, "role_kind": kind, "host": entry.entry,
                                       "entry": str(entry), "model_requested": entry.model, "effort": entry.effort,
                                       "worktree": str(wt) if wt else None, "cwd": str(cwd), "branch": branch,
                                       "status": "active"})
        tx.event("session.opened", sid, f"{name} ({kind}) {entry} in {cwd}")
    try:
        started = start_agent(store, record, timeout)
    except RLError as exc:
        _end(store, sid, f"agent did not start: {exc.message}")
        raise
    tab_id, pane, waiting, trusted = started["tab"], started["pane"], started["waiting"], started["trusted"]
    with store.write() as tx:
        tx.update(sid, {"herdr_agent": name, "herdr_tab": tab_id, "herdr_pane": pane,
                        "host_session_id": started["host_session"]})
    where = f"worktree {wt} on {branch or 'a detached HEAD'}" if wt else f"in {cwd}"
    summary = f"{sid} {name} ({kind}, {entry}) open in herdr tab {tab_id}, {where}" + (f"; {trusted}" if trusted else "")
    if entry.entry == "codex" and not started["host_session"] and not waiting:
        summary += f"\n{name}: {watch.NO_ID}"
    out = {"id": sid, "name": name, "tab": tab_id, "pane": pane, "entry": str(entry), "worktree": str(wt) if wt else None,
           "branch": branch}
    if task or ticket:
        given = assign(store, name, task, ticket, deliver=not waiting)
        summary += "\n" + given["summary"]
    if waiting:
        summary += f"\n{waiting}; then `rl agent assign {name} {'--task ' + task if task else '--ticket ' + ticket if ticket else '...'}`"
    try:
        watching = watch.start(store)
        if watching.get("started"):
            summary += "\n" + watching["summary"]
    except RLError as exc:
        summary += f"\nthe watcher did not start ({exc.message}): `rl agent watch --start`"
    return {**out, "summary": summary}


def dialog(store: Store, name: str) -> dict:
    """What a session's agent shows now, for the coordinator to judge a dialog or a question in it."""
    s = _session(store, name)
    state = herdr.status(s["herdr_agent"])
    screen = herdr.read(s["herdr_agent"])
    shown = [x.rstrip() for x in screen.splitlines() if x.strip()][-40:]
    at = state == "blocked" or unseen(s["host"], screen)
    head = f"{name} is {state}" + (" at a dialog" if state == "blocked" else " at a dialog herdr does not see" if at else "")
    return {"name": name, "state": state, "screen": screen,
            "summary": head + (":\n" + "\n".join(shown) if shown else " (nothing on its screen)")
                       + (f"\nanswer it: rl agent answer {name} \"<option text or number>\" --reason \"<why>\""
                          if at else "")}


def answer(store: Store, name: str, option: str, reason: str, timeout: int = 30) -> dict:
    """The coordinator's answer to a dialog in a session: select the option, confirm it, record why. A session left
    idle with its assignment unfinished is then told to go on."""
    if caller_role():
        raise RLError("INVALID", f"answering a session's dialog is the coordinator's decision, not {caller_role()}'s: "
                                 f"tell the coordinator (rl agent tell coordinator \"..\")")
    if not (reason or "").strip():
        raise RLError("INVALID", "--reason: why you chose this option (it is recorded)")
    s = _session(store, name)
    target = s["herdr_agent"]
    state = herdr.status(target)
    screen = herdr.read(target)
    shown = unseen(s["host"], screen)  # a dialog herdr does not see: it reports the agent idle
    if state != "blocked" and not shown:
        raise RLError("CONFLICT", f"{name} is {state}, not at a dialog; `rl agent dialog {name}` shows its screen")
    problem = select(target, option, screen)
    if problem:
        raise RLError("NOT_FOUND", f"{name}: {problem}; `rl agent dialog {name}` shows the options")
    chosen = " ".join((option_line(herdr.read(target), option) or option).split()).lstrip("".join(SELECTED) + " ")
    herdr.keys(target, "enter")
    deadline, now = time.monotonic() + timeout, screen
    while time.monotonic() < deadline:
        state, now = herdr.status(target), herdr.read(target)
        if (state != "blocked" and not (shown and shown in now)) or option_line(now, option) is None:
            break  # the dialog is gone, or another one followed
        time.sleep(0.5)
    with store.write() as tx:
        tx.event("session.answered", s["id"], f"{name}: chose '{chosen}' at '{excerpt(screen, chars=200)}': {reason}")
    summary = f"{name}: chose '{chosen}' ({reason}); now {state}"
    if state == "blocked" or unseen(s["host"], now):
        summary += f"; it waits at a dialog again: `rl agent dialog {name}`"
    elif state in ("idle", "done") and s.get("assignment"):
        stopped, desc, _ = watch.assignment_state(store, s)
        if stopped:
            summary += f"; {desc}: " + _told(s, f"[smooth-research] {name}: your assignment is {s['assignment']}. "
                                                f"Run `{rl_command()} agent brief {name}` and follow it.")
    return {"id": s["id"], "chosen": chosen, "state": state, "summary": summary}


def _told(s: dict, text: str) -> str:
    """Type a prompt into a session and say whether it arrived (herdr saw it working, or its log a new turn)."""
    try:
        p = watch.probe(s)
    except RLError:
        p = {"status": "unknown", "seq": None, "log": None, "view": None}
    outcome, why = watch.deliver(s, p, text)
    if outcome == "delivered":
        return "told to read its brief"
    return f"not told ({why})" if outcome == "refused" else f"told to read its brief, but {why}"


def _end(store: Store, sid: str, note: str) -> None:
    with store.write() as tx:
        tx.update(sid, {"status": "ended", "ended_at": now_utc()})
        tx.event("session.closed", sid, note)


def assign(store: Store, name: str, task: str | None = None, ticket: str | None = None, deliver: bool = True) -> dict:
    """Give an open session its next assignment: a task for a researcher (on a new branch from the main
    checkout's branch), a review ticket for a reviewer, an advice ticket for an advisor."""
    s = _session(store, name)
    if bool(task) == bool(ticket):
        raise RLError("INVALID", "name exactly one assignment: --task T-x or --ticket RV-x|AD-x")
    kind = s["role_kind"]
    with store.read() as tx:
        if task:
            t = tx.require(task, "Task")
            if kind != "researcher":
                raise RLError("INVALID", f"{name} is a {kind}; tasks go to researcher sessions")
            if t.get("owner_role") not in (None, name):
                raise RLError("CONFLICT", f"{task} is owned by {t['owner_role']}; only that role can continue it")
            if t["state"] not in ("ready", "active", "waiting", "blocked"):
                raise RLError("CONFLICT", f"{task} is {t['state']}; reopen it first if it needs more work")
        else:
            t = tx.require(ticket, "Ticket")
            want = "reviewer" if t["kind"] == "review" else "advisor"
            if kind != want:
                raise RLError("INVALID", f"{ticket} is a {t['kind']} ticket; it goes to a {want} session")
            if t["status"] == "answered" and t["kind"] == "review":
                raise RLError("CONFLICT", f"{ticket} is already answered")
    branch = s.get("branch")
    if task and s.get("worktree"):
        branch = switch_task(store, Path(s["worktree"]), name, task)
    with store.write() as tx:
        if task and branch:
            tx.update(task, {"branch": branch})
        if ticket:
            tx.update(ticket, {"assigned_session": s["id"]})
        tx.update(s["id"], {"assignment": task or ticket, "branch": branch})
        tx.event("session.assigned", s["id"], f"{name} <- {task or ticket}")
    what = task or ticket
    line = f"{name} assigned {what}" + (f" on branch {branch}" if task and branch else "")
    if not deliver:
        return {"id": s["id"], "assignment": what, "summary": line + " (not yet told: the session is not ready)"}
    told = _told(s, f"[smooth-research] {name}: your assignment is {what}. Run `{rl_command()} agent brief "
                    f"{name}` and follow it.")
    return {"id": s["id"], "assignment": what, "summary": f"{line}; {told}"}


def tell(store: Store, to: str, text: str, sender: str | None = None, task: str | None = None) -> dict:
    """A short recorded message to a role or session; its live session, if any, gets a notice."""
    if not (text or "").strip():
        raise RLError("INVALID", "say something: the message text is empty")
    sender = sender or caller_role() or "coordinator"
    with store.write() as tx:
        if task:
            tx.require(task, "Task")
        msg = messages.record(tx, "NOTE", sender, to, task, None, [], text.strip())
    target = messages.notify(store, msg)
    return {"id": msg["id"], "delivered_to": target,
            "summary": f"{msg['id']} NOTE {sender} -> {to}: "
                       + (f"notice sent to {target}" if target else "recorded (no live session of that name: it "
                                                                    f"reads `rl inbox --role {to}`)")}


def reset(store: Store, name: str, mode: str) -> dict:
    """Prepare a session for its next assignment: compact keeps a summary of the conversation, clear starts a
    new one (/clear in Claude Code, /new in Codex and pi; Codex asks where the new one runs: in the session's own
    folder)."""
    s = _session(store, name)
    command = SLASH[s["host"]][mode]
    problem = herdr.prompt(s["herdr_agent"], command, wait_ms=600_000 if mode == "compact" else None)
    if problem:
        raise RLError("HERDR", f"{name}: {command} not sent: {problem}")
    waiting = answer_new(s["herdr_agent"], s["host"]) if mode == "clear" else None
    with store.write() as tx:
        tx.update(s["id"], {"assignment": None, **({"host_session_id": None} if mode == "clear" else {})})
        tx.event("session.reset", s["id"], f"{name} {command} after {s.get('assignment') or 'no assignment'}"
                 + (f"; {waiting}" if waiting else ""))
    return {"id": s["id"], "waiting": waiting,
            "summary": f"{s['id']} {name}: sent {command}; {waiting or f'ready for `rl agent assign {name} ...`'}"
                       + (" (a new host conversation: its id is not known)" if mode == "clear" else "")}


def _close_tab(s: dict) -> list[str]:
    """Close the session's herdr tab and the agent in it; notes on what was left."""
    if not s.get("herdr_tab"):
        return []
    try:
        tab = herdr.get("tab", s["herdr_tab"])
        if tab and tab.get("label") not in (None, s["role_id"]):  # herdr restarted and gave the id to another tab
            return [f"tab {s['herdr_tab']} is now {tab.get('label')!r}, not this session's: left open"]
        if tab:
            herdr.call("tab", "close", s["herdr_tab"], timeout=30)
    except RLError as exc:
        return [f"tab {s['herdr_tab']}: {exc.message}"]
    return []


def close(store: Store, name: str, remove_worktree: bool = False) -> dict:
    """End the session: close its herdr tab (the agent with it) and record the end. Its worktree stays unless
    asked, and is removed only when clean; branches are kept."""
    s = _session(store, name)
    notes = _close_tab(s)
    _end(store, s["id"], f"{name} closed")
    if s.get("branch"):
        notes.append(branch_status(store, s["branch"]))
    if remove_worktree and s.get("worktree"):
        try:
            worktrees.remove(store, s["worktree"])
            notes.append(f"worktree {s['worktree']} removed")
        except RLError as exc:
            notes.append(f"worktree kept: {exc.message}")
    elif s.get("worktree"):
        notes.append(f"worktree {s['worktree']} kept (a new session named {name} reuses it)")
    if not _sessions(store):
        stopped = watch.stop(store)
        notes += [stopped["summary"]] if stopped["stopped"] else []
    return {"id": s["id"], "summary": f"{s['id']} {name} closed" + "".join(f"\n  {n}" for n in notes)}


def switch(store: Store, name: str, entry_text: str | None = None, now: bool = False, timeout: int = 90) -> dict:
    """Continue a researcher's or reviewer's work on another model, usually because its host's subscription is
    low (usage.py): close the session, keeping its worktree, and open one of the same name on the next entry of
    its role (of the ticket, for a reviewer) on a host that is not low, with the same open assignment. A
    researcher hands its task over first (the watcher tells it to); the new session reads the handoff in its
    brief. Advisors keep their model: the rest of a low window is theirs."""
    if caller_role() not in (None, "coordinator"):
        raise RLError("INVALID", f"switching a session's model is the coordinator's decision, not {caller_role()}'s: "
                                 "tell the coordinator (rl agent tell coordinator \"..\")")
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"switching a session needs herdr: {problem}")
    s = _session(store, name)
    kind = s["role_kind"]
    if kind == "advisor":
        raise RLError("INVALID", f"{name} is an advisor: advisors keep their model (the rest of a low subscription "
                                 "window is left for them)")
    try:
        still = bool(s.get("herdr_agent")) and watch.working(watch.probe(s))
    except RLError:
        still = False  # herdr cannot tell: as before, the coordinator decides
    if still and not now:
        raise RLError("CONFLICT", f"{name} is still working (a session on a low host is told to finish and hand "
                                  f"over); switch it when it is idle, or with --now to stop it mid-turn")
    with store.read() as tx:
        x = tx.get(s["assignment"]) if s.get("assignment") else None
    task = x["id"] if x and kind == "researcher" and x.get("state") in ("ready", "active", "waiting", "blocked") \
        else None
    ticket = x if x and kind == "reviewer" and x.get("status") in ("pending", "running", "failed", "unavailable") \
        else None
    entry = _choose_entry(store, kind, entry_text, ticket, instead_of=s["entry"])
    low = usage.low_hosts(store).get(s["host"])
    why = low["reason"] if low else "the coordinator's choice"
    notes = _close_tab(s)
    _end(store, s["id"], f"{name} switched from {s['entry']} to {entry}: {why}")
    deadline = time.monotonic() + 15  # herdr frees the agent's name when the agent has exited
    while s.get("herdr_agent") and herdr.status(s["herdr_agent"]) not in ("gone", "not checked") \
            and time.monotonic() < deadline:
        time.sleep(0.5)
    try:
        opened = open_session(store, kind, name, str(entry), task, ticket["id"] if ticket else None,
                              worktree=bool(s.get("worktree")), timeout=timeout)
    except RLError as exc:
        again = (f"rl agent open {name} --role {kind} --entry {entry}" + (f" --task {task}" if task else "")
                 + (f" --ticket {ticket['id']}" if ticket else ""))
        raise RLError(exc.code, f"{name} ({s['entry']}) was closed, but the new session did not open: {exc.message}. "
                                f"Open it again: {again}") from None
    head = f"{name}: {s['entry']} -> {entry} ({why}); {s['id']} closed" + "".join(f"; {n}" for n in notes)
    return {**opened, "replaced": s["id"], "summary": head + "\n" + opened["summary"]}


def list_sessions(store: Store, include_all: bool = False) -> dict:
    """The sessions as the watcher sees them, without acting or writing: herdr's status, the class, what a session
    waits for, how long since its log showed progress, its last turn error, its retries and what the watcher does
    next; then the hosts' usage (also when no session is open) and the watcher."""
    sessions = _sessions(store, active=not include_all)
    live = herdr.problem() is None
    found = usage.hosts(store)
    low = {host: info["low"] for host, info in found.items() if info["low"]}
    stall, every = stall_minutes(store.project.config), watch_minutes(store.project.config)
    lines = []
    for s in sessions:
        state, detail = s["status"], ""
        if live and s["status"] == "active" and s.get("herdr_agent"):
            try:
                p = watch.probe(s)
                d = watch.describe(s, p, watch.classify(store, s, p, low, stall), every)
                state = p["status"]
                detail = (f"\n  {d['class']}" + (f": {d['desc']}" if d["desc"] else "")
                          + (f"; progress {d['progress']}" if d["progress"] else "")
                          + (f"; {d['log']}" if d["log"] else "")
                          + (f"; last error {d['error']['kind']}: {d['error']['message']}" if d["error"] else "")
                          + (f"; retries {d['retries']} of {watch.MAX_RETRIES}" if d["retries"] else "")
                          + f"; next: {d['next']}")
            except RLError as exc:
                state = "not checked"
                detail = f"\n  not checked: {exc.message}"
        lines.append(f"{s['id']} {s['role_id']} [{s['role_kind']}, {state}] {s.get('entry') or s['host']}"
                     + (f" tab {s['herdr_tab']}" if s.get("herdr_tab") else "")
                     + (f" assignment {s['assignment']}" if s.get("assignment") else "")
                     + (f" branch {s['branch']}" if s.get("branch") else "") + detail)
    lines = lines or ["no agent sessions open"]
    lines += ["usage:", *(f"  {x}" for x in usage.rows(store, found))]
    with store.read() as tx:
        meta = tx.meta()
    if meta.get("watch_tab"):
        lines.append(f"watcher: tab {meta['watch_tab']}, a beat every {meta.get('watch_every') or '?'} min, "
                     f"last {meta.get('watch_beat') or 'never'}")
    return {"sessions": sessions, "summary": "\n".join(lines)}


# -- briefs ---------------------------------------------------------------------------------------------------


def brief(store: Store, name: str | None = None) -> dict:
    """What the session does now: its role, its assignment and how it reports; printed for the agent itself."""
    s = _session(store, name)
    what = s.get("assignment")
    if not what:
        return {"summary": f"{s['role_id']} has no assignment now; wait for the coordinator's next message."}
    if s["role_kind"] == "researcher":
        return {"summary": prompt_for(store, what, s["role_id"], session=s)}
    with store.read() as tx:
        t = tx.require(what, "Ticket")
        package = store.project.abs(tx.require(t["request_ref"], "DocRevision")["object_path"])
        chain = [t]
        while chain[-1].get("recheck_of"):
            chain.append(tx.require(chain[-1]["recheck_of"], "Ticket"))
        researcher = t.get("requested_by") or "coordinator"
        sessions = {x["role_id"]: x for x in tx.all("Session", status="active")}
    text = _reviewer_brief if s["role_kind"] == "reviewer" else _advisor_brief
    return {"summary": text(store, s, t, package, chain, researcher, sessions.get(researcher))}


def _reviewer_brief(store: Store, s: dict, t: dict, package: Path, chain: list, counterpart: str,
                    other: dict | None) -> str:
    rl, rid, name = rl_command(), t["id"], s["role_id"]
    where = f" Its code is in {other['cwd']} (branch {other.get('branch') or '-'})." if other and other.get("cwd") else ""
    earlier = ("\nEarlier reviews in this round: " + "; ".join(f"{x['id']} {x.get('verdict') or x['status']}"
                                                              for x in chain[1:]) +
               ". Check that their findings are fixed and review what changed.") if len(chain) > 1 else ""
    return f"""You are {name}, an independent reviewer for the Smooth Research project at {store.project.root}.
The research records CLI is `{rl}`. Your assignment: {rid}, a {t.get('subject') or 'code'} review.
It is review {t.get('round_index') or 1} of at most 2 in the round that began with {t.get('round_of') or rid}.
It was requested by {counterpart}.{where}{earlier}

1. Read the package: {package}. It holds the question, the scope and the answer format, and either names
   the files under review (read them where they are) or holds the records under review.
2. You are read-only: read any file and run checks that change nothing; never edit, commit or delete files
   in the project or a worktree (scratch files only under /tmp). Put problems and suggested patches in your
   answer; {counterpart} makes the changes.
3. Give every finding a severity: high (the result would be wrong or the experiment invalid), medium (likely
   to distort the result or waste much of the budget; must be fixed or explicitly accepted), low (worth
   fixing, does not change the conclusion). Write the answer, ending with the JSON block, to a file and
   submit it: `{rl} review answer {rid} --answer <file>`.
4. Tell {counterpart} directly: `{rl} agent tell {counterpart} "{rid}: <verdict>, <n> finding(s), <k> of
   medium or higher; see the answer"`, and answer its questions the same way. When it asks for the second
   review of this round (a new review id with --recheck-of), review that one the same way.
5. A round has at most two reviews. When medium or higher findings remain after the second, the CLI hands the
   round to the coordinator, who approves it or returns the work; you need not argue further.
6. When the round is finished, tell the coordinator in one line (`{rl} agent tell coordinator "..."`) and
   wait: it decides whether this session reviews again, is reset or is closed.

{WAITING}

{SUBAGENTS}
"""


def _advisor_brief(store: Store, s: dict, t: dict, package: Path, chain: list, counterpart: str,
                   other: dict | None) -> str:
    rl, aid, name = rl_command(), t["id"], s["role_id"]
    return f"""You are {name}, one of the advisors of advice round {t.get('advice_round') or '-'} for the Smooth
Research project at {store.project.root}. The research records CLI is `{rl}`. Other advisors answer the same
package on their own; the coordinator weighs the answers and decides. The coordinator asked: {t.get('question')}

1. Read the package: {package}. It holds the goal, the key positive and negative evidence, the routes already
   stopped, the surviving explanations, the constraints, the nearest literature and an artifact index. You
   may read any project file; do not change anything (scratch files only under /tmp).
2. Assess the state and the evidence critically: the coordinator's reading may be wrong. Explore the related
   research broadly (search the web and the literature; name each source with a locator and say which claims
   you checked yourself). Find the bottleneck: what keeps the project from its next decision.
3. Propose improvements that differ in mechanism, or recommend pausing or stopping when no route is worth
   its cost. Follow the answer format at the end of the package; write the answer to a file and submit it:
   `{rl} advise answer {aid} --answer <file>`.
4. Tell the coordinator: `{rl} agent tell coordinator "{aid}: <recommendation in one line>"`. It may ask
   follow-up questions the same way; answer them with `{rl} agent tell coordinator "..."`. If your advice
   changes, submit the revised answer with the same `advise answer` command.
5. When the coordinator says it has your final advice, stop: it closes this session.

{WAITING}

{SUBAGENTS}
"""
