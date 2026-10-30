"""Projections generated from the records: a tree view of the hypothesis graph, an HTML page and a report.

They are regenerated on demand and never edited by hand; the database and sealed documents stay authoritative.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from .research import brief, conclusion_reviews, key_hypotheses, review_gap
from .runs import code_review_summary, describe_attempt, evidence_attempts, usage_summary
from .store import Store, Tx

SIDE = ("tests", "supports", "challenges", "decides", "nearest_work", "motivated_by", "cites", "combines")


def _children(tx: Tx, hid: str) -> list[str]:
    return [e["id"] for e in tx.inc(hid, "derived_from") if e["label"] == "Hypothesis"]


def _roots(tx: Tx) -> list[dict]:
    hyps = tx.all("Hypothesis")
    return [h for h in hyps if not any(e["label"] == "Hypothesis" for e in tx.out(h["id"], "derived_from"))]


def tree(store: Store, root: str | None = None) -> dict:
    """Tree projection along derived_from; experiments, evidence, decisions and papers hang off each node."""
    with store.read() as tx:
        lines: list[str] = []

        def walk(hid: str, depth: int, seen: set) -> None:
            h = tx.require(hid)
            pad = "  " * depth
            lines.append(f"{pad}{brief(h)}")
            if hid in seen:
                return
            seen.add(hid)
            for e in tx.inc(hid):
                if e["rel"] in ("tests", "supports", "challenges", "decides"):
                    other = tx.require(e["id"])
                    bound = "" if e.get("revision") in (None, h["revision"]) else f" (@{e['revision']})"
                    lines.append(f"{pad}  ↳ {e['rel']} by {brief(other)}{bound}")
                    if other["_label"] == "Experiment":
                        for run in tx.all("Run", experiment_id=other["id"]):
                            for att in tx.all("Attempt", run_id=run["id"]):
                                lines.append(f"{pad}      {att['id']} {att['execution_state']}/{att['outcome']} "
                                             f"result:{att['result_status']}")
            for e in tx.out(hid):
                if e["rel"] in ("nearest_work", "motivated_by", "cites", "combines"):
                    lines.append(f"{pad}  ↳ {e['rel']} {brief(tx.require(e['id']))}")
            for child in _children(tx, hid):
                walk(child, depth + 1, seen)

        starts = [root] if root else [h["id"] for h in _roots(tx)]
        seen: set = set()
        for hid in starts:
            walk(hid, 0, seen)
    return {"summary": "\n".join(lines) or "no hypotheses yet"}


def mermaid(tx: Tx) -> str:
    out = ["flowchart TD"]
    style = {"Hypothesis": ('["', '"]'), "Experiment": ('[/"', '"/]'), "Evidence": ('("', '")'),
             "Decision": ('{"', '"}'), "Paper": ('[["', '"]]'), "Ticket": ('>"', '"]')}
    nodes = {}
    for label in style:
        for rec in tx.all(label):
            if label == "Ticket" and rec["kind"] != "advice":
                continue
            text = brief(rec).replace('"', "'")[:90]
            nodes[rec["id"]] = label
            a, b = style[label]
            out.append(f"    {rec['id'].replace('-', '_')}{a}{text}{b}")
    for nid, label in nodes.items():
        for e in tx.out(nid, label=label):
            if e["id"] in nodes:
                arrow = "-.->" if e["rel"] in ("challenges", "motivated_by", "nearest_work", "cites") else "-->"
                out.append(f"    {nid.replace('-', '_')} {arrow}|{e['rel']}| {e['id'].replace('-', '_')}")
    return "\n".join(out)


def html_view(store: Store) -> dict:
    with store.read() as tx:
        diagram = mermaid(tx)
        meta = tx.meta()
        rows = {label: tx.all(label) for label in ("Hypothesis", "Experiment", "Evidence", "Decision", "Paper", "Task")}
        attempts = [describe_attempt(tx, a) for a in tx.all("Attempt")]
    text = tree(store)["summary"]
    sections = "".join(
        f"<h2>{label}</h2><ul>" + "".join(f"<li>{html.escape(brief(r))}</li>" for r in recs) + "</ul>"
        for label, recs in rows.items() if recs)
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Research graph — {html.escape(meta.get('title', ''))}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;color:#222}}
pre{{background:#f6f6f4;padding:1em;overflow:auto;font-size:13px}}li{{margin:.2em 0}}</style>
<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
mermaid.initialize({{startOnLoad:true}});</script></head><body>
<h1>Research graph: {html.escape(meta.get('title', ''))}</h1>
<p>Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} from data_version {meta['data_version']}.
A projection of the records; edit records with <code>rl</code>, not this page.</p>
<pre class="mermaid">{html.escape(diagram)}</pre>
<h2>Tree (derived_from)</h2><pre>{html.escape(text)}</pre>
<h2>Attempts</h2><ul>{''.join(f'<li>{html.escape(a)}</li>' for a in attempts) or '<li>none</li>'}</ul>
{sections}</body></html>"""
    path = store.project.reports_root / "research-graph.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return {"path": str(path), "summary": f"wrote {store.project.rel(path)} (data_version {meta['data_version']})"}


def _num(value: float) -> str:
    return f"{value:.4g}"


def _arm_table(tx: Tx, exp_id: str) -> list[str]:
    """One row per arm: attempts, their states, and descriptive means of numeric metrics over usable results."""
    arms: dict[str, dict] = {}
    for run in tx.all("Run", experiment_id=exp_id):
        arm = arms.setdefault(str(run["spec"].get("arm")), {"seeds": set(), "states": {}, "metrics": {},
                                                             "mismatch": 0})
        arm["seeds"].add(run["spec"].get("seed"))
        for att in tx.all("Attempt", run_id=run["id"]):
            state = f"{att['execution_state']}/{att['outcome']}"
            arm["states"][state] = arm["states"].get(state, 0) + 1
            results = [r for r in tx.all("Result", attempt_id=att["id"]) if r["status"] != "rejected"]
            if not results:
                continue
            latest = results[-1]
            if not latest.get("usable_for_planned_comparison", True):
                arm["mismatch"] += 1
                continue
            for key, value in (latest["manifest"].get("metrics") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    arm["metrics"].setdefault(key, []).append(float(value))
    if not arms:
        return ["- no runs registered"]
    keys = sorted({k for a in arms.values() for k in a["metrics"]})[:6]
    lines = ["", f"| arm | seeds | attempts | {' | '.join(keys) or 'metrics'} |",
             "|" + " --- |" * (3 + max(len(keys), 1))]
    for name, a in sorted(arms.items()):
        seeds = ",".join(str(s) for s in sorted(a["seeds"], key=str) if s is not None) or "-"
        states = ", ".join(f"{n} {s}" for s, n in sorted(a["states"].items()))
        cells = [(f"{_num(sum(v) / len(v))} (n={len(v)})" if (v := a["metrics"].get(k)) else "-") for k in keys] or ["-"]
        flag = f" ({a['mismatch']} not usable: spec mismatch)" if a["mismatch"] else ""
        lines.append(f"| {name} | {seeds} | {states}{flag} | {' | '.join(cells)} |")
    lines += [f"\nMeans are descriptive over attempts; per-attempt records: `rl run show {exp_id}`.", ""]
    return lines


def _decision_line(d: dict, check: str | None) -> str:
    status = f"->{d['evidence_status_set']}" if d.get("evidence_status_set") else ""
    reopen = f" (reopen if: {d['reopen_condition']})" if d.get("reopen_condition") else ""
    review = f" [{check}]" if check else ""
    return f"- {d['id']} {d['kind']}:{d['action']}{status} — {d['rationale']}{reopen}{review}"


def report(store: Store, out: str | None = None, title: str | None = None) -> dict:
    """A human-facing progress/stage report generated from the records into reports/."""
    with store.read() as tx:
        meta = tx.meta()
        hyps, decisions = tx.all("Hypothesis"), tx.all("Decision")
        exps, evidence, papers = tx.all("Experiment"), tx.all("Evidence"), tx.all("Paper")
        tickets = tx.all("Ticket")
        usage = usage_summary(tx)
        exp_lines = []
        for exp in exps:
            exp_lines.append(f"### {exp['id']} {exp['title']}\n\n- stage {exp.get('stage') or '-'}, contract "
                             f"{exp.get('contract_ref') or 'draft (not sealed)'}"
                             + (" — revised after results (exploratory)" if exp.get("revised_after_results") else "")
                             + f"\n- question: {exp['question']}")
            exp_lines += _arm_table(tx, exp["id"])
        ev_lines = []
        for ev in evidence:
            rel = ", ".join(f"{e['rel']} {e['id']}@{e.get('revision')}" for e in tx.out(ev["id"])
                            if e["rel"] in ("supports", "challenges"))
            code = code_review_summary(evidence_attempts(tx, ev["id"]))
            ev_lines.append(f"- {ev['id']} [{ev['evidence_level']}, {ev['validity']}] {ev['summary']}"
                            + (f" ({rel})" if rel else "") + (f"; scope: {ev['scope']}" if ev.get("scope") else "")
                            + (f" [code review: {code}]" if code else ""))
        stage = [d for d in decisions if d["kind"] == "stage"]
        checks = {}  # key conclusions: which conclusion review backs each one, or why none does
        for d in decisions:
            if key_hypotheses(tx, d):
                revs = conclusion_reviews(tx, [e["id"] for e in tx.out(d["id"], "based_on")])
                gap = review_gap(revs)
                checks[d["id"]] = gap or "conclusion review " + ", ".join(
                    f"{t['id']} {t['verdict']}" + (f" ({t['relation']})" if t.get("relation") else "")
                    for t in revs if t["status"] == "answered")
    lines = [f"# {title or 'Research report'}: {meta.get('title', '')}",
             f"\nGenerated {datetime.now(timezone.utc).isoformat(timespec='seconds')} from the research records "
             f"(data_version {meta['data_version']}, plan {meta.get('plan_ref') or '-'}). Regenerate with `rl report`.",
             "\n## Hypotheses\n"]
    lines += [f"- {brief(h)}\n  - {h['statement']}" + (f"\n  - scope: {h['scope']}" if h.get("scope") else "") for h in hyps] or ["none"]
    lines += ["\n## Experiments and runs\n", *(exp_lines or ["none"])]
    lines += ["\n## Evidence\n", *(ev_lines or ["none"])]
    lines += ["\n## Decisions\n"]
    lines += [_decision_line(d, checks.get(d["id"])) for d in decisions] or ["none"]
    if stage:
        d = stage[-1]
        lines += ["\n## Stage delivery\n", f"- implementation_ready: {d.get('implementation_ready')}",
                  f"- evidence_scope: {d.get('evidence_scope')}", f"- investment_decision: {d['action']} — {d['rationale']}"]
    lines += ["\n## Reviews and advice\n"]
    lines += [f"- {brief(t)}" + (f" — {t['relation']}" if t.get("relation") else "")
              + (f", by {t['observed_model']}" if t.get("observed_model") else "") for t in tickets] or ["none"]
    lines += ["\n## Literature\n"] + ([f"- {brief(p)}" for p in papers] or ["none"])
    lines += ["\n## Resource use (recorded; units are not added across types)\n"]
    lines += [f"- {t['quantity']:g} {t['unit']} {t['resource_type']} on {t['hardware']} ({t['basis']})"
              for t in usage["totals"]] or ["- none recorded"]
    if usage["unreported_attempts"]:
        lines.append(f"- unreported for: {', '.join(usage['unreported_attempts'])}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    path = store.project.abs(out) if out else store.project.reports_root / f"report-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "summary": f"wrote {store.project.rel(path)}"}
