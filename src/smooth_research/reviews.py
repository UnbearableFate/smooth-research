"""Review rounds and advice tickets (design 6.2, 6.3, 8.9).

A review round (RV) is one ticket with one reviewer session. A design round belongs to a task and has two
segments: the scientist's (until dispatch) and the researcher's (after). Each segment holds at most MAX_PASSES
reviews ("passes"); conclusion and tree rounds hold MAX_PASSES as well. Every finding has a category (method or
engineering), a severity and a status; only the reviewer closes a finding (fixed, constrained with conditions,
withdrawn), or an advisor's ruling (ruled).

What blocks, by round kind (6.2): the scientist's segment is blocked by any open method finding (it blocks the
dispatch); the researcher's segment by an open method finding or an open finding of medium or higher (they
block evidence runs); a tree round by medium or higher; a conclusion round until it passes. A round out of passes
while blocked is escalated: the researcher's segment to the scientist who made the task, the others to an advisor
ruling (an AD ticket the CLI opens).
"""

from __future__ import annotations

from . import documents, gitops
from .roles import Actor, need
from .schema import FINDING_CLOSED
from .store import Store, Tx
from .util import RLError, now_utc

MAX_PASSES = 3
SEVERE = ("medium", "high")


# -- reading --------------------------------------------------------------------------------------------------


def open_findings(t: dict) -> list[dict]:
    return [f for f in t.get("findings") or [] if f["status"] not in FINDING_CLOSED and f["status"] != "draft"]


def blocking(t: dict, segment: str | None = None) -> list[dict]:
    """The open findings that keep this round (in its current segment) from going on."""
    kind = t.get("review_kind")
    found = open_findings(t)
    if kind == "design":
        seg = segment or t.get("segment") or "scientist"
        if seg == "scientist":
            return [f for f in found if f["category"] == "method"]
        return [f for f in found if f["category"] == "method" or f["severity"] in SEVERE]
    if kind == "tree":
        return [f for f in found if f["severity"] in SEVERE]
    return found


def passes_in(t: dict, segment: str | None = None) -> list[dict]:
    seg = segment or t.get("segment")
    return [p for p in t.get("passes") or [] if seg is None or p.get("segment") == seg]


def last_pass(t: dict) -> dict | None:
    done = [p for p in t.get("passes") or [] if p.get("verdict")]
    return done[-1] if done else None


def task_rounds(tx: Tx, task_id: str) -> list[dict]:
    return [t for t in tx.all("Ticket", task_id=task_id) if t["kind"] == "review"]


def covers(tx: Tx, task_id: str, commit: str, for_evidence: bool) -> tuple[bool, str]:
    """Whether a review of the task covers commit: a reviewer gave a verdict on exactly this commit, and nothing that
    blocks is open (method findings; for evidence runs also medium or higher, 5.5)."""
    rounds = [t for t in task_rounds(tx, task_id) if t.get("review_kind") == "design"]
    if not rounds:
        return False, "no review round on this task"
    t = rounds[-1]
    lp = last_pass(t)
    if not lp or lp.get("commit") != commit:
        return False, (f"{t['id']} last reviewed {lp['commit'][:12] if lp else 'nothing yet'}, not {commit[:12]}: "
                       f"request a {'recheck' if lp else 'review'}")
    from .research import design_id
    now = design_id(tx, task_id)
    if now and lp.get("design") != now:
        return False, (f"{t['id']} reviewed {lp.get('design') or 'another design'}, and the experiment is {now} now "
                       "(another revision, or the draft changed since): request a review of it")
    seg = "researcher" if for_evidence else "scientist"
    open_ = blocking(t, seg)
    if open_:
        return False, f"{t['id']} has open {_names(t, open_)}"
    return True, f"{t['id']} covers {commit[:12]}"


def open_blockers(tx: Tx, task_id: str, for_evidence: bool) -> str | None:
    """Open findings that block, on any design round of the task: the fast lane stands in for a review of the
    commit, never for the resolution of a known finding (6.6)."""
    seg = "researcher" if for_evidence else "scientist"
    named = [_names(t, blocking(t, seg)) for t in task_rounds(tx, task_id)
             if t.get("review_kind") == "design" and blocking(t, seg)]
    return f"open {'; '.join(named)}" if named else None


def _names(t: dict, findings: list[dict]) -> str:
    return ", ".join(f"{t['id']}#F{f['n']} ({f['category']}, {f['severity']})" for f in findings)


def last_passed_design(tx: Tx, task_ids: list[str]) -> tuple[dict, str] | None:
    """The latest passed design round among the tasks given, with the commit it last reviewed."""
    found = []
    for tid in task_ids:
        for t in task_rounds(tx, tid):
            lp = last_pass(t)
            if t.get("review_kind") == "design" and t["status"] == "passed" and lp:
                found.append((t, lp["commit"]))
    return found[-1] if found else None


def conclusion_passed(tx: Tx, decision_id: str) -> dict | None:
    for t in tx.all("Ticket", target_id=decision_id):
        if t["kind"] == "review" and t.get("review_kind") == "conclusion" and t["status"] == "passed":
            return t
    return None


def latest_tree_round(tx: Tx, task_id: str) -> dict | None:
    rounds = [t for t in task_rounds(tx, task_id) if t.get("review_kind") == "tree"]
    return max(rounds, key=lambda t: t["id"]) if rounds else None


def tree_passed(tx: Tx, task_id: str) -> dict | None:
    """The task's tree round when its latest one passed: an older pass does not clear a tree whose newer round
    failed or still waits."""
    t = latest_tree_round(tx, task_id)
    return t if t and t["status"] == "passed" else None


def ruled_after(tx: Tx, rv: str) -> str | None:
    found = [t["id"] for t in tx.all("Ticket", ruling_of=rv) if t["status"] == "closed"]
    return found[-1] if found else None


# -- requests -------------------------------------------------------------------------------------------------


def request(store: Store, actor: Actor, target: str, kind: str = "design", question: str | None = None,
            recheck_of: str | None = None) -> dict:
    """Open a review round, or add a pass to the open one (a recheck after fixes). The watcher starts the reviewer
    session for a round that waits (11.2). A conclusion review carries no note from the author (6.3)."""
    if kind == "conclusion" and question:
        raise RLError("INVALID", "a conclusion review carries no note: the reviewer judges from the records alone")
    with store.write() as tx:
        if kind == "design":
            out = _design_request(store, tx, actor, target, question, recheck_of)
        elif kind == "conclusion":
            need(actor, "scientist", what="a conclusion review")
            d = tx.require(target, "Decision")
            if d["state"] != "draft" or d["kind"] not in ("verdict", "stop"):
                raise RLError("INVALID", f"{target} is a {d['state']} {d['kind']}: a conclusion review is of a draft "
                                         "verdict or stop")
            out = _round(tx, actor, "conclusion", d.get("task_id"), target, None, recheck_of, question=None)
        elif kind == "tree":
            need(actor, "scientist", what="a tree review")
            task = tx.require(target, "Task")
            if task["kind"] != "tree" or task.get("holder") != actor.name:
                raise RLError("INVALID", f"a tree review is of the tree task you hold; {target} is not")
            out = _round(tx, actor, "tree", target, target, None, recheck_of, question)
        else:
            raise RLError("INVALID", "--kind is design, conclusion or tree (a recheck: --recheck-of RV-x)")
    return out


def _design_request(store: Store, tx: Tx, actor: Actor, target: str, question: str | None,
                    recheck_of: str | None) -> dict:
    need(actor, "scientist", "researcher", what="a code review")
    task = tx.require(target, "Task")
    if task["kind"] not in ("execute", "prep"):
        raise RLError("INVALID", f"code is reviewed on an execute or preparation task; {target} is {task['kind']}")
    if task.get("holder") != actor.name:
        raise RLError("INVALID", f"{target} is held by {task.get('holder') or 'nobody'}: its holder requests reviews")
    if gitops.dirty(store.project, target):
        raise RLError("INVALID", f"{target}'s worktree has uncommitted changes: commit them; a review is of a commit")
    commit = gitops.head(store.project, target)
    segment = "scientist" if actor.role == "scientist" else "researcher"
    from .research import design_key, sealed_copy, task_experiment
    exp = task_experiment(tx, target)  # what the reviewer is shown and what the verdict clears
    return _round(tx, actor, "design", target, target, commit, recheck_of, question, segment,
                  design_key(exp), sealed_copy(exp) if exp else None)


def _round(tx: Tx, actor: Actor, kind: str, task_id: str | None, target: str, commit: str | None,
           recheck_of: str | None, question: str | None, segment: str | None = None,
           design: str | None = None, snapshot: dict | None = None) -> dict:
    current = None
    if recheck_of:
        current = tx.require(recheck_of, "Ticket")
        if current.get("review_kind") != kind or (task_id and current.get("task_id") != task_id):
            raise RLError("INVALID", f"{recheck_of} is not a {kind} round of {task_id or target}")
        if current["status"] in ("passed", "closed"):
            raise RLError("INVALID", f"{recheck_of} is {current['status']}: open a new round (omit --recheck-of)")
        if current["status"] == "waiting":
            raise RLError("CONFLICT", f"{recheck_of} already waits for its reviewer")
        if current["status"] == "escalated":
            raise RLError("INVALID", f"{recheck_of} is out of passes and escalated; it goes on after the ruling or "
                                     "the scientist's decision")
    elif task_id:
        live = [t for t in tx.all("Ticket", task_id=task_id) if t.get("review_kind") == kind
                and t["status"] in ("waiting", "answered", "escalated")]
        if live:
            raise RLError("CONFLICT", f"{live[-1]['id']} is still open on {task_id}: request a recheck "
                                      f"(--recheck-of {live[-1]['id']})")
    passes = list(current.get("passes") or []) if current else []
    if segment and current and current.get("segment") != segment:
        current = {**current, "segment": segment}
    used = len([p for p in passes if p.get("segment") == segment]) if segment else len(passes)
    if used >= MAX_PASSES:
        raise RLError("INVALID", f"{current['id']} has used its {MAX_PASSES} reviews"
                                 + (f" in the {segment}'s segment" if segment else ""))
    entry = {"n": len(passes) + 1, "segment": segment, "commit": commit, "target": target, "requested_by": actor.name,
             "requested_at": now_utc(), "question": question, **({"design": design} if design else {}),
             **({"design_snapshot": snapshot} if snapshot else {})}
    if current:
        tx.update(current["id"], {"passes": passes + [entry], "status": "waiting", "segment": segment,
                                  "target_id": target, "verdict": None})
        rv = current["id"]
        tx.event("review.recheck", rv, f"pass {entry['n']} of {target}" + (f" at {commit[:12]}" if commit else ""))
    else:
        rv = tx.next_id("Ticket", "RV")
        tx.create("Ticket", {"id": rv, "kind": "review", "status": "waiting", "review_kind": kind, "task_id": task_id,
                             "target_id": target, "segment": segment, "passes": [entry], "findings": [],
                             "requested_by": actor.name, "question": question})
        tx.event("review.requested", rv, f"{kind} of {target}" + (f" at {commit[:12]}" if commit else ""))
    return {"id": rv, "summary": f"{rv} waits for a reviewer ({kind} of {target}"
                                 + (f" at {commit[:12]}" if commit else "") + f", pass {entry['n']})"}


# -- answers --------------------------------------------------------------------------------------------------


def _protected(tx: Tx, t: dict) -> list[str]:
    if t.get("review_kind") != "design" or not t.get("task_id"):
        return []
    from .research import task_design
    passes = t.get("passes") or []
    # the design the reviewer was shown for this pass; else the revision the task was handed
    exp = (passes[-1].get("design_snapshot") if passes else None) or task_design(tx, t["task_id"])
    return list((exp or {}).get("protected") or [])


def _in_protected(loc: str | None, units: list[str]) -> bool:
    if not loc:
        return False
    where = loc.split(":", 1)[1] if ":" in loc.split("::")[0] else loc  # commit:path::symbol -> path::symbol
    path, _, symbol = where.partition("::")
    for u in units:
        up, _, us = u.partition("::")
        if path == up and (not us or not symbol or symbol == us or symbol.startswith(us + ".")):
            return True
    return False


def answer(store: Store, actor: Actor, rv: str, verdict: str, findings: list[dict] = (),
           close: list[dict] = (), answer_doc: str | None = None) -> dict:
    """The reviewer's verdict on the current pass: new findings (category, severity, text, loc; a finding located in
    the protected scope is a method finding unless engineering_reason says why not, 5.3) and the earlier findings
    it closes (n, status fixed|constrained|withdrawn, conditions)."""
    need(actor, "reviewer", what="answering a review")
    if verdict not in ("pass", "fail"):
        raise RLError("INVALID", "--verdict is pass or fail")
    prep = documents.prepare(store.project, answer_doc, "answer") if answer_doc else None
    with store.write() as tx:
        t = tx.require(rv, "Ticket")
        if actor.subject != rv:
            raise RLError("INVALID", f"{actor.name} reviews {actor.subject}, not {rv}")
        if t["status"] != "waiting":
            raise RLError("CONFLICT", f"{rv} is {t['status']}: no pass waits for a verdict")
        units = _protected(tx, t)
        items = [dict(f) for f in t.get("findings") or []]
        for c in close:
            f = next((x for x in items if x["n"] == int(c["n"])), None)
            if f is None:
                raise RLError("NOT_FOUND", f"{rv}#F{c['n']} does not exist")
            status = c.get("status", "fixed")
            if status not in ("fixed", "constrained", "withdrawn"):
                raise RLError("INVALID", "a closed finding is fixed, constrained or withdrawn")
            if status == "constrained" and not c.get("conditions"):
                raise RLError("INVALID", f"{rv}#F{c['n']} closed by constraint needs its conditions")
            f.update(status=status, conditions=c.get("conditions"), closed_at=now_utc(), closed_by=actor.name)
        for raw in findings:
            for key in ("severity", "text"):
                if not raw.get(key):
                    raise RLError("INVALID", f"each finding needs {key}")
            if raw["severity"] not in ("low", "medium", "high"):
                raise RLError("INVALID", "severity is low, medium or high")
            category = raw.get("category") or "method"
            if category not in ("method", "engineering"):
                raise RLError("INVALID", "category is method or engineering")
            if t.get("review_kind") == "design" and _in_protected(raw.get("loc"), units) and category == "engineering" \
                    and not raw.get("engineering_reason"):
                raise RLError("INVALID", f"the finding at {raw.get('loc')} lies in the protected scope, so it is a "
                                         "method finding unless you say why it is engineering (engineering_reason)")
            items.append({"n": len(items) + 1, "category": category, "severity": raw["severity"], "text": raw["text"],
                          "loc": raw.get("loc"), "engineering_reason": raw.get("engineering_reason"),
                          "status": "open", "pass": len(t["passes"]), "segment": t.get("segment")})
        passes = [dict(p) for p in t["passes"]]
        passes[-1].update(verdict=verdict, answered_at=now_utc(), by=actor.name)
        changes = {"findings": items, "passes": passes, "verdict": verdict, "answered_at": now_utc()}
        if prep:
            changes["answer_ref"] = documents.register(tx, prep, rv)
        t2 = {**t, **changes}
        changes["status"] = _status_after(t2)
        tx.update(rv, changes)
        escalated = None
        if changes["status"] == "escalated" and not (t.get("review_kind") == "design"
                                                     and t.get("segment") == "researcher"):
            escalated = _open_ruling(tx, {**t2, "status": "escalated"})
        tx.event("review.answered", rv, f"{verdict}; {len(findings)} new finding(s), {len(close)} closed -> "
                                        f"{changes['status']}" + (f"; ruling {escalated}" if escalated else ""))
    return {"id": rv, "status": changes["status"],
            "summary": f"{rv} {verdict} -> {changes['status']}" + (f"; ruling {escalated} opened" if escalated else "")}


def _status_after(t: dict) -> str:
    if t.get("review_kind") == "conclusion":
        if t["verdict"] == "pass" and not open_findings(t):
            return "passed"
        return "escalated" if len(t["passes"]) >= MAX_PASSES else "answered"
    if not open_findings(t) and t["verdict"] == "pass":
        return "passed"
    if blocking(t):
        return "escalated" if len(passes_in(t)) >= MAX_PASSES else "answered"
    if t.get("review_kind") == "tree" and t["verdict"] == "pass":
        return "passed"
    return "answered"


def _open_ruling(tx: Tx, t: dict) -> str:
    ad = tx.next_id("Ticket", "AD")
    tx.create("Ticket", {"id": ad, "kind": "advice", "status": "waiting", "advice_kind": "ruling",
                         "task_id": t.get("task_id"), "target_id": t["id"], "ruling_of": t["id"],
                         "requested_by": "cli", "question": f"rule on the open findings of {t['id']}"})
    tx.event("advice.ruling", ad, f"{t['id']} out of passes with {len(blocking(t))} blocking finding(s)")
    return ad


def settle(store: Store, actor: Actor, rv: str, n: int, status: str, conditions: str | None = None) -> dict:
    """The scientist who made a task decides an open finding of its escalated researcher segment (6.2)."""
    need(actor, "scientist", what="settling an escalated finding")
    with store.write() as tx:
        t = tx.require(rv, "Ticket")
        task = tx.require(t["task_id"], "Task")
        if t["status"] != "escalated" or t.get("segment") != "researcher" or task["creator"] != actor.name:
            raise RLError("INVALID", f"{rv} is not an escalated researcher segment of a task you made")
        if status not in ("constrained", "withdrawn"):
            raise RLError("INVALID", "settle with constrained (and its conditions) or withdrawn; a change of design is "
                                     "an answer on the task")
        items = [dict(f) for f in t["findings"]]
        f = next((x for x in items if x["n"] == n), None)
        if f is None:
            raise RLError("NOT_FOUND", f"{rv}#F{n} does not exist")
        f.update(status=status, conditions=conditions, closed_at=now_utc(), closed_by=actor.name)
        t2 = {**t, "findings": items}
        tx.update(rv, {"findings": items, "status": "passed" if not blocking(t2) and not open_findings(t2)
                       else "escalated" if blocking(t2) else "answered"})
        tx.event("review.settled", rv, f"F{n} {status} by {actor.name}")
    return {"id": rv, "summary": f"{rv}#F{n} {status}"}


# -- advice ---------------------------------------------------------------------------------------------------


def consult(store: Store, actor: Actor, question: str) -> dict:
    """A scientist asks an advisor at a bottleneck; the conversation goes on the AD ticket (8.7)."""
    need(actor, "scientist", what="a consultation")
    with store.write() as tx:
        ad = tx.next_id("Ticket", "AD")
        tx.create("Ticket", {"id": ad, "kind": "advice", "status": "waiting", "advice_kind": "consult",
                             "task_id": actor.subject, "requested_by": actor.name, "question": question})
        tx.event("advice.requested", ad, question[:300])
    return {"id": ad, "summary": f"{ad} waits for an advisor"}


def advise(store: Store, actor: Actor, ad: str, answer_doc: str, rulings: list[dict] = ()) -> dict:
    """The advisor's answer: advice, or a ruling (uphold|withdraw with a reason for each open finding)."""
    need(actor, "advisor", what="answering an advice ticket")
    prep = documents.prepare(store.project, answer_doc, "advice")
    with store.write() as tx:
        t = tx.require(ad, "Ticket")
        if t["status"] != "waiting":
            raise RLError("CONFLICT", f"{ad} is {t['status']}")
        ref = documents.register(tx, prep, ad)
        if t.get("advice_kind") == "ruling":
            rv = tx.require(t["ruling_of"], "Ticket")
            items = [dict(f) for f in rv["findings"]]
            open_ns = {f["n"] for f in blocking(rv)}
            given = {int(r["n"]): r for r in rulings}
            missing = sorted(open_ns - set(given))
            if missing:
                raise RLError("INVALID", f"rule on every blocking finding of {rv['id']}: missing "
                                         f"{', '.join('F' + str(n) for n in missing)}")
            for f in items:
                r = given.get(f["n"])
                if r is None:
                    continue
                if r.get("ruling") not in ("uphold", "withdraw") or not r.get("reason"):
                    raise RLError("INVALID", "each ruling is {n, ruling: uphold|withdraw, reason}")
                f["ruling"] = {"by": ad, "ruling": r["ruling"], "reason": r["reason"]}
                if r["ruling"] == "withdraw":
                    f.update(status="ruled", closed_at=now_utc(), closed_by=ad)
            rv2 = {**rv, "findings": items}
            # upheld findings: the scientist changes the work and opens a new round; the old one closes
            tx.update(rv["id"], {"findings": items, "status": "passed" if not blocking(rv2) else "closed",
                                 "ruled": {"by": ad, "at": now_utc()}})
        tx.update(ad, {"status": "closed", "answer_ref": ref, "answered_at": now_utc(),
                       "ruled": list(rulings) or None})
        tx.event("advice.answered", ad, ref)
    return {"id": ad, "summary": f"{ad} answered ({ref})"}

