"""Sessions and the observer against a fake herdr (design 2026-11-27, 3.2, 3.3): a session's record follows its
agent (bound again when herdr renumbered it, ended when its agent is gone, opened again by the next command that has
work for it); nothing is done to a pane or tab another agent holds; the observer notes dialogs and redraws the page,
and opens, claims and closes nothing. The FakeHerdr here is shared with test_handoff.py."""

import json
import re

import flow
import pytest

from smooth_research import adapters, herdr, launch, page, sessions, usage, watch
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
    monkeypatch.setattr(herdr, "problem", lambda: None)
    monkeypatch.setattr(herdr, "read", lambda target: "Do you want to proceed?\n❯ 1. Yes\n  2. No")
    monkeypatch.setattr(adapters, "available", lambda entry: None)  # every configured host is installed
    monkeypatch.setattr(usage, "low_hosts", lambda store: {})
    return h


def opened(store) -> dict[str, dict]:
    with store.read() as tx:
        return {s["name"]: s for s in tx.all("Session") if s["status"] == "open"}


S = "scientist-T-0002"


def _coordinator_row(project, status, pane=None):
    with project.write() as tx:
        sid = tx.next_id("Session")
        tx.create("Session", {"id": sid, "name": "coordinator", "role": "coordinator", "subject": "project",
                              "host": "claude", "entry": "claude:m", "status": status,
                              "cwd": str(project.project.root), **({"herdr_pane": pane, "herdr_tab":
                                                                    pane.replace(":p", ":t")} if pane else {})})
    return sid


def _live_coordinator(project, fake, n=7):
    name = launch.herdr_name(project, "coordinator")
    fake.tabs[f"w1:t{n}"] = name
    fake.agents[f"w1:p{n}"] = {"name": name, "agent_status": "idle", "state_change_seq": 1}


def test_a_gone_agent_ends_its_record_and_the_next_command_for_it_opens_it_again(project, rl, fake):
    flow.approved(project, rl)  # release opened the scientist
    first = opened(project)[S]
    del fake.agents[first["herdr_pane"]]
    watch.beat(project)  # the observer opens nothing
    assert not [c for c in fake.calls if c[:2] == ("agent", "start")][1:]
    flow.ok(rl("question", "--to", S, "--on", "T-0002", "--text", "which arm first?", who="coordinator"))
    again = opened(project)[S]
    assert again["id"] != first["id"] and again["conversations"] == 2
    assert "which arm first?" in fake.prompts[-1][1]
    with project.read() as tx:
        assert tx.require("T-0002")["holder"] == S  # still its task


def test_a_session_being_started_is_left_to_its_opener(project, rl, fake):
    """rl resume writes the coordinator's record before its agent starts; meanwhile nothing ends it (which would
    orphan the agent) or opens another."""
    flow.tree(project, rl)
    _coordinator_row(project, "open")
    out = watch.beat(project)
    assert not any("closed" in a for a in out["actions"]), out
    assert "coordinator" in opened(project) and not fake.prompts
    assert sessions.ensure_coordinator(project) == "the coordinator is being started"
    assert sessions.wake(project, "coordinator").startswith("coordinator: being started")


def test_a_coordinator_whose_record_ended_while_its_agent_lives_is_bound_again(project, rl, fake):
    flow.tree(project, rl)
    _coordinator_row(project, "ended")
    _live_coordinator(project, fake)
    assert sessions.ensure_coordinator(project) == "the coordinator runs in pane w1:p7"
    s = opened(project)["coordinator"]
    assert s["herdr_pane"] == "w1:p7" and s["herdr_tab"] == "w1:t7"
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)  # no second agent under its name


def test_a_live_agent_in_a_tab_rl_did_not_make_is_not_bound(project, rl, fake):
    flow.tree(project, rl)
    _coordinator_row(project, "ended")
    _live_coordinator(project, fake)
    fake.tabs["w1:t7"] = "someone else's"
    assert sessions.rebind(project, "coordinator") is None
    watch.beat(project)
    assert "coordinator" not in opened(project) and not fake.prompts


def test_herdr_names_are_lowercase_short_and_differ_between_projects(project, tmp_path):
    name = launch.herdr_name(project, "researcher-T-10003")
    assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) and name.endswith("-researcher-t-10003")
    other = type(project.project)(tmp_path / "elsewhere" / "proj", {})
    assert launch.herdr_name(type(project)(other, "t"), "coordinator") != launch.herdr_name(project, "coordinator")


def test_a_pane_id_that_now_holds_another_agent_is_left_alone(project, rl, fake):
    """After a herdr restart ids start again: the saved pane and tab may hold another project's agent."""
    flow.approved(project, rl)
    s = opened(project)[S]
    fake.agents[s["herdr_pane"]]["name"] = "other1234-scientist-t-0002"
    fake.tabs[s["herdr_tab"]] = "other1234-scientist-t-0002"
    prompts = len(fake.prompts)
    watch.beat(project)
    assert sessions.close(project, S, "test") == f"{S}: closed (test)"  # the record ends; the tab is not ours
    assert s["herdr_tab"] in fake.tabs
    assert not any(p == s["herdr_pane"] for p, _ in fake.prompts[prompts:])
    assert not any(c[:2] == ("tab", "close") for c in fake.calls)


def test_a_tab_with_its_label_but_another_agent_in_it_is_not_closed(project, rl, fake):
    flow.approved(project, rl)
    s = opened(project)[S]
    fake.agents[s["herdr_pane"]]["name"] = "someone-else"
    assert launch.own_tab(project, s) is None
    fake.agents.pop(s["herdr_pane"])  # an empty pane (the agent exited): the tab is still ours to close
    assert launch.own_tab(project, s) == s["herdr_tab"]


def test_a_session_with_nothing_to_do_is_still_watched_for_dialogs(project, rl, fake):
    flow.dispatched(project, rl)  # the scientist waits on its researcher: no items, an open session
    assert flow.types(project, S) == []
    fake.set(opened(project)[S]["herdr_pane"], "blocked")
    watch.beat(project)
    assert "Do you want to proceed?" in opened(project)[S]["needs_decision"]
    assert "session-needs-decision" in flow.types(project, "coordinator")
    fake.set(opened(project)[S]["herdr_pane"], "idle")
    watch.beat(project)
    assert opened(project)[S].get("needs_decision") is None
    assert "session-needs-decision" not in flow.types(project, "coordinator")


def test_the_page_is_drawn_again_only_when_the_records_changed(project, rl, fake, monkeypatch):
    flow.approved(project, rl)
    watch.beat(project)
    drawn = []
    monkeypatch.setattr(page, "write", lambda store: drawn.append(1))
    fake.set(opened(project)[S]["herdr_pane"], "working")
    watch.beat(project)  # the session's seq moved: a record changed
    watch.beat(project)
    watch.beat(project)
    assert len(drawn) == 1


def test_without_herdr_the_observer_still_draws_the_page(project, rl, monkeypatch):
    drawn = []
    monkeypatch.setattr(page, "write", lambda store: drawn.append(1))
    assert watch.run(project, once=True)["summary"] == "nothing to do" and drawn
    assert watch.alive(project)


def test_an_observer_that_stopped_is_no_warning(project, rl):
    """The observer is optional: a stale heartbeat is a status line, not a warning on every command."""
    watch.heartbeat_path(project).write_text(json.dumps({"at": "2026-01-01T00:00:00+00:00", "pid": 1}))
    code, out, _ = rl("status")
    assert code == 0 and "warning:" not in out and "observer: not running (optional" in out


def test_an_open_session_whose_pane_id_went_stale_is_bound_to_its_live_agent(project, rl, fake):
    flow.tree(project, rl)
    sid = _coordinator_row(project, "open", pane="w1:p3")  # herdr renumbered: its agent now lives in w1:p7
    _live_coordinator(project, fake)
    assert sessions.ensure_coordinator(project) == "the coordinator runs in pane w1:p7"
    assert opened(project)["coordinator"]["id"] == sid and "w1:t7" in fake.tabs
    assert not any(c[:2] in (("tab", "close"), ("agent", "start")) for c in fake.calls)


def test_a_session_bound_meanwhile_is_returned_and_not_started_again(project, rl, fake):
    flow.tree(project, rl)
    _coordinator_row(project, "open", pane="w1:p7")
    _live_coordinator(project, fake)
    assert sessions.rebind(project, "coordinator")["herdr_pane"] == "w1:p7"
    out = sessions.open_session(project, "coordinator")  # a command, on its older reading
    assert out.startswith("coordinator: runs already") and not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_a_tab_lookup_herdr_cannot_answer_closes_and_starts_nothing(project, rl, fake, monkeypatch):
    flow.tree(project, rl)
    _coordinator_row(project, "open", pane="w1:p3")  # stale: its agent lives in w1:p7
    _live_coordinator(project, fake)
    real, failed = fake.call, []

    def timeout_once(*args, timeout=90):
        if args[:2] == ("tab", "get") and not failed:
            failed.append(args)
            raise RLError("HERDR", "timed out", herdr_code="timeout")
        return real(*args, timeout=timeout)
    monkeypatch.setattr(herdr, "call", timeout_once)
    assert "could not be checked" in sessions.ensure_coordinator(project)
    assert "w1:t7" in fake.tabs and not any(c[:2] in (("tab", "close"), ("agent", "start")) for c in fake.calls)
    assert sessions.ensure_coordinator(project) == "the coordinator runs in pane w1:p7"  # the next look binds it


def test_an_old_race_that_left_two_records_binds_the_open_one(project, rl, fake):
    flow.tree(project, rl)
    first = _coordinator_row(project, "open", pane="w1:p3")
    _coordinator_row(project, "ended")  # the failed second start of the old race
    _live_coordinator(project, fake)
    sessions.ensure_coordinator(project)
    assert opened(project)["coordinator"]["id"] == first
    with project.read() as tx:
        assert [s["status"] for s in tx.all("Session", name="coordinator")] == ["open", "ended"]


def test_a_close_herdr_cannot_answer_keeps_the_session_open(project, rl, fake, monkeypatch):
    flow.approved(project, rl)
    real = fake.call

    def timeout(*args, timeout=90):
        if args[:2] == ("tab", "close"):
            raise RLError("HERDR", "timed out", herdr_code="timeout")
        return real(*args, timeout=timeout)
    monkeypatch.setattr(herdr, "call", timeout)
    assert "not closed" in sessions.close(project, S, "its work is finished")
    assert S in opened(project)


def test_nothing_is_typed_into_a_dialog_herdr_reports_as_idle(project, rl, fake, monkeypatch):
    """Codex trial, 11-25: a Codex session at its hook review is idle to herdr; the review request typed into it was
    lost while it counted as told."""
    flow.approved(project, rl)
    s = opened(project)[S]
    with project.write() as tx:
        tx.update(s["id"], {"host": "codex"})
    screen = {"text": "Hooks need review\n1 hook is new or changed.\n› 1. Review hooks"}
    monkeypatch.setattr(herdr, "read", lambda target: screen["text"])
    fake.set(s["herdr_pane"], "idle")
    out = flow.ok(rl("question", "--to", S, "--on", "T-0002", "--text", "which arm first?", who="coordinator"))
    mine = lambda: [t for p, t in fake.prompts if p == s["herdr_pane"]]  # noqa: E731
    assert "waits at a dialog" in out and len(mine()) == 1  # held: only the first prompt, before the dialog
    assert "Hooks need review" in opened(project)[S]["needs_decision"]
    assert "session-needs-decision" in flow.types(project, "coordinator")
    screen["text"] = ("• read the reviewer's screen: Hooks need review / 1 hook is new or changed.\n"
                      "› Ask Codex to do anything\n  GPT-Reserve medium · /tmp/x\n  ← for agents · ? for shortcuts")
    watch.beat(project)  # its own transcript quotes another's dialog: it takes prompts
    assert opened(project)[S].get("needs_decision") is None
    assert len(mine()) == 2 and "which arm first?" in mine()[-1]
