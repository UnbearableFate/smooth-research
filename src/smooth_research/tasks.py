"""Tasks, phases and decision requests (design chapters 6 and 7, checks of 9.1).

Five kinds: tree (the plan into a hypothesis tree), hypothesis (one hypothesis for one scientist), phase (the phase
conclusion), prep (preparation work a researcher codes; public ones belong to the project) and execute (one piece of
experiment code for one experiment). The coordinator makes tree, hypothesis, phase and public prep tasks (queued
until released); a scientist makes execute and prep tasks under its hypothesis task (PART_OF) and holds them while
it writes code; dispatch hands one to a researcher. Acceptance merges (gitops): the creator accepts.

No transition writes a message: who has to act is computed from the states by the rules table (inbox.py).
"""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone

from . import documents, gitops, manifest
from .project import limit, tests_mode
from .research import design_key, design_keys, sealed_copy, task_design, task_experiment
from .reviews import covers, last_passed_design, open_blockers
from .roles import Actor, need, session_name
from .schema import TASK_DONE
from .store import Store, Tx
from .util import RLError, now_utc, parse_time

NOTE_CHARS = 300
PARALLEL_MAX = 10
WORK = "research/work"


def work_dir(tid: str) -> str:
    return f"{WORK}/{tid}"


def _note(text: str | None) -> str | None:
    if text and len(text) > NOTE_CHARS:
        raise RLError("INVALID", f"a note is at most {NOTE_CHARS} characters ({len(text)} given): put the rest in a "
                                 "record (a finding, evidence, a decision) or a document on the task, and point to it")
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


def latest_tree(tx: Tx) -> dict | None:
    trees = [t for t in tx.all("Task", kind="tree") if t["state"] != "cancelled"]
    return trees[-1] if trees else None


def approval(tx: Tx, tree: dict | None) -> dict | None:
    """The user's answered approval of a tree and its first phase schedule."""
    if tree is None:
        return None
    asks = [a for a in tx.all("Ask", task_id=tree["id"]) if a["kind"] == "approval"]
    return asks[-1] if asks and asks[-1]["state"] == "answered" and asks[-1]["answer"].lower().startswith("approve") \
        else None


# -- making tasks ---------------------------------------------------------------------------------------------


def create(store: Store, actor: Actor, kind: str, title: str, targets: list[str] = (), priority: int | None = None,
           envelope: str | None = None, parallel: int | None = None, reason: str | None = None,
           phase: str | None = None, experiment: str | None = None, requirement: str | None = None,
           done_check: str | None = None, estimate: str | None = None, mandate: str | None = None,
           depends_on: list[str] = ()) -> dict:
    """The coordinator queues tree, hypothesis, phase and public prep tasks; a scientist makes execute and prep tasks
    in its hypothesis task, and holds one at once unless it waits on unaccepted tasks (DEPENDS_ON)."""
    if kind in ("tree", "hypothesis", "phase") or (kind == "prep" and actor and actor.role == "coordinator"):
        need(actor, "coordinator", what=f"making a {kind} task")
    else:
        need(actor, "scientist", what=f"making a {kind} task")
    if kind == "hypothesis":
        if not targets or priority is None or not envelope or not reason:
            raise RLError("INVALID", "a hypothesis task card has five items: --targets H-x, --priority N, --envelope "
                                     "\"about 4 MIG·h\", --parallel N (default 10), --reason \"<why here>\"")
        parallel = PARALLEL_MAX if parallel is None else parallel
        if not 1 <= parallel <= PARALLEL_MAX:
            raise RLError("INVALID", f"--parallel is 1 to {PARALLEL_MAX}")
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
        if actor.role == "scientist":
            parent = tx.require(actor.subject, "Task")
            if parent["kind"] != "hypothesis":
                raise RLError("INVALID", f"execute and prep tasks are made in a hypothesis task; {parent['id']} is "
                                         f"{parent['kind']}")
        if not title:
            named = [tx.require(t, "Hypothesis")["title"] for t in targets] or \
                [tx.require(experiment, "Experiment")["title"]] if (targets or experiment) else []
            if not named:
                raise RLError("INVALID", "--title is needed (a hypothesis task takes its hypothesis's title)")
            title = ("Test " if kind == "hypothesis" else "") + "; ".join(named)
        tid = tx.next_id("Task")
        deps = [tx.require(d, "Task") for d in depends_on]
        held = actor.role == "scientist" and all(d["state"] == "accepted" for d in deps)
        tx.create("Task", {"id": tid, "title": title, "kind": kind, "state": "active" if held else
                           ("open" if actor.role == "scientist" else "queued"), "revision": 1,
                           "creator": actor.name, "priority": priority, "envelope": envelope,
                           "parallel_limit": parallel, "reason": reason, "requirement": requirement,
                           "done_check": done_check, "estimate": estimate})
        if prep:
            tx.update(tid, {"mandate_ref": documents.register(tx, prep, tid)})
        for t in targets:
            rec = tx.require(t, "Hypothesis")
            tx.link("targets", tid, rec["id"])
        if experiment:
            tx.require(experiment, "Experiment")
            tx.link("targets", tid, experiment)
        if phase:
            tx.require(phase, "Phase")
            if kind == "phase":
                tx.link("targets", tid, phase)
            else:
                tx.link("in_phase", tid, phase)
        if parent:
            tx.link("part_of", tid, parent["id"])
        for d in deps:
            tx.link("depends_on", tid, d["id"])
        if held:
            tx.update(tid, _claim_changes(tx, store, tx.require(tid), actor.name))
        tx.event("task.created", tid, f"{kind}: {title}")
        task = tx.require(tid)
    where = f"; worktree {task['worktree']}" if task.get("worktree") else ""
    return {"id": tid, "summary": f"{tid} {kind} {task['state']}: {title}{where}"}


def _claim_changes(tx: Tx, store: Store, task: dict, holder: str) -> dict:
    branch, path = gitops.make_worktree(store.project, task["id"])
    return {"state": "active", **_hold(tx, task, holder), "branch": branch,
            "worktree": str(gitops.checkout_in(store.project, task["id"])), "revision": task["revision"] + 1}


def claimant(task: dict) -> str:
    """Who takes an open task: the scientist session on a hypothesis, tree or phase task, the researcher session on
    a public preparation task, and the scientist that made any other task (it writes the code, then dispatches)."""
    if task["kind"] in ("hypothesis", "tree", "phase"):
        return session_name("scientist", task["id"])
    if task["kind"] == "prep" and task["creator"] == "coordinator":
        return session_name("researcher", task["id"])
    return task["creator"]


def claim(store: Store, tid: str, holder: str) -> dict:
    """Take an open task: its branch and worktree are made from the mainline now (6.5). Every DEPENDS_ON task must
    be accepted (10.2 rule 7). Only the task's own claimant takes it (the watcher opens that session)."""
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if holder != claimant(task):
            raise RLError("INVALID", f"{tid} is taken by {claimant(task)}, not by {holder}")
        if task["state"] != "open" or task.get("holder"):
            raise RLError("CONFLICT", f"{tid} is {task['state']}" + (f", held by {task['holder']}" if task.get("holder")
                                                                     else "") + ": it cannot be claimed now")
        waiting = [d["id"] for d in tx.out(tid, "depends_on") if tx.require(d["id"])["state"] != "accepted"]
        if waiting:
            raise RLError("INVALID", f"{tid} depends on {', '.join(waiting)}, not accepted yet")
        tx.update(tid, _claim_changes(tx, store, task, holder))
        tx.event("task.claimed", tid, holder)
        task = tx.require(tid)
    return {"id": tid, "summary": f"{tid} held by {holder}; worktree {task['worktree']}"}


def release(store: Store, actor: Actor, tid: str, to_scientist: bool = False) -> dict:
    """The coordinator releases a queued task; the watcher starts its session. A hypothesis or public preparation
    task needs the user's approval of the latest tree and schedule (9.1). --to scientist hands a blocked public
    preparation task to a short scientist session (6.4)."""
    need(actor, "coordinator", what="releasing a task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if to_scientist:
            if task["kind"] != "prep" or task["creator"] != "coordinator":
                raise RLError("INVALID", "--to scientist is for a public preparation task")
            if task.get("scientist_session"):
                raise RLError("CONFLICT", f"{tid} is already with {task['scientist_session']}")
            tx.update(tid, {"scientist_session": session_name("scientist", tid), "revision": task["revision"] + 1})
            tx.event("task.to_scientist", tid, "handed to a short scientist session")
            return {"id": tid, "summary": f"{tid} handed to scientist-{tid}"}
        if task["state"] != "queued":
            raise RLError("CONFLICT", f"{tid} is {task['state']}, not queued")
        if task["kind"] in ("hypothesis", "prep"):
            if not approval(tx, latest_tree(tx)):
                raise RLError("INVALID", "the user has not approved the latest tree and its schedule: record their "
                                         "answer first (rl request answer RQ-x --by user --text \"approve ...\")")
        if task["kind"] == "prep" and not (task.get("requirement") and task.get("done_check")):
            raise RLError("INVALID", f"{tid} needs a requirement and a done check before it is released")
        tx.update(tid, {"state": "open", "released_at": now_utc(), "revision": task["revision"] + 1})
        tx.event("task.released", tid, task["title"])
    return {"id": tid, "summary": f"{tid} released; the watcher starts its session"}


def cancel(store: Store, actor: Actor, tid: str, reason: str) -> dict:
    """The coordinator cancels a queued task; a scientist cancels a task it made (answering a blocker with stop).
    A released hypothesis task stops through a stop conclusion instead (7.6)."""
    need(actor, "coordinator", "scientist", what="cancelling a task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] in TASK_DONE:
            raise RLError("CONFLICT", f"{tid} is already {task['state']}")
        if actor.role == "coordinator":
            if task["creator"] != "coordinator" or (task["kind"] == "hypothesis" and task["state"] != "queued"):
                raise RLError("INVALID", f"{tid}: the coordinator cancels its own queued tasks; a released hypothesis "
                                         "task stops through its scientist's stop conclusion")
        elif task["creator"] != actor.name and task.get("scientist_session") != actor.name:
            raise RLError("INVALID", f"{tid} was made by {task['creator']}")
        tx.update(tid, {"state": "cancelled", "cancel_reason": reason, "revision": task["revision"] + 1})
        tx.event("task.cancelled", tid, reason)
        if task["state"] == "merging":  # the acceptance it abandons
            sync_candidates(tx, store)
    return {"id": tid, "summary": f"{tid} cancelled: {reason} (running jobs are not cancelled by this)"}


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
    need(actor, "scientist", "researcher", what="testing a task")
    with store.read() as tx:
        task = tx.require(tid, "Task")
        command = test_command(tx, task)
    if task.get("holder") != actor.name:
        raise RLError("INVALID", f"{tid} is held by {task.get('holder') or 'nobody'}")
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
            raise RLError("INVALID", f"compute.tests is job: run the tests as a short job (rl run register --task "
                                     f"{tid} --request-id <new id> --purpose design-test --minutes N [--resource R]), "
                                     f"then record its outcome: rl task test {tid} --record pass|fail --attempt <ATT>")
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
        tx.event("task.tested", tid, f"{'pass' if ok else 'fail'} at {at[:12]}: {command}")
    return {"id": tid, "passed": ok, "summary": f"{tid} tests {'pass' if ok else 'FAIL'} at {at[:12]} ({command})"
                                                + ("" if ok else f": {detail}") + _manifest_note(store, tid, at)}


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


def test_runs(task: dict, commit: str, command: str | None, design: str | set | None = None) -> list[dict]:
    """The recorded runs of this test command at this commit (for this experiment revision, when given: a key or
    the keys of research.design_keys), in the order they ended."""
    keys = {design} if isinstance(design, str) else design
    runs = [t for t in task.get("tests") or [] if t["commit"] == commit and t.get("command") == command
            and (keys is None or t.get("design") in keys)]
    return sorted(runs, key=_instant)


def _instant(t: dict) -> float:
    """A test result's time as an instant (results come with any UTC offset)."""
    try:
        return parse_time(t.get("at")).timestamp()
    except (RLError, TypeError, ValueError):
        return 0.0


def passed_at(task: dict, commit: str, command: str | None, design: str | set | None = None) -> bool:
    """The latest recorded run of this test command at this commit (for this experiment revision) passed: a later
    failure, a changed command or a new revision needs a new pass."""
    runs = test_runs(task, commit, command, design)
    if not runs or runs[-1]["outcome"] != "pass":
        return False
    # a failure that cannot be put before the latest pass (ended in the same second) needs another pass
    return not any(t["outcome"] != "pass" and _instant(t) >= _instant(runs[-1]) for t in runs)


# -- dispatch -------------------------------------------------------------------------------------------------


def fast_lane(store: Store, tx: Tx, task: dict, exp: dict, commit: str) -> tuple[bool, list[str]]:
    """The three conditions of 6.6, judged by the CLI: the protected scope is unchanged since the last passed
    review of this experiment's tasks; the experiment is sealed and its manifest unchanged; the estimate is below
    the project's fast-lane limit for its unit."""
    why = []
    blockers = open_blockers(tx, task["id"], for_evidence=False)
    if blockers:
        why.append(blockers)
    others = [e["id"] for e in tx.inc(exp["id"], "targets") if e["id"] != task["id"]]
    last = last_passed_design(tx, others)
    if not exp.get("design_commit"):
        why.append(f"{exp['id']} is not sealed yet")
    if last is None:
        why.append(f"no passed review on an earlier task of {exp['id']}")
    else:
        changed = gitops.protected_changes(store.project, last[1], commit, list(exp.get("protected") or []))
        if changed:
            why.append(f"protected scope changed since {last[0]['id']}: {', '.join(changed)}")
    if exp.get("manifest_path") and exp.get("manifest_digest"):
        text = gitops.show(store.project, commit, exp["manifest_path"])
        if text is None or manifest.parse(text, exp["manifest_path"]) != exp.get("manifest"):
            why.append("the manifest differs from the sealed one")
    m = re.match(r"^\s*(?:about\s+)?([0-9.]+)\s*(\S.*)$", task.get("estimate") or "")
    caps = limit(store.project.config, "fast_lane")
    if not m:
        why.append("the task has no estimate (--estimate \"0.2 MIG·h\")")
    elif m[2].strip() not in caps or float(m[1]) > caps[m[2].strip()]:
        why.append(f"estimate {task['estimate']} is not under limits.fast_lane ({caps or 'none set'})")
    return not why, why


def dispatch(store: Store, actor: Actor, tid: str, note: str | None = None) -> dict:
    """Hand a task to a researcher (9.1): an execute task needs passing tests at its committed head, a review that
    covers that commit with no open method finding (or the fast lane), and a manifest that parses; its design commit
    is sealed now (4.6). A preparation task needs its requirement and done check. The watcher starts the
    researcher in the task's worktree."""
    need(actor, "scientist", what="dispatching a task")
    _note(note)
    with store.read() as tx:
        task = tx.require(tid, "Task")
    if task.get("holder") != actor.name or task["state"] != "active":
        raise RLError("INVALID", f"{tid} is {task['state']}, held by {task.get('holder') or 'nobody'}: you dispatch "
                                 "a task you hold")
    if task["kind"] not in ("execute", "prep"):
        raise RLError("INVALID", f"{task['kind']} tasks are not dispatched")
    commit = None
    if task["kind"] == "execute":
        if gitops.dirty(store.project, tid):
            raise RLError("INVALID", f"{tid}'s worktree has uncommitted changes: commit the design first")
        commit = gitops.head(store.project, tid)
    with store.write() as tx:
        task = tx.require(tid, "Task")
        changes = {"state": "active", **_hold(tx, task, session_name("researcher", tid)),
                   "dispatch_note": note, "revision": task["revision"] + 1}
        if task["kind"] == "execute":
            exp = task_experiment(tx, tid)
            missing = [k for k in ("manifest_path", "semantic_tests") if not exp.get(k)]
            if missing:
                raise RLError("INVALID", f"{exp['id']} needs {', '.join(missing)} before its code is handed over "
                                         "(rl experiment set ..)")
            if not passed_at(task, commit, exp["semantic_tests"], design_keys(exp)):
                raise RLError("INVALID", f"the latest test of `{exp['semantic_tests']}` at {commit[:12]} did not pass "
                                         f"(or none is recorded): rl task test {tid}")
            ok, why = covers(tx, tid, commit, for_evidence=False)
            lane = False
            if not ok:
                lane, lane_why = fast_lane(store, tx, task, exp, commit)
                if not lane:
                    raise RLError("INVALID", f"{why}; the fast lane does not apply either: {'; '.join(lane_why)}")
            text = gitops.show(store.project, commit, exp["manifest_path"])
            if text is None:
                raise RLError("INVALID", f"{exp['manifest_path']} is not in {commit[:12]}")
            parsed = manifest.parse(text, exp["manifest_path"])
            from .util import digest_json
            digest = digest_json(parsed)
            if exp.get("design_commit"):
                if exp.get("manifest_digest") != digest:
                    raise RLError("INVALID", f"{exp['id']}@{exp['revision']} was sealed with another manifest: a "
                                             "changed manifest is a new revision (rl experiment set "
                                             f"{exp['id']} --reason ..)")
                changed = gitops.protected_changes(store.project, exp["design_commit"], commit,
                                                   list(exp.get("protected") or []))
                if changed and not ok:
                    raise RLError("INVALID", f"the protected scope changed since {exp['id']} was sealed "
                                             f"({', '.join(changed)}): a new revision and a review")
            else:
                tx.update(exp["id"], {"design_commit": commit, "sealed_at": now_utc(), "sealed_by": tid,
                                      "manifest": parsed, "manifest_digest": digest})
                tx.event("experiment.sealed", exp["id"], f"@{exp['revision']} at {commit[:12]} by dispatch of {tid}")
            exp = tx.require(exp["id"])
            changes.update(design_commit=commit, fast_lane=lane or None, sealed_design=sealed_copy(exp))
        tx.update(tid, changes)
        tx.event("task.dispatched", tid, note or "")
    return {"id": tid, "summary": f"{tid} dispatched to researcher-{tid}" + (f"; design {commit[:12]} sealed" if commit
                                                                          else "")
            + (_manifest_note(store, tid, commit) if commit else "")}


# -- blockers -------------------------------------------------------------------------------------------------


def block(store: Store, actor: Actor, tid: str, reason: str, note: str) -> dict:
    """A researcher stops on a design or mandate problem, or only asks what something means (8.2)."""
    need(actor, "researcher", what="blocking a task")
    _note(note)
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task.get("holder") != actor.name or task["state"] != "active":
            raise RLError("INVALID", f"{tid} is {task['state']}, held by {task.get('holder')}")
        tx.update(tid, {"state": "blocked", "block_reason": reason, "block_note": note, "answer_note": None,
                        "revision": task["revision"] + 1})
        tx.event("task.blocked", tid, f"{reason}: {note}")
    return {"id": tid, "summary": f"{tid} blocked ({reason})"}


def answer(store: Store, actor: Actor, tid: str, text: str) -> dict:
    """The scientist (or the short scientist session of a public prep task) answers a blocker; the task goes back to
    its researcher. A changed design is a new experiment revision written before this answer."""
    need(actor, "scientist", what="answering a blocker")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "blocked":
            raise RLError("CONFLICT", f"{tid} is {task['state']}, not blocked")
        if actor.name not in (task["creator"], task.get("scientist_session")) and \
                (hypothesis_task_of(tx, task) or {}).get("holder") != actor.name:
            raise RLError("INVALID", f"{tid}'s blockers go to {task['creator']}")
        live = task_experiment(tx, tid) if task["kind"] == "execute" else None
        sealed = task.get("sealed_design") or {}
        if live and sealed and live["revision"] != sealed.get("revision"):
            # a changed design is handed over like the first one: its tests, its review, a dispatch
            tx.update(tid, {"state": "active", "answer_note": text, "revision": task["revision"] + 1,
                            "sealed_design": None, **_hold(tx, task, actor.name)})
            tx.event("task.answered", tid, f"design changed to {live['id']}@{live['revision']}: {text[:250]}")
            return {"id": tid, "summary": f"{tid} is back with you: {live['id']} is now at revision "
                                          f"{live['revision']}, so commit the design, rl task test {tid}, get its "
                                          f"review and rl task dispatch {tid} again; runs already registered stay "
                                          f"on revision {sealed.get('revision')}"}
        tx.update(tid, {"state": "active", "answer_note": text, "revision": task["revision"] + 1})
        tx.event("task.answered", tid, text[:300])
    return {"id": tid, "summary": f"{tid} back with its researcher"}


def set_prep(store: Store, actor: Actor, tid: str, requirement: str | None, done_check: str | None) -> dict:
    """The short scientist session of a public prep task changes its requirement or done check."""
    need(actor, "scientist", what="changing a preparation task")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["kind"] != "prep" or actor.name not in (task["creator"], task.get("scientist_session")):
            raise RLError("INVALID", f"{tid} is not a preparation task of yours")
        tx.update(tid, {k: v for k, v in (("requirement", requirement), ("done_check", done_check)) if v})
        tx.event("task.prep_changed", tid, f"requirement/done check changed by {actor.name}")
    return {"id": tid, "summary": f"{tid} changed"}


# -- delivery -------------------------------------------------------------------------------------------------


def deliver(store: Store, actor: Actor, tid: str, conclusion: str | None = None, limitations: str | None = None,
            outputs: list[str] = (), reusable: str | None = None, note: str | None = None) -> dict:
    _note(note)
    with store.read() as tx:
        task = tx.require(tid, "Task")
    if task.get("holder") != actor.name or task["state"] != "active":
        raise RLError("INVALID", f"{tid} is {task['state']}, held by {task.get('holder') or 'nobody'}: its holder "
                                 "delivers it")
    if task["kind"] in ("execute", "prep"):
        need(actor, "researcher", what=f"delivering a {task['kind']} task")
        return _deliver_work(store, actor, tid, conclusion, limitations, list(outputs), reusable, note)
    need(actor, "scientist", what=f"delivering a {task['kind']} task")
    return _deliver_scientist(store, actor, tid, conclusion, note)


def _deliver_work(store: Store, actor: Actor, tid: str, conclusion, limitations, outputs, reusable, note) -> dict:
    if not conclusion or not limitations:
        raise RLError("INVALID", "a task conclusion has one sentence (--conclusion, its direction only, citing "
                                 "evidence; no copied numbers) and its limits (--limitations)")
    if gitops.dirty(store.project, tid):
        raise RLError("INVALID", f"{tid}'s worktree has uncommitted changes: commit them; outputs stay on the branch")
    head = gitops.head(store.project, tid)
    bad = gitops.tracked_and_committed(store.project, tid, outputs)
    if bad:
        raise RLError("INVALID", f"outputs not committed on {gitops.branch_name(tid)}: {', '.join(bad)}")
    with store.read() as tx:
        task = tx.require(tid, "Task")
        if task["kind"] == "execute" and not tx.all("Evidence", task_id=tid):
            raise RLError("INVALID", f"{tid}'s conclusion cites evidence: write it first (rl evidence add --task {tid})")
        command = test_command(tx, task)
    public = task["creator"] == "coordinator"
    check = None
    if task["kind"] == "prep":
        if tests_mode(store.project.config) == "local":
            ok, detail = run_check(store, tid, command)
            check = {"commit": head, "command": command, "outcome": "pass" if ok else "fail", "at": now_utc(),
                     "by": "cli", "detail": detail}
            if not ok:
                raise RLError("INVALID", f"the done check fails at {head[:12]}: {detail}")
        elif not passed_at(task, head, command):
            raise RLError("INVALID", f"no passing done check is recorded at {head[:12]}: run it as a job, then rl task "
                                     f"test {tid} --record pass --attempt <ATT>")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        changes = {"state": "delivered", "conclusion": conclusion, "limitations": limitations,
                   "delivered_commit": head, "delivered_at": now_utc(), "state_note": note, "return_note": None,
                   "outputs": [{"path": p, **({"reusable": reusable} if reusable else {})} for p in outputs] or None,
                   "revision": task["revision"] + 1, "merge_state": None, "merge_note": None}
        if check:
            changes["tests"] = list(task.get("tests") or []) + [check]
        tx.update(tid, changes)
        tx.event("task.delivered", tid, conclusion)
    out = {"id": tid, "summary": f"{tid} delivered at {head[:12]}"}
    if public:  # the CLI accepts a public preparation task on its done check (6.4)
        merged = _merge(store, tid, run_tests=False)
        out["summary"] += "; " + merged
    return out


def _deliver_scientist(store: Store, actor: Actor, tid: str, conclusion: str | None, note: str | None) -> dict:
    from .reviews import tree_passed
    with store.read() as tx:
        task = tx.require(tid, "Task")
        base = gitops.fork_point(store.project, tid)
    head = gitops.head(store.project, tid)
    if gitops.dirty(store.project, tid):
        raise RLError("INVALID", f"{tid}'s worktree has uncommitted changes: commit the documents in {work_dir(tid)}/")
    outside = gitops.only_under(store.project, base, head, work_dir(tid)) if base else []
    if outside:
        raise RLError("INVALID", f"a {task['kind']} task changes only {work_dir(tid)}/; it also changed "
                                 f"{', '.join(outside[:5])}")
    with store.write() as tx:
        task = tx.require(tid, "Task")
        problems: list[str] = []
        if task["kind"] == "hypothesis":
            if not conclusion:
                problems.append("--conclusion: one sentence, its direction only")
            targets = [e["id"] for e in tx.out(tid, "targets") if e["label"] == "Hypothesis"]
            finals = [d for d in tx.all("Decision", task_id=tid) if d["state"] == "final"
                      and d["kind"] in ("verdict", "stop")]
            decided = {e["id"] for d in finals for e in tx.out(d["id"], "decides")}
            missing = [h for h in targets if h not in decided]
            if missing:
                problems.append(f"no final verdict or stop on {', '.join(missing)} (rl decide .., a conclusion review, "
                                "rl decide --final D-x)")
            if any(d["kind"] == "verdict" and not (d.get("did") and d.get("means")) for d in finals):
                problems.append("a verdict lacks its interpretation (--did, --means)")
            unfinished = [e["id"] for e in tx.inc(tid, "part_of") if tx.require(e["id"])["state"] not in TASK_DONE]
            if unfinished:
                problems.append(f"tasks still open under it: {', '.join(unfinished)} (accept or cancel them)")
        elif task["kind"] == "tree":
            hyps = [h for h in tx.all("Hypothesis") if h.get("origin") == tid]
            if not hyps:
                problems.append("no hypotheses were added on this tree task")
            for h in hyps:
                lacks = [k for k in ("statement", "scope", "source", "purpose") if not h.get(k)]
                if lacks:
                    problems.append(f"{h['label']} lacks {', '.join(lacks)}")
                f = h.get("flag")
                if f and not (f.get("basis") and f.get("options")):
                    problems.append(f"{h['label']} is flagged without basis and options")
            problems += _plan_coverage(store, tx, tid, hyps)
            if not tree_passed(tx, tid):
                problems.append("the tree review has not passed (rl review request T-x --kind tree)")
            if not any(d["kind"] == "route" for d in tx.all("Decision", task_id=tid)):
                problems.append("no route decision on why the tree is split this way (rl decide --kind route ..)")
            prep_list = gitops.show(store.project, head, f"{work_dir(tid)}/prep.yaml")
            if prep_list is not None:
                problems += _prep_list_problems(prep_list)
        elif task["kind"] == "phase":
            phase = [e["id"] for e in tx.out(tid, "targets") if e["label"] == "Phase"][0]
            ds = [d for d in tx.all("Decision", task_id=tid) if d["kind"] == "phase"
                  and any(e["id"] == phase for e in tx.out(d["id"], "decides"))]
            if not ds:
                problems.append(f"no phase decision decides {phase} (rl decide --kind phase --targets {phase} ..)")
        if problems:
            raise RLError("INVALID", f"{tid} cannot be delivered yet:\n- " + "\n- ".join(problems))
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
        tx.event("task.delivered", tid, conclusion or task["kind"])
    return {"id": tid, "summary": f"{tid} delivered; the coordinator receives it"}


def _plan_coverage(store: Store, tx: Tx, tid: str, hyps: list[dict]) -> list[str]:
    """Every heading of the plan has a hypothesis whose source names it, or is listed in left-out.md (4.8)."""
    plan = store.project.plan_path
    if not plan.is_file():
        return []
    heads = [m[2].strip() for m in re.finditer(r"^(#{1,3})\s+(.+)$", plan.read_text(encoding="utf-8"), re.M)][1:]
    left = gitops.checkout_in(store.project, tid) / work_dir(tid) / "left-out.md"
    left_text = left.read_text(encoding="utf-8").lower() if left.is_file() else ""
    sources = " ".join((h.get("source") or "").lower() for h in hyps)
    missing = [h for h in heads if h.lower() not in sources and h.lower() not in left_text]
    return [f"plan sections with no hypothesis and not in {work_dir(tid)}/left-out.md: {', '.join(missing[:8])}"] \
        if missing else []


def _prep_list_problems(text: str) -> list[str]:
    import yaml
    try:
        data = yaml.safe_load(text) or []
    except yaml.YAMLError as exc:
        return [f"prep.yaml does not parse: {exc}"]
    bad = [str(i) for i, p in enumerate(data if isinstance(data, list) else [None])
           if not isinstance(p, dict) or not (p.get("title") and p.get("requirement") and p.get("done_check"))]
    return [f"prep.yaml entries need title, requirement and done_check (entries {', '.join(bad)})"] if bad else []


# -- acceptance -----------------------------------------------------------------------------------------------


def accept(store: Store, actor: Actor, tid: str) -> dict:
    """The creator accepts a delivery, which merges it (6.5). A scientist accepts the execute and prep tasks it made;
    the coordinator receives tree, hypothesis and phase tasks."""
    with store.read() as tx:
        task = tx.require(tid, "Task")
    if task["state"] != "delivered":
        raise RLError("CONFLICT", f"{tid} is {task['state']}, not delivered")
    if task["kind"] in ("tree", "hypothesis", "phase"):
        need(actor, "coordinator", what=f"receiving a {task['kind']} task")
        return {"id": tid, "summary": f"{tid}: " + _merge(store, tid, run_tests=False, seen=task["revision"])}
    need(actor, "scientist", what="accepting a task")
    if task["creator"] != actor.name and task.get("scientist_session") != actor.name:
        raise RLError("INVALID", f"{tid} was made by {task['creator']}, who accepts it")
    return {"id": tid, "summary": f"{tid}: " + _merge(store, tid, run_tests=True, seen=task["revision"])}


def _merge(store: Store, tid: str, run_tests: bool, seen: int | None = None) -> str:
    """Accept a delivery (6.5): a merge commit of the delivered commit and the mainline, the task's tests on it in a
    temporary checkout, then the mainline moves to it. What merges is the delivered commit, whatever the branch
    holds now. The acceptance is recorded first (its candidate and round), so a finish, a failure, a test job and
    a restart all act on this acceptance only. A conflict or a failing test leaves the mainline where it was.
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
    public = task["kind"] == "prep" and task["creator"] == "coordinator"
    recovered = _recover(store, task, public)
    if recovered:
        return recovered
    head = gitops.head(store.project, tid)
    if head != delivered:
        raise RLError("CONFLICT", f"{tid}'s branch is at {head[:12]}, not at its delivery {delivered[:12]}: what is "
                                  "accepted is what was delivered, so deliver it again (or reset the extra commits)")
    commit, conflicts, contained = gitops.merge_candidate(store.project, tid, delivered)
    command = None if contained else command  # a delivery the mainline already holds: nothing new to test (6.5)
    job = bool(command) and tests_mode(store.project.config) == "job"
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
                        **({"state": "merging"} if job else {})})
        tx.event("task.merging", tid, f"candidate {commit[:12]} of delivery {delivered[:12]}"
                                      + (": its tests run as a job" if job else ""))
        if job:
            sync_candidates(tx, store)
    if job:
        return (f"merge commit {commit[:12]} made (branch {gitops.candidate_branch(store.project, tid)}); run its "
                f"tests as a short "
                f"job: rl run register --task {tid} --request-id <new id> --purpose merge-test --minutes N "
                f"[--resource R]; the mainline moves when it passes")
    if command:
        with gitops.temp_checkout(store.project, commit) as where:
            ok, detail = run_check(store, tid, command, cwd=where)
        if not ok:
            return _conflict(store, tid, f"tests fail on the merge commit {commit[:12]}: {detail}", public,
                             delivered, rnd)
    return _advance(store, tid, commit, delivered, rnd, public, "merged")


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
             needs_job: bool = False) -> str:
    """Move the mainline to the tested merge commit and accept the task, under the write lock and only for the
    acceptance that tested it: a task returned, cancelled or delivered again meanwhile keeps the mainline where it
    is. A mainline that already holds the commit (a move whose record was interrupted) is accepted as it is.
    needs_job: the round's test jobs decide, as the records say under the write lock (a failure, also one recorded
    after a success, rejects it; an attempt corrected to running meanwhile is waited on)."""
    if needs_job:  # a round that still waits changes nothing, so it writes nothing (a beat would redraw the page)
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
            tx.update(tid, _conflict_changes(task, why, public))
            tx.event("task.merge_conflict", tid, why)
            sync_candidates(tx, store)
            return _conflict_text(why, public)
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
                        "revision": task["revision"] + 1})
        tx.event("task.accepted", tid, f"{how} at {commit[:12]}")
        sync_candidates(tx, store)
    gitops.follow(store.project, tid, delivered, commit)
    return f"accepted and merged ({commit[:12]}); the mainline moved"


def _failed(jobs: list[dict]) -> list[dict]:
    return [a for a in jobs if a["execution_state"] == "ended" and a["outcome"] != "success"]


def _conflict_changes(task: dict, why: str, public: bool) -> dict:
    # a public preparation task goes back to the researcher who delivered it (6.4)
    return {"merge_state": "conflict", "merge_note": why, "revision": task["revision"] + 1,
            "state": "active" if public else "delivered"}


def _conflict_text(why: str, public: bool) -> str:
    return (f"NOT merged, the mainline did not move: {why}. "
            + ("It is back with its researcher, who merges the mainline into the branch and delivers again"
               if public else "Open a small task for a researcher to resolve it, or fix it and accept again"))


def _conflict(store: Store, tid: str, why: str, public: bool, delivered: str, rnd: int) -> str:
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if not _same(task, delivered, rnd):
            return f"{why}; {tid} changed meanwhile (now {task['state']}), so nothing was recorded"
        tx.update(tid, _conflict_changes(task, why, public))
        tx.event("task.merge_conflict", tid, why)
        sync_candidates(tx, store)
    return _conflict_text(why, public)


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


def reconcile_merges(store: Store) -> list[str]:
    """Acceptances waiting on a test job whose end is already recorded (a crash between the two, or an end that came
    through rl run record): finish them now; and acceptances interrupted after their candidate reached the mainline
    (a public preparation task's too, which nobody accepts by hand). The watcher runs this every beat."""
    repair_candidates(store)
    with store.read() as tx:
        merging = [t for t in tx.all("Task") if t["state"] == "merging"]
        ended = [a["id"] for t in merging for r in tx.all("Run", task_id=t["id"])
                 if r.get("purpose") == "merge-test" and r.get("code_commit") == t.get("merge_commit")
                 for a in tx.all("Attempt", run_id=r["id"]) if a["execution_state"] == "ended"]
        stuck = [t for t in tx.all("Task") if t["state"] in ("delivered", "merging") and (
            t.get("merge_state") == "merging" or (_public(t) and t["state"] == "delivered"
                                                  and t.get("merge_state") != "conflict"))]
    with store.read() as tx:  # a failure decides a round before any success of it
        ended.sort(key=lambda a: tx.require(a)["outcome"] == "success")
    done = [x for x in (finish_merge(store, a) for a in ended) if x]
    for t in stuck:
        try:
            got = _recover(store, t, t["kind"] == "prep" and t["creator"] == "coordinator")
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
    """Send a delivery back: a scientist to the researcher, the coordinator to the scientist (8.2)."""
    with store.write() as tx:
        task = tx.require(tid, "Task")
        if task["state"] != "delivered":
            raise RLError("CONFLICT", f"{tid} is {task['state']}, not delivered")
        if task["kind"] in ("tree", "hypothesis", "phase"):
            need(actor, "coordinator", what="returning a delivery")
        else:
            need(actor, "scientist", what="returning a delivery")
            if actor.name not in (task["creator"], task.get("scientist_session")):
                raise RLError("INVALID", f"{tid} was made by {task['creator']}")
        tx.update(tid, {"state": "active", "return_note": reason, "merge_state": None,
                        "revision": task["revision"] + 1})
        tx.event("task.returned", tid, reason)
    return {"id": tid, "summary": f"{tid} returned to {task['holder']}: {reason}"}


# -- phases ---------------------------------------------------------------------------------------------------


def phase_create(store: Store, actor: Actor, title: str, scientists: int | None, budget: str | None,
                 tasks: list[str] = ()) -> dict:
    """A phase: the hypothesis tasks it holds (the first phase also the public preparation tasks), the scientists
    at work at once, and a budget that is only shown (7.5)."""
    need(actor, "coordinator", what="making a phase")
    with store.write() as tx:
        ph = tx.next_id("Phase")
        tx.create("Phase", {"id": ph, "title": title, "budget": budget,
                            "scientist_limit": scientists or int(limit(store.project.config, "scientists"))})
        for t in tasks:
            _add_to_phase(tx, ph, t)
        tx.event("phase.created", ph, title)
    return {"id": ph, "summary": f"{ph} {title}"}


def phase_add(store: Store, actor: Actor, ph: str, tasks: list[str]) -> dict:
    need(actor, "coordinator", what="changing a phase")
    with store.write() as tx:
        tx.require(ph, "Phase")
        for t in tasks:
            _add_to_phase(tx, ph, t)
        tx.event("phase.changed", ph, f"added {', '.join(tasks)}")
    return {"id": ph, "summary": f"{ph}: added {', '.join(tasks)}"}


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
    """A decision request (7.3): a scientist on its hypothesis or tree task to the coordinator; the coordinator's
    request to the user for the approval of a tree and its schedule (kind approval, on the tree task)."""
    if kind == "approval":
        need(actor, "coordinator", what="asking the user to approve the tree")
        if not task_id:
            raise RLError("INVALID", "an approval is asked on the tree task (--task T-x)")
    else:
        need(actor, "scientist", "coordinator", what="a decision request")
        task_id = task_id or actor.subject
    with store.write() as tx:
        task = tx.require(task_id, "Task")
        if kind == "approval":
            if task["kind"] != "tree" or task["state"] != "accepted":
                raise RLError("INVALID", f"approval is asked on a received tree task; {task_id} is "
                                         f"{task['kind']}/{task['state']}")
        elif actor.role == "scientist" and task["kind"] not in ("hypothesis", "tree"):
            raise RLError("INVALID", f"a decision request is made on a hypothesis or tree task, not {task['kind']}")
        rq = tx.next_id("Ask")
        tx.create("Ask", {"id": rq, "kind": kind, "task_id": task_id, "by": actor.name,
                          "state": "forwarded" if kind == "approval" or actor.role == "coordinator" else "open",
                          "stuck": stuck, "options": options, "recommend": recommend, "cost": cost,
                          "forwarded_at": now_utc() if kind == "approval" or actor.role == "coordinator" else None})
        tx.event("ask.created", rq, f"{kind} on {task_id}: {stuck[:200]}")
    to = "the user" if kind == "approval" or actor.role == "coordinator" else "the coordinator"
    return {"id": rq, "summary": f"{rq} waits for {to}"}


def forward(store: Store, actor: Actor, rq: str) -> dict:
    need(actor, "coordinator", what="forwarding a request to the user")
    with store.write() as tx:
        a = tx.require(rq, "Ask")
        if a["state"] != "open":
            raise RLError("CONFLICT", f"{rq} is {a['state']}")
        tx.update(rq, {"state": "forwarded", "forwarded_at": now_utc()})
        tx.event("ask.forwarded", rq, "to the user")
    return {"id": rq, "summary": f"{rq} waits for the user"}


def answer_ask(store: Store, actor: Actor, rq: str, by: str, text: str) -> dict:
    """The coordinator records an answer: its own, or the user's words said in its pane (--by user, 7.3). The user
    runs no write command."""
    need(actor, "coordinator", what="recording an answer")
    if by not in ("coordinator", "user"):
        raise RLError("INVALID", "--by is coordinator or user")
    with store.write() as tx:
        a = tx.require(rq, "Ask")
        if a["state"] in ("answered", "closed"):
            raise RLError("CONFLICT", f"{rq} is already {a['state']}")
        if a["kind"] in ("approval", "report") and by != "user":
            raise RLError("INVALID", f"{rq} is the user's to answer: record their words with --by user")
        tx.update(rq, {"state": "answered", "answer": text, "answered_by": by, "answered_at": now_utc()})
        tx.event("ask.answered", rq, f"by {by}: {text[:200]}")
    return {"id": rq, "summary": f"{rq} answered by {by}"}


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
