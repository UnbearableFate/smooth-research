"""Sessions: opening, waking and closing the agents of a project (design 2026-11-27, 3.2 and 3.3).

Work moves on by the agents' own commands. A command that hands work to another session tells that session
itself (wake): what is told is the to-do items the records give the session now (inbox.py) that it was not told
yet, so a notice is only a pointer and a missed one loses nothing. By the state of the session:

1. open and idle: one line per item is typed into its pane, and the items are noted as told;
2. open and working: nothing is typed; its Stop hook hands it the items when it ends its turn (hooks.py);
3. at a dialog: nothing is typed; the session is recorded as needing a decision, which is the coordinator's item
   (the user's, for the coordinator's own dialog);
4. no live session: it is opened (going on with its earlier host conversation when there is one), and its first
   prompt tells it to read its pack;
5. the coordinator while the user talked to it within agents.user_quiet_minutes: nothing is typed; its
   UserPromptSubmit hook adds the items when the user speaks next, its Stop hook when it ends its turn;
6. anything that fails (no herdr, a timeout): the command still succeeds and says who was not told and how to
   open the session by hand.

A session is closed only by the command that ends its work, and only while it is idle (close_for_task). Nothing
here claims or assigns a task.
"""

from __future__ import annotations

import shlex
from datetime import datetime, timezone

from . import adapters, gitops, herdr, inbox, launch, usage
from .project import agents_value, usable_entries
from .roles import parse
from .schema import TASK_DONE
from .store import Store, Tx
from .util import RLError, now_utc, parse_time

NOTICE = "[smooth-research]"
START_MINUTES = 5  # longer than an agent's start and the folder trust question (start_agent waits 90 s)
SHOWN = 4  # items typed at once; the rest are in rl inbox


def _minutes(at: str | None) -> float:
    if not at:
        return float("inf")
    return (datetime.now(timezone.utc) - parse_time(at)).total_seconds() / 60


def subject_of(tx: Tx, name: str) -> dict | None:
    actor = parse(name)
    return tx.get(actor.subject) if actor.subject else None


def finished(tx: Tx, name: str) -> bool:
    """A session's work is over: its task accepted or cancelled, its ticket passed or closed."""
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


def notice_line(item: inbox.Item) -> str:
    note = f" — {' '.join(str(item.note).split())[:200]}" if item.note else ""
    return f"{NOTICE} {item.type} {item.subject}: {item.ask}{note}"


def notice_text(items: list[inbox.Item], tail: str = " — handle these, then end your turn.") -> str:
    text = " | ".join(notice_line(i) for i in items[:SHOWN])
    more = f" (+{len(items) - SHOWN} more: rl inbox)" if len(items) > SHOWN else ""
    return text + more + tail


def open_record(tx: Tx, name: str) -> dict | None:
    """The session's open record (the newest, if an old race left two)."""
    live = [s for s in tx.all("Session", name=name) if s["status"] == "open"]
    return live[-1] if live else None


def untold(tx: Tx, config: dict, name: str, s: dict | None) -> list[inbox.Item]:
    """The session's items it was not told of yet."""
    told = (s or {}).get("told") or {}
    return [i for i in inbox.for_recipient(tx, config, name) if i.key() not in told]


def mark_told(store: Store, sid: str, items: list[inbox.Item], how: str) -> None:
    stamp = now_utc()
    with store.write() as tx:
        s = tx.require(sid, "Session")
        told = dict(s.get("told") or {})
        told.update({i.key(): stamp for i in items})
        tx.update(sid, {"told": told})
        for i in items:
            tx.event("notice", i.subject, f"{i.type} to {s['name']}: {how}")


# -- choosing a model and a folder ------------------------------------------------------------------------------


def choose_entry(store: Store, role: str, prefer: str | None = None) -> adapters.Entry | None:
    """The first configured entry of the role whose executable is here, the hosts whose subscription runs low
    (usage.switch_left_percent) put last; prefer (the entry of the session's earlier conversation) when it is still
    in the list and usable. Read when a session opens; an open session keeps its model (3.3)."""
    entries = [e for e in usable_entries(store.project.config, role)
               if e.entry != "manual" and adapters.available(e) is None]
    if prefer:
        same = [e for e in entries if str(e) == prefer]
        if same:
            return same[0]
    try:
        low = usage.low_hosts(store)
    except Exception:  # a reading that cannot be parsed changes no choice
        low = {}
    entries.sort(key=lambda e: e.entry in low)
    return entries[0] if entries else None


def folder(store: Store, tx: Tx, name: str) -> str:
    """Where a session works: a task's worktree for its scientist or researcher, else the project root. The
    worktree is the one of this project (a project restored elsewhere has its own); one that is missing is refused
    with the way to make it again."""
    rec = subject_of(tx, name)
    if rec and rec["_label"] == "Task" and parse(name).role in ("scientist", "researcher"):
        if rec.get("worktree"):
            here = gitops.checkout_in(store.project, rec["id"])
            if not here.is_dir():
                raise RLError("NOT_FOUND", f"{rec['id']}'s worktree {here} is missing (a project moved or restored?):"
                                           f" make it again on its branch: git worktree add "
                                           f"{gitops.worktree_path(store.project, rec['id'])} {rec.get('branch')}")
            return str(here)
    return str(store.project.root)


# -- binding records to live agents ----------------------------------------------------------------------------


def starting(s: dict) -> bool:
    """The session is being started: its record is open but the opener has not yet written its pane. Others leave
    it alone (ending it would orphan the agent the opener is starting)."""
    return not s.get("herdr_pane") and _minutes(s.get("created_at")) < START_MINUTES


def _rebind_row(rows: list[dict]) -> dict | None:
    live = [r for r in rows if r["status"] == "open"]
    return live[-1] if live else (rows[-1] if rows else None)


def rebind(store: Store, name: str) -> dict | None:
    """Bind a session's latest record to the agent that lives under its herdr name in the tab rl made for it: one
    whose record ended while the agent ran on, or one still open whose pane id went stale (herdr renumbered it).
    The record bound to the live agent, or None when no such agent lives."""
    info = herdr.agent(launch.herdr_name(store, name))
    if not info or not info.get("pane_id"):
        return None
    pane, tab = info["pane_id"], info.get("tab_id")
    with store.read() as tx:
        old = _rebind_row(tx.all("Session", name=name))
    if old is None:
        return None
    if old["status"] == "open" and old.get("herdr_pane") == pane:
        return old
    if launch.own_tab(store, {**old, "herdr_tab": tab}) is None:
        return None
    with store.write() as tx:
        s = _rebind_row(tx.all("Session", name=name))
        if s is None or s["id"] != old["id"]:
            return None
        if s["status"] == "open" and s.get("herdr_pane") == pane:
            return s
        tx.update(s["id"], {"status": "open", "ended_at": None, "herdr_pane": pane, "herdr_tab": tab,
                            "last_active_at": now_utc(), "last_seq": info.get("state_change_seq")})
        tx.event("session.rebound", name, f"its agent lives on in {pane}")
        return tx.require(s["id"], "Session")


def live(store: Store, name: str) -> tuple[dict | None, str | None, dict | None]:
    """(record, pane, agent info) of the session's live agent; (record, None, None) while it is being started or
    when its agent is gone (the record is ended then); (None, None, None) when it has no open record and no live
    agent under its name."""
    with store.read() as tx:
        s = open_record(tx, name)
    if s and starting(s):
        return s, None, None
    if s:
        pane, info = launch.bound(store, s)
        if info is not None:
            return s, pane, info
    again = rebind(store, name)
    if again:
        pane, info = launch.bound(store, again)
        if info is not None:
            return again, pane, info
    if s:  # its agent is gone: the record ends; the session opens again when it has work
        with store.write() as tx:
            tx.update(s["id"], {"status": "ended", "ended_at": now_utc()})
            tx.event("session.closed", name, "its agent is gone")
    return None, None, None


# -- telling -----------------------------------------------------------------------------------------------------


def _status(store: Store, s: dict, pane: str, info: dict) -> tuple[str, dict]:
    """The agent's state as herdr sees it, a dialog herdr reports as idle counted as one (Codex's hook review), and
    the session's record kept up to date (needs_decision, last_seq, last_user_at)."""
    status, seq = info.get("agent_status") or "unknown", info.get("state_change_seq")
    changes: dict = {}
    if seq is not None and seq != s.get("last_seq"):
        changes.update(last_seq=seq, last_active_at=now_utc())
        if s["role"] == "coordinator" and _minutes(s.get("last_prompt_at")) * 60 > 2 * agents_value(
                store.project.config, "beat_seconds"):
            changes["last_user_at"] = now_utc()  # it moved without a prompt of ours: the user talks to it
    screen = herdr.read(pane) if status in ("idle", "done") and s["host"] in launch.UNSEEN else ""
    if status != "blocked" and launch.unseen(s["host"], screen):
        status = "blocked"
        seen = f"waits at a dialog herdr does not see ({launch.excerpt(screen, 300)})"
        if seen != s.get("needs_decision"):
            changes["needs_decision"] = seen
    elif status == "blocked":
        seen = launch.excerpt(herdr.read(pane), 300)
        if seen != s.get("needs_decision"):
            changes["needs_decision"] = seen
    elif s.get("needs_decision"):
        changes["needs_decision"] = None
    if changes:
        with store.write() as tx:
            tx.update(s["id"], changes)
        s = {**s, **changes}
    return status, s


def type_items(store: Store, s: dict, pane: str, items: list[inbox.Item], text: str | None = None) -> str | None:
    """Type the items (or text) into the pane; None once typed (they are noted as told), else why not."""
    why = herdr.prompt(pane, text or notice_text(items))
    if why is None:
        with store.write() as tx:
            tx.update(s["id"], {"last_prompt_at": now_utc()})
        if items:
            mark_told(store, s["id"], items, "typed")
    return why


def wake(store: Store, name: str, open_it: bool = True) -> str:
    """Tell a session what waits on it that it was not told yet (the six cases of 3.2), opening it when it has no
    live session and something waits on it (open_it). One line for the command's output; never raises."""
    if name == "user":
        return "the user: it is in their to-do list (rl status); the coordinator asks them"
    try:
        problem = herdr.problem()
        if problem:
            with store.read() as tx:
                waiting = inbox.for_recipient(tx, store.project.config, name)
            if not waiting:
                return f"{name}: nothing waits on it"
            return (f"could not notify {name}: {problem}; remedy: rl session open {name} (in a herdr pane) or "
                    "rl resume")
        s, pane, info = live(store, name)
        if s is not None and pane is None:
            return f"{name}: being started by another command; it reads its pack when it is up"
        if s is None:
            with store.read() as tx:
                items = inbox.for_recipient(tx, store.project.config, name)
            if not items:
                return f"{name}: nothing waits on it"
            if not open_it:
                return f"{name}: has work but no open session (rl session open {name})"
            return open_session(store, name, items)
        status, s = _status(store, s, pane, info)
        with store.read() as tx:
            items = untold(tx, store.project.config, name, s)
        if status == "blocked":
            if name == "coordinator":
                return f"{name} waits at a dialog ({(s.get('needs_decision') or '')[:200]}): the user answers it"
            told = wake(store, "coordinator") if items else "nothing new for it"
            return (f"{name} waits at a dialog ({(s.get('needs_decision') or '')[:200]}); the coordinator decides it "
                    f"(rl agent dialog {name}): {told}")
        if not items:
            return f"{name}: told already"
        if status in ("working", "unknown"):
            return f"{name} is working: its Stop hook hands it {len(items)} new item(s) when it ends its turn"
        if name == "coordinator" and _minutes(s.get("last_user_at")) < agents_value(store.project.config,
                                                                                    "user_quiet_minutes"):
            return ("the user is talking to the coordinator: it gets the news when the user speaks next or when it "
                    "ends its turn")
        why = type_items(store, s, pane, items)
        return f"{name}: told " + ", ".join(f"{i.type} {i.subject}" for i in items[:SHOWN]) if why is None else \
            f"could not notify {name}: {why}; it sees the items in rl inbox"
    except RLError as exc:
        return f"could not notify {name}: {exc.message[:300]}; remedy: rl session open {name} or rl resume"


def wake_all(store: Store, names: list[str]) -> list[str]:
    """wake for each recipient once, in order (the coordinator last: what the others do may be news to it)."""
    seen = list(dict.fromkeys(n for n in names if n))
    seen.sort(key=lambda n: n == "coordinator")
    return [wake(store, n) for n in seen]


# -- opening and closing -----------------------------------------------------------------------------------------


def open_session(store: Store, name: str, items: list[inbox.Item] | None = None) -> str:
    """Open a session in its own herdr tab: the first usable entry of its role (or the one of its earlier
    conversation, which it goes on with), in its folder; its first prompt says who it is and to read its pack."""
    actor = parse(name)
    with store.read() as tx:
        rows = tx.all("Session", name=name)
        live_rows = [o for o in rows if o["status"] == "open"]
        if live_rows:
            return f"{name}: runs already ({live_rows[-1].get('herdr_pane') or 'being started'})"
        earlier = next((o for o in reversed(rows) if o.get("host_session_id")), None)
        items = items if items is not None else inbox.for_recipient(tx, store.project.config, name)
    entry = choose_entry(store, actor.role, prefer=(earlier or {}).get("entry"))
    if entry is None:
        return f"could not open {name}: no usable model entry for the {actor.role} (models.{actor.role})"
    resume = earlier["host_session_id"] if earlier and earlier.get("entry") == str(entry) else None
    with store.write() as tx:
        if open_record(tx, name):  # opened meanwhile by another command: that one runs
            return f"{name}: being started by another command"
        cwd = folder(store, tx, name)
        sid = tx.next_id("Session")
        s = tx.create("Session", {"id": sid, "name": name, "role": actor.role, "subject": actor.subject or "project",
                                  "host": entry.entry, "entry": str(entry), "model": entry.model,
                                  "effort": entry.effort, "status": "open", "cwd": cwd,
                                  "conversations": 1 + sum(o.get("conversations") or 0 for o in rows)})
        tx.event("session.opened", name, f"{entry} in {cwd}" + (f", going on with {resume}" if resume else ""))
    try:
        started = launch.start_agent(store, s, resume=resume)
    except RLError as exc:
        with store.write() as tx:
            tx.update(sid, {"status": "ended", "ended_at": now_utc()})
            tx.event("session.failed", name, exc.message[:300])
        return f"could not open {name}: {exc.message[:300]}; remedy: rl session open {name}"
    with store.write() as tx:
        tx.update(sid, {"herdr_tab": started["tab"], "herdr_pane": started["pane"],
                        "host_session_id": started["host_session"], "needs_decision": started["waiting"],
                        "last_active_at": now_utc()})
        s = tx.require(sid, "Session")
    if started["waiting"]:
        return f"{name}: opened, waits at a dialog ({started['waiting'][:200]})"
    rl = launch.rl_command()
    first = (f"{NOTICE} You are {name}. Run `{rl} pack` and work from what it prints; `{rl} inbox` lists what "
             "waits on you. ") + " | ".join(notice_line(i) for i in items[:3])
    why = type_items(store, s, started["pane"], items, first)
    return f"{name}: opened ({entry})" + ("" if why is None else f"; its first prompt was not typed ({why})")


def close(store: Store, name: str, why: str, force: bool = False) -> str:
    """Close a session with its tab, only while its agent is seen idle or done, or is gone (force: whatever it does;
    rl session close --force). Its live agent is looked up by name first, so a stale pane id does not make a working
    agent look gone. When herdr cannot answer, the record stays open."""
    with store.read() as tx:
        s = open_record(tx, name)
    if s is None:
        return f"{name}: not open"
    try:
        pane, info = launch.bound(store, s)
        if info is None:  # its pane id may have gone stale while its agent lives on in its tab
            again = rebind(store, name)
            if again is not None:
                s = again
                pane, info = launch.bound(store, s)
        status = (info or {}).get("agent_status") or ("gone" if info is None else "unknown")
        if status not in ("idle", "done", "gone") and not force:  # only an agent seen at rest is closed
            return f"{name}: left open ({status}); rl session close {name} when it is idle"
        tab = launch.own_tab(store, s)
        if tab:
            herdr.call("tab", "close", tab, timeout=30)
    except RLError as exc:
        return f"{name}: not closed ({exc.message[:200]})"
    with store.write() as tx:
        tx.update(s["id"], {"status": "ended", "ended_at": now_utc()})
        tx.event("session.closed", name, why)
    return f"{name}: closed ({why})"


def ending_task(tx: Tx, tid: str) -> list[str]:
    """The sessions whose work ends with a task (3.3): its researcher and the reviewers of its rounds; for a
    hypothesis or phase task also its scientist and the sessions of the tasks under it and of its advice."""
    task = tx.get(tid, "Task")
    if task is None:
        return []
    names = [f"researcher-{tid}"] + [f"reviewer-{r['id']}" for r in tx.all("Ticket", task_id=tid)
                                     if r["kind"] == "review"]
    if task["kind"] in ("hypothesis", "phase", "tree"):
        names.append(f"scientist-{tid}")
        for e in tx.inc(tid, "part_of"):
            names += ending_task(tx, e["id"])
        names += [f"advisor-{a['id']}" for a in tx.all("Ticket", task_id=tid) if a["kind"] == "advice"]
    elif task["kind"] == "prep" and task.get("scientist_session"):
        names.append(task["scientist_session"])
    return names


def close_for_task(store: Store, tid: str) -> list[str]:
    """Close the idle sessions whose work ended with the task; the open ones only (nothing is said of the rest)."""
    if herdr.problem():
        return []
    with store.read() as tx:
        names = [n for n in dict.fromkeys(ending_task(tx, tid)) if open_record(tx, n)]
    out = []
    for n in names:
        try:
            out.append(close(store, n, f"{tid} ended"))
        except RLError as exc:
            out.append(f"{n}: not closed ({exc.message[:200]})")
    return out


def ensure_coordinator(store: Store) -> str:
    """The coordinator's pane: its live session, bound again when its record went stale, else a new one. When herdr
    cannot answer, nothing is closed, bound or started, and the line says so (rl resume again looks again)."""
    try:
        s, pane, _ = live(store, "coordinator")
    except RLError as exc:
        return f"the coordinator's pane could not be checked ({exc.message[:200]}); rl resume again looks again"
    if s and pane:
        return f"the coordinator runs in pane {pane}"
    if s:
        return "the coordinator is being started"
    return open_session(store, "coordinator")


def print_command(store: Store, name: str) -> str:
    """How to open a session by hand without herdr (rl session open NAME --print)."""
    actor = parse(name)
    entry = choose_entry(store, actor.role)
    if entry is None:
        return f"# no usable model entry for the {actor.role} (models.{actor.role})"
    with store.read() as tx:
        cwd = folder(store, tx, name)
    import uuid
    args = launch.launch_args(store, entry, actor.role, name, str(uuid.uuid4()) if entry.entry != "codex" else None)
    env = {"SMOOTH_RESEARCH_PROJECT": str(store.project.root), "SMOOTH_RESEARCH_SESSION": name,
           **({"SMOOTH_RESEARCH_CHECKOUT": cwd} if cwd != str(store.project.root) else {})}
    return (f"cd {shlex.quote(cwd)} && " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "
            + " ".join(shlex.quote(a) for a in [adapters.EXECUTABLES[entry.entry], *[a for a in args if a]])
            + f"   # then tell it: run `{launch.rl_command()} pack`")


def rows(store: Store) -> list[str]:
    """rl session list: the open sessions, whether their work ended, and who has work but no session."""
    with store.read() as tx:
        opened = [s for s in tx.all("Session") if s["status"] == "open"]
        items = inbox.all_items(tx, store.project.config)
        lines = []
        for s in opened:
            done = " · its work ended (rl session close " + s["name"] + ")" if finished(tx, s["name"]) else ""
            ask = f" · at a dialog: {s['needs_decision'][:120]}" if s.get("needs_decision") else ""
            lines.append(f"{s['name']} {s['entry']} pane {s.get('herdr_pane') or '-'} since "
                         f"{(s.get('created_at') or '')[:16]}{ask}{done}")
        names = {s["name"] for s in opened}
        idle = sorted({i.to for i in items if i.to not in names and i.to != "user"})
    lines += [f"{n}: has work, no open session (rl session open {n})" for n in idle]
    return lines or ["no sessions"]
