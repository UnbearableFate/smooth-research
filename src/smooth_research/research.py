"""Project setup and the scientific graph: hypotheses, experiments, evidence, decisions and relations."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from . import documents
from .project import (CONFIG_NAME, DOCS_DIRS, REPORTS_DIRS, RESEARCH_DIRS, Project, load_config,
                      write_default_config)
from .schema import ENUMS, LINKABLE, RELATIONS
from .store import Store, Tx, label_of
from .util import RLError, digest_file

CONSTRAINTS_TEMPLATE = """# Constraints

Who decided each item and when. The coordinator and executors follow this; the plugin does not enforce it.

- Research goal and claims in scope:
- Allowed data, models and environments:
- Resource ceiling (per experiment / per batch / total; units such as CPU-h, GPU-h, node-h):
- Model choices and substitutes (reviewer, advisor):
- Actions that need the user's decision first:
"""

ACTION_DISPOSITION = {"pause": "paused", "stop": "retired", "reopen": "active"}
STAGE_ACTIONS = ("recommend_formal", "continue_probe", "pause", "stop")
# Key conclusions get an independent, read-only check before anyone relies on them (rl review conclusion):
# a hypothesis set supported or challenged, a retired route, a recommended formal experiment.
KEY_STATUSES = ("supported", "challenged")


# -- project --------------------------------------------------------------------------------------------------


def init_project(root: Path, plan: str | None = None, title: str | None = None) -> dict:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_default_config(root)
    project = Project(root, load_config(root))
    project.code_root.mkdir(parents=True, exist_ok=True)
    for area, names in (("research", RESEARCH_DIRS), ("docs", DOCS_DIRS), ("reports", REPORTS_DIRS)):
        for name in names:
            project.dir(name, area)
    gitignore = project.research_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# the active database, call logs and raw run artifacts stay out of git;\n"
                             "# sealed documents are kept, and `rl export` writes consistent snapshots\n"
                             "state/\ncalls/\nartifacts/\n", encoding="utf-8")
    code_ignore = project.code_root / ".gitignore"
    if not code_ignore.exists():  # a commit is staged as the code snapshot of remote runs: sources only
        code_ignore.write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    constraints = project.abs(project.config["constraints_ref"])
    if not constraints.exists():
        constraints.parent.mkdir(parents=True, exist_ok=True)
        constraints.write_text(CONSTRAINTS_TEMPLATE, encoding="utf-8")
    created = not project.db_path.exists()
    if created:
        Store.create(project, title)
    store = Store(project, "coordinator")
    prep = None
    if plan:
        source = Path(plan).expanduser().resolve()
        if not source.is_file():
            raise RLError("NOT_FOUND", f"plan not found: {plan}")
        snapshot = project.abs(project.config["plan"])
        if snapshot.exists() and snapshot.resolve() != source and digest_file(snapshot) != digest_file(source):
            raise RLError("CONFLICT", f"{project.rel(snapshot)} already holds a different imported plan; the "
                                      "original snapshot is immutable. Record plan changes as hypothesis "
                                      "revisions and decisions instead.")
        if snapshot.resolve() != source:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, snapshot)
        prep = documents.prepare(project, snapshot, "plan")
    with store.write() as tx:
        plan_ref = documents.register(tx, prep) if prep else tx.meta().get("plan_ref")
        if prep:
            tx.set_meta(plan_ref=plan_ref, plan_source=str(Path(plan).expanduser().resolve()))
        tx.event("project.init" if created else "project.reinit", None, f"plan {plan_ref}")
    cfg = project.config
    return {"project": str(root), "created": created, "plan_ref": plan_ref, "config": str(root / CONFIG_NAME),
            "warnings": project.warnings,
            "summary": (f"{'initialized' if created else 'already initialized'} {root}"
                        + (f"; plan snapshot {plan_ref} ({cfg['plan']})" if plan_ref else "; no plan imported yet")
                        + f"\nlayout: experiment code {cfg['code_root']}/ · documents {cfg['docs_root']}/ · "
                          f"reports {cfg['reports_root']}/ · records {cfg['research_root']}/"),
            "next": [f"fill {cfg['constraints_ref']} with the user's resource scope and model choices",
                     "register yourself: rl session start --role coordinator ...",
                     "extract near-term hypotheses: rl hypothesis add ..."]}


# -- hypotheses -----------------------------------------------------------------------------------------------


def add_hypothesis(store: Store, title: str, statement: str, scope: str | None = None,
                   disposition: str = "active", derived_from: list[str] = (), combines: list[str] = (),
                   motivated_by: list[str] = (), origin: str | None = None, statement_file: str | None = None) -> dict:
    prep = documents.prepare(store.project, statement_file, "hypothesis") if statement_file else None
    with store.write() as tx:
        hid = tx.next_id("Hypothesis")
        tx.create("Hypothesis", {"id": hid, "title": title, "statement": statement, "scope": scope,
                                 "statement_ref": documents.register(tx, prep) if prep else None,
                                 "evidence_status": "untested", "disposition": disposition, "revision": 1,
                                 "origin": origin})
        for rel, targets in (("derived_from", derived_from), ("combines", combines), ("motivated_by", motivated_by)):
            for target in targets:
                tx.link(rel, hid, target)
        tx.event("hypothesis.added", hid, title)
    return {"id": hid, "revision": 1, "summary": f"{hid}@1 {disposition}: {title}"}


def revise_hypothesis(store: Store, hid: str, reason: str, statement: str | None = None, scope: str | None = None,
                      title: str | None = None, base_revision: int | None = None,
                      statement_file: str | None = None) -> dict:
    prep = documents.prepare(store.project, statement_file, "hypothesis") if statement_file else None
    with store.write() as tx:
        h = tx.require(hid, "Hypothesis")
        if base_revision is not None and base_revision != h["revision"]:
            raise RLError("STALE_REVISION", f"{hid} is at revision {h['revision']}, not {base_revision}",
                          current=_brief(h))
        changes: dict = {}
        if title:
            changes["title"] = title
        scientific = {k: v for k, v in (("statement", statement), ("scope", scope)) if v is not None and v != h.get(k)}
        if prep:
            scientific["statement_ref"] = documents.register(tx, prep)
        if scientific:
            changes.update(scientific, revision=h["revision"] + 1, evidence_status="untested",
                           needs_recheck=False, recheck_reason=None)
        if not changes:
            return {"id": hid, "revision": h["revision"], "summary": f"{hid}@{h['revision']} unchanged"}
        h2 = tx.update(hid, changes)
        old = [e for e in tx.inc(hid) if e["rel"] in ("supports", "challenges") and e.get("revision") != h2["revision"]]
        tx.event("hypothesis.revised", hid, f"rev {h2['revision']}: {reason}")
    note = (f"; {len(old)} evidence link(s) stay bound to earlier revisions — re-link after checking scope"
            if scientific and old else "")
    return {"id": hid, "revision": h2["revision"], "summary": f"{hid}@{h2['revision']} revised{note}"}


def set_hypothesis(store: Store, hid: str, reason: str, evidence_status: str | None = None,
                   disposition: str | None = None, rechecked: bool = False) -> dict:
    with store.write() as tx:
        tx.require(hid, "Hypothesis")
        changes = {k: v for k, v in (("evidence_status", evidence_status), ("disposition", disposition)) if v}
        if rechecked:
            changes.update(needs_recheck=False, recheck_reason=None)
        h = tx.update(hid, changes)
        tx.event("hypothesis.status", hid, f"{h['evidence_status']}/{h['disposition']}: {reason}")
    return {"id": hid, "summary": f"{hid}: evidence {h['evidence_status']}, disposition {h['disposition']}"}


# -- experiments ----------------------------------------------------------------------------------------------

CONTRACT_FIELDS = ("question", "intervention", "comparator", "metric", "scope", "outcome_actions", "budget", "stop")


def add_experiment(store: Store, title: str, question: str, hypotheses: list[str], stage: str | None = None,
                   compared_with: list[str] = (), code_from: str | None = None, depends_on: list[str] = (),
                   **contract: str | None) -> dict:
    if not hypotheses:
        raise RLError("INVALID", "an experiment tests at least one hypothesis (--hypotheses H-0001)")
    with store.write() as tx:
        eid = tx.next_id("Experiment")
        path = store.project.dir("experiments", "docs") / f"{eid}.md"
        refs = []
        for hid in hypotheses:
            h = tx.require(hid, "Hypothesis")
            refs.append(f"{hid}@{h['revision']}")
        tx.create("Experiment", {"id": eid, "title": title, "question": question, "stage": stage,
                                 "contract_path": store.project.rel(path), "revision": 0})
        for hid in hypotheses:
            tx.link("tests", eid, hid, revision=tx.require(hid)["revision"])
        for other in compared_with:
            tx.link("compared_with", eid, other)
        if code_from:
            tx.link("code_from", eid, code_from)
        for dep in depends_on:
            tx.link("depends_on", eid, dep)
        front = {"experiment": eid, "title": title, "stage": stage, "hypotheses": refs,
                 **{k: (question if k == "question" else contract.get(k)) or "TODO" for k in CONTRACT_FIELDS}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n"
                        "## Design\n\nArms, seeds, data split, positive/negative controls and what each outcome "
                        "changes. Edit freely until the first run, review or task references this contract; "
                        "later edits become a new sealed revision.\n", encoding="utf-8")
        tx.event("experiment.added", eid, title)
    todo = [k for k in CONTRACT_FIELDS if not (question if k == "question" else contract.get(k))]
    return {"id": eid, "contract": store.project.rel(path),
            "summary": f"{eid} draft contract {store.project.rel(path)}"
                       + (f"; fill in: {', '.join(todo)}" if todo else "")}


def prepare_contract(store: Store, eid: str) -> documents.Prepared:
    with store.read() as tx:
        exp = tx.require(eid, "Experiment")
    return documents.prepare(store.project, exp["contract_path"], "experiment_contract")


def seal_contract(tx: Tx, eid: str, prep: documents.Prepared, project: Project) -> str:
    """Seal the current contract; a changed draft becomes a new revision, flagged if results were already seen."""
    exp = tx.require(eid, "Experiment")
    current = exp.get("contract_ref")
    if current and tx.require(current, "DocRevision")["digest"] == prep.digest:
        return f"{eid}@{exp['revision']}"
    ref = documents.register(tx, prep)
    changes: dict = {"contract_ref": ref, "revision": exp["revision"] + 1}
    front = _front_matter(project.abs(prep.object_path))
    for key in ("title", "question"):
        if isinstance(front.get(key), str) and front[key].strip() and front[key] != "TODO":
            changes[key] = front[key].strip()
    if front.get("stage") in ENUMS["stage"]:
        changes["stage"] = front["stage"]
    seen = any(tx.all("Result", attempt_id=a["id"]) for run in tx.all("Run", experiment_id=eid)
               for a in tx.all("Attempt", run_id=run["id"]))
    if current and seen:
        changes["revised_after_results"] = True
        tx.event("experiment.revised_after_results", eid,
                 f"contract {ref} written after results were imported; not a pre-specified design")
    tx.update(eid, changes)
    tx.event("experiment.sealed", eid, ref)
    return f"{eid}@{changes['revision']}"


def seal_experiment(store: Store, eid: str) -> dict:
    prep = prepare_contract(store, eid)
    with store.write() as tx:
        ref = seal_contract(tx, eid, prep, store.project)
        exp = tx.require(eid)
    flag = " (revised after results: exploratory, not pre-specified)" if exp.get("revised_after_results") else ""
    return {"id": eid, "ref": ref, "contract_ref": exp["contract_ref"], "summary": f"{ref} sealed as {exp['contract_ref']}{flag}"}


def _front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except yaml.YAMLError:
        return {}


# -- evidence and decisions -----------------------------------------------------------------------------------


def add_evidence(store: Store, summary: str, level: str, validity: str = "pending", scope: str | None = None,
                 supports: list[str] = (), challenges: list[str] = (), attempts: list[str] = (),
                 results: list[str] = (), experiment: str | None = None, artifacts: list[str] = (),
                 doc: str | None = None) -> dict:
    prep = documents.prepare(store.project, doc, "evidence") if doc else None
    warnings = []
    with store.write() as tx:
        for res_id in results:
            res = tx.require(res_id, "Result")
            if not res.get("usable_for_planned_comparison", True):
                warnings.append(f"{res_id} does not match its run spec; it cannot serve the pre-specified comparison")
                if level == "confirmatory":
                    raise RLError("INVALID", f"{res_id} differs from its RunSpec; it cannot be confirmatory evidence")
        for att_id in attempts:
            att = tx.require(att_id, "Attempt")
            if att["outcome"] != "success":
                warnings.append(f"{att_id} outcome is {att['outcome']}: diagnose before treating this as a test of the mechanism")
        evid = tx.next_id("Evidence")
        tx.create("Evidence", {"id": evid, "summary": summary, "validity": validity, "evidence_level": level,
                               "scope": scope, "artifact_refs": list(artifacts) or None,
                               "doc_ref": documents.register(tx, prep) if prep else None})
        for rel, hids in (("supports", supports), ("challenges", challenges)):
            for hid in hids:
                tx.link(rel, evid, hid, revision=tx.require(hid, "Hypothesis")["revision"], scope=scope)
        for att_id in attempts:
            tx.link("from_attempt", evid, att_id)
        for res_id in results:
            tx.link("from_result", evid, res_id)
        if experiment:
            tx.link("about", evid, experiment)
        tx.event("evidence.added", evid, f"{level}/{validity}: {summary}")
    return {"id": evid, "warnings": warnings,
            "summary": f"{evid} {level}/{validity}" + (f" (supports {', '.join(supports)})" if supports else "")
                       + (f" (challenges {', '.join(challenges)})" if challenges else "")}


def set_evidence(store: Store, evid: str, validity: str, reason: str) -> dict:
    """Retraction marks only what actually depends on this evidence for re-checking."""
    with store.write() as tx:
        ev = tx.require(evid, "Evidence")
        tx.update(evid, {"validity": validity, "validity_reason": reason})
        flagged = []
        if validity != ev["validity"] and (validity == "invalid" or ev["validity"] == "valid"):
            for e in tx.out(evid):
                if e["rel"] in ("supports", "challenges"):
                    tx.update(e["id"], {"needs_recheck": True, "recheck_reason": f"{evid} now {validity}: {reason}"})
                    flagged.append(e["id"])
            for e in tx.inc(evid, "based_on"):
                tx.update(e["id"], {"needs_recheck": True, "recheck_reason": f"{evid} now {validity}: {reason}"})
                flagged.append(e["id"])
        tx.event("evidence.validity", evid, f"{validity}: {reason}")
    return {"id": evid, "flagged": flagged,
            "summary": f"{evid} -> {validity}" + (f"; re-check {', '.join(flagged)}" if flagged else "")}


def _is_key(kind: str, action: str, evidence_status: str | None) -> bool:
    return evidence_status in KEY_STATUSES or action == "stop" or (kind == "stage" and action == "recommend_formal")


def key_hypotheses(tx: Tx, d: dict) -> list[str]:
    """The hypotheses on which decision d states a key conclusion; empty when it states none."""
    if not _is_key(d["kind"], d["action"], d.get("evidence_status_set")):
        return []
    return [e["id"] for e in tx.out(d["id"], "decides") if e["label"] == "Hypothesis"]


def conclusion_reviews(tx: Tx, refs: list[str]) -> list[dict]:
    tickets = [tx.require(r) for r in refs if label_of(r) == "Ticket"]
    return [t for t in tickets if t.get("subject") == "conclusion"]


def review_gap(reviews: list[dict]) -> str | None:
    """Why a key conclusion has no passing independent check, or None when a cited review answered pass/issues."""
    if any(t["status"] == "answered" and t.get("verdict") in ("pass", "issues") for t in reviews):
        return None
    if reviews:
        last = reviews[-1]
        return f"conclusion review {last['id']} is {last.get('verdict') or last['status']}, not a pass"
    return "no conclusion review"


def _review_hint(hyps: list[str], did: str) -> str:
    return (f"rl review conclusion --target {','.join(hyps)} --proposal .. --author-model .., "
            f"then rl link {did} based_on RV-x")


def decide(store: Store, targets: list[str], action: str, rationale: str, based_on: list[str] = (),
           reopen_condition: str | None = None, kind: str = "route", implementation_ready: str | None = None,
           evidence_scope: str | None = None, set_evidence_status: str | None = None,
           rationale_file: str | None = None) -> dict:
    if not targets:
        raise RLError("INVALID", "a decision targets at least one hypothesis, experiment or task")
    if action == "pause" and not reopen_condition:
        raise RLError("INVALID", "pause needs --reopen-condition: what new evidence would reopen it")
    if kind == "stage":
        if action not in STAGE_ACTIONS:
            raise RLError("INVALID", f"a stage delivery decides one of {', '.join(STAGE_ACTIONS)}")
        if not implementation_ready or not evidence_scope:
            raise RLError("INVALID", "a stage delivery records --implementation-ready and --evidence-scope separately")
    prep = documents.prepare(store.project, rationale_file, "decision") if rationale_file else None
    with store.write() as tx:
        records = [tx.require(t, "Hypothesis", "Experiment", "Task") for t in targets]
        for rec in records:
            if rec["_label"] == "Hypothesis" and rec["disposition"] in ("paused", "retired") \
                    and action not in ("reopen", "stop", "pause", "consult"):
                raise RLError("INVALID", f"{rec['id']} is {rec['disposition']}; reopen it explicitly with new "
                                         "evidence (action reopen) before continuing")
        did = tx.next_id("Decision")
        tx.create("Decision", {"id": did, "kind": kind, "action": action, "rationale": rationale,
                               "rationale_ref": documents.register(tx, prep) if prep else None,
                               "reopen_condition": reopen_condition, "implementation_ready": implementation_ready,
                               "evidence_scope": evidence_scope, "evidence_status_set": set_evidence_status})
        for rec in records:
            tx.link("decides", did, rec["id"], revision=rec.get("revision"))
            if rec["_label"] == "Hypothesis":
                changes = {}
                if action in ACTION_DISPOSITION:
                    changes["disposition"] = ACTION_DISPOSITION[action]
                elif rec["disposition"] == "candidate" and action != "consult":
                    changes["disposition"] = "active"
                if set_evidence_status:
                    changes["evidence_status"] = set_evidence_status
                if changes:
                    tx.update(rec["id"], changes)
            elif rec["_label"] == "Task" and action == "stop" and rec["state"] not in ("accepted", "stopped"):
                tx.update(rec["id"], {"state": "stopped", "note": f"stopped by {did}"})
        for ref in based_on:
            tx.link("based_on", did, ref)
        key = _is_key(kind, action, set_evidence_status)
        hyps = [r["id"] for r in records if r["_label"] == "Hypothesis"] if key else []
        gap = review_gap(conclusion_reviews(tx, list(based_on))) if hyps else None
        tx.event("decision", did, f"{action} {', '.join(targets)}: {rationale}")
    note = (f"\nkey conclusion on {', '.join(hyps)}: {gap}; before relying on it: {_review_hint(hyps, did)}"
            if gap else "")
    return {"id": did, "conclusion_review_gap": gap, "summary": f"{did} {kind}:{action} on {', '.join(targets)}{note}"}


def link(store: Store, source: str, rel: str, target: str, note: str | None = None, locator: str | None = None,
         assessment: str | None = None) -> dict:
    if rel not in LINKABLE:
        raise RLError("INVALID", f"relation must be one of {', '.join(LINKABLE)}")
    with store.write() as tx:
        source_rec, target_rec = tx.require(source), tx.require(target)
        props = {"note": note, "locator": locator, "assessment": assessment}
        if RELATIONS[rel].revision_bound:
            # bind to the claim's revision: the target's (tests, supports) or the citing hypothesis' (cites)
            props["revision"] = target_rec.get("revision")
            props["source_revision"] = source_rec.get("revision") if target_rec["_label"] == "Paper" else None
        tx.link(rel, source, target, **props)
        tx.event("link", source, f"{source} {rel} {target}")
    rev = f"@{props['revision']}" if props.get("revision") else ""
    return {"summary": f"{source} {rel} {target}{rev}"}


# -- reading --------------------------------------------------------------------------------------------------


def _brief(rec: dict) -> str:
    label = rec["_label"]
    if label == "Hypothesis":
        flag = " NEEDS-RECHECK" if rec.get("needs_recheck") else ""
        return f"{rec['id']}@{rec['revision']} [{rec['disposition']}/{rec['evidence_status']}{flag}] {rec['title']}"
    if label == "Experiment":
        return f"{rec['id']}@{rec['revision']} [{rec.get('stage') or '-'}] {rec['title']}"
    if label == "Evidence":
        return f"{rec['id']} [{rec['evidence_level']}/{rec['validity']}] {rec['summary']}"
    if label == "Decision":
        status = f"->{rec['evidence_status_set']}" if rec.get("evidence_status_set") else ""
        return f"{rec['id']} [{rec['kind']}:{rec['action']}{status}] {rec['rationale'][:160]}"
    if label == "Task":
        owner = f" owner={rec['owner_role']}" if rec.get("owner_role") else ""
        return f"{rec['id']} [{rec['kind']}/{rec['state']}{owner}] {rec['title']}"
    if label == "Paper":
        ident = rec.get("arxiv_id") or rec.get("doi") or "no-id"
        return f"{rec['id']} [{ident}, {rec['metadata_status']}] {rec['title']}"
    if label == "Ticket":
        kind = f"{rec['kind']}:{rec['subject']}" if rec.get("subject") == "conclusion" else rec["kind"]
        verdict = f"/{rec['verdict']}" if rec.get("verdict") else ""
        return f"{rec['id']} [{kind}/{rec['status']}{verdict}] {rec.get('question') or rec.get('target_id') or ''}"
    if label == "Attempt":
        return (f"{rec['id']} [{rec['execution_state']}/{rec['outcome']}/result:{rec['result_status']}"
                f"{' NEEDS-CHECK' if rec.get('needs_check') else ''}]")
    return f"{rec['id']} [{label}]"


def brief(rec: dict) -> str:
    return _brief(rec)


def show(store: Store, entity_id: str) -> dict:
    with store.read() as tx:
        label = label_of(entity_id)
        if label == "Document" or label == "DocRevision":
            rev = documents.resolve(tx, entity_id)
            text = documents.read(store.project, rev)
            return {"record": rev, "summary": f"{rev['id']} {rev['source_path']} {rev['digest']}\n\n{text}"}
        rec = tx.require(entity_id)
        out, inc = tx.out(entity_id), tx.inc(entity_id)
        lines = [_brief(rec)]
        for key, value in rec.items():
            if key not in ("id", "_label", "title") and value not in (None, "", []):
                lines.append(f"  {key}: {value}")
        if label == "Hypothesis":
            for e in inc:
                if e["rel"] in ("supports", "challenges"):
                    ev = tx.require(e["id"])
                    bound = "" if e.get("revision") == rec["revision"] else f" (bound to @{e.get('revision')}, re-check)"
                    lines.append(f"  <- {e['rel']}: {_brief(ev)}{bound}")
        for e in out:
            lines.append(f"  -> {e['rel']} {e['id']}" + (f"@{e['revision']}" if e.get("revision") else ""))
        for e in inc:
            if not (label == "Hypothesis" and e["rel"] in ("supports", "challenges")):
                lines.append(f"  <- {e['rel']} {e['id']}")
    return {"record": rec, "out": out, "in": inc, "summary": "\n".join(lines)}


def attention(tx: Tx) -> list[str]:
    items = []
    for h in tx.all("Hypothesis"):
        if h.get("needs_recheck"):
            items.append(f"{h['id']} needs re-check: {h.get('recheck_reason')}")
    for d in tx.all("Decision"):
        if d.get("needs_recheck"):
            items.append(f"{d['id']} needs re-check: {d.get('recheck_reason')}")
    for a in tx.all("Attempt"):
        if a.get("needs_check"):
            items.append(f"{a['id']} has conflicting observations: {(a.get('issues') or ['?'])[-1]}")
        elif a["execution_state"] == "unknown":
            items.append(f"{a['id']} execution state unknown since {a.get('last_observed_at')}; "
                         f"last trusted {a.get('last_trusted_state')} — check with the execution tool")
    for e in tx.all("Experiment"):
        if e.get("revised_after_results"):
            items.append(f"{e['id']} contract revised after results: later comparisons are exploratory")
    standing: dict[str, dict] = {}  # hypothesis -> the latest decision stating a key conclusion on it
    for d in tx.all("Decision"):
        for hid in key_hypotheses(tx, d):
            standing[hid] = d
    for did in sorted({d["id"] for d in standing.values()}):
        gap = review_gap(conclusion_reviews(tx, [e["id"] for e in tx.out(did, "based_on")]))
        if gap:
            hyps = [h for h, d in standing.items() if d["id"] == did]
            items.append(f"{did} key conclusion on {', '.join(hyps)}: {gap} — {_review_hint(hyps, did)}")
    return items


def status(store: Store) -> dict:
    with store.read() as tx:
        meta = tx.meta()
        hyps = tx.all("Hypothesis")
        tasks = [t for t in tx.all("Task") if t["state"] not in ("accepted", "stopped")]
        attempts = [a for a in tx.all("Attempt") if a["execution_state"] != "ended" or a.get("needs_check")]
        tickets = [t for t in tx.all("Ticket") if t["status"] in ("pending", "running", "unavailable", "failed")]
        coordinator = [s for s in tx.all("Session", status="active") if s["role_id"] == "coordinator"]
        counts = {label: len(tx.all(label)) for label in ("Hypothesis", "Experiment", "Evidence", "Decision",
                                                           "Paper", "Task", "Run")}
        items = attention(tx)
    lines = [f"project {store.project.root} · data_version {meta['data_version']} · coordinator "
             + (coordinator[-1]["id"] if coordinator else "none"),
             "counts: " + ", ".join(f"{k} {v}" for k, v in counts.items())]
    open_h = [h for h in hyps if h["disposition"] in ("active", "candidate")]
    lines += ["hypotheses (active/candidate):"] + [f"  {_brief(h)}" for h in open_h] if open_h else ["no open hypotheses"]
    lines += (["open tasks:"] + [f"  {_brief(t)}" + (f" next: {t['next_action']}" if t.get("next_action") else "")
                                 for t in tasks]) if tasks else []
    lines += (["unfinished attempts:"] + [f"  {_brief(a)}" for a in attempts]) if attempts else []
    lines += (["open review/advice tickets:"] + [f"  {_brief(t)}" for t in tickets]) if tickets else []
    lines += (["attention:"] + [f"  {i}" for i in items]) if items else []
    return {"data_version": meta["data_version"], "counts": counts, "attention": items,
            "summary": "\n".join(lines)}


def history(store: Store, entity_id: str | None = None, limit: int = 30) -> dict:
    with store.read() as tx:
        events = tx.all("Event", entity_id=entity_id) if entity_id else tx.all("Event")
    events = events[-limit:]
    return {"events": events, "summary": "\n".join(f"{e['created_at']} {e['type']} {e.get('entity_id') or ''} "
                                                   f"{e.get('summary') or ''}".rstrip() for e in events)}
