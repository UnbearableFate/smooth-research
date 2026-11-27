"""The to-do items: who is waited on at each step, the dialogue and rl doctor's invariant."""

import json

import flow
import pytest

from smooth_research import inbox


def unwatched(store) -> list[str]:
    with store.read() as tx:
        return inbox.unwatched(tx, store.project.config)


@pytest.mark.parametrize("step", ["tree", "approved", "designed", "dispatched", "delivered", "concluded"])
def test_nothing_waits_on_nobody_at_any_step(project, rl, step):
    getattr(flow, step)(project, rl)
    assert unwatched(project) == []


def test_a_scientist_waiting_on_its_researcher_has_nothing_to_do(project, rl):
    flow.dispatched(project, rl)
    assert flow.types(project, "scientist-T-0002") == []
    assert flow.types(project, "researcher-T-0003") == ["task-card"]


def test_a_delivered_result_goes_to_the_scientist_who_made_the_task(project, rl):
    flow.delivered(project, rl)
    mine = {i.subject: i.type for i in flow.items(project, "scientist-T-0002")}
    assert mine == {"T-0003": "task-conclusion", "T-0002": "hypothesis-task"}  # its own task no longer waits
    assert flow.types(project, "researcher-T-0003") == []


def flag_codes(store, rid) -> list[str]:
    with store.read() as tx:
        return [f["code"] for f in tx.require(rid).get("flags") or []]


def test_a_question_is_answered_and_closed_and_others_doing_it_are_flagged(project, rl):
    flow.dispatched(project, rl)
    c, s = "coordinator", "scientist-T-0002"
    out = flow.ok(rl("question", "--to", s, "--on", "T-0003", "--text", "why two arms?", who=c))
    mid = out.split()[0] if out.startswith("MSG-") else "MSG-0001"
    assert flow.types(project, s) == ["question"]
    out = flow.ok(rl("question", "--to", c, "--on", "T-0003", "--text", "and you?", who=s))
    assert "flag: [role]" in out and "role" in flag_codes(project, "T-0003")
    flow.ok(rl("reply", "MSG-0002", "--text", "later", who=c))
    out = flow.ok(rl("reply", mid, "--text", "not mine", who="researcher-T-0003"))
    assert "flag: [not-holder]" in out
    flow.ok(rl("reply", mid, "--text", "a baseline and the change", who=s))
    assert flow.types(project, s) == ["answer"]  # the coordinator's answer to its own question
    flow.ok(rl("question", "--close", "MSG-0002", who=s))
    assert flow.types(project, s) == []
    assert ("answer", "a baseline and the change") in [(i.type, i.note) for i in flow.items(project, c)]
    out = flow.ok(rl("reply", mid, "--text", "again", who=s))
    assert "has a reply already" in out  # recorded after it, said
    flow.ok(rl("question", "--close", mid, who=c))
    assert flow.types(project, c) == []
    assert unwatched(project) == []


def test_review_discussion_waits_on_the_other_side(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    flow.ok(rl("review", "request", "T-0003", who=s))
    reviewer = "reviewer-RV-0001"
    flow.ok(rl("discuss", "RV-0001", "--text", "is the seed fixed?", who=reviewer))
    assert "review-discussion" in flow.types(project, s)
    out = flow.ok(rl("discuss", "RV-0001", "--text", "me too", who="researcher-T-0003"))
    assert "flag: [not-holder]" in out and "between" in out


def test_a_recheck_on_the_same_ticket_is_news_to_the_reviewer_and_its_verdict_to_the_author(project, rl):
    """Each item is told once by its key; a review item's key carries its review."""
    flow.designed(project, rl)
    s, r = "scientist-T-0002", "reviewer-RV-0001"
    flow.ok(rl("review", "request", "T-0003", who=s))
    [first] = flow.items(project, r)
    at = flow.head(project, "T-0003")[:12]
    flow.ok(rl("review", "answer", "RV-0001", "--verdict", "fail", "--at", at, "--findings", json.dumps(
        [{"category": "method", "severity": "medium", "text": "seed count", "loc": None}]), who=r))
    [failed] = [i for i in flow.items(project, s) if i.type == "review-verdict"]
    flow.ok(rl("review", "request", "T-0003", "--recheck-of", "RV-0001", who=s))
    [again] = flow.items(project, r)
    assert again.key() != first.key()
    flow.ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", at, "--close",
               json.dumps([{"n": 1, "status": "fixed"}]), who=r))
    [passed] = [i for i in flow.items(project, s) if i.type == "review-verdict"]
    assert passed.key() != failed.key()


@pytest.mark.parametrize("first", [1, 9999])  # 9999: the next round is RV-10000, after it by number, not as text
def test_only_the_latest_round_counts_for_a_dispatch(project, rl, first):
    """A design whose newer round failed is not cleared by an older pass: its dispatch is flagged, not refused."""
    with project.write() as tx:
        tx.txn.set_property(tx._meta_node(), "seq_RV", first - 1)
    flow.designed(project, rl)
    s = "scientist-T-0002"
    one, two = (f"RV-{n:04d}" for n in (first, first + 1))
    flow.ok(rl("review", "request", "T-0003", who=s))
    flow.ok(rl("review", "answer", one, "--verdict", "pass", "--at", flow.head(project, "T-0003")[:12],
               who=f"reviewer-{one}"))
    flow.ok(rl("review", "request", "T-0003", who=s))  # a new round on the same commit
    flow.ok(rl("review", "answer", two, "--verdict", "fail", "--at", flow.head(project, "T-0003")[:12],
               "--findings", json.dumps([{"category": "method", "severity": "medium", "text": "scope too wide",
                                          "loc": None}]), who=f"reviewer-{two}"))
    assert not any(i.type == "review-verdict" and i.subject == one for i in flow.items(project, s))
    out = flow.ok(rl("task", "dispatch", "T-0003", who=s))
    assert "flag: [" in out and two in out


def test_the_researchers_go_on_item_clears_once_it_runs(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    flow.ok(rl("review", "request", "T-0003", who=r))
    flow.ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--at", flow.head(project, "T-0003")[:12],
               who="reviewer-RV-0002"))
    assert "review-verdict" in flow.types(project, r)
    flow.run_arm(project, rl, "single")
    assert "review-verdict" not in flow.types(project, r)


def test_a_design_key_from_before_edit_seq_stays_the_same():
    from smooth_research.research import KEY_FIELDS, design_key
    from smooth_research.util import digest_json
    exp = {"id": "E-0001", "revision": 1, "question": "q", "manifest_path": "a.yaml", "semantic_tests": "t",
           "protected": ["x"], "brief_ref": None}
    old = {k: exp.get(k) for k in KEY_FIELDS if k != "edit_seq"}
    assert design_key(exp) == f"E-0001@1#{digest_json(old)}"
    assert design_key({**exp, "edit_seq": 1}) != design_key(exp)


def test_the_go_on_item_follows_the_latest_round_and_the_event_order(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    flow.run_arm(project, rl, "single")  # registered before the pass (in the same second, possibly)
    flow.ok(rl("review", "request", "T-0003", who=r))
    flow.ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--at", flow.head(project, "T-0003")[:12],
               who="reviewer-RV-0002"))
    assert "review-verdict" in flow.types(project, r)  # a run before the pass does not clear it
    flow.ok(rl("review", "request", "T-0003", who=r))  # a newer round that fails says fix, not go on
    flow.ok(rl("review", "answer", "RV-0003", "--verdict", "fail", "--at", flow.head(project, "T-0003")[:12],
               "--findings", json.dumps([{"category": "method", "severity": "high", "text": "wrong baseline",
                                          "loc": None}]), who="reviewer-RV-0003"))
    verdicts = [i for i in flow.items(project, r) if i.type == "review-verdict"]
    assert [i.subject for i in verdicts] == ["RV-0003"] and "fix" in verdicts[0].ask


def test_a_task_delivered_again_after_a_return_is_news_to_the_coordinator(project, rl):
    flow.concluded(project, rl)
    [first] = flow.items(project, "coordinator")
    flow.ok(rl("task", "return", "T-0002", "--reason", "say what the tails do", who="coordinator"))
    flow.ok(rl("task", "deliver", "T-0002", "--conclusion", "averaging helps, revised", who="scientist-T-0002"))
    [again] = flow.items(project, "coordinator")
    assert again.type == first.type == "hypothesis-conclusion" and again.key() != first.key()


def test_a_settlement_after_a_trial_still_says_go_on(project, rl):
    """The round passed when the scientist settled its last finding; a trial registered before that is not the
    researcher going on."""
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    flow.ok(rl("review", "request", "T-0003", who=r))
    flow.ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--at", flow.head(project, "T-0003")[:12],
               who="reviewer-RV-0002"))
    flow.ok(rl("run", "register", "--task", "T-0003", "--request-id", "smoke", "--trial", "--minutes", "1", who=r))
    assert "review-verdict" not in flow.types(project, r)
    with project.write() as tx:  # the settlement that made the round pass came after the trial
        tx.event("review.settled", "RV-0002", "F1 withdrawn by scientist-T-0002")
    assert "review-verdict" in flow.types(project, r)


def test_a_task_returned_and_delivered_again_is_news_to_both_sides(project, rl):
    flow.delivered(project, rl)
    s, r = "scientist-T-0002", "researcher-T-0003"
    first = next(i for i in flow.items(project, s) if i.type == "task-conclusion")
    flow.ok(rl("task", "return", "T-0003", "--reason", "add a seed", who=s))
    returned = next(i for i in flow.items(project, r) if i.type == "returned")
    flow.ok(rl("task", "deliver", "T-0003", "--conclusion", "mean3 lowers the error (EV-0001), revised",
               "--limitations", "one seed", "--outputs", "research/work/T-0003/mean3-0.json", who=r))
    again = next(i for i in flow.items(project, s) if i.type == "task-conclusion")
    assert again.key() != first.key()
    flow.ok(rl("task", "return", "T-0003", "--reason", "still one seed", who=s))
    assert next(i for i in flow.items(project, r) if i.type == "returned").key() != returned.key()
