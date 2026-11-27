"""The rules table: who has to act now, computed from the records' states (design 8.7, 8.8).

There is no inbox of stored messages. Each rule reads states and yields to-do items: (type, recipient, subject,
ask, the command that does it, since when, a note). An item leaves the list when the state changes, so nothing is
acknowledged and nothing piles up. The same table feeds `rl inbox`, the packs, the watcher (which opens or wakes the
recipient's session) and `rl doctor` (every unfinished record waits on someone, or on a dated external event, or on
another record someone waits on).

A record a role holds is not on its list while part of it waits on someone else or on something external (49):
a job before its expected end, a review round waiting for its reviewer, a task in a researcher's hands, a decision
request waiting for the coordinator or the user. When that ends, the item that says so comes to the holder.

Recipients are session names (coordinator, scientist-T-0040, researcher-T-0042, reviewer-RV-0036, advisor-AD-0003)
and "user".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .project import limit
from .reviews import ENDED, ended, open_findings, pass_key, target_text
from .roles import session_name
from .schema import TASK_DONE
from .store import Tx
from .util import RLError, now_utc, parse_time


@dataclass
class Item:
    type: str
    to: str
    subject: str
    ask: str
    cmd: str
    since: str
    note: str | None = None
    frm: str | None = None
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        """The item's identity for the sessions' told record; a review item carries its pass, so a recheck on the
        same ticket is news to the reviewer and its verdict to the author; an item about a task its revision, so a
        task delivered again or returned again is news to the one it goes to."""
        base = f"{self.type}:{self.subject}:{self.to}"
        if "pass" in self.extra:
            return f"{base}#{self.extra['pass']}"
        base = f"{base}@{self.extra['rev']}" if self.extra.get("rev") is not None else base
        return base + ("+prerequisites" if self.extra.get("prerequisites") else "")


TEST_PURPOSES = ("merge-test", "design-test", "review-test")  # runs that are not the task's evidence


def pass_of(r: dict) -> dict:
    """A review item's pass number (Item.key)."""
    return {"pass": len(r.get("passes") or [])}


def _ts(value: str | None) -> datetime:
    return parse_time(value) if value else datetime.now(timezone.utc)


def minutes_since(at: str | None, now: datetime | None = None) -> float:
    return ((now or datetime.now(timezone.utc)) - _ts(at)).total_seconds() / 60


# Events that do not change what is waited on: the watcher's own notices and reminders, and session bookkeeping.
# An item's age counts from the last change of its records, so a reminder never restarts its deadline.
QUIET_EVENTS = ("notice", "session.opened", "session.closed", "session.cleared", "session.failed",
                "session.trusted", "session.answered")


def _last_event(tx: Tx, ids: list[str]) -> str | None:
    times = [e["created_at"] for i in ids for e in tx.all("Event", entity_id=i) if e["type"] not in QUIET_EVENTS]
    return max(times) if times else None


class Rules:
    """One pass over the records (read once, kept in memory) that yields every to-do item."""

    def __init__(self, tx: Tx, config: dict | None = None):
        self.tx = tx
        self.config = config or {}
        self.tasks = tx.all("Task")
        self.by_id = {t["id"]: t for t in self.tasks}
        self.tickets = tx.all("Ticket")  # in id order, as reviews.latest_round reads them
        self.latest: dict[tuple, dict] = {}  # (task, review kind) -> its newest round, the same choice
        for t in self.tickets:
            if t["kind"] == "review" and t.get("task_id"):
                self.latest[(t["task_id"], t.get("review_kind"))] = t
        self.asks = tx.all("Ask")
        self.attempts = tx.all("Attempt")
        self.runs = {r["id"]: r for r in tx.all("Run")}
        self.decisions = tx.all("Decision")
        self.messages = tx.all("Message")
        self.sessions = tx.all("Session")
        self.project = getattr(tx, "project", None)
        self.now = datetime.now(timezone.utc)
        self.items: list[Item] = []

    # -- shared facts -------------------------------------------------------------------------------------------

    def ready(self, task: dict) -> bool:
        """Every task it depends on is accepted: the rule claim checks (tasks.claim)."""
        deps = [self.by_id.get(e["id"]) for e in self.tx.out(task["id"], "depends_on")]
        return all(d and d["state"] == "accepted" for d in deps)

    def children(self, tid: str) -> list[dict]:
        return [self.by_id[e["id"]] for e in self.tx.inc(tid, "part_of") if e["id"] in self.by_id]

    def scientist_for(self, task: dict) -> str:
        """Who answers for an execute or prep task: its creator, or the short scientist session of a public one."""
        if task["creator"] == "coordinator":
            return task.get("scientist_session") or "coordinator"
        return task["creator"]

    def running_jobs(self, submitter: str) -> list[dict]:
        out = []
        for a in self.attempts:
            if a.get("submitted_by") != submitter or a["execution_state"] == "ended":
                continue
            if a.get("expected_end") and _ts(a["expected_end"]) > self.now:
                out.append(a)
        return out

    def waiting_rounds(self, task_ids: list[str]) -> list[dict]:
        return [t for t in self.tickets if t["kind"] == "review" and t["status"] == "waiting"
                and (t.get("task_id") in task_ids)]

    def open_asks(self, tid: str) -> list[dict]:
        return [a for a in self.asks if a.get("task_id") == tid and a["kind"] == "decision"
                and a["state"] in ("open", "forwarded")]

    def scientist_waits(self, task: dict) -> str | None:
        """Why a scientist's task waits on someone else or on something external (None: it waits on the scientist)."""
        kids = self.children(task["id"])
        holder = task.get("holder")
        mine = [task["id"]] + [k["id"] for k in kids]
        with_researchers = [k["id"] for k in kids if k["state"] in ("active",) and k.get("holder", "").startswith(
            "researcher-")]
        if with_researchers:
            return f"tasks with researchers: {', '.join(with_researchers)}"
        rounds = self.waiting_rounds(mine) + [t for t in self.tickets if t["kind"] == "review"
                                              and t["status"] == "waiting" and t.get("review_kind") == "conclusion"
                                              and t.get("task_id") == task["id"]]
        if rounds:
            return f"review {rounds[0]['id']} with its reviewer"
        if self.open_asks(task["id"]):
            return f"decision request {self.open_asks(task['id'])[0]['id']}"
        consult = [t for t in self.tickets if t["kind"] == "advice" and t["status"] == "waiting"
                   and t.get("requested_by") == holder]
        if consult:
            return f"advice {consult[0]['id']}"
        jobs = self.running_jobs(holder) if holder else []
        if jobs:
            return f"job {jobs[0]['id']} until {jobs[0]['expected_end'][11:16]}"
        if any(k["state"] == "merging" for k in kids):
            return "a merge test job"
        return None

    def researcher_waits(self, task: dict) -> str | None:
        rounds = self.waiting_rounds([task["id"]])
        if rounds:
            return f"review {rounds[0]['id']}"
        jobs = self.running_jobs(task.get("holder") or "")
        if jobs:
            return f"job {jobs[0]['id']} until {jobs[0]['expected_end'][11:16]}"
        return None

    def since(self, ids: list[str], fallback: str | None = None) -> str:
        return _last_event(self.tx, ids) or fallback or now_utc()

    def add(self, **kw) -> None:
        self.items.append(Item(**kw))

    # -- the rules ----------------------------------------------------------------------------------------------

    def run(self) -> list[Item]:
        for rule in (self.scientist_tasks, self.researcher_tasks, self.task_results, self.reviews, self.advice,
                     self.jobs, self.coordinator, self.user, self.asks_answered, self.upstream, self.usage,
                     self.dialogue, self.sessions_blocked, self.stopped):
            rule()
        return self.items

    def scientist_tasks(self) -> None:
        """Hypothesis and phase tasks (tree tasks in older records) and handed-over public prep tasks, on the
        scientist's list unless they wait on others."""
        for t in self.tasks:
            if t["kind"] in ("hypothesis", "tree", "phase"):
                who = session_name("scientist", t["id"])
                if t["state"] == "open" and not t.get("holder") and t.get("released_at") and self.ready(t):
                    # released before its prerequisites were accepted, and not handed on since (older records, or
                    # a worktree that could not be made): its session takes it
                    self.add(type=f"{t['kind']}-task", to=who, subject=t["id"], ask=f"claim {t['id']}, then work on it",
                             cmd=f"rl task claim {t['id']}", since=t.get("released_at") or now_utc(),
                             frm="coordinator", note=t.get("reason"))
                elif t["state"] == "active" and t.get("holder") == who:
                    waits = self.scientist_waits(t)
                    if waits:
                        continue
                    targets = ', '.join(e['id'] for e in self.tx.out(t['id'], 'targets'))
                    ask = {"hypothesis": f"test {targets}", "tree": "an older tree task: deliver or ask the "
                           "coordinator to cancel it", "phase": "write the phase conclusion"}[t["kind"]]
                    kids = self.children(t["id"])
                    ended = [k for k in kids if k["state"] in TASK_DONE]
                    if kids and len(ended) == len(kids) and t["kind"] == "hypothesis":
                        ask = (f"its tasks have ended: design the next one, or write the verdict on {targets} "
                               "(rl decide --kind verdict ..), have it reviewed, deliver")
                    # each return is news, and so is each of its tasks ending: a scientist that ended its turn
                    # after accepting the last one is woken for the verdict (Codex trial, 11-25)
                    rev = t["revision"] if t.get("return_note") else f"done{len(ended)}" if ended else None
                    self.add(type=f"{t['kind']}-task" if not t.get("return_note") else "returned", to=who,
                             subject=t["id"], ask=ask, cmd=f"rl task deliver {t['id']}",
                             extra={"rev": rev} if rev is not None else {},
                             since=self.since([t["id"]] + [k["id"] for k in self.children(t["id"])]),
                             frm="coordinator", note=_with_flags(t, t.get("return_note")))
            elif t["kind"] == "prep" and t["creator"] == "coordinator" and t.get("scientist_session") \
                    and t["state"] not in TASK_DONE:
                if t["state"] == "blocked":
                    self.add(type="prep-to-scientist", to=t["scientist_session"], subject=t["id"],
                             extra={"rev": t["revision"]},
                             ask="answer the blocker, change the requirement or done check, or cancel",
                             cmd=f"rl task answer {t['id']} | rl task cancel {t['id']}", since=self.since([t["id"]]),
                             frm="coordinator", note=t.get("block_note"))

    def researcher_tasks(self) -> None:
        for t in self.tasks:
            if t["kind"] not in ("execute", "prep"):
                continue
            who = session_name("researcher", t["id"])
            if t["state"] == "open" and t["creator"] == "coordinator" and t.get("released_at") and not t.get("holder") \
                    and self.ready(t):
                # released before its prerequisites were accepted and not handed on since: its session takes it
                self.add(type="prep-task", to=who, subject=t["id"], ask=f"claim {t['id']}, write the code, run it "
                         "until the done check passes, deliver", cmd=f"rl task claim {t['id']}",
                         since=t["released_at"], frm="coordinator", note=t.get("requirement"))
            if t["state"] == "active" and t.get("holder") == who and not self.researcher_waits(t):
                kind = "returned" if t.get("return_note") else "merge-conflict" if t.get("merge_state") == "conflict" \
                    else "task-card"
                note = t.get("return_note") or t.get("merge_note") or t.get("answer_note") or t.get("dispatch_note")
                deps = self.deps(t)
                waiting = [d for d in deps if d["state"] != "accepted"]
                extra = {"rev": t["revision"]}  # an answer to its blocker or a return is news on the same card
                if waiting:  # handed on before its prerequisites were accepted (5.2): who it waits on
                    note = (f"waits on {', '.join(d['id'] for d in waiting)} (not accepted yet): merge the mainline "
                            "into the branch once it is" + (f" · {note}" if note else ""))
                elif deps and any(f["code"] == "prerequisite" for f in t.get("flags") or []):
                    note = (f"{', '.join(d['id'] for d in deps)} accepted now: merge the mainline into the branch"
                            + (f" · {note}" if note else ""))
                    extra = {**extra, "prerequisites": "in"}  # news to a researcher told while it waited
                self.add(type=kind, to=who, subject=t["id"], extra=extra,
                         ask="fix engineering findings, run, deliver"
                         if t["kind"] == "execute" else "write the code, pass the done check, deliver",
                         cmd=f"rl task deliver {t['id']}", since=self.since([t["id"]]), frm=t["creator"],
                         note=_with_flags(t, note))

    def task_results(self) -> None:
        """What comes back to the one who made a task: its conclusion, a merge conflict, a blocker, its
        prerequisites accepted."""
        for t in self.tasks:
            if t["kind"] not in ("execute", "prep"):
                continue
            sci = self.scientist_for(t)
            public = t["creator"] == "coordinator"
            if t["state"] == "delivered" and (not public or t.get("merge_state") == "conflict"
                                              or any(f["code"] == "done-check" for f in t.get("flags") or [])):
                conflict = t.get("merge_state") == "conflict"
                # a public preparation task: its failed merge tests wait on the coordinator (tasks.decider); its
                # failed done check on the one who receives it (the coordinator, or the scientist it was handed to)
                to = sci if not public else "coordinator" if conflict else (t.get("scientist_session") or "coordinator")
                self.add(type="merge-conflict" if conflict else "task-conclusion", to=to, subject=t["id"],
                         extra={"rev": t["revision"]},
                         ask=f"accept {t['id']} (it merges) or return it" if not conflict else
                         "its tests fail on the merge commit: accept it all the same (--force --reason) or return it"
                         if public else "open a small task to resolve it, or fix and accept again",
                         cmd=f"rl task accept {t['id']} | rl task return {t['id']} --reason .." if not conflict
                         else f"rl task accept {t['id']} --force --reason .. | rl task return {t['id']} --reason .."
                         if public else f"rl task accept {t['id']} | rl task return {t['id']} --reason ..",
                         since=t.get("delivered_at") or now_utc(), frm=t.get("holder"),
                         note=_with_flags(t, t.get("merge_note") if conflict else t.get("state_note")
                                          or t.get("conclusion")))
            elif t["state"] == "blocked":
                self.add(type=f"{t.get('block_reason') or 'design'}-block", to=sci, subject=t["id"],
                         extra={"rev": t["revision"]},  # blocked again later: news again
                         ask="answer, change the design or mandate, or cancel"
                         if sci != "coordinator" else f"hand it to a scientist: rl task release {t['id']} --to "
                                                      "scientist",
                         cmd=f"rl task answer {t['id']} --text .." if sci != "coordinator"
                         else f"rl task release {t['id']} --to scientist",
                         since=self.since([t["id"]]), frm=t.get("holder"), note=t.get("block_note"))
            elif t["state"] == "open" and not public and not t.get("holder"):
                deps = self.deps(t)
                if self.ready(t):
                    self.add(type="prerequisites-accepted", to=t["creator"], subject=t["id"],
                             ask=f"claim {t['id']}, write its code or dispatch it", cmd=f"rl task claim {t['id']}",
                             since=self.since([d["id"] for d in deps], t.get("created_at")), frm="cli")

    def reviews(self) -> None:
        """Review rounds (2026-11-27, chapter 6). The reviewer has the review that waits. The author has the verdict
        while the round waits on it (findings open after the first review: fix and ask for the recheck, or go on).
        When a round ends, its next step is an item on the work it reviewed (the task, or the draft): dispatch, go
        on, or finalize, until the author moves on; the ended round itself is on no list (4). Nothing of a round is
        on anyone's list once its task ended or a newer round on the same work was opened, nor once its author went
        on (dispatched, ran, delivered, wrote a newer draft): those rounds rest until their task ends (self.resting,
        which rl doctor reads)."""
        self.resting: set[str] = set()
        decisions = {d["id"]: d for d in self.decisions}
        for r in self.tickets:
            if r["kind"] != "review":
                continue
            task = self.by_id.get(r.get("task_id") or "")
            if task and task["state"] in TASK_DONE:
                self.resting.add(r["id"])
                continue
            reviewer = session_name("reviewer", r["id"])
            p = (r.get("passes") or [{}])[-1]
            if r["status"] == "waiting":
                tests = [a for a in self.attempts if self.runs.get(a["run_id"], {}).get("ticket_id") == r["id"]]
                running = [a for a in tests if a["execution_state"] != "ended"
                           and a.get("expected_end") and _ts(a["expected_end"]) > self.now]
                if running:
                    continue
                at, n = target_text(p), len(r.get("passes") or [])
                note = p.get("question")
                moved = (p.get("retargeted") or [{}])[-1]
                if p.get("retargeted"):
                    diff = f"git diff {moved['commit'][:12]} {p['commit'][:12]}" if moved.get("commit") and \
                        p.get("commit") else f"rl show {p.get('target')}"
                    note = f"moved from {target_text(moved)} to {at} by {moved.get('by')}: {diff}" + (
                        f" · {note}" if note else "")
                self.add(type="review-request", to=reviewer, subject=r["id"],
                         ask=f"{'recheck' if n > 1 else 'review'} {r.get('target_id')} at {at} (review {n} of 2)",
                         cmd=f"rl review answer {r['id']} --at {at} --verdict pass|fail ..",
                         since=p.get("retargeted_at") or p.get("requested_at") or r["created_at"],
                         frm=p.get("requested_by"), note=_with_flags(r, note), extra={"pass": pass_key(r)})
                continue
            if r.get("review_kind") not in ("design", "conclusion") or r["status"] == "escalated":
                self.resting.add(r["id"])  # older records: tree rounds, escalated rounds
                continue
            newest = self.latest.get((r.get("task_id"), r.get("review_kind"))) if r.get("task_id") else None
            if newest and newest["id"] != r["id"]:
                self.resting.add(r["id"])  # a newer round on the same work says what comes next
                continue
            author = self.round_author(r)
            left = open_findings(r)
            over = r["status"] in ENDED
            how = (f"{r['id']} passed" if r["status"] == "passed" and not left else
                   f"{r['id']} ended ({r['status']}) with {len(left)} finding(s) open, flagged on it")
            since = r.get("answered_at") or now_utc()
            if r["review_kind"] == "design":
                if not task or task["state"] != "active" or task.get("holder") != author \
                        or self.went_on(task["id"], r["id"]):
                    self.resting.add(r["id"])  # its author went on (or the task is with someone else now)
                    continue
                work = task["id"]
                if not over:
                    self.add(type="review-verdict", to=author, subject=r["id"], frm=reviewer, since=since,
                             ask=(f"{len(left)} finding(s) open: fix them (or reject one with its reason: rl review "
                                  f"reject {r['id']}#Fn --reason ..), then ask for the recheck; or go on, and they "
                                  "stay on the record") if left else
                             "it failed without findings: read the answer, fix, then ask for the recheck",
                             cmd=f"rl review request {work} --recheck-of {r['id']}",
                             note=_with_flags(r, "; ".join(f"F{f['n']} {f['category']}/{f['severity']}/{f['status']}"
                                                           f": {f['text'][:80]}" for f in left[:4]) or None),
                             extra={"pass": pass_key(r)})
                elif author.startswith("scientist-"):
                    self.add(type="review-verdict", to=author, subject=work, frm=reviewer, since=since,
                             ask=f"{how}: dispatch {work}", cmd=f"rl task dispatch {work} --note ..",
                             note=_with_flags(r, None), extra={"pass": f"{r['id']}.{pass_key(r)}"})
                else:
                    self.add(type="review-verdict", to=author, subject=work, frm=reviewer, since=since,
                             ask=f"{how}: go on with {work} ("
                                 + ("run, then deliver)" if task["kind"] == "execute" else "pass the done check, "
                                                                                          "deliver)"),
                             cmd="rl pack", note=_with_flags(r, None), extra={"pass": f"{r['id']}.{pass_key(r)}"})
                continue
            d = decisions.get(p.get("target") or r.get("target_id") or "")
            newer = [x for x in self.decisions if r.get("task_id") and x.get("task_id") == r["task_id"] and d
                     and x["id"] != d["id"] and x["kind"] in ("verdict", "stop") and x["created_at"] > since]
            if not d or d["state"] != "draft" or newer or (task and task["state"] not in ("active", "blocked")):
                self.resting.add(r["id"])  # finalized, superseded by a newer draft, or its task delivered
                continue
            if not over:
                self.add(type="review-verdict", to=author, subject=r["id"], frm=reviewer, since=since,
                         ask=f"{len(left)} finding(s) open: write the revised draft (rl decide ..) and ask for the "
                             f"recheck, or reject a finding with its reason (rl review reject {r['id']}#Fn --reason ..)"
                             "; or go on, and they stay on the record",
                         cmd=f"rl review request D-y --recheck-of {r['id']}",
                         note=_with_flags(r, "; ".join(f"F{f['n']} {f['category']}/{f['severity']}/{f['status']}: "
                                                       f"{f['text'][:80]}" for f in left[:4]) or None),
                         extra={"pass": pass_key(r)})
            else:
                self.add(type="review-verdict", to=author, subject=d["id"], frm=reviewer, since=since,
                         ask=f"{how}: finalize {d['id']}" + ("" if r["status"] == "passed" and not left else
                                                             " (the flags go with it), or write a new draft and "
                                                             "open a new round"),
                         cmd=f"rl decide --final {d['id']}", note=_with_flags(r, None),
                         extra={"pass": f"{r['id']}.{pass_key(r)}"})

    def went_on(self, task_id: str, rv: str) -> bool:
        """An attempt of a non-test run of the task was registered after the round's last answer, by event seq (two
        in the same second keep their order)."""
        # review.settled: older records (an escalated finding settled by the scientist)
        answered = [e["seq"] for kind in ("review.answered", "review.settled")
                    for e in self.tx.all("Event", entity_id=rv, type=kind)]
        if not answered:
            return False
        mine = [a["id"] for a in self.attempts if (self.runs.get(a["run_id"]) or {}).get("task_id") == task_id
                and (self.runs.get(a["run_id"]) or {}).get("purpose") not in TEST_PURPOSES]
        return any(e["seq"] > max(answered) for att in mine
                   for e in self.tx.all("Event", entity_id=att, type="run.register"))

    def round_author(self, r: dict) -> str:
        """The session that opened the round (reviews.round_author; older records: the design task's holder)."""
        if r.get("author"):
            return r["author"]
        if r.get("review_kind") == "design":
            t = self.by_id.get(r.get("task_id"))
            if t and t.get("holder"):
                return t["holder"]
        return (r.get("passes") or [{}])[-1].get("requested_by") or r["requested_by"]

    def advice(self) -> None:
        """A consultation waits on its advisor; its advice waits on whoever asked until it decides after it. Ruling
        tickets are older records only (schema 6 closed the ones that waited)."""
        for t in self.tickets:
            if t["kind"] != "advice" or t.get("advice_kind") == "ruling":
                continue
            if t["status"] == "waiting":
                self.add(type="consultation", to=session_name("advisor", t["id"]), subject=t["id"], ask="advise",
                         cmd=f"rl advise answer {t['id']} --doc ..", since=t["created_at"], frm=t["requested_by"],
                         note=_with_flags(t, t.get("question")))
            elif t["status"] == "closed" and t.get("advice_kind") == "consult":
                later = [d for d in self.decisions if d.get("author") == t["requested_by"]
                         and d["created_at"] > (t.get("answered_at") or "")]
                task = self.by_id.get(t.get("task_id") or "")
                if not later and task and task["state"] not in TASK_DONE + ("delivered",):
                    self.add(type="advice", to=t["requested_by"], subject=t["id"], ask="read the advice and decide "
                             "(a short route decision)", cmd=f"rl show {t['answer_ref']}",
                             since=t.get("answered_at") or now_utc(), frm=session_name("advisor", t["id"]))

    def jobs(self) -> None:
        for t in self.tasks:  # an acceptance waits on a merge test that nobody registered for this round yet
            if t["state"] != "merging" or not t.get("merge_commit"):
                continue
            rounds = [r for r in self.runs.values() if r["task_id"] == t["id"] and r.get("purpose") == "merge-test"
                      and r.get("code_commit") == t["merge_commit"]
                      and ((r.get("spec") or {}).get("acceptance") or {}).get("round") == t["revision"]]
            if not rounds:
                public = t["kind"] == "prep" and t["creator"] == "coordinator"
                who = t.get("holder") if public else self.scientist_for(t)
                if who:
                    self.add(type="merge-test", to=who, subject=t["id"], ask="run the merge test of its acceptance "
                             "as a short job", cmd=f"rl run register --task {t['id']} --request-id <new id> --purpose "
                             "merge-test --minutes N [--resource R]", since=self.since([t["id"]]), frm="cli",
                             note=f"candidate {t['merge_commit'][:12]} (rl compute sync pushes its branch)")
        for a in self.attempts:
            run = self.runs.get(a["run_id"]) or {}
            who = a.get("submitted_by")
            if not who:
                continue
            if run.get("ticket_id"):
                who = session_name("reviewer", run["ticket_id"])
            if a["execution_state"] == "ended":
                if run.get("purpose") == "merge-test":  # its end decides the acceptance (tasks.finish_merge)
                    continue
                newer = run.get("attempt_count", 1) > a["index"]
                if not a.get("handled_at") and a["result_status"] == "absent" and not newer:
                    self.add(type="test-job-ended" if run.get("ticket_id") else "job-ended", to=who,
                             subject=a["id"], ask="analyse (import results) or retry within the mandate",
                             cmd=f"rl run import-result --attempt {a['id']} .. | rl run handled {a['id']} ..",
                             since=a.get("closed_at") or now_utc(), frm="executor",
                             note=a.get("ended_note") or a["outcome"])
            elif a.get("expected_end") and _ts(a["expected_end"]) <= self.now \
                    and a["execution_state"] in ("queued", "running", "unknown", "prepared"):
                never = a["execution_state"] == "prepared" and not a.get("last_observed_at")
                self.add(type="job-overdue", to=who, subject=a["id"],
                         ask="nothing was recorded since it was registered: check whether it was submitted, and "
                         "record what happened" if never else "check it with the executor and record what you find",
                         cmd=f"rl run record --attempt {a['id']} ..", since=a["expected_end"], frm="cli")

    def coordinator(self) -> None:
        for t in self.tasks:
            if t["state"] != "delivered" or t["kind"] not in ("hypothesis", "tree", "phase"):
                continue
            kind = {"hypothesis": "hypothesis-conclusion", "tree": "tree", "phase": "phase-conclusion"}[t["kind"]]
            report = next((a["id"] for a in self.asks if a["kind"] == "report" and a.get("task_id") == t["id"]
                           and a["state"] == "open"), None)
            self.add(type=kind, to="coordinator", subject=t["id"], ask=f"receive {t['id']} or return it",
                     extra={"rev": t["revision"]},  # each delivery (or a changed acceptance) is news
                     cmd=f"rl task accept {t['id']} | rl task return {t['id']} --reason ..",
                     since=t.get("delivered_at") or now_utc(), frm=t.get("holder"), note=_with_flags(t, (
                         t.get("state_note") or t.get("conclusion") or "") + (
                         f" · the user reads its report ({report}); record what they say: rl request answer "
                         f"{report} --by user --text .." if report else "")))
        for t in self.tasks:  # an older tree task still open: the tree is drafted by a subagent now (7.2)
            if t["kind"] == "tree" and t["state"] in ("queued", "open"):
                self.add(type="old-tree-task", to="coordinator", subject=t["id"], ask=f"cancel {t['id']}: the tree "
                         "is drafted by your subagent now (rl tree prompt)", cmd=f"rl task cancel {t['id']} --reason "
                         "\"the tree is drafted by a subagent\"", since=t.get("created_at") or now_utc(), frm="cli")
        self.tree_item()
        for a in self.asks:
            if a["state"] == "open" and a["kind"] == "decision":
                self.add(type="decision-request", to="coordinator", subject=a["id"], ask="decide, or forward to "
                         "the user", cmd=f"rl request answer {a['id']} --by coordinator .. | rl request forward "
                         f"{a['id']}", since=a["created_at"], frm=a["by"], note=a["stuck"])
        from .tasks import phase_ended
        for ph in self.tx.all("Phase"):
            if phase_ended(self.tx, ph["id"]):
                done = [t for t in self.tasks if t["kind"] == "phase" and t["state"] != "cancelled"
                        and any(e["id"] == ph["id"] for e in self.tx.out(t["id"], "targets"))]
                if not done or all(t["state"] == "queued" for t in done):
                    self.add(type="phase-ended", to="coordinator", subject=ph["id"],
                             ask="make and release the phase conclusion task",
                             cmd=f"rl task create --kind phase --phase {ph['id']} ..", since=self.since(
                                 [t["id"] for t in self.tasks if self.tx.out(t["id"], "in_phase")
                                  and self.tx.out(t["id"], "in_phase")[0]["id"] == ph["id"]]), frm="cli")
        for h in self.tx.all("Hypothesis"):  # composite hypotheses of older records
            if not h.get("composite") or h["evidence_status"] != "untested" or h["disposition"] in ("paused",
                                                                                                    "retired"):
                continue
            kids = [self.tx.require(e["id"]) for e in self.tx.inc(h["id"], "derived_from") if e.get("primary")]
            settled = [k for k in kids if k["evidence_status"] != "untested" or k["disposition"] in ("paused",
                                                                                                    "retired")]
            busy = [k for k in kids + [h] if any(t["state"] not in TASK_DONE for t in self.hyp_tasks(k["id"]))]
            if settled and not busy:
                self.add(type="children-settled", to="coordinator", subject=h["id"],
                         ask="conclude it now (a hypothesis task) or schedule the rest of its children",
                         cmd=f"rl task create --kind hypothesis --targets {h['id']} ..",
                         since=self.since([k["id"] for k in kids]), frm="cli",
                         note=f"undecided children: {', '.join(k['id'] for k in kids if k not in settled) or 'none'}")

    def hyp_tasks(self, hid: str) -> list[dict]:
        return [self.by_id[e["id"]] for e in self.tx.inc(hid, "targets") if self.by_id.get(e["id"], {}).get("kind")
                == "hypothesis"]

    def user(self) -> None:
        for a in self.asks:
            if a["kind"] == "approval" and a["state"] == "forwarded":
                self.add(type="approval", to="user", subject=a["id"], ask="approve or change: "
                         + (a.get("stuck") or ""), cmd="tell the coordinator", since=a["created_at"],
                         frm="coordinator", note=a.get("recommend"))
            elif a["kind"] == "decision" and a["state"] == "forwarded":
                self.add(type="decision-request", to="user", subject=a["id"], ask="decide",
                         cmd="tell the coordinator", since=a.get("forwarded_at") or a["created_at"],
                         frm="coordinator", note=a["stuck"])
            elif a["kind"] == "report" and a["state"] == "open":
                self.add(type="phase-report", to="user", subject=a["id"], ask="read the phase report on the "
                         "research page; say in the coordinator's pane that you read it, or what to change",
                         cmd=f"tell the coordinator (it records your words: rl request answer {a['id']} --by user "
                         "--text ..)", since=a["created_at"], frm="cli",
                         note=f"{a['stuck']} ({a['task_id']})")

    def asks_answered(self) -> None:
        """An answer stays with the scientist until it writes a decision citing the task after it (52)."""
        for a in self.asks:
            if a["kind"] != "decision" or a["state"] != "answered":
                continue
            task = self.by_id.get(a.get("task_id"))
            if not task or task["state"] in TASK_DONE or not task.get("holder"):
                continue
            if task["kind"] == "tree" and task["state"] == "delivered":
                continue
            done = [d for d in self.decisions if d["created_at"] >= a["answered_at"]
                    and any(e["id"] == task["id"] for e in self.tx.out(d["id"], "based_on"))]
            if not done:
                self.add(type="request-answered", to=task["holder"], subject=a["id"],
                         ask="go on as answered: a short decision citing the task (--based-on "
                             f"{task['id']}); a stop answer is a stop conclusion",
                         cmd=f"rl decide --kind route|stop .. --based-on {task['id']}", since=a["answered_at"],
                         frm=a.get("answered_by"), note=a.get("answer"))

    def upstream(self) -> None:
        """A final decision changed a hypothesis that an undecided one assumes (4.7)."""
        finals = [d for d in self.decisions if d["state"] == "final" and d["kind"] in ("verdict", "stop")]
        for d in finals:
            for e in self.tx.out(d["id"], "decides"):
                for dep in self.tx.inc(e["id"], "assumes"):
                    h = self.tx.require(dep["id"])
                    if h["evidence_status"] != "untested" or h["disposition"] in ("paused", "retired"):
                        continue
                    for t in self.hyp_tasks(h["id"]):
                        if t["state"] != "active" or not t.get("holder"):
                            continue
                        later = [x for x in self.decisions if x.get("task_id") == t["id"]
                                 and x["created_at"] >= (d.get("final_at") or d["created_at"])]
                        if not later:
                            self.add(type="upstream-changed", to=t["holder"], subject=t["id"],
                                     ask=f"{e['id']} changed ({d['id']}): continue, change the design or stop",
                                     cmd="rl decide --kind route|stop ..", since=d.get("final_at") or now_utc(),
                                     frm="cli", note=f"{h['id']} assumes {e['id']}")

    def usage(self) -> None:
        from .usage_view import _task_of, lines_crossed, task_ids
        shares = limit(self.config, "envelope_lines")
        charges = [(e["created_at"], {_task_of(self.tx, c["id"]) for c in self.tx.out(e["entity_id"], "covers")})
                   for e in self.tx.all("Event", type="run.usage")]
        for t in self.tasks:
            if t["kind"] != "hypothesis" or t["state"] not in ("active", "delivered") or not t.get("envelope"):
                continue
            crossed = lines_crossed(self.tx, t, shares)
            if not crossed:
                continue
            top = max(s for s, _ in crossed)
            mine = set(task_ids(self.tx, t))  # charges of this hypothesis's work only: another task's charge
            usage_times = [at for at, tasks in charges if tasks & mine]  # does not reopen a handled warning
            last_usage = max(usage_times) if usage_times else t["created_at"]
            line = {"rev": f"{top:g}"}  # the line crossed: a higher one is news to whoever was told of a lower
            later = [d for d in self.decisions if d.get("task_id") == t["id"] and d["created_at"] >= last_usage]
            env_changed = [e for e in self.tx.all("Event", entity_id=t["id"]) if e["type"] == "task.envelope"
                           and e["created_at"] >= last_usage]
            if not later and not env_changed and t["state"] == "active":
                self.add(type="usage-line", to=t["holder"], subject=t["id"], ask="adjust the plan, or make a "
                         "decision request", cmd="rl decide --kind route .. | rl request ..", since=last_usage,
                         frm="cli", note=f"past {top:.0%} of the envelope", extra=line)
            if top >= shares[-1] and not env_changed:
                self.add(type="usage-line", to="coordinator", subject=t["id"], ask="look at the envelope",
                         cmd=f"rl task set {t['id']} --envelope ..", since=last_usage, frm="cli",
                         note=f"past {top:.0%} of the envelope", extra=line)

    def dialogue(self) -> None:
        """Questions, review discussion and consultations: an entry waits for its addressee's reply (8.7); an answer
        waits for whoever asked to read it and close the question. Discussion in a round that has ended, or whose
        task ended, is on nobody's list: the same condition under which rl reply and rl discuss tell nobody (4)."""
        rounds = {t["id"]: t for t in self.tickets}
        replied = {m["in_reply_to"] for m in self.messages if m.get("in_reply_to")}
        for m in self.messages:
            if m["id"] in replied:
                continue
            if m["kind"] == "answer":
                if not m.get("closed_at"):
                    self.add(type="answer", to=m["to_role"], subject=m["subject"], ask="read the answer, then close "
                             "the question", cmd=f"rl question --close {m['in_reply_to']}", since=m["created_at"],
                             frm=m["from_role"], note=m["text"][:300])
                continue
            if m.get("closed_at"):
                continue
            r = rounds.get(m["subject"].split("#")[0]) if m["kind"] == "discussion" else None
            if r and ended(r, self.by_id.get(r.get("task_id") or "")):
                continue
            self.add(type={"question": "question", "discussion": "review-discussion",
                           "consult": "consultation"}[m["kind"]], to=m["to_role"],
                     subject=m["subject"] + (m.get("finding") or ""), ask="reply",
                     cmd=f"rl reply {m['id']} --text ..", since=m["created_at"], frm=m["from_role"],
                     note=m["text"][:300])

    def sessions_blocked(self) -> None:
        for s in self.sessions:
            if s["status"] == "open" and s.get("needs_decision"):
                to = "user" if s["role"] == "coordinator" else "coordinator"
                self.add(type="session-needs-decision", to=to, subject=s["name"], ask=f"answer {s['name']}'s dialog",
                         cmd=f"rl agent answer {s['name']} \"<option>\" --reason ..", since=s.get("last_active_at")
                         or now_utc(), frm="watch", note=s["needs_decision"][:300])

    def deps(self, task: dict) -> list[dict]:
        return [d for d in (self.by_id.get(e["id"]) for e in self.tx.out(task["id"], "depends_on")) if d]

    def tree_item(self) -> None:
        """No hypotheses yet: build the tree with a subagent (7.2), or, with a draft written, have the user look at
        it (so a restarted coordinator does not open a second subagent)."""
        if any(True for _ in self.tx.all("Hypothesis")):
            return
        from . import tree
        from .project import tree_entry
        draft = tree.has_draft(self.project) if self.project else False
        coordinator = next((s for s in reversed(self.sessions) if s["name"] == "coordinator"), None)
        host = (coordinator or {}).get("host") or "claude"
        entry = tree_entry(self.config, host)
        model = f"{entry.model}" + (f" at effort {entry.effort}" if entry and entry.effort else "") if entry else \
            "the best model of your family at effort xhigh"
        if draft:
            self.add(type="tree-draft", to="coordinator", subject="tree", ask="have the user read the tree draft on "
                     "the research page (rl view draws it); change it with a subagent as they say, record it once "
                     "they approve", cmd="rl tree record --approval \"<the user's words>\"",
                     since=now_utc(), frm="cli", note="a draft is in research/tree-draft/tree.yaml")
        else:
            self.add(type="build-tree", to="coordinator", subject="tree", ask=f"open a subagent ({model}) and tell "
                     "it: run `rl tree prompt`, do what it says, hand back the summary it asks for",
                     cmd="rl tree prompt [--from <document>...]", since=now_utc(), frm="cli",
                     note="it drafts the hypothesis tree in research/tree-draft/; you show the user the draft")

    def stopped(self) -> None:
        """A session still open on a task cancelled, or taken from it (3.2): told once to stop working on it; the
        item leaves when its session closes."""
        open_names = {s["name"] for s in self.sessions if s["status"] == "open"}
        for t in self.tasks:
            for role in ("researcher", "scientist"):
                who = session_name(role, t["id"])
                if who not in open_names or (role == "scientist" and t["kind"] in ("execute", "prep")
                                             and t.get("scientist_session") != who):
                    continue
                if t["state"] == "cancelled":
                    self.add(type="task-cancelled", to=who, subject=t["id"], ask=f"stop working on {t['id']}: it was "
                             "cancelled; end your turn", cmd=f"rl show {t['id']}", since=self.since([t["id"]]),
                             frm="cli", note=t.get("cancel_reason"))
                elif t["state"] in ("active", "blocked") and t.get("holder") and t["holder"] != who and any(
                        h.get("holder") == who for h in t.get("holders") or []) and not (
                        role == "scientist" and t["kind"] in ("execute", "prep")):
                    self.add(type="task-taken", to=who, subject=t["id"], ask=f"stop working on {t['id']}: "
                             f"{t['holder']} holds it now; end your turn", cmd=f"rl show {t['id']}",
                             since=self.since([t["id"]]), frm=t["holder"], extra={"rev": t["revision"]})



def all_items(tx: Tx, config: dict) -> list[Item]:
    return Rules(tx, config).run()


def for_recipient(tx: Tx, config: dict, who: str) -> list[Item]:
    return [i for i in all_items(tx, config) if i.to == who]


def recipients(tx: Tx, config: dict, types: tuple[str, ...]) -> list[str]:
    """The sessions that have items of these types now (a command tells them: 3.2)."""
    return sorted({i.to for i in all_items(tx, config) if i.type in types and i.to != "user"})


def _with_flags(rec: dict, note: str | None) -> str | None:
    """A to-do note with the record's process flags after it (5.1: the receiver sees them)."""
    from .flags import short
    found = short(rec)
    return " · ".join(x for x in (note, found) if x) or None


def age(minutes: float) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} d"


def render(items: list[Item], who: str) -> str:
    """At most four lines per item: type and subject, ask, from whom and how long, a note (8.11)."""
    lines = [f"inbox · {who} · {len(items)} item(s)"]
    for n, it in enumerate(items, 1):
        lines.append(f"{n} {it.type}  {it.subject}")
        lines.append(f"  ask   {it.ask}")
        lines.append(f"  from  {it.frm or '-'} · {age(minutes_since(it.since))}")
        if it.note:
            lines.append(f"  note  {' '.join(str(it.note).split())[:300]}")
    return "\n".join(lines)


def unwatched(tx: Tx, config: dict) -> list[str]:
    """The invariant of 8.8 for rl doctor: every unfinished record waits on a role, on a dated external event, or
    on another record someone waits on. What waits on nobody was missed."""
    rules = Rules(tx, config)
    items = rules.run()
    subjects = {i.subject for i in items}
    out = []
    for t in rules.tasks:
        if t["state"] in TASK_DONE or t["id"] in subjects:
            continue
        if t["state"] == "queued":
            continue  # on the coordinator's schedule
        if t["state"] in ("active",) and t.get("holder", "").startswith("scientist-") and rules.scientist_waits(t):
            continue
        if t["state"] == "active" and t.get("holder", "").startswith("researcher-") and rules.researcher_waits(t):
            continue
        if t["state"] == "open":
            deps = [rules.by_id.get(e["id"]) for e in tx.out(t["id"], "depends_on")]
            if any(d and d["state"] != "accepted" for d in deps):
                continue  # waits on its prerequisites
        if t["state"] == "merging":
            continue  # waits on its test job
        if t["state"] == "active" and t.get("holder") and t["holder"] in {i.to for i in items}:
            continue
        out.append(f"{t['id']} {t['kind']}/{t['state']} waits on nobody")
    resting = getattr(rules, "resting", set())
    for r in rules.tickets:
        if r["status"] not in ("waiting", "answered") or r["id"] in subjects:
            continue
        if r["id"] in resting:
            continue  # its task ended, a newer round took over, or its author went on: it ends with its task (3.3)
        if r["status"] == "waiting":
            continue  # a running test job on the round; an older ruling (schema 6 closed those)
        out.append(f"{r['id']} {r['status']} waits on nobody")
    for a in rules.asks:
        if a["state"] in ("open", "forwarded") and a["id"] not in subjects and a.get("task_id") not in subjects:
            out.append(f"{a['id']} {a['state']} waits on nobody")
    return out


ABOUT_SESSIONS = ("task-cancelled", "task-taken", "session-needs-decision")  # items about a session, not a record


def leftovers(tx: Tx, config: dict) -> list[str]:
    """The reverse check of rl doctor (4): no to-do item on a record that has ended (a task accepted or cancelled,
    a round passed or closed, a request answered or closed)."""
    out = []
    for i in all_items(tx, config):
        if i.type in ABOUT_SESSIONS:
            continue
        try:
            rec = tx.get(i.subject.split("#")[0])
        except RLError:  # a subject that names no record (the tree, a session)
            rec = None
        if rec is None:
            continue
        ended = (rec["_label"] == "Task" and rec["state"] in TASK_DONE) or \
            (rec["_label"] == "Ticket" and rec["status"] in ("passed", "closed")) or \
            (rec["_label"] == "Ask" and rec["state"] in ("answered", "closed") and i.type != "request-answered")
        if ended:
            out.append(f"{i.type} for {i.to} on {i.subject}, which is {rec.get('state') or rec.get('status')}")
    return out
