"""The watcher against a fake herdr: a beat opens a session for work nobody holds (claiming its task), tells an idle
agent each item once, records a dialog for the coordinator, reopens a session whose agent is gone and closes the
ones whose work is finished."""

import json
import re

import flow
import pytest

from smooth_research import adapters, herdr, launch, page, usage, watch
from smooth_research.util import RLError


class FakeHerdr:
    """Tabs and agents as herdr would report them; every call is kept."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.tabs: dict[str, str] = {}  # tab id -> label
        self.agents: dict[str, dict] = {}  # pane id -> {name, status, seq}
        self.prompts: list[tuple[str, str]] = []

    def call(self, *args, timeout=90):
        self.calls.append(args)
        group, command, rest = args[0], args[1], list(args[2:])
        if (group, command) == ("tab", "create"):
            n = len(self.tabs) + 1
            tab, pane = f"w1:t{n}", f"w1:p{n}"
            self.tabs[tab] = rest[rest.index("--label") + 1]
            return {"tab": {"tab_id": tab}, "root_pane": {"pane_id": pane}}
        if (group, command) == ("tab", "close"):
            tab = rest[0]
            self.tabs.pop(tab, None)
            self.agents.pop(tab.replace(":t", ":p"), None)
            return {}
        if (group, command) == ("tab", "get"):
            if rest[0] not in self.tabs:
                raise RLError("HERDR", "no tab", herdr_code="tab_not_found")
            return {"tab": {"tab_id": rest[0], "label": self.tabs[rest[0]]}}
        if (group, command) == ("pane", "list"):  # one pane per tab here
            return {"panes": [{"pane_id": t.replace(":t", ":p"), "tab_id": t,
                               "agent": "claude" if t.replace(":t", ":p") in self.agents else None}
                              for t in self.tabs]}
        if (group, command) == ("agent", "start"):
            name, pane = rest[0], rest[rest.index("--pane") + 1]
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):  # herdr's grammar
                raise RLError("HERDR", "invalid name", herdr_code="invalid_agent_name")
            if any(a["name"] == name for a in self.agents.values()):
                raise RLError("HERDR", "name taken", herdr_code="agent_name_taken")
            self.agents[pane] = {"name": name, "agent_status": "idle", "state_change_seq": 1,
                                 "args": rest[rest.index("--") + 1:]}
            return {"agent": {"pane_id": pane}}
        if (group, command) == ("agent", "get"):  # by pane id or by name
            pane = rest[0] if rest[0] in self.agents else next(
                (p for p, a in self.agents.items() if a["name"] == rest[0]), None)
            if pane is None:
                raise RLError("HERDR", "no agent", herdr_code="agent_not_found")
            return {"agent": {**self.agents[pane], "pane_id": pane, "tab_id": pane.replace(":p", ":t")}}
        if (group, command) == ("agent", "prompt"):
            if self.agents.get(rest[0], {}).get("agent_status") == "blocked":
                raise RLError("HERDR", "blocked", herdr_code="agent_blocked")
            self.prompts.append((rest[0], rest[1]))
            return {}
        raise AssertionError(f"unexpected herdr call {args}")

    def set(self, pane: str, status: str) -> None:
        a = self.agents[pane]
        a["agent_status"], a["state_change_seq"] = status, a["state_change_seq"] + 1


@pytest.fixture
def fake(monkeypatch):
    h = FakeHerdr()
    monkeypatch.setattr(herdr, "call", h.call)
    monkeypatch.setattr(herdr, "read", lambda target: "Do you want to proceed?\n❯ 1. Yes\n  2. No")
    monkeypatch.setattr(adapters, "available", lambda entry: None)  # every configured host is installed
    monkeypatch.setattr(usage, "low_hosts", lambda store: {})
    return h


def sessions(store) -> dict[str, dict]:
    with store.read() as tx:
        return {s["name"]: s for s in tx.all("Session") if s["status"] == "open"}


def test_a_beat_opens_a_session_that_claims_its_task_and_tells_it_once(project, rl, fake):
    flow.approved(project, rl)
    out = watch.beat(project)
    assert any(a.startswith("scientist-T-0002: opened") for a in out["actions"]), out
    with project.read() as tx:
        task = tx.require("T-0002")
    assert task["holder"] == "scientist-T-0002" and task["state"] == "active"
    s = sessions(project)["scientist-T-0002"]
    assert s["cwd"] == task["worktree"] and s["herdr_pane"] in fake.agents
    created = next(c for c in fake.calls if c[:2] == ("tab", "create"))
    env = [created[i + 1] for i, a in enumerate(created) if a == "--env"]
    assert "SMOOTH_RESEARCH_SESSION=scientist-T-0002" in env and created[created.index("--cwd") + 1] == s["cwd"]
    [(pane, first)] = fake.prompts
    assert pane == s["herdr_pane"] and "You are scientist-T-0002" in first and "rl pack" in first
    assert "coordinator" not in sessions(project)  # rl resume opens the coordinator's pane, not the beat

    again = watch.beat(project)
    assert len(fake.prompts) == 1, again  # told once; nothing new
    assert json.loads(watch.heartbeat_path(project).read_text())["at"]
    assert watch.alive(project)


def test_a_new_item_is_typed_into_an_idle_pane_and_held_while_it_works(project, rl, fake):
    flow.approved(project, rl)
    watch.beat(project)
    pane = sessions(project)["scientist-T-0002"]["herdr_pane"]
    flow.ok(rl("question", "--to", "scientist-T-0002", "--on", "T-0002", "--text", "which arm first?",
               who="coordinator"))
    fake.set(pane, "working")
    watch.beat(project)
    assert len(fake.prompts) == 1  # it works: nothing typed
    fake.set(pane, "idle")
    watch.beat(project)
    assert len(fake.prompts) == 2 and "question T-0002" in fake.prompts[-1][1]
    assert "which arm first?" in fake.prompts[-1][1]


def test_a_dialog_goes_to_the_coordinator_and_clears_when_answered(project, rl, fake):
    flow.approved(project, rl)
    watch.beat(project)
    pane = sessions(project)["scientist-T-0002"]["herdr_pane"]
    fake.set(pane, "blocked")
    watch.beat(project)
    assert "Do you want to proceed?" in sessions(project)["scientist-T-0002"]["needs_decision"]
    assert "session-needs-decision" in flow.types(project, "coordinator")
    fake.set(pane, "idle")
    watch.beat(project)
    assert sessions(project)["scientist-T-0002"].get("needs_decision") is None
    assert "session-needs-decision" not in flow.types(project, "coordinator")


def test_a_gone_agent_ends_its_session_and_the_next_beat_opens_it_again(project, rl, fake):
    flow.approved(project, rl)
    watch.beat(project)
    first = sessions(project)["scientist-T-0002"]
    del fake.agents[first["herdr_pane"]]
    out = watch.beat(project)
    assert any("its agent is gone" in a for a in out["actions"])
    watch.beat(project)
    again = sessions(project)["scientist-T-0002"]
    assert again["id"] != first["id"] and again["conversations"] == 2
    with project.read() as tx:
        assert tx.require("T-0002")["holder"] == "scientist-T-0002"  # still its task


def test_a_session_being_started_is_left_to_its_opener(project, rl, fake):
    """rl resume writes the coordinator's record before its agent starts; a beat meanwhile neither ends it (which
    would orphan the agent) nor opens another."""
    flow.tree(project, rl)
    with project.write() as tx:
        tx.create("Session", {"id": tx.next_id("Session"), "name": "coordinator", "role": "coordinator",
                              "subject": "project", "host": "claude", "entry": "claude:m", "status": "open",
                              "cwd": str(project.project.root)})
    out = watch.beat(project)
    assert not any("closed" in a for a in out["actions"]), out
    assert "coordinator" in sessions(project) and not fake.prompts
    assert watch.ensure_coordinator(project) == "the coordinator is being started"


def test_a_coordinator_whose_record_ended_while_its_agent_lives_is_bound_again(project, rl, fake):
    flow.tree(project, rl)  # the delivered tree waits on the coordinator
    name = launch.herdr_name(project, "coordinator")
    with project.write() as tx:
        tx.create("Session", {"id": tx.next_id("Session"), "name": "coordinator", "role": "coordinator",
                              "subject": "project", "host": "claude", "entry": "claude:m", "status": "ended",
                              "cwd": str(project.project.root)})
    fake.tabs["w1:t7"] = name
    fake.agents["w1:p7"] = {"name": name, "agent_status": "idle", "state_change_seq": 1}
    out = watch.beat(project)
    assert "coordinator: bound again to its live agent in w1:p7" in out["actions"], out
    s = sessions(project)["coordinator"]
    assert s["herdr_pane"] == "w1:p7" and s["herdr_tab"] == "w1:t7"
    assert [p for p, _ in fake.prompts] == ["w1:p7"] and "T-0001" in fake.prompts[0][1]
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)  # no second agent under its name
    with project.write() as tx:  # ended again: rl resume binds it too, rather than starting another
        tx.update(s["id"], {"status": "ended"})
    assert watch.ensure_coordinator(project) == "the coordinator runs in pane w1:p7 (its session bound again)"


def test_a_live_agent_in_a_tab_rl_did_not_make_is_not_bound(project, rl, fake):
    flow.tree(project, rl)
    name = launch.herdr_name(project, "coordinator")
    with project.write() as tx:
        tx.create("Session", {"id": tx.next_id("Session"), "name": "coordinator", "role": "coordinator",
                              "subject": "project", "host": "claude", "entry": "claude:m", "status": "ended",
                              "cwd": str(project.project.root)})
    fake.tabs["w1:t7"] = "someone else's"
    fake.agents["w1:p7"] = {"name": name, "agent_status": "idle", "state_change_seq": 1}
    watch.beat(project)
    assert "coordinator" not in sessions(project) and not fake.prompts


def test_a_finished_session_is_closed_with_its_tab(project, rl, fake):
    flow.tree(project, rl)
    with project.write() as tx:  # the tree task's scientist ran in a session of its own
        tx.create("Session", {"id": tx.next_id("Session"), "name": "scientist-T-0001", "role": "scientist",
                              "subject": "T-0001", "host": "claude", "entry": "claude:m", "status": "open",
                              "cwd": str(project.project.root), "herdr_tab": "w1:t9", "herdr_pane": "w1:p9"})
    fake.tabs["w1:t9"] = launch.herdr_name(project, "scientist-T-0001")
    fake.agents["w1:p9"] = {"name": launch.herdr_name(project, "scientist-T-0001"), "agent_status": "idle",
                            "state_change_seq": 1}
    flow.ok(rl("task", "accept", "T-0001", who="coordinator"))
    out = watch.beat(project)
    assert "scientist-T-0001: closed (its work is finished)" in out["actions"]
    assert "w1:t9" not in fake.tabs and "scientist-T-0001" not in sessions(project)


def test_herdr_names_are_lowercase_short_and_differ_between_projects(project, tmp_path):
    name = launch.herdr_name(project, "researcher-T-10003")
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) and name.endswith("-researcher-t-10003")
    other = type(project.project)(tmp_path / "elsewhere" / "proj", {})
    assert launch.herdr_name(type(project)(other, "t"), "coordinator") != launch.herdr_name(project, "coordinator")


def test_a_pane_id_that_now_holds_another_agent_is_left_alone(project, rl, fake):
    """After a herdr restart ids start again: the saved pane and tab may hold another project's agent."""
    flow.approved(project, rl)
    watch.beat(project)
    s = sessions(project)["scientist-T-0002"]
    fake.agents[s["herdr_pane"]]["name"] = "other1234-scientist-t-0002"
    fake.tabs[s["herdr_tab"]] = "other1234-scientist-t-0002"
    prompts = len(fake.prompts)
    out = watch.beat(project)
    assert any("its agent is gone" in a for a in out["actions"])
    assert s["herdr_tab"] in fake.tabs  # not closed: not ours
    assert not any(p == s["herdr_pane"] for p, _ in fake.prompts[prompts:])
    assert not any(c[:2] == ("tab", "close") for c in fake.calls)


def test_a_tab_with_its_label_but_another_agent_in_it_is_not_closed(project, rl, fake):
    flow.approved(project, rl)
    watch.beat(project)
    s = sessions(project)["scientist-T-0002"]
    fake.agents[s["herdr_pane"]]["name"] = "someone-else"
    assert launch.own_tab(project, s) is None
    fake.agents.pop(s["herdr_pane"])  # an empty pane (the agent exited): the tab is still ours to close
    assert launch.own_tab(project, s) == s["herdr_tab"]


def test_a_session_with_nothing_to_do_is_still_watched_for_dialogs(project, rl, fake):
    flow.dispatched(project, rl)
    with project.write() as tx:  # the scientist waits on its researcher: no items, an open session
        tx.create("Session", {"id": tx.next_id("Session"), "name": "scientist-T-0002", "role": "scientist",
                              "subject": "T-0002", "host": "claude", "entry": "claude:m", "status": "open",
                              "cwd": str(project.project.root), "herdr_tab": "w1:t9", "herdr_pane": "w1:p9"})
    fake.agents["w1:p9"] = {"name": launch.herdr_name(project, "scientist-T-0002"), "agent_status": "blocked",
                            "state_change_seq": 2}
    assert flow.types(project, "scientist-T-0002") == []
    watch.beat(project)
    assert "session-needs-decision" in flow.types(project, "coordinator")


def test_a_claim_that_fails_does_not_stop_the_beat(project, rl, fake, monkeypatch):
    flow.approved(project, rl)
    from smooth_research import tasks

    def taken(store, tid, holder):
        raise tasks.RLError("CONFLICT", f"{tid} is active, held by someone")
    monkeypatch.setattr(tasks, "claim", taken)
    out = watch.beat(project)
    assert any("not opened" in a for a in out["actions"]) and watch.alive(project)
    assert "scientist-T-0002" not in sessions(project)


def test_the_page_is_drawn_again_only_when_the_records_changed(project, rl, fake, monkeypatch):
    flow.approved(project, rl)
    watch.beat(project)
    drawn = []
    monkeypatch.setattr(page, "write", lambda store: drawn.append(1))
    pane = sessions(project)["scientist-T-0002"]["herdr_pane"]
    fake.set(pane, "working")
    watch.beat(project)  # the session's seq moved: a record changed
    watch.beat(project)
    watch.beat(project)
    assert len(drawn) == 1


def test_the_watcher_needs_herdr(project):
    with pytest.raises(RLError, match="needs herdr"):
        watch.run(project, once=True)


def test_a_stale_heartbeat_makes_every_command_say_the_watcher_stopped(project, rl):
    assert watch.stale_note(project) is None  # never started: nothing to say
    watch.heartbeat_path(project).write_text(json.dumps({"at": "2026-01-01T00:00:00+00:00", "pid": 1}))
    code, out, _ = rl("status")
    assert code == 0 and "warning: the watcher is not running: run `rl resume`" in out


def _coordinator_row(project, status, pane=None):
    with project.write() as tx:
        sid = tx.next_id("Session")
        tx.create("Session", {"id": sid, "name": "coordinator", "role": "coordinator", "subject": "project",
                              "host": "claude", "entry": "claude:m", "status": status,
                              "cwd": str(project.project.root), **({"herdr_pane": pane, "herdr_tab":
                                                                    pane.replace(":p", ":t")} if pane else {})})
    return sid


def test_an_open_session_whose_pane_id_went_stale_is_bound_to_its_live_agent(project, rl, fake):
    flow.tree(project, rl)
    name = launch.herdr_name(project, "coordinator")
    sid = _coordinator_row(project, "open", pane="w1:p3")  # herdr renumbered: its agent now lives in w1:p7
    fake.tabs["w1:t7"] = name
    fake.agents["w1:p7"] = {"name": name, "agent_status": "idle", "state_change_seq": 1}
    assert watch.ensure_coordinator(project) == "the coordinator runs in pane w1:p7 (its session bound again)"
    assert sessions(project)["coordinator"]["id"] == sid and "w1:t7" in fake.tabs
    assert not any(c[:2] in (("tab", "close"), ("agent", "start")) for c in fake.calls)


def test_a_session_bound_meanwhile_is_returned_and_not_started_again(project, rl, fake):
    flow.tree(project, rl)
    name = launch.herdr_name(project, "coordinator")
    _coordinator_row(project, "open", pane="w1:p7")
    fake.tabs["w1:t7"] = name
    fake.agents["w1:p7"] = {"name": name, "agent_status": "idle", "state_change_seq": 1}
    assert watch.rebind(project, "coordinator")["herdr_pane"] == "w1:p7"  # the watcher bound it a moment ago
    out = watch.open_session(project, "coordinator", [], {})  # rl resume, on its older reading
    assert out.startswith("coordinator: runs already") and not any(c[:2] == ("agent", "start") for c in fake.calls)
