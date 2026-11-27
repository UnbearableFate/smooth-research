"""The observer `rl watch` (design 2026-11-27, 3.4): optional, and read-only towards the research records.

Each beat:
1. redraws the research page when the records changed, or every agents.page_minutes;
2. tells an open, idle session the to-do items it was not told of yet (a command's notice that did not get
   through, items the coordinator's pane held back while the user talked, items that appear with time such as a job
   past its expected end), and reminds it once of an item waiting longer than agents.deadlines;
3. notes a session at a dialog (needs_decision); the to-do rules give that to the coordinator (to the user, for the
   coordinator's own dialog).
It opens no session, claims nothing, closes no session, finishes no merge, switches no model and clears no context.
It writes only the sessions' told, reminded and needs_decision (with the bookkeeping that tells the user's typing in
the coordinator's pane from ours), its heartbeat file and the research page. A beat that finds the lock taken is
skipped; the observer does not stop for it. When it stops, research goes on: commands tell the sessions themselves.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import herdr, inbox, launch, sessions
from .project import ENV_PROJECT, agents_value, deadline_minutes
from .store import Store
from .util import RLError, atomic_write, now_utc, parse_time

LABEL = "rl watch"
HEARTBEAT = "observer.json"  # this observer's own: a watcher from before schema 6 rewrites watch.json, keeping
LEGACY_HEARTBEAT = "watch.json"  # whatever else it held, so the two never share one file
GENERATION = 6  # the schema from which `rl watch` is this read-only observer; earlier watchers scheduled work


def heartbeat_path(store: Store):
    return store.project.state_dir / HEARTBEAT


def heartbeat(store: Store) -> dict | None:
    try:
        return json.loads(heartbeat_path(store).read_text())
    except (OSError, ValueError):
        return None


def alive(store: Store) -> bool:
    """This observer runs: a fresh heartbeat of its generation (an older watcher's does not count)."""
    beat = heartbeat(store)
    if not beat or (beat.get("generation") or 0) < GENERATION:
        return False
    # agents.heartbeat_minutes, and never less than two beats and a minute (a long beat_seconds)
    config = store.project.config
    stale = max(60 * agents_value(config, "heartbeat_minutes"), 2 * agents_value(config, "beat_seconds") + 60)
    return (datetime.now(timezone.utc) - parse_time(beat["at"])).total_seconds() < stale


def _argv(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None  # gone, or not on this host
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _an_rl_watcher(argv: list[str]) -> bool:
    """argv runs `rl watch run`: the plugin's rl entry (its environment's bin/rl, or the project interpreter
    running smooth_research.cli, as plugin/bin/rl starts it) with the arguments watch run."""
    for i in range(len(argv) - 1):
        if argv[i:i + 2] == ["watch", "run"]:
            return any(Path(a).name == "rl" or "from smooth_research.cli import main" in a for a in argv[:i])
    return False


def _of_project(store: Store, pid: int) -> bool:
    """The process works on this project: started in its folder, or with its path as SMOOTH_RESEARCH_PROJECT (as
    rl starts the watcher, in a tab of its own)."""
    root = store.project.root.resolve()
    try:
        if Path(os.readlink(f"/proc/{pid}/cwd")).resolve() == root:
            return True
    except OSError:
        pass
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(e.startswith(f"{ENV_PROJECT}=".encode()) and Path(e.split(b"=", 1)[1].decode(errors="replace"))
               .resolve() == root for e in env)


def older(store: Store) -> dict | None:
    """A watcher from before schema 6 that may still run on this host: its heartbeat (watch.json) names a pid that
    is still an `rl watch run` process. It schedules work by the old rules, also after the project was upgraded.
    `mine`: the process works on this project (else it is only reported: a copied project carries the heartbeat of
    another's watcher)."""
    try:
        beat = json.loads((store.project.state_dir / LEGACY_HEARTBEAT).read_text())
        pid = int(beat["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # whatever it says of its generation was copied from the file it once shared: this observer writes its own
    if pid == os.getpid():
        return None
    argv = _argv(pid)
    if not argv or not _an_rl_watcher(argv):
        return None  # gone, or the pid names another program now
    return {"pid": pid, "tab": beat.get("tab"), "mine": _of_project(store, pid)}


def retire_older(store: Store) -> str | None:
    """Stop a watcher from before schema 6 (older) that works on this project: it would go on opening sessions and
    claiming tasks. Its sessions and the records are left as they are; rl resume starts this observer. One rl cannot
    tie to this project is left running and reported."""
    old = older(store)
    if not old:
        return None
    if not old["mine"]:
        return (f"the old watcher heartbeat names pid {old['pid']}, an `rl watch run` that does not work in this "
                "project (a copied heartbeat?): left running; stop it yourself if it is this project's")
    try:
        os.kill(old["pid"], signal.SIGTERM)
    except OSError as exc:
        return (f"a watcher from before schema 6 still runs (pid {old['pid']}) and schedules by the old rules: stop "
                f"it (kill {old['pid']}; {exc})")
    return (f"stopped the watcher from before schema 6 (pid {old['pid']}"
            + (f", tab {old['tab']}" if old.get("tab") else "") + "): rl resume starts the read-only observer")


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


def look(store: Store, s: dict, items: list[inbox.Item]) -> str | None:
    """One open session: at a dialog (noted), working (left alone), or idle with something not told yet, or told
    longer ago than its deadline and not reminded (typed once)."""
    config = store.project.config
    name = s["name"]
    if sessions.starting(s):
        return None
    pane, info = launch.bound(store, s)
    if info is None:
        again = sessions.rebind(store, name)  # its pane id went stale while its agent lives on in its tab
        if again is None:
            return None  # its agent is gone: rl status lists it; the next command for it opens it again
        s = again
        pane, info = launch.bound(store, s)
        if info is None:
            return None
    status, s = sessions._status(store, s, pane, info)
    if status != "idle" and status != "done":
        return None
    if name == "coordinator" and sessions._minutes(s.get("last_user_at")) < agents_value(config,
                                                                                          "user_quiet_minutes"):
        return None
    told, reminded = s.get("told") or {}, s.get("reminded") or {}
    new = [i for i in items if i.key() not in told]
    late = [i for i in items if i.key() in told and i.key() not in reminded
            and sessions._minutes(i.since) >= deadline_minutes(config, i.type)]
    if new:
        why = sessions.type_items(store, s, pane, new)
        return f"{name}: told " + ", ".join(i.key() for i in new) if why is None else f"{name}: not told ({why})"
    if late:
        text = sessions.notice_text(late, " — waiting a while: handle them, or say in rl inbox why not, then end "
                                          "your turn.")
        why = sessions.type_items(store, s, pane, [], text)
        if why is None:
            stamp = now_utc()
            with store.write() as tx:
                rec = tx.require(s["id"], "Session")
                tx.update(s["id"], {"reminded": {**(rec.get("reminded") or {}),
                                                 **{i.key(): stamp for i in late}}})
            return f"{name}: reminded of " + ", ".join(i.key() for i in late)
        return f"{name}: not reminded ({why})"
    return None


def beat(store: Store) -> dict:
    """One pass: tell, remind, note dialogs, redraw."""
    config = store.project.config
    done = []
    if herdr.problem() is None:
        with store.read() as tx:
            items = inbox.all_items(tx, config)
            opened = [s for s in tx.all("Session") if s["status"] == "open"]
        by_to: dict[str, list[inbox.Item]] = {}
        for it in items:
            if it.to != "user":
                by_to.setdefault(it.to, []).append(it)
        opened.sort(key=lambda s: s["name"] == "coordinator")  # what the others hear may be news to it
        for s in opened:
            try:
                out = look(store, s, by_to.get(s["name"], []))
            except RLError as exc:  # one session's trouble does not stop the beat for the others
                out = f"{s['name']}: {exc.message}"
            if out:
                done.append(out)
    _redraw(store)
    return {"actions": done, "summary": "\n".join(done) or "nothing to do"}


def write_heartbeat(store: Store, **values) -> None:
    """The one writer of the heartbeat file: what it held, updated with values and the time of this beat."""
    beat_info = {**(heartbeat(store) or {}), **values, "at": now_utc(), "pid": os.getpid(), "generation": GENERATION}
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
    if seq != beat_info.get("page_seq") or sessions._minutes(beat_info.get("page_at")) >= every:
        try:
            page.write(store)
            drawn = {"page_seq": seq, "page_at": now_utc()}
        except RLError:
            pass
    write_heartbeat(store, **drawn)


# -- running it -----------------------------------------------------------------------------------------------


def _fresh(store: Store) -> Store:
    """The store with the project's configuration read again (every command reads it; the observer once a beat)."""
    from .project import find_project
    try:
        return Store(find_project(start=store.project.root), store.actor)
    except RLError:
        return store  # a configuration that does not read now: the last one that did


def run(store: Store, once: bool = False) -> dict:
    problem = herdr.problem()
    every = agents_value(store.project.config, "beat_seconds")
    print(f"{now_utc()} observing {store.project.root}: a beat every {every:g} s"
          + (f" (only the research page: {problem})" if problem else ""), flush=True)
    out: dict = {"summary": "nothing to do"}
    while True:
        store = _fresh(store)  # the configuration as it is now: deadlines, quiet time, the beat itself
        with beat_lock(store) as mine:
            if mine:
                try:
                    out = beat(store)
                    if out["actions"]:
                        print(f"{now_utc()} " + out["summary"].replace("\n", f"\n{now_utc()} "), flush=True)
                except RLError as exc:
                    print(f"{now_utc()} beat failed: {exc.message}", flush=True)
            elif once:
                return {"summary": "another beat runs now; this one is skipped"}
        if once:
            return out
        try:
            time.sleep(agents_value(store.project.config, "beat_seconds"))
        except KeyboardInterrupt:  # the user stops it in its tab: no traceback
            print(f"{now_utc()} stopped; rl resume starts it again", flush=True)
            return {"summary": "stopped"}


def start(store: Store) -> str:
    """Run the observer in its own herdr tab, unless one runs (its heartbeat is fresh)."""
    problem = herdr.problem()
    if problem:
        return f"the observer was not started: {problem} (rl watch run in any terminal runs it)"
    if alive(store):
        return "the observer runs"
    root = store.project.root
    made = herdr.new_tab("--cwd", str(root), "--label", LABEL, "--no-focus", "--env", f"{ENV_PROJECT}={root}",
                         "--env", f"SMOOTH_RESEARCH_RL={launch.rl_command()}")
    pane = made["root_pane"]["pane_id"]
    herdr.call("pane", "run", pane, f"{shlex.quote(launch.rl_command())} watch run")
    write_heartbeat(store, tab=made["tab"]["tab_id"])
    return f"the observer started in tab {made['tab']['tab_id']}"
