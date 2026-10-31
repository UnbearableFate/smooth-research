"""The watcher: a round every agents.watch_minutes (default 20) over the agent sessions the coordinator opened,
recovering unexpected stops.

A session stopped unexpectedly when its agent is gone (it exited, or its tab was closed) while the record is still
open, or when the agent is idle while its assignment is unfinished and it waits for nothing: a researcher's task
ready or active, a review or advice ticket not answered. Normal pauses are left alone: a task recorded as
waiting (rl task wait, for example on cluster jobs) or blocked, a review the researcher asked for and has not got,
runs of the task's experiments still queued or running, and a message the session sent that has no answer yet (to
the coordinator: one it has not read). A gone agent is started again in its folder, in a new conversation that
reads its assignment from the records (rl agent brief); an idle one is reminded to continue, at most twice per
assignment. A session that keeps exiting or does not react to a reminder, and a dialog other than the trust
question for the session's own folder, go to the coordinator, which asks the user where the user decides. The
coordinator gets one recorded note for a round that changed or found something, and one reminder for messages it
has left unread for a whole round while idle. The watcher runs in its own herdr tab (sr-watch): rl agent open
starts it, and it stops when no session is open.
"""

from __future__ import annotations

import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, herdr
from .agents import _answer_trust, launch_args, new_tab
from .delegate import rl_command
from .project import ENV_PROJECT, watch_minutes
from .store import Store
from .tasks import _message
from .util import RLError, now_utc, parse_time

LABEL = "sr-watch"
MAX_RESTARTS = 3  # within an hour; a session that exits more often goes to the coordinator
MAX_REMINDERS = 2  # per assignment
OPEN_TICKET = ("pending", "running")
IN_PROGRESS = ("queued", "running")
NOT_USER = "(from the watcher, not from the user)"


def check(store: Store) -> dict:
    """One round: the state of every open session, recovering what it can. What the coordinator should know goes
    to it as one recorded note (with a notice)."""
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    with store.read() as tx:
        sessions = sorted((s for s in tx.all("Session", status="active") if s.get("herdr_agent")),
                          key=lambda s: s["id"])
    lines, news = [], []
    for s in sessions:
        try:
            line, tell = _round(store, s)
        except RLError as exc:
            line, tell = f"{s['role_id']}: not checked ({exc.message})", False
        lines.append(line)
        news += [line] if tell else []
    note = told = None
    if news:
        with store.write() as tx:
            msg = _message(tx, "NOTE", "watch", "coordinator", text="watch round: " + " | ".join(news))
        told = herdr.notify(store, msg)
        note = msg["id"]
    reminder = _coordinator(store, quiet=bool(told))
    head = f"{now_utc()} watch round: {len(sessions)} session(s)" + (f"; {note} to the coordinator" if note else "")
    return {"sessions": len(sessions), "news": news, "note": note, "reminder": reminder,
            "summary": "\n".join([head, *(f"  {x}" for x in lines), *([f"  {reminder}"] if reminder else [])])}


def _round(store: Store, s: dict) -> tuple[str, bool]:
    """One session: a line for the report, and whether the coordinator should hear it."""
    name, what = s["role_id"], s.get("assignment")
    info = herdr.agent(s["herdr_agent"])
    if info is None:
        return _restart(store, s)
    state, seq = info.get("agent_status") or "unknown", info.get("state_change_seq")
    if state == "blocked":
        why = _answer_trust(name, s["host"], Path(s["cwd"]), 90)
        if why is None:
            return f"{name}: answered the trust question for its folder", True
        return why, _once(store, s, "blocked", seq)
    if state not in ("idle", "done"):  # working, or a state herdr cannot tell
        return f"{name}: {state}" + (f" on {what}" if what else ""), False
    stopped, desc, problems = _assignment(store, s)
    if not stopped:
        fresh = [p for p in problems if _once(store, s, p.split()[0], p)]
        return f"{name}: {state}, {desc}" + "".join(f"; {p}" for p in problems), bool(fresh)
    memo = s.get("watch") or {}
    count = memo.get("reminders", 0) if memo.get("assignment") == what else 0
    if count and (memo.get("seq") == seq or count >= MAX_REMINDERS):
        return (f"{name}: {state} with {desc}; it did not continue after {count} reminder(s): look at it "
                f"(herdr agent read {name}), then reset it (rl agent reset {name} --clear, then assign) or close it"), \
            _once(store, s, "stuck", f"{what} {seq}")
    rl = rl_command()
    problem = herdr.prompt(s["herdr_agent"], f"[smooth-research watch] {name}: you stopped while {desc}. Continue it "
                                             f"(`{rl} agent brief {name}`). If you wait for something, record it and "
                                             f"end your turn: a run you started as running (`{rl} run record`), cluster "
                                             f"jobs with `{rl} task wait`, a blocker with `{rl} task block`. {NOT_USER}")
    _remember(store, s, ("session.reminded", s["id"], f"{name}: {desc}"), assignment=what, reminders=count + 1,
              seq=seq)
    return f"{name}: was {state} with {desc}; reminded it to continue" + (f" (not delivered: {problem})" if problem else ""), True


def _assignment(store: Store, s: dict) -> tuple[bool, str, list[str]]:
    """Is the assignment of an idle session unfinished, with nothing it waits for? Also a description, and what
    the coordinator should do about a wait (a review nobody is assigned to)."""
    name, what = s["role_id"], s.get("assignment")
    if not what:
        return False, "no assignment", []
    with store.read() as tx:
        x = tx.get(what)
        if x is None:
            return False, f"{what} not found", []
        open_ = x["status"] in OPEN_TICKET if s["role_kind"] != "researcher" else x["state"] in ("ready", "active")
        desc = f"{what} {x.get('status') or x.get('state')}"
        if not open_:  # done, or waiting or blocked as recorded: a normal pause
            return False, desc, []
        waits, problems = [], []
        if s["role_kind"] == "researcher":
            asked = [t for t in tx.all("Ticket", kind="review") if t.get("requested_by") == name
                     and t["status"] in OPEN_TICKET]
            waits += [f"review {', '.join(t['id'] for t in asked)}"] if asked else []
            problems += [f"{t['id']} has no reviewer session: rl agent open --role reviewer --ticket {t['id']}"
                         for t in asked if t["status"] == "pending" and not _live(tx, t.get("assigned_session"))]
            runs = _runs_in_progress(tx, what)
            waits += [f"runs {', '.join(runs)}"] if runs else []
        reply = _awaited_reply(tx, name)
        waits += [reply] if reply else []
    if waits:
        return False, f"{desc}, waiting for {'; '.join(waits)}", problems
    return True, desc, problems


def _live(tx, sid: str | None) -> bool:
    found = tx.get(sid, "Session") if sid else None
    return bool(found and found["status"] == "active")


def _runs_in_progress(tx, task_id: str) -> list[str]:
    """Attempts still queued or running of the experiments a task targets (directly or through a hypothesis)."""
    exps = set()
    for e in tx.out(task_id, "targets"):
        if e["label"] == "Experiment":
            exps.add(e["id"])
        elif e["label"] == "Hypothesis":
            exps.update(x["id"] for x in tx.inc(e["id"], "tests"))
    return sorted(a["id"] for x in exps for r in tx.all("Run", experiment_id=x)
                  for a in tx.all("Attempt", run_id=r["id"]) if a["execution_state"] in IN_PROGRESS)


def _awaited_reply(tx, name: str) -> str | None:
    """The answer a session waits for: its last message has no reply yet (one to the coordinator counts until the
    coordinator has read it)."""
    mine = [m for m in tx.all("Message") if name in (m["from_role"], m["to_role"])]
    last = max(mine, key=lambda m: m["seq"], default=None)
    if not last or last["from_role"] != name:
        return None
    if last["to_role"] == "coordinator":
        cursor = tx.get("CUR-coordinator", "Cursor")
        if last["seq"] <= (cursor["position"] if cursor else 0):
            return None
    return f"a reply to {last['id']} from {last['to_role']}"


def _restart(store: Store, s: dict) -> tuple[str, bool]:
    """Start a gone agent again in its folder (in its pane if that is still open, else in a new tab), in a new
    conversation, and tell it how to go on."""
    name = s["role_id"]
    now = datetime.now(timezone.utc)
    recent = [t for t in (s.get("watch") or {}).get("restarts", []) if (now - parse_time(t)).total_seconds() < 3600]
    if len(recent) >= MAX_RESTARTS:
        return (f"{name}: its agent exited again after {len(recent)} restarts within the hour; not started again: "
                f"look at it and decide (rl agent close {name}, or open a new session)"), \
            _once(store, s, "given_up", recent[0])
    tab, pane = s.get("herdr_tab"), s.get("herdr_pane")
    found = herdr.get("pane", pane) if pane else None
    ours = found and found.get("tab_id") == tab and (herdr.get("tab", tab) or {}).get("label") == name
    if ours and found.get("agent"):
        return (f"{name}: herdr no longer knows its agent by name, but its pane {pane} holds a {found['agent']} "
                f"agent: look at it (herdr pane read {pane}); rl agent close {name} closes the tab"), \
            _once(store, s, "unnamed", pane)
    if not ours:
        made = new_tab(store, s)
        tab, pane = made["tab"]["tab_id"], made["root_pane"]["pane_id"]
    entry = adapters.Entry.parse(s["entry"])
    host_session = str(uuid.uuid4()) if entry.entry in ("claude", "pi") else None
    waiting = None
    try:
        herdr.call("agent", "start", name, "--kind", entry.entry, "--pane", pane, "--timeout", "90000", "--",
                   *launch_args(store, entry, s["role_kind"], name, host_session), timeout=120)
    except RLError as exc:
        if exc.details.get("herdr_code") != "agent_not_ready":
            if not ours:
                herdr.call("tab", "close", tab, timeout=30)
            _remember(store, s, None, restarts=recent + [now_utc()])
            return f"{name}: its agent had exited and could not be started again: {exc.message}", True
        waiting = _answer_trust(name, entry.entry, Path(s["cwd"]), 90)
    with store.write() as tx:
        tx.update(s["id"], {"herdr_tab": tab, "herdr_pane": pane, "host_session_id": host_session})
    _remember(store, s, ("session.restarted", s["id"], f"{name} started again in {s['cwd']}"),
              restarts=recent + [now_utc()])
    if waiting:
        return f"{name}: its agent had exited; started again, but {waiting}", True
    what, rl = s.get("assignment"), rl_command()
    stopped, desc, _ = _assignment(store, s)
    if stopped:
        go_on = f"Your assignment is {what}: run `{rl} agent brief {name}` and continue it from the records. "
    elif what:
        go_on = f"The records show {desc}: run `{rl} agent brief {name}`, then end your turn and wait for the next message. "
    else:
        go_on = "Wait for the coordinator's next message. "
    problem = herdr.prompt(name, f"[smooth-research watch] {name}: your session had stopped and was started again "
                                 f"(a new conversation). {go_on}{NOT_USER}")
    return (f"{name}: its agent had exited; started again in {s['cwd']}"
            + (f" (not told: {problem})" if problem else f" and told to continue {what}" if stopped else "")), True


def _coordinator(store: Store, quiet: bool = False) -> str | None:
    """Remind an idle coordinator, once, of messages it has left unread for a whole round (quiet: a notice has
    just reached it, so only take note of them)."""
    with store.read() as tx:
        coords = [s for s in tx.all("Session", status="active") if s["role_id"] == "coordinator" and s.get("herdr_pane")]
        if not coords:
            return None
        c = max(coords, key=lambda x: x["id"])
        cursor = tx.get("CUR-coordinator", "Cursor")
        position = cursor["position"] if cursor else 0
        seqs = [m["seq"] for m in tx.all("Message", to_role="coordinator")]
    memo = c.get("watch") or {}
    old = [q for q in seqs if position < q <= memo.get("seen", 0) and q > memo.get("reminded", 0)]
    out, reminded = None, memo.get("reminded", 0)
    if old and not quiet:
        try:
            info = herdr.agent(c["herdr_pane"])
        except RLError:
            info = None
        if info and info.get("agent_status") in ("idle", "done"):
            problem = herdr.prompt(c["herdr_pane"], f"[smooth-research watch] {len(old)} message(s) have waited in "
                                                    f"your inbox for a whole round: `{rl_command()} inbox --role "
                                                    f"coordinator`, then --ack. {NOT_USER}")
            out = (f"coordinator: idle with {len(old)} message(s) unread for a round; reminded"
                   + (f" (not delivered: {problem})" if problem else ""))
            reminded = max(old)
    elif old:
        reminded = max(old)
    _remember(store, c, None, seen=max(seqs, default=0), reminded=reminded)
    return out


def _remember(store: Store, s: dict, event: tuple | None = None, **changes) -> None:
    """Keep the watcher's notes about a session (in Session.watch), with an event when it acted."""
    memo = {**(s.get("watch") or {}), **changes}
    with store.write() as tx:
        tx.update(s["id"], {"watch": memo})
        if event:
            tx.event(*event)
    s["watch"] = memo


def _once(store: Store, s: dict, key: str, value) -> bool:
    """Has the coordinator not heard this yet (the same condition is not reported every round)?"""
    told = (s.get("watch") or {}).get("told") or {}
    if told.get(key) == value:
        return False
    _remember(store, s, None, told={**told, key: value})
    return True


# -- the watcher's tab ----------------------------------------------------------------------------------------


def start(store: Store, every: float | None = None) -> dict:
    """Run the watcher in its own herdr tab, unless one for this project already runs."""
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    every = watch_minutes(store.project.config) if every is None else every
    if every <= 0:
        return {"tab": None, "started": False, "summary": "the watcher is off (agents.watch_minutes: 0)"}
    with store.read() as tx:
        meta = tx.meta()
    tab = meta.get("watch_tab")
    if tab and _ours(tab):
        beat, step = meta.get("watch_beat"), meta.get("watch_every") or every
        if beat and (datetime.now(timezone.utc) - parse_time(beat)).total_seconds() < (2 * step + 5) * 60:
            return {"tab": tab, "started": False, "summary": f"the watcher runs in tab {tab} (a round every {step:g} min)"}
        herdr.call("tab", "close", tab, timeout=30)  # it stopped reporting
    root = store.project.root
    made = herdr.call("tab", "create", "--cwd", str(root), "--label", LABEL, "--no-focus", "--env",
                      f"{ENV_PROJECT}={root}", "--env", f"SMOOTH_RESEARCH_RL={rl_command()}")
    tab, pane = made["tab"]["tab_id"], made["root_pane"]["pane_id"]
    with store.write() as tx:
        tx.set_meta(watch_tab=tab, watch_pane=pane, watch_beat=now_utc(), watch_every=every)
    herdr.call("pane", "run", pane, f"{shlex.quote(rl_command())} agent watch --own-tab --every {every:g} "
                                    f"--project {shlex.quote(str(root))}")
    return {"tab": tab, "started": True,
            "summary": f"the watcher runs in tab {tab}: a round every {every:g} min over the open sessions"}


def stop(store: Store) -> dict:
    with store.write() as tx:
        tab = tx.meta().get("watch_tab")
        tx.set_meta(watch_tab=None, watch_pane=None)
    note = ""
    try:
        if tab and herdr.problem() is None and _ours(tab):
            herdr.call("tab", "close", tab, timeout=30)
    except RLError as exc:
        note = f" (its tab: {exc.message}; the watcher leaves at its next round)"
    return {"stopped": bool(tab),
            "summary": f"the watcher in tab {tab} stopped{note}" if tab else "no watcher was running"}


def _ours(tab: str) -> bool:
    """Is this the watcher's tab (open, with the label it was given)?"""
    return (herdr.get("tab", tab) or {}).get("label") == LABEL


def run(store: Store, every: float | None = None, own_tab: bool = False) -> dict:
    """The watcher's loop: a round every few minutes until no session is open. In the tab that start() made
    (own_tab) it reports that it is alive, leaves when another watcher took over, and closes the tab at the end."""
    every = watch_minutes(store.project.config) if every is None else every
    if every <= 0:
        return {"summary": "the watcher is off (agents.watch_minutes: 0)"}
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    here = os.environ.get("HERDR_TAB_ID")
    print(f"{now_utc()} watching the agent sessions of {store.project.root}: a round every {every:g} min", flush=True)
    end = "no agent session is open"
    while True:
        time.sleep(every * 60)
        if own_tab:
            with store.read() as tx:
                registered = tx.meta().get("watch_tab")
            if registered != here:
                end = f"the watcher in tab {registered} took over" if registered else "the watcher was stopped"
                break
        try:
            out = check(store)
        except RLError as exc:
            print(f"{now_utc()} watch round failed: {exc.message}", flush=True)
            continue
        print(out["summary"], flush=True)
        if own_tab:
            with store.write() as tx:
                tx.set_meta(watch_beat=now_utc(), **({"watch_tab": None, "watch_pane": None} if not out["sessions"] else {}))
        if not out["sessions"]:
            break
    print(f"{now_utc()} {end}; the watcher stops", flush=True)
    if own_tab and here:
        herdr.call("tab", "close", here, timeout=30)
    return {"summary": f"{end}; the watcher stopped"}
