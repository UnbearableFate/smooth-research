"""Project setup and the scientific graph: hypotheses, experiments, evidence, decisions and relations (design ch. 10).

Decisions are the only way a hypothesis changes state. A verdict (supported, challenged, inconclusive) or a stop
(paused, retired) starts as a draft and becomes final with `rl decide --final`, normally after a passing conclusion
review of that draft (reviews.py); a route decision (why these hypotheses, why a new experiment, how an answer is
followed) and a phase conclusion are final at once and change no hypothesis.

What is refused here is only what cannot be recorded or would be recorded falsely (design 2026-11-27, 5.1):
unknown or mistyped ids, missing defining parameters, files that do not exist, a decided hypothesis's statement, a
verdict reviewed for another revision of its hypothesis. The rest of the process (who writes what, whether a
conclusion was reviewed, what evidence a verdict cites, which runs evidence cites) is followed with a flag on the
record (flags.py).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import documents, gitops, labels
from .flags import Flags
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
    write(project.docs_root / ".gitignore", "# drawn by rl view (and the optional observer, rl watch), not tracked\nresearch.html\n")
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
                                      "direction comes as another document: rl tree prompt --from <document>")
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
                    + ["rl resume: opens the coordinator's pane in herdr (and the optional observer)"]}


def _flags(actor: Actor | None, *roles: str, what: str) -> Flags:
    """The flags one command collects, with a role flag when the caller is not of the roles the process names; only a
    command without any session identity is refused (roles.need)."""
    fl = Flags(actor.name if actor else "?")
    need(actor, *roles, what=what, flags=fl)
    return fl


def _result(rid: str | None, summary: str, fl: Flags | None = None, **more) -> dict:
    out = {"id": rid, "summary": summary, **{k: v for k, v in more.items() if v}}
    if fl:
        out["flags"] = fl.items
    return out


def _task_of(tx: Tx, actor: Actor) -> dict | None:
    """The task the calling session is on (scientist-T-x: T-x), if it is one."""
    if not actor.subject:
        return None
    try:
        return tx.get(actor.subject, "Task") if label_of(actor.subject) == "Task" else None
    except RLError:
        return None


# -- hypotheses -----------------------------------------------------------------------------------------------


def add_hypothesis(store: Store, actor: Actor, title: str, statement: str, purpose: str, scope: str | None = None,
                   source: str | None = None, parent: str | None = None, assumes: list[str] = (),
                   cost: str | None = None, candidate: bool = False, motivated_by: list[str] = ()) -> dict:
    """One hypothesis: the coordinator adds one (the initial tree comes through rl tree record, 7.2), or a scientist
    proposes one a verdict raised (a candidate the coordinator schedules, 7.3). No task is needed. The primary
    parent edge places it in the tree; ASSUMES edges name its premises (4.7). Composite hypotheses are no longer
    made (the field stays for older records)."""
    fl = _flags(actor, "coordinator", "scientist", what="adding a hypothesis")
    if not (purpose or "").strip():
        raise RLError("INVALID", "a hypothesis needs --purpose: one sentence on what testing it answers (the "
                                 "research page shows it)")
    with store.write() as tx:
        task = _task_of(tx, actor)
        if parent:
            tx.require(parent, "Hypothesis")
        for ref in list(assumes):
            tx.require(ref, "Hypothesis")
        for ref in list(motivated_by):
            tx.require(ref, "Paper", "Decision")
        label = labels.for_hypothesis(tx, [parent] if parent else [], parent)
        hid = tx.next_id("Hypothesis")
        # the coordinator's (and an old tree task's) hypotheses are to be tested; a scientist's is a proposal
        active = not candidate and (actor.role == "coordinator" or (task or {}).get("kind") == "tree")
        tx.create("Hypothesis", {"id": hid, "label": label, "title": title, "statement": statement, "scope": scope,
                                 "source": source, "purpose": purpose.strip(), "evidence_status": "untested",
                                 "disposition": "active" if active else "candidate", "revision": 1, "cost": cost,
                                 "origin": task["id"] if task else None})
        if parent:
            tx.link("derived_from", hid, parent, primary=True)
        for premise in assumes:
            tx.link("assumes", hid, premise)
        for m in motivated_by:
            tx.link("motivated_by", hid, m)
        fl.write(tx, hid)
        tx.event("hypothesis.added", hid, f"{label}: {title}")
    return _result(hid, f"{label} ({hid}) {'active' if active else 'candidate'}: {title}", fl, label=label)


def revise_hypothesis(store: Store, actor: Actor, hid: str, reason: str, statement: str | None = None,
                      scope: str | None = None, title: str | None = None, purpose: str | None = None,
                      source: str | None = None, cost: str | None = None) -> dict:
    """A hypothesis not yet decided changes its statement or scope as a new revision; title, purpose, source and cost
    change in place. A decided one keeps its statement: what was judged stays recorded (5.1)."""
    fl = _flags(actor, "scientist", "coordinator", what="revising a hypothesis")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        changes = {k: v for k, v in (("title", title), ("purpose", purpose), ("source", source), ("cost", cost))
                   if v is not None}
        scientific = {k: v for k, v in (("statement", statement), ("scope", scope)) if v is not None and v != h.get(k)}
        if scientific:
            if h["evidence_status"] != "untested":
                raise RLError("INVALID", f"{hid} is {h['evidence_status']}: a decided hypothesis keeps its statement; "
                                         "propose a new hypothesis under it instead (rl hypothesis add --parent "
                                         f"{hid} ..)")
            changes.update(scientific, revision=h["revision"] + 1)
        if not changes:
            return _result(hid, f"{hid} unchanged")
        tx.update(hid, changes)
        fl.write(tx, hid)
        tx.event("hypothesis.revised", hid, reason)
    return _result(hid, f"{hid} revised: {reason}", fl)


def flag_hypothesis(store: Store, actor: Actor, hid: str, lack: str, basis: str, options: str) -> dict:
    """A hypothesis whose novelty or feasibility falls short (its `flag`, a doubt for the user, not a process flag);
    it stays a candidate until the user decides (4.8)."""
    fl = _flags(actor, "scientist", "coordinator", what="flagging a hypothesis")
    if lack not in ("novelty", "feasibility"):
        raise RLError("INVALID", "--lack is novelty or feasibility")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        tx.update(hid, {"flag": {"lack": lack, "basis": basis, "options": options, "by": actor.name},
                        "disposition": "candidate" if h["disposition"] == "active" else h["disposition"]})
        fl.write(tx, hid)
        tx.event("hypothesis.flagged", hid, f"{lack}: {basis}")
    return _result(hid, f"{hid} flagged ({lack}); it stays a candidate until the user decides", fl)


def settle_flag(store: Store, actor: Actor, hid: str, outcome: str) -> dict:
    """The coordinator records the user's decision on a flagged hypothesis (keep it active, or leave it out)."""
    fl = _flags(actor, "coordinator", what="settling a flagged hypothesis")
    if outcome not in ("keep", "drop"):
        raise RLError("INVALID", "the outcome is keep or drop; narrowing or another test is a revision of the "
                                 "hypothesis (rl hypothesis revise)")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        if not h.get("flag"):
            raise RLError("INVALID", f"{hid} has no novelty or feasibility doubt to settle")
        tx.update(hid, {"flag": {**h["flag"], "settled": outcome, "settled_at": now_utc()},
                        "disposition": "active" if outcome == "keep" else "retired"})
        fl.write(tx, hid)
        tx.event("hypothesis.flag_settled", hid, outcome)
    return _result(hid, f"{hid}: doubt settled ({outcome})", fl)


def defer_hypothesis(store: Store, actor: Actor, hid: str, reason: str | None) -> dict:
    """The coordinator marks a candidate 'not now' (or clears it with reason None); scheduling only (7.1)."""
    fl = _flags(actor, "coordinator", what="deferring a hypothesis")
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        if h["evidence_status"] != "untested":
            raise RLError("INVALID", f"{hid} is already {h['evidence_status']}: nothing is left to defer")
        tx.update(hid, {"defer_reason": reason})
        fl.write(tx, hid)
        tx.event("hypothesis.deferred" if reason else "hypothesis.undeferred", hid, reason or "")
    return _result(hid, f"{hid} {'not now: ' + reason if reason else 'back among the candidates'}", fl)


# -- experiments ----------------------------------------------------------------------------------------------


def add_experiment(store: Store, actor: Actor, title: str, question: str, hypotheses: list[str],
                   motivated_by: str | None = None, manifest_path: str | None = None, protected: list[str] = (),
                   semantic_tests: str | None = None, brief: str | None = None, estimate: str | None = None) -> dict:
    """A new experiment, a draft until a task carrying it is dispatched (the design commit is sealed then). The test
    plan is the scientist's own (7.3): --motivated-by is optional, and when given links the decision (usually a route
    decision) or paper behind it."""
    fl = _flags(actor, "scientist", what="adding an experiment")
    if not hypotheses:
        raise RLError("INVALID", "an experiment tests at least one hypothesis (--hypotheses H-0001)")
    prep = documents.prepare(store.project, brief, "brief") if brief else None
    with store.write() as tx:
        if motivated_by:
            tx.require(motivated_by, "Decision", "Paper")
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
        if motivated_by:
            tx.link("motivated_by", eid, motivated_by)
        fl.write(tx, eid)
        tx.event("experiment.added", eid, f"{label}: {title}")
    return _result(eid, f"{label} ({eid}) draft: {title}", fl, label=label)


def update_experiment(store: Store, actor: Actor, eid: str, reason: str | None = None, **fields) -> dict:
    """Change an experiment's design fields. A draft (not sealed) changes freely; a sealed one becomes a new
    revision with a reason, and the next dispatch seals it again (4.6)."""
    fl = _flags(actor, "scientist", what="changing an experiment")
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
        fl.write(tx, eid)
    return _result(eid, f"{eid} updated" + (f" (new revision: {reason})" if reason else ""), fl)


def first_evidence_run(tx: Tx, eid: str) -> str | None:
    runs = [r for r in tx.all("Run", experiment_id=eid) if r["category"] == "evidence"]
    return runs[0]["id"] if runs else None


# -- evidence -------------------------------------------------------------------------------------------------


def add_evidence(store: Store, actor: Actor, task_id: str, summary: str, level: str, results: list[str] = (),
                 attempts: list[str] = (), scope: str | None = None, artifacts: list[str] = (),
                 doc: str | None = None) -> dict:
    """A fact drawn from results, without direction (10.2 rule 4). Evidence counts when it cites evidence runs of its
    own execute task that do not deviate from the design and whose results check out (9.1); anything else is
    recorded as said, flagged not-evidence, for the reviewer and the verdict to weigh (5.2)."""
    fl = _flags(actor, "researcher", what="writing evidence")
    if not results and not attempts:
        raise RLError("INVALID", "evidence is drawn from results or attempts: --results RES-x or --attempts ATT-x")
    prep = documents.prepare(store.project, doc, "analysis") if doc else None
    with store.write() as tx:
        task = tx.require(task_id, "Task")
        if task["kind"] != "execute":
            fl.add("not-evidence", f"drawn from {task['kind']} task {task_id}, not an execute task (a preparation "
                                   "task's outputs are not evidence)")
        exp = task_experiment(tx, task_id)
        runs = []
        for res_id in results:
            res = tx.require(res_id, "Result")
            if not res.get("usable", True):
                why = (f"differs from its run's registration ({res['mismatches']})" if res.get("mismatches") else
                       f"is {res['status']}" if res["status"] == "rejected" else
                       f"cannot be checked against its run ({res.get('verification_scope')})")
                fl.add("not-evidence", f"cites {res_id}, which {why}")
            runs.append(tx.require(tx.require(res["attempt_id"])["run_id"]))
        runs += [tx.require(tx.require(a, "Attempt")["run_id"]) for a in attempts]
        for run in {r["id"]: r for r in runs}.values():
            if run["category"] != "evidence":
                fl.add("not-evidence", f"cites {run['id']}, a {run['category']} run (only evidence runs count)")
            if run.get("deviates"):
                fl.add("not-evidence", f"cites {run['id']}, which deviates from the design ({run.get('deviation')})")
            if run["task_id"] != task_id:
                fl.add("not-evidence", f"cites {run['id']} of {run['task_id']}, not of {task_id}")
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
        fl.write(tx, evid)
        tx.event("evidence.added", evid, f"{level}: {summary}")
    return _result(evid, f"{evid} {level}: {summary}", fl)


def set_evidence(store: Store, actor: Actor, evid: str, validity: str, reason: str) -> dict:
    """Retraction marks what rests on the evidence for re-checking."""
    fl = _flags(actor, "researcher", "scientist", what="changing evidence validity")
    with store.write() as tx:
        ev = tx.require(evid, "Evidence")
        tx.update(evid, {"validity": validity, "validity_reason": reason})
        flagged = recheck_dependents(tx, evid, f"{evid} now {validity}: {reason}") if validity != ev["validity"] else []
        fl.write(tx, evid)
        tx.event("evidence.validity", evid, f"{validity}: {reason}")
    return _result(evid, f"{evid} -> {validity}" + (f"; re-check {', '.join(flagged)}" if flagged else ""), fl)


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


def design_id(tx: Tx, task_id: str) -> str | None:
    """The key of the experiment as it stands now for a task (design_key)."""
    return design_key(task_experiment(tx, task_id))


def sealed_copy(exp: dict) -> dict:
    return {k: exp.get(k) for k in DESIGN_FIELDS}


# -- decisions ------------------------------------------------------------------------------------------------


def decide(store: Store, actor: Actor, kind: str, targets: list[str], rationale: str, based_on: list[str] = (),
           status: str | None = None, supports: list[str] = (), challenges: list[str] = (), did: str | None = None,
           means: str | None = None, highlights: list[dict] = (), rationale_file: str | None = None) -> dict:
    """A decision by a scientist (a route decision also by the coordinator). verdict and stop are drafts until rl
    decide --final, normally after a passing conclusion review; route and phase decisions are final now and change no
    hypothesis."""
    fl = _flags(actor, *(("scientist", "coordinator") if kind == "route" else ("scientist",)),
                what=f"writing a {kind} decision")
    warnings = []
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
                    warnings.append(f"highlight {d['id']} is not a decision of {recs[0]['id']}'s hypothesis tasks; "
                                    "recorded as given")
        task = _task_of(tx, actor)
        dec = tx.next_id("Decision")
        final = kind in ("route", "phase")
        tx.create("Decision", {"id": dec, "kind": kind, "state": "final" if final else "draft",
                               "rationale": rationale, "evidence_status_set": status if kind == "verdict" else None,
                               "disposition_set": status if kind == "stop" else None, "did": did, "means": means,
                               "highlights": list(highlights) or None, "task_id": task["id"] if task else None,
                               "supports": list(supports) or None, "challenges": list(challenges) or None,
                               "author": actor.name, "final_at": now_utc() if final else None})
        if prep:
            tx.update(dec, {"rationale_ref": documents.register(tx, prep, dec)})
        for r in recs:
            tx.link("decides", dec, r["id"], revision=r.get("revision"))
        for ref in dict.fromkeys(refs):
            tx.link("based_on", dec, ref)
        fl.write(tx, dec)
        tx.event("decision.draft" if not final else "decision", dec, f"{kind} {', '.join(targets)}: {rationale}")
    return _result(dec, f"{dec} {kind} {'final' if final else 'draft'} on {', '.join(targets)}", fl,
                   warnings=warnings, next=[] if final else [
                       f"rl review request {dec} --kind conclusion; when it passes, rl decide --final {dec}"])


def finalize(store: Store, actor: Actor, dec: str) -> dict:
    """A draft verdict or stop becomes final. The hypothesis state changes now; evidence directions are written;
    dependents learn through the rules table that an upstream conclusion changed (4.7). Without a passing conclusion
    review, a verdict without evidence, or a stop without the decision or answer behind it, it is final all the same,
    flagged (unreviewed, no-evidence, no-basis: 5.2). A draft reviewed for another revision of its hypothesis is not
    finalized: the verdict would be recorded on a statement nobody judged."""
    fl = _flags(actor, "scientist", what="finalizing a decision")
    from .reviews import conclusion_flags, conclusion_passed
    with store.write() as tx:
        d = tx.require(dec, "Decision")
        if d["state"] == "final":
            return _result(dec, f"{dec} is already final")
        edges = [e for e in tx.out(dec, "decides") if e["label"] == "Hypothesis"]
        hyps = [tx.require(e["id"]) for e in edges]
        moved = [f"{e['id']} is at revision {h['revision']}, the draft decides revision {e.get('revision')}"
                 for e, h in zip(edges, hyps) if e.get("revision") is not None and e["revision"] != h["revision"]]
        if moved:
            raise RLError("INVALID", f"{dec} was written for another statement: {'; '.join(moved)}. Write a new "
                                     "draft for the revised hypothesis (rl decide ..)")
        review = conclusion_passed(tx, dec)
        for code, text in conclusion_flags(tx, dec):
            fl.add(code, f"finalized: {text}")
        cited = [tx.require(e["id"]) for e in tx.out(dec, "based_on")]
        supports, challenges = d.get("supports") or [], d.get("challenges") or []
        if d["kind"] == "verdict":
            if not (supports or challenges or any(c["_label"] == "Evidence" for c in cited)):
                fl.add("no-evidence", f"{dec} cites no evidence (--based-on EV-x, --supports/--challenges EV-x)")
        elif not any(c["_label"] == "Decision" or (c["_label"] == "Task" and _answered(tx, c["id"])) for c in cited):
            fl.add("no-basis", f"{dec} stops {', '.join(h['id'] for h in hyps)} citing neither the upstream decision "
                               "that changed (--based-on D-x) nor the task whose decision request was answered "
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
        if review and review["id"] not in [c["id"] for c in cited]:
            tx.link("based_on", dec, review["id"])
        tx.update(dec, {"state": "final", "final_at": now_utc()})
        fl.write(tx, dec)
        tx.event("decision", dec, f"{d['kind']} final: {', '.join(h['id'] for h in hyps)} "
                                  f"{d.get('evidence_status_set') or d.get('disposition_set')}")
    from .inbox import recipients
    with store.read() as tx:  # the holders of tasks on hypotheses that assume these (3.2: upstream changed)
        tell = recipients(tx, store.project.config, ("upstream-changed",))
    return _result(dec, f"{dec} final; {', '.join(h['id'] for h in hyps)} -> "
                        f"{d.get('evidence_status_set') or d.get('disposition_set')}", fl, wake=tell)


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
        kind = " competing" if rec.get("kind") == "competing" else ""
        return f"{labels.name(rec)} [{rec['disposition']}/{rec['evidence_status']}{kind}{flag}] {rec['title']}"
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
        return (f"{rec['id']} [{rec['kind']}/{rec['state']} on {rec.get('task_id') or rec.get('decision_id')}] "
                f"{rec['stuck'][:160]}")
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
    by the CLI (7.7). Only these paths: code reaches the mainline through a task and its acceptance."""
    fl = _flags(actor, "coordinator", what="committing explanations")
    for p in paths:
        rel = store.project.rel(store.project.abs(p))
        if not (rel == "docs/glossary.md" or (rel.startswith("docs/explanations/") and rel.endswith(".md"))):
            raise RLError("INVALID", f"{p}: only docs/glossary.md and docs/explanations/*.md are committed this way")
        if not store.project.abs(p).is_file():
            raise RLError("NOT_FOUND", f"{p} does not exist")
    commit = gitops.commit_paths(store.project, paths, message)
    with store.write() as tx:
        for f in fl.items:  # no record to carry it: the event log keeps it
            tx.event("flag", None, f"[{f['code']}] {f['text']}")
        tx.event("docs.committed", None, f"{', '.join(paths)} at {commit[:12]}: {message}")
    return _result(None, f"committed {', '.join(paths)} on the mainline ({commit[:12]})", fl, commit=commit)
