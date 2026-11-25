"""Check lines: the mechanical facts under a conclusion, computed by the CLI when the conclusion is read (design 9.2,
9.3). The scientist reads a task's lines before accepting it; the coordinator reads a hypothesis's before
receiving it. Nothing here is stored.
"""

from __future__ import annotations

from . import gitops
from .project import Project
from .research import task_design, task_experiment
from .reviews import blocking, open_findings, task_rounds
from .store import Tx
from .usage_view import usage_line
from .util import RLError


def _safe(fn, *args, default: str = "unknown") -> str:
    try:
        return fn(*args)
    except RLError as exc:
        return f"{default} ({exc.message[:80]})"


def task_lines(tx: Tx, project: Project, task: dict) -> list[str]:
    """9.2: for an execute or prep task."""
    tid = task["id"]
    tip = task.get("delivered_commit")
    base = task.get("design_commit")
    head = f"{tid} {task['state']}" + (f" by {task['holder']}" if task.get("holder") else "")
    if task.get("branch"):
        n = gitops.commits_after(project, base, tip) if base and tip else 0
        head += f" · branch {task['branch']}" + (f", {n} commit(s) after design {base[:9]}" if base else "")
    lines = [head]
    attempts = [a for r in tx.all("Run", task_id=tid) for a in tx.all("Attempt", run_id=r["id"])]
    ended = [a for a in attempts if a["execution_state"] == "ended"]
    ok = [a for a in ended if a["outcome"] == "success"]
    lines.append(f"attempts   {len(ended)} ended: {len(ok)} success, {len(ended) - len(ok)} other"
                 + (f"; {len(attempts) - len(ended)} not ended" if len(attempts) > len(ended) else ""))
    runs = tx.all("Run", task_id=tid)
    evidence_runs = [r for r in runs if r["category"] == "evidence"]
    if task["kind"] == "execute":
        dev = [r for r in evidence_runs if r.get("deviates")]
        lines.append(f"arms       {len(evidence_runs) - len(dev)}/{len(evidence_runs)} evidence runs match the "
                     f"design, {len(dev)} deviation(s)" + (f" ({', '.join(r['id'] for r in dev)})" if dev else ""))
        results = [x for a in attempts for x in tx.all("Result", attempt_id=a["id"])]
        evs = [e["id"] for e in tx.all("Evidence", task_id=tid)]
        lines.append(f"results    {len([r for r in results if r['status'] == 'complete'])} complete; evidence "
                     f"{', '.join(evs) or 'none'}")
    rounds = [r for r in task_rounds(tx, tid) if r.get("review_kind") == "design"]
    if rounds:
        r = rounds[-1]
        method = [f for f in open_findings(r) if f["category"] == "method"]
        severe = [f for f in open_findings(r) if f["severity"] in ("medium", "high")]
        lines.append(f"review     {r['id']} {r['status']} · method {len(method)} open · {len(severe)} medium+ open"
                     + (f" · {len(blocking(r))} blocking" if blocking(r) else ""))
    elif task.get("fast_lane"):
        lines.append("review     fast lane (6.6): the last passed review of this experiment covers it")
    else:
        lines.append("review     none")
    exp = task_design(tx, tid) if task["kind"] == "execute" else None  # as handed over to this task
    if exp and base and tip:
        units = list(exp.get("protected") or [])
        changed = _safe(lambda: ", ".join(gitops.protected_changes(project, base, tip, units)) or "")
        if changed:
            stats = "; ".join(f"{u} {gitops.diffstat(project, base, tip, u)}" for u in changed.split(", "))
            rechecked = [r["id"] for r in rounds if any(p.get("commit") == tip for p in r.get("passes") or [])]
            lines.append(f"protected  {stats} changed after design"
                         + (f", rechecked in {', '.join(rechecked)}" if rechecked else ", NOT rechecked"))
        else:
            lines.append("protected  unchanged since design")
        from .tasks import test_runs
        command = exp.get("semantic_tests")
        runs = test_runs(task, tip, command)
        state = "not recorded" if not runs else "passed" if runs[-1]["outcome"] == "pass" else "FAILED (latest run)"
        lines.append(f"tests      semantic tests {state} at the delivered commit ({command})")
        def on_main() -> str:
            main = gitops.mainline(project)
            fork = gitops.git(gitops.top(project), "merge-base", base, main, check=False)
            return ", ".join(gitops.protected_changes(project, fork, main, units)) if fork else ""
        main_changed = _safe(on_main)
        lines.append("mainline   protected scope " + (f"changed on the mainline since design: {main_changed}"
                                                       if main_changed else "unchanged on the mainline since design"))
    lines.append("usage      " + usage_line(tx, task))
    outputs = task.get("outputs") or []
    lines.append(f"outputs    {len(outputs)} registered" + (f": {', '.join(o['path'] for o in outputs[:4])}"
                                                            + (" ..." if len(outputs) > 4 else "") if outputs else ""))
    if task.get("merge_state"):
        lines.append(f"merge      {task['merge_state']}" + (f": {task['merge_note']}" if task.get("merge_note") else ""))
    return lines


def hypothesis_lines(tx: Tx, task: dict) -> list[str]:
    """9.3: for a hypothesis task the coordinator receives."""
    hyps = [tx.require(e["id"]) for e in tx.out(task["id"], "targets") if e["id"].startswith("H-")]
    decs = [d for d in tx.all("Decision", task_id=task["id"]) if d["kind"] in ("verdict", "stop")]
    lines = []
    for h in hyps:
        d = next((x for x in reversed(decs) if any(e["id"] == h["id"] for e in tx.out(x["id"], "decides"))), None)
        if d is None:
            lines.append(f"{h['id']} by {task.get('holder')} · no decision")
            continue
        what = (f"{d['evidence_status_set']}" if d["kind"] == "verdict"
                else f"stopped, {h['id']} {d.get('disposition_set')}")
        lines.append(f"{h['id']} by {d.get('author')} · decision {d['id']} ({d['state']}): {what}")
        reviews = [t for t in tx.all("Ticket", target_id=d["id"]) if t.get("review_kind") == "conclusion"]
        if reviews:
            r = reviews[-1]
            ruled = f" after ruling {r['ruled'].get('by')}" if isinstance(r.get("ruled"), dict) else ""
            lines.append(f"review       conclusion review {r['id']} {r['status']}{ruled}")
        else:
            lines.append("review       no conclusion review")
    exps: dict[str, str] = {}  # each child's experiment as handed over to it; a later draft is marked as such
    kids = [tx.require(e["id"]) for e in tx.inc(task["id"], "part_of")]
    for k in kids:
        if k["kind"] != "execute":
            continue
        exp, live = task_design(tx, k["id"]), task_experiment(tx, k["id"])
        if not exp:
            continue
        key = f"{exp['id']}@{exp['revision']}"
        exps.setdefault(key, key + (f" sealed for {k['id']}" if k.get("sealed_design") else " draft")
                        + (f" (revised: {exp['revision_reason'][:60]})" if exp.get("revision_reason") else ""))
        if live and live["revision"] != exp["revision"]:
            exps.setdefault(f"{live['id']}@{live['revision']}", f"{live['id']}@{live['revision']} newer draft, "
                                                               "not handed over")
    lines.append("experiments  " + ("; ".join(exps.values()) or "none (a composite or a stop)"))
    done = [k for k in kids if k["state"] == "accepted"]
    cancelled = [k for k in kids if k["state"] == "cancelled"]
    lines.append(f"tasks        {len(done)} accepted and merged, {len(cancelled)} cancelled, "
                 f"{len(kids) - len(done) - len(cancelled)} open")
    lines.append("usage        " + usage_line(tx, task))
    affects = []
    for h in hyps:
        for e in tx.inc(h["id"], "assumes"):
            other = tx.require(e["id"])
            busy = [t for t in tx.inc(other["id"], "targets") if tx.require(t["id"]).get("kind") == "hypothesis"]
            state = tx.require(busy[-1]["id"])["state"] if busy else other["disposition"]
            affects.append(f"{other['id']} ({state})")
    if affects:
        lines.append("affects      " + ", ".join(affects))
    proposed = [tx.require(e["id"]) for h in hyps for e in tx.inc(h["id"], "derived_from") if e.get("primary")]
    proposed = [p for p in proposed if p["disposition"] == "candidate"]
    if proposed:
        lines.append("next         proposed children " + ", ".join(p["id"] for p in proposed))
    return lines
