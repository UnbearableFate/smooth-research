"""Projections generated from the records: a tree view of the hypothesis graph, the arm tables and a stage report
(research/reports/); the explanation page docs/research.html is explain.py.

They are regenerated on demand and never edited by hand; the database and sealed documents stay authoritative.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from . import labels
from .research import brief, conclusion_reviews, contract_front, key_hypotheses, review_gap, review_mark
from .runs import code_review_summary, evidence_attempts, usage_summary
from .store import Store, Tx, _sort_key
from .util import canonical_json, short

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
        known = labels.names(tx)
    return {"summary": labels.relabel("\n".join(lines), known) or "no hypotheses yet"}


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
    return labels.relabel("\n".join(out), labels.names(tx))


CELL_CHARS = 60  # a longer table cell is cut short: rl run show has all of it
MAX_METRICS = 6  # metric columns when the contract declares none


def _num(value: float) -> str:
    return f"{value:.4g}"


def cut_cell(text: str) -> str:
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS - 1] + "…"


def _value(value) -> str:
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in sorted(value.items())) or "-"
    text = "-" if value is None else str(value)
    return short(text) if ":" in text and " " not in text and len(text) > 40 else text  # a digest


def condition(run: dict) -> dict:
    """What makes runs of an experiment one comparison: the contract revision they were registered under, the
    arm, the data and model versions, the configuration and the environment (runs with a different
    environment_ref are not the same comparison). Seeds, code commits and output places may differ."""
    spec = run["spec"]
    return {"contract": int(run["experiment_ref"].rpartition("@")[2] or 0), "arm": str(spec.get("arm")),
            "data": spec.get("data_revision"), "model": spec.get("model_revision"),
            "config": spec.get("config_ref") or spec.get("config_digest"), "params": spec.get("params") or {},
            "environment": spec.get("environment_ref")}


def run_result(tx: Tx, run: dict) -> tuple[dict | None, list[dict]]:
    """The run's one result, over all its attempts (a re-run does not count twice): the latest complete one, else
    the latest partial one; and the attempts."""
    attempts = tx.all("Attempt", run_id=run["id"])
    results = sorted((r for a in attempts for r in tx.all("Result", attempt_id=a["id"]) if r["status"] != "rejected"),
                     key=lambda r: _sort_key(r["id"]))
    return ([r for r in results if r["status"] == "complete"] or results or [None])[-1], attempts


def arm_rows(tx: Tx, store: Store, exp: dict) -> tuple[list[str], list[dict]]:
    """The metrics shown and one row per condition (condition): its seeds, its attempts by state, the descriptive
    mean of each metric over its runs (each run once: run_result), and what to keep in mind (partial results,
    results not usable for the planned comparison, more than one code version). Rows of different conditions are never
    averaged together. The metrics are those the contract declares (metrics:); without them the contract's main
    metric and the runs' come first, then the others alphabetically, MAX_METRICS at most."""
    groups: dict[str, dict] = {}
    named = []
    for run in tx.all("Run", experiment_id=exp["id"]):
        cond = condition(run)
        g = groups.setdefault(canonical_json(cond), {"condition": cond, "seeds": set(), "states": Counter(),
                                                      "values": {}, "partial": 0, "mismatch": 0, "code": set()})
        g["seeds"].add(run["spec"].get("seed"))
        g["code"].add(canonical_json(run["spec"].get("code_ref")))
        named.append(run["spec"].get("metric"))
        result, attempts = run_result(tx, run)
        g["states"].update(f"{a['execution_state']}/{a['outcome']}" for a in attempts)
        if result is None:
            continue
        if not result.get("usable_for_planned_comparison", True):
            g["mismatch"] += 1
            continue
        g["partial"] += result["status"] == "partial"
        metrics = result["manifest"].get("metrics")
        for key, value in (metrics if isinstance(metrics, dict) else {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                g["values"].setdefault(key, []).append(float(value))
    front = contract_front(tx, store, exp)
    declared = front.get("metrics") if isinstance(front.get("metrics"), list) else []
    found = sorted({k for g in groups.values() for k in g["values"]})
    keys = [str(m) for m in declared] or list(dict.fromkeys(
        [m for m in (front.get("metric"), *named) if m in found] + found))[:MAX_METRICS]
    varying = [k for k in ("data", "model", "config", "params", "environment")
               if len({canonical_json(g["condition"][k]) for g in groups.values()}) > 1]
    rows = []
    for g in sorted(groups.values(), key=lambda g: (g["condition"]["arm"], g["condition"]["contract"],
                                                    canonical_json(g["condition"]))):
        c = g["condition"]
        notes = ([f"{g['partial']} partial"] if g["partial"] else []) \
            + ([f"{g['mismatch']} not usable for the planned comparison"] if g["mismatch"] else []) \
            + ([f"{len(g['code'])} code versions"] if len(g["code"]) > 1 else [])
        rows.append({"arm": c["arm"], "contract": f"@{c['contract']}",
                     "condition": "; ".join(f"{k} {_value(c[k])}" for k in varying),
                     "seeds": ",".join(str(s) for s in sorted(g["seeds"], key=str) if s is not None) or "-",
                     "attempts": ", ".join(f"{n} {s}" for s, n in sorted(g["states"].items())),
                     "cells": [f"{_num(sum(v) / len(v))} (n={len(v)})" if (v := g["values"].get(k)) else "-"
                               for k in keys],
                     "notes": "; ".join(notes)})
    return keys, rows


def arm_table(tx: Tx, store: Store, exp: dict) -> tuple[list[str], list[list[str]]]:
    """arm_rows as a header and rows of cells; the condition column only when conditions differ."""
    keys, rows = arm_rows(tx, store, exp)
    cond = any(r["condition"] for r in rows)
    head = ["arm", "contract", *(["condition"] if cond else []), "seeds", "attempts", *keys, "notes"]
    return head, [[r["arm"], r["contract"], *([r["condition"]] if cond else []), r["seeds"], r["attempts"],
                   *r["cells"], r["notes"] or "-"] for r in rows]


def costs(tx: Tx, exp_id: str) -> list[str]:
    """The experiment's recorded resource use, kept apart: reported (usage that covers only its attempts), shared
    jobs (usage that also covers other experiments' attempts: not split) and unknown (attempts that ran with
    none recorded: unknown, not zero)."""
    ran = {a["id"] for run in tx.all("Run", experiment_id=exp_id) for a in tx.all("Attempt", run_id=run["id"])
           if a["execution_state"] != "prepared"}
    reported: dict[tuple, float] = {}
    shared, covered = [], set()
    for uid in sorted({e["id"] for att in ran for e in tx.inc(att, "covers")}, key=_sort_key):
        u = tx.require(uid, "Usage")
        if u.get("superseded"):
            continue
        cover = {e["id"] for e in tx.out(uid, "covers")}
        covered |= cover & ran
        key = (u["resource_type"], u["unit"], u.get("hardware") or "-", u["basis"])
        if cover <= ran:
            reported[key] = reported.get(key, 0.0) + u["quantity"]
        else:
            shared.append(f"{u['id']} {u['quantity']:g} {key[1]} {key[0]} on {key[2]} ({key[3]}), also for "
                          f"{len(cover - ran)} attempt(s) of other experiments")
    unknown = sorted(ran - covered, key=_sort_key)
    out = ["reported " + "; ".join(f"{q:g} {k[1]} {k[0]} on {k[2]} ({k[3]})" for k, q in sorted(reported.items()))
           ] if reported else []
    out += [f"shared job {s}" for s in shared]
    out += [f"unknown for {len(unknown)} attempt(s) ({', '.join(unknown[:4])}{', …' if len(unknown) > 4 else ''})"
            ] if unknown else []
    return out


def _arm_table(tx: Tx, store: Store, exp: dict) -> list[str]:
    """The arm table in Markdown, long cells cut short, and the experiment's resource use."""
    head, rows = arm_table(tx, store, exp)
    if not rows:
        return ["- no runs registered"]
    lines = ["", "| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    lines += ["| " + " | ".join(cut_cell(str(c)).replace("|", "\\|") for c in row) + " |" for row in rows]
    lines += [f"\nMeans are descriptive, one result per run; rows of different conditions are not averaged together. "
              f"Per-attempt records: `rl run show {exp['id']}`."]
    lines += [f"Resource use: {'; '.join(costs(tx, exp['id'])) or 'no attempt ran'}.", ""]
    return lines


def evidence_line(tx: Tx, ev: dict) -> str:
    rel = ", ".join(f"{e['rel']} {e['id']}@{e.get('revision')}" for e in tx.out(ev["id"])
                    if e["rel"] in ("supports", "challenges"))
    code = code_review_summary(tx, evidence_attempts(tx, ev["id"]))
    return (f"{ev['id']} [{ev['evidence_level']}, {ev['validity']}] {ev['summary']}" + (f" ({rel})" if rel else "")
            + (f"; scope: {ev['scope']}" if ev.get("scope") else "") + (f" [code review: {code}]" if code else ""))


def conclusion_checks(tx: Tx, decisions: list[dict]) -> dict[str, str]:
    """For each decision on a key conclusion: the conclusion reviews that back it, or why none does."""
    checks = {}
    for d in decisions:
        if hyps := key_hypotheses(tx, d):
            revs = conclusion_reviews(tx, [e["id"] for e in tx.out(d["id"], "based_on")])
            checks[d["id"]] = review_gap(tx, revs, hyps) or "conclusion review " + ", ".join(
                f"{t['id']} {review_mark(t)}" + (f" ({t['relation']})" if t.get("relation") else "")
                for t in revs if t["status"] == "answered")
    return checks


def decision_line(d: dict, check: str | None) -> str:
    status = f"->{d['evidence_status_set']}" if d.get("evidence_status_set") else ""
    reopen = f" (reopen if: {d['reopen_condition']})" if d.get("reopen_condition") else ""
    review = f" [{check}]" if check else ""
    return f"{d['id']} {d['kind']}:{d['action']}{status} — {d['rationale']}{reopen}{review}"


def report(store: Store, out: str | None = None, title: str | None = None) -> dict:
    """A progress or stage report generated from the records into research/reports/."""
    with store.read() as tx:
        meta = tx.meta()
        hyps, decisions = labels.ordered(tx.all("Hypothesis")), tx.all("Decision")
        exps, evidence, papers = labels.ordered(tx.all("Experiment")), tx.all("Evidence"), tx.all("Paper")
        known = labels.names(tx)
        tickets = tx.all("Ticket")
        usage = usage_summary(tx)
        exp_lines = []
        for exp in exps:
            exp_lines.append(f"### {labels.name(exp)} {exp['title']}\n\n- stage {exp.get('stage') or '-'}, contract "
                             f"{exp.get('contract_ref') or 'draft (not sealed)'}"
                             + (" — revised after results (exploratory)" if exp.get("revised_after_results") else "")
                             + f"\n- question: {exp['question']}")
            exp_lines += _arm_table(tx, store, exp)
        ev_lines = [f"- {evidence_line(tx, ev)}" for ev in evidence]
        stage = [d for d in decisions if d["kind"] == "stage"]
        checks = conclusion_checks(tx, decisions)
    lines = [f"# {title or 'Research report'}: {meta.get('title', '')}",
             f"\nGenerated {datetime.now(timezone.utc).isoformat(timespec='seconds')} from the research records "
             f"(data_version {meta['data_version']}, plan {meta.get('plan_ref') or '-'}). Regenerate with `rl report`.",
             "\n## Hypotheses\n"]
    lines += [f"- {brief(h)}\n  - {h['statement']}" + (f"\n  - scope: {h['scope']}" if h.get("scope") else "") for h in hyps] or ["none"]
    lines += ["\n## Experiments and runs\n", *(exp_lines or ["none"])]
    lines += ["\n## Evidence\n", *(ev_lines or ["none"])]
    lines += ["\n## Decisions\n"]
    lines += [f"- {decision_line(d, checks.get(d['id']))}" for d in decisions] or ["none"]
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
    path = store.project.abs(out) if out else store.project.dir("reports") / f"report-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(labels.relabel("\n".join(lines), known) + "\n", encoding="utf-8")
    return {"path": str(path), "summary": f"wrote {store.project.rel(path)}"}
