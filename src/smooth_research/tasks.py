"""Tasks, phases and decision requests (design chapters 6 and 7; 2026-11-27 design 3.2, 3.3, 5).

Four kinds (a fifth, tree, in older records only): hypothesis (one hypothesis for one scientist, who does the whole
test), phase (the phase conclusion), prep (preparation work a researcher codes; public ones belong to the project)
and execute (one piece of experiment code for one experiment). The coordinator makes hypothesis, phase and public
prep tasks (queued until released, which gives them to their session); a scientist makes execute and prep tasks
under its hypothesis task (PART_OF) and holds them while it writes code; dispatch hands one to a researcher.
Acceptance merges (gitops): the creator accepts.

A command refuses only what it cannot record, or what would be recorded falsely; every other rule of the process is
followed with a flag on the record (flags.py). A command that hands work on names the sessions to tell (result
"wake"; cli.handoff tells them) and the tasks whose sessions' work ended (result "close").
"""

from __future__ import annotations

import shlex
import subprocess
from datetime import datetime, timezone

from . import documents, flags, gitops, manifest
from .flags import Flags
from .project import tests_mode
from .research import design_key, sealed_copy, task_design, task_experiment
from . import reviews
from .roles import Actor, need, session_name
from .schema import TASK_DONE
from .store import Store, Tx
from .util import RLError, digest_json, now_utc, parse_time

NOTE_CHARS = 300
WORK = "research/work"


def work_dir(tid: str) -> str:
    return f"{WORK}/{tid}"


def _note(text: str | None, fl: Flags) -> str | None:
    """A note is recorded whole; notices and to-do notes show its first NOTE_CHARS characters (the 11-10 cap is a
    reminder now, 5.2)."""
    if text and len(text) > NOTE_CHARS:
        fl.add("long-note", f"a note of {len(text)} characters: notices and to-do notes show its first "
                            f"{NOTE_CHARS}; put the rest in a record or a document and point to it")
    return text


def _hold(tx: Tx, task: dict, holder: str | None) -> dict:
    history = list(task.get("holders") or [])
    if holder:
        history.append({"holder": holder, "at": now_utc()})
    return {"holder": holder, "holders": history}


def hypothesis_task_of(tx: Tx, task: dict) -> dict | None:
    """The hypothesis task an execute or prep task is part of."""
    found = [e["id"] for e in tx.out(task["id"], "part_of")]
    return tx.require(found[0]) if found else None


def phase_of(tx: Tx, task_id: str) -> dict | None:
    found = [e["id"] for e in tx.out(task_id, "in_phase")]
    return tx.require(found[0]) if found else None


# -- making tasks ---------------------------------------------------------------------------------------------


def create(store: Store, actor: Actor, kind: str, title: str, targets: list[str] = (), priority: int | None = None,
           envelope: str | None = None, reason: str | None = None, phase: str | None = None,
           experiment: str | None = None, requirement: str | None = None, done_check: str | None = None,
           estimate: str | None = None, mandate: str | None = None, depends_on: list[str] = ()) -> dict:
    """The coordinator queues hypothesis, phase and public prep tasks; a scientist makes execute and prep tasks in
    its hypothesis task and holds them at once, unless they wait on unaccepted tasks (DEPENDS_ON): those wait
    unassigned, without a worktree, until rl task accept says their prerequisites are in (3.3)."""
    if kind == "tree":
        raise RLError("INVALID", "the tree is no task any more: the coordinator's subagent drafts it (rl tree prompt) "
                                 "and rl tree record records it once the user approved it")
    fl = _check(actor, *(("coordinator",) if kind in ("hypothesis", "phase") else ("scientist",) if kind == "execute"
                         else ("coordinator", "scientist")), what=f"making a {kind} task")
    coordinators = kind in ("hypothesis", "phase") or (kind == "prep" and actor.role != "scientist")
    if kind == "hypothesis" and (not targets or priority is None or not envelope or not reason):
        raise RLError("INVALID", "a hypothesis task card has four items: --targets H-x, --priority N, --envelope "
                                 "\"about 4 MIG·h\", --reason \"<why here>\"")
    if kind == "phase" and not phase:
        raise RLError("INVALID", "a phase conclusion task names its phase (--phase PH-x)")
    if kind == "execute" and not experiment:
        raise RLError("INVALID", "an execute task carries one experiment (--experiment E-x)")
    if kind == "prep" and (not requirement or not done_check):
        raise RLError("INVALID", "a preparation task needs --requirement (what to produce) and --done-check (a "
                                 "command that passes when it is done)")
    prep = documents.prepare(store.project, mandate, "mandate") if mandate else None
    with store.write() as tx:
        parent = None
        if not coordinators and actor.subject:
            found = tx.get(actor.subject, "Task")
            if found and found["kind"] == "hypothesis":
                parent = found
        if not coordinators and parent is None:
            fl.add("role", f"execute and prep tasks are made in a hypothesis task; {actor.name} holds none, so this "
                           "one is part of no hypothesis task")
        if not title:
            named = [tx.require(t, "Hypothesis")["title"] for t in targets] or \
                [tx.require(experiment, "Experiment")["title"]] if (targets or experiment) else []
            if not named:
                raise RLError("INVALID", "--title is needed (a hypothesis task takes its hypothesis's title)")
            title = ("Test " if kind == "hypothesis" else "") + "; ".join(named)
        tid = tx.next_id("Task")
        deps = [tx.require(d, "Task") for d in depends_on]
        held = not coordinators and all(d["state"] == "accepted" for d in deps)
        tx.create("Task", {"id": tid, "title": title, "kind": kind, "state": "queued" if coordinators else
                           ("active" if held else "open"), "revision": 1, "creator": actor.name,
                           "priority": priority, "envelope": envelope, "reason": reason, "requirement": requirement,
                           "done_check": done_check, "estimate": estimate})
        if prep:
            tx.update(tid, {"mandate_ref": documents.register(tx, prep, tid)})
        for t in targets:
            tx.link("targets", tid, tx.require(t, "Hypothesis")["id"])
        if experiment:
            tx.require(experiment, "Experiment")
            tx.link("targets", tid, experiment)
        if phase:
            tx.require(phase, "Phase")
            tx.link("targets" if kind == "phase" else "in_phase", tid, phase)
        if parent:
            tx.link("part_of", tid, parent["id"])
        for d in deps:
            tx.link("depends_on", tid, d["id"])
        if held:
            tx.update(tid, _claim_changes(tx, store, tx.require(tid), actor.name))
        fl.write(tx, tid)
        tx.event("task.created", tid, f"{kind}: {title}")
        task = tx.require(tid)
    where = f"; worktree {task['worktree']}" if task.get("worktree") else ""
    waits = "" if coordinators or held else \
        f"; it waits on {', '.join(d['id'] for d in deps if d['state'] != 'accepted')} (rl task accept of those " \
        "tells you; rl task claim takes it earlier, flagged)"
    return _result(tid, f"{tid} {kind} {task['state']}: {title}{where}{waits}", fl)


def waits_on(tx: Tx, tid: str) -> list[str]:
    """The tasks a task depends on that are not accepted yet."""
    return [d["id"] for d in tx.out(tid, "depends_on") if tx.require(d["id"])["state"] != "accepted"]


def approval_problem(tx: Tx, tid: str) -> str | None:
    """Why the user's approval asked on a task is missing: the latest approval request on it is not answered, or was
    answered with change (5.2). None when nothing was asked, or it was approved."""
    asks = [a for a in tx.all("Ask", task_id=tid) if a["kind"] == "approval"]
    if not asks:
        return None
    a = asks[-1]
    if a["state"] != "answered":
        return f"the user's approval {a['id']} is not answered"
    if (a.get("choice") or "") != "approve":
        return f"the user answered {a['id']} with change: {(a.get('answer') or '')[:200]}"
    return None


def _check(actor: Actor | None, *roles: str, what: str) -> Flags:
    """The flags of one command, with a role flag when the caller is not of the roles the process names (the
    command is refused only without a session identity)."""
    fl = Flags(actor.name if actor else "?")
    need(actor, *roles, what=what, flags=fl)
    return fl


def _result(rid: str, summary: str, fl: Flags | None = None, **more) -> dict:
    out = {"id": rid, "summary": summary, **{k: v for k, v in more.items() if v}}
    if fl:
        out["flags"] = fl.items
    return out


def _claim_changes(tx: Tx, store: Store, task: dict, holder: str) -> dict:
    branch, path = gitops.make_worktree(store.project, task["id"])
    return {"state": "active", **_hold(tx, task, holder), "branch": branch,
            "worktree": str(gitops.checkout_in(store.project, task["id"])), "revision": task["revision"] + 1}


def claimant(task: dict) -> str:
    """Who a task goes to: the scientist session on a hypothesis or phase task (a tree task, in older records), the
    researcher session on a public preparation task, and the scientist that made any other task (it writes the
    code, then dispatches)."""
    if task["kind"] in ("hypothesis", "tree", "phase"):
        return session_name("scientist", task["id"])
    if task["kind"] == "prep" and task["creator"] == "coordinator":
        return session_name("researcher", task["id"])
    return task["creator"]


def claim(store: Store, actor: Actor, tid: str) -> dict:
    """Take a task (3.3): an open one waiting on its prerequisites (its branch and worktree are made from the
    mainline now; before they are accepted it is flagged), or a held one taken back by its creator or the
    coordinator (the earlier holder is told). Anyone else taking it, or taking one the coordinator has not released
    yet, is flagged. A task delivered or ended is not claimed: that would contradict its recorded end."""
    fl = _check(actor, "scientist", "researcher", "coordinator", what="claiming a task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] in TASK_DONE + ("delivered", "merging"):
            raise RLError("CONFLICT", f"{tid} is {task['state']}: it cannot be claimed now"
                          + (" (rl task return gives a delivered task back to its holder)"
                             if task["state"] == "delivered" else ""))
        if task["state"] == "queued":
            fl.add("role", f"{tid} was not released yet: releasing is the coordinator's step; {actor.name} took it")
        before = task.get("holder")
        if before == actor.name:
            return _result(tid, f"{tid} is yours already" + (f"; worktree {task['worktree']}"
                                                              if task.get("worktree") else ""))
        if actor.name not in (claimant(task), task["creator"], "coordinator"):
            fl.add("not-holder", f"{tid} goes to {claimant(task)}; {actor.name} took it")
        waiting = waits_on(tx, tid)
        nxt = []
        if waiting and not task.get("worktree"):
            fl.add("prerequisite", f"claimed before {', '.join(waiting)} was accepted")
            nxt.append(f"when {', '.join(waiting)} is accepted, merge the mainline into {gitops.branch_name(tid)}")
        if task.get("worktree"):
            changes = {**_hold(tx, task, actor.name), "revision": task["revision"] + 1,
                       **({"state": "active"} if task["state"] == "blocked" else {})}
        else:
            changes = _claim_changes(tx, store, task, actor.name)
        tx.update(tid, changes)
        fl.write(tx, tid)
        tx.event("task.claimed", tid, actor.name + (f", taken from {before}" if before else ""))
        task = tx.require(tid)
    taken = f"; taken from {before}, who is told" if before else ""
    return _result(tid, f"{tid} held by {actor.name}; worktree {task['worktree']}{taken}", fl, next=nxt,
                   wake=[before] if before else None)


def release(store: Store, actor: Actor, tid: str, to_scientist: bool = False) -> dict:
    """The coordinator releases a queued task to the session it goes to (3.2): a hypothesis or phase task to its
    scientist, a public preparation task to its researcher; its branch and worktree are made now. One waiting on
    unaccepted tasks stays unassigned until rl task accept of the last of them. --to scientist hands a blocked public
    preparation task to a short scientist session (6.4). An approval asked on the task and not given is flagged."""
    fl = _check(actor, "coordinator", what="releasing a task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if to_scientist:
            if task["kind"] != "prep" or task["creator"] != "coordinator":
                raise RLError("INVALID", "--to scientist is for a public preparation task")
            who = session_name("scientist", tid)
            if task.get("scientist_session") != who:
                tx.update(tid, {"scientist_session": who, "revision": task["revision"] + 1})
                tx.event("task.to_scientist", tid, "handed to a short scientist session")
            fl.write(tx, tid)
            return _result(tid, f"{tid} handed to {who}", fl, wake=[who])
        if task["state"] != "queued":
            raise RLError("CONFLICT", f"{tid} is {task['state']}, not queued: it was released already")
        why = approval_problem(tx, tid)
        if why:
            fl.add("unapproved", f"released although {why}")
        waiting = waits_on(tx, tid)
        who = claimant(task)
        changes = {"state": "open", "released_at": now_utc(), "revision": task["revision"] + 1}
        tx.update(tid, changes)
        if not waiting:
            tx.update(tid, _claim_changes(tx, store, tx.require(tid), who))
        fl.write(tx, tid)
        tx.event("task.released", tid, task["title"] + (f"; waits on {', '.join(waiting)}" if waiting else
                                                        f"; to {who}"))
    if waiting:
        return _result(tid, f"{tid} released; it waits on {', '.join(waiting)}: rl task accept of the last of them "
                            f"hands it to {who}", fl)
    return _result(tid, f"{tid} released to {who}", fl, wake=[who])


def cancel(store: Store, actor: Actor, tid: str, reason: str) -> dict:
    """The task's creator or the coordinator cancels a task (anyone else is flagged); its idle sessions close, a
    busy one is told (3.3). A released hypothesis task usually stops through its scientist's stop conclusion."""
    fl = _check(actor, "coordinator", "scientist", what="cancelling a task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] in TASK_DONE:
            raise RLError("CONFLICT", f"{tid} is already {task['state']}")
        if actor.name not in (task["creator"], task.get("scientist_session"), "coordinator"):
            fl.add("not-holder", f"{tid} was made by {task['creator']}; {actor.name} cancelled it")
        elif actor.role == "coordinator" and task["kind"] == "hypothesis" and task["state"] != "queued":
            fl.add("not-holder", f"the coordinator cancelled the released {tid}; a hypothesis task usually stops "
                                 "through its scientist's stop conclusion")
        tx.update(tid, {"state": "cancelled", "cancel_reason": reason, "revision": task["revision"] + 1})
        fl.write(tx, tid)
        tx.event("task.cancelled", tid, reason)
        if task["state"] == "merging":  # the acceptance it abandons
            sync_candidates(tx, store)
    holder = task.get("holder")
    return _result(tid, f"{tid} cancelled: {reason} (running jobs are not cancelled by this)", fl, close=[tid],
                   wake=[holder] if holder else None)


# -- tests ----------------------------------------------------------------------------------------------------


def test_command(tx: Tx, task: dict) -> str | None:
    """A prep task's done check; an execute task's semantic tests, from the design sealed for it (a later
    revision of the experiment is not this task's)."""
    return test_target(tx, task)[0]


def test_target(tx: Tx, task: dict) -> tuple[str | None, str | None]:
    """(command, design key) of a task's tests, both from one reading of its design: the design sealed for it
    once handed over (a researcher's test credits that handoff), else the draft as it stands."""
    if task["kind"] == "prep":
        return task.get("done_check"), None
    exp = task_design(tx, task["id"])
    return (exp or {}).get("semantic_tests"), design_key(exp)


def run_check(store: Store, task_id: str, command: str, cwd=None) -> tuple[bool, str]:
    where = cwd or gitops.checkout_in(store.project, task_id)
    try:
        out = subprocess.run(command, shell=True, cwd=where, capture_output=True, text=True, timeout=1800,
                             env=_check_env(store))
    except subprocess.TimeoutExpired:
        return False, "timed out after 30 minutes"
    tail = (out.stdout + out.stderr).strip().splitlines()[-6:]
    return out.returncode == 0, " | ".join(tail)[-400:]


def _check_env(store: Store) -> dict:
    import os
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env


def test(store: Store, actor: Actor, tid: str, record: str | None = None, commit: str | None = None,
         log: str | None = None, attempt: str | None = None) -> dict:
    """Run the task's tests (an execute task's semantic tests, a prep task's done check) at its committed head and
    record the outcome; with record (pass|fail), record the outcome of a test job instead (compute.tests: job): the
    job's run says which command it ran at which commit (registered with --purpose design-test), so a command
    changed since is not marked as tested."""
    fl = _check(actor, "scientist", "researcher", what="testing a task")
    with store.read() as tx:
        task = tx.require(tid, "Task")
        command = test_command(tx, task)
    if task.get("holder") != actor.name:
        fl.add("not-holder", f"{tid} is held by {task.get('holder') or 'nobody'}; {actor.name} tested it")
    warnings = []
    if not command:
        raise RLError("INVALID", f"{tid} has no test command: set the experiment's --semantic-tests (execute) or the "
                                 "task's done check (prep)")
    if attempt and not record:
        raise RLError("INVALID", "--attempt names a test job whose outcome you record: give --record pass|fail")
    if record:
        if record not in ("pass", "fail") or not attempt:
            raise RLError("INVALID", "--record pass|fail with --attempt <the attempt of the test job>")
        register = (f"rl run register --task {tid} --request-id <new id> --purpose design-test --minutes N "
                    "[--resource R]")
        with store.read() as tx:
            att = tx.require(attempt, "Attempt")
            run = tx.require(att["run_id"], "Run")
        ran = (run.get("spec") or {}).get("test") or {}
        if run["task_id"] != tid or run.get("purpose") not in ("design-test", "review-test") or not ran.get("command"):
            raise RLError("INVALID", f"{attempt} is not a test job of {tid}: register one with {register}")
        if commit and gitops.rev(store.project, commit) != run["code_commit"]:
            raise RLError("INVALID", f"{attempt} tested {run['code_commit'][:12]}, not {commit[:12]}")
        command, at, design = ran["command"], run["code_commit"], ran.get("design")
        ok, detail = record == "pass", f"job {attempt}, log {log or 'not given'}"
        # results are ordered by when the job ended as observed (the executor's time), else when rl heard of it
        when = att.get("last_trusted_at") if att.get("execution_state") == "ended" else None
        when = when or att.get("closed_at") or now_utc()
    else:
        if tests_mode(store.project.config) == "job":
            warnings.append(f"compute.tests is job: the tests ran here; usually they run as a short job (rl run "
                            f"register --task {tid} --request-id <new id> --purpose design-test --minutes N), then rl "
                            f"task test {tid} --record pass|fail --attempt <ATT>")
        if gitops.dirty(store.project, tid):
            raise RLError("INVALID", f"{tid}'s worktree has uncommitted changes: tests are recorded for a commit")
        at = gitops.head(store.project, tid)
        with store.read() as tx:
            command, design = test_target(tx, tx.require(tid, "Task"))
        ok, detail = run_check(store, tid, command)
        when = now_utc()
    with store.write() as tx:
        task = tx.require(tid, "Task")
        outcome = "pass" if ok else "fail"
        if not attempt and test_target(tx, task) != (command, design):  # the design changed while the test ran
            raise RLError("CONFLICT", f"{tid}'s design or test command changed while the test ran: nothing was "
                                      "recorded; test again")
        same = [t for t in task.get("tests") or [] if attempt and t.get("attempt") == attempt]
        if same:  # one job, one result: a repeated report changes nothing
            if same[0]["outcome"] != outcome:
                raise RLError("CONFLICT", f"{attempt}'s result is recorded as {same[0]['outcome']} already")
            return {"id": tid, "passed": ok, "duplicate": True,
                    "summary": f"{tid}: {attempt}'s result ({outcome}) is recorded already"}
        tests = list(task.get("tests") or []) + [{"commit": at, "command": command, "outcome": outcome, "at": when,
                                                  "by": actor.name, "detail": detail, "design": design,
                                                  **({"attempt": attempt} if attempt else {})}]
        tx.update(tid, {"tests": tests})
        fl.write(tx, tid)
        tx.event("task.tested", tid, f"{'pass' if ok else 'fail'} at {at[:12]}: {command}")
    return {**_result(tid, f"{tid} tests {'pass' if ok else 'FAIL'} at {at[:12]} ({command})"
                      + ("" if ok else f": {detail}") + _manifest_note(store, tid, at), fl, warnings=warnings),
            "passed": ok}


def _manifest_note(store: Store, tid: str, commit: str) -> str:
    """A warning about the task's manifest at this commit (manifest.unapplied), or a parse error, for the one who
    tests it before the review; empty when there is nothing to say."""
    with store.read() as tx:
        exp = task_design(tx, tid)
    if not exp or not exp.get("manifest_path"):
        return ""
    text = gitops.show(store.project, commit, exp["manifest_path"])
    if text is None:
        return f"\nWARNING: {exp['manifest_path']} is not in {commit[:12]}"
    try:
        why = manifest.unapplied(manifest.parse(text, exp["manifest_path"]))
    except RLError as exc:
        return f"\nWARNING: {exc.message}"
    return f"\nWARNING: {why}" if why else ""


def test_runs(task: dict, commit: str, command: str | None, design: str | None = None) -> list[dict]:
    """The recorded runs of this test command at this commit (for this experiment revision, when given), in the
    order they ended."""
    runs = [t for t in task.get("tests") or [] if t["commit"] == commit and t.get("command") == command
            and (design is None or t.get("design") == design)]
    return sorted(runs, key=_instant)


def _instant(t: dict) -> float:
    """A test result's time as an instant (results come with any UTC offset)."""
    try:
        return parse_time(t.get("at")).timestamp()
    except (RLError, TypeError, ValueError):
        return 0.0


def passed_at(task: dict, commit: str, command: str | None, design: str | None = None) -> bool:
    """The latest recorded run of this test command at this commit (for this experiment revision) passed: a later
    failure, a changed command or a new revision needs a new pass."""
    runs = test_runs(task, commit, command, design)
    if not runs or runs[-1]["outcome"] != "pass":
        return False
    # a failure that cannot be put before the latest pass (ended in the same second) needs another pass
    return not any(t["outcome"] != "pass" and _instant(t) >= _instant(runs[-1]) for t in runs)


def review_flags(store: Store, tx: Tx, tid: str, commit: str, fl: Flags) -> None:
    """unreviewed or open-findings when no review covers the commit (5.2), noting whether the protected scope
    changed since the last passed review (what the fast lane used to judge)."""
    for code, text in reviews.review_flags(tx, tid, commit, False, store.project):
        fl.add(code, text)


def answerer(tx: Tx, task: dict) -> str:
    """Who answers a task's blocker and receives its delivery: its creator; for a public preparation task handed to
    a scientist, that scientist."""
    if task["creator"] == "coordinator" and task.get("scientist_session"):
        return task["scientist_session"]
    return task["creator"]


def decider(task: dict) -> str:
    """Who decides a delivery whose tests fail on the merge commit: the coordinator for a public preparation task
    (accept it with --force or return it), else the one who receives its delivery. (A public one whose done check
    fails at delivery goes to the one who receives it: the coordinator, or the scientist it was handed to.)"""
    return "coordinator" if _public(task) else answerer(None, task)


def _dirty_warning(store: Store, tid: str) -> list[str]:
    return [f"{tid}'s worktree has uncommitted changes: what is delivered is its committed head; commit the rest and "
            "deliver again if it belongs to the delivery"] if gitops.dirty(store.project, tid) else []


def _add_after(out: dict, after: dict) -> None:
    for key in ("wake", "close", "next"):
        if after.get(key):
            out[key] = list(out.get(key) or []) + after[key]
    if after.get("notes"):
        out["summary"] += "\n" + "\n".join(after["notes"])


def _after_accept(store: Store, tid: str) -> dict:
    """What an accepted task hands on (3.2, 3.3): the tasks that waited on it and now wait on nothing (a released one
    is given to the session it goes to; a scientist's is its creator's to claim), the sessions whose work ended,
    and the next steps the coordinator would otherwise miss (4)."""
    wake, nxt, notes = [], [], []
    with store.read() as tx:
        task = tx.require(tid, "Task")
        dependents = [tx.require(e["id"]) for e in tx.inc(tid, "depends_on")]
    for d in dependents:
        with store.read() as tx:
            if waits_on(tx, d["id"]) or d["state"] not in ("open", "active", "blocked"):
                continue
        if d["state"] == "open" and not d.get("holder") and d.get("released_at"):
            try:
                with store.write() as tx:
                    fresh = tx.require(d["id"], "Task")
                    if fresh["state"] == "open" and not fresh.get("holder"):
                        tx.update(d["id"], _claim_changes(tx, store, fresh, claimant(fresh)))
                        tx.event("task.claimed", d["id"], f"{claimant(fresh)}, its prerequisites accepted")
                wake.append(claimant(d))
                notes.append(f"{d['id']} goes to {claimant(d)}")
            except RLError as exc:
                notes.append(f"{d['id']} not handed on ({exc.message[:200]}): {claimant(d)} claims it")
                wake.append(claimant(d))
        elif d["state"] == "open" and not d.get("holder"):
            wake.append(d["creator"])
            notes.append(f"{d['id']} waits on nothing now: {d['creator']} claims it")
        elif d.get("holder"):
            nxt.append(f"{d['id']} ({d['holder']}) was taken before {tid} was accepted: merge the mainline into "
                       f"{gitops.branch_name(d['id'])}")
            wake.append(d["holder"])
    if task["kind"] == "hypothesis":
        with store.read() as tx:
            for ph in [e["id"] for e in tx.out(tid, "in_phase")]:
                if phase_ended(tx, ph):
                    nxt.append(f"{ph}'s hypothesis tasks have all ended: make and release its phase conclusion task "
                               f"(rl task create --kind phase --phase {ph} ..)")
            for h in [e["id"] for e in tx.out(tid, "targets")]:
                for p in tx.out(h, "derived_from"):
                    parent = tx.get(p["id"], "Hypothesis")
                    if parent and parent.get("composite"):
                        nxt.append(f"{parent['id']} is composite: once its children are settled, conclude it")
    return {"wake": wake, "close": [tid], "next": nxt, "notes": notes}


# -- dispatch -------------------------------------------------------------------------------------------------


def dispatch(store: Store, actor: Actor, tid: str, note: str | None = None) -> dict:
    """Hand a task to its researcher (3.2): an execute task's design commit (its committed head) is sealed now
    (4.6), with a manifest that parses. Usually its tests pass and a review covers the commit; when not, it is
    dispatched all the same and flagged (untested, unreviewed, open-findings), as it is when its prerequisites are not
    accepted yet (prerequisite) or when someone other than its holder dispatches it."""
    fl = _check(actor, "scientist", what="dispatching a task")
    _note(note, fl)
    with store.read() as tx:
        task = tx.require(tid, "Task")
    if task["kind"] not in ("execute", "prep"):
        raise RLError("INVALID", f"{task['kind']} tasks are not dispatched")
    if task["state"] not in ("open", "active", "blocked") or (task["state"] == "open" and task.get("holder")):
        raise RLError("CONFLICT", f"{tid} is {task['state']}: a task is dispatched while it is worked on")
    if not task.get("worktree") and task["kind"] == "execute":
        raise RLError("INVALID", f"{tid} has no worktree, so no committed design to seal: rl task claim {tid}, "
                                 "commit the design, then dispatch")
    if task.get("holder") and task.get("holder") != actor.name:
        fl.add("not-holder", f"{tid} is held by {task.get('holder') or 'nobody'}; {actor.name} dispatched it")
    warnings = []
    commit = None
    if task["kind"] == "execute":
        if gitops.dirty(store.project, tid):
            warnings.append(f"{tid}'s worktree has uncommitted changes: what is dispatched is its committed head")
        commit = gitops.head(store.project, tid)
    researcher = session_name("researcher", tid)
    with store.write() as tx:
        task = tx.require(tid, "Task")
        waiting = waits_on(tx, tid)
        if waiting:
            fl.add("prerequisite", f"dispatched before {', '.join(waiting)} was accepted: merge the mainline into the "
                                   "branch when it is")
        if not task.get("worktree"):  # a prep task handed on before anyone claimed it: its worktree now
            tx.update(tid, _claim_changes(tx, store, task, researcher))
            task = tx.require(tid, "Task")
        changes = {"state": "active", **_hold(tx, task, researcher), "dispatch_note": note,
                   "revision": task["revision"] + 1}
        if task["kind"] == "execute":
            exp = task_experiment(tx, tid)
            missing = [k for k in ("manifest_path", "semantic_tests") if not exp.get(k)]
            if missing:
                raise RLError("INVALID", f"{exp['id']} needs {', '.join(missing)} before its design is sealed "
                                         "(rl experiment set ..)")
            text = gitops.show(store.project, commit, exp["manifest_path"])
            if text is None:
                raise RLError("INVALID", f"{exp['manifest_path']} is not in {commit[:12]}: the design sealed now "
                                         "needs its manifest")
            parsed = manifest.parse(text, exp["manifest_path"])
            digest = digest_json(parsed)
            if exp.get("design_commit") and exp.get("manifest_digest") != digest:
                raise RLError("INVALID", f"{exp['id']}@{exp['revision']} was sealed with another manifest: a changed "
                                         f"manifest is a new revision (rl experiment set {exp['id']} --reason ..)")
            if not passed_at(task, commit, exp["semantic_tests"], design_key(exp)):
                fl.add("untested", f"no passing test of `{exp['semantic_tests']}` at {commit[:12]} is recorded")
            review_flags(store, tx, tid, commit, fl)
            if not exp.get("design_commit"):
                tx.update(exp["id"], {"design_commit": commit, "sealed_at": now_utc(), "sealed_by": tid,
                                      "manifest": parsed, "manifest_digest": digest})
                tx.event("experiment.sealed", exp["id"], f"@{exp['revision']} at {commit[:12]} by dispatch of {tid}")
            exp = tx.require(exp["id"])
            changes.update(design_commit=commit, sealed_design=sealed_copy(exp))
        tx.update(tid, changes)
        fl.write(tx, tid)
        tx.event("task.dispatched", tid, note or "")
    return _result(tid, f"{tid} dispatched to {researcher}" + (f"; design {commit[:12]} sealed" if commit else "")
                   + (_manifest_note(store, tid, commit) if commit else ""), fl, warnings=warnings, wake=[researcher])


# -- blockers -------------------------------------------------------------------------------------------------


def block(store: Store, actor: Actor, tid: str, reason: str, note: str) -> dict:
    """A researcher stops on a design or mandate problem, or only asks what something means (8.2); its creator is
    told."""
    fl = _check(actor, "researcher", what="blocking a task")
    _note(note, fl)
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "active":
            raise RLError("CONFLICT", f"{tid} is {task['state']}: a task is blocked while it is worked on")
        if task.get("holder") != actor.name:
            fl.add("not-holder", f"{tid} is held by {task.get('holder')}; {actor.name} blocked it")
        tx.update(tid, {"state": "blocked", "block_reason": reason, "block_note": note, "answer_note": None,
                        "revision": task["revision"] + 1})
        fl.write(tx, tid)
        tx.event("task.blocked", tid, f"{reason}: {note}")
        to = answerer(tx, task)
    return _result(tid, f"{tid} blocked ({reason})", fl, wake=[to])


def answer(store: Store, actor: Actor, tid: str, text: str) -> dict:
    """The scientist (or the short scientist session of a public prep task) answers a blocker; the task goes back to
    its researcher, who is told. A changed design is a new experiment revision written before this answer."""
    fl = _check(actor, "scientist", what="answering a blocker")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "blocked":
            raise RLError("CONFLICT", f"{tid} is {task['state']}, not blocked")
        if actor.name not in (task["creator"], task.get("scientist_session")) and \
                (hypothesis_task_of(tx, task) or {}).get("holder") != actor.name:
            fl.add("not-holder", f"{tid}'s blockers go to {answerer(tx, task)}; {actor.name} answered")
        live = task_experiment(tx, tid) if task["kind"] == "execute" else None
        sealed = task.get("sealed_design") or {}
        if live and sealed and live["revision"] != sealed.get("revision"):
            # a changed design is handed over like the first one: its tests, its review, a dispatch
            tx.update(tid, {"state": "active", "answer_note": text, "revision": task["revision"] + 1,
                            "sealed_design": None, **_hold(tx, task, actor.name)})
            fl.write(tx, tid)
            tx.event("task.answered", tid, f"design changed to {live['id']}@{live['revision']}: {text[:250]}")
            return _result(tid, f"{tid} is back with you: {live['id']} is now at revision {live['revision']}, so "
                                f"commit the design, rl task test {tid}, get it reviewed and rl task dispatch {tid} "
                                f"again; runs already registered stay on revision {sealed.get('revision')}", fl)
        tx.update(tid, {"state": "active", "answer_note": text, "revision": task["revision"] + 1})
        fl.write(tx, tid)
        tx.event("task.answered", tid, text[:300])
    return _result(tid, f"{tid} back with {task.get('holder')}", fl, wake=[task.get("holder")])


def set_prep(store: Store, actor: Actor, tid: str, requirement: str | None, done_check: str | None) -> dict:
    """The maker of a prep task (or the short scientist session of a public one) changes its requirement or done
    check."""
    fl = _check(actor, "scientist", "coordinator", what="changing a preparation task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["kind"] != "prep":
            raise RLError("INVALID", f"{tid} is a {task['kind']} task, not a preparation task")
        if actor.name not in (task["creator"], task.get("scientist_session")):
            fl.add("not-holder", f"{tid} was made by {task['creator']}; {actor.name} changed it")
        tx.update(tid, {k: v for k, v in (("requirement", requirement), ("done_check", done_check)) if v})
        fl.write(tx, tid)
        tx.event("task.prep_changed", tid, f"requirement/done check changed by {actor.name}")
    return _result(tid, f"{tid} changed", fl)


# -- delivery -------------------------------------------------------------------------------------------------


def deliver(store: Store, actor: Actor, tid: str, conclusion: str | None = None, limitations: str | None = None,
            outputs: list[str] = (), reusable: str | None = None, note: str | None = None) -> dict:
    with store.read() as tx:
        task = tx.require(tid, "Task")
    work = task["kind"] in ("execute", "prep")
    fl = _check(actor, "researcher" if work else "scientist", what=f"delivering a {task['kind']} task")
    _note(note, fl)
    if task["state"] not in ("active", "blocked") or not task.get("worktree"):
        raise RLError("CONFLICT", f"{tid} is {task['state']}: a task is delivered while it is worked on")
    if task.get("holder") != actor.name:
        fl.add("not-holder", f"{tid} is held by {task.get('holder') or 'nobody'}; {actor.name} delivered it")
    if work:
        return _deliver_work(store, actor, tid, conclusion, limitations, list(outputs), reusable, note, fl,
                             task["revision"])
    return _deliver_scientist(store, actor, tid, conclusion, note, fl, task["revision"])


def _still(task: dict, seen: int) -> None:
    """The task as the delivery found it: a cancellation, takeover or return meanwhile is not overwritten."""
    if task["revision"] != seen or task["state"] not in ("active", "blocked"):
        raise RLError("CONFLICT", f"{task['id']} changed while it was being delivered (now {task['state']}): "
                                  "nothing was delivered; look at it again")


def _deliver_work(store: Store, actor: Actor, tid: str, conclusion, limitations, outputs, reusable, note,
                  fl: Flags, seen: int) -> dict:
    if not conclusion or not limitations:
        raise RLError("INVALID", "a task conclusion has one sentence (--conclusion, its direction only, citing "
                                 "evidence; no copied numbers) and its limits (--limitations)")
    warnings = _dirty_warning(store, tid)
    head = gitops.head(store.project, tid)
    bad = gitops.tracked_and_committed(store.project, tid, outputs)
    if bad:
        raise RLError("INVALID", f"outputs not committed on {gitops.branch_name(tid)}: {', '.join(bad)} (an output "
                                 "is recorded at the delivered commit)")
    with store.read() as tx:
        task = tx.require(tid, "Task")
        if task["kind"] == "execute" and not tx.all("Evidence", task_id=tid):
            fl.add("no-evidence", f"{tid} was delivered without evidence (rl evidence add --task {tid})")
        command = test_command(tx, task)
    public = _public(task)
    check = None
    if task["kind"] == "prep":
        if tests_mode(store.project.config) == "local":
            with gitops.temp_checkout(store.project, head) as where:  # the delivered commit, not the worktree
                ok, detail = run_check(store, tid, command, cwd=where)
            check = {"commit": head, "command": command, "outcome": "pass" if ok else "fail", "at": now_utc(),
                     "by": "cli", "detail": detail}
            if not ok:
                fl.add("done-check", f"the done check fails at {head[:12]}: {detail}"[:400])
        elif not passed_at(task, head, command):
            fl.add("done-check", f"no passing done check is recorded at {head[:12]} (run it as a job, then rl task "
                                 f"test {tid} --record pass --attempt <ATT>)")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        _still(task, seen)
        changes = {"state": "delivered", "conclusion": conclusion, "limitations": limitations,
                   "delivered_commit": head, "delivered_at": now_utc(), "state_note": note, "return_note": None,
                   "outputs": [{"path": p, **({"reusable": reusable} if reusable else {})} for p in outputs] or None,
                   "revision": task["revision"] + 1, "merge_state": None, "merge_note": None}
        if check:
            changes["tests"] = list(task.get("tests") or []) + [check]
        tx.update(tid, changes)
        fl.write(tx, tid)
        tx.event("task.delivered", tid, conclusion)
        to = answerer(tx, task)
    out = _result(tid, f"{tid} delivered at {head[:12]}", fl, warnings=warnings, wake=[to])
    if public and not any(f["code"] == "done-check" for f in fl.items):
        # the CLI accepts a public preparation task on its done check (6.4), then tells as rl task accept does
        out["summary"] += "; " + _merge(store, tid, run_tests=False)
        with store.read() as tx:
            now = tx.require(tid, "Task")
        if now["state"] == "accepted":
            _add_after(out, _after_accept(store, tid))
        elif now["state"] == "delivered":  # its tests fail on the merge commit: the coordinator decides
            out["wake"] = [decider(now)]
    elif public:
        out["summary"] += (f"; not accepted on delivery since its done check does not pass: {to} accepts it or "
                           "returns it" + (", or hands it to a scientist" if to == "coordinator" else ""))
    return out


def _deliver_scientist(store: Store, actor: Actor, tid: str, conclusion: str | None, note: str | None,
                       fl: Flags, seen: int) -> dict:
    with store.read() as tx:
        task = tx.require(tid, "Task")
        base = gitops.fork_point(store.project, tid)
    warnings = _dirty_warning(store, tid)
    head = gitops.head(store.project, tid)
    outside = gitops.only_under(store.project, base, head, work_dir(tid)) if base else []
    if outside:
        fl.add("outside-work-dir", f"a {task['kind']} task changes only {work_dir(tid)}/; it also changed "
                                   f"{', '.join(outside[:5])}" + (" …" if len(outside) > 5 else ""))
    if task["kind"] == "hypothesis" and not conclusion:
        raise RLError("INVALID", "a hypothesis task is delivered with --conclusion: one sentence, its direction only")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        _still(task, seen)
        if task["kind"] == "hypothesis":
            targets = [e["id"] for e in tx.out(tid, "targets") if e["label"] == "Hypothesis"]
            finals = [d for d in tx.all("Decision", task_id=tid) if d["state"] == "final"
                      and d["kind"] in ("verdict", "stop")]
            decided = {e["id"] for d in finals for e in tx.out(d["id"], "decides")}
            missing = [h for h in targets if h not in decided]
            if missing:
                fl.add("no-verdict", f"no final verdict or stop on {', '.join(missing)}")
            bare = [d["id"] for d in finals if d["kind"] == "verdict" and not (d.get("did") and d.get("means"))]
            if bare:
                fl.add("no-verdict", f"{', '.join(bare)} lacks its interpretation (--did, --means)")
            unfinished = [e["id"] for e in tx.inc(tid, "part_of") if tx.require(e["id"])["state"] not in TASK_DONE]
            if unfinished:
                fl.add("open-children", f"tasks still open under it: {', '.join(unfinished)}")
        phase = None
        if task["kind"] == "phase":
            phase = ([e["id"] for e in tx.out(tid, "targets") if e["label"] == "Phase"] or [None])[0]
            ds = [d for d in tx.all("Decision", task_id=tid) if d["kind"] == "phase"
                  and any(e["id"] == phase for e in tx.out(d["id"], "decides"))]
            if not ds:
                fl.add("no-verdict", f"no phase decision decides {phase} (rl decide --kind phase --targets {phase} ..)")
        tx.update(tid, {"state": "delivered", "conclusion": conclusion, "delivered_commit": head,
                        "delivered_at": now_utc(), "state_note": note, "return_note": None,
                        "revision": task["revision"] + 1})
        if task["kind"] == "phase":
            for old in tx.all("Ask", kind="report"):
                if old["state"] == "open":
                    tx.update(old["id"], {"state": "closed"})
            rq = tx.next_id("Ask")
            tx.create("Ask", {"id": rq, "kind": "report", "task_id": tid, "state": "open", "by": "cli",
                              "stuck": f"the phase report of {phase} is on the research page"})
        fl.write(tx, tid)
        tx.event("task.delivered", tid, conclusion or task["kind"])
    return _result(tid, f"{tid} delivered; the coordinator receives it", fl, warnings=warnings,
                   wake=[task["creator"]])


# -- acceptance -----------------------------------------------------------------------------------------------


def accept(store: Store, actor: Actor, tid: str, force: bool = False, reason: str | None = None) -> dict:
    """The creator accepts a delivery, which merges it (6.5): a scientist the execute and prep tasks it made, the
    coordinator the hypothesis and phase tasks (anyone else is flagged). Run again on an acceptance in progress, it
    goes on with it. --force --reason moves the mainline although the tests on the merge commit fail (the one
    forced step, 5.1); a merge conflict is never forced."""
    if force and not reason:
        raise RLError("INVALID", "--force moves the mainline past failing tests: say why (--reason ..)")
    with store.read() as tx:
        task = tx.require(tid, "Task")
    mine = ("coordinator",) if task["kind"] in ("tree", "hypothesis", "phase") else ("scientist", "coordinator")
    fl = _check(actor, *mine, what=f"accepting a {task['kind']} task")
    resumed = []
    if task["state"] == "merging" or (task["state"] == "delivered" and task.get("merge_state") == "merging"):
        resumed = [x for x in reconcile_merges(store, only=tid) if x]
        with store.read() as tx:
            task = tx.require(tid, "Task")
        running = _local_merge_running(store, task)
        if task["state"] != "delivered" or task.get("merge_state") != "merging" or running:
            out = _result(tid, "; ".join(resumed) or (running if task["state"] == "delivered" else
                                                      f"{tid} is {task['state']}: its acceptance waits on its "
                                                      "merge-test job"))
            if task["state"] == "accepted":
                _add_after(out, _after_accept(store, tid))
            return out
        resumed.append(f"{tid}'s acceptance was interrupted before the mainline moved: it starts again")
    if task["state"] != "delivered":
        raise RLError("CONFLICT", f"{tid} is {task['state']}, not delivered")
    if actor.name not in (task["creator"], task.get("scientist_session"), "coordinator"):
        fl.add("not-holder", f"{tid} was made by {task['creator']}, who accepts it; {actor.name} did")
    if fl:
        with store.write() as tx:
            fl.write(tx, tid)
    run_tests = task["kind"] in ("execute", "prep")
    summary = "; ".join(resumed + [f"{tid}: " + _merge(store, tid, run_tests=run_tests, seen=task["revision"],
                                                        force=reason if force else None, by=actor.name)])
    with store.read() as tx:
        task = tx.require(tid, "Task")
    out = _result(tid, summary, fl)
    if task["state"] == "accepted":
        _add_after(out, _after_accept(store, tid))
        if force and FORCED in summary:
            out["flags"] = list(out.get("flags") or []) + [f for f in task.get("flags") or []
                                                           if f["code"] == "force"][-1:]
    return out


def _merge(store: Store, tid: str, run_tests: bool, seen: int | None = None, force: str | None = None,
           by: str = "cli") -> str:
    """Accept a delivery (6.5): a merge commit of the delivered commit and the mainline, the task's tests on it in a
    temporary checkout, then the mainline moves to it. What merges is the delivered commit, whatever the branch
    holds now. The acceptance is recorded first (its candidate and round), so a finish, a failure, a test job and
    a restart all act on this acceptance only. A conflict or a failing test leaves the mainline where it was, unless
    force (the reason) is given: then failing tests (or a test job not run) move it all the same, flagged `force`.
    seen: the task's revision when the one accepting read it; a task returned and delivered again since (even at the
    same commit, with another conclusion) is not accepted on that reading."""
    with store.read() as tx:
        task = tx.require(tid, "Task")
        command = test_command(tx, task) if run_tests or task["kind"] == "prep" else None
    seen = task["revision"] if seen is None else seen
    if task["revision"] != seen:
        raise RLError("CONFLICT", f"{tid} changed since you read it (now {task['state']}): read it again")
    delivered = task.get("delivered_commit")
    if not delivered:
        raise RLError("INVALID", f"{tid} has no delivered commit: deliver it first")
    public = _public(task)
    recovered = _recover(store, task, public)
    if recovered:
        return recovered
    head = gitops.head(store.project, tid)
    if head != delivered:
        raise RLError("CONFLICT", f"{tid}'s branch is at {head[:12]}, not at its delivery {delivered[:12]}: what is "
                                  "accepted is what was delivered, so deliver it again (or reset the extra commits)")
    commit, conflicts, contained = gitops.merge_candidate(store.project, tid, delivered)
    command = None if contained else command  # a delivery the mainline already holds: nothing new to test (6.5)
    job_mode = bool(command) and tests_mode(store.project.config) == "job"
    note = ""
    if force and job_mode:  # --force moves the mainline past tests that failed, not past tests never run
        with store.read() as tx:
            earlier = _failed_tests(tx, task, delivered)
        if not earlier:
            note = ("--force applies once the tests on the merge commit have failed; none has failed for this "
                    "delivery yet, so its test job runs first. ")
            force = None
    job = job_mode and not force
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "delivered" or task.get("delivered_commit") != delivered or task["revision"] != seen:
            raise RLError("CONFLICT", f"{tid} changed meanwhile (now {task['state']}): nothing was merged")
        rnd = task["revision"] + 1
        if conflicts:
            why = f"merge conflict in {', '.join(conflicts[:8])}"
            tx.update(tid, _conflict_changes(task, why, public))
            tx.event("task.merge_conflict", tid, why)
            return _conflict_text(why, public)
        tx.update(tid, {"merge_commit": commit, "merge_state": "merging", "revision": rnd,
                        **({"state": "merging"} if job else {"merge_proc": _this_process()})})
        tx.event("task.merging", tid, f"candidate {commit[:12]} of delivery {delivered[:12]}"
                                      + (": its tests run as a job" if job else ""))
        if job:
            sync_candidates(tx, store)
    if job:
        return note + (f"merge commit {commit[:12]} made (branch {gitops.candidate_branch(store.project, tid)}); run its "
                f"tests as a short job: rl run register --task {tid} --request-id <new id> --purpose merge-test "
                f"--minutes N [--resource R]; the mainline moves when it passes (rl task accept {tid} goes on with "
                "it after the job's end is recorded)")
    forced = None
    if command and job_mode:  # forced past a test job of this delivery that failed (earlier, above)
        forced = (f"its test job {earlier[-1]['id']} failed ({earlier[-1]['outcome']}); the tests were not run again "
                  f"on the merge commit {commit[:12]} (--force): {force}")
    elif command:
        with gitops.temp_checkout(store.project, commit) as where:
            ok, detail = run_check(store, tid, command, cwd=where)
        if not ok and not force:
            return _conflict(store, tid, f"tests fail on the merge commit {commit[:12]}: {detail}", public,
                             delivered, rnd, tests=True) + f" (rl task accept {tid} --force --reason .. moves it " \
                                                           "all the same)"
        if not ok:
            forced = f"tests fail on the merge commit {commit[:12]} ({detail[:200]}): {force}"
    return _advance(store, tid, commit, delivered, rnd, public, "merged", forced=forced, by=by)


def _recover(store: Store, task: dict, public: bool) -> str | None:
    """An acceptance whose record was interrupted after the mainline moved: its recorded candidate is on the
    mainline, so it is finished as it was tested, whatever the mainline holds since."""
    commit = task.get("merge_commit")
    if task.get("merge_state") != "merging" or not commit or task["state"] not in ("delivered", "merging") \
            or not gitops.exists(store.project, commit) \
            or not gitops.is_ancestor(store.project, commit, gitops.mainline(store.project)):
        return None
    with store.read() as tx:  # a test job of this round that is still out, or did not pass, decides instead
        jobs = [a for r in tx.all("Run", task_id=task["id"]) if r.get("purpose") == "merge-test"
                and r.get("code_commit") == commit
                and ((r.get("spec") or {}).get("acceptance") or {}).get("round") == task["revision"]
                for a in tx.all("Attempt", run_id=r["id"])]
    if any(a["execution_state"] != "ended" or a["outcome"] != "success" for a in jobs):
        return None
    return _advance(store, task["id"], commit, task["delivered_commit"], task["revision"], public,
                    "merged (finished after an interruption)", needs_job=task["state"] == "merging")


def _same(task: dict, delivered: str, rnd: int) -> bool:
    """The task is still in the acceptance that was started: the same delivery, the same round, not ended."""
    return task["state"] in ("delivered", "merging") and task.get("delivered_commit") == delivered \
        and task["revision"] == rnd


def _round_jobs(tx: Tx, task: dict) -> list[dict]:
    """The merge-test attempts of the task's current acceptance round (its candidate, its round)."""
    return [a for r in tx.all("Run", task_id=task["id"]) if r.get("purpose") == "merge-test"
            and r.get("code_commit") == task.get("merge_commit")
            and ((r.get("spec") or {}).get("acceptance") or {}).get("round") == task["revision"]
            for a in tx.all("Attempt", run_id=r["id"])]


def _wanted(tx: Tx) -> dict[str, str]:
    return {t["id"]: t["merge_commit"] for t in tx.all("Task") if t["state"] == "merging" and t.get("merge_commit")}


def sync_candidates(tx: Tx, store: Store) -> None:
    """The candidate branches as the records say, under their write lock (gitops.set_candidates)."""
    gitops.set_candidates(store.project, _wanted(tx))


def repair_candidates(store: Store) -> bool:
    """Bring the candidate branches back to the records (a process stopped between the two): only when they
    differ, so a beat with nothing to repair writes nothing. True when something was repaired."""
    with store.read() as tx:
        wanted = _wanted(tx)
    if gitops.candidate_refs(store.project) == wanted:
        return False
    with store.write() as tx:
        sync_candidates(tx, store)
    return True


def _advance(store: Store, tid: str, commit: str, delivered: str, rnd: int, public: bool, how: str,
             needs_job: bool = False, forced: str | None = None, by: str = "cli") -> str:
    """Move the mainline to the tested merge commit and accept the task, under the write lock and only for the
    acceptance that tested it: a task returned, cancelled or delivered again meanwhile keeps the mainline where it
    is. A mainline that already holds the commit (a move whose record was interrupted) is accepted as it is.
    needs_job: the round's test jobs decide, as the records say under the write lock (a failure, also one recorded
    after a success, rejects it; an attempt corrected to running meanwhile is waited on). forced: why the mainline
    moves although the tests failed (a flag and an event)."""
    if needs_job:  # a round that still waits changes nothing, so it writes nothing
        with store.read() as tx:
            task = tx.require(tid, "Task")
            jobs = _round_jobs(tx, task) if _same(task, delivered, rnd) and task.get("merge_commit") == commit \
                else None
        if jobs is not None and not _failed(jobs) and not (jobs and all(a["execution_state"] == "ended"
                                                                         for a in jobs)):
            return f"{tid} waits on its other merge-test attempts of this round"
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if not _same(task, delivered, rnd) or task.get("merge_commit") != commit:
            raise RLError("CONFLICT", f"{tid} changed during its acceptance (now {task['state']}): the mainline did "
                                      "not move")
        jobs = _round_jobs(tx, task) if needs_job else []
        failed = _failed(jobs)
        if failed:  # any test job of the round that did not pass decides it
            why = (f"the test job {failed[0]['id']} on {commit[:12]} ended {failed[0]['outcome']}: "
                   f"{failed[0].get('ended_note') or ''}")[:300]
            tx.update(tid, _conflict_changes(task, why, public, tests=True))
            tx.event("task.merge_conflict", tid, why)
            sync_candidates(tx, store)
            return _conflict_text(why, public, tests=True) + f" (rl task accept {tid} --force --reason .. moves it " \
                                                             "all the same)"
        if needs_job and not (jobs and all(a["execution_state"] == "ended" for a in jobs)):
            return f"{tid} waits on its other merge-test attempts of this round"  # not an error: they decide later
        main = gitops.mainline(store.project)
        why = None if gitops.is_ancestor(store.project, commit, main) else gitops.move_mainline(store.project, commit)
        if why:
            changes = _conflict_changes(task, f"the mainline could not move to {commit[:12]}: {why}", public)
            tx.update(tid, changes)
            tx.event("task.merge_conflict", tid, changes["merge_note"])
            sync_candidates(tx, store)
            return _conflict_text(changes["merge_note"], public)
        tx.update(tid, {"state": "accepted", "merge_state": "merged", "merge_commit": commit, "merge_note": None,
                        "merge_proc": None, "revision": task["revision"] + 1})
        if forced:
            flags.add(tx, tid, "force", forced, by)
            tx.event("task.forced", tid, forced[:300])
        tx.event("task.accepted", tid, f"{how} at {commit[:12]}")
        sync_candidates(tx, store)
    gitops.follow(store.project, tid, delivered, commit)
    return f"accepted and merged ({commit[:12]}); " + (FORCED if forced else "the mainline moved")


def _failed(jobs: list[dict]) -> list[dict]:
    return [a for a in jobs if a["execution_state"] == "ended" and a["outcome"] != "success"]


FORCED = "the mainline moved past failing tests (--force)"


def _conflict_changes(task: dict, why: str, public: bool, tests: bool = False) -> dict:
    # a public preparation task with a merge conflict goes back to the researcher who delivered it (6.4); one whose
    # tests failed stays delivered, so the coordinator can accept it with --force or return it
    return {"merge_state": "conflict", "merge_note": why, "revision": task["revision"] + 1, "merge_proc": None,
            "state": "active" if public and not tests else "delivered"}


def _conflict_text(why: str, public: bool, tests: bool = False) -> str:
    return (f"NOT merged, the mainline did not move: {why}. "
            + ("It waits on the coordinator, who returns it to its researcher or accepts it with --force"
               if public and tests else
               "It is back with its researcher, who merges the mainline into the branch and delivers again"
               if public else "Open a small task for a researcher to resolve it, or fix it and accept again"))


def _conflict(store: Store, tid: str, why: str, public: bool, delivered: str, rnd: int, tests: bool = False) -> str:
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if not _same(task, delivered, rnd):
            return f"{why}; {tid} changed meanwhile (now {task['state']}), so nothing was recorded"
        tx.update(tid, _conflict_changes(task, why, public, tests))
        tx.event("task.merge_conflict", tid, why)
        sync_candidates(tx, store)
    return _conflict_text(why, public, tests)


def _failed_tests(tx: Tx, task: dict, delivered: str) -> list[dict]:
    """The merge-test attempts of this delivery (any round) that ended without success."""
    return [a for r in tx.all("Run", task_id=task["id"]) if r.get("purpose") == "merge-test"
            and ((r.get("spec") or {}).get("acceptance") or {}).get("delivered") == delivered
            for a in tx.all("Attempt", run_id=r["id"])
            if a["execution_state"] == "ended" and a["outcome"] != "success"]


def _this_process() -> dict:
    import os
    import socket
    return {"host": socket.gethostname(), "pid": os.getpid(), "at": now_utc()}


def _local_merge_running(store: Store, task: dict) -> str | None:
    """Why a local acceptance in progress (delivered, merging) is still running elsewhere, or None when it was
    interrupted: it started longer ago than its tests may take (a live pid then is another process: pids are
    reused), or its process is gone on this host."""
    import os
    import socket
    proc = task.get("merge_proc") or {}
    started = _minutes_since_event(store, task["id"], "task.merging")
    if started > STALE_MERGE_MINUTES:  # its tests time out before this: a live pid now is another process
        return None
    if proc.get("host") == socket.gethostname() and proc.get("pid"):
        try:
            os.kill(int(proc["pid"]), 0)
        except ProcessLookupError:
            return None
        except (PermissionError, OSError, ValueError):
            pass
        if int(proc["pid"]) != os.getpid():
            return f"{task['id']}'s acceptance runs in process {proc['pid']} (started {proc.get('at')})"
        return None
    return (f"{task['id']}'s acceptance started less than {STALE_MERGE_MINUTES} minutes ago on "
            f"{proc.get('host') or 'another process'}: rl task accept {task['id']} starts it again after that")


def finish_merge(store: Store, attempt: str) -> str | None:
    """The merge-test job of an acceptance ended (runs.py): move the mainline or record a conflict, for the
    acceptance that job was registered for only (its candidate and round). Runs again harmlessly."""
    with store.read() as tx:
        att = tx.require(attempt, "Attempt")
        run = tx.require(att["run_id"], "Run")
        task = tx.require(run["task_id"], "Task")
    acc = (run.get("spec") or {}).get("acceptance") or {}
    if run.get("purpose") != "merge-test" or att["execution_state"] != "ended" or task["state"] != "merging" \
            or not _same(task, acc.get("delivered"), acc.get("round")) or task.get("merge_commit") != run.get(
                "code_commit"):
        return None
    public = task["kind"] == "prep" and task["creator"] == "coordinator"
    # a failure and a success alike go to one decision under the write lock, on the attempts as they stand then
    try:
        return f"{task['id']}: " + _advance(store, task["id"], task["merge_commit"], acc["delivered"], acc["round"],
                                            public, f"merged after its test job {attempt}", needs_job=True)
    except RLError as exc:  # the acceptance ended meanwhile: this job no longer decides anything
        if exc.code != "CONFLICT":
            raise
        return f"{task['id']}: {exc.message}"


def reconcile_merges(store: Store, only: str | None = None) -> list[str]:
    """Acceptances waiting on a test job whose end is already recorded (a crash between the two, or an end that came
    through rl run record): finish them now; and acceptances interrupted after their candidate reached the mainline
    (a public preparation task's too, which nobody accepts by hand). rl task accept runs it for its task (only),
    rl resume for all."""
    repair_candidates(store)
    with store.read() as tx:
        every = [t for t in tx.all("Task") if only is None or t["id"] == only]
        merging = [t for t in every if t["state"] == "merging"]
        ended = [a["id"] for t in merging for r in tx.all("Run", task_id=t["id"])
                 if r.get("purpose") == "merge-test" and r.get("code_commit") == t.get("merge_commit")
                 for a in tx.all("Attempt", run_id=r["id"]) if a["execution_state"] == "ended"]
        stuck = [t for t in every if t["state"] in ("delivered", "merging") and (
            t.get("merge_state") == "merging" or (_public(t) and t["state"] == "delivered"
                                                  and t.get("merge_state") != "conflict"
                                                  and not any(f["code"] == "done-check" for f in t.get("flags") or [])))]
    with store.read() as tx:  # a failure decides a round before any success of it
        ended.sort(key=lambda a: tx.require(a)["outcome"] == "success")
    done = [x for x in (finish_merge(store, a) for a in ended) if x]
    for t in stuck:
        try:
            got = _recover(store, t, _public(t))
        except RLError:
            continue
        if got:
            done.append(f"{t['id']}: {got}")
        elif _public(t) and t["state"] == "delivered" and min(
                _minutes_since_event(store, t["id"], "task.merging"),
                _minutes_since_event(store, t["id"], "task.delivered")) > STALE_MERGE_MINUTES:
            try:  # its automatic acceptance stopped before the mainline moved: start it again
                done.append(f"{t['id']}: " + _merge(store, t["id"], run_tests=False))
            except RLError as exc:
                done.append(f"{t['id']}: {exc.message}")
    return done


def _public(task: dict) -> bool:
    """A public preparation task: the CLI accepts it on delivery, nobody accepts it by hand (6.4)."""
    return task["kind"] == "prep" and task["creator"] == "coordinator"


STALE_MERGE_MINUTES = 30  # an acceptance in progress longer than this was interrupted (its tests time out at 30)


def _minutes_since_event(store: Store, tid: str, kind: str) -> float:
    with store.read() as tx:
        found = [e for e in tx.all("Event", entity_id=tid) if e["type"] == kind]
    try:
        return (datetime.now(timezone.utc) - parse_time(found[-1]["created_at"])).total_seconds() / 60
    except (IndexError, KeyError, RLError, TypeError):
        return float("inf")


def return_task(store: Store, actor: Actor, tid: str, reason: str) -> dict:
    """Send a delivery back (8.2): its receiver returns it to its holder, who is told."""
    with store.read() as tx:
        task = tx.require(tid, "Task")
    mine = ("coordinator",) if task["kind"] in ("tree", "hypothesis", "phase") else ("scientist", "coordinator")
    fl = _check(actor, *mine, what="returning a delivery")
    _note(reason, fl)
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "delivered":
            raise RLError("CONFLICT", f"{tid} is {task['state']}, not delivered")
        if actor.name not in (task["creator"], task.get("scientist_session"), "coordinator"):
            fl.add("not-holder", f"{tid} was made by {task['creator']}; {actor.name} returned it")
        tx.update(tid, {"state": "active", "return_note": reason, "merge_state": None,
                        "revision": task["revision"] + 1})
        fl.write(tx, tid)
        tx.event("task.returned", tid, reason)
    return _result(tid, f"{tid} returned to {task['holder']}: {reason}", fl, wake=[task.get("holder")])


# -- phases ---------------------------------------------------------------------------------------------------


def phase_create(store: Store, actor: Actor, title: str, budget: str | None, tasks: list[str] = ()) -> dict:
    """A phase: the hypothesis tasks it holds (the first phase also the public preparation tasks) and a budget that
    is only shown (7.5). As many tasks run at once as the coordinator releases."""
    fl = _check(actor, "coordinator", what="making a phase")
    with store.write() as tx:
        ph = tx.next_id("Phase")
        tx.create("Phase", {"id": ph, "title": title, "budget": budget})
        for t in tasks:
            _add_to_phase(tx, ph, t)
        tx.event("phase.created", ph, title)
    return _result(ph, f"{ph} {title}", fl)


def phase_add(store: Store, actor: Actor, ph: str, tasks: list[str]) -> dict:
    fl = _check(actor, "coordinator", what="changing a phase")
    with store.write() as tx:
        tx.require(ph, "Phase")
        for t in tasks:
            _add_to_phase(tx, ph, t)
        tx.event("phase.changed", ph, f"added {', '.join(tasks)}")
    return _result(ph, f"{ph}: added {', '.join(tasks)}", fl)


def _add_to_phase(tx: Tx, ph: str, tid: str) -> None:
    task = tx.require(tid, "Task")
    if task["kind"] not in ("hypothesis", "prep"):
        raise RLError("INVALID", f"a phase holds hypothesis and public preparation tasks; {tid} is {task['kind']}")
    if tx.out(tid, "in_phase"):
        raise RLError("CONFLICT", f"{tid} is already in a phase")
    tx.link("in_phase", tid, ph)


def phase_tasks(tx: Tx, ph: str) -> list[dict]:
    return [tx.require(e["id"]) for e in tx.inc(ph, "in_phase")]


def phase_ended(tx: Tx, ph: str) -> bool:
    """Every hypothesis task of the phase received or cancelled (7.5); the budget does not end it."""
    hyp = [t for t in phase_tasks(tx, ph) if t["kind"] == "hypothesis"]
    return bool(hyp) and all(t["state"] in TASK_DONE for t in hyp)


# -- decision requests ----------------------------------------------------------------------------------------


def ask(store: Store, actor: Actor, stuck: str, options: str, recommend: str, cost: str | None = None,
        task_id: str | None = None, kind: str = "decision") -> dict:
    """A decision request (7.3): a scientist's on its hypothesis task goes to the coordinator, who is told; the
    coordinator's goes to the user (it asks them in its pane). kind approval: the coordinator asks the user to approve
    a task, e.g. a hypothesis task predicted to go past the budget set at the start (7.2)."""
    fl = _check(actor, *(("coordinator",) if kind == "approval" else ("scientist", "coordinator")),
                what="asking the user to approve" if kind == "approval" else "a decision request")
    task_id = task_id or actor.subject
    if not task_id:
        raise RLError("INVALID", "a request is made on a task (--task T-x)")
    to_user = kind == "approval" or actor.role == "coordinator"
    with store.write() as tx:
        task = tx.require(task_id, "Task")
        if kind == "decision" and actor.role == "scientist" and task["kind"] not in ("hypothesis", "tree"):
            fl.add("role", f"a decision request is made on a hypothesis task; {task_id} is {task['kind']}")
        rq = tx.next_id("Ask")
        tx.create("Ask", {"id": rq, "kind": kind, "task_id": task_id, "by": actor.name,
                          "state": "forwarded" if to_user else "open", "stuck": stuck, "options": options,
                          "recommend": recommend, "cost": cost, "forwarded_at": now_utc() if to_user else None})
        fl.write(tx, rq)
        tx.event("ask.created", rq, f"{kind} on {task_id}: {stuck[:200]}")
    if to_user:
        return _result(rq, f"{rq} waits for the user: ask them in your pane, then rl request answer {rq} --by user "
                           + ("--approve|--change " if kind == "approval" else "") + "--text \"<their words>\"", fl)
    return _result(rq, f"{rq} waits for the coordinator", fl, wake=["coordinator"])


def forward(store: Store, actor: Actor, rq: str) -> dict:
    fl = _check(actor, "coordinator", what="forwarding a request to the user")
    with store.write() as tx:
        a = tx.require(rq, "Ask")
        if a["state"] != "open":
            raise RLError("CONFLICT", f"{rq} is {a['state']}")
        tx.update(rq, {"state": "forwarded", "forwarded_at": now_utc()})
        fl.write(tx, rq)
        tx.event("ask.forwarded", rq, "to the user")
    return _result(rq, f"{rq} waits for the user", fl)


def answer_ask(store: Store, actor: Actor, rq: str, by: str, text: str, choice: str | None = None) -> dict:
    """The coordinator records an answer: its own, or the user's words said in its pane (--by user, 7.3). An
    approval is answered with its choice (--approve or --change, 5.2). The one who asked is told."""
    fl = _check(actor, "coordinator", what="recording an answer")
    if by not in ("coordinator", "user"):
        raise RLError("INVALID", "--by is coordinator or user")
    with store.write() as tx:
        a = tx.require(rq, "Ask")
        if a["state"] in ("answered", "closed"):
            raise RLError("CONFLICT", f"{rq} is already {a['state']}")
        if a["kind"] == "approval" and choice not in ("approve", "change"):
            raise RLError("INVALID", f"{rq} is an approval: record the user's choice with --approve or --change, and "
                                     "their words with --text")
        if a["kind"] in ("approval", "report") and by != "user":
            fl.add("unapproved", f"{rq} is the user's to answer; the coordinator answered it")
        tx.update(rq, {"state": "answered", "answer": text, "answered_by": by, "answered_at": now_utc(),
                       "choice": choice if a["kind"] == "approval" else None})
        fl.write(tx, rq)
        tx.event("ask.answered", rq, f"by {by}" + (f" ({choice})" if choice else "") + f": {text[:200]}")
    nxt = []
    if a["kind"] == "approval" and a.get("task_id"):
        with store.read() as tx:
            t = tx.get(a["task_id"], "Task") if a.get("task_id") else None
        if t and t["state"] == "queued":
            nxt.append(f"rl task release {t['id']}" if choice == "approve" else
                       f"change {t['id']} as the user said, or cancel it (rl task cancel {t['id']} --reason ..)")
    asker = a["by"] if a["by"] not in ("cli", "coordinator") else None
    return _result(rq, f"{rq} answered by {by}" + (f": {choice}" if choice else ""), fl, next=nxt,
                   wake=[asker] if asker else None)


def schedule_lines(tx: Tx) -> list[str]:
    """The schedule (7.1): hypothesis tasks queued or at work, by priority, each with its reason."""
    from .usage_view import task_usage_short
    rows = [t for t in tx.all("Task", kind="hypothesis") if t["state"] not in TASK_DONE]
    rows.sort(key=lambda t: (t["state"] != "queued", t.get("priority") or 99, t["id"]))
    out = []
    for t in rows:
        hyps = ",".join(e["id"] for e in tx.out(t["id"], "targets"))
        out.append(f"{t['id']} {hyps:<8} {t['state']:<9} P{t.get('priority')} {task_usage_short(tx, t)}  "
                   f"{t.get('reason') or ''}")
    return out


def task_line(t: dict) -> str:
    return f"{t['id']} {t['kind']}/{t['state']}" + (f" ({t['holder']})" if t.get("holder") else "") + f" {t['title']}"


def shell_quote(text: str) -> str:
    return shlex.quote(text)
