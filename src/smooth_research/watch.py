"""The watcher: one beat every agents.watch_minutes (default 5) over the agent sessions the coordinator opened and
over the coordinator itself, recovering unexpected stops.

A beat looks at herdr once per session (agent get: its status, state_change_seq and the host's session id, which
is kept as Session.host_session_id), reads what a Codex session appended to its own log since the last beat
(agentlog.py), and works out once which hosts are low on usage (usage.py). Each session is then in the first of
these classes that applies:

  exited      its agent is gone (it exited, or its tab was closed) while the record is open: started again in its
              folder, in a new conversation that reads its assignment from the records (rl agent brief), at most 3
              times an hour; on a low host it is not started again and the coordinator is asked to switch it
  dialog      it waits at a dialog: the trust question for the session's own folder is answered; any other
              dialog goes to the coordinator, once. Notices of messages that come meanwhile are held
              (messages.py): the first beat that finds it at no dialog tells it of them in one notice
  low usage   a researcher or reviewer on a low host: told once per window to finish (a researcher commits and
              hands its task over, a reviewer stops) and the coordinator to switch it when it is idle (rl agent
              switch); no retries, reminders or restarts there. Advisors keep the rest of the window
  turn error  its log shows that its last turn ended with an error. A transient one (the service was overloaded)
              is retried with a fixed prompt at the next beat, then after 2 and after 4 more beats: at most 3
              times per streak of errors, which ends only when a turn ends normally after some work (herdr's
              working alone does not end it); then the coordinator hears it. A used-up subscription is handled as
              low usage; any other error goes to the coordinator, once
  stalled     its turn is open with no tool call pending and no progress for agents.stall_minutes (30): the
              coordinator gets the end of its screen, once, and nothing is typed. A tool call pending 4 times
              that long is reported once too
  idle        idle while its assignment is unfinished and it waits for nothing (a researcher's task ready or
              active, a review or advice ticket not answered): reminded to continue once it stayed so for two
              beats in a row (the same state_change_seq), at most twice per assignment
  other       working, or a normal pause: a task recorded as waiting or blocked, a review the researcher asked for
              and has not got, runs of the task's experiments still queued or running, or a message the session
              sent that has no answer yet (to the coordinator: one it has not read)

When the log says the turn has ended, the log outweighs herdr's working. A prompt counts as delivered only when
herdr sees the agent working or at a dialog after it, or, for Codex, the log shows a new turn; a prompt that cannot
be confirmed is reported, not counted and not typed again until the session moves. The counters (reminders per
assignment, retries per streak of errors, restarts per hour) and what the coordinator was told once are kept in
Session.watch, read again inside the write that changes them; what was told is written with the note that tells it.

The coordinator is looked at too: its own turn that ended with a transient error and then stayed idle for a full
beat is retried by the same rules, and it is reminded once of messages left unread for a whole beat while idle. A
task waiting on runs that rl has heard nothing new of for agents.wait_check_minutes (60) goes to it as well, in case
the executor's notice that they ended never reaches it (a wrong target, a daemon that stopped): it is asked to check
them with their executor, again after twice as long without news, and so on. rl asks no executor itself. It
gets one recorded note for a beat that found something. A beat and rl agent check take the same lock: when it is
held, the other one skips. The watcher runs in its own herdr tab (sr-watch): rl agent open starts it, it stops
when no session is open and no task waits on runs, and it prints only the beats that found something.
"""

from __future__ import annotations

import fcntl
import math
import os
import shlex
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import agentlog, herdr, messages, usage
from .delegate import rl_command
from .launch import UNSEEN, answer_trust, excerpt, start_agent, unseen
from .project import DEFAULT_CONFIG, ENV_PROJECT, stall_minutes, wait_check_minutes, watch_minutes
from .store import Store
from .util import RLError, now_utc, parse_time

LABEL = "sr-watch"
MAX_RESTARTS = 3  # within an hour; a session that exits more often goes to the coordinator
MAX_REMINDERS = 2  # per assignment
MAX_RETRIES = 3  # per streak of transient turn errors
RETRY_BEATS = (0, 2, 4)  # beats before each retry: the next beat, then 2 more, then 4 more
TOOL_STALLS = 4  # a tool call pending this many times agents.stall_minutes is reported
OPEN_TICKET = ("pending", "running")
# Codex chooses its session's id; herdr reports it through its Codex integration (from the first turn of each
# conversation on), and Codex's /status shows it. Without either rl sees the session through herdr alone, and does
# not guess its log
NO_ID = ("rl found no Codex session id (herdr reports none, and /status showed none), so rl reads no log of it (no "
         "turn errors or retries, no log progress, its model unverified); herdr's Codex integration reports it: if "
         "`herdr integration status` does not list codex as installed, ask the user to install it (`herdr "
         "integration install codex`; it adds a hook to their Codex configuration)")
NO_ID_YET = ("rl has no Codex session id yet (herdr reports none before the first turn, and /status showed none in "
             "time); herdr's Codex integration is installed and reports it from the first turn on, and the watcher "
             "records it then. Until then rl reads no log of it (no turn errors or retries, no log progress, its "
             "model unverified)")


def no_id(screen: str = "") -> str:
    """Why rl has no id of a Codex session: none reported yet (the integration is installed), or no integration
    (the user installs it); with the end of the screen /status left, when there is one."""
    shown = f" (the screen: {excerpt(screen)})" if screen.strip() else ""
    return (NO_ID_YET if herdr.integration("codex") else NO_ID) + shown
IN_PROGRESS = ("queued", "running")
NOT_USER = "(from the watcher, not from the user)"
CONFIRM_S = 5.0  # how long the log of a Codex session that herdr already saw working is given to show a new turn
SCREEN_CHARS = 400


def target(s: dict) -> str | None:
    """The herdr target of a session: the agent's name for the sessions rl opened, the pane for the coordinator."""
    return s.get("herdr_agent") or s.get("herdr_pane")


# -- looking at a session -------------------------------------------------------------------------------------


def probe(s: dict) -> dict:
    """How a session is now: herdr's status ("gone" when no agent answers; "blocked" also at a dialog herdr does
    not see, named in "dialog"), state_change_seq and the host's session id (the one herdr reports, unless it is
    the stale one of a conversation reset left; else the recorded one), and what its log says. Raises RLError
    when herdr fails."""
    info = herdr.agent(target(s))
    memo = s.get("watch") or {}
    if info is None:
        return {"status": "gone", "seq": None, "sid": s.get("host_session_id"), "reported": None,
                "log": memo.get("log"), "view": None, "dialog": None}
    given = info.get("agent_session")
    reported = str(given["value"]) if isinstance(given, dict) and given.get("value") else None
    reported = None if reported == memo.get("stale") else reported  # an old conversation's, after a reset
    sid = reported or s.get("host_session_id")
    log = agentlog.follow(s.get("host") or "", sid, memo.get("log"))
    status, dialog = info.get("agent_status") or "unknown", None
    if status in ("idle", "done") and s.get("host") in UNSEEN:
        dialog = unseen(s.get("host"), herdr.read(target(s)))
    return {"status": "blocked" if dialog else status, "seq": info.get("state_change_seq"), "sid": sid,
            "reported": reported, "log": log, "view": agentlog.turn_view(log), "dialog": dialog}


def classify(store: Store, s: dict, p: dict, low: dict, stall: float, now: datetime | None = None) -> dict:
    """The class of a session (the first that applies; see the module's description), with what it is based on:
    {"kind", "low", "error", "view", "idle", "desc", "stopped", "problems", "quiet_min", "tool"}."""
    now = now or datetime.now(timezone.utc)
    kind = s.get("role_kind")
    short = low.get(s.get("host")) if kind in ("researcher", "reviewer") else None
    view = p.get("view")
    out = {"kind": "other", "low": short, "error": None, "view": view, "idle": False, "desc": "",
           "stopped": False, "problems": [], "quiet_min": None, "tool": None}
    if p["status"] == "gone":
        return {**out, "kind": "exited"}
    if p["status"] == "blocked":
        return {**out, "kind": "dialog"}
    if short:
        return {**out, "kind": "low"}
    if view and view["state"] == "error":
        return {**out, "kind": "error", "error": view["error"]}
    if working(p):
        if not view:
            return out
        quiet = _minutes(view.get("progress_at"), now)
        out["quiet_min"] = quiet
        pending = sorted(view["pending"].items(), key=lambda kv: kv[1].get("at") or "")
        if pending:
            call_id, call = pending[0]
            waited = _minutes(call.get("at"), now)
            if waited is not None and waited > TOOL_STALLS * stall:
                return {**out, "kind": "stalled", "tool": {"id": call_id, **call, "minutes": waited}}
            return out
        if quiet is not None and quiet > stall:
            return {**out, "kind": "stalled"}
        return out
    idle = p["status"] in ("idle", "done") or bool(view and view["state"] in ("ended", "aborted"))
    if not idle:  # a state herdr cannot tell, and no log that tells it
        return out
    stopped, desc, problems = assignment_state(store, s)
    return {**out, "kind": "idle" if stopped else "other", "idle": True, "stopped": stopped, "desc": desc,
            "problems": problems}


def working(p: dict) -> bool:
    """Is the session in a turn now? herdr's working, unless its log says the turn has ended (the log outweighs
    herdr); herdr's unknown, when the log shows an open turn."""
    view = p.get("view")
    if p["status"] == "working":
        return not (view and view["state"] != "open")
    return p["status"] == "unknown" and bool(view and view["state"] == "open")


def _minutes(at: str | None, now: datetime) -> float | None:
    try:
        return (now - parse_time(at)).total_seconds() / 60 if at else None
    except RLError:
        return None


def assignment_state(store: Store, s: dict) -> tuple[bool, str, list[str]]:
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
    coordinator has read it). Only a healthy idle session waits so: one whose turn failed does not."""
    mine = [m for m in tx.all("Message") if name in (m["from_role"], m["to_role"])]
    last = max(mine, key=lambda m: m["seq"], default=None)
    if not last or last["from_role"] != name:
        return None
    if last["to_role"] == "coordinator":
        cursor = tx.get("CUR-coordinator", "Cursor")
        if last["seq"] <= (cursor["position"] if cursor else 0):
            return None
    return f"a reply to {last['id']} from {last['to_role']}"


def describe(s: dict, p: dict, c: dict, every: float, now: datetime | None = None) -> dict:
    """What rl agent list shows of a session: its class, its assignment's state (with what it waits for), how
    long since its log showed progress, its last turn error, its retries and what the watcher does next."""
    now = now or datetime.now(timezone.utc)
    memo, view, name = s.get("watch") or {}, c["view"], s["role_id"]
    kind = {"exited": "exited", "dialog": "at a dialog", "low": "low usage", "error": "turn error",
            "stalled": "stalled", "idle": "idle, unfinished"}.get(c["kind"], "idle" if c["idle"] else "working"
                                                                   if p["status"] == "working" else p["status"])
    log = p.get("log")
    if s.get("host") != "codex":
        seen = "herdr only"  # no rule for the other hosts' logs yet
    elif not p.get("sid"):
        seen = ("log unavailable (no session id yet: herdr reports it from the first turn)" if herdr.integration("codex")
                else "log unavailable (no session id: herdr integration install codex)")
    else:
        seen = None if (log or {}).get("path") else "log not found"
    quiet = _minutes(view.get("progress_at"), now) if view else None
    retries = memo.get("retries") or {}
    nxt = {"exited": "start it again (or ask for a switch on a low host)", "dialog": "answer its own folder's trust "
           "question, else report it", "low": f"tell it to finish; then rl agent switch {name}",
           "stalled": "report its screen once", "idle": "remind it after two beats in a row"}.get(c["kind"], "nothing")
    if c["kind"] == "error":
        err = c["error"] or {}
        if err.get("kind") in agentlog.TRANSIENT:
            nxt = (f"retry {retries.get('count', 0) + 1} of {MAX_RETRIES}" + (f" at {retries['next_at']}"
                                                                             if retries.get("next_at") else "")
                   if retries.get("count", 0) < MAX_RETRIES else "report it: retries used up")
        else:
            nxt = "report it"
    return {"class": kind, "desc": c["desc"] or None,
            "progress": None if quiet is None else f"{quiet:.0f} min ago", "log": seen,
            "error": (view or {}).get("error") if view and view.get("state") == "error" else None,
            "retries": retries.get("count", 0), "next": nxt}


# -- one beat -------------------------------------------------------------------------------------------------


@contextmanager
def beat_lock(store: Store) -> Iterator[bool]:
    """The single actor: the watcher's beat and rl agent check hold this lock; True when it was free."""
    path = store.project.state_dir / "watch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing releases the lock


def check(store: Store) -> dict:
    """One beat: the state of every open session and of the coordinator, recovering what it can. What the
    coordinator should know goes to it as one recorded note (with a notice)."""
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    with beat_lock(store) as mine:
        if not mine:
            return {"skipped": True, "sessions": None, "news": [], "note": None, "reminder": None,
                    "summary": f"{now_utc()} watch beat skipped: another beat (the watcher's, or rl agent check) "
                               "is running"}
        return _Beat(store).run()


class _Beat:
    def __init__(self, store: Store):
        self.store = store
        config = store.project.config
        self.every = watch_minutes(config) or float(DEFAULT_CONFIG["agents"]["watch_minutes"])
        self.stall = stall_minutes(config)
        self.wait_check = wait_check_minutes(config)
        self.low = usage.low_hosts(store)
        self.marks: dict[str, dict] = {}  # what the coordinator is told this beat, per session: kept with the note

    def run(self) -> dict:
        store = self.store
        with store.read() as tx:
            sessions = sorted((s for s in tx.all("Session", status="active") if s.get("herdr_agent")),
                              key=lambda s: s["id"])
            coords = [s for s in tx.all("Session", status="active")
                      if s["role_id"] == "coordinator" and s.get("herdr_pane")]
        lines, news = [], []
        for s in sessions:
            try:
                line, tell = self.session(s)
            except RLError as exc:
                line, tell = f"{s['role_id']}: not checked ({exc.message})", False
            lines.append(line)
            news += [line] if tell else []
        coordinator = max(coords, key=lambda x: x["id"]) if coords else None
        if coordinator:
            try:
                line, tell = self.coordinator_turn(coordinator)
            except RLError as exc:
                line, tell = f"coordinator: not checked ({exc.message})", False
            if line:
                lines.append(line)
                news += [line] if tell else []
            waits = self.waits(coordinator)
            lines += waits
            news += waits
        note = told = None
        if news:
            with store.write() as tx:
                msg = messages.record(tx, "NOTE", "watch", "coordinator", text="watch beat: " + " | ".join(news))
                for sid, marks in self.marks.items():  # what was told is kept with the note that tells it
                    memo = tx.require(sid).get("watch") or {}
                    tx.update(sid, {"watch": {**memo, "told": {**(memo.get("told") or {}), **marks}}})
            told = messages.notify(store, msg)
            note = msg["id"]
        reminder = self.unread(coordinator, quiet=bool(told)) if coordinator else None
        head = f"{now_utc()} watch beat: {len(sessions)} session(s)" + (f"; {note} to the coordinator" if note else "")
        summary = "\n".join([head, *(f"  {x}" for x in lines), *([f"  {reminder}"] if reminder else [])])
        return {"sessions": len(sessions), "news": news, "note": note, "reminder": reminder, "summary": summary}

    # -- what the coordinator hears once --

    def told(self, s: dict, key: str, value) -> bool:
        return ((s.get("watch") or {}).get("told") or {}).get(key) == value

    def once(self, s: dict, key: str, value) -> bool:
        """Has the coordinator not heard this yet? The mark is written with this beat's note."""
        if self.told(s, key, value):
            return False
        self.marks.setdefault(s["id"], {})[key] = value
        return True

    # -- a session --

    def session(self, s: dict) -> tuple[str, bool]:
        """One session: a line for the report, and whether the coordinator should hear it."""
        p = probe(s)
        self.settle(s, p)
        c = classify(self.store, s, p, self.low, self.stall)
        if c["kind"] != "idle" and (s.get("watch") or {}).get("idle"):
            remember(self.store, s, idle=None)  # two beats in a row: the count starts again
        if c["kind"] not in ("dialog", "exited") and (s.get("watch") or {}).get("notices"):
            released = messages.release(self.store, s)  # notices held while it waited at a dialog
            if released and released[0]:
                return f"{s['role_id']}: {released[1]}", False  # the notice moves it: the rest waits a beat
        return getattr(self, f"on_{c['kind']}")(s, p, c)

    def settle(self, s: dict, p: dict) -> None:
        """Keep what the probe found: the host's session id (a new one starts over: what was counted for the old
        conversation and its log no longer applies), the log's place, the end of a streak of errors, and a prompt
        whose delivery was not confirmed but which the session has since reacted to."""
        memo, fields, changes, event = s.get("watch") or {}, {}, {}, None
        if p["reported"] and memo.get("stale"):
            changes["stale"] = None  # herdr reports the new conversation now
        if p["reported"] and p["reported"] != s.get("host_session_id"):
            fields["host_session_id"] = p["reported"]
            changes.update(retries=None, unconfirmed=None, idle=None)
            if s.get("host_session_id"):
                event = ("session.bound", s["id"], f"{s['role_id']}: host session {s.get('host_session_id')} -> "
                                                   f"{p['reported']}")
        if p["log"] is not None and p["log"] != memo.get("log"):
            changes["log"] = p["log"]
        view, retries = p.get("view"), memo.get("retries")
        if retries and view and view["state"] == "ended" and view["worked"] and view["turn"] != retries.get("last"):
            changes["retries"] = None  # a turn ended normally after some work: the streak of errors is over
        pending = memo.get("unconfirmed")
        if pending and "unconfirmed" not in changes:
            moved = p["seq"] is not None and p["seq"] != pending.get("seq")
            started = bool(view and view.get("started") and _after(view["started"], pending.get("at")))
            if moved or started or s.get("assignment") != pending.get("assignment"):
                changes["unconfirmed"] = None
                if (moved or started) and s.get("assignment") == pending.get("assignment"):
                    changes.update(pending.get("apply") or {})  # it arrived after all: count it now
        if fields or changes or event:
            remember(self.store, s, event, fields, **changes)

    def say(self, s: dict, p: dict, text: str, what: str, apply: dict, event: tuple) -> tuple[str, str | None]:
        """Type a prompt and count it (apply: the changes to the session's watch notes, with the event) only when
        it was delivered; one whose delivery is not confirmed is held: counted once the session reacts to it, and
        nothing else is typed until then."""
        outcome, why = deliver(s, p, text)
        if outcome == "delivered":
            remember(self.store, s, event, **apply)
        elif outcome == "unconfirmed":
            remember(self.store, s, None, unconfirmed={"what": what, "seq": p.get("seq"), "at": now_utc(),
                                                       "assignment": s.get("assignment"), "apply": apply,
                                                       "why": why})
        return outcome, why

    def held(self, s: dict) -> tuple[str, bool] | None:
        """The line for a session whose last prompt is not confirmed: nothing more is typed into it meanwhile."""
        pending = (s.get("watch") or {}).get("unconfirmed")
        if not pending:
            return None
        return (f"{s['role_id']}: its {pending['what']} typed at {pending['at']} is not confirmed ({pending['why']}); "
                f"nothing more is typed until it reacts: look at it (herdr agent read {s['role_id']})"), \
            self.unconfirmed(s)

    def unconfirmed(self, s: dict) -> bool:
        """The coordinator hears once of a prompt whose delivery is not confirmed."""
        return self.once(s, "unconfirmed", ((s.get("watch") or {}).get("unconfirmed") or {}).get("at"))

    def on_exited(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        name, low = s["role_id"], c["low"]
        if low:  # not started again on a host that is nearly used up
            return (f"{name}: its agent exited, and {low['reason']}: not started again there; continue its work "
                    f"on another model: rl agent switch {name}"), self.once(s, "usage_gone", low["key"])
        return self.restart(s)

    def on_dialog(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        name = s["role_id"]
        why = answer_trust(name, s["host"], Path(s["cwd"]), 90)
        if why is None:
            return f"{name}: answered the trust question for its folder", True
        return why, self.once(s, "blocked", p["seq"])

    def on_low(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        """A researcher or reviewer on a host whose subscription is low: told to finish so that another model
        takes its work over (once per window, and again at the next beat while it certainly was not typed); the
        coordinator hears it once and switches the session when it is idle."""
        name, what, low = s["role_id"], s.get("assignment"), c["low"]
        key, told = low["key"], (s.get("watch") or {}).get("told") or {}
        text, problem = _finish(self.store, s), None
        held = bool((s.get("watch") or {}).get("unconfirmed"))  # nothing more is typed until it reacts
        if text and told.get("usage_agent") != key and not held:
            outcome, why = self.say(s, p, f"[smooth-research watch] {name}: {low['reason']}, so another model "
                                          f"continues your work. {text} {NOT_USER}", "notice to finish",
                                    {"told": {**told, "usage_agent": key}},
                                    ("session.told_to_finish", s["id"], f"{name}: {low['reason']}"))
            problem = None if outcome == "delivered" else why
        state = ("nothing open to finish" if not text else f"not yet told to finish {what} ({problem})" if problem
                 else f"its notice to finish {what} is not confirmed" if held else f"told to finish {what}")
        return (f"{name} ({s['entry']}): {low['reason']}; {state}; when it is idle: rl agent switch {name}",
                self.once(s, "usage", key))

    def on_error(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        name, err, view = s["role_id"], c["error"], c["view"]
        if err["kind"] in agentlog.QUOTA:
            then = (f"continue its work on another model: rl agent switch {name}"
                    if s.get("role_kind") in ("researcher", "reviewer")
                    else "an advisor keeps its model: wait for the window to reset, or close it")
            return (f"{name}: its last turn failed because its subscription is used up ({err['message']}); "
                    f"{then}"), self.once(s, "quota", view["turn"])
        if err["kind"] not in agentlog.TRANSIENT:
            return (f"{name}: its last turn ended with an error ({err['kind']}: {err['message']}); nothing was "
                    f"typed: look at it (herdr agent read {name})"), self.once(s, "error", view["turn"])
        return self.retry(s, p, view)

    def retry(self, s: dict, p: dict, view: dict) -> tuple[str, bool]:
        """A turn cut off by a transient service error: typed again at the next beat, then after 2 and 4 more
        beats; after 3 retries in one streak the coordinator hears it."""
        name, err = s["role_id"], view["error"]
        r = (s.get("watch") or {}).get("retries") or {}
        count, failed = r.get("count", 0), f"its last turn failed ({err['kind']}: {err['message']})"
        if r.get("last") == view["turn"]:
            return f"{name}: {failed}; retried at {r.get('at')}, its new turn has not started yet", False
        if count >= MAX_RETRIES:
            if s.get("role_kind") in ("researcher", "reviewer"):
                then = f"continue its work on another model: rl agent switch {name}"
            elif s.get("role_kind") == "advisor":
                then = f"look at it (herdr agent read {name}) and decide; an advisor keeps its model"
            else:
                then = "look at it and decide"
            return (f"{name}: {failed} again after {count} retries; not retried again: {then}"), \
                self.once(s, "retries", r.get("first"))
        held = self.held(s)
        if held:
            return held
        due = r.get("next_at")
        if due and datetime.now(timezone.utc) < parse_time(due) - timedelta(seconds=30):
            return f"{name}: {failed}; retry {count + 1} of {MAX_RETRIES} at {due}", False
        n = count + 1
        wait = RETRY_BEATS[n] if n < len(RETRY_BEATS) else 0
        later = (datetime.now(timezone.utc) + timedelta(minutes=wait * self.every)).isoformat(timespec="seconds")
        apply = {"retries": {"count": n, "last": view["turn"], "first": r.get("first") or view["turn"],
                             "at": now_utc(), "next_at": later}}
        text = (f"[smooth-research watch] {name}: your last turn was cut off by a model service error "
                f"({err['kind']}: {err['message']}). Continue from where you stopped"
                + (f" (`{rl_command()} agent brief {name}` shows your assignment)" if s.get("herdr_agent") else "")
                + f". {NOT_USER}")
        outcome, why = self.say(s, p, text, f"retry {n}", apply,
                                ("session.retried", s["id"], f"{name}: retry {n} after {err['kind']}"))
        if outcome == "delivered":
            return f"{name}: {failed}; retried ({n} of {MAX_RETRIES})", True
        if outcome == "unconfirmed":
            return f"{name}: {failed}; retry {n} typed, but {why}", self.unconfirmed(s)
        return f"{name}: {failed}; retry {n} not typed: {why}", self.once(s, "retry_refused", view["turn"])

    def on_stalled(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        name, view = s["role_id"], c["view"]
        if c["tool"]:
            call = c["tool"]
            line = (f"{name}: its tool call {call['name']} has been pending for {call['minutes']:.0f} min (since "
                    f"{call['at']}); nothing was typed: look at it (herdr agent read {name})")
            return line, self.once(s, "tool", call["id"])
        key = f"{view['turn']} {view.get('progress_at')}"
        line = (f"{name}: its turn shows no progress for {c['quiet_min']:.0f} min (since {view.get('progress_at')}) "
                f"with no tool call pending; nothing was typed")
        if self.told(s, "stalled", key):
            return line, False
        screen = herdr.read(target(s))  # read once, for the report
        return line + f"; its screen ends: {excerpt(screen, SCREEN_CHARS)}", self.once(s, "stalled", key)

    def on_idle(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        name, what, seq, desc = s["role_id"], s.get("assignment"), p["seq"], c["desc"]
        memo = s.get("watch") or {}
        held = self.held(s)
        if held:
            return held
        first = memo.get("idle") or {}
        if first.get("seq") != seq or first.get("assignment") != what:
            remember(self.store, s, idle={"seq": seq, "assignment": what})
            return f"{name}: idle with {desc}; reminded at the next beat if it is still idle then", False
        count = _reminders(memo, what)
        if count >= MAX_REMINDERS:
            return (f"{name}: idle with {desc}; it did not continue after {count} reminder(s): look at it "
                    f"(herdr agent read {name}), then reset it (rl agent reset {name} --clear, then assign) or "
                    f"close it"), self.once(s, "stuck", f"{what} {seq}")
        rl = rl_command()
        text = (f"[smooth-research watch] {name}: you stopped while {desc}. Continue it (`{rl} agent brief {name}`). "
                f"If you wait for something, record it and end your turn: a run you started as running (`{rl} run "
                f"record`), cluster jobs with `{rl} task wait`, a blocker with `{rl} task block`. A question or a "
                f"request for permission goes to the coordinator (`{rl} agent tell coordinator`), not to the user. "
                f"{NOT_USER}")
        apply = {"reminders": {"assignment": what, "count": count + 1}, "idle": None}
        outcome, why = self.say(s, p, text, "reminder", apply,
                                ("session.reminded", s["id"], f"{name}: {desc}"))
        if outcome == "delivered":
            return f"{name}: was idle with {desc} for two beats; reminded it to continue", True
        if outcome == "unconfirmed":
            return f"{name}: was idle with {desc}; a reminder was typed, but {why}", self.unconfirmed(s)
        return f"{name}: idle with {desc}; not reminded: {why}", self.once(s, "remind_refused", f"{what} {seq}")

    def on_other(self, s: dict, p: dict, c: dict) -> tuple[str, bool]:
        name, what = s["role_id"], s.get("assignment")
        if not c["idle"]:  # working, or a state herdr cannot tell
            return f"{name}: {p['status']}" + (f" on {what}" if what else ""), False
        problems, held = c["problems"], (s.get("watch") or {}).get("problems") or []
        if held != problems:
            remember(self.store, s, problems=problems)
        # what the coordinator should resolve goes to it when it is still there a beat later, and once
        due = [x for x in problems if x in held and self.once(s, x.split()[0], x)]
        return f"{name}: idle, {c['desc']}" + "".join(f"; {x}" for x in problems), bool(due)

    def restart(self, s: dict) -> tuple[str, bool]:
        """Start a gone agent again in its folder (in its pane if that is still open, else in a new tab), in a new
        conversation, and tell it how to go on."""
        name = s["role_id"]
        now = datetime.now(timezone.utc)
        recent = [t for t in (s.get("watch") or {}).get("restarts", []) if (now - parse_time(t)).total_seconds() < 3600]
        if len(recent) >= MAX_RESTARTS:
            return (f"{name}: its agent exited again after {len(recent)} restarts within the hour; not started "
                    f"again: look at it and decide (rl agent close {name}, or open a new session)"), \
                self.once(s, "given_up", recent[0])
        if not _open(self.store, s["id"]):  # the coordinator closed it during this beat
            return f"{name}: closed", False
        tab, pane = s.get("herdr_tab"), s.get("herdr_pane")
        found = herdr.get("pane", pane) if pane else None
        ours = found and found.get("tab_id") == tab and (herdr.get("tab", tab) or {}).get("label") == name
        if ours and found.get("agent"):
            return (f"{name}: herdr no longer knows its agent by name, but its pane {pane} holds a {found['agent']} "
                    f"agent: look at it (herdr pane read {pane}); rl agent close {name} closes the tab"), \
                self.once(s, "unnamed", pane)
        try:
            started = start_agent(self.store, s, 90, tab, pane) if ours else start_agent(self.store, s, 90)
        except RLError as exc:
            remember(self.store, s, restarts=recent + [now_utc()])
            return f"{name}: its agent had exited and could not be started again: {exc.message}", True
        if not _open(self.store, s["id"]):  # closed while it started; rl agent close closed the recorded tab only
            if not ours:
                herdr.call("tab", "close", started["tab"], timeout=30)
            return f"{name}: closed while the watcher started it again", False
        remember(self.store, s, ("session.restarted", s["id"], f"{name} started again in {s['cwd']}"),
                 {"herdr_tab": started["tab"], "herdr_pane": started["pane"],
                  "host_session_id": started["host_session"]},
                 restarts=recent + [now_utc()], retries=None, unconfirmed=None, idle=None, log=None)
        if started["waiting"]:
            return f"{name}: its agent had exited; started again, but {started['waiting']}", True
        what, rl = s.get("assignment"), rl_command()
        stopped, desc, _ = assignment_state(self.store, s)
        if stopped:
            go_on = f"Your assignment is {what}: run `{rl} agent brief {name}` and continue it from the records. "
        elif what:
            go_on = (f"The records show {desc}: run `{rl} agent brief {name}`, then end your turn and wait for the "
                     f"next message. ")
        else:
            go_on = "Wait for the coordinator's next message. "
        outcome, why = deliver(s, {"status": "idle", "seq": None, "log": None, "view": None},
                               f"[smooth-research watch] {name}: your session had stopped and was started again "
                               f"(a new conversation). {go_on}{NOT_USER}")
        if outcome == "refused":
            told = f" (not told: {why})"
        elif outcome == "unconfirmed":
            told = f" (told, but {why})"
        else:
            told = f" and told to continue {what}" if stopped else ""
        return f"{name}: its agent had exited; started again in {s['cwd']}{told}", True

    # -- the coordinator --

    def coordinator_turn(self, c: dict) -> tuple[str | None, bool]:
        """The coordinator's own turn: one that ended with a transient error and stayed idle for a full beat is
        retried by the same rules as a session's."""
        p = probe(c)
        self.settle(c, p)
        view = p.get("view")
        memo = c.get("watch") or {}
        failed = bool(view and view["state"] == "error" and view["error"]["kind"] in agentlog.TRANSIENT
                      and p["status"] != "blocked" and p["status"] != "gone")
        if not failed:
            if memo.get("error_seen"):
                remember(self.store, c, error_seen=None)
            return None, False
        if memo.get("error_seen") != view["turn"]:
            remember(self.store, c, error_seen=view["turn"])
            return (f"coordinator: its last turn failed ({view['error']['kind']}); retried at the next beat if it is "
                    f"still idle then"), False
        return self.retry(c, p, view)

    def waits(self, c: dict) -> list[str]:
        """Tasks waiting on runs that rl has heard nothing new of (no observation, no change to the task) for
        agents.wait_check_minutes: the coordinator is asked to check them with their executor, once after that long,
        once after twice as long, and so on; news of them starts the count again."""
        if not self.wait_check:
            return []
        now, out = datetime.now(timezone.utc), []
        with self.store.read() as tx:
            for t in tx.all("Task", state="waiting"):
                runs = [a for a in (tx.get(x, "Attempt") for x in t.get("wait_attempts") or [])
                        if a and a["execution_state"] in IN_PROGRESS]
                times = [_time(x) for x in (t.get("updated_at"), *(a.get("last_observed_at") for a in runs))]
                times = [x for x in times if x]
                if not runs or not times:
                    continue
                since = max(times)
                quiet = (now - since).total_seconds() / 60
                if quiet < self.wait_check:
                    continue
                level = int(math.log2(quiet / self.wait_check))  # 0 from 1x, 1 from 2x, 2 from 4x ...
                if not self.once(c, f"wait {t['id']}", f"{since.isoformat()} {level}"):
                    continue
                jobs = ", ".join(f"{a['id']} ({', '.join(_job(tx.require(e['id'])) for e in tx.out(a['id'], 'bound_to')) or 'no job id recorded'})"
                                 for a in runs)
                reach = f" (yours is herdr target {c['herdr_pane']})" if c.get("herdr_pane") else ""
                out.append(f"{t['id']} waits on {jobs}, and rl has heard nothing new of them for {_span(quiet)}: if "
                           f"no notice that they ended has reached you, check them with their executor, record what "
                           f"it reports, and check where its notice goes{reach}")
        return out

    def unread(self, c: dict, quiet: bool = False) -> str | None:
        """Remind an idle coordinator, once, of messages it has left unread for a whole beat (quiet: a notice has
        just reached it, so only take note of them)."""
        with self.store.read() as tx:
            c = tx.require(c["id"])
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
                outcome, why = deliver(c, {"status": info.get("agent_status"), "seq": info.get("state_change_seq"),
                                           "log": None, "view": None},
                                       f"[smooth-research watch] {len(old)} message(s) have waited in your inbox for a "
                                       f"whole beat: `{rl_command()} inbox --role coordinator`, then --ack. {NOT_USER}")
                out = (f"coordinator: idle with {len(old)} message(s) unread for a beat; "
                       + ("reminded" if outcome == "delivered" else f"reminder {outcome}: {why}"))
                reminded = max(old) if outcome != "refused" else reminded  # typed: not typed again
        elif old:
            reminded = max(old)
        remember(self.store, c, seen=max(seqs, default=0), reminded=reminded)
        return out


def waits_on_runs(tx) -> bool:
    """Does a task wait on runs still queued or running, with a coordinator in herdr to tell if no news of them
    comes? The watcher keeps beating for it after the last session closed."""
    if not any(s["role_id"] == "coordinator" and s.get("herdr_pane") for s in tx.all("Session", status="active")):
        return False
    return any(a and a["execution_state"] in IN_PROGRESS for t in tx.all("Task", state="waiting")
               for a in (tx.get(x, "Attempt") for x in t.get("wait_attempts") or []))


def _time(at: str | None) -> datetime | None:
    try:
        return parse_time(at) if at else None
    except RLError:
        return None


def _job(ref: dict) -> str:
    return f"{ref['executor_namespace']}/{ref['opaque_id']}"


def _span(minutes: float) -> str:
    return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"


def _finish(store: Store, s: dict) -> str | None:
    """What a researcher or reviewer does before another model takes its work over; None when nothing is open."""
    name, what, rl = s["role_id"], s.get("assignment"), rl_command()
    with store.read() as tx:
        x = tx.get(what) if what else None
    if not x:
        return None
    if s["role_kind"] == "researcher" and x.get("owner_role") == name \
            and x["state"] not in ("delivered", "accepted", "stopped"):
        return (f"Stop at a safe point now: commit your work on your branch, write a handoff note (what is done, "
                f"what is next, what to check first) and hand the task over: `{rl} task handoff {what} --role "
                f"{name} --doc <note>`. Then end your turn.")
    if s["role_kind"] == "researcher" and x.get("state") == "ready":
        return f"Do not claim {what}: end your turn."
    if s["role_kind"] == "reviewer" and x.get("status") in OPEN_TICKET:
        return f"Stop now without answering {what} and end your turn."
    return None


def _reminders(memo: dict, what: str | None) -> int:
    """Reminders delivered for this assignment ({"assignment", "count"}; older notes kept a bare count)."""
    r = memo.get("reminders")
    if isinstance(r, dict):
        return r.get("count", 0) if r.get("assignment") == what else 0
    return r if isinstance(r, int) and memo.get("assignment") == what else 0


def _after(at: str | None, since: str | None) -> bool:
    try:
        return bool(at and since) and parse_time(at) >= parse_time(since)
    except RLError:
        return False


def _open(store: Store, sid: str) -> bool:
    with store.read() as tx:
        found = tx.get(sid, "Session")
    return bool(found and found["status"] == "active")


def remember(store: Store, s: dict, event: tuple | None = None, fields: dict | None = None, **changes) -> None:
    """Keep the watcher's notes about a session (in Session.watch; a None value removes a note), read again and
    merged inside the write, with fields of the session itself and an event when it acted. Nothing new: no write,
    so an idle project keeps its data_version."""
    def merged(held: dict) -> dict:
        return {k: v for k, v in {**held, **changes}.items() if v is not None}

    with store.read() as tx:
        current = tx.require(s["id"])
    held = current.get("watch") or {}
    if merged(held) == held and not event and all(current.get(k) == v for k, v in (fields or {}).items()):
        s["watch"] = held
        return
    with store.write() as tx:
        current = tx.require(s["id"])
        memo = merged(current.get("watch") or {})
        tx.update(s["id"], {"watch": memo, **(fields or {})})
        if event:
            tx.event(*event)
    s.update(fields or {})
    s["watch"] = memo


# -- typing into a session ------------------------------------------------------------------------------------


def deliver(s: dict, p: dict, text: str) -> tuple[str, str | None]:
    """Type a prompt (not a slash command) into a session and find out whether it arrived: ("delivered", None)
    when herdr saw the agent working or at a dialog after it, or a Codex log shows a turn that started after it;
    ("refused", why) when it certainly was not typed (a dialog was open, the agent is gone); ("unconfirmed", why)
    otherwise, with the end of its screen. p: the session as probed just before."""
    where = target(s)
    if p.get("dialog"):  # herdr would take the prompt, and the dialog would swallow it
        return "refused", (f"{where} waits at a dialog herdr does not see ({p['dialog']}), so nothing was typed; "
                           f"the coordinator decides it (`rl agent dialog {s['role_id']}`)")
    sent = now_utc()
    outcome, why = herdr.submit(where, text)
    codex = s.get("host") == "codex" and (p.get("log") or {}).get("path")
    if outcome == "delivered":
        if p.get("status") == "working" and codex and not _started(s, p, sent, CONFIRM_S):
            outcome, why = "unconfirmed", "herdr already showed it working, and its log shows no new turn"
        else:
            return "delivered", None
    if outcome == "refused":
        return outcome, why
    if s.get("host") == "codex" and (p.get("log") or {}).get("id") and _started(s, p, sent, 0):
        return "delivered", None  # its log, found now if the prompt began its first turn
    screen = herdr.read(where)
    panel = "possibly held by a panel, " if "hooks" in screen.lower() else ""
    return "unconfirmed", f"delivery not confirmed ({why}); {panel}its screen ends: {excerpt(screen, SCREEN_CHARS)}"


def _started(s: dict, p: dict, since: str, wait: float) -> bool:
    """Does the session's Codex log show a turn that started at or after since (waiting up to wait seconds)?"""
    deadline = time.monotonic() + wait
    while True:
        log = agentlog.follow("codex", p["log"]["id"], p["log"])
        p["log"], p["view"] = log, agentlog.turn_view(log)
        if p["view"] and _after(p["view"].get("started"), since):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


# -- the watcher's tab ----------------------------------------------------------------------------------------


def config_hint(store: Store) -> str | None:
    """A note for a project that still sets the old 20-minute rounds."""
    every = watch_minutes(store.project.config)
    default = DEFAULT_CONFIG["agents"]["watch_minutes"]
    if every <= default:
        return None
    return (f"agents.watch_minutes is {every:g} in smooth-research.yaml: the watcher now beats every {default} min by "
            f"default (each beat also looks at usage, turn errors and stalls); set it to {default} or remove it")


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
            return {"tab": tab, "started": False, "summary": f"the watcher runs in tab {tab} (a beat every {step:g} min)"}
        herdr.call("tab", "close", tab, timeout=30)  # it stopped reporting
    root = store.project.root
    made = herdr.new_tab("--cwd", str(root), "--label", LABEL, "--no-focus", "--env", f"{ENV_PROJECT}={root}",
                         "--env", f"SMOOTH_RESEARCH_RL={rl_command()}")
    tab, pane = made["tab"]["tab_id"], made["root_pane"]["pane_id"]
    with store.write() as tx:
        tx.set_meta(watch_tab=tab, watch_pane=pane, watch_beat=now_utc(), watch_every=every)
    herdr.call("pane", "run", pane, f"{shlex.quote(rl_command())} agent watch --own-tab --every {every:g} "
                                    f"--project {shlex.quote(str(root))}")
    hint = config_hint(store)
    return {"tab": tab, "started": True,
            "summary": f"the watcher runs in tab {tab}: a beat every {every:g} min over the open sessions"
                       + (f"\n  note: {hint}" if hint else "")}


def stop(store: Store) -> dict:
    with store.write() as tx:
        tab = tx.meta().get("watch_tab")
        tx.set_meta(watch_tab=None, watch_pane=None)
    note = ""
    try:
        if tab and herdr.problem() is None and _ours(tab):
            herdr.call("tab", "close", tab, timeout=30)
    except RLError as exc:
        note = f" (its tab: {exc.message}; the watcher leaves at its next beat)"
    return {"stopped": bool(tab),
            "summary": f"the watcher in tab {tab} stopped{note}" if tab else "no watcher was running"}


def _ours(tab: str) -> bool:
    """Is this the watcher's tab (open, with the label it was given)?"""
    return (herdr.get("tab", tab) or {}).get("label") == LABEL


def run(store: Store, every: float | None = None, own_tab: bool = False) -> dict:
    """The watcher's loop: a beat at once and then every few minutes until no session is open; it prints the beats
    that found something. In the tab that start() made (own_tab) it reports that it is alive, leaves when another
    watcher took over, and closes the tab at the end."""
    every = watch_minutes(store.project.config) if every is None else every
    if every <= 0:
        return {"summary": "the watcher is off (agents.watch_minutes: 0)"}
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    here = os.environ.get("HERDR_TAB_ID")
    hint = config_hint(store)
    print(f"{now_utc()} watching the agent sessions of {store.project.root}: a beat every {every:g} min"
          + (f" ({hint})" if hint else ""), flush=True)
    end, first = "no agent session is open and no task waits on runs", True
    while True:
        if not first:
            time.sleep(every * 60)
        first = False
        if own_tab:
            with store.read() as tx:
                registered = tx.meta().get("watch_tab")
            if registered != here:
                end = f"the watcher in tab {registered} took over" if registered else "the watcher was stopped"
                break
        try:
            out = check(store)
        except RLError as exc:
            print(f"{now_utc()} watch beat failed: {exc.message}", flush=True)
            continue
        if out.get("skipped"):
            print(out["summary"], flush=True)
            continue
        if out["news"] or out["reminder"]:
            print(out["summary"], flush=True)
        watched = bool(out["sessions"])
        if own_tab:
            with store.write() as tx:
                # decided where no session can be recorded in between: `rl agent open` finds this watcher registered
                # and starts none, so one recorded since the beat read the sessions keeps it
                watched = watched or any(s.get("herdr_agent") for s in tx.all("Session", status="active")) \
                    or waits_on_runs(tx)
                tx.set_meta(watch_beat=now_utc(), **({} if watched else {"watch_tab": None, "watch_pane": None}))
        elif not watched:
            with store.read() as tx:
                watched = waits_on_runs(tx)
        if not watched:
            break
    print(f"{now_utc()} {end}; the watcher stops", flush=True)
    if own_tab and here:
        herdr.call("tab", "close", here, timeout=30)
    return {"summary": f"{end}; the watcher stopped"}
