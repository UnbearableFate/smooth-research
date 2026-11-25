"""The to-do items: who is waited on at each step, the escalation ladder, the dialogue and rl doctor's invariant."""

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
