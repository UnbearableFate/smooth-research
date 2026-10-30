"""Review and advice tickets on one shared call mechanism, with separate packages and answer protocols.

Code reviews are keyed by a scope fingerprint (code, configuration with whitelisted keys removed, dependencies
and the scope text); conclusion reviews by the package built from the records behind a proposed key conclusion.
Either is reused only while its key is unchanged. The reviewer comes from a different model family than the
authors; when none is available the ticket stays pending or unavailable — never a fake pass.
Advice requests are deduplicated by question, evidence version, constraints and advisor configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from . import adapters, documents
from .context import advice_pack, _contract
from .research import brief
from .store import Store, Tx
from .util import RLError, digest_file, digest_json, now_utc, short
from .view import _arm_table

INLINE_FILE_CHARS = 60_000
INLINE_TOTAL_CHARS = 240_000

REVIEW_PROTOCOL = """## What to return

Review the code against the method definition. For each problem give: location (file:line), the evidence
that reproduces or demonstrates it, its impact (result_error | invalid_experiment | resource_waste |
nonblocking), the minimal fix, and how to verify the fix. State what you checked and what you did not.
Do not restate the package. End with exactly one ```json block:

```json
{"verdict": "pass | issues | blocking",
 "findings": [{"location": "", "evidence": "", "impact": "", "fix": "", "verify": ""}],
 "checked": [""], "unchecked": [""]}
```
"""

CONCLUSION_PROTOCOL = """## What to return

Check whether the records support the proposed conclusion. In particular: is each piece of evidence at the
level it claims (diagnostic, screening, exploratory, confirmatory) given what was actually run (arms, seeds,
results); does the conclusion stay inside the scope of that evidence (data, model, size, conditions); does a
confirmation rest on a comparison fixed before the results (contract revisions); are engineering failures or
failed controls counted as refutations, or one cheap failure generalized; which competing explanations are
not ruled out; do the summaries match the recorded numbers. For each problem give: location (record id), the
evidence, its impact (overclaim | wrong_level | out_of_scope | unruled_alternative | data_mismatch |
nonblocking), the minimal fix (a weaker wording, a missing control, a re-run) and how to verify it. State what
you checked and what you did not. Do not restate the package. End with exactly one ```json block:

```json
{"verdict": "pass | issues | blocking",
 "findings": [{"location": "", "evidence": "", "impact": "", "fix": "", "verify": ""}],
 "checked": [""], "unchecked": [""]}
```
"""

ADVICE_PROTOCOL = """## What to return

You are the advisor for a research bottleneck. Read the evidence critically; the coordinator's
interpretation may be wrong. Return at most three proposals that differ in mechanism. For each one give:
(1) which unexplained observation it explains, (2) the key difference from routes already tried,
(3) a distinguishing prediction and the controls it needs, (4) the cheapest test and its expected cost,
(5) the result that would make us drop it. Pausing is an acceptable recommendation when no route is worth
its cost. End with exactly one ```json block:

```json
{"proposals": [{"title": "", "explains": "", "difference": "", "prediction": "", "controls": "",
                "cheapest_test": "", "expected_cost": "", "stop_if": ""}],
 "pause_recommended": false}
```
"""


# -- shared ---------------------------------------------------------------------------------------------------


def _entries(store: Store, kind: str) -> list[adapters.Entry]:
    models = store.project.config["models"]
    if kind == "review":
        raw = models["reviewer"]["candidates"]
    else:
        raw = [models["advisor"]["primary"], *models["advisor"].get("fallback", [])]
    return [adapters.Entry.parse(r) for r in raw]


def _relation(author_families: set, observed: str | None, requested: str) -> str:
    fam = adapters.family(observed) if observed else None
    if fam is None:
        return "cross_model_unverified" if adapters.family(requested) not in author_families else "same_family_unverified"
    return "cross_model" if fam not in author_families else "same_family_independent_session"


def _worker_alive(ticket: dict) -> bool:
    pid = (ticket.get("calls") or [{}])[-1].get("pid") if ticket.get("calls") else None
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_background(store: Store, ticket_id: str, timeout: int) -> dict:
    """Run the call in a detached worker process; `rl review show` / `rl advise show` report the outcome."""
    log = store.project.dir("calls") / f"{ticket_id}.worker.log"
    cmd = [sys.executable, "-m", "smooth_research", "--project", str(store.project.root),
           "review" if ticket_id.startswith("RV") else "advise", "run", ticket_id, "--timeout", str(timeout)]
    with open(log, "ab") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=store.project.root)
    with store.write() as tx:
        t = tx.require(ticket_id, "Ticket")
        tx.update(ticket_id, {"status": "running", "calls": (t.get("calls") or []) + [
            {"pid": proc.pid, "started_at": now_utc(), "background": True}]})
    return {"id": ticket_id, "pid": proc.pid, "log": store.project.rel(log),
            "summary": f"{ticket_id} running in background (pid {proc.pid}); check with "
                       f"`rl {'review' if ticket_id.startswith('RV') else 'advise'} show {ticket_id}`"}


def run_ticket(store: Store, ticket_id: str, timeout: int = 1800, only: str | None = None) -> dict:
    """Try the configured entries in order (keeping the family rule for reviews); record every attempt."""
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
        if not t.get("request_ref"):
            raise RLError("INVALID", f"{ticket_id} has no request package; prepare it again")
        request_path = store.project.abs(tx.require(t["request_ref"], "DocRevision")["object_path"])
    if t["status"] == "answered":
        return {"id": ticket_id, "summary": f"{ticket_id} already answered ({t.get('verdict') or 'see answer'})"}
    candidates = [adapters.Entry.parse(c) for c in (t.get("candidates") or [])]
    if only:
        candidates = [adapters.Entry.parse(only)]
    authors = set(filter(None, (adapters.family(m) for m in (t.get("author_models") or []))))
    calls = [c for c in (t.get("calls") or []) if not c.get("background")]
    # The whole package goes in the prompt: the answer must not depend on the callee's file tools or sandbox.
    prompt = (f"You are the independent {'reviewer' if t['kind'] == 'review' else 'advisor'} for a research "
              f"project at {store.project.root}. Everything you need is in the package below; you may read more "
              "project files if your tools work, but do not modify anything. If something you need is missing, "
              "say so (for a review: under 'unchecked'). Answer in the requested format.\n\n"
              + request_path.read_text(encoding="utf-8"))
    manual, answered = None, 0
    for entry in candidates:
        if entry.entry == "manual":
            manual = entry
            break
        log = store.project.dir("calls") / f"{ticket_id}.{entry.entry}.{len(calls) + 1}.log"
        res = adapters.call(entry, prompt, store.project.root, read_only=True, timeout=timeout, log=log)
        if res.ok and not _parsed(t["kind"], res.text):
            answered += 1
            res.ok, res.error = False, ("answer has no parseable result block: "
                                        + " ".join(res.text.split())[:200])
        calls.append({"entry": str(entry), **res.as_dict(), "finished_at": now_utc()})
        if res.ok:
            return _store_answer(store, ticket_id, res.text, str(entry), res.observed_model, res.provider,
                                 res.session_id, res.usage, res.cost_usd, calls, authors)
    status = "pending" if manual else "failed" if answered else "unavailable"
    errors = "; ".join(f"{c['entry']}: {c.get('error')}" for c in calls if c.get("error"))
    with store.write() as tx:
        tx.update(ticket_id, {"status": status, "calls": calls,
                              "error": errors or ("waiting for a manual answer" if manual else "no candidate could be called")})
        tx.event(f"{t['kind']}.{status}", ticket_id, "no usable automatic answer")
    if manual:
        return {"id": ticket_id, "status": status,
                "summary": f"{ticket_id}: submit {store.project.rel(request_path)} to {manual.model} yourself (or ask the "
                           f"user to), then `rl {t['kind'] if t['kind'] == 'review' else 'advise'} import {ticket_id} "
                           "--answer <file> --model-observed <model> --source <where>`"}
    return {"id": ticket_id, "status": status,
            "summary": f"{ticket_id} {status}: {errors}. Recorded as {status} — not a pass; independent low-cost "
                       f"work can continue (raw output in research/calls/)."}


def _parsed(kind: str, text: str) -> bool:
    data = adapters.last_json_block(text) or {}
    if kind == "review":
        return data.get("verdict") in ("pass", "issues", "blocking")
    return isinstance(data.get("proposals"), list)


def _store_answer(store: Store, ticket_id: str, text: str, entry: str, observed: str | None, provider: str | None,
                  session_id: str | None, usage: dict | None, cost: float | None, calls: list, authors: set) -> dict:
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
    folder = "reviews" if t["kind"] == "review" else "advice"
    path = store.project.dir(folder, "reports") / f"{ticket_id}.md"
    header = (f"<!-- {ticket_id} answer · entry {entry} · observed model {observed or 'unverified'} · "
              f"session {session_id or 'unknown'} · received {now_utc()} -->\n\n")
    path.write_text(header + text, encoding="utf-8")
    prep = documents.prepare(store.project, path, f"{t['kind']}_answer")
    data = adapters.last_json_block(text) or {}
    verdict = data.get("verdict") if t["kind"] == "review" else None
    if t["kind"] == "review" and verdict not in ("pass", "issues", "blocking"):
        verdict = "unparsed"
    relation = _relation(authors, observed, entry.split(":", 1)[-1].split("@")[0]) if t["kind"] == "review" else None
    summary = (f"{len(data.get('findings') or [])} finding(s)" if t["kind"] == "review"
               else f"{len(data.get('proposals') or [])} proposal(s)" + (", pause recommended" if data.get("pause_recommended") else ""))
    with store.write() as tx:
        ref = documents.register(tx, prep)
        tx.update(ticket_id, {"status": "answered", "answer_ref": ref, "verdict": verdict, "relation": relation,
                              "requested_entry": entry, "observed_model": observed, "provider": provider,
                              "host_session_id": session_id, "usage": usage,
                              "cost_usd": float(cost) if cost is not None else None, "calls": calls,
                              "summary": summary, "answered_at": now_utc(), "error": None})
        tx.event(f"{t['kind']}.answered", ticket_id, f"{verdict or ''} {summary} via {entry}")
    rel_note = f", {relation}" if relation else ""
    return {"id": ticket_id, "answer_ref": ref, "verdict": verdict, "relation": relation,
            "summary": f"{ticket_id} answered by {observed or entry + ' (model unverified)'}{rel_note}: "
                       f"{verdict + ', ' if verdict else ''}{summary}; read {store.project.rel(path)}"}


def import_answer(store: Store, ticket_id: str, answer: str, model_observed: str | None, source: str,
                  provider: str | None = None, session_id: str | None = None) -> dict:
    """Import an answer obtained outside the adapters (a person in a product UI, another tool)."""
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
    text = Path(answer).read_text(encoding="utf-8")
    authors = set(filter(None, (adapters.family(m) for m in (t.get("author_models") or []))))
    calls = (t.get("calls") or []) + [{"entry": f"manual:{source}", "imported_at": now_utc()}]
    return _store_answer(store, ticket_id, text, f"manual:{source}", model_observed, provider, session_id,
                         None, None, calls, authors)


def show(store: Store, ticket_id: str | None, kind: str) -> dict:
    with store.read() as tx:
        tickets = [tx.require(ticket_id, "Ticket")] if ticket_id else tx.all("Ticket", kind=kind)
    lines = []
    for t in tickets:
        alive = " (worker not running: run again)" if t["status"] == "running" and not _worker_alive(t) else ""
        lines.append(f"{brief(t)}{alive}")
        for key in ("target_id", "relation", "requested_entry", "observed_model", "host_session_id", "summary",
                    "request_ref", "answer_ref", "reused_from", "error", "cost_usd"):
            if t.get(key) not in (None, ""):
                lines.append(f"  {key}: {t[key]}")
    return {"tickets": tickets, "summary": "\n".join(lines) or f"no {kind} tickets"}


# -- reviews --------------------------------------------------------------------------------------------------


def _strip(value, keys: set):
    if isinstance(value, dict):
        return {k: _strip(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_strip(v, keys) for v in value]
    return value


def fingerprint(store: Store, files: list[str], configs: list[str], deps: list[str], ignore_keys: list[str],
                scope: str) -> tuple[str, dict]:
    def digest(p: str) -> str:
        path = store.project.abs(p)
        if not path.is_file():
            raise RLError("NOT_FOUND", f"review scope file not found: {p}")
        return digest_file(path)

    detail = {"files": {store.project.rel(store.project.abs(f)): digest(f) for f in sorted(files)},
              "deps": {store.project.rel(store.project.abs(d)): digest(d) for d in sorted(deps)},
              "configs": {}, "ignored_keys": sorted(ignore_keys), "scope": scope.strip()}
    for c in sorted(configs):
        path = store.project.abs(c)
        digest(c)
        try:
            loaded = (json.loads if path.suffix == ".json" else yaml.safe_load)(path.read_text(encoding="utf-8"))
            detail["configs"][store.project.rel(path)] = digest_json(_strip(loaded, set(ignore_keys)))
        except (ValueError, yaml.YAMLError):
            detail["configs"][store.project.rel(path)] = digest_file(path)  # not structured: whole file counts
    return digest_json(detail), detail


def _eligible(store: Store, author_models: list[str]) -> list[adapters.Entry]:
    authors = set(filter(None, (adapters.family(m) for m in author_models)))
    rule = store.project.config["models"]["reviewer"].get("relation_to_author", "different_family")
    return [e for e in _entries(store, "review") if rule != "different_family" or e.entry == "manual"
            or e.family not in authors]


def _reuse(tx: Tx, key: str, target_text: str | None, targets: list[str], author_models: list[str],
           subject: str) -> dict | None:
    """An answered cross-model review (pass or issues) with the same key answers this request too."""
    for old in tx.all("Ticket", dedupe_key=key):
        if old["kind"] == "review" and old["status"] == "answered" and old.get("verdict") in ("pass", "issues") \
                and str(old.get("relation", "")).startswith("cross_model"):
            rid = tx.next_id("Ticket", "RV")
            tx.create("Ticket", {**{k: old.get(k) for k in ("question", "request_ref", "answer_ref", "requested_entry",
                                                            "requested_model", "observed_model", "provider",
                                                            "host_session_id", "relation", "verdict", "summary",
                                                            "answered_at")},
                                 "id": rid, "kind": "review", "subject": subject, "status": "answered",
                                 "target_id": target_text, "dedupe_key": key, "author_models": list(author_models),
                                 "reused_from": old.get("reused_from") or old["id"]})
            for t in targets:
                tx.link("targets", rid, t)
            tx.event("review.reused", rid, f"{subject} key {short(key)} unchanged since {old['id']}")
            return {"id": rid, "reused_from": old["id"], "verdict": old.get("verdict"),
                    "summary": f"{rid} reuses {old['id']} ({old.get('verdict')}): {subject} unchanged"}
    return None


def request_review(store: Store, target: str | None, files: list[str], question: str, author_models: list[str],
                   configs: list[str] = (), deps: list[str] = (), ignore_keys: list[str] = (), scope: str = "",
                   diff_base: str | None = None, recheck_of: str | None = None) -> dict:
    if not files:
        raise RLError("INVALID", "name the code under review with --files")
    if not author_models:
        raise RLError("INVALID", "name the model(s) that wrote this code with --author-model; the reviewer must "
                                 "come from a different family")
    fp, detail = fingerprint(store, files, list(configs), list(deps), list(ignore_keys), scope or question)
    authors = set(filter(None, (adapters.family(m) for m in author_models)))
    eligible = _eligible(store, author_models)
    with store.write() as tx:
        if target:
            tx.require(target)
        reused = None if recheck_of else _reuse(tx, fp, target, [target] if target else [], author_models, "code")
        if reused:
            return reused
        rid = tx.next_id("Ticket", "RV")
        tx.create("Ticket", {"id": rid, "kind": "review", "subject": "code",
                             "status": "pending" if eligible else "unavailable",
                             "target_id": target, "question": question, "dedupe_key": fp,
                             "candidates": [str(e) for e in eligible], "author_models": list(author_models),
                             "recheck_of": recheck_of,
                             "error": None if eligible else f"no configured reviewer outside families {sorted(authors)}"})
        if target:
            tx.link("targets", rid, target)
        contract = ""
        if target and tx.require(target)["_label"] == "Experiment":
            contract = _contract(tx, store, tx.require(target))
        prior = brief(tx.require(recheck_of)) if recheck_of else ""
    path = store.project.dir("packages") / f"{rid}.request.md"
    path.write_text(_review_package(store, rid, target, question, scope, contract, detail, diff_base, prior),
                    encoding="utf-8")
    prep = documents.prepare(store.project, path, "review_request")
    with store.write() as tx:
        ref = documents.register(tx, prep)
        tx.update(rid, {"request_ref": ref})
        tx.event("review.requested", rid, f"{target or '-'} fingerprint {short(fp)}")
    note = (f"candidates {', '.join(str(e) for e in eligible)}; run it: rl review run {rid} [--background]"
            if eligible else "no eligible reviewer configured: recorded unavailable (not a pass)")
    return {"id": rid, "request_ref": ref, "fingerprint": fp, "summary": f"{rid} {note}"}


def _review_package(store: Store, rid: str, target: str | None, question: str, scope: str, contract: str,
                    detail: dict, diff_base: str | None, prior: str) -> str:
    parts = [f"# Review package {rid}\n",
             f"Independent code review for a research experiment. Question: {question}\n",
             f"Scope: {scope or question}\n"]
    if prior:
        parts.append(f"This re-checks only what the earlier review raised, after fixes: {prior}\n")
    if contract:
        parts.append(f"## Method definition\n\n{contract}\n")
    budget = [INLINE_TOTAL_CHARS]

    def inline(paths: list[str]) -> str:
        """The exact content under review, so the package binds to this snapshot and needs no file tools."""
        blocks = []
        for p in paths:
            text = store.project.abs(p).read_text(encoding="utf-8", errors="replace")
            if len(text) > min(INLINE_FILE_CHARS, budget[0]):
                blocks.append(f"### {p}\n\n(not inlined: {len(text)} characters; read the file at this path)")
                continue
            budget[0] -= len(text)
            fence = "````" if "```" in text else "```"
            blocks.append(f"### {p}\n\n{fence}{Path(p).suffix.lstrip('.')}\n{text.rstrip()}\n{fence}")
        return "\n\n".join(blocks)

    parts.append("## Files under review\n\n" + inline(list(detail["files"])))
    if detail["configs"]:
        parts.append("\n## Execution configuration\n\n" + inline(list(detail["configs"]))
                     + (f"\n\n(keys allowed to vary without a new review: {', '.join(detail['ignored_keys'])})"
                        if detail["ignored_keys"] else ""))
    if detail["deps"]:
        parts.append("\n## Key dependencies\n\n" + "\n".join(f"- {p}" for p in detail["deps"]))
    if diff_base:
        try:
            diff = subprocess.run(["git", "diff", diff_base, "--", *detail["files"]], cwd=store.project.root,
                                  capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            diff = ""
        if diff:
            parts.append(f"\n## Diff against {diff_base}\n\n```diff\n{diff[:40000]}\n```")
    parts.append("\n" + REVIEW_PROTOCOL)
    return "\n".join(parts)


CONCLUSION_QUESTION = "Do the records support this conclusion, at this evidence level and within this scope?"


def request_conclusion_review(store: Store, targets: list[str], proposal: str, author_models: list[str],
                              question: str | None = None, recheck_of: str | None = None) -> dict:
    """A read-only check of a key conclusion before it is recorded, built from the records behind it."""
    if not targets:
        raise RLError("INVALID", "name the hypotheses the conclusion is about with --target")
    if not (proposal or "").strip():
        raise RLError("INVALID", "state the conclusion you plan to record with --proposal")
    if not author_models:
        raise RLError("INVALID", "name the model(s) that drew this conclusion with --author-model; the reviewer "
                                 "must come from a different family")
    question = question or CONCLUSION_QUESTION
    eligible = _eligible(store, author_models)
    authors = set(filter(None, (adapters.family(m) for m in author_models)))
    with store.read() as tx:
        prior = brief(tx.require(recheck_of, "Ticket")) if recheck_of else ""
        body = _conclusion_body(tx, store, targets, proposal, question, prior)
    key = digest_json({"subject": "conclusion", "body": body})
    target_text = ", ".join(targets)
    error = None if eligible else f"no configured reviewer outside families {sorted(authors)}"
    with store.write() as tx:
        reused = None if recheck_of else _reuse(tx, key, target_text, targets, author_models, "conclusion")
        if reused:
            return reused
        rid = tx.next_id("Ticket", "RV")
        tx.create("Ticket", {"id": rid, "kind": "review", "subject": "conclusion",
                             "status": "pending" if eligible else "unavailable", "target_id": target_text,
                             "question": question, "dedupe_key": key, "candidates": [str(e) for e in eligible],
                             "author_models": list(author_models), "recheck_of": recheck_of, "error": error})
        for t in targets:
            tx.link("targets", rid, t)
    path = store.project.dir("packages") / f"{rid}.request.md"
    path.write_text(f"# Conclusion review package {rid}\n\n{body}\n{CONCLUSION_PROTOCOL}", encoding="utf-8")
    prep = documents.prepare(store.project, path, "review_request")
    with store.write() as tx:
        ref = documents.register(tx, prep)
        tx.update(rid, {"request_ref": ref})
        tx.event("review.requested", rid, f"conclusion on {target_text} key {short(key)}")
    note = (f"candidates {', '.join(str(e) for e in eligible)}; run it: rl review run {rid} [--background], then "
            f"cite it: rl decide ... --based-on {rid}"
            if eligible else "no eligible reviewer configured: recorded unavailable (not a pass)")
    return {"id": rid, "request_ref": ref, "summary": f"{rid} {note}"}


def _result_line(res: dict) -> str:
    m = res.get("manifest") or {}
    shown = json.dumps({k: m[k] for k in ("metric", "metrics", "data_revision", "validator") if k in m},
                       ensure_ascii=False, sort_keys=True)
    flags = []
    if res.get("spec_match") is False:
        flags.append(f"differs from its RunSpec: {res.get('mismatches')}")
    if res.get("usable_for_planned_comparison") is False:
        flags.append("not usable for the pre-specified comparison")
    if res["status"] == "rejected":
        flags.append(f"rejected: {res.get('rejected_reason')}")
    return (f"- result {res['id']}@{res['revision']} [{res['status']}] {shown[:3000]}"
            + (f" ({'; '.join(flags)})" if flags else ""))


def _conclusion_body(tx: Tx, store: Store, targets: list[str], proposal: str, question: str, prior: str) -> str:
    """Everything the records hold behind the conclusion; deterministic, so its digest is the reuse key."""
    parts = ["Independent check of a research conclusion before it is recorded. The records below are what the "
             f"project knows about it.\n\nQuestion: {question}\n"]
    if prior:
        parts.append(f"This re-checks the conclusion after changes made in response to: {prior}\n")
    parts.append(f"## Proposed conclusion\n\n{proposal.strip()}\n")
    experiments: dict[str, dict] = {}
    for hid in targets:
        h = tx.require(hid, "Hypothesis")
        lines = [f"## Hypothesis {brief(h)}\n", f"statement: {h['statement']}"]
        lines += [f"scope: {h['scope']}"] if h.get("scope") else []
        evidence = [(e, tx.require(e["id"])) for e in tx.inc(hid) if e["rel"] in ("supports", "challenges")]
        lines.append("" if evidence else "\nNo evidence is linked to this hypothesis.")
        for edge, ev in evidence:
            bound = "" if edge.get("revision") == h["revision"] else f", bound to revision {edge.get('revision')}"
            lines.append(f"### {ev['id']} {edge['rel']} {hid}{bound}: [{ev['evidence_level']}/{ev['validity']}] "
                         f"{ev['summary']}")
            lines += [f"- scope: {ev['scope']}"] if ev.get("scope") else []
            lines += [f"- validity reason: {ev['validity_reason']}"] if ev.get("validity_reason") else []
            for e in tx.out(ev["id"]):
                if e["rel"] == "about":
                    experiments.setdefault(e["id"], tx.require(e["id"]))
                elif e["rel"] == "from_attempt":
                    att = tx.require(e["id"])
                    spec = tx.require(att["run_id"])["spec"]
                    lines.append(f"- attempt {brief(att)} arm {spec.get('arm')} seed {spec.get('seed')}")
                elif e["rel"] == "from_result":
                    lines.append(_result_line(tx.require(e["id"])))
        for e in tx.inc(hid, "tests"):
            experiments.setdefault(e["id"], tx.require(e["id"]))
        decisions = [brief(tx.require(e["id"])) for e in tx.inc(hid, "decides")]
        lines += ["", "Decisions so far: " + ("; ".join(decisions) if decisions else "none")]
        parts.append("\n".join(lines) + "\n")
    for exp in experiments.values():
        lines = [f"## Experiment {_contract(tx, store, exp)}"]
        if exp.get("revised_after_results"):
            lines.append("The contract was revised after results: later comparisons are exploratory.")
        lines += _arm_table(tx, exp["id"])
        code = [tx.require(e["id"]) for e in tx.inc(exp["id"], "targets") if e["label"] == "Ticket"]
        code = [f"{brief(t)}" + (f" ({t['relation']})" if t.get("relation") else "")
                for t in code if t["kind"] == "review" and t.get("subject") != "conclusion"]
        lines.append("Code reviews: " + ("; ".join(code) if code else "none recorded"))
        parts.append("\n".join(lines) + "\n")
    others = [h for h in tx.all("Hypothesis") if h["id"] not in targets and h["disposition"] in ("active", "candidate")]
    parts.append("## Other open hypotheses (competing explanations)\n\n"
                 + ("\n".join(f"- {brief(h)}: {h['statement']}" for h in others) if others else "none") + "\n")
    return "\n".join(parts)


# -- advice ---------------------------------------------------------------------------------------------------


def _evidence_version(tx: Tx) -> str:
    state = [(ev["id"], ev["validity"], [(e["rel"], e["id"], e.get("revision")) for e in tx.out(ev["id"])])
             for ev in tx.all("Evidence")]
    hyps = [(h["id"], h["revision"], h["disposition"], h["evidence_status"]) for h in tx.all("Hypothesis")]
    return digest_json({"evidence": state, "hypotheses": hyps})


def prepare_advice(store: Store, question: str, target: str | None = None) -> dict:
    constraints = store.project.abs(store.project.config["constraints_ref"])
    with store.read() as tx:
        if target:
            tx.require(target)
        key = digest_json({"question": question.strip(), "target": target, "evidence": _evidence_version(tx),
                           "constraints": digest_file(constraints) if constraints.exists() else None,
                           "advisor": store.project.config["models"]["advisor"]})
        for old in tx.all("Ticket", dedupe_key=key):
            if old["kind"] == "advice":
                return {"id": old["id"], "duplicate": True,
                        "summary": f"{old['id']} already asks this with the same evidence ({old['status']}); "
                                   "send only new data, or wait for new evidence"}
    pack = advice_pack(store, target, question)
    with store.write() as tx:
        aid = tx.next_id("Ticket", "AD")
        tx.create("Ticket", {"id": aid, "kind": "advice", "status": "pending", "target_id": target,
                             "question": question, "dedupe_key": key,
                             "candidates": [str(e) for e in _entries(store, "advice")]})
        if target:
            tx.link("targets", aid, target)
    path = store.project.dir("packages") / f"{aid}.request.md"
    path.write_text(pack["summary"] + "\n\n" + ADVICE_PROTOCOL, encoding="utf-8")
    prep = documents.prepare(store.project, path, "advice_request")
    with store.write() as tx:
        ref = documents.register(tx, prep)
        tx.update(aid, {"request_ref": ref})
        tx.event("advice.prepared", aid, question)
    return {"id": aid, "request_ref": ref, "tokens": pack["tokens"],
            "summary": f"{aid} package {store.project.rel(path)} (≈{pack['tokens']} tokens); "
                       f"run: rl advise run {aid} [--background], or submit it manually"}
