"""Project setup and the scientific graph: hypotheses, experiments, evidence, decisions and relations (design ch. 10).

Decisions are the only way a hypothesis changes state. A verdict (supported, challenged, inconclusive) or a stop
(paused, retired) starts as a draft and becomes final only after a passing conclusion review of that draft
(reviews.py); a route decision (why a new experiment, how an answer is followed) and a phase conclusion are final
at once and change no hypothesis.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import documents, gitops, labels
from .project import CONFIG_NAME, RESEARCH_DIRS, Project, load_config, write_default_config
from .roles import Actor, need
from .schema import LINKABLE, RELATIONS
from .store import Store, Tx, _sort_key, label_of
from .upgrade import environment
from .util import RLError, digest_file, now_utc

CONSTRAINTS_TEMPLATE = """# Constraints

What only the user may change. The coordinator fills this from the plan once, with the user; every role follows
it, and what it does not allow goes to the user as a decision request.

- Goal and claims in scope:
- Allowed data, and which compute resources may be used for what (the resources themselves are in
  smooth-research.yaml, compute):
- Resource ceiling (total; units such as GPU·h, MIG·h, node·h):
- Actions that need the user's decision first:
"""

DEFAULT_PYTHON = "3.13"  # of the project's environment; bin/rl makes it before init (rl init --python)
VERDICTS = ("supported", "challenged", "inconclusive")
STOPS = ("paused", "retired")
GITIGNORE = ("# the database and raw run artifacts stay out of git; sealed documents are kept\nstate/\n"
             "artifacts/\n")


# -- project --------------------------------------------------------------------------------------------------


def ensure_layout(project: Project, skip: list[Path] = ()) -> list[str]:
    """The fixed directories and starter files, created where missing (nothing existing is changed)."""
    made = []

    def write(path: Path, text: str) -> None:
        if not path.exists() and path not in skip:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            made.append(project.rel(path))

    for path in [project.code_root, project.docs_root / "explanations"] + \
            [project.research_root / name for name in RESEARCH_DIRS]:
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            made.append(project.rel(path) + "/")
    write(project.research_root / ".gitignore", GITIGNORE)
    write(project.docs_root / ".gitignore", "# drawn by the watcher (rl view), not tracked\nresearch.html\n")
    write(project.code_root / ".gitignore", "__pycache__/\n*.py[cod]\n")
    write(project.constraints_path, CONSTRAINTS_TEMPLATE)
    return made


def init_project(root: Path, plan: str | None = None, title: str | None = None) -> dict:
    """Create the project (or check an existing one) and import the plan."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_default_config(root)
    project = Project(root, load_config(root))
    ensure_layout(project)
    created = not project.db_path.exists()
    if created:
        Store.create(project, title)
    env_line, _ = environment(project)
    store = Store(project, "coordinator")
    prep = None
    if plan:
        source = Path(plan).expanduser().resolve()
        if not source.is_file():
            raise RLError("NOT_FOUND", f"plan not found: {plan}")
        snapshot = project.plan_path
        with store.read() as tx:
            sealed = tx.meta().get("plan_ref")
            sealed_digest = tx.require(sealed, "DocRevision")["digest"] if sealed else None
        held = sealed_digest or (digest_file(snapshot) if snapshot.exists() and snapshot.resolve() != source else None)
        if held and held != digest_file(source):
            raise RLError("CONFLICT", f"the project already holds a different plan ({sealed or 'not sealed'}); a new "
                                      "direction is a new tree task (rl task create --kind tree --reason ..)")
        if snapshot.resolve() != source:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, snapshot)
        prep = documents.prepare(project, snapshot, "plan")
    with store.write() as tx:
        plan_ref = documents.register(tx, prep, documents.PROJECT) if prep else tx.meta().get("plan_ref")
        if prep:
            tx.set_meta(plan_ref=plan_ref, plan_source=str(Path(plan).expanduser().resolve()))
        tx.event("project.init" if created else "project.reinit", None, f"plan {plan_ref}; {env_line}")
    return {"project": str(root), "created": created, "plan_ref": plan_ref, "config": str(root / CONFIG_NAME),
            "warnings": project.warnings,
            "summary": (f"{'initialized' if created else 'already initialized'} {root}"
                        + (f"; plan {plan_ref} ({project.rel(project.plan_path)})" if plan_ref else "; no plan yet")
                        + f"\n{env_line}"),
            "next": (["commit the new project (git init; git add -A; git commit): task branches start from the "
                      "mainline"] if created else [])
                    + ["rl resume: opens the coordinator's pane in herdr and starts the watcher"]}


# -- hypotheses -----------------------------------------------------------------------------------------------


def add_hypothesis(store: Store, actor: Actor, title: str, statement: str, purpose: str, scope: str | None = None,
                   source: str | None = None, parent: str | None = None, assumes: list[str] = (),
                   cost: str | None = None, composite: bool = False, candidate: bool = False,
                   motivated_by: list[str] = ()) -> dict:
    """A hypothesis written by a scientist: on a tree task (the initial tree) or as a proposed child in a
    hypothesis task. The primary parent edge places it in the tree; ASSUMES edges name its premises (4.7)."""
    need(actor, "scientist", what="adding a hypothesis")
    if not (purpose or "").strip():
        raise RLError("INVALID", "a hypothesis needs --purpose: one sentence on what testing it answers (the "
                                 "research page shows it)")
    with store.write() as tx:
        task = tx.require(actor.subject, "Task")
        if task["kind"] not in ("tree", "hypothesis"):
            raise RLError("INVALID", f"hypotheses are added on a tree or hypothesis task, not on {task['kind']} "
                                     f"{task['id']}")
        if parent:
            tx.require(parent, "Hypothesis")
        label = labels.for_hypothesis(tx, [parent] if parent else [], parent)
        hid = tx.next_id("Hypothesis")
        tx.create("Hypothesis", {"id": hid, "label": label, "title": title, "statement": statement, "scope": scope,
                                 "source": source, "purpose": purpose.strip(), "evidence_status": "untested",
                                 "disposition": "candidate" if candidate or task["kind"] == "hypothesis" else "active",
                                 "revision": 1, "cost": cost, "composite": composite or None, "origin": task["id"]})
        if parent:
            tx.link("derived_from", hid, parent, primary=True)
        for premise in assumes:
            tx.link("assumes", hid, premise)
        for m in motivated_by:
            tx.link("motivated_by", hid, m)
        tx.event("hypothesis.added", hid, f"{label}: {title}")
    return {"id": hid, "label": label, "summary": f"{label} ({hid}): {title}"}


def revise_hypothesis(store: Store, actor: Actor, hid: str, reason: str, statement: str | None = None,
                      scope: str | None = None, title: str | None = None, purpose: str | None = None,
                      source: str | None = None, cost: str | None = None) -> dict:
    need(actor, "scientist", what="revising a hypothesis")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        changes = {k: v for k, v in (("title", title), ("purpose", purpose), ("source", source), ("cost", cost))
                   if v is not None}
        scientific = {k: v for k, v in (("statement", statement), ("scope", scope)) if v is not None and v != h.get(k)}
        if scientific:
            if h["evidence_status"] != "untested":
                raise RLError("INVALID", f"{hid} is {h['evidence_status']}: a decided hypothesis keeps its statement; "
                                         "propose a child hypothesis instead")
            changes.update(scientific, revision=h["revision"] + 1)
        if not changes:
            return {"id": hid, "summary": f"{hid} unchanged"}
        tx.update(hid, changes)
        tx.event("hypothesis.revised", hid, reason)
    return {"id": hid, "summary": f"{hid} revised: {reason}"}


def flag_hypothesis(store: Store, actor: Actor, hid: str, lack: str, basis: str, options: str) -> dict:
    """A scientist marks a hypothesis whose novelty or feasibility falls short; it stays a candidate until the user
    decides (4.8)."""
    need(actor, "scientist", what="flagging a hypothesis")
    if lack not in ("novelty", "feasibility"):
        raise RLError("INVALID", "--lack is novelty or feasibility")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        tx.update(hid, {"flag": {"lack": lack, "basis": basis, "options": options, "by": actor.name},
                        "disposition": "candidate" if h["disposition"] == "active" else h["disposition"]})
        tx.event("hypothesis.flagged", hid, f"{lack}: {basis}")
    return {"id": hid, "summary": f"{hid} flagged ({lack}); it stays a candidate until the user decides"}


def settle_flag(store: Store, actor: Actor, hid: str, outcome: str) -> dict:
    """The coordinator records the user's decision on a flagged hypothesis (keep it active, or leave it out)."""
    need(actor, "coordinator", what="settling a flagged hypothesis")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        if not h.get("flag"):
            raise RLError("INVALID", f"{hid} is not flagged")
        flag = {**h["flag"], "settled": outcome, "settled_at": now_utc()}
        changes = {"flag": flag}
        if outcome == "keep":
            changes["disposition"] = "active"
        elif outcome == "drop":
            changes["disposition"] = "retired"
        else:
            raise RLError("INVALID", "--outcome is keep or drop; narrowing or another test is the scientist's "
                                     "change, returned to the tree task")
        tx.update(hid, changes)
        tx.event("hypothesis.flag_settled", hid, outcome)
    return {"id": hid, "summary": f"{hid}: flag settled ({outcome})"}


def defer_hypothesis(store: Store, actor: Actor, hid: str, reason: str | None) -> dict:
    """The coordinator marks a candidate 'not now' (or clears it with reason None); scheduling only (7.1)."""
    need(actor, "coordinator", what="deferring a hypothesis")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        if h["evidence_status"] != "untested":
            raise RLError("INVALID", f"{hid} is already {h['evidence_status']}")
        tx.update(hid, {"defer_reason": reason})
        tx.event("hypothesis.deferred" if reason else "hypothesis.undeferred", hid, reason or "")
    return {"id": hid, "summary": f"{hid} {'not now: ' + reason if reason else 'back among the candidates'}"}


# -- experiments ----------------------------------------------------------------------------------------------


def add_experiment(store: Store, actor: Actor, title: str, question: str, hypotheses: list[str],
                   motivated_by: str, manifest_path: str | None = None, protected: list[str] = (),
                   semantic_tests: str | None = None, brief: str | None = None, estimate: str | None = None) -> dict:
    """A new experiment, a draft until a task carrying it is dispatched (the design commit is sealed then). It points
    back to the short decision that motivates it (4.5)."""
    need(actor, "scientist", what="adding an experiment")
    if not hypotheses:
        raise RLError("INVALID", "an experiment tests at least one hypothesis (--tests H-0001)")
    prep = documents.prepare(store.project, brief, "brief") if brief else None
    with store.write() as tx:
        d = tx.require(motivated_by, "Decision")
        if d["kind"] != "route" or d["state"] != "final":
            raise RLError("INVALID", f"--motivated-by is the short route decision on why this experiment now "
                                     f"(rl decide --kind route ..); {motivated_by} is a {d['state']} {d['kind']}")
        for hid in hypotheses:
            tx.require(hid, "Hypothesis")
        label = labels.for_experiment(tx, list(hypotheses))
        eid = tx.next_id("Experiment")
        tx.create("Experiment", {"id": eid, "label": label, "title": title, "question": question, "revision": 1,
                                 "manifest_path": manifest_path, "protected": list(protected) or None,
                                 "semantic_tests": semantic_tests, "estimate": estimate})
        if prep:
            tx.update(eid, {"brief_ref": documents.register(tx, prep, eid)})
        for hid in hypotheses:
            tx.link("tests", eid, hid, revision=tx.require(hid)["revision"])
        tx.link("motivated_by", eid, motivated_by)
        tx.event("experiment.added", eid, f"{label}: {title}")
    return {"id": eid, "label": label, "summary": f"{label} ({eid}) draft: {title}"}


def update_experiment(store: Store, actor: Actor, eid: str, reason: str | None = None, **fields) -> dict:
    """Change an experiment's design fields. A draft (not sealed) changes freely; a sealed one becomes a new
    revision with a reason, and the next dispatch seals it again (4.6)."""
    need(actor, "scientist", what="changing an experiment")
    brief = fields.pop("brief", None)
    prep = documents.prepare(store.project, brief, "brief") if brief else None
    with store.write() as tx:
        exp = tx.require(eid, "Experiment")
        changes = {k: v for k, v in fields.items() if v is not None}
        if prep:
            changes["brief_ref"] = documents.register(tx, prep, eid)
        if exp.get("design_commit"):
            if not reason:
                raise RLError("INVALID", f"{eid}@{exp['revision']} is sealed (design {exp['design_commit'][:12]}): a "
                                         "change is a new revision; give --reason")
            first = first_evidence_run(tx, eid)
            changes.update(revision=exp["revision"] + 1, revision_reason=reason, design_commit=None, sealed_at=None,
                           manifest=None, manifest_digest=None)
            tx.event("experiment.revised", eid, f"@{exp['revision'] + 1} "
                                                f"{'after' if first else 'before'} the first evidence run: {reason}")
        # every edit counts, so a clearance never carries over an edit and its undoing (design_key)
        changes["edit_seq"] = (exp.get("edit_seq") or 0) + 1
        tx.update(eid, changes)
    return {"id": eid, "summary": f"{eid} updated" + (f" (new revision: {reason})" if reason else "")}


def first_evidence_run(tx: Tx, eid: str) -> str | None:
    runs = [r for r in tx.all("Run", experiment_id=eid) if r["category"] == "evidence"]
    return runs[0]["id"] if runs else None


# -- evidence -------------------------------------------------------------------------------------------------


def add_evidence(store: Store, actor: Actor, task_id: str, summary: str, level: str, results: list[str] = (),
                 attempts: list[str] = (), scope: str | None = None, artifacts: list[str] = (),
                 doc: str | None = None) -> dict:
    """A fact drawn from results, without direction (10.2 rule 4). Only evidence runs that do not deviate from the
    design count (9.1)."""
    need(actor, "researcher", what="writing evidence")
    if not results and not attempts:
        raise RLError("INVALID", "evidence is drawn from results or attempts: --results RES-x or --attempts ATT-x")
    prep = documents.prepare(store.project, doc, "analysis") if doc else None
    with store.write() as tx:
        task = tx.require(task_id, "Task")
        if task["kind"] != "execute":
            raise RLError("INVALID", f"evidence comes from an execute task; {task_id} is {task['kind']} (a "
                                     "preparation task's outputs are not evidence)")
        exp = task_experiment(tx, task_id)
        runs = []
        for res_id in results:
            res = tx.require(res_id, "Result")
            if not res.get("usable", True):
                why = (f"differs from its run's registration ({res['mismatches']})" if res.get("mismatches") else
                       f"is {res['status']}" if res["status"] == "rejected" else
                       f"cannot be checked against its run ({res.get('verification_scope')})")
                raise RLError("INVALID", f"{res_id} {why}: it cannot be evidence")
            runs.append(tx.require(tx.require(res["attempt_id"])["run_id"]))
        runs += [tx.require(tx.require(a, "Attempt")["run_id"]) for a in attempts]
        for run in runs:
            if run["category"] != "evidence":
                raise RLError("INVALID", f"{run['id']} is a {run['category']} run: only evidence runs can be cited")
            if run.get("deviates"):
                raise RLError("INVALID", f"{run['id']} deviates from the design ({run.get('deviation')}): not evidence")
            if run["task_id"] != task_id:
                raise RLError("INVALID", f"{run['id']} belongs to {run['task_id']}, not {task_id}")
        evid = tx.next_id("Evidence")
        tx.create("Evidence", {"id": evid, "summary": summary, "validity": "valid", "evidence_level": level,
                               "scope": scope, "artifact_refs": list(artifacts) or None, "task_id": task_id})
        if prep:
            tx.update(evid, {"doc_ref": documents.register(tx, prep, evid)})
        for att_id in attempts:
            tx.link("from_attempt", evid, att_id)
        for res_id in results:
            tx.link("from_result", evid, res_id)
        if exp:
            tx.link("about", evid, exp["id"])
        tx.event("evidence.added", evid, f"{level}: {summary}")
    return {"id": evid, "summary": f"{evid} {level}: {summary}"}


def set_evidence(store: Store, actor: Actor, evid: str, validity: str, reason: str) -> dict:
    """Retraction marks what rests on the evidence for re-checking."""
    need(actor, "researcher", "scientist", what="changing evidence validity")
    with store.write() as tx:
        ev = tx.require(evid, "Evidence")
        tx.update(evid, {"validity": validity, "validity_reason": reason})
        flagged = recheck_dependents(tx, evid, f"{evid} now {validity}: {reason}") if validity != ev["validity"] else []
        tx.event("evidence.validity", evid, f"{validity}: {reason}")
    return {"id": evid, "summary": f"{evid} -> {validity}" + (f"; re-check {', '.join(flagged)}" if flagged else "")}


def recheck_dependents(tx: Tx, source: str, reason: str) -> list[str]:
    """Mark what rests on a record that no longer holds: hypotheses an evidence supports or challenges, decisions
    based on it; for a result, also what rests on the evidence drawn from it."""
    sources = [source] + ([e["id"] for e in tx.inc(source, "from_result")] if label_of(source) == "Result" else [])
    flagged = []
    for sid in sources:
        rests = [e["id"] for e in tx.out(sid) if e["rel"] in ("supports", "challenges")]
        for rid in rests + [e["id"] for e in tx.inc(sid, "based_on")]:
            if rid not in flagged:
                tx.update(rid, {"needs_recheck": True, "recheck_reason": reason})
                flagged.append(rid)
    return flagged


def task_experiment(tx: Tx, task_id: str) -> dict | None:
    found = [e for e in tx.out(task_id, "targets") if e["label"] == "Experiment"]
    return tx.require(found[0]["id"]) if found else None


DESIGN_FIELDS = ("id", "revision", "edit_seq", "question", "manifest_path", "manifest", "manifest_digest",
                 "design_commit", "semantic_tests", "protected", "brief_ref", "label")


def task_design(tx: Tx, task_id: str) -> dict | None:
    """The experiment as a dispatched task was given it: the revision sealed at its dispatch, kept on the task, so
    a later revision of the experiment does not change what its runs, results and packs are checked against.
    Before dispatch, the experiment as it stands (a draft)."""
    task = tx.require(task_id, "Task")
    return task.get("sealed_design") or task_experiment(tx, task_id)


KEY_FIELDS = ("id", "revision", "edit_seq", "question", "manifest_path", "semantic_tests", "protected", "brief_ref")


def design_key(exp: dict | None) -> str | None:
    """What a review pass and a test record clear: the experiment revision and its contents (E-x@n#digest), so a
    new revision, or a draft edited after its review (another manifest in the same commit), needs its own (9.1).
    Sealing adds the design commit and the parsed manifest, which the commit and the path already fix."""
    if not exp:
        return None
    from .util import digest_json
    # a field a record has never had (edit_seq on one from before it existed) is left out, so the key such a record
    # was cleared under stays the same
    fields = {k: exp.get(k) for k in KEY_FIELDS if not (k == "edit_seq" and exp.get(k) is None)}
    return f"{exp['id']}@{exp['revision']}#{digest_json(fields)}"


def design_keys(exp: dict | None) -> set[str]:
    """The keys a clearance of this experiment may carry: its key, and for one never edited also the key the version
    of 298955e wrote (it digested an explicit null edit_seq), so a clearance made under either stays valid. Once
    edited, only the exact key."""
    if not exp:
        return set()
    from .util import digest_json
    keys = {design_key(exp)}
    if exp.get("edit_seq") is None:
        keys.add(f"{exp['id']}@{exp['revision']}#{digest_json({k: exp.get(k) for k in KEY_FIELDS})}")
    return keys


def design_id(tx: Tx, task_id: str) -> str | None:
    """The key of the experiment as it stands now for a task (design_key)."""
    return design_key(task_experiment(tx, task_id))


def sealed_copy(exp: dict) -> dict:
    return {k: exp.get(k) for k in DESIGN_FIELDS}


# -- decisions ------------------------------------------------------------------------------------------------


def decide(store: Store, actor: Actor, kind: str, targets: list[str], rationale: str, based_on: list[str] = (),
           status: str | None = None, supports: list[str] = (), challenges: list[str] = (), did: str | None = None,
           means: str | None = None, highlights: list[dict] = (), rationale_file: str | None = None) -> dict:
    """A decision by a scientist. verdict and stop are drafts until a conclusion review passes (rl decide --final);
    route and phase decisions are final now and change no hypothesis."""
    need(actor, "scientist", what="writing a decision")
    if kind not in ("verdict", "stop", "route", "phase"):
        raise RLError("INVALID", "--kind is verdict, stop, route or phase")
    if not targets:
        raise RLError("INVALID", "a decision targets a hypothesis, an experiment or a phase (--targets)")
    if kind == "verdict" and status not in VERDICTS:
        raise RLError("INVALID", f"a verdict sets --status {'|'.join(VERDICTS)}")
    if kind == "stop" and status not in STOPS:
        raise RLError("INVALID", f"a stop sets --status {'|'.join(STOPS)} (the hypothesis is not judged)")
    if kind in ("route", "phase") and status:
        raise RLError("INVALID", f"a {kind} decision changes no hypothesis state")
    if kind == "verdict" and not (did and means):
        raise RLError("INVALID", "a verdict carries the interpretation in two parts, --did (what was done) and "
                                 "--means (what it means), three sentences at most together, no copied numbers")
    if supports or challenges:
        if kind != "verdict":
            raise RLError("INVALID", "--supports/--challenges are written with a verdict")
    prep = documents.prepare(store.project, rationale_file, "rationale") if rationale_file else None
    with store.write() as tx:
        recs = [tx.require(t, "Hypothesis", "Experiment", "Phase") for t in targets]
        if kind in ("verdict", "stop") and any(r["_label"] != "Hypothesis" for r in recs):
            raise RLError("INVALID", f"a {kind} decides hypotheses only")
        if kind == "phase" and (len(recs) != 1 or recs[0]["_label"] != "Phase"):
            raise RLError("INVALID", "a phase conclusion decides exactly one phase")
        refs = list(based_on) + list(supports) + list(challenges)
        for ref in refs:
            tx.require(ref)
        for ev in list(supports) + list(challenges):
            if tx.require(ev)["_label"] != "Evidence":
                raise RLError("INVALID", f"{ev} is not evidence")
        if kind == "phase":
            phase_of = {e["id"] for e in tx.inc(recs[0]["id"], "in_phase")}
            for h in highlights:
                d = tx.require(h.get("decision", ""), "Decision")
                if d.get("task_id") not in phase_of:
                    raise RLError("INVALID", f"highlight {d['id']} is not a decision of {recs[0]['id']}'s hypothesis "
                                             "tasks")
        dec = tx.next_id("Decision")
        final = kind in ("route", "phase")
        tx.create("Decision", {"id": dec, "kind": kind, "state": "final" if final else "draft",
                               "rationale": rationale, "evidence_status_set": status if kind == "verdict" else None,
                               "disposition_set": status if kind == "stop" else None, "did": did, "means": means,
                               "highlights": list(highlights) or None, "task_id": actor.subject,
                               "supports": list(supports) or None, "challenges": list(challenges) or None,
                               "author": actor.name, "final_at": now_utc() if final else None})
        if prep:
            tx.update(dec, {"rationale_ref": documents.register(tx, prep, dec)})
        for r in recs:
            tx.link("decides", dec, r["id"], revision=r.get("revision"))
        for ref in dict.fromkeys(refs):
            tx.link("based_on", dec, ref)
        tx.event("decision.draft" if not final else "decision", dec, f"{kind} {', '.join(targets)}: {rationale}")
    return {"id": dec, "summary": f"{dec} {kind} {'final' if final else 'draft'} on {', '.join(targets)}"
                                  + ("" if final else f"; next: rl review request {dec} --kind conclusion")}


def finalize(store: Store, actor: Actor, dec: str) -> dict:
    """A draft verdict or stop becomes final: its conclusion review passed, and it rests on what the rules ask
    (9.1). The hypothesis state changes now; evidence directions are written; dependents learn through the rules
    table that an upstream conclusion changed (4.7)."""
    need(actor, "scientist", what="finalizing a decision")
    from .reviews import conclusion_passed
    with store.write() as tx:
        d = tx.require(dec, "Decision")
        if d["state"] == "final":
            return {"id": dec, "summary": f"{dec} is already final"}
        review = conclusion_passed(tx, dec)
        if not review:
            raise RLError("INVALID", f"{dec} has no passing conclusion review: rl review request {dec} --kind "
                                     "conclusion; it becomes final once that review passes")
        edges = [e for e in tx.out(dec, "decides") if e["label"] == "Hypothesis"]
        hyps = [tx.require(e["id"]) for e in edges]
        moved = [f"{e['id']} is at revision {h['revision']}, the reviewed draft decides revision {e.get('revision')}"
                 for e, h in zip(edges, hyps) if e.get("revision") is not None and e["revision"] != h["revision"]]
        if moved:
            raise RLError("INVALID", f"{dec} was reviewed for another statement: {'; '.join(moved)}. Write a new "
                                     "draft for the revised hypothesis (rl decide ..) and have it reviewed")
        cited = [tx.require(e["id"]) for e in tx.out(dec, "based_on")]
        supports, challenges = d.get("supports") or [], d.get("challenges") or []
        if d["kind"] == "verdict":
            composite = all(h.get("composite") for h in hyps)
            if composite:
                kids = {c["id"] for h in hyps for c in children(tx, h["id"])}
                if not any(c["_label"] == "Decision" and any(e["id"] in kids for e in tx.out(c["id"], "decides"))
                           for c in cited):
                    raise RLError("INVALID", f"{dec} decides a composite hypothesis: cite its children's decisions "
                                             "(--based-on D-x)")
            elif not (supports or challenges or any(c["_label"] == "Evidence" for c in cited)):
                raise RLError("INVALID", f"{dec} cites no evidence: a verdict rests on evidence (--based-on EV-x, "
                                         "--supports/--challenges EV-x)")
        else:  # stop
            if not any(c["_label"] == "Decision" or (c["_label"] == "Task" and _answered(tx, c["id"]))
                       for c in cited):
                raise RLError("INVALID", f"{dec} stops a hypothesis: cite the upstream decision that changed "
                                         "(--based-on D-x) or the task whose decision request was answered stop "
                                         "(--based-on T-x)")
        for h in hyps:
            changes = {"evidence_status": d["evidence_status_set"]} if d["kind"] == "verdict" else \
                {"disposition": d["disposition_set"]}
            if d["kind"] == "verdict" and h["disposition"] == "candidate":
                changes["disposition"] = "active"
            tx.update(h["id"], changes)
            for ev in supports:
                tx.link("supports", ev, h["id"], revision=h["revision"])
            for ev in challenges:
                tx.link("challenges", ev, h["id"], revision=h["revision"])
        if review["id"] not in [c["id"] for c in cited]:
            tx.link("based_on", dec, review["id"])
        tx.update(dec, {"state": "final", "final_at": now_utc()})
        tx.event("decision", dec, f"{d['kind']} final: {', '.join(h['id'] for h in hyps)} "
                                  f"{d.get('evidence_status_set') or d.get('disposition_set')}")
    return {"id": dec, "summary": f"{dec} final; {', '.join(h['id'] for h in hyps)} -> "
                                  f"{d.get('evidence_status_set') or d.get('disposition_set')}"}


def _answered(tx: Tx, task_id: str) -> bool:
    return any(a["state"] == "answered" for a in tx.all("Ask", task_id=task_id))


def children(tx: Tx, hid: str) -> list[dict]:
    return [tx.require(e["id"]) for e in tx.inc(hid, "derived_from") if e.get("primary")]


def parent_of(tx: Tx, hid: str) -> str | None:
    found = [e["id"] for e in tx.out(hid, "derived_from") if e.get("primary")]
    return found[0] if found else None


def link(store: Store, source: str, rel: str, target: str, note: str | None = None, locator: str | None = None,
         assessment: str | None = None) -> dict:
    if rel not in LINKABLE:
        raise RLError("INVALID", f"relation must be one of {', '.join(LINKABLE)}")
    with store.write() as tx:
        source_rec, target_rec = tx.require(source), tx.require(target)
        props = {"note": note, "locator": locator, "assessment": assessment}
        if RELATIONS[rel].revision_bound:
            props["revision"] = target_rec.get("revision")
            props["source_revision"] = source_rec.get("revision") if target_rec["_label"] == "Paper" else None
        tx.link(rel, source, target, **props)
        tx.event("link", source, f"{source} {rel} {target}")
    return {"summary": f"{source} {rel} {target}"}


# -- reading --------------------------------------------------------------------------------------------------


def brief(rec: dict) -> str:
    label = rec["_label"]
    if label == "Hypothesis":
        flag = " FLAGGED" if rec.get("flag") and not rec["flag"].get("settled") else ""
        return (f"{labels.name(rec)} [{rec['disposition']}/{rec['evidence_status']}{flag}] {rec['title']}")
    if label == "Experiment":
        sealed = f"sealed {rec['design_commit'][:10]}" if rec.get("design_commit") else "draft"
        return f"{labels.name(rec)}@{rec['revision']} [{sealed}] {rec['title']}"
    if label == "Evidence":
        return f"{rec['id']} [{rec['evidence_level']}/{rec['validity']}] {rec['summary']}"
    if label == "Decision":
        status = rec.get("evidence_status_set") or rec.get("disposition_set")
        return f"{rec['id']} [{rec['kind']}/{rec['state']}{' -> ' + status if status else ''}] {rec['rationale'][:160]}"
    if label == "Task":
        holder = f" held by {rec['holder']}" if rec.get("holder") else ""
        return f"{rec['id']} [{rec['kind']}/{rec['state']}{holder}] {rec['title']}"
    if label == "Paper":
        ident = rec.get("arxiv_id") or rec.get("doi") or "no-id"
        return f"{rec['id']} [{ident}, {rec['metadata_status']}] {rec['title']}"
    if label == "Ticket":
        what = rec.get("review_kind") or rec.get("advice_kind")
        return f"{rec['id']} [{rec['kind']}:{what}/{rec['status']}] {rec.get('task_id') or rec.get('target_id') or ''}"
    if label == "Attempt":
        return f"{rec['id']} [{rec['execution_state']}/{rec['outcome']}/result:{rec['result_status']}]"
    if label == "Phase":
        return f"{rec['id']} [phase] {rec['title']}"
    if label == "Ask":
        return f"{rec['id']} [{rec['kind']}/{rec['state']} on {rec['task_id']}] {rec['stuck'][:160]}"
    return f"{rec['id']} [{label}]"


def show(store: Store, entity_id: str) -> dict:
    with store.read() as tx:
        label = label_of(entity_id.split("#")[0])
        if label in ("Document", "DocRevision"):
            rev = documents.resolve(tx, entity_id)
            text = documents.read(store.project, rev)
            return {"record": rev, "summary": f"{rev['id']} {rev['source_path']}\n\n{text}"}
        rec = tx.require(entity_id.split("#")[0])
        out, inc = tx.out(rec["id"]), tx.inc(rec["id"])
        lines = [brief(rec)] + ([f"  id: {rec['id']}"] if rec.get("label") else [])
        for key, value in rec.items():
            if key not in ("id", "_label", "title", "label") and value not in (None, "", []):
                lines.append(f"  {key}: {value}")
        for e in out:
            lines.append(f"  -> {e['rel']} {e['id']}" + (" (primary)" if e.get("primary") else ""))
        for e in inc:
            lines.append(f"  <- {e['rel']} {e['id']}")
        for m in tx.all("Message", subject=rec["id"]):
            lines.append(f"  {m['id']} {m['kind']} {m['from_role']} -> {m['to_role']}: {m['text'][:300]}")
    return {"record": rec, "out": out, "in": inc, "summary": "\n".join(lines)}


def history(store: Store, entity_id: str | None = None, limit: int = 30) -> dict:
    with store.read() as tx:
        events = tx.all("Event", entity_id=entity_id) if entity_id else tx.all("Event")
    events = events[-limit:]
    return {"events": events, "summary": "\n".join(f"{e['created_at']} {e['type']} {e.get('entity_id') or ''} "
                                                   f"{e.get('summary') or ''}".rstrip() for e in events)}


def tree_lines(tx: Tx, mark: dict[str, str] | None = None) -> list[str]:
    """The hypothesis tree, one line each, indented by the primary parent edges (mark: id -> a note at the end)."""
    hyps = {h["id"]: h for h in tx.all("Hypothesis")}
    parent = {hid: parent_of(tx, hid) for hid in hyps}
    kids: dict[str | None, list[str]] = {}
    for hid, p in parent.items():
        kids.setdefault(p, []).append(hid)
    out: list[str] = []

    def walk(node: str | None, depth: int) -> None:
        for hid in sorted(kids.get(node, []), key=lambda h: _sort_key(hyps[h]["label"])):
            h = hyps[hid]
            status = h["evidence_status"] if h["evidence_status"] != "untested" else h["disposition"]
            m = (mark or {}).get(hid)
            out.append(f"{'  ' * depth}{h['label']} {hid} [{status}] {h['title']}" + (f"  <- {m}" if m else ""))
            walk(hid, depth + 1)
    walk(None, 0)
    return out


def commit_user_docs(store: Store, actor: Actor, paths: list[str], message: str) -> dict:
    """The coordinator's explanations and glossary (docs/explanations/, docs/glossary.md), committed to the mainline
    by the CLI (7.7)."""
    need(actor, "coordinator", what="committing explanations")
    for p in paths:
        rel = store.project.rel(store.project.abs(p))
        if not (rel == "docs/glossary.md" or (rel.startswith("docs/explanations/") and rel.endswith(".md"))):
            raise RLError("INVALID", f"{p}: only docs/glossary.md and docs/explanations/*.md are committed this way")
        if not store.project.abs(p).is_file():
            raise RLError("NOT_FOUND", f"{p} does not exist")
    commit = gitops.commit_paths(store.project, paths, message)
    with store.write() as tx:
        tx.event("docs.committed", None, f"{', '.join(paths)} at {commit[:12]}: {message}")
    return {"commit": commit, "summary": f"committed {', '.join(paths)} on the mainline ({commit[:12]})"}
