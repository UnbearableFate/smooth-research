"""Purpose-specific context packs built around the current decision: work, resume and advice; and rl status.

Sections are added in priority order until the token target (an estimate, labelled as such) is reached; the
rest is listed as an index to read on demand. Nothing here queries external systems or restarts work.
"""

from __future__ import annotations

import difflib

from . import compute, documents, explain, labels
from .project import Project, context_target
from .research import CONTRACT_FIELDS, attention, brief, contract_front
from .runs import describe_attempt
from .store import Store, Tx
from .tasks import open_tasks
from .util import RLError, TOKEN_METHOD, estimate_tokens


def _doc_text(tx: Tx, store: Store, ref: str | None, limit_chars: int = 6000) -> str:
    if not ref:
        return ""
    try:
        text = documents.read(store.project, documents.resolve(tx, ref))
    except RLError as exc:
        return f"({ref}: {exc.message})"
    return text if len(text) <= limit_chars else text[:limit_chars] + f"\n… (truncated; full text: rl show {ref})"


def contract_summary(tx: Tx, store: Store, exp: dict) -> str:
    """An experiment and the filled-in fields of its contract (the sealed revision, else the draft)."""
    front = contract_front(tx, store, exp)
    fields = "\n".join(f"  {k}: {front.get(k)}" for k in CONTRACT_FIELDS if front.get(k) not in (None, "TODO"))
    return f"{brief(exp)} (contract {exp.get('contract_ref') or 'draft'}: {exp.get('contract_path')})\n{fields}"


DIFF_LINES = 80  # lines of a contract diff in a work pack; rl show prints both revisions in full


def _bound_contract(tx: Tx, store: Store, exp: dict, edge: dict) -> str:
    """The contract revision a task is bound to (task create sealed it) and, when the contract was revised since,
    both revisions and what changed. The task goes on; runs already registered keep the revision they were
    registered under."""
    ref, rev = edge.get("contract_ref"), edge.get("revision")
    if not ref:  # a task created before tasks were bound to contract revisions
        return ""
    eid = exp["id"]
    if ref == exp.get("contract_ref"):
        return f"\nThis task is bound to contract {eid}@{rev} ({ref}), the current revision."
    try:
        old, new = (documents.read(store.project, documents.resolve(tx, r)).splitlines()
                    for r in (ref, exp["contract_ref"]))
        diff = list(difflib.unified_diff(old, new, f"{eid}@{rev} ({ref})",
                                         f"{eid}@{exp['revision']} ({exp['contract_ref']})", lineterm=""))
    except RLError as exc:
        diff = [f"(no diff: {exc.message})"]
    if len(diff) > DIFF_LINES:
        diff = diff[:DIFF_LINES] + [f"… {len(diff) - DIFF_LINES} more lines: rl show {ref}, rl show "
                                    f"{exp['contract_ref']}"]
    return (f"\nThis task was created on contract {eid}@{rev} ({ref}); the contract has been revised since, to "
            f"{eid}@{exp['revision']} ({exp['contract_ref']}; draft {exp.get('contract_path')}). New runs are "
            "registered under the latest revision, runs registered before keep theirs. What changed:\n"
            + "\n".join(diff))


def _evidence_for(tx: Tx, hid: str, current_rev: int) -> list[str]:
    lines = []
    for e in tx.inc(hid):
        if e["rel"] not in ("supports", "challenges"):
            continue
        ev = tx.require(e["id"])
        bound = "" if e.get("revision") == current_rev else f" [bound to @{e.get('revision')}]"
        order = {"valid": 0, "pending": 1, "invalid": 2}[ev["validity"]]
        lines.append((order, f"  {e['rel']}: {brief(ev)}{bound}" + (f" scope: {ev['scope']}" if ev.get("scope") else "")))
    return [text for _, text in sorted(lines)]


def _papers_for(tx: Tx, hid: str) -> list[str]:
    out = []
    for e in tx.out(hid):
        if e["label"] == "Paper":
            p = tx.require(e["id"])
            extra = ", ".join(x for x in (e.get("assessment"), e.get("locator"), e.get("note")) if x)
            out.append(f"  {e['rel']}: {brief(p)}" + (f" ({extra})" if extra else ""))
    return out


def _assemble(title: str, sections: list[tuple[str, str]], budget: int, known: dict[str, str]) -> dict:
    """The sections in order until the budget is used (the rest listed by name), hypotheses and experiments named
    by their labels (known)."""
    used, parts, omitted = estimate_tokens(title), [title], []
    for name, text in sections:
        name, text = labels.relabel(name, known), labels.relabel(text, known)
        if not text.strip():
            continue
        block = f"\n## {name}\n{text.rstrip()}\n"
        cost = estimate_tokens(block)
        if used + cost > budget and parts[1:]:
            omitted.append(name)
            continue
        parts.append(block)
        used += cost
    if omitted:
        parts.append("\n## Omitted (read on demand)\n" + "\n".join(f"- {o}" for o in omitted) + "\n")
    text = "".join(parts)
    return {"tokens": estimate_tokens(text), "token_method": TOKEN_METHOD, "omitted": omitted,
            "summary": text + f"\n(≈{estimate_tokens(text)} tokens, {TOKEN_METHOD})"}


def locations(store: Store) -> str:
    p = store.project
    if p.in_worktree:  # a researcher's worktree: code and notes here, records and run outputs in the project
        return (f"code src/ and notes research/notes/ in this checkout ({p.checkout}) · records, packages and run "
                f"outputs in the project: {p.research_root}/ (artifacts/<attempt>/, contracts experiments/) · "
                f"constraints {p.constraints_path}")
    return ("code src/ · for people docs/ (plan, constraints, research.html (rl view), manuscript, explanations/) "
            "· records and working documents research/ (experiments/, tasks/, notes/, reviews/, advice/, reports/, "
            "sealed documents, packages, artifacts/<attempt>/)")


def work_pack(store: Store, task_id: str, budget: int | None = None) -> dict:
    budget = budget or context_target(store.project.config, "work_target_tokens")
    with store.read() as tx:
        task = tx.require(task_id, "Task")
        coord = [s for s in tx.all("Session", status="active") if s["role_id"] == "coordinator"]
        c = coord[-1] if coord else None
        sections = [("Task", "\n".join(filter(None, [
            brief(task), f"next action: {task.get('next_action') or '-'}",
            None if task.get("owner_role") else f"unclaimed: rl task claim {task_id} --role <role>",
            f"waiting on: {', '.join(task['wait_attempts'])}" if task.get("wait_attempts") else None,
            f"handoff: {task['handoff_ref']}" if task.get("handoff_ref") else None,
            f"coordinator: {c['id']} ({c['host']} session {c.get('host_session_id') or 'unknown'}); notices that "
            "jobs ended go to this session" if c else "coordinator: no active session"]))),
                    ("Locations", locations(store))]
        if task.get("request_ref"):
            sections.append((f"Request {task['request_ref']}", _doc_text(tx, store, task["request_ref"])))
        if task.get("handoff_ref"):
            sections.append((f"Handoff {task['handoff_ref']}", _doc_text(tx, store, task["handoff_ref"])))
        edges = {e["id"]: e for e in tx.out(task_id, "targets")}
        targets = [tx.require(tid) for tid in edges]
        hyps = {t["id"]: t for t in targets if t["_label"] == "Hypothesis"}
        exps = [t for t in targets if t["_label"] == "Experiment"]
        for exp in exps:
            for e in tx.out(exp["id"], "tests"):
                hyps.setdefault(e["id"], tx.require(e["id"]))
        for exp in exps:
            sections.append((f"Experiment {exp['id']}", contract_summary(tx, store, exp)
                             + _bound_contract(tx, store, exp, edges[exp["id"]])))
        for h in hyps.values():
            body = [brief(h), f"  statement: {h['statement']}"] + ([f"  scope: {h['scope']}"] if h.get("scope") else [])
            sections.append((f"Hypothesis {h['id']}", "\n".join(body + _evidence_for(tx, h["id"], h["revision"]))))
        decisions = {e["id"] for t in [*hyps.values(), *exps] for e in tx.inc(t["id"], "decides")}
        if decisions:
            sections.append(("Decisions on these targets",
                             "\n".join(brief(tx.require(d)) for d in sorted(decisions))))
        attempts = [describe_attempt(tx, a) for exp in exps for run in tx.all("Run", experiment_id=exp["id"])
                    for a in tx.all("Attempt", run_id=run["id"])]
        if attempts:
            sections.append(("Runs of these experiments", "\n".join(attempts)))
        papers = [p for h in hyps.values() for p in _papers_for(tx, h["id"])]
        if papers:
            sections.append(("Literature", "\n".join(papers)))
        sections.append(("Compute resources (smooth-research.yaml)", compute.pack_text(store.project)))
        sections.append(("Constraints", f"see {store.project.rel(store.project.constraints_path)} (user-set scope "
                                        "and resource limits; follow them, the plugin does not enforce them)"))
        known = labels.names(tx)
    pack = _assemble(f"# Work pack for {task_id}\n", sections, budget, known)
    pack["labels"] = {r["id"]: known[r["id"]] for r in [*exps, *hyps.values()] if r["id"] in known}
    return pack


def open_work(tx: Tx, project: Project) -> dict:
    """What the summaries of the project start from (rl status, rl resume, the session start hook): the
    coordinator session, the open tasks, unfinished attempts and tickets, and what needs attention (with one item
    for the explanations of docs/research.html once it exists)."""
    coord = [s for s in tx.all("Session", status="active") if s["role_id"] == "coordinator"]
    return {"coordinator": coord[-1] if coord else None, "tasks": open_tasks(tx),
            "attempts": [a for a in tx.all("Attempt") if a["execution_state"] != "ended" or a.get("needs_check")],
            "tickets": [t for t in tx.all("Ticket") if t["status"] != "answered"],
            "attention": attention(tx) + explain.attention(tx, project)}


def status(store: Store) -> dict:
    with store.read() as tx:
        meta = tx.meta()
        hyps = tx.all("Hypothesis")
        work = open_work(tx, store.project)
        counts = {label: len(tx.all(label)) for label in ("Hypothesis", "Experiment", "Evidence", "Decision",
                                                           "Paper", "Task", "Run")}
        known = labels.names(tx)
    c, items = work["coordinator"], work["attention"]
    lines = [f"project {store.project.root} · data_version {meta['data_version']} · coordinator "
             + (c["id"] if c else "none"),
             "counts: " + ", ".join(f"{k} {v}" for k, v in counts.items())]
    open_h = labels.ordered([h for h in hyps if h["disposition"] in ("active", "candidate")])
    lines += (["hypotheses (active/candidate):"] + [f"  {brief(h)}" for h in open_h]) if open_h \
        else ["no open hypotheses"]
    lines += (["open tasks:"] + [f"  {brief(t)}" + (f" next: {t['next_action']}" if t.get("next_action") else "")
                                 for t in work["tasks"]]) if work["tasks"] else []
    lines += (["unfinished attempts:"] + [f"  {brief(a)}" for a in work["attempts"]]) if work["attempts"] else []
    lines += (["open review/advice tickets:"] + [f"  {brief(t)}" for t in work["tickets"]]) if work["tickets"] else []
    lines += (["attention:"] + [f"  {i}" for i in items]) if items else []
    return {"data_version": meta["data_version"], "counts": counts, "attention": items,
            "summary": labels.relabel("\n".join(lines), known)}


def resume_pack(store: Store, role: str | None = None, budget: int | None = None) -> dict:
    budget = budget or context_target(store.project.config, "work_target_tokens")
    with store.read() as tx:
        meta = tx.meta()
        work = open_work(tx, store.project)
        tasks = [t for t in work["tasks"]
                 if role is None or role in (t.get("owner_role"), t.get("to_role")) or role == "coordinator"]
        attempts = [describe_attempt(tx, a) for a in work["attempts"]]
        hyps = labels.ordered(tx.all("Hypothesis"))
        decisions = tx.all("Decision")[-5:]
        cursor = tx.get(f"CUR-{role}", "Cursor") if role else None
        unread = [m for m in tx.all("Message", to_role=role) if m["seq"] > (cursor["position"] if cursor else 0)] if role else []
        known = labels.names(tx)
    c = work["coordinator"]
    model = (c.get("model_observed") or c.get("model_requested") or "model unverified") if c else None
    identity = [f"coordinator {c['id']} ({c['host']}, {model})" if c else "no active coordinator session",
                f"constraints {store.project.rel(store.project.constraints_path)}",
                f"data_version {meta['data_version']}"]
    first = []
    for t in tasks:
        line = f"{brief(t)} next: {t.get('next_action') or '-'}"
        if t.get("wait_attempts"):
            line += f" (waiting on {', '.join(t['wait_attempts'])})"
        first.append(line)
    sections = [("Identity", "\n".join(identity)),
                ("Unfinished tasks (first actions)", "\n".join(first) or "none"),
                ("Unfinished attempts (recorded facts only; verify unknown ones with your execution tool)",
                 "\n".join(attempts) or "none"),
                ("Attention", "\n".join(work["attention"]) or "none"),
                ("Open review/advice tickets", "\n".join(brief(t) for t in work["tickets"]) or "none"),
                ("Open hypotheses", "\n".join(brief(h) for h in hyps if h["disposition"] in ("active", "candidate")) or "none"),
                ("Paused or retired routes", "\n".join(brief(h) for h in hyps if h["disposition"] in ("paused", "retired")) or "none"),
                ("Recent decisions", "\n".join(brief(d) + (f" reopen if: {d['reopen_condition']}" if d.get("reopen_condition") else "")
                                               for d in decisions) or "none")]
    if role:
        sections.append((f"Inbox for {role}", f"{len(unread)} unread message(s): rl inbox --role {role}"))
    sections.append(("Locations", locations(store)))
    sections.append(("Compute resources (smooth-research.yaml)", compute.pack_text(store.project)))
    return _assemble("# Resume pack (records only: nothing was queried, restarted or retried)\n", sections, budget,
                     known)


def advice_pack(store: Store, target: str | None, question: str, budget: int | None = None) -> dict:
    budget = budget or context_target(store.project.config, "advice_target_tokens")
    with store.read() as tx:
        hyps = labels.ordered(tx.all("Hypothesis"))
        known = labels.names(tx)
        focus = tx.require(target) if target else None
        sections = [("Question", question)]
        if focus:
            text = brief(focus)
            if focus["_label"] == "Hypothesis":
                text += f"\n  statement: {focus['statement']}" + (f"\n  scope: {focus['scope']}" if focus.get("scope") else "")
                text += "\n" + "\n".join(_evidence_for(tx, focus["id"], focus["revision"]))
            sections.append(("Bottleneck focus", text))
        evidence = [ev for ev in tx.all("Evidence") if ev["validity"] == "valid"]
        sections.append(("Key valid evidence (positive and negative)",
                         "\n".join(f"{brief(ev)} -> " + ", ".join(f"{e['rel']} {e['id']}" for e in tx.out(ev['id'])
                                                                    if e["rel"] in ("supports", "challenges"))
                                   for ev in evidence) or "none yet"))
        diag = [ev for ev in tx.all("Evidence") if ev["evidence_level"] == "diagnostic" or ev["validity"] == "invalid"]
        if diag:
            sections.append(("Diagnostics and invalidated results (not mechanism evidence)",
                             "\n".join(brief(ev) for ev in diag)))
        tried = [h for h in hyps if h["disposition"] in ("paused", "retired")]
        sections.append(("Routes tried and stopped or paused",
                         "\n".join(brief(h) + "".join(f"\n  {brief(tx.require(e['id']))}" for e in tx.inc(h["id"], "decides"))
                                   for h in tried) or "none"))
        sections.append(("Surviving explanations",
                         "\n".join(f"{brief(h)}\n  {h['statement']}" for h in hyps if h["disposition"] in ("active", "candidate")) or "none"))
        papers = [p for h in hyps for p in _papers_for(tx, h["id"])]
        if papers:
            sections.append(("Nearest literature", "\n".join(sorted(set(papers)))))
        artifacts = [f"{ev['id']}: {ev.get('doc_ref') or ''} {', '.join(ev.get('artifact_refs') or [])}".strip()
                     for ev in tx.all("Evidence") if ev.get("doc_ref") or ev.get("artifact_refs")]
        if artifacts:
            sections.append(("Raw artifact index (challenge the interpretation if they disagree)", "\n".join(artifacts)))
        sections.append(("Constraints", _read_constraints(store)))
    return _assemble("# Advice pack\n", sections, budget, known)


def _read_constraints(store: Store) -> str:
    path = store.project.constraints_path
    return path.read_text(encoding="utf-8")[:3000] if path.exists() else "no constraints document"
