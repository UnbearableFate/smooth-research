"""The watcher: scheduling and recovery by rules (design 11.2, 11.3).

Each beat reads the to-do items of the rules table (inbox.py) and, per recipient session:
- no session open yet: open one in a herdr tab of the project's workspace (within the scientist limit of the
  phase and the parallel limit of a hypothesis task), claim the task it is for, start the agent and give it a
  one-line notice that tells it to read its pack;
- its agent idle and an item it was not told of (or one it was told of longer ago than the item's deadline): type
  one line per item into its pane. Before that, a session idle longer than the cache keeps it and with a large
  context gets its conversation cleared and reads its pack anew;
- its agent at a dialog (herdr: blocked): record that the session needs a decision; the rules table gives it to the
  coordinator (or to the user, for the coordinator's own session);
- its agent gone: the session ends; the next beat opens it again when it still has items.
The coordinator's pane is shared with the user: nothing is typed while the user talked to it within the last
agents.user_quiet_minutes, nor while it works.

Sessions whose work is finished (their task accepted or cancelled, their ticket closed) close. Every beat writes
a heartbeat file; any rl command warns when it is stale. The research page is redrawn when the newest event
changed, or every agents.page_minutes.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import adapters, herdr, inbox, launch, usage
from .project import ENV_PROJECT, agents_value, limit, usable_entries
from .roles import parse, session_name
from .schema import TASK_DONE
from .store import Store, Tx
from .util import RLError, atomic_write, now_utc, parse_time

LABEL = "rl watch"
HEARTBEAT = "watch.json"
NOTICE = "[smooth-research]"


def heartbeat_path(store: Store):
    return store.project.state_dir / HEARTBEAT


def heartbeat(store: Store) -> dict | None:
    try:
        return json.loads(heartbeat_path(store).read_text())
    except (OSError, ValueError):
        return None


def alive(store: Store) -> bool:
    beat = heartbeat(store)
    if not beat:
        return False
    # agents.heartbeat_minutes, and never less than two beats and a minute (a long beat_seconds)
    config = store.project.config
    stale = max(60 * agents_value(config, "heartbeat_minutes"), 2 * agents_value(config, "beat_seconds") + 60)
    return (datetime.now(timezone.utc) - parse_time(beat["at"])).total_seconds() < stale


def stale_note(store: Store) -> str | None:
    """The warning every rl command prints when the watcher stopped (11.3)."""
    if heartbeat(store) is None or alive(store):
        return None
    return "the watcher is not running: run `rl resume`"


@contextmanager
def beat_lock(store: Store) -> Iterator[bool]:
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
        os.close(fd)


# -- who may be opened ----------------------------------------------------------------------------------------


def subject_of(tx: Tx, name: str) -> dict | None:
    actor = parse(name)
    return tx.get(actor.subject) if actor.subject else None


def finished(tx: Tx, name: str) -> bool:
    """A session's work is over: its task accepted or cancelled (a scientist's also when delivered and received),
    its ticket closed or passed."""
    actor = parse(name)
    if actor.role == "coordinator":
        return False
    rec = subject_of(tx, name)
    if rec is None:
        return True
    if rec["_label"] == "Task":
        return rec["state"] in TASK_DONE
    if rec["_label"] == "Ticket":
        return rec["status"] in ("passed", "closed")
    return False


def may_open(tx: Tx, config: dict, name: str, open_names: set[str]) -> str | None:
    """Why a session cannot be opened now (None: it can): the scientist limit of the phase, the parallel limit of a
    hypothesis task (11.2)."""
    actor = parse(name)
    rec = subject_of(tx, name)
    if actor.role == "scientist" and rec and rec.get("kind") == "hypothesis":
        from .tasks import phase_of
        ph = phase_of(tx, rec["id"])
        cap = (ph or {}).get("scientist_limit") or limit(config, "scientists")
        busy = [n for n in open_names if n.startswith("scientist-") and (subject_of(tx, n) or {}).get("kind")
                == "hypothesis" and (phase_of(tx, parse(n).subject) or {}).get("id") == (ph or {}).get("id")]
        if len(busy) >= cap:
            return f"the phase allows {cap} scientist(s) at once"
    if actor.role == "researcher" and rec:
        parent = next((e["id"] for e in tx.out(rec["id"], "part_of")), None)
        if parent:
            cap = tx.require(parent).get("parallel_limit") or 10
            siblings = {e["id"] for e in tx.inc(parent, "part_of")}
            busy = [n for n in open_names if n.startswith("researcher-") and parse(n).subject in siblings]
            if len(busy) >= cap:
                return f"{parent} allows {cap} researcher(s) at once"
    return None


SWITCHING = ("researcher", "reviewer")  # the roles that move to another host when theirs runs low (usage)


def choose_entry(store: Store, role: str, low: dict) -> adapters.Entry | None:
    """The first configured entry of the role whose executable is there and, for a researcher or reviewer, whose
    host is not low on usage (the coordinator, scientists and advisors keep their model)."""
    low = low if role in SWITCHING else {}
    entries = usable_entries(store.project.config, role)
    fine = [e for e in entries if e.entry != "manual" and adapters.available(e) is None and e.entry not in low]
    return (fine or [e for e in entries if e.entry != "manual" and adapters.available(e) is None] or [None])[0]


def folder(store: Store, tx: Tx, name: str) -> str:
    """Where a session works: a task's worktree for its scientist or researcher, else the project root."""
    rec = subject_of(tx, name)
    if rec and rec["_label"] == "Task" and parse(name).role in ("scientist", "researcher"):
        if rec.get("worktree"):
            return rec["worktree"]
    return str(store.project.root)


# -- opening, telling, closing --------------------------------------------------------------------------------


def notice_line(item: inbox.Item) -> str:
    note = f" — {' '.join(str(item.note).split())[:200]}" if item.note else ""
    return f"{NOTICE} {item.type} {item.subject}: {item.ask}{note}"


def open_session(store: Store, name: str, items: list[inbox.Item], low: dict) -> str:
    actor = parse(name)
    entry = choose_entry(store, actor.role, low)
    if entry is None:
        return f"{name}: no usable model entry for the {actor.role} (models.{actor.role})"
    with store.read() as tx:
        rec = subject_of(tx, name)
    # the session takes what it is for: an open task (a scientist's hypothesis, tree or phase task; a researcher's
    # public preparation task), so that only one session works on it
    if rec and rec["_label"] == "Task" and rec["state"] == "open" and not rec.get("holder"):
        from .tasks import claim
        mine = (actor.role == "scientist" and rec["kind"] in ("hypothesis", "tree", "phase")) or \
            (actor.role == "researcher" and rec["kind"] == "prep" and rec["creator"] == "coordinator")
        if mine:
            try:
                claim(store, rec["id"], name)
            except RLError as exc:  # taken meanwhile, or a prerequisite changed: the next beat looks again
                return f"{name}: not opened ({exc.message})"
    with store.write() as tx:
        cwd = folder(store, tx, name)
        old = tx.all("Session", name=name)
        live = [o for o in old if o["status"] == "open"]
        if live:  # opened or bound again meanwhile (a watcher and rl resume at once): that one runs
            return f"{name}: runs already ({live[-1].get('herdr_pane') or 'being started'})"
        sid = tx.next_id("Session")
        s = tx.create("Session", {"id": sid, "name": name, "role": actor.role, "subject": actor.subject or "project",
                                  "host": entry.entry, "entry": str(entry), "model": entry.model,
                                  "effort": entry.effort, "status": "open", "cwd": cwd,
                                  "conversations": 1 + sum(o.get("conversations") or 0 for o in old)})
        tx.event("session.opened", name, f"{str(entry)} in {cwd}")
    try:
        started = launch.start_agent(store, s)
    except RLError as exc:
        with store.write() as tx:
            tx.update(sid, {"status": "ended", "ended_at": now_utc()})
            tx.event("session.failed", name, exc.message[:300])
        return f"{name}: could not start ({exc.message})"
    with store.write() as tx:
        tx.update(sid, {"herdr_tab": started["tab"], "herdr_pane": started["pane"],
                        "host_session_id": started["host_session"], "needs_decision": started["waiting"],
                        "last_active_at": now_utc()})
    if started["waiting"]:
        return f"{name}: opened, waits at a dialog"
    first = (f"{NOTICE} You are {name}. Run `{launch.rl_command()} pack` and work from what it prints; "
             f"`{launch.rl_command()} inbox` lists what waits on you. ") + " | ".join(notice_line(i) for i in items[:3])
    return f"{name}: opened ({str(entry)}); " + tell(store, sid, started["pane"], items, first)


def tell(store: Store, sid: str, pane: str, items: list[inbox.Item], text: str | None = None) -> str:
    text = text or " | ".join(notice_line(i) for i in items[:4]) + \
        (f" (+{len(items) - 4} more: rl inbox)" if len(items) > 4 else "") + " — handle these, then end your turn."
    why = herdr.prompt(pane, text)
    stamp = now_utc()
    with store.write() as tx:
        s = tx.require(sid, "Session")
        told = dict(s.get("told") or {})
        if why is None:
            told.update({i.key(): stamp for i in items})
        tx.update(sid, {"told": told, **({"last_prompt_at": stamp} if why is None else {})})
        for i in items:
            tx.event("notice", i.subject, f"{i.type} to {s['name']}: {'delivered' if why is None else 'held'}")
    return "told " + ", ".join(i.key() for i in items) if why is None else f"held ({why})"


START_MINUTES = 5  # longer than an agent's start and the folder trust question (start_agent waits 90 s)


def starting(s: dict) -> bool:
    """The session is being started: its record is open but the opener has not yet written its pane. A beat
    leaves it alone (ending it would orphan the agent the opener is starting)."""
    return not s.get("herdr_pane") and _minutes(s.get("created_at")) < START_MINUTES


def rebind(store: Store, name: str) -> dict | None:
    """Bind a session's latest record to the agent that lives under its herdr name in the tab rl made for it: one
    that ended while the agent ran on (closed by an older watcher, say), or one still open whose pane id went stale
    (herdr renumbered it). The record bound to the live agent (also when someone else bound it meanwhile), or None
    when no such agent lives."""
    info = herdr.agent(launch.herdr_name(store, name))
    if not info or not info.get("pane_id"):
        return None
    pane, tab = info["pane_id"], info.get("tab_id")
    with store.read() as tx:
        old = _rebind_row(tx.all("Session", name=name))
    if old is None:
        return None
    if old["status"] == "open" and old.get("herdr_pane") == pane:
        return old  # bound already (a watcher and rl resume both recovering it)
    if launch.own_tab(store, {**old, "herdr_tab": tab}) is None:
        return None
    with store.write() as tx:
        s = _rebind_row(tx.all("Session", name=name))
        if s is None or s["id"] != old["id"]:  # another record meanwhile: it decides
            return None
        if s["status"] == "open" and s.get("herdr_pane") == pane:
            return s
        tx.update(s["id"], {"status": "open", "ended_at": None, "herdr_pane": pane, "herdr_tab": tab,
                            "last_active_at": now_utc(),
                            # what it did so far is not the user talking to it
                            "last_seq": info.get("state_change_seq")})
        tx.event("session.rebound", name, f"its agent lives on in {pane}")
        return tx.require(s["id"], "Session")


def _rebind_row(rows: list[dict]) -> dict | None:
    """The record a live agent belongs to: the open one (the newest, if an old race left two), else the newest
    (rows in id order: S-9999 before S-10000)."""
    live = [r for r in rows if r["status"] == "open"]
    return live[-1] if live else (rows[-1] if rows else None)


def close_session(store: Store, s: dict, why: str) -> str:
    """End a session with its tab. When herdr cannot answer (a timeout), the record stays open and the next beat
    tries again: an ended record over a live agent would leave that agent running unwatched."""
    try:
        tab = launch.own_tab(store, s)
        if tab:
            herdr.call("tab", "close", tab, timeout=30)
    except RLError as exc:
        return f"{s['name']}: not closed yet ({exc.message}); the next beat tries again"
    with store.write() as tx:
        tx.update(s["id"], {"status": "ended", "ended_at": now_utc()})
        tx.event("session.closed", s["name"], why)
    return f"{s['name']}: closed ({why})"


def _minutes(at: str | None) -> float:
    if not at:
        return float("inf")
    return (datetime.now(timezone.utc) - parse_time(at)).total_seconds() / 60


def _due(s: dict, items: list[inbox.Item], config: dict) -> list[inbox.Item]:
    """Items the session was not told of, and items told longer ago than their deadline (the first step of the
    escalation ladder: remind the holder)."""
    from .project import deadline_minutes
    told = s.get("told") or {}
    return [i for i in items if i.key() not in told or _minutes(told[i.key()]) >= deadline_minutes(config, i.type)]


def look(store: Store, s: dict, items: list[inbox.Item], low: dict) -> str | None:
    """One open session: gone, at a dialog, working, or idle with something to tell."""
    config = store.project.config
    if starting(s):
        return None
    try:
        pane, info = launch.bound(store, s)
    except RLError as exc:
        return f"{s['name']}: herdr failed ({exc.message})"
    if info is None:
        again = rebind(store, s["name"])  # its pane id went stale while its agent lives on in its tab
        if again is None:
            return close_session(store, s, "its agent is gone; reopened when it has work")
        s = again
        pane, info = launch.bound(store, s)
        if info is None:
            return None
    status, seq = info.get("agent_status") or "unknown", info.get("state_change_seq")
    changes: dict = {}
    if seq is not None and seq != s.get("last_seq"):
        changes["last_seq"] = seq
        changes["last_active_at"] = now_utc()
        if s["role"] == "coordinator" and _minutes(s.get("last_prompt_at")) * 60 > 2 * agents_value(
                config, "beat_seconds"):
            changes["last_user_at"] = now_utc()  # it moved without a prompt of ours: the user talks to it
    if status == "blocked":
        seen = launch.excerpt(herdr.read(pane), 300)
        if seen != s.get("needs_decision"):  # an unchanged dialog changes no record (nor the page)
            changes["needs_decision"] = seen
    elif s.get("needs_decision"):
        changes["needs_decision"] = None
    if changes:
        with store.write() as tx:
            tx.update(s["id"], changes)
        s = {**s, **changes}
    if status in ("blocked", "working", "unknown"):
        return None
    if s["host"] in low and s["role"] in SWITCHING and (other := choose_entry(store, s["role"], low)) \
            and other.entry not in low:  # only when another host can take it
        return close_session(store, s, low[s["host"]]["reason"] + "; reopened on another model")
    due = _due(s, items, config)
    if not due:
        return None
    if s["role"] == "coordinator" and _minutes(s.get("last_user_at")) < agents_value(config, "user_quiet_minutes"):
        return None
    if _minutes(s.get("last_active_at")) >= agents_value(config, "clear_idle_minutes") and \
            (s.get("context_tokens") or 0) >= agents_value(config, "clear_context_tokens"):
        herdr.prompt(pane, "/clear")
        waits = launch.answer_new(pane, s["host"])  # a host that asks where the new conversation runs
        if waits:
            with store.write() as tx:
                tx.update(s["id"], {"needs_decision": waits})
            return f"{s['name']}: cleared; {waits}"
        with store.write() as tx:
            tx.update(s["id"], {"conversations": (s.get("conversations") or 1) + 1, "told": {}})
            tx.event("session.cleared", s["name"], "idle with a large context: a new conversation from its pack")
        return f"{s['name']}: cleared; " + tell(store, s["id"], pane, items, (
            f"{NOTICE} Your conversation was cleared. Run `{launch.rl_command()} pack` and work from it. ")
            + " | ".join(notice_line(i) for i in items[:3]))
    return f"{s['name']}: " + tell(store, s["id"], pane, due)


def beat(store: Store) -> dict:
    """One pass of the rules."""
    from .tasks import reconcile_merges
    config = store.project.config
    reconcile_merges(store)
    with store.read() as tx:
        items = inbox.all_items(tx, config)
        sessions = {s["name"]: s for s in tx.all("Session") if s["status"] == "open"}
    low = usage.low_hosts(store)
    by_to: dict[str, list[inbox.Item]] = {}
    for it in items:
        if it.to != "user":
            by_to.setdefault(it.to, []).append(it)
    done = []
    with store.read() as tx:
        ending = [s for n, s in sessions.items() if n not in by_to and not starting(s) and finished(tx, n)]
    for s in ending:
        done.append(close_session(store, s, "its work is finished"))
        sessions.pop(s["name"], None)
    for name, its in sorted(by_to.items(), key=lambda kv: kv[0] != "coordinator"):
        try:
            s = sessions.get(name)
            if s is None:
                if name != "coordinator":  # the coordinator's pane is opened by rl resume
                    with store.read() as tx:
                        if may_open(tx, config, name, set(sessions)):
                            continue
                # a session whose record ended while its agent lives on (closed by an older watcher, say) is
                # bound to that agent again; another agent under its herdr name could not be started
                s = rebind(store, name)
                if s is None:
                    if name != "coordinator":
                        done.append(open_session(store, name, its, low))
                    with store.read() as tx:
                        sessions = {x["name"]: x for x in tx.all("Session") if x["status"] == "open"}
                    continue
                sessions[name] = s
                done.append(f"{name}: bound again to its live agent in {s['herdr_pane']}")
            out = look(store, s, its, low)
            if out:
                done.append(out)
        except RLError as exc:  # one session's trouble does not stop the beat for the others
            done.append(f"{name}: {exc.message}")
    for name, s in sessions.items():
        if name not in by_to:  # nothing to tell, but a dialog it shows still needs someone
            try:
                look(store, s, [], low)
            except RLError as exc:
                done.append(f"{name}: {exc.message}")
    _redraw(store)
    return {"actions": done, "items": len(items), "summary": "\n".join(done) or "nothing to do"}


def write_heartbeat(store: Store, **values) -> None:
    """The one writer of the heartbeat file: what it held, updated with values and the time of this beat."""
    beat_info = {**(heartbeat(store) or {}), **values, "at": now_utc(), "pid": os.getpid()}
    atomic_write(heartbeat_path(store), json.dumps(beat_info).encode())


def _redraw(store: Store) -> None:
    """The page again when the records changed since it was drawn, or every agents.page_minutes; then the
    heartbeat."""
    from . import page
    beat_info = heartbeat(store) or {}
    with store.read() as tx:
        seq = tx.meta().get("data_version")
    every = agents_value(store.project.config, "page_minutes")
    drawn = {}
    if seq != beat_info.get("page_seq") or _minutes(beat_info.get("page_at")) >= every:
        try:
            page.write(store)
            drawn = {"page_seq": seq, "page_at": now_utc()}
        except RLError:
            pass
    write_heartbeat(store, **drawn)


# -- running it -----------------------------------------------------------------------------------------------


def run(store: Store, once: bool = False) -> dict:
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    every = agents_value(store.project.config, "beat_seconds")
    print(f"{now_utc()} watching {store.project.root}: a beat every {every:g} s", flush=True)
    while True:
        with beat_lock(store) as mine:
            if not mine:
                return {"summary": "another watcher runs for this project"}
            try:
                out = beat(store)
                if out["actions"]:
                    print(f"{now_utc()} " + out["summary"].replace("\n", f"\n{now_utc()} "), flush=True)
            except RLError as exc:
                print(f"{now_utc()} beat failed: {exc.message}", flush=True)
        if once:
            return out
        try:
            time.sleep(every)
        except KeyboardInterrupt:  # the user stops it in its tab: no traceback
            print(f"{now_utc()} stopped; rl resume starts it again", flush=True)
            return {"summary": "stopped"}


def start(store: Store) -> str:
    """Run the watcher in its own herdr tab, unless one runs (its heartbeat is fresh)."""
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"the watcher needs herdr: {problem}")
    if alive(store):
        return "the watcher runs"
    root = store.project.root
    made = herdr.new_tab("--cwd", str(root), "--label", LABEL, "--no-focus", "--env", f"{ENV_PROJECT}={root}",
                         "--env", f"SMOOTH_RESEARCH_RL={launch.rl_command()}")
    pane = made["root_pane"]["pane_id"]
    herdr.call("pane", "run", pane, f"{shlex.quote(launch.rl_command())} watch run")
    write_heartbeat(store, tab=made["tab"]["tab_id"])
    return f"the watcher started in tab {made['tab']['tab_id']}"


def ensure_coordinator(store: Store) -> str:
    """The coordinator's pane (11.2): one session on the project, opened from its pack."""
    with store.read() as tx:
        s = next((x for x in reversed(tx.all("Session", name="coordinator")) if x["status"] == "open"), None)
    if s:
        try:
            pane, _ = launch.bound(store, s)
        except RLError as exc:
            return f"the coordinator's pane could not be checked ({exc.message})"
        if pane:
            return f"the coordinator runs in pane {pane}"
    if s and starting(s):
        return "the coordinator is being started"
    try:  # before anything is closed or started: its agent may live on (a stale pane id, an ended record, or
        again = rebind(store, "coordinator")  # the watcher bound it a moment ago)
    except RLError as exc:
        return f"the coordinator's agent could not be checked ({exc.message})"
    if again:
        return f"the coordinator runs in pane {again['herdr_pane']} (its session bound again)"
    if s:
        close_session(store, s, "its pane is gone")
    with store.read() as tx:
        items = [i for i in inbox.all_items(tx, store.project.config) if i.to == "coordinator"]
    return open_session(store, session_name("coordinator", None), items, usage.low_hosts(store))
