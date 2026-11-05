"""Review and advice tickets on one shared call mechanism, with separate packages and answer protocols.

Code reviews are keyed by the fingerprint of their scope (scope.py, which also tells whether the reviews still
cover the code a run executes); conclusion reviews by the package built from the records behind a proposed key
conclusion. Either is reused only while its key is unchanged. Reviewers are tried in the configured order whatever
model wrote the code; the relation to the authors' model family is recorded, not enforced. When no reviewer is
available the ticket stays pending or unavailable — never a fake pass.
A review round holds at most two reviews: the first and one re-check after fixes. A review passes when no
finding of medium or higher severity remains; when such findings remain after the second, the round goes to
the coordinator, who approves it or returns the work (a further review then starts a new round, with the
coordinator's reason). Answers come from a headless call (rl review run), a live reviewer session in a herdr
tab (rl review answer), or an import; on every path a review answer is final (a further review is a re-check).
Advice comes in rounds: every configured advisor gets its own ticket on the same package, and a project has a
limited number of rounds (advice.max_rounds) unless the user approves more. A round is deduplicated by its
package and advisors: the same question on the same records gives the same package. A live advisor may revise
its answer.
A ticket is created in the write that seals its request package, so that no ticket is left without one (a package
file left by a failed write is overwritten by the next request).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import adapters, documents, messages, usage
from .agents import assign
from .context import advice_pack, contract_summary
from .herdr import caller_role
from .project import advice_limit, usable_entries
from .research import brief, passing_rechecks, prepare_contracts, review_mark, review_passes, seal_referenced
from .runs import code_review_summary, evidence_attempts
from .schema import ENUMS
from .scope import changed_since_request, fingerprint
from .store import Store, Tx, _sort_key
from .util import RLError, digest_json, now_utc, short
from .view import _arm_table

REVIEWS_PER_ROUND = 2
SEVERITIES = ENUMS["severity"]  # in rising order
IMPACT_SEVERITY = {"result_error": "high", "invalid_experiment": "high", "overclaim": "high", "data_mismatch": "high",
                   "resource_waste": "medium", "wrong_level": "medium", "out_of_scope": "medium",
                   "unruled_alternative": "medium", "nonblocking": "low"}

REVIEW_PROTOCOL = """## What to return

Review the code against the method definition. For each problem give: location (file:line), the evidence
that reproduces or demonstrates it, its impact (result_error | invalid_experiment | resource_waste |
nonblocking), its severity, the minimal fix (a suggested patch is welcome; you do not edit files yourself),
and how to verify the fix. Severity: high = the result would be wrong or the experiment invalid; medium =
likely to distort the result or waste much of the budget, to be fixed or explicitly accepted; low = worth
fixing, does not change the conclusion. State what you checked and what you did not. Do not restate the
package. End with exactly one ```json block:

```json
{"verdict": "pass | issues | blocking",
 "findings": [{"location": "", "evidence": "", "impact": "", "severity": "high | medium | low", "fix": "",
               "verify": ""}],
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
nonblocking), its severity, the minimal fix (a weaker wording, a missing control, a re-run) and how to verify
it. Severity: high = the conclusion is not supported as stated; medium = it needs a weaker wording, a narrower
scope or a missing control before it is recorded; low = a wording or presentation point. State what you
checked and what you did not. Do not restate the package. End with exactly one ```json block:

```json
{"verdict": "pass | issues | blocking",
 "findings": [{"location": "", "evidence": "", "impact": "", "severity": "high | medium | low", "fix": "",
               "verify": ""}],
 "checked": [""], "unchecked": [""]}
```
"""

ADVICE_PROTOCOL = """## What to return

You are one of the advisors of this research project. Other advisors answer the same package on their own,
and the coordinator weighs the answers together. Read the evidence critically: the coordinator's
interpretation may be wrong.

1. Assess the state: what the evidence supports, at which level and in which scope, and what it does not.
2. Explore the related research broadly (search the web and the literature if your tools allow). Name each
   source with a locator (arXiv id, DOI or URL and section), and say whether you checked the claim yourself.
3. Name the bottleneck: what keeps the project from its next decision.
4. Propose at most three improvements that differ in mechanism. For each one give: (1) which unexplained
   observation it explains, (2) the key difference from routes already tried, (3) a distinguishing prediction
   and the controls it needs, (4) the cheapest test and its expected cost, (5) the result that would make us
   drop it.
5. Recommend continuing (with your proposals), pausing, or stopping this line of work, and say why. Pausing or
   stopping is a valid recommendation when no route is worth its cost.

End with exactly one ```json block:

```json
{"assessment": "", "bottleneck": "",
 "related_work": [{"title": "", "locator": "", "relevance": "", "checked": false}],
 "proposals": [{"title": "", "explains": "", "difference": "", "prediction": "", "controls": "",
                "cheapest_test": "", "expected_cost": "", "stop_if": ""}],
 "recommendation": "continue | pause | stop", "pause_recommended": false}
```
"""


# -- shared ---------------------------------------------------------------------------------------------------


def _entries(store: Store, kind: str) -> list[adapters.Entry]:
    return usable_entries(store.project.config, "reviewer" if kind == "review" else "advisor")


def _relation(author_models: list[str], observed: str | None, requested: str) -> str:
    """How the answering model relates to the models that wrote the code or drew the conclusion; the same model
    name is the same family even when the family table does not know it."""
    families = {adapters.family(m) for m in author_models} - {None}
    names = {m.lower().rsplit("/", 1)[-1] for m in author_models if m}
    if (observed or requested).lower().rsplit("/", 1)[-1] in names:
        return "same_family_independent_session" if observed else "same_family_unverified"
    fam = adapters.family(observed) if observed else None
    if fam is None:
        return "cross_model_unverified" if adapters.family(requested) not in families else "same_family_unverified"
    return "cross_model" if fam not in families else "same_family_independent_session"


def open_severity(data: dict) -> str:
    """The highest severity among an answer's findings. A finding without one takes it from its impact, else
    counts as medium; a blocking verdict counts as high."""
    worst = "high" if data.get("verdict") == "blocking" else "none"
    for finding in data.get("findings") or []:
        finding = finding if isinstance(finding, dict) else {}
        sev = str(finding.get("severity") or "").strip().lower()
        if sev not in SEVERITIES[1:]:
            sev = IMPACT_SEVERITY.get(str(finding.get("impact") or "").strip().lower(), "medium")
        worst = max(worst, sev, key=SEVERITIES.index)
    return worst


def _round(tx: Tx, recheck_of: str | None, new_round: str | None) -> dict:
    """Round fields of a new review. A re-check continues the round of the review it re-checks; a round holds
    at most two reviews, and a further one starts a new round with a stated reason."""
    if not recheck_of or (new_round or "").strip():
        return {"round_index": 1, "round_reason": (new_round or "").strip() or None}
    prev = tx.require(recheck_of, "Ticket")
    if prev["kind"] != "review":
        raise RLError("INVALID", f"{recheck_of} is not a review")
    # an answer given while the files changed does not count: its re-check takes its place in the round
    root = prev.get("round_of") or prev["id"]
    held = [t for t in tx.all("Ticket", kind="review", round_of=root) if not t.get("changed_during_review")]
    if len(held) >= REVIEWS_PER_ROUND:
        last = max(held, key=lambda t: t.get("round_index") or 1)["id"]
        raise RLError("LIMIT", f"the round that began with {root} already had its {REVIEWS_PER_ROUND} reviews (the "
                               f"last: {last}). The coordinator approves the remaining findings (`rl review "
                               f"approve {last} --rationale ..`) or returns the work; a further review starts a "
                               f"new round with the coordinator's reason: `--recheck-of {last} --new-round "
                               "\"<reason>\"`")
    return {"round_of": root, "round_index": len(held) + 1}


def _route(store: Store, rid: str, recheck_of: str | None, requester: str | None) -> str:
    """Who takes a new review: the reviewer session that answered the review it re-checks, while that session
    (or one of the same name that replaced it, as rl agent switch does) is open; otherwise, when a live session
    asked for it, the coordinator is asked to assign a reviewer."""
    with store.read() as tx:
        prev = tx.require(recheck_of, "Ticket") if recheck_of else {}
        session = tx.get(prev.get("answered_by") or prev.get("assigned_session") or "", "Session") if prev else None
        if session and session["status"] != "active":
            session = next((x for x in tx.all("Session", status="active") if x["role_id"] == session["role_id"]
                            and x.get("role_kind") == "reviewer"), None)
    if session and session["status"] == "active" and session.get("herdr_agent"):
        return "; " + assign(store, session["role_id"], ticket=rid)["summary"]
    if not requester or requester == "coordinator":
        return ""
    with store.write() as tx:
        msg = messages.record(tx, "NOTE", requester, "coordinator", None,
                              f"assign a reviewer: rl agent open --role reviewer --ticket {rid}, or rl agent assign "
                              f"<reviewer> --ticket {rid}", [], f"{rid} needs a reviewer ({brief(tx.require(rid))})")
    target = messages.notify(store, msg)
    return f"; the coordinator is asked to assign a reviewer ({msg['id']}" + (f", notice sent to {target})" if target else ")")


def _worker(ticket: dict) -> int | None:
    """The pid of another live process that holds the ticket: its claim is the last entry of calls."""
    pid = (ticket.get("calls") or [{}])[-1].get("pid") if ticket.get("status") == "running" else None
    if not pid or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:  # a reused pid belongs to a process that is not running this ticket
        return pid if ticket["id"].encode() in Path(f"/proc/{pid}/cmdline").read_bytes() else None
    except OSError:
        return pid


def _spawn(store: Store, ticket_id: str, timeout: int, entry: str | None = None) -> tuple[subprocess.Popen, Path]:
    log = store.project.dir("calls") / f"{ticket_id}.worker.log"
    cmd = [sys.executable, "-m", "smooth_research", "--project", str(store.project.root),
           "review" if ticket_id.startswith("RV") else "advise", "run", ticket_id, "--timeout", str(timeout),
           *(["--entry", entry] if entry else [])]
    with open(log, "ab") as out:  # the worker claims the ticket itself (run_ticket)
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=store.project.root)
    return proc, log


def start_background(store: Store, ticket_id: str, timeout: int, entry: str | None = None) -> dict:
    """Run the call in a detached worker process; `rl review show` / `rl advise show` report the outcome."""
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
    if t["status"] == "answered" or _worker(t):
        return {"id": ticket_id, "summary": f"{ticket_id} " + (f"already answered ({t.get('verdict') or 'see answer'})"
                                                               if t["status"] == "answered" else
                                                               f"is being answered by process {_worker(t)}")}
    proc, log = _spawn(store, ticket_id, timeout, entry)
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
    """Try the ticket's entries in order and record every attempt; the first parseable answer is kept (a reviewer
    on a host whose subscription is low is skipped, unless named). The process claims the ticket first, so a
    second run does not call the models again while this one works."""
    chosen = [adapters.Entry.parse(only)] if only else None
    with store.write() as tx:
        t = tx.require(ticket_id, "Ticket")
        request_path = store.project.abs(tx.require(t["request_ref"], "DocRevision")["object_path"])
        calls = [c for c in (t.get("calls") or []) if "pid" not in c]  # without the claims of earlier workers
        if t["status"] != "answered":
            if holder := _worker(t):
                raise RLError("CONFLICT", f"{ticket_id} is being answered by process {holder}; see `rl "
                                          f"{'review' if ticket_id.startswith('RV') else 'advise'} show {ticket_id}`")
            tx.update(ticket_id, {"status": "running",
                                  "calls": calls + [{"pid": os.getpid(), "started_at": now_utc()}]})
    if t["status"] == "answered":
        return {"id": ticket_id, "summary": f"{ticket_id} already answered ({t.get('verdict') or 'see answer'})"}
    candidates = chosen or [adapters.Entry.parse(c) for c in (t.get("candidates") or [])]
    authors = list(t.get("author_models") or [])
    # The package goes in the prompt. A code review's package names the files, which the reviewer reads where
    # they are (in the requester's checkout, which may be a worktree outside the project).
    checkout = t.get("code_checkout")
    holds = ("names the files under review (read them where they are) and gives the answer format" if checkout
             else "holds what the question needs and the answer format")
    prompt = (f"You are the independent {'reviewer' if t['kind'] == 'review' else 'advisor'} for a research "
              f"project at {store.project.root}. The package below {holds}; you may read any other project "
              "file" + (" and search the web" if t["kind"] == "advice" else "") + ", but do not modify "
              "anything. If you cannot read something you need, say so (for a review: under 'unchecked'). "
              "Answer in the requested format.\n\n" + request_path.read_text(encoding="utf-8"))
    outside = [checkout] if checkout and Path(checkout).resolve() != store.project.root.resolve() else []
    # a reviewer on a host whose subscription is low is not called; advisors keep the rest of the window
    low = usage.low_hosts(store) if t["kind"] == "review" and not chosen else {}
    manual, answered = None, 0
    for entry in candidates:
        if entry.entry == "manual":
            manual = entry
            break
        if entry.entry in low:
            calls.append({"entry": str(entry), "ok": False, "error": f"not called: {low[entry.entry]['reason']}",
                          "finished_at": now_utc()})
            continue
        log = store.project.dir("calls") / f"{ticket_id}.{entry.entry}.{len(calls) + 1}.log"
        res = adapters.call(entry, prompt, store.project.root, read_only=True, timeout=timeout, add_dirs=outside,
                            log=log, web=t["kind"] == "advice")
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
                "summary": f"{ticket_id}: submit {store.project.rel(request_path)}"
                           f"{' with the files it names' if checkout else ''} to {manual.model} yourself (or ask the "
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
                  session_id: str | None, usage: dict | None, cost: float | None, calls: list, authors: list,
                  answered_by: dict | None = None) -> dict:
    """Record an answer. A review answer records its open severity and is final; the second review of a round
    that still has a medium or higher finding goes to the coordinator. A code review whose files changed after the
    request may describe another version: it is recorded, but does not count. A revised advice answer keeps the
    earlier ones."""
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
    if t["kind"] == "review" and t["status"] == "answered":
        raise RLError("CONFLICT", f"{ticket_id} is already answered; a further review is a re-check: "
                                  f"rl review request .. --recheck-of {ticket_id}")
    folder = "reviews" if t["kind"] == "review" else "advice"
    earlier = list(t.get("earlier_answers") or []) + ([t["answer_ref"]] if t.get("answer_ref") else [])
    name = f"{ticket_id}.md" if not earlier else f"{ticket_id}.revision-{len(earlier) + 1}.md"
    path = store.project.dir(folder) / name
    header = (f"<!-- {ticket_id} answer · entry {entry} · observed model {observed or 'unverified'} · "
              f"session {session_id or 'unknown'} · received {now_utc()} -->\n\n")
    path.write_text(header + text, encoding="utf-8")
    prep = documents.prepare(store.project, path, f"{t['kind']}_answer")
    data = adapters.last_json_block(text) or {}
    verdict = data.get("verdict") if t["kind"] == "review" else None
    if t["kind"] == "review" and verdict not in ("pass", "issues", "blocking"):
        verdict = "unparsed"
    relation = _relation(authors, observed, entry.split(":", 1)[-1].split("@")[0]) if t["kind"] == "review" else None
    severity = open_severity(data) if t["kind"] == "review" else None
    changed = changed_since_request(t) if t["kind"] == "review" else []
    escalate = (t["kind"] == "review" and (t.get("round_index") or 0) >= REVIEWS_PER_ROUND
                and severity in ("medium", "high") and not changed)
    if t["kind"] == "review":
        summary = f"{len(data.get('findings') or [])} finding(s)" + (f", {severity} open" if severity not in ("none", None) else "")
    else:
        rec = data.get("recommendation") or ("pause" if data.get("pause_recommended") else None)
        summary = f"{len(data.get('proposals') or [])} proposal(s)" + (f", recommends {rec}" if rec else "")
        summary += ", pause recommended" if data.get("pause_recommended") and rec != "pause" else ""
    msg = None
    with store.write() as tx:
        ref = documents.register(tx, prep)
        tx.update(ticket_id, {"status": "answered", "answer_ref": ref, "verdict": verdict, "relation": relation,
                              "requested_entry": entry, "observed_model": observed, "provider": provider,
                              "host_session_id": session_id, "usage": usage,
                              "cost_usd": float(cost) if cost is not None else None, "calls": calls,
                              "summary": summary, "answered_at": now_utc(), "error": None,
                              "open_severity": severity, "escalated": escalate or None,
                              "answered_by": answered_by["id"] if answered_by else None,
                              "earlier_answers": earlier or None, "changed_during_review": changed or None})
        tx.event(f"{t['kind']}.answered", ticket_id, f"{verdict or ''} {summary} via {entry}"
                 + (f" (revision {len(earlier) + 1})" if earlier else ""))
        if escalate:
            root = t.get("round_of") or ticket_id
            msg = messages.record(tx, "NOTE", answered_by["role_id"] if answered_by else "reviewer", "coordinator",
                                  None, f"approve it (rl review approve {ticket_id} --rationale ..) or return the work "
                                  f"to {t.get('requested_by') or 'its author'}", [],
                                  f"{ticket_id}: the review round {root} used its {REVIEWS_PER_ROUND} reviews and "
                                  f"{severity} finding(s) remain; your decision")
    rel_note = f", {relation}" if relation else ""
    note = (f"\n{', '.join(changed)} changed after the request, so the answer may describe another version: it does "
            f"not count. Request the review again (`--recheck-of {ticket_id}` takes its place in the round)"
            if changed else "")
    if escalate:
        target = messages.notify(store, msg)
        note += (f"\nthe round used its {REVIEWS_PER_ROUND} reviews with {severity} findings open: the coordinator "
                 f"decides ({msg['id']}" + (f", notice sent to {target})" if target else ")"))
    return {"id": ticket_id, "answer_ref": ref, "verdict": verdict, "relation": relation, "escalated": escalate,
            "summary": f"{ticket_id} answered by {observed or entry + ' (model unverified)'}{rel_note}: "
                       f"{verdict + ', ' if verdict else ''}{summary}; read {store.project.rel(path)}{note}"}


def import_answer(store: Store, ticket_id: str, answer: str, model_observed: str | None, source: str,
                  provider: str | None = None, session_id: str | None = None) -> dict:
    """Import an answer obtained outside the adapters (a person in a product UI, another tool)."""
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
    text = store.project.src(answer).read_text(encoding="utf-8")
    authors = list(t.get("author_models") or [])
    calls = (t.get("calls") or []) + [{"entry": f"manual:{source}", "imported_at": now_utc()}]
    return _store_answer(store, ticket_id, text, f"manual:{source}", model_observed, provider, session_id,
                         None, None, calls, authors)


def answer_live(store: Store, ticket_id: str, answer: str, session_name: str | None = None) -> dict:
    """The answer of a reviewer or advisor session that `rl agent open` started; an advisor may revise its own."""
    name = session_name or caller_role()
    with store.read() as tx:
        t = tx.require(ticket_id, "Ticket")
        found = [s for s in tx.all("Session", status="active") if s["role_id"] == name and s.get("role_kind")] if name else []
    if not found:
        raise RLError("INVALID", "answer from the reviewer or advisor session that `rl agent open` started (name it "
                                 "with --session); an answer obtained elsewhere is imported: rl review|advise import")
    s = found[-1]
    want = "reviewer" if t["kind"] == "review" else "advisor"
    if s["role_kind"] != want:
        raise RLError("INVALID", f"{ticket_id} is a {t['kind']} ticket; {s['role_id']} is a {s['role_kind']}")
    if t["kind"] == "advice" and t["status"] == "answered" and t.get("answered_by") != s["id"]:
        raise RLError("CONFLICT", f"{ticket_id} was answered by another session; only that advisor revises it")
    text = store.project.src(answer).read_text(encoding="utf-8")
    if not _parsed(t["kind"], text):
        raise RLError("INVALID", "the answer has no parseable result block: end it with the JSON block of the "
                                 "package's answer format")
    observed, provider = adapters.session_model(s["host"], s.get("host_session_id"))
    entry = s.get("entry") or f"{s['host']}:{s.get('model_requested')}"
    authors = list(t.get("author_models") or [])
    calls = (t.get("calls") or []) + [{"entry": entry, "session": s["id"], "answered_at": now_utc()}]
    return _store_answer(store, ticket_id, text, entry, observed or s.get("model_observed"), provider,
                         s.get("host_session_id"), None, None, calls, authors, answered_by=s)


def approve(store: Store, ticket_id: str, rationale: str) -> dict:
    """The coordinator accepts a review's remaining findings, with its reason; the review then passes, and the code
    it saw counts as reviewed. Findings that a later passing re-check resolved are recorded with resolve instead."""
    if not (rationale or "").strip():
        raise RLError("INVALID", "say why the remaining findings are accepted: --rationale")
    if caller_role():
        raise RLError("INVALID", f"approving a review is the coordinator's decision, not {caller_role()}'s")
    with store.write() as tx:
        t = tx.require(ticket_id, "Ticket")
        if t["kind"] != "review" or t["status"] != "answered":
            raise RLError("INVALID", f"{ticket_id} is not an answered review")
        later = passing_rechecks(tx.all("Ticket", kind="review")).get(ticket_id)
        tx.update(ticket_id, {"approved": rationale.strip(), "approved_at": now_utc()})
        tx.event("review.approved", ticket_id, rationale.strip())
        msg = messages.record(tx, "NOTE", "coordinator", t["requested_by"], None, None, [],
                              f"{ticket_id} approved by the coordinator: {rationale.strip()}") \
            if t.get("requested_by") not in (None, "coordinator") else None
    target = messages.notify(store, msg) if msg else None
    note = (f"\nnote: {', '.join(later)} re-checked it and passed. Approving accepts the findings that remain, and "
            f"what {ticket_id} saw now counts as reviewed; when the re-check resolved them, `rl review resolve "
            f"{ticket_id} --by {later[-1]} --reason ..` says so instead" if later else "")
    return {"id": ticket_id, "summary": f"{ticket_id} approved: {rationale.strip()} ({review_mark({**t, 'approved': 1})})"
                                        + (f"; notice sent to {target}" if target else "") + note}


def resolve(store: Store, ticket_id: str, by: str, reason: str) -> dict:
    """The coordinator records that a passing re-check (--recheck-of, directly or down the chain) resolved a review's
    findings. The review leaves attention; its verdict stays and it still does not pass, so the code it saw does not
    count as reviewed by it (approve accepts the findings instead)."""
    if not (reason or "").strip():
        raise RLError("INVALID", "say how the re-check resolved the findings: --reason")
    if caller_role():
        raise RLError("INVALID", f"resolving a review is the coordinator's decision, not {caller_role()}'s")
    with store.write() as tx:
        t, other = tx.require(ticket_id, "Ticket"), tx.require(by, "Ticket")
        if t["kind"] != "review" or t["status"] != "answered":
            raise RLError("INVALID", f"{ticket_id} is not an answered review")
        if review_passes(t):
            raise RLError("INVALID", f"{ticket_id} passes ({review_mark(t)}): it has nothing to resolve")
        if t.get("resolved_by"):
            raise RLError("CONFLICT", f"{ticket_id} was resolved by {t['resolved_by']} already")
        chain, prev = [], other.get("recheck_of")
        while prev and prev not in chain:
            chain.append(prev)
            prev = (tx.get(prev, "Ticket") or {}).get("recheck_of")
        if ticket_id not in chain:
            raise RLError("INVALID", f"{by} is not a re-check of {ticket_id}: a review is resolved by a review "
                                     f"requested with --recheck-of {ticket_id}, or with --recheck-of a re-check of it")
        if not review_passes(other):
            raise RLError("INVALID", f"{by} does not pass ({review_mark(other)}): only a passing re-check resolves")
        tx.update(ticket_id, {"resolved_by": by, "resolved_reason": reason.strip(), "resolved_at": now_utc()})
        tx.event("review.resolved", ticket_id, f"by {by}: {reason.strip()}")
    return {"id": ticket_id, "resolved_by": by,
            "summary": f"{ticket_id} resolved by {by}: {reason.strip()}. Its verdict stays ({review_mark(t)}); the code "
                       f"it saw counts as reviewed only where {by} or another passing review saw it"}


def show(store: Store, ticket_id: str | None, kind: str) -> dict:
    with store.read() as tx:
        tickets = [tx.require(ticket_id, "Ticket")] if ticket_id else tx.all("Ticket", kind=kind)
    lines = []
    for t in tickets:
        alive = " (worker not running: run again)" if t["status"] == "running" and not _worker(t) else ""
        lines.append(f"{brief(t)}{alive}")
        for key in ("target_id", "relation", "requested_entry", "observed_model", "host_session_id", "summary",
                    "round_of", "round_index", "round_reason", "requested_by", "assigned_session", "answered_by",
                    "escalated", "approved", "resolved_by", "resolved_reason", "changed_during_review",
                    "request_ref", "answer_ref",
                    "earlier_answers", "reused_from", "error",
                    "cost_usd"):
            if t.get(key) not in (None, ""):
                lines.append(f"  {key}: {t[key]}")
    return {"tickets": tickets, "summary": "\n".join(lines) or f"no {kind} tickets"}


# -- reviews --------------------------------------------------------------------------------------------------


def _reuse(tx: Tx, key: str, target_text: str | None, targets: dict[str, dict], author_models: list[str],
           subject: str, extra: dict | None = None) -> dict | None:
    """An answered review (pass or issues) with the same key answers this request too. targets: each target with the
    properties of its targets edge (the revision the review is of)."""
    for old in tx.all("Ticket", dedupe_key=key):
        if old["kind"] == "review" and review_passes(old):
            rid = tx.next_id("Ticket", "RV")
            tx.create("Ticket", {**{k: old.get(k) for k in ("question", "request_ref", "answer_ref", "requested_entry",
                                                            "requested_model", "observed_model", "provider",
                                                            "host_session_id", "relation", "verdict", "summary",
                                                            "answered_at", "open_severity", "approved")},
                                 "id": rid, "kind": "review", "subject": subject, "status": "answered",
                                 "target_id": target_text, "dedupe_key": key, "author_models": list(author_models),
                                 "reused_from": old.get("reused_from") or old["id"], **(extra or {})})
            for t, props in targets.items():
                tx.link("targets", rid, t, **props)
            tx.event("review.reused", rid, f"{subject} key {short(key)} unchanged since {old['id']}")
            return {"id": rid, "reused_from": old["id"], "verdict": old.get("verdict"),
                    "summary": f"{rid} reuses {old['id']} ({review_mark(old)}): {subject} unchanged"}
    return None


def request_review(store: Store, target: str | None, files: list[str], question: str, author_models: list[str],
                   configs: list[str] = (), deps: list[str] = (), ignore_keys: list[str] = (), scope: str = "",
                   diff_base: str | None = None, recheck_of: str | None = None, new_round: str | None = None) -> dict:
    if not files:
        raise RLError("INVALID", "name the code under review with --files")
    if not author_models:
        raise RLError("INVALID", "name the model(s) that wrote this code with --author-model (recorded with the "
                                 "review)")
    fp, detail = fingerprint(store, files, list(configs), list(deps), list(ignore_keys), scope or question)
    eligible = _entries(store, "review")
    contracts = prepare_contracts(store, [target] if target else [])  # a targeted experiment's contract is sealed
    diff = _diff(store, diff_base, detail) if diff_base else ""  # git runs before the write lock is taken
    with store.write() as tx:
        if target:
            tx.require(target)
        bound, sealed = seal_referenced(tx, contracts, store.project)
        targets = {target: bound.get(target, {})} if target else {}
        reused = None if recheck_of else _reuse(tx, fp, target, targets, author_models, "code", {"code_scope": detail})
        if reused:
            return {**reused, "summary": reused["summary"] + "".join(f"; {note}" for note in sealed)}
        rid = tx.next_id("Ticket", "RV")
        rounds = _round(tx, recheck_of, new_round)
        contract = ""
        if target and tx.require(target)["_label"] == "Experiment":
            contract = contract_summary(tx, store, tx.require(target))
        prior = brief(tx.require(recheck_of)) if recheck_of else ""
        path = store.project.dir("packages") / f"{rid}.request.md"
        path.write_text(_review_package(store, rid, target, question, scope, contract, detail, diff_base, diff,
                                        prior), encoding="utf-8")
        ref = documents.register(tx, documents.prepare(store.project, path, "review_request"))
        tx.create("Ticket", {"id": rid, "kind": "review", "subject": "code",
                             "status": "pending" if eligible else "unavailable",
                             "target_id": target, "question": question, "dedupe_key": fp,
                             "candidates": [str(e) for e in eligible], "author_models": list(author_models),
                             "recheck_of": recheck_of, "code_scope": detail, "request_ref": ref,
                             "code_checkout": str(store.project.checkout), "round_of": rid, **rounds,
                             "requested_by": caller_role(),
                             "error": None if eligible else "no reviewer configured (models.reviewer is empty)"})
        if target:
            tx.link("targets", rid, target, **targets[target])
        tx.event("review.requested", rid, f"{target or '-'} fingerprint {short(fp)}")
    note = (f"candidates {', '.join(str(e) for e in eligible)}; run it: rl review run {rid} [--background], or "
            "in herdr by a reviewer session. The reviewer reads the files where they are: leave them unchanged "
            "until the answer (an answer given while they change does not count)"
            if eligible else "no reviewer configured: recorded unavailable (not a pass)")
    route = _route(store, rid, recheck_of, caller_role()) if eligible else ""
    return {"id": rid, "request_ref": ref, "fingerprint": fp,
            "summary": f"{rid} {_round_note(store, rid)}{note}{route}" + "".join(f"; {n}" for n in sealed)}


def _round_note(store: Store, rid: str) -> str:
    with store.read() as tx:
        t = tx.require(rid, "Ticket")
    return f"(review {t.get('round_index') or 1} of {REVIEWS_PER_ROUND} in round {t.get('round_of') or rid}) "


def _diff(store: Store, diff_base: str, detail: dict) -> str:
    """The diff of the files under review against diff_base, or nothing when git cannot give it."""
    try:
        return subprocess.run(["git", "diff", diff_base, "--", *detail["files"]], cwd=store.project.checkout,
                              capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _review_package(store: Store, rid: str, target: str | None, question: str, scope: str, contract: str,
                    detail: dict, diff_base: str | None, diff: str, prior: str) -> str:
    """The question, the method and the paths of the code under review: the reviewer reads the files where they
    are. The request recorded their digests; the answer counts only while they still match."""
    parts = [f"# Review package {rid}\n",
             f"Independent code review for a research experiment. Question: {question}\n",
             f"Scope: {scope or question}\n"]
    if prior:
        parts.append(f"This re-checks only what the earlier review raised, after fixes: {prior}\n")
    if contract:
        parts.append(f"## Method definition\n\n{contract}\n")
    checkout = store.project.checkout

    def listed(paths) -> str:
        return "\n".join(f"- {checkout / p}" for p in paths)

    where = "the requester's worktree" if store.project.in_worktree else "the project"
    parts.append(f"## Files under review\n\nRead them where they are, in {checkout} ({where}); the package does "
                 "not copy them. Cite findings by path and line.\n\n" + listed(detail["files"]))
    if detail["configs"]:
        parts.append("\n## Execution configuration\n\n" + listed(detail["configs"])
                     + (f"\n\n(keys allowed to vary without a new review: {', '.join(detail['ignored_keys'])})"
                        if detail["ignored_keys"] else ""))
    if detail["deps"]:
        parts.append("\n## Key dependencies\n\n" + listed(detail["deps"]))
    if diff:
        path = store.project.dir("packages") / f"{rid}.diff"
        path.write_text(diff, encoding="utf-8")
        parts.append(f"\n## Diff against {diff_base}\n\n{path}")
    parts.append("\n" + REVIEW_PROTOCOL)
    return "\n".join(parts)


CONCLUSION_QUESTION = "Do the records support this conclusion, at this evidence level and within this scope?"


def request_conclusion_review(store: Store, targets: list[str], proposal: str, author_models: list[str],
                              question: str | None = None, recheck_of: str | None = None,
                              new_round: str | None = None) -> dict:
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
        # what the package shows of each hypothesis: the review counts for that revision only (research.review_gap),
        # and evidence linked later is pointed out (research.review_hints)
        bound = {h: {"revision": tx.require(h, "Hypothesis")["revision"],
                     "evidence": sorted({e["id"] for e in tx.inc(h) if e["rel"] in ("supports", "challenges")},
                                        key=_sort_key)} for h in targets}
    key = digest_json({"subject": "conclusion", "body": body})
    target_text = ", ".join(targets)
    error = None if eligible else "no reviewer configured (models.reviewer is empty)"
    with store.write() as tx:
        reused = None if recheck_of else _reuse(tx, key, target_text, bound, author_models, "conclusion")
        if reused:
            return reused
        rid = tx.next_id("Ticket", "RV")
        rounds = _round(tx, recheck_of, new_round)
        path = store.project.dir("packages") / f"{rid}.request.md"
        path.write_text(f"# Conclusion review package {rid}\n\n{body}\n{CONCLUSION_PROTOCOL}", encoding="utf-8")
        ref = documents.register(tx, documents.prepare(store.project, path, "review_request"))
        tx.create("Ticket", {"id": rid, "kind": "review", "subject": "conclusion",
                             "status": "pending" if eligible else "unavailable", "target_id": target_text,
                             "question": question, "dedupe_key": key, "candidates": [str(e) for e in eligible],
                             "author_models": list(author_models), "recheck_of": recheck_of, "error": error,
                             "request_ref": ref, "round_of": rid, **rounds, "requested_by": caller_role()})
        for t, props in bound.items():
            tx.link("targets", rid, t, **props)
        tx.event("review.requested", rid, f"conclusion on {target_text} key {short(key)}")
    note = (f"candidates {', '.join(str(e) for e in eligible)}; run it: rl review run {rid} [--background] (or "
            f"assign it to a reviewer session in herdr), then cite it: rl decide ... --based-on {rid}"
            if eligible else "no reviewer configured: recorded unavailable (not a pass)")
    route = _route(store, rid, recheck_of, caller_role()) if eligible else ""
    return {"id": rid, "request_ref": ref, "summary": f"{rid} {_round_note(store, rid)}{note}{route}"}


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
            if code := code_review_summary(tx, evidence_attempts(tx, ev["id"])):
                lines.append(f"- code review at registration: {code}")
        for e in tx.inc(hid, "tests"):
            experiments.setdefault(e["id"], tx.require(e["id"]))
        decisions = [brief(tx.require(e["id"])) for e in tx.inc(hid, "decides")]
        lines += ["", "Decisions so far: " + ("; ".join(decisions) if decisions else "none")]
        parts.append("\n".join(lines) + "\n")
    for exp in experiments.values():
        lines = [f"## Experiment {contract_summary(tx, store, exp)}"]
        if exp.get("revised_after_results"):
            lines.append("The contract was revised after results: later comparisons are exploratory.")
        lines += _arm_table(tx, store, exp)
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


def advice_rounds(tx: Tx) -> list:
    """Advice rounds that count toward advice.max_rounds: those with an answer or still waiting for one (a round
    no advisor could answer does not count)."""
    rounds: dict = {}
    for t in tx.all("Ticket", kind="advice"):
        rounds.setdefault(t["advice_round"], []).append(t["status"])
    return [r for r, statuses in rounds.items() if set(statuses) & {"answered", "pending", "running"}]


def prepare_advice(store: Store, question: str, target: str | None = None, user_approved: str | None = None) -> dict:
    """One advice round: a package for every configured advisor, each on its own ticket, to be asked at once. The
    same package for the same advisors is the same round."""
    entries = _entries(store, "advice")
    if not entries:
        raise RLError("INVALID", "no advisor configured: list model entries under models.advisor")
    limit = advice_limit(store.project.config)
    question = question.strip()
    pack = advice_pack(store, target, question)
    key = digest_json({"package": pack["summary"], "advisor": [str(e) for e in entries]})
    with store.read() as tx:
        same = [t for t in tx.all("Ticket", dedupe_key=key) if t["kind"] == "advice"]
    if same:
        ids = [t["id"] for t in same]
        return {"ids": ids, "duplicate": True,
                "summary": f"{', '.join(ids)} already ask this with the same evidence "
                           f"({', '.join(t['status'] for t in same)}); send only new data, or wait for new evidence"}
    with store.write() as tx:
        used = len(advice_rounds(tx))
        if used >= limit and not (user_approved or "").strip():
            raise RLError("LIMIT", f"{used} of {limit} advice rounds used (advice.max_rounds); report to the user "
                                   "why another round is needed, and if they agree pass their approval: "
                                   "--user-approved \"<what the user said>\"")
        number = 1 + max([t["advice_round"] for t in tx.all("Ticket", kind="advice")], default=0)
        path = store.project.dir("packages") / f"advice-round-{number}.request.md"
        path.write_text(pack["summary"] + "\n\n" + ADVICE_PROTOCOL, encoding="utf-8")
        ref = documents.register(tx, documents.prepare(store.project, path, "advice_request"))
        ids = []
        for entry in entries:
            aid = tx.next_id("Ticket", "AD")
            tx.create("Ticket", {"id": aid, "kind": "advice", "status": "pending", "target_id": target,
                                 "question": question, "dedupe_key": key, "candidates": [str(entry)],
                                 "advice_round": number, "user_approved": (user_approved or "").strip() or None,
                                 "request_ref": ref})
            if target:
                tx.link("targets", aid, target)
            ids.append(aid)
        tx.event("advice.prepared", ids[0], f"round {number} ({len(ids)} advisors)"
                 + (f"; beyond the limit, approved by the user: {user_approved.strip()}" if used >= limit else "")
                 + f": {question}")
    advisors = ", ".join(f"{aid} {entry}" for aid, entry in zip(ids, entries))
    return {"ids": ids, "round": number, "request_ref": ref, "tokens": pack["tokens"],
            "summary": f"advice round {number} ({used + 1} of {limit} used): {advisors}; package "
                       f"{store.project.rel(path)} (≈{pack['tokens']} tokens); ask them all at once: "
                       f"rl advise run {' '.join(ids)} [--background]"}
