"""Review rounds of the 2026-11-27 design (chapter 6): one author, one target, one reviewer session, at most two
reviews; the waiting review can move to a newer target; the reviewer names what it reviewed (--at); the author can
reject a finding with a reason; who does a step is flagged, never refused; nothing of an ended round is on a list."""

from __future__ import annotations

import json

import pytest
from conftest import git

from smooth_research import gitops, inbox, reviews
from smooth_research.store import Store

S = "scientist-T-0001"
R = "researcher-T-0002"
SIM = "def error(x):\n    return abs(x)\n\n\ndef helper():\n    return 1\n"


def ok(result) -> str:
    code, out, err = result
    assert code == 0, f"exit {code}: {err or out}"
    return out


def res(result) -> dict:
    return json.loads(ok(result))


def refused(result, code: int = 1) -> str:
    got, out, err = result
    assert got == code, f"expected exit {code}, got {got}: {out}"
    return err


def setup(store: Store, kind: str = "execute") -> str:
    """H-0001; the hypothesis task T-0001 its scientist holds; under it T-0002 (execute, carrying E-0001 with a
    protected unit; or prep) that the scientist holds, with a worktree and a first commit. Returns the commit."""
    with store.write() as tx:
        h = tx.next_id("Hypothesis")
        tx.create("Hypothesis", {"id": h, "title": "Averaging helps", "statement": "mean of 3 beats 1", "purpose": "p",
                                 "evidence_status": "untested", "disposition": "active", "revision": 1,
                                 "label": "H1"})
        t1 = tx.next_id("Task")
        tx.create("Task", {"id": t1, "title": "Test H1", "kind": "hypothesis", "state": "active", "revision": 1,
                           "creator": "coordinator", "holder": S})
        tx.link("targets", t1, h)
        t2 = tx.next_id("Task")
        tx.create("Task", {"id": t2, "title": "Sim", "kind": kind, "state": "active", "revision": 1, "creator": S,
                           "holder": S, "requirement": "a sim", "done_check": "true"})
        tx.link("part_of", t2, t1)
        if kind == "execute":
            e = tx.next_id("Experiment")
            tx.create("Experiment", {"id": e, "title": "Sim", "question": "is mean3 better?", "label": "EH1/1",
                                     "revision": 1, "protected": ["src/sim.py::error"]})
            tx.link("targets", t2, e)
    branch, _ = gitops.make_worktree(store.project, "T-0002")
    with store.write() as tx:
        tx.update("T-0002", {"branch": branch, "worktree": str(gitops.checkout_in(store.project, "T-0002"))})
    return commit(store, "src/sim.py", SIM)


def commit(store: Store, path: str, text: str) -> str:
    wt = gitops.checkout_in(store.project, "T-0002")
    f = wt / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "change")
    return git(wt, "rev-parse", "HEAD")


def draft(store: Store) -> str:
    with store.write() as tx:
        d = tx.next_id("Decision")
        tx.create("Decision", {"id": d, "kind": "verdict", "state": "draft", "rationale": "lower", "task_id": "T-0001",
                               "author": S})
        tx.link("decides", d, "H-0001", revision=1)
    return d


def ticket(store: Store, rv: str) -> dict:
    with store.read() as tx:
        return tx.require(rv)


def items(store: Store, who: str) -> list[inbox.Item]:
    with store.read() as tx:
        return inbox.for_recipient(tx, store.project.config, who)


def codes(rec: dict) -> list[str]:
    return [f["code"] for f in rec.get("flags") or []]


FAIL_METHOD = json.dumps([{"category": "method", "severity": "high", "text": "the error is not the metric"}])


# -- one round ------------------------------------------------------------------------------------------------


def test_a_first_review_that_passes_ends_the_round(project, rl):
    c1 = setup(project)
    out = res(rl("--json", "review", "request", "T-0002", who=S))
    assert out["id"] == "RV-0001" and out["wake"] == ["reviewer-RV-0001"]
    t = ticket(project, "RV-0001")
    assert t["status"] == "waiting" and t["author"] == S and t["review_kind"] == "design"
    assert t["passes"][0]["commit"] == c1 and t["passes"][0]["design"].startswith("E-0001@1#")
    req = [i for i in items(project, "reviewer-RV-0001") if i.type == "review-request"]
    assert req and c1[:12] in req[0].cmd and "--at" in req[0].cmd
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "pass", "--at", c1[:7],
                 who="reviewer-RV-0001"))
    assert out["status"] == "passed" and out["wake"] == [S]
    assert items(project, "reviewer-RV-0001") == []
    # the ended round is on no list; its next step is on the task it reviewed
    mine = [i for i in items(project, S) if i.type == "review-verdict"]
    assert [(i.subject, i.cmd.split(" --")[0]) for i in mine] == [("T-0002", "rl task dispatch T-0002")]
    with project.read() as tx:
        assert reviews.review_flags(tx, "T-0002", c1, for_evidence=False) == []
        assert reviews.covers(tx, "T-0002", c1, for_evidence=True)[0]


def test_the_reviewer_answers_only_the_current_target(project, rl):
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    err = refused(rl("review", "answer", "RV-0001", "--verdict", "pass", who="reviewer-RV-0001"))
    assert "--at" in err
    c2 = commit(project, "src/notes.txt", "more\n")
    err = refused(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", c2[:12], who="reviewer-RV-0001"))
    assert c1[:12] in err  # the round still waits on c1: c2 is not what it was asked to review
    assert ticket(project, "RV-0001")["status"] == "waiting"
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", c1, who="reviewer-RV-0001"))


def test_a_waiting_review_moves_to_the_newer_commit(project, rl):
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    before = [i.key() for i in items(project, "reviewer-RV-0001")]
    c2 = commit(project, "src/sim.py", SIM + "\n# tidy\n")
    out = res(rl("--json", "review", "request", "T-0002", "--recheck-of", "RV-0001", who=S))
    assert out["id"] == "RV-0001" and out["wake"] == ["reviewer-RV-0001"] and "moved" in out["summary"]
    t = ticket(project, "RV-0001")
    assert len(t["passes"]) == 1 and t["passes"][0]["commit"] == c2  # not another review
    assert t["passes"][0]["retargeted"][0]["commit"] == c1
    now = items(project, "reviewer-RV-0001")
    assert [i.key() for i in now] != before  # news to the reviewer
    assert f"git diff {c1[:12]} {c2[:12]}" in now[0].note
    err = refused(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", c1[:12], who="reviewer-RV-0001"))
    assert f"git diff {c1[:12]} {c2[:12]}" in err
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "pass", "--at", c2[:12],
                 who="reviewer-RV-0001"))
    assert out["status"] == "passed"


def test_the_second_review_ends_the_round_whatever_remains(project, rl):
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "fail", "--at", c1, "--findings", FAIL_METHOD,
                 who="reviewer-RV-0001"))
    assert out["status"] == "answered"
    verdict = [i for i in items(project, S) if i.type == "review-verdict"]
    assert verdict[0].subject == "RV-0001" and "--recheck-of RV-0001" in verdict[0].cmd
    with project.read() as tx:
        assert [c for c, _ in reviews.review_flags(tx, "T-0002", c1, for_evidence=False)] == ["unreviewed",
                                                                                               "open-findings"]
    c2 = commit(project, "src/sim.py", SIM + "\n# fixed half\n")
    out = res(rl("--json", "review", "request", "T-0002", "--recheck-of", "RV-0001", who=S))
    assert "review 2 of 2" in out["summary"]
    assert ticket(project, "RV-0001")["passes"][1]["commit"] == c2
    assert not [i for i in items(project, S) if i.type == "review-verdict"]  # it waits on the reviewer now
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "fail", "--at", c2, who="reviewer-RV-0001"))
    assert out["status"] == "closed" and [f["code"] for f in out["flags"]] == ["open-findings"]
    t = ticket(project, "RV-0001")
    assert t["findings"][0]["status"] == "open" and "open-findings" in codes(t)
    # nothing on the ended round; the author is told on the task, and goes on
    mine = [i for i in items(project, S) if i.type == "review-verdict"]
    assert [i.subject for i in mine] == ["T-0002"] and "ended" in mine[0].ask
    assert not [i for i in items(project, "reviewer-RV-0001")]
    # more review is a new round: a recheck of the ended one opens it, flagged
    out = res(rl("--json", "review", "request", "T-0002", "--recheck-of", "RV-0001", who=S))
    assert out["id"] == "RV-0002" and out["wake"] == ["reviewer-RV-0002"]
    assert [f["code"] for f in out["flags"]] == ["round-ended"] and out["warnings"]
    assert "round-ended" in codes(ticket(project, "RV-0002"))


def test_a_second_round_on_the_same_work_is_opened_with_a_warning(project, rl):
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    ok(rl("review", "answer", "RV-0001", "--verdict", "fail", "--at", c1, "--findings", FAIL_METHOD,
          who="reviewer-RV-0001"))
    out = res(rl("--json", "review", "request", "T-0002", who=S))
    assert out["id"] == "RV-0002" and "--recheck-of RV-0001" in out["warnings"][0]
    # the newer round says what comes next; the older one rests
    assert not [i for i in items(project, S) if i.subject == "RV-0001"]
    with project.read() as tx:
        assert not [x for x in inbox.unwatched(tx, project.project.config) if x.startswith("RV-")]


# -- rejecting a finding --------------------------------------------------------------------------------------


def two_findings(project, rl) -> str:
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    ok(rl("review", "answer", "RV-0001", "--verdict", "fail", "--at", c1, "--findings", json.dumps(
        [{"category": "method", "severity": "high", "text": "wrong metric"},
         {"category": "engineering", "severity": "low", "text": "name it better"}]), who="reviewer-RV-0001"))
    return c1


def test_the_author_rejects_a_finding_and_the_reviewer_keeps_it(project, rl):
    two_findings(project, rl)
    refused(rl("review", "reject", "RV-0001#F1", "--reason", " ", who=S))
    out = res(rl("--json", "review", "reject", "RV-0001#F1", "--reason", "the metric is the plan's", who=S))
    assert out["flags"] == [] and "--recheck-of RV-0001" in out["next"][0]
    f1 = ticket(project, "RV-0001")["findings"][0]
    assert f1["status"] == "rejected" and f1["rejected"]["reason"] == "the metric is the plan's"
    refused(rl("review", "reject", "RV-0001#F1", "--reason", "again", who=S), code=3)  # a recorded answer stays
    c2 = commit(project, "src/sim.py", SIM.replace("helper", "one"))
    ok(rl("review", "request", "T-0002", "--recheck-of", "RV-0001", who=S))
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "pass", "--at", c2, "--close",
                 json.dumps([{"n": 2, "status": "fixed"}]), who="reviewer-RV-0001"))
    t = ticket(project, "RV-0001")
    assert [f["status"] for f in t["findings"]] == ["disputed", "fixed"]
    assert out["status"] == "passed" and "disputed" in out["warnings"][0]
    assert "open-findings" in codes(t)
    with project.read() as tx:
        flags = reviews.review_flags(tx, "T-0002", c2, for_evidence=False)
    assert [c for c, _ in flags] == ["open-findings"] and "disputed by the author" in flags[0][1]


def test_the_reviewer_accepts_a_rejection_by_withdrawing(project, rl):
    two_findings(project, rl)
    ok(rl("review", "reject", "RV-0001#F1", "--reason", "the metric is the plan's", who=S))
    c2 = commit(project, "src/sim.py", SIM.replace("helper", "one"))
    ok(rl("review", "request", "T-0002", "--recheck-of", "RV-0001", who=S))
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "pass", "--at", c2, "--close",
                 json.dumps([{"n": 1, "status": "withdrawn"}, {"n": 2}]), who="reviewer-RV-0001"))
    assert out["status"] == "passed" and out["flags"] == []
    assert [f["status"] for f in ticket(project, "RV-0001")["findings"]] == ["withdrawn", "fixed"]


# -- who does it: flagged, not refused ------------------------------------------------------------------------


def test_steps_by_other_sessions_are_flagged(project, rl):
    c1 = setup(project)
    out = res(rl("--json", "review", "request", "T-0002", who=R))  # the scientist holds it
    assert [f["code"] for f in out["flags"]] == ["not-holder"]
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "fail", "--at", c1, "--findings",
                 FAIL_METHOD, who="coordinator"))
    assert sorted(f["code"] for f in out["flags"]) == ["not-holder", "role"]
    assert out["wake"] == [R]  # the round's author
    out = res(rl("--json", "review", "reject", "RV-0001#F1", "--reason", "no", who="researcher-T-0009"))
    assert [f["code"] for f in out["flags"]] == ["not-holder"]
    assert sorted(codes(ticket(project, "RV-0001"))) == ["not-holder", "not-holder", "not-holder", "role"]
    # without any session nothing could say who recorded it
    refused(rl("review", "request", "T-0002"))


def test_an_engineering_finding_in_the_protected_scope_is_flagged(project, rl):
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    out = res(rl("--json", "review", "answer", "RV-0001", "--verdict", "fail", "--at", c1, "--findings", json.dumps(
        [{"category": "engineering", "severity": "low", "text": "slow", "loc": "src/sim.py::error"}]),
        who="reviewer-RV-0001"))
    assert [f["code"] for f in out["flags"]] == ["category"]
    assert ticket(project, "RV-0001")["findings"][0]["category"] == "engineering"  # as the reviewer said


# -- what cannot be recorded ----------------------------------------------------------------------------------


def test_requests_that_cannot_be_recorded(project, rl):
    setup(project)
    assert "tree reviews are gone" in refused(rl("review", "request", "T-0002", "--kind", "tree", who=S))
    assert "execute or preparation" in refused(rl("review", "request", "T-0001", who=S))
    assert "omit --kind" in refused(rl("review", "request", "T-0002", "--kind", "conclusion", who=S))
    refused(rl("review", "request", "T-0099", who=S))
    with pytest.raises(SystemExit):  # settle is gone
        rl("review", "settle", "RV-0001#F1", "--status", "withdrawn", who=S)


def test_a_prep_task_is_reviewed_without_an_experiment(project, rl):
    c1 = setup(project, kind="prep")
    ok(rl("review", "request", "T-0002", who=S))
    assert "design" not in ticket(project, "RV-0001")["passes"][0]
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", c1[:10], who="reviewer-RV-0001"))
    with project.read() as tx:
        assert reviews.covers(tx, "T-0002", c1, for_evidence=False) == (True, f"RV-0001 covers {c1[:12]}")


# -- flags for going on ---------------------------------------------------------------------------------------


def test_review_flags_note_whether_the_protected_scope_changed(project, rl):
    c1 = setup(project)
    with project.read() as tx:
        (code, text), = reviews.review_flags(tx, "T-0002", c1, False, project.project)
    assert code == "unreviewed" and "no review of T-0002 yet" in text and "no review of E-0001's code" in text
    ok(rl("review", "request", "T-0002", who=S))
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", c1, who="reviewer-RV-0001"))
    c2 = commit(project, "src/sim.py", SIM.replace("return 1", "return 2"))  # outside the protected unit
    with project.read() as tx:
        (code, text), = reviews.review_flags(tx, "T-0002", c2, False, project.project)
        ok_, why = reviews.covers(tx, "T-0002", c2, False, project.project)
    assert code == "unreviewed" and f"unchanged since RV-0001 passed at {c1[:12]}" in text
    assert not ok_ and why == text
    c3 = commit(project, "src/sim.py", SIM.replace("abs(x)", "x * x"))
    with project.read() as tx:
        (code, text), = reviews.review_flags(tx, "T-0002", c3, True, project.project)
        assert reviews.open_blockers(tx, "T-0002", True) is None
        assert reviews.last_passed_design(tx, ["T-0002"])[1] == c1
    assert "changed since RV-0001 passed" in text and "src/sim.py::error" in text


# -- conclusion review ----------------------------------------------------------------------------------------


def test_a_conclusion_round_follows_the_revised_draft(project, rl):
    setup(project)
    d1 = draft(project)
    out = res(rl("--json", "review", "request", d1, who=S))
    assert ticket(project, out["id"])["review_kind"] == "conclusion"
    rv = out["id"]
    ok(rl("review", "answer", rv, "--verdict", "fail", "--at", d1, "--findings", FAIL_METHOD, who=f"reviewer-{rv}"))
    assert [i.subject for i in items(project, S) if i.type == "review-verdict"] == [rv]
    d2 = draft(project)  # the revised conclusion is a new draft
    ok(rl("review", "request", d2, "--recheck-of", rv, who=S))
    t = ticket(project, rv)
    assert [p["target"] for p in t["passes"]] == [d1, d2] and t["target_id"] == d2
    assert d2 in refused(rl("review", "answer", rv, "--verdict", "pass", "--at", d1, who=f"reviewer-{rv}"))
    ok(rl("review", "answer", rv, "--verdict", "pass", "--at", d2, "--close", json.dumps([{"n": 1}]),
          who=f"reviewer-{rv}"))
    with project.read() as tx:
        assert reviews.conclusion_passed(tx, d2)["id"] == rv
        assert reviews.conclusion_passed(tx, d1) is None
        assert reviews.conclusion_flags(tx, d2) == []
        assert [c for c, _ in reviews.conclusion_flags(tx, d1)] == ["unreviewed"]
    mine = [i for i in items(project, S) if i.type == "review-verdict"]
    assert [(i.subject, i.cmd) for i in mine] == [(d2, f"rl decide --final {d2}")]


def test_a_waiting_conclusion_review_moves_to_the_new_draft(project, rl):
    setup(project)
    d1 = draft(project)
    rv = res(rl("--json", "review", "request", d1, "--question", "is the scope right?", who=S))["id"]
    d2 = draft(project)
    out = res(rl("--json", "review", "request", d2, "--recheck-of", rv, who=S))
    assert out["id"] == rv and "moved" in out["summary"]
    assert [p["target"] for p in ticket(project, rv)["passes"]] == [d2]


# -- discussion -----------------------------------------------------------------------------------------------


def test_discussion_after_the_round_ended_is_recorded_on_no_list(project, rl):
    c1 = setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    out = res(rl("--json", "discuss", "RV-0001", "--text", "is the metric fixed by the plan?", who=S))
    assert out["wake"] == ["reviewer-RV-0001"]
    assert "review-discussion" in [i.type for i in items(project, "reviewer-RV-0001")]
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", c1, who="reviewer-RV-0001"))
    assert "review-discussion" not in [i.type for i in items(project, "reviewer-RV-0001")]
    out = res(rl("--json", "reply", "MSG-0001", "--text", "yes", who="reviewer-RV-0001"))
    assert out["wake"] == [] and out["warnings"]
    out = res(rl("--json", "discuss", "RV-0001", "--text", "thanks", who=S))
    assert out["wake"] == [] and out["warnings"]
    assert not [i for i in items(project, S) if i.type == "review-discussion"]
    with project.read() as tx:
        assert len(tx.all("Message")) == 3


def test_a_discussion_by_another_session_is_flagged(project, rl):
    setup(project)
    ok(rl("review", "request", "T-0002", who=S))
    out = res(rl("--json", "discuss", "RV-0001", "--text", "a note", who="coordinator"))
    assert [f["code"] for f in out["flags"]] == ["not-holder"] and out["wake"] == ["reviewer-RV-0001"]


def test_questions_go_back_to_whoever_asked(project, rl):
    setup(project)
    out = res(rl("--json", "question", "--to", S, "--on", "T-0001", "--text", "how far?", who="coordinator"))
    assert out["wake"] == [S]
    refused(rl("question", "--to", "user", "--on", "T-0001", "--text", "?", who="coordinator"))
    out = res(rl("--json", "reply", "MSG-0001", "--text", "half way", who=S))
    assert out["wake"] == ["coordinator"]
    assert [i.type for i in items(project, "coordinator") if i.subject == "T-0001"] == ["answer"]
    ok(rl("question", "--close", "MSG-0001", who="coordinator"))
    assert not [i for i in items(project, "coordinator") if i.subject == "T-0001"]


# -- advice ---------------------------------------------------------------------------------------------------


def test_advice_is_asked_for_and_answered(project, rl, tmp_path):
    setup(project)
    out = res(rl("--json", "advise", "ask", "--question", "which metric?", who=S))
    assert out["wake"] == ["advisor-AD-0001"]
    assert [i.type for i in items(project, "advisor-AD-0001")] == ["consultation"]
    doc = project.project.root / "advice.md"
    doc.write_text("use the plan's metric\n", encoding="utf-8")
    out = res(rl("--json", "advise", "answer", "AD-0001", "--doc", "advice.md", who="advisor-AD-0001"))
    assert out["wake"] == [S]
    refused(rl("advise", "answer", "AD-0001", "--doc", "advice.md", who="advisor-AD-0001"), code=3)
