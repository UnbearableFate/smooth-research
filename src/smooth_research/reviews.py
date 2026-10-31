"""Review and advice tickets on one shared call mechanism, with separate packages and answer protocols.

Code reviews are keyed by a scope fingerprint (code, configuration with whitelisted keys removed, dependencies
and the scope text); conclusion reviews by the package built from the records behind a proposed key conclusion.
Either is reused only while its key is unchanged. A code review also keeps its scope, so registering a run can
tell whether the reviews still cover the code it executes. Reviewers are tried in the configured order whatever
model wrote the code; the relation to the authors' model family is recorded, not enforced. When no reviewer is
available the ticket stays pending or unavailable — never a fake pass.
Advice comes in rounds: every configured advisor gets its own ticket on the same package, and a project has a
limited number of rounds (advice.max_rounds) unless the user approves more. A round is deduplicated by
question, evidence version, constraints and advisor configuration.
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
from .project import advice_limit, model_entries
from .research import brief
from .runs import code_review_summary, evidence_attempts
from .store import Store, Tx
from .util import RLError, digest_bytes, digest_file, digest_json, now_utc, short
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
    role = "reviewer" if kind == "review" else "advisor"
    return [adapters.Entry.parse(r) for r in model_entries(store.project.config, role)]


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


def _spawn(store: Store, ticket_id: str, timeout: int) -> tuple[subprocess.Popen, Path]:
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
    return proc, log


def start_background(store: Store, ticket_id: str, timeout: int) -> dict:
    """Run the call in a detached worker process; `rl review show` / `rl advise show` report the outcome."""
    proc, log = _spawn(store, ticket_id, timeout)
    return {"id": ticket_id, "pid": proc.pid, "log": store.project.rel(log),
            "summary": f"{ticket_id} running in background (pid {proc.pid}); check with "
                       f"`rl {'review' if ticket_id.startswith('RV') else 'advise'} show {ticket_id}`"}


def run_many(store: Store, ticket_ids: list[str], timeout: int, background: bool = False) -> dict:
    """Each ticket in its own worker process at the same time (the advisors of a round answer independently);
    without --background, wait for all of them."""
    started = [(tid, *_spawn(store, tid, timeout)) for tid in ticket_ids]
    if not background:
        for _, proc, _ in started:
            try:
                proc.wait(timeout=timeout + 60)
            except subprocess.TimeoutExpired:
                pass  # the worker records its own timeout; show what is recorded so far
    with store.read() as tx:
        tickets = [tx.require(tid, "Ticket") for tid, _, _ in started]
    lines = [f"{brief(t)}" + (f" (pid {proc.pid}, log {store.project.rel(log)})" if background else "")
             for t, (_, proc, log) in zip(tickets, started)]
    return {"tickets": [t["id"] for t in tickets], "summary": "\n".join(lines)}


def run_ticket(store: Store, ticket_id: str, timeout: int = 1800, only: str | None = None) -> dict:
    """Try the ticket's entries in order and record every attempt; the first parseable answer is kept."""
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
                       f"work can continue (raw output in {store.project.rel(store.project.dir('calls'))}/)."}


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


def _config_digest(path: str, data: bytes, ignore_keys: list[str]) -> str:
    try:
        loaded = (json.loads if Path(path).suffix == ".json" else yaml.safe_load)(data.decode("utf-8"))
        return digest_json(_strip(loaded, set(ignore_keys)))
    except (ValueError, yaml.YAMLError):
        return digest_bytes(data)  # not structured: the whole file counts


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
        detail["configs"][store.project.rel(path)] = _config_digest(c, path.read_bytes(), ignore_keys)
    return digest_json(detail), detail


def _reuse(tx: Tx, key: str, target_text: str | None, targets: list[str], author_models: list[str],
           subject: str, extra: dict | None = None) -> dict | None:
    """An answered review (pass or issues) with the same key answers this request too."""
    for old in tx.all("Ticket", dedupe_key=key):
        if old["kind"] == "review" and old["status"] == "answered" and old.get("verdict") in ("pass", "issues"):
            rid = tx.next_id("Ticket", "RV")
            tx.create("Ticket", {**{k: old.get(k) for k in ("question", "request_ref", "answer_ref", "requested_entry",
                                                            "requested_model", "observed_model", "provider",
                                                            "host_session_id", "relation", "verdict", "summary",
                                                            "answered_at")},
                                 "id": rid, "kind": "review", "subject": subject, "status": "answered",
                                 "target_id": target_text, "dedupe_key": key, "author_models": list(author_models),
                                 "reused_from": old.get("reused_from") or old["id"], **(extra or {})})
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
        raise RLError("INVALID", "name the model(s) that wrote this code with --author-model (recorded with the "
                                 "review)")
    fp, detail = fingerprint(store, files, list(configs), list(deps), list(ignore_keys), scope or question)
    eligible = _entries(store, "review")
    with store.write() as tx:
        if target:
            tx.require(target)
        reused = None if recheck_of else _reuse(tx, fp, target, [target] if target else [], author_models, "code",
                                                {"code_scope": detail})
        if reused:
            return reused
        rid = tx.next_id("Ticket", "RV")
        tx.create("Ticket", {"id": rid, "kind": "review", "subject": "code",
                             "status": "pending" if eligible else "unavailable",
                             "target_id": target, "question": question, "dedupe_key": fp,
                             "candidates": [str(e) for e in eligible], "author_models": list(author_models),
                             "recheck_of": recheck_of, "code_scope": detail,
                             "error": None if eligible else "no reviewer configured (models.reviewer is empty)"})
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


# -- code review coverage of a run ----------------------------------------------------------------------------


def _code_reviews(tx: Tx, experiment: str, named: list[str]) -> list[dict]:
    """Code reviews of the experiment and of the experiments its code comes from, plus the ones named."""
    exps, todo = set(), [experiment]
    while todo:
        eid = todo.pop()
        if eid not in exps:
            exps.add(eid)
            todo += [e["id"] for e in tx.out(eid, "code_from")]
    ids = {e["id"] for eid in exps for e in tx.inc(eid, "targets") if e["label"] == "Ticket"}
    for rid in named:
        t = tx.require(rid, "Ticket")
        if t["kind"] != "review" or t.get("subject") == "conclusion":
            raise RLError("INVALID", f"{rid} is not a code review")
        ids.add(rid)
    tickets = [tx.require(i) for i in sorted(ids)]
    return [t for t in tickets if t["kind"] == "review" and t.get("subject") != "conclusion"]


def _code_reader(store: Store, code_ref) -> tuple:
    """Files as the run executes them: from the commit its RunSpec names, otherwise from the working tree."""
    commit = code_ref.get("commit") if isinstance(code_ref, dict) else None
    if not commit:
        def read_tree(p: str) -> bytes | None:
            path = store.project.abs(p)
            return path.read_bytes() if path.is_file() else None
        return read_tree, "working tree"

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=store.project.root, capture_output=True, timeout=30)

    try:
        found = git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        found = False
    if not found:
        return None, f"commit {commit[:12]} is not in this repository"

    def read_commit(p: str) -> bytes | None:
        out = git("cat-file", "blob", f"{commit}:./{p}")
        return out.stdout if out.returncode == 0 else None
    return read_commit, f"commit {commit[:12]}"


def code_coverage(store: Store, experiment: str, code_ref, named: list[str] = (), waived: str | None = None) -> dict:
    """Do answered code reviews still cover the code a run of this experiment executes?

    Every path a passing review covered must still match, in that code, the version at least one passing
    review saw (configs compared without the keys that review allowed to vary). The result is recorded on the
    attempt; nothing is blocked, because only the researcher knows whether the run needs a review.
    """
    if waived is not None:
        if not waived.strip():
            raise RLError("INVALID", "--no-review needs the reason this run needs no code review")
        return {"status": "waived", "reason": waived.strip()}
    with store.read() as tx:
        tickets = _code_reviews(tx, experiment, list(named))
    if not tickets:
        return {"status": "none"}
    passing = [t for t in tickets if t["status"] == "answered" and t.get("verdict") in ("pass", "issues")]
    others = [t for t in tickets if t not in passing]
    if not passing:
        return {"status": "not_passed", "open": [f"{t['id']} {t.get('verdict') or t['status']}" for t in others]}
    scoped = [t for t in passing if t.get("code_scope")]
    if not scoped:
        return {"status": "unchecked", "reviews": [f"{t['id']} {t['verdict']}" for t in passing],
                "reason": f"{', '.join(t['id'] for t in passing)} recorded no scope; request it again with the "
                          "same scope to record one (an unchanged scope reuses the answer)"}
    read, source = _code_reader(store, code_ref)
    if read is None:
        return {"status": "unchecked", "reviews": [f"{t['id']} {t['verdict']}" for t in scoped], "reason": source}

    def current(path: str, kind: str, ignored: list[str]) -> str | None:
        data = read(path)
        if data is None:
            return None
        return _config_digest(path, data, ignored) if kind == "configs" else digest_bytes(data)

    seen: dict[str, list[str]] = {}  # path -> passing reviews that saw the version this run executes
    older: dict[str, list[str]] = {}  # path -> passing reviews that saw another version
    try:
        for t in scoped:
            scope = t["code_scope"]
            for kind in ("files", "deps", "configs"):
                for path, digest in (scope.get(kind) or {}).items():
                    now = current(path, kind, scope.get("ignored_keys") or [])
                    (seen if now == digest else older).setdefault(path, []).append(t["id"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unchecked", "reviews": [f"{t['id']} {t['verdict']}" for t in scoped],
                "reason": f"could not read the {source}: {exc}"}
    changed = {p: ids for p, ids in sorted(older.items()) if p not in seen}
    used = {i for ids in seen.values() for i in ids}
    out = {"status": "stale" if changed else "covered", "source": source,
           "reviews": [f"{t['id']} {t['verdict']}" for t in scoped if t["id"] in used]}
    if changed:
        out["changed"] = changed
    pending = [f"{t['id']} {t['status']}" for t in others if t["status"] in ("pending", "running")]
    if pending:
        out["open"] = pending
    return out


CONCLUSION_QUESTION = "Do the records support this conclusion, at this evidence level and within this scope?"


def request_conclusion_review(store: Store, targets: list[str], proposal: str, author_models: list[str],
                              question: str | None = None, recheck_of: str | None = None) -> dict:
    """A read-only check of a key conclusion before it is recorded, built from the records behind it."""
    if not targets:
        raise RLError("INVALID", "name the hypotheses the conclusion is about with --target")
    if not (proposal or "").strip():
        raise RLError("INVALID", "state the conclusion you plan to record with --proposal")
    if not author_models:
        raise RLError("INVALID", "name the model(s) that drew this conclusion with --author-model (recorded with the "
                                 "review)")
    question = question or CONCLUSION_QUESTION
    eligible = _entries(store, "review")
    with store.read() as tx:
        prior = brief(tx.require(recheck_of, "Ticket")) if recheck_of else ""
        body = _conclusion_body(tx, store, targets, proposal, question, prior)
    key = digest_json({"subject": "conclusion", "body": body})
    target_text = ", ".join(targets)
    error = None if eligible else "no reviewer configured (models.reviewer is empty)"
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
            if code := code_review_summary(evidence_attempts(tx, ev["id"])):
                lines.append(f"- code review at registration: {code}")
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


def advice_rounds(tx: Tx) -> list:
    """Advice rounds that count toward advice.max_rounds: those with an answer or still waiting for one (a round
    no advisor could answer does not count). Tickets from before rounds existed are one round each."""
    rounds: dict = {}
    for t in tx.all("Ticket", kind="advice"):
        rounds.setdefault(t.get("advice_round") or t["id"], []).append(t["status"])
    return [r for r, statuses in rounds.items() if set(statuses) & {"answered", "pending", "running"}]


def prepare_advice(store: Store, question: str, target: str | None = None, user_approved: str | None = None) -> dict:
    """One advice round: a package for every configured advisor, each on its own ticket, to be asked at once."""
    constraints = store.project.abs(store.project.config["constraints_ref"])
    entries = _entries(store, "advice")
    if not entries:
        raise RLError("INVALID", "no advisor configured: list model entries under models.advisor")
    limit = advice_limit(store.project.config)
    with store.read() as tx:
        if target:
            tx.require(target)
        key = digest_json({"question": question.strip(), "target": target, "evidence": _evidence_version(tx),
                           "constraints": digest_file(constraints) if constraints.exists() else None,
                           "advisor": [str(e) for e in entries]})
        same = [t for t in tx.all("Ticket", dedupe_key=key) if t["kind"] == "advice"]
        if same:
            ids = [t["id"] for t in same]
            return {"ids": ids, "duplicate": True,
                    "summary": f"{', '.join(ids)} already ask this with the same evidence "
                               f"({', '.join(t['status'] for t in same)}); send only new data, or wait for new evidence"}
    pack = advice_pack(store, target, question)
    with store.write() as tx:
        used = len(advice_rounds(tx))
        if used >= limit and not (user_approved or "").strip():
            raise RLError("LIMIT", f"{used} of {limit} advice rounds used (advice.max_rounds); report to the user "
                                   "why another round is needed, and if they agree pass their approval: "
                                   "--user-approved \"<what the user said>\"")
        number = 1 + max([t.get("advice_round") or 0 for t in tx.all("Ticket", kind="advice")], default=0)
        ids = []
        for entry in entries:
            aid = tx.next_id("Ticket", "AD")
            tx.create("Ticket", {"id": aid, "kind": "advice", "status": "pending", "target_id": target,
                                 "question": question, "dedupe_key": key, "candidates": [str(entry)],
                                 "advice_round": number, "user_approved": (user_approved or "").strip() or None})
            if target:
                tx.link("targets", aid, target)
            ids.append(aid)
    path = store.project.dir("packages") / f"advice-round-{number}.request.md"
    path.write_text(pack["summary"] + "\n\n" + ADVICE_PROTOCOL, encoding="utf-8")
    prep = documents.prepare(store.project, path, "advice_request")
    with store.write() as tx:
        ref = documents.register(tx, prep)
        for aid in ids:
            tx.update(aid, {"request_ref": ref})
        tx.event("advice.prepared", ids[0], f"round {number} ({len(ids)} advisors)"
                 + (f"; beyond the limit, approved by the user: {user_approved.strip()}" if used >= limit else "")
                 + f": {question}")
    advisors = ", ".join(f"{aid} {entry}" for aid, entry in zip(ids, entries))
    return {"ids": ids, "round": number, "request_ref": ref, "tokens": pack["tokens"],
            "summary": f"advice round {number} ({used + 1} of {limit} used): {advisors}; package "
                       f"{store.project.rel(path)} (≈{pack['tokens']} tokens); ask them all at once: "
                       f"rl advise run {' '.join(ids)} [--background]"}
