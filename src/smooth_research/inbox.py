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

from .project import deadline_minutes, limit
from .reviews import blocking, last_pass, latest_round, open_findings
from .roles import session_name
from .schema import TASK_DONE
from .store import Tx, id_order
from .util import now_utc, parse_time


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
    timed: bool = True  # counts toward the deadline (False while it waits for a session slot)
    level: int = 0  # escalation level for a timeout item (1 the creator, 2 the coordinator, 3 the user)
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        """The item's identity for the watcher's told record; a review item carries its pass, so a recheck on the
        same ticket is news to the reviewer and its verdict to the author."""
        base = f"{self.type}:{self.subject}:{self.to}"
        return f"{base}#{self.extra['pass']}" if "pass" in self.extra else base


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
        self.tickets = tx.all("Ticket")
        self.asks = tx.all("Ask")
        self.attempts = tx.all("Attempt")
        self.runs = {r["id"]: r for r in tx.all("Run")}
        self.decisions = tx.all("Decision")
        self.messages = tx.all("Message")
        self.sessions = tx.all("Session")
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
        return [a for a in self.asks if a["task_id"] == tid and a["kind"] == "decision"
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
                     self.dialogue, self.sessions_blocked):
            rule()
        self.items += escalations(self.items, self.config, self.now, self.by_id)
        return self.items

    def scientist_tasks(self) -> None:
        """Hypothesis, tree and phase tasks and handed-over public prep tasks, on the scientist's list unless they
        wait on others; released ones not yet claimed wait for a session slot."""
        for t in self.tasks:
            if t["kind"] in ("hypothesis", "tree", "phase"):
                who = session_name("scientist", t["id"])
                if t["state"] == "open" and self.ready(t):
                    self.add(type=f"{t['kind']}-task", to=who, subject=t["id"], ask=f"work on {t['id']}",
                             cmd="rl pack", since=t.get("released_at") or now_utc(), frm="coordinator", timed=False,
                             note=t.get("reason"))
                elif t["state"] == "active" and t.get("holder") == who:
                    waits = self.scientist_waits(t)
                    if waits:
                        continue
                    ask = {"hypothesis": f"test {', '.join(e['id'] for e in self.tx.out(t['id'], 'targets'))}",
                           "tree": "build the hypothesis tree", "phase": "write the phase conclusion"}[t["kind"]]
                    self.add(type=f"{t['kind']}-task" if not t.get("return_note") else "returned", to=who,
                             subject=t["id"], ask=ask, cmd=f"rl task deliver {t['id']}",
                             since=self.since([t["id"]] + [k["id"] for k in self.children(t["id"])]),
                             frm="coordinator", note=t.get("return_note"))
            elif t["kind"] == "prep" and t["creator"] == "coordinator" and t.get("scientist_session") \
                    and t["state"] not in TASK_DONE:
                if t["state"] == "blocked" or any(r["status"] == "escalated" for r in self.tickets
                                                  if r.get("task_id") == t["id"]):
                    self.add(type="prep-to-scientist", to=t["scientist_session"], subject=t["id"],
                             ask="answer the blocker, change the requirement or done check, or cancel",
                             cmd=f"rl task answer {t['id']} | rl task cancel {t['id']}", since=self.since([t["id"]]),
                             frm="coordinator", note=t.get("block_note"))

    def researcher_tasks(self) -> None:
        for t in self.tasks:
            if t["kind"] not in ("execute", "prep"):
                continue
            who = session_name("researcher", t["id"])
            if t["state"] == "open" and t["creator"] == "coordinator" and t.get("released_at"):
                if self.ready(t):
                    self.add(type="prep-task", to=who, subject=t["id"], ask="write the code, run it until the done "
                             "check passes, deliver", cmd=f"rl task deliver {t['id']}", since=t["released_at"],
                             frm="coordinator", timed=False, note=t.get("requirement"))
            if t["state"] == "active" and t.get("holder") == who and not self.researcher_waits(t):
                kind = "returned" if t.get("return_note") else "merge-conflict" if t.get("merge_state") == "conflict" \
                    else "task-card"
                note = t.get("return_note") or t.get("merge_note") or t.get("answer_note") or t.get("dispatch_note")
                self.add(type=kind, to=who, subject=t["id"], ask="fix engineering findings, run, deliver"
                         if t["kind"] == "execute" else "write the code, pass the done check, deliver",
                         cmd=f"rl task deliver {t['id']}", since=self.since([t["id"]]), frm=t["creator"], note=note)

    def task_results(self) -> None:
        """What comes back to the scientist who made a task: its conclusion, a merge conflict, a blocker, its
        prerequisites accepted."""
        for t in self.tasks:
            if t["kind"] not in ("execute", "prep"):
                continue
            sci = self.scientist_for(t)
            if t["state"] == "delivered" and t["creator"] != "coordinator":
                conflict = t.get("merge_state") == "conflict"
                self.add(type="merge-conflict" if conflict else "task-conclusion", to=sci, subject=t["id"],
                         ask=f"accept {t['id']} (it merges) or return it" if not conflict else
                         "open a small task to resolve it, or fix and accept again",
                         cmd=f"rl task accept {t['id']} | rl task return {t['id']} --reason ..",
                         since=t.get("delivered_at") or now_utc(), frm=t.get("holder"),
                         note=t.get("merge_note") if conflict else t.get("state_note") or t.get("conclusion"))
            elif t["state"] == "blocked":
                self.add(type=f"{t.get('block_reason') or 'design'}-block", to=sci, subject=t["id"],
                         ask="answer, change the design or mandate, or cancel"
                         if sci != "coordinator" else f"hand it to a scientist: rl task release {t['id']} --to "
                                                      "scientist",
                         cmd=f"rl task answer {t['id']} --text .." if sci != "coordinator"
                         else f"rl task release {t['id']} --to scientist",
                         since=self.since([t["id"]]), frm=t.get("holder"), note=t.get("block_note"))
            elif t["state"] == "open" and t["creator"] != "coordinator" and not t.get("holder"):
                deps = [self.by_id.get(e["id"]) for e in self.tx.out(t["id"], "depends_on")]
                if self.ready(t):
                    self.add(type="prerequisites-accepted", to=t["creator"], subject=t["id"],
                             ask=f"claim {t['id']}, write its code or dispatch it", cmd=f"rl task claim {t['id']}",
                             since=self.since([d["id"] for d in deps], t.get("created_at")), frm="cli")

    def reviews(self) -> None:
        for r in self.tickets:
            if r["kind"] != "review":
                continue
            reviewer = session_name("reviewer", r["id"])
            if r["status"] == "waiting":
                tests = [a for a in self.attempts if self.runs.get(a["run_id"], {}).get("ticket_id") == r["id"]]
                running = [a for a in tests if a["execution_state"] != "ended"
                           and a.get("expected_end") and _ts(a["expected_end"]) > self.now]
                if running:
                    continue
                lp = (r.get("passes") or [{}])[-1]
                self.add(type="review-request", to=reviewer, subject=r["id"], ask="review",
                         cmd=f"rl review answer {r['id']} --verdict pass|fail ..",
                         since=lp.get("requested_at") or r["created_at"], frm=lp.get("requested_by"),
                         note=lp.get("question"), extra=pass_of(r))
                continue
            author = self.round_author(r)
            if r["status"] == "answered":
                self.add(type="review-verdict", extra=pass_of(r), to=author, subject=r["id"],
                         ask="fix, then request a recheck" if blocking(r) else "go on: dispatch, run or deliver",
                         cmd=f"rl review request {r.get('target_id')} --recheck-of {r['id']}",
                         since=r.get("answered_at") or now_utc(), frm=reviewer,
                         note=f"{len(open_findings(r))} open finding(s), {len(blocking(r))} blocking")
            elif r["status"] == "passed" and r.get("review_kind") == "conclusion":
                d = next((x for x in self.decisions if x["id"] == r.get("target_id")), None)
                if d and d["state"] == "draft":
                    self.add(type="review-verdict", extra=pass_of(r), to=author, subject=r["id"],
                             ask=f"finalize {d['id']}", cmd=f"rl decide --final {d['id']}",
                             since=r.get("answered_at") or now_utc(), frm=reviewer)
            elif r["status"] == "passed" and r.get("review_kind") == "design":
                t = self.by_id.get(r.get("task_id"))
                if t and (latest_round(self.tx, t["id"], "design") or {}).get("id") != r["id"]:
                    continue  # a newer round of the task decides (one that failed says fix, not go on)
                if t and t["state"] == "active" and t.get("holder", "").startswith("scientist-") \
                        and last_pass(r) and last_pass(r).get("segment") == "scientist":
                    self.add(type="review-verdict", extra=pass_of(r), to=t["holder"], subject=r["id"],
                             ask=f"dispatch {t['id']}", cmd=f"rl task dispatch {t['id']} --note ..",
                             since=r.get("answered_at") or now_utc(), frm=reviewer)
                elif t and t["state"] == "active" and t.get("holder", "").startswith("researcher-") \
                        and last_pass(r) and last_pass(r).get("segment") == "researcher":
                    # the researcher waits on this pass to run; its task card stays the item that times. An
                    # attempt of a run of the task registered after the pass (by the event order) shows it went on.
                    if self.went_on(t["id"], r["id"]):
                        continue
                    self.add(type="review-verdict", extra=pass_of(r), to=t["holder"], subject=r["id"],
                             ask=f"the review passed: go on with {t['id']} (run, then deliver)",
                             cmd="rl pack", since=r.get("answered_at") or now_utc(), frm=reviewer, timed=False)
            elif r["status"] == "passed" and r.get("review_kind") == "tree":
                t = self.by_id.get(r.get("task_id"))
                # only the task's latest tree round says deliver
                if t and t["state"] == "active" and t.get("holder") and \
                        (latest_round(self.tx, t["id"], "tree") or {}).get("id") == r["id"]:
                    self.add(type="review-verdict", extra=pass_of(r), to=t["holder"], subject=r["id"],
                             ask=f"deliver {t['id']}", cmd=f"rl task deliver {t['id']} ..",
                             since=r.get("answered_at") or now_utc(), frm=reviewer)
            elif r["status"] == "escalated" and r.get("segment") == "researcher":
                t = self.by_id.get(r.get("task_id"))
                if t:
                    self.add(type="review-escalation", to=self.scientist_for(t), subject=r["id"],
                             ask="decide the open findings (settle, change the design, or cancel)",
                             cmd=f"rl review settle {r['id']}#F<n> ..", since=r.get("answered_at") or now_utc(),
                             frm="cli")
            elif r["status"] == "closed" and r.get("ruled"):
                newer = [x for x in self.tickets if x["kind"] == "review" and x.get("task_id") == r.get("task_id")
                         and x.get("review_kind") == r.get("review_kind") and id_order(x["id"]) > id_order(r["id"])]
                if not newer:
                    self.add(type="ruling", to=author, subject=r["id"],
                             ask="the ruling upheld findings: change the work and open a new round, or make a "
                                 "decision request", cmd=f"rl show {r['ruled'].get('by')}",
                             since=r["ruled"].get("at") or now_utc(), frm=r["ruled"].get("by"))

    def went_on(self, task_id: str, rv: str) -> bool:
        """An attempt of a non-test run of the task was registered after the round's last answer, by event seq
        (two in the same second keep their order)."""
        answered = [e["seq"] for e in self.tx.all("Event", entity_id=rv, type="review.answered")]
        if not answered:
            return False
        mine = [a["id"] for a in self.attempts if (self.runs.get(a["run_id"]) or {}).get("task_id") == task_id
                and (self.runs.get(a["run_id"]) or {}).get("purpose") not in TEST_PURPOSES]
        return any(e["seq"] > max(answered) for att in mine
                   for e in self.tx.all("Event", entity_id=att, type="run.register"))

    def round_author(self, r: dict) -> str:
        if r.get("review_kind") == "design":
            t = self.by_id.get(r.get("task_id"))
            if t and t.get("holder"):
                return t["holder"]
        return (r.get("passes") or [{}])[-1].get("requested_by") or r["requested_by"]

    def advice(self) -> None:
        for t in self.tickets:
            if t["kind"] != "advice":
                continue
            if t["status"] == "waiting":
                self.add(type="ruling-request" if t.get("advice_kind") == "ruling" else "consultation",
                         to=session_name("advisor", t["id"]), subject=t["id"],
                         ask="rule on each blocking finding" if t.get("advice_kind") == "ruling" else "advise",
                         cmd=f"rl advise answer {t['id']} --doc ..", since=t["created_at"], frm=t["requested_by"],
                         note=t.get("question"))
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
            report = next((a["id"] for a in self.asks if a["kind"] == "report" and a["task_id"] == t["id"]
                           and a["state"] == "open"), None)
            self.add(type=kind, to="coordinator", subject=t["id"], ask=f"receive {t['id']} or return it",
                     cmd=f"rl task accept {t['id']} | rl task return {t['id']} --reason ..",
                     since=t.get("delivered_at") or now_utc(), frm=t.get("holder"), note=(t.get("state_note")
                     or t.get("conclusion") or "") + (f" · the user reads its report ({report}); record what they "
                     f"say: rl request answer {report} --by user --text .." if report else ""))
        from .tasks import approval, latest_tree, phase_ended
        tree = latest_tree(self.tx)
        if tree and tree["state"] == "accepted":
            asks = [a for a in self.asks if a["task_id"] == tree["id"] and a["kind"] == "approval"]
            if approval(self.tx, tree):
                queued = [t for t in self.tasks if t["state"] == "queued" and t["kind"] in ("hypothesis", "prep")]
                if queued and not any(t.get("released_at") for t in self.tasks if t["kind"] == "hypothesis"):
                    self.add(type="tree-approved", to="coordinator", subject=tree["id"],
                             ask="release the first phase", cmd="rl task release T-x ...",
                             since=asks[-1]["answered_at"], frm="user", note=asks[-1]["answer"])
            elif not asks or asks[-1]["state"] == "answered":
                pass  # the coordinator's next step after receiving the tree is its own (schedule, ask the user)
        for a in self.asks:
            if a["state"] == "open" and a["kind"] == "decision":
                self.add(type="decision-request", to="coordinator", subject=a["id"], ask="decide, or forward to "
                         "the user", cmd=f"rl request answer {a['id']} --by coordinator .. | rl request forward "
                         f"{a['id']}", since=a["created_at"], frm=a["by"], note=a["stuck"])
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
        for h in self.tx.all("Hypothesis"):
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
                self.add(type="tree-approval", to="user", subject=a["task_id"], ask="approve or change the tree and "
                         "the first phase; decide the flagged hypotheses", cmd="tell the coordinator",
                         since=a["created_at"], frm="coordinator", note=a["stuck"])
            elif a["kind"] == "decision" and a["state"] == "forwarded":
                self.add(type="decision-request", to="user", subject=a["id"], ask="decide",
                         cmd="tell the coordinator", since=a.get("forwarded_at") or a["created_at"],
                         frm="coordinator", note=a["stuck"])
            elif a["kind"] == "report" and a["state"] == "open":
                self.add(type="phase-report", to="user", subject=a["id"], ask="read the phase report on the "
                         "research page; say in the coordinator's pane that you read it, or what to change",
                         cmd=f"tell the coordinator (it records your words: rl request answer {a['id']} --by user "
                         "--text ..)", since=a["created_at"], frm="cli", timed=False,
                         note=f"{a['stuck']} ({a['task_id']})")

    def asks_answered(self) -> None:
        """An answer stays with the scientist until it writes a decision citing the task after it (52); on a tree
        task, delivering the tree counts too."""
        for a in self.asks:
            if a["kind"] != "decision" or a["state"] != "answered":
                continue
            task = self.by_id.get(a["task_id"])
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
        from .usage_view import lines_crossed
        shares = limit(self.config, "envelope_lines")
        for t in self.tasks:
            if t["kind"] != "hypothesis" or t["state"] not in ("active", "delivered") or not t.get("envelope"):
                continue
            crossed = lines_crossed(self.tx, t, shares)
            if not crossed:
                continue
            top = max(s for s, _ in crossed)
            usage_times = [e["created_at"] for e in self.tx.all("Event", type="run.usage")]
            last_usage = max(usage_times) if usage_times else t["created_at"]
            later = [d for d in self.decisions if d.get("task_id") == t["id"] and d["created_at"] >= last_usage]
            env_changed = [e for e in self.tx.all("Event", entity_id=t["id"]) if e["type"] == "task.envelope"
                           and e["created_at"] >= last_usage]
            if not later and not env_changed and t["state"] == "active":
                self.add(type="usage-line", to=t["holder"], subject=t["id"], ask="adjust the plan, or make a "
                         "decision request", cmd="rl decide --kind route .. | rl request ..", since=last_usage,
                         frm="cli", note=f"past {top:.0%} of the envelope")
            if top >= shares[-1] and not env_changed:
                self.add(type="usage-line", to="coordinator", subject=t["id"], ask="look at the envelope",
                         cmd=f"rl task set {t['id']} --envelope ..", since=last_usage, frm="cli",
                         note=f"past {top:.0%} of the envelope")

    def dialogue(self) -> None:
        """Questions, review discussion and consultations: an entry waits for its addressee's reply (8.7)."""
        replied = {m["in_reply_to"] for m in self.messages if m.get("in_reply_to")}
        for m in self.messages:
            if m["id"] in replied:
                continue
            if m["kind"] == "answer":
                if m["to_role"] == "coordinator" and not m.get("closed_at"):
                    self.add(type="answer", to="coordinator", subject=m["subject"], ask="read the answer, then close "
                             "the question", cmd=f"rl question --close {m['in_reply_to']}", since=m["created_at"],
                             frm=m["from_role"], note=m["text"][:300])
                continue
            if m.get("closed_at"):
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


ESCALATION = ("the session itself", "who made the task", "the coordinator", "the user")


def escalations(items: list[Item], config: dict, now: datetime, by_id: dict) -> list[Item]:
    """Items past their deadline go up a fixed ladder (8.8): the holder is reminded first, then the one who made
    the task, then the coordinator, then the user. The coordinator's own overdue items go to the user. A phase
    report never escalates."""
    out = []
    for it in items:
        if not it.timed or it.type in ("phase-report", "timeout"):
            continue
        limit_m = deadline_minutes(config, it.type)
        waited = minutes_since(it.since, now)
        if waited < limit_m:
            continue
        steps = int(waited // limit_m)  # 1: remind the holder (the watcher does), 2: creator, 3: coordinator, 4: user
        ladder = []
        task = by_id.get(it.subject)
        if it.to == "coordinator":
            ladder = ["user"]
        elif it.to != "user":
            creator = task.get("creator") if task else None
            ladder = list(dict.fromkeys(x for x in (creator, "coordinator", "user") if x and x != it.to))
        for level, who in enumerate(ladder[:max(0, steps - 1)], start=1):
            out.append(Item(type="timeout", to=who, subject=it.subject, ask=f"{it.type} for {it.to} waits "
                            f"{waited / 60:.1f} h: handle it, or restart the session on it",
                            cmd=f"rl inbox --for {it.to}", since=it.since, frm="watch", level=level,
                            note=f"{it.type}: {it.ask}"))
    return out


def all_items(tx: Tx, config: dict) -> list[Item]:
    return Rules(tx, config).run()


def for_recipient(tx: Tx, config: dict, who: str) -> list[Item]:
    return [i for i in all_items(tx, config) if i.to == who]


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
        lines.append(f"  from  {it.frm or '-'} · {age(minutes_since(it.since))}" + ("" if it.timed else " (untimed)"))
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
    for r in rules.tickets:
        if r["status"] in ("waiting", "answered", "escalated") and r["id"] not in subjects:
            if r["status"] == "escalated" and any(x.get("ruling_of") == r["id"] and x["status"] == "waiting"
                                                  for x in rules.tickets):
                continue
            if r["status"] == "waiting":
                continue  # a running test job on it
            out.append(f"{r['id']} {r['status']} waits on nobody")
    for a in rules.asks:
        if a["state"] in ("open", "forwarded") and a["id"] not in subjects and a["task_id"] not in subjects:
            out.append(f"{a['id']} {a['state']} waits on nobody")
    return out
