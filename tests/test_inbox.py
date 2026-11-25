"""The to-do items: who is waited on at each step, the escalation ladder, the dialogue and rl doctor's invariant."""

import json
from datetime import datetime, timedelta, timezone

import flow
import pytest

from smooth_research import inbox
from smooth_research.inbox import Item


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


def at(minutes_ago: float, now: datetime) -> str:
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def test_an_overdue_item_climbs_the_ladder_one_deadline_at_a_time():
    now = datetime.now(timezone.utc)
    config = {"agents": {"deadlines": {"default": 60, "question": 10}}}
    task = {"id": "T-0003", "creator": "scientist-T-0002"}

    def ladder(minutes: float, type_: str = "task", to: str = "researcher-T-0003") -> list[tuple[str, int]]:
        item = Item(type=type_, to=to, subject="T-0003", ask="run it", cmd="", since=at(minutes, now))
        return [(x.to, x.level) for x in inbox.escalations([item], config, now, {"T-0003": task})]

    assert ladder(59) == []
    assert ladder(61) == []  # the first deadline: the watcher reminds the holder
    assert ladder(121) == [("scientist-T-0002", 1)]
    assert ladder(181) == [("scientist-T-0002", 1), ("coordinator", 2)]
    assert ladder(600) == [("scientist-T-0002", 1), ("coordinator", 2), ("user", 3)]
    assert ladder(21, "question") == [("scientist-T-0002", 1)]  # its own deadline
    assert ladder(600, to="coordinator") == [("user", 1)]
    untimed = Item(type="task", to="researcher-T-0003", subject="T-0003", ask="", cmd="", since=at(600, now), timed=False)
    report = Item(type="phase-report", to="coordinator", subject="PH-0001", ask="", cmd="", since=at(600, now))
    assert inbox.escalations([untimed, report], config, now, {}) == []


def test_a_question_is_answered_once_and_closed_by_the_coordinator(project, rl):
    flow.dispatched(project, rl)
    c, s = "coordinator", "scientist-T-0002"
    out = flow.ok(rl("question", "--to", s, "--on", "T-0003", "--text", "why two arms?", who=c))
    mid = out.split()[0] if out.startswith("MSG-") else "MSG-0001"
    assert flow.types(project, s) == ["question"]
    code, _, err = rl("question", "--to", c, "--on", "T-0003", "--text", "and you?", who=s)
    assert code != 0 and "only the coordinator" in err
    code, _, err = rl("reply", mid, "--text", "not mine", who="researcher-T-0003")
    assert code != 0 and "addressed to" in err
    flow.ok(rl("reply", mid, "--text", "a baseline and the change", who=s))
    assert flow.types(project, s) == []
    assert [(i.type, i.note) for i in flow.items(project, c)] == [("answer", "a baseline and the change")]
    code, _, err = rl("reply", mid, "--text", "again", who=s)
    assert code != 0 and "already has a reply" in err
    flow.ok(rl("question", "--close", mid, who=c))
    assert flow.types(project, c) == []
    assert unwatched(project) == []


def test_review_discussion_waits_on_the_other_side(project, rl):
    flow.designed(project, rl)
    s = "scientist-T-0002"
    flow.ok(rl("review", "request", "T-0003", who=s))
    reviewer = "reviewer-RV-0002"
    flow.ok(rl("discuss", "RV-0002", "--text", "is the seed fixed?", who=reviewer))
    assert "review-discussion" in flow.types(project, s)
    code, _, err = rl("discuss", "RV-0002", "--text", "me too", who="researcher-T-0003")
    assert code != 0 and "between" in err


def test_a_recheck_on_the_same_ticket_is_news_to_the_reviewer_and_its_verdict_to_the_author(project, rl):
    """The watcher tells each item once by its key; a review item's key carries its pass."""
    flow.designed(project, rl)
    s, r = "scientist-T-0002", "reviewer-RV-0002"
    flow.ok(rl("review", "request", "T-0003", who=s))
    [first] = flow.items(project, r)
    flow.ok(rl("review", "answer", "RV-0002", "--verdict", "fail", "--findings", json.dumps(
        [{"category": "method", "severity": "medium", "text": "seed count", "loc": None}]), who=r))
    [failed] = [i for i in flow.items(project, s) if i.type == "review-verdict"]
    flow.ok(rl("review", "request", "T-0003", "--recheck-of", "RV-0002", who=s))
    [again] = flow.items(project, r)
    assert again.key() != first.key()
    flow.ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--close",
               json.dumps([{"n": 1, "status": "fixed"}]), who=r))
    [passed] = [i for i in flow.items(project, s) if i.type == "review-verdict"]
    assert passed.key() != failed.key()


@pytest.mark.parametrize("first", [1, 9999])  # 9999: the next round is RV-10000, after it by number, not as text
def test_only_the_latest_tree_round_clears_the_delivery(project, rl, first):
    """A tree whose newer round failed is not delivered on an older pass, nor told to be."""
    with project.write() as tx:
        tx.txn.set_property(tx._meta_node(), "seq_RV", first - 1)
    flow.ok(rl("task", "create", "--kind", "tree", "--title", "Tree", who="coordinator"))
    flow.ok(rl("task", "release", "T-0001", who="coordinator"))
    s = "scientist-T-0001"
    flow.ok(rl("task", "claim", "T-0001", who=s))
    flow.ok(rl("hypothesis", "add", "--title", "Averaging helps", "--statement", "mean of 3 beats 1", "--purpose",
               "whether averaging is worth it", "--scope", "N(5,1)", "--source", "Averaging", who=s))
    flow.ok(rl("decide", "--kind", "route", "--targets", "H-0001", "--rationale", "one", who=s))
    flow.ok(rl("review", "request", "T-0001", "--kind", "tree", who=s))
    one, two = (f"RV-{n:04d}" for n in (first, first + 1))
    flow.ok(rl("review", "answer", one, "--verdict", "pass", who=f"reviewer-{one}"))
    flow.ok(rl("review", "request", "T-0001", "--kind", "tree", who=s))  # the tree changed: a new round
    flow.ok(rl("review", "answer", two, "--verdict", "fail", "--findings", json.dumps(
        [{"category": "method", "severity": "medium", "text": "scope too wide", "loc": None}]),
        who=f"reviewer-{two}"))
    assert not any(i.type == "review-verdict" and i.subject == one for i in flow.items(project, s))
    code, _, err = rl("task", "deliver", "T-0001", "--conclusion", "x", who=s)
    assert code == 1 and "tree review has not passed" in err


def test_the_researchers_go_on_item_clears_once_it_runs(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    flow.ok(rl("review", "request", "T-0003", who=r))
    rv = max(i.subject for i in flow.items(project, "reviewer-RV-0003"))
    flow.ok(rl("review", "answer", rv, "--verdict", "pass", who=f"reviewer-{rv}"))
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


def test_a_delivered_tree_is_not_reviewed_again(project, rl):
    flow.tree(project, rl)
    code, _, err = rl("review", "request", "T-0001", "--kind", "tree", who="scientist-T-0001")
    assert code == 1 and "delivered" in err


def test_the_go_on_item_follows_the_latest_round_and_the_event_order(project, rl):
    flow.dispatched(project, rl)
    r = "researcher-T-0003"
    flow.run_arm(project, rl, "single")  # registered before the pass (in the same second, possibly)
    flow.ok(rl("review", "request", "T-0003", who=r))
    flow.ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    assert "review-verdict" in flow.types(project, r)  # a run before the pass does not clear it
    flow.ok(rl("review", "request", "T-0003", who=r))  # a newer round that fails says fix, not go on
    flow.ok(rl("review", "answer", "RV-0004", "--verdict", "fail", "--findings", json.dumps(
        [{"category": "method", "severity": "high", "text": "wrong baseline", "loc": None}]), who="reviewer-RV-0004"))
    verdicts = [i for i in flow.items(project, r) if i.type == "review-verdict"]
    assert [i.subject for i in verdicts] == ["RV-0004"] and "fix" in verdicts[0].ask
