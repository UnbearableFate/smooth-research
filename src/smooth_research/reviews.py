"""Review rounds and advice tickets (design 2026-11-27, chapter 6).

A review round (RV) is one author, one target and one reviewer session (reviewer-RV-x). The target is a commit of an
execute or preparation task (a design review) or a draft verdict or stop (a conclusion review; a revised conclusion
is a new draft, reviewed in the same round). A round holds at most two reviews ("passes"): review, fix, recheck, fix.

- The first review ends the round when it passes with no finding open. Otherwise the author fixes and asks for the
  recheck (`rl review request <target> --recheck-of RV-x`), or goes on.
- The second review ends the round whatever remains: passed on a pass verdict, closed on a fail. Findings still open
  stay on the record, and the round is flagged (open-findings). More review is a new round.
- While a review waits, the author can move it to a newer commit or draft (the same request with --recheck-of); that
  is not another review. The reviewer answers with the target it reviewed (--at); a verdict on anything other than
  the current target is refused, so no verdict lands on code or a draft its reviewer did not see.
- The author can reject a finding with a reason (rl review reject RV-x#Fn), without the reviewer's consent. At the
  recheck the reviewer withdraws it (closes it) or keeps it, which marks it disputed.

Findings keep their category (method or engineering) and severity: they decide which flags work that goes on past
them gets (5.2). Handing a task to a researcher with method findings open, or running evidence with method or medium+
findings open, is flagged, and so is going on without a review of the commit. Nothing here refuses a step for want of
a review: review_flags() and covers() say what to flag.

Advisors are consulted on request only (rl advise ask). Ruling tickets of older records stay readable.
"""

from __future__ import annotations

from . import documents, gitops
from .flags import Flags
from .roles import Actor, need, session_name
from .schema import FINDING_CLOSED, TASK_DONE
from .store import Store, Tx, label_of
from .util import RLError, now_utc, short

REVIEWS_PER_ROUND = 2
SEVERE = ("medium", "high")
ENDED = ("passed", "closed", "escalated")  # escalated: older records only (schema 6 closes them)


# -- reading --------------------------------------------------------------------------------------------------


def open_findings(t: dict) -> list[dict]:
    """Findings not closed: open, rejected by the author (the reviewer has not seen the reason yet) and disputed."""
    return [f for f in t.get("findings") or [] if f["status"] not in FINDING_CLOSED and f["status"] != "draft"]


def _matter(findings: list[dict], for_evidence: bool) -> list[dict]:
    """The findings that flag work going on past them: method findings; for evidence runs also medium or higher."""
    return [f for f in findings if f["category"] == "method" or (for_evidence and f["severity"] in SEVERE)]


def blocking(t: dict, segment: str | None = None) -> list[dict]:
    """The open findings that flag an evidence run (method, or medium or higher). Nothing blocks any more; the name
    stays for its readers. segment: older callers pass it; rounds have no segments now, so it is ignored."""
    return _matter(open_findings(t), True)


def last_pass(t: dict) -> dict | None:
    done = [p for p in t.get("passes") or [] if p.get("verdict")]
    return done[-1] if done else None


def reviews_done(t: dict) -> int:
    return len([p for p in t.get("passes") or [] if p.get("verdict")])


def task_rounds(tx: Tx, task_id: str) -> list[dict]:
    return [t for t in tx.all("Ticket", task_id=task_id) if t["kind"] == "review"]


def ended(r: dict, task: dict | None = None) -> bool:
    """A round is over: passed or closed, its two reviews given, or its task (given here) ended (3.3)."""
    if r["status"] in ENDED:
        return True
    if r["status"] != "waiting" and reviews_done(r) >= REVIEWS_PER_ROUND:
        return True
    return bool(task) and task["state"] in TASK_DONE


def round_ended(tx: Tx, r: dict) -> bool:
    task = tx.get(r["task_id"], "Task") if r.get("task_id") else None
    return ended(r, task)


def round_author(tx: Tx, r: dict) -> str:
    """Who the round is for: the session that opened it (older records: a design round's task holder, else the
    session that asked for its latest review)."""
    if r.get("author"):
        return r["author"]
    if r.get("review_kind") == "design" and r.get("task_id"):
        task = tx.get(r["task_id"], "Task")
        if task and task.get("holder"):
            return task["holder"]
    return (r.get("passes") or [{}])[-1].get("requested_by") or r["requested_by"]


def pass_key(r: dict) -> int | str:
    """The identity of a round's latest review for to-do items (inbox.Item.key): its number, and how often a waiting
    review was moved to a newer target, so a recheck and a move are both news to the reviewer."""
    passes = r.get("passes") or []
    moved = len((passes[-1] if passes else {}).get("retargeted") or [])
    return f"{len(passes)}.{moved}" if moved else len(passes)


def design_token(p: dict) -> str:
    """The design a review is of, short: E-x@n#<first 8 of its digest>."""
    head, _, digest = (p.get("design") or "").partition("#")
    return f"{head}#{short(digest, 8)}" if digest else head


def redesigned(p: dict) -> bool:
    """The waiting review was moved to another design at the same commit (rl experiment set changes no commit):
    the commit alone no longer says what its reviewer saw."""
    return bool(p.get("commit") and p.get("design")) and any(
        m.get("commit") == p.get("commit") and m.get("design") != p.get("design") for m in p.get("retargeted") or [])


def target_text(p: dict) -> str:
    """What a review is of, as the reviewer names it with --at: a commit (12 characters), with its design when the
    design changed at that commit while the review waited (commit+E-x@n#digest), or a draft decision."""
    base = (p.get("commit") or "")[:12] or p.get("target") or "?"
    return f"{base}+{design_token(p)}" if redesigned(p) else base


def _names(t: dict, findings: list[dict]) -> str:
    return ", ".join(f"{t['id']}#F{f['n']} ({f['category']}, {f['severity']}"
                     + (f", {f['status']} by the author: {(f.get('rejected') or {}).get('reason', '')[:120]}"
                        if f["status"] in ("rejected", "disputed") else "") + ")" for f in findings)


def _covering(rounds: list[dict], commit: str, design: str | None) -> dict | None:
    """The newest round with a pass verdict on exactly this commit (and this experiment revision)."""
    for t in reversed(rounds):
        for p in reversed(t.get("passes") or []):
            if p.get("verdict") == "pass" and p.get("commit") == commit and (not design or p.get("design") == design):
                return t
    return None


def _unreviewed_why(rounds: list[dict], task_id: str, commit: str, design: str | None) -> str:
    if not rounds:
        return f"no review of {task_id} yet"
    t = rounds[-1]
    p = (t.get("passes") or [{}])[-1]
    if t["status"] == "waiting" and p.get("commit") == commit:
        return f"{t['id']} still waits for its reviewer at {commit[:12]}"
    lp = last_pass(t)
    if lp and lp.get("commit") == commit and design and lp.get("design") != design:
        return (f"{t['id']} reviewed {lp.get('design') or 'another design'}, and the experiment is {design} now "
                "(another revision, or the draft changed since)")
    if lp and lp.get("commit") == commit:
        return f"{t['id']} did not pass {commit[:12]} ({lp.get('verdict')})"
    last = lp["commit"][:12] if lp and lp.get("commit") else "nothing yet"
    return f"{t['id']} last reviewed {last}, not {commit[:12]}"


def protected_note(tx: Tx, project, task_id: str, commit: str) -> str | None:
    """Whether the protected scope of the task's experiment changed since its last passed review (on any task of the
    experiment): the note an unreviewed flag carries (5.2). None for a task without protected units."""
    from .research import task_design
    exp = task_design(tx, task_id)
    units = list((exp or {}).get("protected") or [])
    if not exp or not units:
        return None
    tids = list(dict.fromkeys([task_id] + [e["id"] for e in tx.inc(exp["id"], "targets")]))
    last = last_passed_design(tx, tids)
    if last is None:
        return f"no review of {exp['id']}'s code has passed yet"
    t, base = last
    try:
        changed = gitops.protected_changes(project, base, commit, units)
    except Exception:  # a note only: a commit git cannot read leaves it out
        return None
    if changed:
        return f"the protected scope changed since {t['id']} passed at {base[:12]}: {', '.join(changed)}"
    return f"the protected scope is unchanged since {t['id']} passed at {base[:12]}"


def review_flags(tx: Tx, task_id: str, commit: str, for_evidence: bool, project=None) -> list[tuple[str, str]]:
    """The flags work on the task at this commit gets (5.2), as (code, text): unreviewed when no review passed this
    commit (and the experiment as it stands), open-findings when findings that matter are open on any design round
    of the task (method ones; for an evidence run also medium or higher). Empty when a review covers it. With the
    project given, the unreviewed text says whether the protected scope changed since the last passed review."""
    from .research import design_id
    rounds = [t for t in task_rounds(tx, task_id) if t.get("review_kind") == "design"]
    now = design_id(tx, task_id)
    out = []
    if _covering(rounds, commit, now) is None:
        why = _unreviewed_why(rounds, task_id, commit, now)
        note = protected_note(tx, project, task_id, commit) if project is not None else None
        out.append(("unreviewed", why + (f"; {note}" if note else "")))
    named = [_names(t, _matter(open_findings(t), for_evidence)) for t in rounds
             if _matter(open_findings(t), for_evidence)]
    if named:
        out.append(("open-findings", f"open {'; '.join(named)}"))
    return out


def covers(tx: Tx, task_id: str, commit: str, for_evidence: bool, project=None) -> tuple[bool, str]:
    """(True, which round covers it) when a review passed exactly this commit and nothing that matters is open;
    else (False, the flag texts joined; review_flags gives them with their codes). It decides flags, never a
    refusal."""
    found = review_flags(tx, task_id, commit, for_evidence, project)
    if found:
        return False, "; ".join(text for _, text in found)
    from .research import design_id
    rounds = [t for t in task_rounds(tx, task_id) if t.get("review_kind") == "design"]
    t = _covering(rounds, commit, design_id(tx, task_id))
    return True, f"{t['id']} covers {commit[:12]}"


def open_blockers(tx: Tx, task_id: str, for_evidence: bool) -> str | None:
    """The findings that matter (method; for evidence also medium or higher), open on any design round of the task."""
    named = [_names(t, _matter(open_findings(t), for_evidence)) for t in task_rounds(tx, task_id)
             if t.get("review_kind") == "design" and _matter(open_findings(t), for_evidence)]
    return f"open {'; '.join(named)}" if named else None


def last_passed_design(tx: Tx, task_ids: list[str]) -> tuple[dict, str] | None:
    """The latest review with a pass verdict on a design round of the tasks given, with the commit it passed."""
    found = []
    for tid in task_ids:
        for t in task_rounds(tx, tid):
            if t.get("review_kind") != "design":
                continue
            for p in t.get("passes") or []:
                if p.get("verdict") == "pass" and p.get("commit"):
                    found.append((p.get("answered_at") or "", t, p["commit"]))
    if not found:
        return None
    _, t, commit = max(found, key=lambda x: x[0])
    return t, commit


def conclusion_passed(tx: Tx, decision_id: str) -> dict | None:
    """The passed conclusion round whose last review was of this draft."""
    for t in tx.all("Ticket", target_id=decision_id):
        if t["kind"] == "review" and t.get("review_kind") == "conclusion" and t["status"] == "passed" \
                and ((last_pass(t) or {}).get("target") or t.get("target_id")) == decision_id:
            return t
    return None


def conclusion_flags(tx: Tx, decision_id: str) -> list[tuple[str, str]]:
    """The flags finalizing a draft gets (5.2): unreviewed when no conclusion review of it passed, open-findings when
    the round that passed it (or the latest one on it) left findings open."""
    rounds = [t for t in tx.all("Ticket") if t["kind"] == "review" and t.get("review_kind") == "conclusion"
              and any(p.get("target") == decision_id for p in t.get("passes") or [])]
    passed = conclusion_passed(tx, decision_id)
    out = []
    if passed is None:
        state = f" ({rounds[-1]['id']} {rounds[-1]['status']})" if rounds else ""
        out.append(("unreviewed", f"no conclusion review of {decision_id} passed{state}"))
    last = passed or (rounds[-1] if rounds else None)
    if last and open_findings(last):
        out.append(("open-findings", f"open {_names(last, open_findings(last))}"))
    return out


def latest_round(tx: Tx, task_id: str, kind: str) -> dict | None:
    """The task's newest review round of a kind (rounds in id order: RV-9999 before RV-10000)."""
    rounds = [t for t in task_rounds(tx, task_id) if t.get("review_kind") == kind]
    return rounds[-1] if rounds else None


def latest_tree_round(tx: Tx, task_id: str) -> dict | None:
    """Older records only: tree reviews are gone (a tree is approved by the user)."""
    return latest_round(tx, task_id, "tree")


def tree_passed(tx: Tx, task_id: str) -> dict | None:
    """Older records only: the task's tree round when its latest one passed."""
    t = latest_tree_round(tx, task_id)
    return t if t and t["status"] == "passed" else None


# -- requests -------------------------------------------------------------------------------------------------


KINDS = {"Task": "design", "Decision": "conclusion"}


def request(store: Store, actor: Actor, target: str, kind: str | None = None, question: str | None = None,
            recheck_of: str | None = None) -> dict:
    """Open a round on a target, or with --recheck-of go on with one: its second review when its first is answered,
    the waiting review moved to the current commit or draft when the reviewer has not answered yet (not another
    review), or a new round (flagged round-ended) when it has ended. The reviewer session is told (result wake)."""
    if kind == "tree":
        raise RLError("INVALID", "tree reviews are gone: a tree is drafted by the coordinator's subagent and recorded "
                                 "once the user approves it (rl tree record); older tree rounds stay readable")
    label = label_of(target)
    inferred = KINDS.get(label)
    if inferred is None:
        raise RLError("INVALID", f"{target} is a {label}: a review is of an execute or preparation task (its current "
                                 "commit) or of a draft verdict or stop (D-x)")
    if kind and kind != inferred:
        raise RLError("INVALID", f"{target} is a {label}, so its review is a {inferred} review (omit --kind)")
    kind = inferred
    fl = Flags(actor.name if actor else "?")
    need(actor, *(("scientist", "researcher") if kind == "design" else ("scientist",)),
         what=f"asking for a {kind} review", flags=fl)
    warnings: list[str] = []
    with store.write() as tx:
        if kind == "design":
            task_id, entry, owner = _design_target(store, tx, target, warnings)
        else:
            task_id, entry, owner = _conclusion_target(tx, target, warnings)
        entry.update(requested_by=actor.name, requested_at=now_utc(), question=question)
        current = _recheck_round(tx, recheck_of, kind, task_id, target) if recheck_of else None
        if current is not None and round_ended(tx, current):
            why = current["status"] if current["status"] in ENDED else f"its {REVIEWS_PER_ROUND} reviews given"
            current = None
            fl.add("round-ended", f"{recheck_of} had ended ({why}) when {actor.name} asked for its recheck; a new "
                                  "round was opened instead")
            warnings.append(f"{recheck_of} has ended ({why}): this opens a new round of up to {REVIEWS_PER_ROUND} "
                            "reviews instead")
        if current is None:
            if owner and owner[0] != actor.name:
                fl.add("not-holder", f"{owner[1]}; {actor.name} asked for its review")
            if not recheck_of:
                live = [t for t in tx.all("Ticket", review_kind=kind) if t["kind"] == "review"
                        and not round_ended(tx, t) and _same_work(t, kind, task_id, target)]
                if live:
                    warnings.append(f"{live[-1]['id']} is still open on {task_id or target} ({live[-1]['status']}): "
                                    f"a recheck goes on in it (--recheck-of {live[-1]['id']}); this opens another "
                                    "round")
            rv, what = _open(tx, actor, kind, task_id, target, entry, question)
        else:
            author = round_author(tx, current)
            if author != actor.name:
                fl.add("not-holder", f"{current['id']} is {author}'s round; {actor.name} asked for its review")
            if current["status"] == "waiting":
                rv, what = _retarget(tx, actor, current, entry)
            else:
                rv, what = _recheck(tx, current, entry)
        written = fl.write(tx, rv)
    return {"id": rv, "summary": what, "flags": written, "warnings": warnings, "wake": [session_name("reviewer", rv)],
            "next": ["the reviewer's answer comes to you; meanwhile go on with other work. A newer "
                     + (f"commit before it answers: rl review request {target} --recheck-of {rv} moves the review "
                        "to it" if kind == "design" else f"draft before it answers (rl decide ..): rl review request "
                        f"D-y --recheck-of {rv} moves the review to it")]}


def _design_target(store: Store, tx: Tx, target: str, warnings: list[str]) -> tuple[str, dict, tuple]:
    task = tx.require(target, "Task")
    if task["kind"] not in ("execute", "prep"):
        raise RLError("INVALID", f"code is reviewed on an execute or preparation task; {target} is a {task['kind']} "
                                 "task")
    if task["state"] in TASK_DONE:
        raise RLError("INVALID", f"{target} is {task['state']}: its review rounds ended with it (3.3); review the "
                                 "code in a task that is still open")
    commit = gitops.head(store.project, target)  # refuses a task without a worktree: there is no commit to review
    dirty = gitops.dirty(store.project, target)
    if dirty:
        warnings.append(f"{target}'s worktree has uncommitted changes ({', '.join(dirty[:5])}): the review is of the "
                        f"commit {commit[:12]}; commit them and move the review to the new commit (--recheck-of RV-x)")
    from .research import design_key, sealed_copy, task_experiment
    exp = task_experiment(tx, target)  # what the reviewer is shown and what the verdict clears
    entry = {"commit": commit, "target": target}
    if exp:
        entry.update(design=design_key(exp), design_snapshot=sealed_copy(exp))
    return target, entry, (task.get("holder"), f"{target} is held by {task.get('holder') or 'nobody'}")


def _conclusion_target(tx: Tx, target: str, warnings: list[str]) -> tuple[str | None, dict, tuple | None]:
    d = tx.require(target, "Decision")
    if d["kind"] not in ("verdict", "stop"):
        raise RLError("INVALID", f"{target} is a {d['kind']} decision: a conclusion review is of a verdict or stop")
    task = tx.get(d["task_id"], "Task") if d.get("task_id") else None
    if task and task["state"] in TASK_DONE:
        raise RLError("INVALID", f"{target}'s task {task['id']} is {task['state']}: its review rounds ended with it "
                                 "(3.3)")
    if d["state"] == "final":
        warnings.append(f"{target} is final already: what the review finds changes nothing in it (a change is a new "
                        "decision)")
    owner = (d["author"], f"{target} is {d['author']}'s draft") if d.get("author") else None
    return d.get("task_id"), {"commit": None, "target": target}, owner


def _same_work(t: dict, kind: str, task_id: str | None, target: str) -> bool:
    if kind == "design" or task_id:
        return t.get("task_id") == task_id
    return t.get("target_id") == target


def _recheck_round(tx: Tx, rv: str, kind: str, task_id: str | None, target: str) -> dict:
    cur = tx.require(rv, "Ticket")
    if cur["kind"] != "review":
        raise RLError("INVALID", f"{rv} is an advice ticket, not a review round")
    if cur.get("review_kind") != kind:
        raise RLError("INVALID", f"{rv} is a {cur.get('review_kind')} round; {target} takes a {kind} review: open a "
                                 "new round (omit --recheck-of)")
    if not _same_work(cur, kind, task_id, target) and not any(p.get("target") == target
                                                              for p in cur.get("passes") or []):
        raise RLError("INVALID", f"{rv} reviews {cur.get('task_id') or cur.get('target_id')}, not {target}: a round "
                                 "has one target; open a new round (omit --recheck-of)")
    return cur


def _open(tx: Tx, actor: Actor, kind: str, task_id: str | None, target: str, entry: dict,
          question: str | None) -> tuple[str, str]:
    rv = tx.next_id("Ticket", "RV")
    tx.create("Ticket", {"id": rv, "kind": "review", "status": "waiting", "review_kind": kind, "task_id": task_id,
                         "target_id": target, "passes": [{"n": 1, **entry}], "findings": [],
                         "requested_by": actor.name, "question": question, "author": actor.name})
    tx.event("review.requested", rv, f"{kind} of {target} at {target_text(entry)}")
    return rv, (f"{rv} waits for its reviewer: {kind} review of {target} at {target_text(entry)} (review 1 of "
                f"{REVIEWS_PER_ROUND})")


def _recheck(tx: Tx, cur: dict, entry: dict) -> tuple[str, str]:
    passes = list(cur.get("passes") or [])
    entry = {"n": len(passes) + 1, **entry}
    tx.update(cur["id"], {"passes": passes + [entry], "status": "waiting", "target_id": entry["target"],
                          "verdict": None})
    tx.event("review.recheck", cur["id"], f"review {entry['n']} of {entry['target']} at {target_text(entry)}")
    return cur["id"], (f"{cur['id']} waits for its reviewer: recheck of {entry['target']} at {target_text(entry)} "
                       f"(review {entry['n']} of {REVIEWS_PER_ROUND}; the round ends with its answer)")


MOVED = ("commit", "target", "design", "design_snapshot")


def _retarget(tx: Tx, actor: Actor, cur: dict, entry: dict) -> tuple[str, str]:
    """The reviewer has not answered yet: its review moves to the current target (not another review)."""
    passes = [dict(p) for p in cur.get("passes") or []]
    p = passes[-1]
    if all(p.get(k) == entry.get(k) for k in ("commit", "target", "design")):
        return cur["id"], f"{cur['id']} already waits on {entry['target']} at {target_text(entry)}"
    before = target_text(p)
    p["retargeted"] = list(p.get("retargeted") or []) + [
        {"commit": p.get("commit"), "target": p.get("target"), "design": p.get("design"), "at": now_utc(),
         "by": actor.name}]
    for k in MOVED:
        p[k] = entry.get(k)
    p["retargeted_at"] = now_utc()
    if entry.get("question"):
        p["question"] = entry["question"]
    tx.update(cur["id"], {"passes": passes, "target_id": entry["target"]})
    tx.event("review.retarget", cur["id"], f"review {p.get('n', len(passes))} moved from {before} to "
                                           f"{target_text(p)}")
    return cur["id"], (f"{cur['id']}'s waiting review moved from {before} to {entry['target']} at {target_text(p)} "
                       "(not another review); its reviewer is told")


# -- answers --------------------------------------------------------------------------------------------------


def _protected(tx: Tx, t: dict) -> list[str]:
    if t.get("review_kind") != "design" or not t.get("task_id"):
        return []
    from .research import task_design
    passes = t.get("passes") or []
    # the design the reviewer was shown for this review; else the revision the task was handed
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


def _check_at(t: dict, p: dict, at: str | None) -> None:
    """The reviewer names what it reviewed; a verdict on another target would be recorded falsely (5.1)."""
    now = target_text(p)
    if not at:
        raise RLError("INVALID", f"--at names what you reviewed: the commit (7+ characters) or, in a conclusion "
                                 f"review, the draft D-x. {t['id']} waits on {now}")
    if p.get("commit"):
        commit, _, token = at.strip().partition("+")
        ok = len(commit) >= 7 and p["commit"].startswith(commit.lower())
        if ok and (token or redesigned(p)) and token != design_token(p):  # a design named is always checked
            why = (f"was moved to the design {design_token(p)} at the same commit while it waited" if redesigned(p)
                   else f"waits on the design {design_token(p) or '(none)'}, not {token}")
            raise RLError("INVALID", f"{t['id']} {why}: review that design, then answer with --at {now}. A verdict "
                                     "is recorded only on the target its reviewer saw")
    else:
        ok = at.strip() == (p.get("target") or t.get("target_id"))
    if ok:
        return
    moved = [m for m in p.get("retargeted") or [] if (m.get("commit") or m.get("target") or "").startswith(at.strip())]
    how = (f" (its author moved it there from {target_text(moved[-1])} at {(moved[-1].get('at') or '')[:16]}: "
           + (f"git diff {moved[-1]['commit'][:12]} {p['commit'][:12]})" if p.get("commit") and moved[-1].get("commit")
              else f"rl show {p.get('target')})")) if moved else ""
    raise RLError("INVALID", f"{t['id']} waits on {now}, not {at}{how}: review {now}, then answer with --at {now}. A "
                             "verdict is recorded only on the target its reviewer saw")


def answer(store: Store, actor: Actor, rv: str, verdict: str, findings: list[dict] = (),
           close: list[dict] = (), answer_doc: str | None = None, at: str | None = None) -> dict:
    """The reviewer's verdict on the waiting review, given with the target it reviewed (--at): new findings
    (category, severity, text, loc; a finding located in the protected scope is a method finding unless
    engineering_reason says why not) and the earlier findings it closes (n, status fixed|constrained|withdrawn,
    conditions). A finding the author rejected and the reviewer leaves open becomes disputed. The first review ends the
    round when it passes with nothing open; the second ends it whatever remains. The author is told (result wake)."""
    fl = Flags(actor.name if actor else "?")
    need(actor, "reviewer", what="answering a review", flags=fl)
    if verdict not in ("pass", "fail"):
        raise RLError("INVALID", "--verdict is pass or fail")
    prep = documents.prepare(store.project, answer_doc, "answer") if answer_doc else None
    warnings: list[str] = []
    with store.write() as tx:
        t = tx.require(rv, "Ticket")
        if t["kind"] != "review":
            raise RLError("INVALID", f"{rv} is an advice ticket: rl advise answer {rv} --doc ..")
        if t["status"] != "waiting":
            raise RLError("CONFLICT", f"{rv} is {t['status']}: no review of it waits for a verdict")
        if actor.name != session_name("reviewer", rv):
            fl.add("not-holder", f"{rv} is reviewer-{rv}'s round; {actor.name} answered it")
        passes = [dict(p) for p in t["passes"]]
        _check_at(t, passes[-1], at)
        units = _protected(tx, t)
        items = [dict(f) for f in t.get("findings") or []]
        closed_ns = set()
        for c in close:
            if not isinstance(c, dict) or "n" not in c:
                raise RLError("INVALID", "each --close item is {n, status: fixed|constrained|withdrawn, conditions}")
            f = next((x for x in items if x["n"] == int(c["n"])), None)
            if f is None:
                raise RLError("NOT_FOUND", f"{rv}#F{c['n']} does not exist")
            status = c.get("status", "fixed")
            if status not in ("fixed", "constrained", "withdrawn"):
                raise RLError("INVALID", "a closed finding is fixed, constrained or withdrawn")
            if status == "constrained" and not c.get("conditions"):
                raise RLError("INVALID", f"{rv}#F{c['n']} closed by constraint needs its conditions")
            f.update(status=status, conditions=c.get("conditions"), closed_at=now_utc(), closed_by=actor.name)
            closed_ns.add(f["n"])
        for f in items:  # the author's reason was read and the finding kept
            if f["status"] == "rejected" and f["n"] not in closed_ns:
                f.update(status="disputed", disputed_at=now_utc(), disputed_by=actor.name)
                warnings.append(f"{rv}#F{f['n']} was rejected by its author ({(f.get('rejected') or {}).get('reason')}"
                                "); you kept it, so it is disputed (closing it as withdrawn accepts the reason)")
        done = reviews_done(t) + 1
        for raw in findings:
            if not isinstance(raw, dict):
                raise RLError("INVALID", "each finding is {category, severity, text, loc[, engineering_reason]}")
            for key in ("severity", "text"):
                if not raw.get(key):
                    raise RLError("INVALID", f"each finding needs {key}")
            if raw["severity"] not in ("low", "medium", "high"):
                raise RLError("INVALID", "severity is low, medium or high")
            category = raw.get("category") or "method"
            if category not in ("method", "engineering"):
                raise RLError("INVALID", "category is method or engineering")
            n = len(items) + 1
            if category == "engineering" and not raw.get("engineering_reason") and _in_protected(raw.get("loc"), units):
                fl.add("category", f"{rv}#F{n} at {raw.get('loc')} lies in the protected scope and is recorded as "
                                   "engineering without a reason why it is not a method finding")
            items.append({"n": n, "category": category, "severity": raw["severity"], "text": raw["text"],
                          "loc": raw.get("loc"), "engineering_reason": raw.get("engineering_reason"),
                          "status": "open", "pass": done})
        passes[-1].update(verdict=verdict, answered_at=now_utc(), by=actor.name)
        changes = {"findings": items, "passes": passes, "verdict": verdict, "answered_at": now_utc()}
        if prep:
            changes["answer_ref"] = documents.register(tx, prep, rv)
        left = open_findings({"findings": items})
        if done >= REVIEWS_PER_ROUND:
            status = "passed" if verdict == "pass" else "closed"
        else:
            status = "passed" if verdict == "pass" and not left else "answered"
        changes["status"] = status
        if status != "answered" and left:
            fl.add("open-findings", f"{rv} ended after {done} review(s) with {_names(t, left)} open")
        tx.update(rv, changes)
        written = fl.write(tx, rv)
        author = round_author(tx, t)
        tx.event("review.answered", rv, f"{verdict} at {target_text(passes[-1])}; {len(findings)} new finding(s), "
                                        f"{len(closed_ns)} closed -> {status}")
    end = "" if status == "answered" else f"; the round has ended ({len(left)} finding(s) open)" if left else \
        "; the round has ended"
    return {"id": rv, "status": status, "flags": written, "warnings": warnings, "wake": [author],
            "summary": f"{rv} {verdict} at {target_text(passes[-1])} -> {status} (review {done} of "
                       f"{REVIEWS_PER_ROUND}){end}; {author} is told"}


def reject(store: Store, actor: Actor, ref: str, reason: str) -> dict:
    """The author declines a finding with a reason; the reviewer's consent is not needed. The reviewer reads the
    reason at the recheck: it withdraws the finding (closes it) or keeps it (disputed)."""
    rv, sep, n = ref.partition("#F")
    if not sep or not n.strip().isdigit():
        raise RLError("INVALID", "name the finding: RV-x#Fn")
    if not (reason or "").strip():
        raise RLError("INVALID", "--reason says why the finding is declined: the reviewer reads it at the recheck")
    fl = Flags(actor.name if actor else "?")
    need(actor, "scientist", "researcher", what="rejecting a finding", flags=fl)
    warnings: list[str] = []
    nxt: list[str] = []
    with store.write() as tx:
        t = tx.require(rv, "Ticket")
        if t["kind"] != "review":
            raise RLError("INVALID", f"{rv} is an advice ticket, not a review round")
        items = [dict(f) for f in t.get("findings") or []]
        f = next((x for x in items if x["n"] == int(n)), None)
        if f is None:
            raise RLError("NOT_FOUND", f"{ref} does not exist")
        if f["status"] not in ("open", "disputed"):
            raise RLError("CONFLICT", f"{ref} is {f['status']} already" + (
                f" ({(f.get('rejected') or {}).get('reason')})" if f["status"] == "rejected" else "")
                + ": a recorded answer is not rewritten; discuss it (rl discuss) instead")
        author = round_author(tx, t)
        owner = finding_owner(tx, t, f)
        if actor.name not in (author, owner):
            fl.add("not-holder", f"{ref} is for {owner}" + (f" or {author}" if author != owner else "")
                   + f"; {actor.name} rejected it")
        f.update(status="rejected", rejected={"by": actor.name, "at": now_utc(), "reason": reason})
        tx.update(rv, {"findings": items})
        written = fl.write(tx, rv)
        tx.event("review.rejected", rv, f"F{f['n']} by {actor.name}: {reason}")
        if round_ended(tx, t):
            warnings.append(f"{rv} has ended: no recheck reads the reason; it stays on the finding")
        elif t["status"] == "answered":
            nxt.append(f"the reviewer reads the reason at the recheck: rl review request {t.get('target_id')} "
                       f"--recheck-of {rv}")
        else:
            nxt.append("the reviewer reads the reason when it answers the waiting review")
    return {"id": rv, "summary": f"{ref} rejected: {reason}", "flags": written, "warnings": warnings, "next": nxt}


def finding_owner(tx: Tx, rv: dict, finding: dict | None) -> str:
    """Who answers for a finding: a method finding the scientist who made the task (a public task's scientist
    session), an engineering finding or the round as a whole its author."""
    task = tx.get(rv["task_id"], "Task") if rv.get("task_id") else None
    if finding and finding.get("category") == "method" and task and task.get("kind") in ("execute", "prep"):
        return task.get("scientist_session") if task["creator"] == "coordinator" and task.get("scientist_session") \
            else task["creator"]
    return round_author(tx, rv)


# -- advice ---------------------------------------------------------------------------------------------------


def consult(store: Store, actor: Actor, question: str) -> dict:
    """A scientist asks an advisor (8.7); the advisor session is told (result wake)."""
    fl = Flags(actor.name if actor else "?")
    need(actor, "scientist", what="a consultation", flags=fl)
    with store.write() as tx:
        ad = tx.next_id("Ticket", "AD")
        task = actor.subject if actor.subject and actor.subject.startswith("T-") else None
        tx.create("Ticket", {"id": ad, "kind": "advice", "status": "waiting", "advice_kind": "consult",
                             "task_id": task, "requested_by": actor.name, "question": question})
        written = fl.write(tx, ad)
        tx.event("advice.requested", ad, question[:300])
    return {"id": ad, "summary": f"{ad} waits for an advisor", "flags": written,
            "wake": [session_name("advisor", ad)]}


def advise(store: Store, actor: Actor, ad: str, answer_doc: str) -> dict:
    """The advisor's answer, a document on the ticket; whoever asked is told (result wake)."""
    fl = Flags(actor.name if actor else "?")
    need(actor, "advisor", what="answering an advice ticket", flags=fl)
    prep = documents.prepare(store.project, answer_doc, "advice")
    with store.write() as tx:
        t = tx.require(ad, "Ticket")
        if t["kind"] != "advice":
            raise RLError("INVALID", f"{ad} is a review round: rl review answer {ad} ..")
        if t["status"] != "waiting":  # its answer is recorded: a second one would rewrite it
            raise RLError("CONFLICT", f"{ad} is {t['status']}: its answer is recorded ({t.get('answer_ref') or '-'})")
        if actor.name != session_name("advisor", ad):
            fl.add("not-holder", f"{ad} is advisor-{ad}'s; {actor.name} answered it")
        ref = documents.register(tx, prep, ad)
        tx.update(ad, {"status": "closed", "answer_ref": ref, "answered_at": now_utc()})
        written = fl.write(tx, ad)
        tx.event("advice.answered", ad, ref)
    return {"id": ad, "summary": f"{ad} answered ({ref}); {t['requested_by']} is told", "flags": written,
            "wake": [t["requested_by"]]}

