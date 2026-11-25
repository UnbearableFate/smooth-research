"""Run records: registrations, attempts, external references, observations, results and usage (design 5.5, 9.1).

Nothing here starts, polls, retries, cancels, downloads or validates external work. Executors run work with their
own tools and hand the facts to register / record / import-result / ended.

A run is one of three categories. A trial (tests, smoke runs, timing) needs no review, may last at most
limits.trial_minutes, and is never evidence. A preparation run belongs to a preparation task and is never evidence.
An evidence run is an arm of its experiment's sealed manifest: the review must cover the code commit (no open method
finding, nothing medium or higher open) or the fast lane apply, and its configuration must match the manifest; a
run that does not match may still be registered as deviating (--deviation), and is not evidence.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import compute, gitops, manifest
from .project import Project, coordinator_is_login, limit
from .research import brief, recheck_dependents, task_design, task_experiment
from .reviews import covers, open_blockers
from .roles import Actor, need
from .schema import ENUMS
from .store import Store, Tx
from .util import RLError, digest_file, digest_json, now_utc, parse_time, short

FULL_COMMIT = compute.FULL_COMMIT
OBS_FIELDS = {"record_id", "attempt_id", "source", "observed_at", "occurred_at", "source_sequence", "execution_state",
              "outcome", "raw_state", "source_ref", "execution_ref", "usage", "correction_of", "note"}
STALE_AFTER_H = 6.0
PURPOSES = ("merge-test", "review-test", "design-test")


# -- register -------------------------------------------------------------------------------------------------


def register(store: Store, actor: Actor, task_id: str, request_id: str, arm: str | None = None, seed=None,
             config: dict | None = None, split: str | None = None, commit: str | None = None,
             minutes: float | None = None, trial: bool = False, deviation: str | None = None,
             purpose: str | None = None, ticket: str | None = None, run_id: str | None = None,
             resource: str | None = None, params: dict | None = None) -> dict:
    need(actor, "researcher", "scientist", what="registering a run")
    # the same request again returns what it made, whatever changed since (the task accepted, a finding opened)
    payload = digest_json({"task": task_id, "arm": arm, "seed": seed, "config": config, "split": split,
                           "commit": commit, "minutes": minutes, "trial": trial, "deviation": deviation,
                           "purpose": purpose, "ticket": ticket, "run": run_id, "resource": resource,
                           "params": params})
    with store.read() as tx:
        previous = tx.request("run.register", request_id, payload)
    if previous:
        return {**previous, "duplicate": True,
                "summary": f"duplicate request {request_id}: {previous['run']} {previous['attempt']}"}
    if coordinator_is_login(store.project.config) and not resource:
        raise RLError("INVALID", "the project runs work through an executor (compute.coordinator: login): register "
                                 "with --resource <cluster resource>")
    if purpose and purpose not in PURPOSES:
        raise RLError("INVALID", f"--purpose is one of {', '.join(PURPOSES)}")
    if purpose:
        trial = True
    res = compute.resource(store.project, resource) if resource else None
    with store.read() as tx:
        task = tx.require(task_id, "Task")
        exp = task_design(tx, task_id)
        live = task_experiment(tx, task_id)
    holder_ok = task.get("holder") == actor.name or (purpose == "merge-test" and actor.name == task["creator"])
    if not holder_ok:
        raise RLError("INVALID", f"{task_id} is held by {task.get('holder') or 'nobody'}: its holder registers runs")
    if purpose == "merge-test":
        if task["state"] != "merging" or not task.get("merge_commit"):
            raise RLError("INVALID", f"{task_id} is {task['state']}: a merge test runs while its acceptance waits on it")
        if commit and gitops.rev(store.project, commit) != task["merge_commit"]:
            raise RLError("INVALID", f"a merge test of {task_id} tests its merge commit {task['merge_commit'][:12]}, "
                                     f"not {commit[:12]}")
        commit = task["merge_commit"]
    if commit is None:
        if gitops.dirty(store.project, task_id) and not trial:
            raise RLError("INVALID", f"{task_id}'s worktree has uncommitted changes: runs use committed code")
        commit = task.get("merge_commit") if purpose == "merge-test" else gitops.head(store.project, task_id)
    if not FULL_COMMIT.match(commit or ""):
        resolved = gitops.rev(store.project, commit) if commit else None
        if not resolved:
            raise RLError("INVALID", f"--commit {commit!r} is not a commit of the repository")
        commit = resolved
    category = "trial" if trial else "prep" if task["kind"] == "prep" else "evidence" if task["kind"] == "execute" \
        else None
    if run_id:  # a retry is an attempt of the same run: its category is the run's, whatever flags it gives
        with store.read() as tx:
            category = tx.require(run_id, "Run")["category"]
    if category is None:
        raise RLError("INVALID", f"runs belong to execute or prep tasks (or are trials); {task_id} is {task['kind']}")
    cap = float(limit(store.project.config, "trial_minutes"))
    if category == "trial" and (minutes is None or minutes > cap):
        raise RLError("INVALID", f"a trial run gives --minutes, at most limits.trial_minutes ({cap:g}); a longer run "
                                 "is an evidence or preparation run")
    deviates = None
    design = None
    if category == "evidence":
        if not exp or not exp.get("design_commit") or not task.get("design_commit"):
            raise RLError("INVALID", f"{task_id}'s experiment is not sealed: it is sealed when the task is dispatched")
        if live and live["revision"] != exp["revision"]:
            raise RLError("INVALID", f"{exp['id']} is at revision {live['revision']} now, and {task_id} was "
                                     f"dispatched with revision {exp['revision']}: evidence waits until the new "
                                     "revision is handed over (block the task for its scientist)")
        if arm is None:
            raise RLError("INVALID", "an evidence run names its arm (--arm) from the manifest")
        config, split = manifest.effective(exp["manifest"], arm, config, split)
        # what this run is checked against later, whatever the experiment becomes: its sealed revision and arm
        design = {"experiment": exp["id"], "revision": exp["revision"], "manifest_digest": exp["manifest_digest"],
                  "design_commit": exp["design_commit"], "arm": manifest.arm(exp["manifest"], arm),
                  "allowed": list(exp["manifest"].get("allowed") or [])}
    # a merge test is bound to the acceptance it tests: that delivery, that round (tasks.finish_merge); a test
    # job to the command it runs (tasks.test --record)
    acceptance = {"delivered": task["delivered_commit"], "round": task["revision"]} if purpose == "merge-test" \
        else None
    tested = None
    if purpose in ("merge-test", "design-test", "review-test"):
        from .tasks import test_target
        with store.read() as tx:
            command, key = test_target(tx, tx.require(task_id, "Task"))
            tested = {"command": command, "design": key}
    spec = {"arm": arm, "seed": seed, "config": config, "split": split, "commit": commit, "params": params,
            **({"design": design} if design else {}), **({"acceptance": acceptance} if acceptance else {}),
            **({"test": tested} if tested else {})}
    changed: list[str] = []
    if category == "evidence":  # every new attempt, a retry of a run too: a finding opened since blocks it
        problems = manifest.check_run(exp["manifest"], arm, seed, config, split)
        if task.get("fast_lane"):
            changed = gitops.protected_changes(store.project, task["design_commit"], commit,
                                               list(exp.get("protected") or []))
        with store.read() as tx:
            _evidence_allowed(tx, task, commit, changed)
        if problems:
            if not deviation:
                raise RLError("INVALID", "the run does not match the sealed manifest: " + "; ".join(problems)
                              + ". Fix the run's configuration, or register it as deviating (--deviation '<why>'), "
                                "which is not evidence")
            deviates = True
    with store.write() as tx:
        previous = tx.request("run.register", request_id, payload)
        if previous:
            return {**previous, "duplicate": True,
                    "summary": f"duplicate request {request_id}: {previous['run']} {previous['attempt']}"}
        if purpose == "merge-test":  # again under the write lock: the acceptance it tests still waits
            now = tx.require(task_id, "Task")
            if now["state"] != "merging" or now.get("merge_commit") != commit or now["revision"] != acceptance["round"]:
                raise RLError("CONFLICT", f"{task_id}'s acceptance changed meanwhile (now {now['state']}): nothing was "
                                          "registered")
        if category == "evidence":  # again under the write lock: a finding or a revision recorded meanwhile
            now = tx.require(task_id, "Task")
            sealed, live_now = task_design(tx, task_id), task_experiment(tx, task_id)
            if now.get("holder") != task.get("holder") or (sealed or {}).get("revision") != exp["revision"] \
                    or (live_now or {}).get("revision") != exp["revision"]:
                raise RLError("CONFLICT", f"{task_id} changed meanwhile (its holder or its experiment's revision): "
                                          "nothing was registered; look at it again")
            _evidence_allowed(tx, now, commit, changed)
        if run_id:
            run = tx.require(run_id, "Run")
            if run["task_id"] != task_id:
                raise RLError("INVALID", f"{run_id} belongs to {run['task_id']}, not {task_id}")
            if _comparable(run["spec"], spec) != _comparable(spec, run["spec"]):
                raise RLError("CONFLICT", f"the spec differs from {run_id}'s: a changed run is a new run (omit --run)")
            index = run["attempt_count"] + 1
            tx.update(run_id, {"attempt_count": index})
        else:
            run_id, index = tx.next_id("Run"), 1
            tx.create("Run", {"id": run_id, "task_id": task_id, "experiment_id": exp["id"] if exp else None,
                              "category": category, "arm": None if arm is None else str(arm),
                              "seed": None if seed is None else str(seed), "deviates": deviates,
                              "deviation": deviation if deviates else None, "request_id": request_id, "spec": spec,
                              "spec_digest": digest_json(spec), "attempt_count": 1, "code_commit": commit,
                              "minutes": minutes, "purpose": purpose, "ticket_id": ticket})
            tx.link("of_task", run_id, task_id)
            if exp:
                tx.link("of_experiment", run_id, exp["id"])
        attempt = f"ATT-{run_id.split('-')[1]}-{index}"
        placed = {**res.record(), **compute.locations(store.project, res, commit, attempt)} if res else None
        expected = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds") \
            if minutes else None
        tx.create("Attempt", {"id": attempt, "run_id": run_id, "index": index, "request_id": request_id,
                              "execution_state": "prepared", "outcome": "unknown", "result_status": "absent",
                              "issues": [], "resource": placed, "expected_end": expected,
                              "submitted_by": actor.name})
        tx.link("of_run", attempt, run_id)
        result = {"run": run_id, "attempt": attempt, "category": category, "deviates": bool(deviates)}
        tx.remember("run.register", request_id, payload, result)
        tx.event("run.register", attempt, f"{run_id} {category}{' (deviates)' if deviates else ''} attempt {index}")
    note = (f"\nresource {res.name}: how to stage, run and fetch: rl compute steps --attempt {attempt}" if res and
            res.remote else "")
    return {**result, "summary": f"{run_id} {attempt} {category}" + (" DEVIATES (not evidence)" if deviates else "")
                                 + f" at {commit[:12]}" + (f", expected end {expected[11:16]} UTC" if expected else "")
                                 + ". Nothing was started: start it with your own tool, then rl run record" + note}


def _evidence_allowed(tx: Tx, task: dict, commit: str, changed: list[str]) -> None:
    """An evidence run needs a review that covers its commit, or the fast lane with no open blocker and the
    protected scope unchanged since the design (changed: the protected units the commit changed)."""
    ok, why = covers(tx, task["id"], commit, for_evidence=True)
    if not ok and task.get("fast_lane"):
        blockers = open_blockers(tx, task["id"], for_evidence=True)
        ok = not blockers and not changed
        why = blockers or (f"protected scope changed: {', '.join(changed)}" if changed else why)
    if not ok:
        raise RLError("INVALID", f"no evidence run at {commit[:12]}: {why}. Smoke and timing runs are trials "
                                 "(--trial --minutes N)")


def ended(store: Store, raw: str | None = None, attempt: str | None = None, outcome: str | None = None,
          note: str | None = None, namespace: str | None = None) -> dict:
    """The end of jobs, recorded on their attempts (8.8). The executor's command delivery runs this with the
    notification on stdin ({notification, msg_id, message}); the attempts are found by the job ids the message names
    (their bound execution references: scheduler jobs, of the executor namespace given, else of the one namespace
    that has the job id; a job id bound in two namespaces is left for the submitter to record). With attempt and
    outcome, one attempt is recorded directly."""
    found: list[tuple[str, str]] = []
    text = ""
    if raw:
        try:
            data = json.loads(raw)
            text = str(data.get("message") or data)
        except ValueError:
            text = raw
    pending: list[str] = []
    ambiguous: list[str] = []
    with store.read() as tx:
        if attempt:
            found = [(attempt, outcome or "unknown")]
        else:
            rows = job_rows(text)
            refs = [x for x in tx.all("ExecRef") if x["opaque_id"] in rows and x["kind"] == "hpc_job"
                    and (namespace is None or x["executor_namespace"] == namespace)]
            spaces: dict[str, set] = {}
            for x in refs:
                spaces.setdefault(x["opaque_id"], set()).add(x["executor_namespace"])
            for x in refs:
                if len(spaces[x["opaque_id"]]) > 1:
                    ambiguous.append(f"{x['opaque_id']} ({x['executor_namespace']})")
                    continue
                for e in tx.inc(x["id"], "bound_to"):
                    if rows[x["opaque_id"]] in TERMINAL:
                        found.append((e["id"], rows[x["opaque_id"]]))
                    else:
                        pending.append(e["id"])
    unsure = (f"; the job ids {', '.join(sorted(set(ambiguous)))} are bound in more than one executor namespace: "
              "name it with --namespace, or record each attempt with rl run record") if ambiguous else ""
    if not found:
        return {"recorded": [], "pending": pending, "ambiguous": ambiguous,
                "summary": (f"nothing ended: {', '.join(pending)} not finished by this message" if pending else
                            "no attempt of this project is bound to a job whose row this message gives") + unsure}
    done = []
    for att_id, out in found:
        with store.read() as tx:
            att = tx.require(att_id, "Attempt")
        if att["execution_state"] == "ended":
            continue
        obs = {"attempt_id": att_id, "source": "executor", "observed_at": now_utc(), "execution_state": "ended",
               "outcome": out, "note": (note or text)[:500] or None}
        record(store, obs)
        with store.write() as tx:
            tx.update(att_id, {"ended_note": (note or text)[:500] or None})
        done.append(att_id)
    _finish_merge_tests(store, [a for a, _ in found])  # also those ended before: acceptance may still wait on them
    return {"recorded": done, "pending": pending, "ambiguous": ambiguous,
            "summary": f"ended: {', '.join(done) or 'nothing new'}"
            + (f"; still running or unknown: {', '.join(pending)}" if pending else "") + unsure}


TERMINAL = ("success", "failed", "cancelled")
# One status row per job in an executor's notification (miyabi-mcp: "- <job id> <name>  <raw state> (<outcome>)
# walltime .."): only a row's own outcome ends its job; a job whose row says unknown (still queued or running, or
# lost by the scheduler) stays pending, a message without rows (prose) ends nothing, and neither does a notice
# whose header (its first line, "[miyabi-mcp] job state unknown in ..") says the state is unknown; a note the
# user attached further down does not count.
ROW = re.compile(r"^- (\S+) .*?\((success|failed|cancelled|unknown|None)\)", re.MULTILINE)


def job_rows(text: str) -> dict[str, str]:
    """job id -> outcome, from the job table of a notification: the status rows right under its header line, up to
    the first other line (a note the user attached below cannot add or change a row). None for a job-state-unknown
    notice; a job listed twice with different outcomes stays unknown."""
    lines = text.strip().splitlines()
    if not lines or re.match(r"\[[\w.-]+\] job state unknown in", lines[0]):
        return {}
    rows: dict[str, str] = {}
    for line in lines[1:] if lines[0].startswith("[") else lines:
        m = ROW.match(line)
        if not m:
            break
        rows[m[1]] = m[2] if rows.get(m[1], m[2]) == m[2] else "unknown"
    return rows


def _comparable(spec: dict, other: dict) -> str:
    """A retry repeats its run's spec, including the design it was sealed against: a run of another revision is a
    new run. Only a run registered before runs kept their design (it has none) is compared without it."""
    keep_design = "design" in spec and "design" in other
    return digest_json({k: v for k, v in spec.items() if keep_design or k != "design"})


def _finish_merge_tests(store: Store, attempts: list[str]) -> None:
    """Acceptance that waits on a merge test: an ended attempt of the run registered for it decides it (tasks.py
    checks the acceptance, the round and the merge commit)."""
    from .tasks import finish_merge
    for att_id in attempts:
        finish_merge(store, att_id)


def handled(store: Store, actor: Actor, attempts: list[str], note: str) -> dict:
    """The submitter has dealt with ended attempts (results imported, or retried): they leave its to-do list."""
    need(actor, "researcher", "scientist", "reviewer", what="marking attempts handled")
    with store.write() as tx:
        for a in attempts:
            tx.require(a, "Attempt")
            tx.update(a, {"handled_at": now_utc()})
        tx.event("run.handled", attempts[0] if attempts else None, f"{', '.join(attempts)}: {note}")
    return {"summary": f"handled: {', '.join(attempts)}"}


# -- record ---------------------------------------------------------------------------------------------------


def _newer(att: dict, obs: dict) -> bool:
    """Order by the source's own sequence when both come from one source, otherwise by observed_at."""
    if not att.get("last_observed_at"):
        return True
    if obs.get("source_sequence") is not None and att.get("last_sequence") is not None \
            and obs["source"] == att.get("last_source"):
        return obs["source_sequence"] > att["last_sequence"]
    return parse_time(obs["observed_at"]) >= parse_time(att["last_observed_at"])


def _project(att: dict, obs: dict, received: str) -> tuple[str, dict, list[str]]:
    """Derive the attempt's state from one more observation: (effect, changes, new issues)."""
    state, outcome = obs["execution_state"], obs["outcome"]
    newer = _newer(att, obs)
    applied = {"last_observed_at": obs["observed_at"], "last_record_id": obs["record_id"],
               "last_source": obs["source"], "last_sequence": obs.get("source_sequence")}
    if obs.get("correction_of"):
        changes = {**applied, "execution_state": state, "outcome": outcome, "needs_check": False,
                   "closed_at": received if state == "ended" else None}
        if state not in ("unknown",):
            changes.update(last_trusted_state=state, last_trusted_at=obs["observed_at"])
        return "correction", changes, [f"corrected by {obs['record_id']}: {obs.get('note')}"]
    if att["execution_state"] == "ended":
        if state == "ended":
            if outcome in (att["outcome"], "unknown"):
                return "confirming", {}, []
            if att["outcome"] == "unknown":
                return "applied", {**applied, "outcome": outcome}, []
            return "conflict", {"needs_check": True}, [
                f"{obs['record_id']} reports outcome {outcome}; recorded outcome {att['outcome']} kept — verify and "
                "record an explicit correction if needed"]
        if state == "unknown":
            return "observation_issue", {}, [f"{obs['record_id']}: lookup gave no state ({obs.get('raw_state') or 'unknown'}); "
                                             "the recorded end state stands"]
        if newer:
            return "conflict", {"needs_check": True}, [
                f"{obs['record_id']} reports {state} after the recorded end — verify with the execution tool"]
        return "stale", {}, []
    if not newer:
        if state == "ended":
            return "conflict", {"needs_check": True}, [
                f"{obs['record_id']} reports ended at {obs['observed_at']}, earlier than the last observation; "
                "order unclear — verify"]
        return "stale", {}, []
    if state == "unknown":
        return "applied", {**applied, "execution_state": "unknown"}, []
    changes = {**applied, "execution_state": state, "outcome": outcome,
               "last_trusted_state": state, "last_trusted_at": obs["observed_at"]}
    if state == "ended":
        changes["closed_at"] = received
    return "applied", changes, []


def _bind(tx: Tx, attempt: str, ref: dict, record_id: str, correction_of: str | None) -> tuple[str, str | None]:
    """Bind the attempt to the execution its observation names: (the execution ref, the one it replaced). The
    binding edge keeps the observation that brought it. A correction of exactly that observation moves the binding
    (a job id recorded by mistake); anything else naming another execution is a conflict, since a re-execution is a
    new attempt."""
    for key in ("kind", "executor_namespace", "opaque_id"):
        if not ref.get(key):
            raise RLError("INVALID", f"execution_ref needs {key}")
    if ref["kind"] not in ENUMS["exec_kind"]:
        raise RLError("INVALID", f"execution_ref.kind must be one of {', '.join(ENUMS['exec_kind'])}")
    found = [x for x in tx.all("ExecRef", opaque_id=str(ref["opaque_id"]))
             if x["executor_namespace"] == ref["executor_namespace"]]
    if found:
        xid = found[0]["id"]
        if found[0]["kind"] != ref["kind"]:
            raise RLError("CONFLICT", f"{xid} ({ref['executor_namespace']}/{ref['opaque_id']}) is a {found[0]['kind']}")
    else:
        xid = tx.next_id("ExecRef")
        tx.create("ExecRef", {"id": xid, "kind": ref["kind"], "executor_namespace": ref["executor_namespace"],
                              "opaque_id": str(ref["opaque_id"]), "locator": ref.get("locator")})
    bound = tx.out(attempt, "bound_to")
    if bound and bound[0]["id"] != xid:
        old, by = tx.require(bound[0]["id"], "ExecRef"), bound[0].get("observation")
        if correction_of and correction_of == by:
            tx.unlink("bound_to", attempt, old["id"])
            tx.link("bound_to", attempt, xid, observation=record_id)
            tx.event("run.rebound", attempt, f"{old['id']} -> {xid}: {record_id} corrects {correction_of}")
            return xid, old["id"]
        where = f"{old['id']} ({old['executor_namespace']}/{old['opaque_id']})"
        new = "a re-execution is a new attempt (rl run register --run <RUN> --request-id <new>)"
        if correction_of:
            why = (f"{by} brought that binding, not {correction_of}" if by else
                   "that binding was recorded before rl kept the observation that brought it")
            raise RLError("CONFLICT", f"{attempt} is bound to {where}; {why}, so this correction cannot move it. If "
                                      f"the job ran again, {new}")
        fix = (f"rl run record --attempt {attempt} --correction-of {by} --exec-id <the right id> --note "
               f"\"<how you know>\"" if by else "(that binding was recorded before rl kept the observation that "
               "brought it, so it cannot be moved)")
        raise RLError("CONFLICT", f"{attempt} is bound to {where}. If that id was recorded by mistake, correct the "
                                  f"observation that brought it: {fix}. If the job ran again, {new}")
    if not bound:
        tx.link("bound_to", attempt, xid, observation=record_id)
    return xid, None


def record(store: Store, obs: dict) -> dict:
    unknown = set(obs) - OBS_FIELDS
    if unknown:
        raise RLError("INVALID", f"unknown observation fields {sorted(unknown)}; allowed: {sorted(OBS_FIELDS)}")
    for key in ("attempt_id", "source", "observed_at", "execution_state"):
        if obs.get(key) in (None, ""):
            raise RLError("INVALID", f"observation needs {key}")
    obs = {**obs, "outcome": obs.get("outcome") or "unknown"}
    if obs["execution_state"] not in ENUMS["execution_state"] or obs["outcome"] not in ENUMS["outcome"]:
        raise RLError("INVALID", f"execution_state is one of {ENUMS['execution_state']}; outcome one of {ENUMS['outcome']}")
    if obs["outcome"] != "unknown" and obs["execution_state"] != "ended":
        raise RLError("INVALID", "an outcome other than unknown needs execution_state ended")
    if obs.get("correction_of") and not obs.get("note"):
        raise RLError("INVALID", "a correction needs a note stating the evidence for it")
    parse_time(obs["observed_at"])
    if obs.get("occurred_at"):
        parse_time(obs["occurred_at"])  # the source's own event time; stored as evidence, never used for ordering
    content = {k: v for k, v in obs.items() if k != "record_id"}
    digest = digest_json(content)
    obs["record_id"] = obs.get("record_id") or f"OBS-{short(digest, 16)}"
    warnings = []
    ref = obs.get("execution_ref")
    if ref and ref.get("kind") == "local_process" and str(ref.get("opaque_id", "")).isdigit() \
            and (ref.get("executor_namespace") or "").count(":") < 1:
        warnings.append("a bare PID is not unique: include host and boot/start identity in executor_namespace")
    with store.write() as tx:
        existing = tx.get(obs["record_id"], "Observation")
        if existing:
            if existing["content_digest"] != digest:
                raise RLError("CONFLICT", f"record {obs['record_id']} was already received with different content")
            return {"record_id": obs["record_id"], "duplicate": True,
                    "summary": f"duplicate {obs['record_id']} ignored (already {existing['effect']})"}
        att = tx.require(obs["attempt_id"], "Attempt")
        if obs.get("correction_of"):
            target = tx.require(obs["correction_of"], "Observation")
            if target["attempt_id"] != att["id"]:
                raise RLError("INVALID", f"{obs['correction_of']} belongs to {target['attempt_id']}")
        xid, replaced = _bind(tx, att["id"], ref, obs["record_id"], obs.get("correction_of")) if ref else (None, None)
        received = now_utc()
        effect, changes, issues = _project(att, obs, received)
        if issues:
            changes["issues"] = (att.get("issues") or []) + issues
        tx.create("Observation", {"id": obs["record_id"], "attempt_id": att["id"], "source": obs["source"],
                                  "observed_at": obs["observed_at"], "received_at": received,
                                  "source_sequence": obs.get("source_sequence"),
                                  "execution_state": obs["execution_state"], "outcome": obs["outcome"],
                                  "raw_state": obs.get("raw_state"), "source_ref": obs.get("source_ref"),
                                  "occurred_at": obs.get("occurred_at"),
                                  "correction_of": obs.get("correction_of"), "note": obs.get("note"),
                                  "content_digest": digest, "effect": effect})
        if changes:
            att = tx.update(att["id"], changes)
        usage = None
        if obs.get("usage"):
            usage = _usage(tx, obs["usage"], [att["id"]])
        tx.event("run.record", att["id"], f"{obs['record_id']} {obs['execution_state']}/{obs['outcome']} -> {effect}")
    if att["execution_state"] == "ended":
        _finish_merge_tests(store, [att["id"]])
    return {"record_id": obs["record_id"], "effect": effect, "attempt": brief(att), "execution_ref": xid,
            "usage": usage, "warnings": warnings,
            "summary": f"{obs['record_id']} {effect}; {brief(att)}" + (f"; {issues[0]}" if issues else "")
                       + (f"; bound to {xid} now, in place of {replaced}" if replaced else "")}


def executor_hint(store: Store, attempt: str) -> str | None:
    """For an attempt on a resource whose jobs an external executor submits (jobs: mcp:.. or skill:..): job ids
    come from that executor's export, not from memory."""
    with store.read() as tx:
        placed = tx.require(attempt, "Attempt").get("resource") or {}
    if not placed.get("name"):
        return None
    try:
        res = compute.resource(store.project, placed["name"])
    except RLError:
        return None
    if not res.jobs.startswith(("mcp:", "skill:")):
        return None
    return (f"{attempt} runs on {res.name}, whose jobs {res.jobs} submits: take the job id from its export (rl run "
            "record --observation @<export.jsonl>) rather than typing it; a wrong one is corrected with "
            "--correction-of the observation that recorded it")


# -- results --------------------------------------------------------------------------------------------------


def _verify_artifacts(project: Project, artifacts: list) -> tuple[list[dict], str]:
    out, counts = [], {}
    for art in artifacts:
        if not isinstance(art, dict) or not (art.get("path") or art.get("uri")):
            raise RLError("INVALID", "each artifact is {path: <local path>} or {uri: <remote>}, with optional digest")
        entry = {k: art.get(k) for k in ("path", "uri", "role", "digest") if art.get(k)}
        if art.get("path"):
            local = project.abs(art["path"])
            if not local.is_file():
                entry["check"] = "missing_locally"
            else:
                actual = digest_file(local)
                entry["local_digest"] = actual
                reported = art.get("digest")
                if not reported or not reported.startswith("blake2b:"):
                    entry["check"] = "read_locally" + ("" if not reported else "; reported digest uses another algorithm, not compared")
                else:
                    entry["check"] = "verified_locally" if reported == actual else "digest_mismatch"
        else:
            entry["check"] = "remote_reference_not_read"
        counts[entry["check"].split(";")[0]] = counts.get(entry["check"].split(";")[0], 0) + 1
        out.append(entry)
    scope = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no artifacts"
    return out, scope


def _mismatches(tx: Tx, run: dict, reported: dict) -> tuple[list[dict], list[str]]:
    """What a result reports about itself (arm, seed, commit, config, split) against the run's registration and,
    for an evidence run, the sealed revision and arm it was registered against (9.1 import), whatever the
    experiment is now. (mismatches, fields the result does not report)."""
    found, unreported = [], []
    spec = run["spec"]
    for field in ("arm", "seed", "commit", "split"):
        expected, actual = spec.get(field), reported.get(field)
        if expected is None:
            continue
        if actual is None:
            unreported.append(field)
        elif str(actual) != str(expected) and not (field == "commit" and len(str(actual)) >= 7
                                                   and str(expected).startswith(str(actual))):
            found.append({"field": field, "expected": expected, "actual": actual})
    if not isinstance(reported.get("config"), dict):  # absent or null: what ran is not said
        unreported.append("config")
    elif run.get("category") == "evidence" and not (spec.get("design") or {}).get("arm"):
        unreported.append("the sealed design of its run")  # registered before runs kept it: nothing to check against
    elif (spec.get("design") or {}).get("arm"):
        sealed = {"arms": [spec["design"]["arm"]], "allowed": spec["design"].get("allowed") or []}
        for problem in manifest.check_run(sealed, spec.get("arm"), spec.get("seed"), reported["config"],
                                          reported.get("split", spec.get("split"))):
            found.append({"field": "config", "expected": f"the sealed manifest of {spec['design']['experiment']}@"
                                                         f"{spec['design']['revision']}", "actual": problem})
    return found, unreported


def _best(results: list[dict]) -> str:
    statuses = {r["status"] for r in results if r["status"] != "rejected"}
    return "complete" if "complete" in statuses else "partial" if "partial" in statuses else (
        "rejected" if results else "absent")


def import_result(store: Store, actor: Actor, attempt: str, reported: dict, request_id: str) -> dict:
    """Import the result manifest a run wrote (status, metrics, artifacts, and what it ran: arm, seed, commit,
    config, split). One that differs from the registration or the sealed manifest is kept but marked unusable:
    it cannot be evidence."""
    need(actor, "researcher", "scientist", "reviewer", what="importing a result")
    if reported.get("status") not in ("partial", "complete"):
        raise RLError("INVALID", f"manifest.status is {reported.get('status')!r}, not partial or complete: "
                                 "import-result takes the manifest of a run that produced results. A run that ended "
                                 f"without any is recorded by its end: `rl run record --attempt {attempt} --state "
                                 "ended --outcome failed --source-ref <its log or manifest>`")
    artifacts = reported.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise RLError("INVALID", "manifest.artifacts is a list")
    verification, scope = _verify_artifacts(store.project, artifacts)
    manifest_digest = digest_json(reported)
    payload = digest_json({"attempt": attempt, "manifest": reported})
    with store.write() as tx:
        previous = tx.request("run.import-result", request_id, payload)
        if previous:
            return {**previous, "duplicate": True, "summary": f"duplicate request {request_id}: {previous['result']}"}
        att = tx.require(attempt, "Attempt")
        run = tx.require(att["run_id"], "Run")
        prior = tx.all("Result", attempt_id=attempt)
        same = [r for r in prior if r["manifest_digest"] == manifest_digest]
        if same:
            result = {"result": same[0]["id"], "attempt": attempt}
            tx.remember("run.import-result", request_id, payload, result)
            return {**result, "duplicate": True, "summary": f"same manifest already imported as {same[0]['id']}"}
        mismatches, unreported = _mismatches(tx, run, reported)
        bad = [v["path"] for v in verification if v["check"] == "digest_mismatch"]
        rid = tx.next_id("Result")
        scope_text = scope + (f"; fields not reported: {', '.join(unreported)}" if unreported else "")
        tx.create("Result", {"id": rid, "attempt_id": attempt, "request_id": request_id, "revision": len(prior) + 1,
                             "status": reported["status"], "manifest": reported, "manifest_digest": manifest_digest,
                             "spec_match": not mismatches, "mismatches": mismatches or None,
                             "verification": verification, "verification_scope": scope_text,
                             "usable": not mismatches and not bad and not run.get("deviates")
                             and not (unreported and run["category"] == "evidence")})
        tx.update(attempt, {"result_status": _best(prior + [{"status": reported["status"]}])})
        if mismatches and run["category"] == "evidence" and not run.get("deviates"):
            tx.update(run["id"], {"deviates": True, "deviation": f"result {rid} differs: "
                                  + ", ".join(m["field"] for m in mismatches)})
        result = {"result": rid, "attempt": attempt}
        tx.remember("run.import-result", request_id, payload, result)
        tx.event("run.import_result", attempt, f"{rid} {reported['status']} ({scope_text})")
    notes = []
    if mismatches:
        notes.append("differs from the registration or the sealed manifest in " + ", ".join(
            f"{m['field']} ({m['actual']})" for m in mismatches) + ": kept, marked deviating, not evidence")
    arm = next((m for m in mismatches if m["field"] == "arm"), None)
    if arm:
        notes.append(f"the run was registered as arm {arm['expected']!r} and the result says {arm['actual']!r}: if the "
                     f"code is right, register a new run with --arm {arm['actual']} and run it again; if the "
                     f"registration is right, fix the code to write arm {arm['expected']!r}")
    if bad:
        notes.append(f"digest mismatch: {', '.join(bad)}")
    if unreported and run["category"] == "evidence":
        notes.append(f"unverified: the result does not report {', '.join(unreported)}, so it cannot be checked "
                     "against its registration and cannot be evidence; have the run write them and import again")
    if att["execution_state"] != "ended":
        notes.append(f"attempt is still {att['execution_state']}: a result file is not an execution end record")
    return {**result, "mismatches": mismatches, "verification_scope": scope_text,
            "summary": f"{rid} {reported['status']} for {attempt} (checked: {scope_text})"
                       + ("; " + "; ".join(notes) if notes else "")}


def reject_result(store: Store, actor: Actor, result_id: str, reason: str) -> dict:
    """What rests on the result (the evidence drawn from it, their hypotheses, decisions based on either) is
    marked for re-checking; whether it still holds is the researcher's judgment."""
    need(actor, "researcher", "scientist", what="rejecting a result")
    with store.write() as tx:
        res = tx.require(result_id, "Result")
        tx.update(result_id, {"status": "rejected", "rejected_reason": reason, "usable": False})
        results = tx.all("Result", attempt_id=res["attempt_id"])
        tx.update(res["attempt_id"], {"result_status": _best(results)})
        flagged = recheck_dependents(tx, result_id, f"{result_id} rejected: {reason}")
        tx.event("run.reject_result", result_id, reason)
    return {"result": result_id, "flagged": flagged,
            "summary": f"{result_id} rejected: {reason}" + (f"; re-check {', '.join(flagged)}" if flagged else "")}


# -- usage ----------------------------------------------------------------------------------------------------


def _usage(tx: Tx, usage: dict, attempts: list[str], revise: bool = False) -> dict:
    for key in ("accounting_ref", "quantity", "unit", "resource_type"):
        if usage.get(key) in (None, ""):
            raise RLError("INVALID", f"usage needs {key} (unreported usage is simply not recorded, never zero)")
    if ":" not in usage["accounting_ref"]:
        raise RLError("INVALID", "accounting_ref needs a namespace, e.g. scheduler:12345 or local:<host>:<run>")
    body = {k: usage.get(k) for k in ("quantity", "unit", "resource_type", "hardware", "basis", "source_ref")}
    body["basis"] = body["basis"] or "reported"
    digest = digest_json({**body, "attempts": sorted(attempts)})
    current = [u for u in tx.all("Usage", accounting_ref=usage["accounting_ref"]) if not u.get("superseded")]
    if current:
        old = current[-1]
        if old["content_digest"] == digest:
            return {"usage": old["id"], "duplicate": True}
        if not revise:
            raise RLError("CONFLICT", f"{usage['accounting_ref']} is already recorded as {old['id']} "
                                      f"({old['quantity']:g} {old['unit']}, {old.get('source_ref') or 'no source_ref'}); "
                                      "if that record is right there is nothing to add, otherwise pass --revise "
                                      "to record a corrected revision")
    uid = tx.next_id("Usage")
    tx.create("Usage", {"id": uid, "accounting_ref": usage["accounting_ref"], "quantity": float(body["quantity"]),
                        "unit": body["unit"], "resource_type": body["resource_type"], "hardware": body["hardware"],
                        "basis": body["basis"], "source_ref": body["source_ref"], "note": usage.get("note"),
                        "revision": current[-1]["revision"] + 1 if current else 1, "content_digest": digest})
    for old in current:
        tx.update(old["id"], {"superseded": True})
        tx.link("supersedes", uid, old["id"])
    for a in attempts:
        tx.require(a, "Attempt")
        tx.link("covers", uid, a)
    return {"usage": uid, "duplicate": False}


def add_usage(store: Store, usage: dict, attempts: list[str], revise: bool = False) -> dict:
    with store.write() as tx:
        out = _usage(tx, usage, attempts, revise)
        if not out["duplicate"]:
            tx.event("run.usage", out["usage"], f"{usage['quantity']} {usage['unit']} {usage['accounting_ref']}")
    return {**out, "summary": f"{out['usage']} {'already recorded' if out['duplicate'] else 'recorded'} "
                              f"({usage['quantity']} {usage['unit']}, {usage['accounting_ref']})"}


def usage_summary(tx: Tx) -> dict:
    totals: dict[tuple, float] = {}
    covered = set()
    for u in tx.all("Usage"):
        if u.get("superseded"):
            continue
        key = (u["resource_type"], u["unit"], u.get("hardware") or "-", u["basis"])
        totals[key] = totals.get(key, 0.0) + u["quantity"]
        covered |= {e["id"] for e in tx.out(u["id"], "covers")}
    attempts = [a["id"] for a in tx.all("Attempt") if a["execution_state"] != "prepared"]
    return {"totals": [{"resource_type": k[0], "unit": k[1], "hardware": k[2], "basis": k[3], "quantity": v}
                       for k, v in sorted(totals.items())],
            "unreported_attempts": [a for a in attempts if a not in covered]}


# -- reading --------------------------------------------------------------------------------------------------


def _age_h(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return (datetime.now(timezone.utc) - parse_time(stamp)).total_seconds() / 3600


def describe_attempt(tx: Tx, att: dict) -> str:
    """An attempt in one line."""
    refs = [tx.require(e["id"]) for e in tx.out(att["id"], "bound_to")]
    on = f" on {att['resource']['name']}" if att.get("resource") else ""
    ref_text = ", ".join(f"{r['kind']} {r['executor_namespace']}/{r['opaque_id']}" for r in refs) or "no external ref yet"
    age = _age_h(att.get("last_observed_at"))
    fresh = "never observed" if age is None else f"last observed {age:.1f}h ago" + (
        " (stale? refresh with your execution tool)" if age > STALE_AFTER_H and att["execution_state"] != "ended" else "")
    trusted = (f"; last trusted {att['last_trusted_state']} at {att['last_trusted_at']}"
               if att.get("last_trusted_state") and att["execution_state"] == "unknown" else "")
    run = tx.require(att["run_id"])
    end = f"; expected end {att['expected_end']}" if att.get("expected_end") and att["execution_state"] != "ended" \
        else ""
    return (f"{brief(att)} {run['category']}{' DEVIATES' if run.get('deviates') else ''}{on} {ref_text}; "
            f"{fresh}{trusted}{end}")


def show(store: Store, entity_id: str | None = None, active: bool = False) -> dict:
    with store.read() as tx:
        if entity_id:
            rec = tx.require(entity_id, "Run", "Attempt", "Experiment", "Task")
            runs = ([rec] if rec["_label"] == "Run" else
                    [tx.require(rec["run_id"])] if rec["_label"] == "Attempt" else
                    tx.all("Run", task_id=entity_id) if rec["_label"] == "Task" else
                    tx.all("Run", experiment_id=entity_id))
            lines = []
            for run in runs:
                lines.append(f"{run['id']} {run['category']} of {run['task_id']} arm {run.get('arm')} seed "
                             f"{run.get('seed')} at {(run.get('code_commit') or '?')[:12]} request {run['request_id']}"
                             + (f" DEVIATES: {run.get('deviation')}" if run.get("deviates") else ""))
                lines.append(f"  spec: {run['spec']}")
                for att in tx.all("Attempt", run_id=run["id"]):
                    if rec["_label"] == "Attempt" and att["id"] != entity_id:
                        continue
                    lines.append(f"  {describe_attempt(tx, att)}")
                    for issue in att.get("issues") or []:
                        lines.append(f"    issue: {issue}")
                    for o in sorted(tx.all("Observation", attempt_id=att["id"]), key=lambda o: o["received_at"])[-5:]:
                        lines.append(f"    obs {o['id']} {o['observed_at']} {o['source']}: "
                                     f"{o['execution_state']}/{o['outcome']} -> {o['effect']}"
                                     + (f" (event at {o['occurred_at']})" if o.get("occurred_at") else ""))
                    for r in tx.all("Result", attempt_id=att["id"]):
                        lines.append(f"    result {r['id']} r{r['revision']} {r['status']} usable={r.get('usable')} "
                                     f"({r['verification_scope']}) metrics={r['manifest'].get('metrics')}")
            return {"summary": "\n".join(lines) or f"no runs for {entity_id}"}
        atts = tx.all("Attempt")
        if active:
            atts = [a for a in atts if a["execution_state"] != "ended" or a.get("needs_check")]
        lines = [describe_attempt(tx, a) for a in atts]
        usage = usage_summary(tx)
    head = f"{len(lines)} {'unfinished ' if active else ''}attempt(s); recorded facts only — nothing was queried"
    lines += [f"usage: {t['quantity']:g} {t['unit']} {t['resource_type']} on {t['hardware']} ({t['basis']})"
              for t in usage["totals"]]
    if usage["unreported_attempts"]:
        lines.append(f"usage unreported for: {', '.join(usage['unreported_attempts'])}")
    return {"summary": "\n".join([head, *lines]), "usage": usage}


def digest_paths(paths: list[str]) -> dict:
    """blake2b digests for files, and a combined digest for directories (sorted relative paths + file digests)."""
    out = {}
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files = sorted(f for f in path.rglob("*") if f.is_file() and "__pycache__" not in f.parts
                           and ".git" not in f.parts)
            out[p] = digest_json([[str(f.relative_to(path)), digest_file(f)] for f in files])
        elif path.is_file():
            out[p] = digest_file(path)
        else:
            raise RLError("NOT_FOUND", f"{p} does not exist")
    return {"digests": out, "summary": "\n".join(f"{v}  {k}" for k, v in out.items())}
