"""Review and advice tickets through fake host CLIs: family rule, fallbacks, reuse, honesty when unavailable."""

import json
import os
import stat

import pytest
import yaml

from smooth_research import adapters, delegate, research, reviews, tasks
from smooth_research.util import RLError

FAKE_CODEX = r"""#!{python}
import json, os, sys, uuid, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
model = args[args.index("--model") + 1]
out = args[args.index("--output-last-message") + 1]
thread = str(uuid.uuid4())
answer = os.environ.get("FAKE_ANSWER", 'Looks fine.\n```json\n{"verdict": "pass", "findings": [], "checked": ["all"], "unchecked": []}\n```')
home = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "30"
home.mkdir(parents=True, exist_ok=True)
(home / f"rollout-2026-10-30T00-00-00-{thread}.jsonl").write_text(
    json.dumps({"type": "turn_context", "payload": {"model": model, "effort": "medium"}}) + "\n")
pathlib.Path(out).write_text(answer)
for event in ({"type": "thread.started", "thread_id": thread}, {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
              {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}):
    print(json.dumps(event))
"""

FAKE_CLAUDE = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
sys.stdin.read()
model = args[args.index("--model") + 1]
answer = os.environ.get("FAKE_ANSWER", 'Proposals.\n```json\n{"proposals": [{"title": "x"}, {"title": "y"}], "pause_recommended": false}\n```')
print(json.dumps({"type": "result", "is_error": False, "result": answer, "session_id": args[args.index("--session-id") + 1],
                  "modelUsage": {model: {"inputTokens": 5}}, "total_cost_usd": 0.02, "usage": {"input_tokens": 5}}))
"""


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
        path = bindir / name
        path.write_text(body.replace("{python}", os.sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return bindir


def code(tmp_path, body="def f(x):\n    return x\n", config=None):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/model.py").write_text(body)
    (tmp_path / "configs").mkdir(exist_ok=True)
    (tmp_path / "configs/run.yaml").write_text(yaml.safe_dump(config or {"lr": 0.1, "seed": 0}))


def request(project, author="claude-sonnet-5-5", **kw):
    return reviews.request_review(project, "E-0001", ["src/model.py"], "is the operator right?", [author],
                                  configs=["configs/run.yaml"], ignore_keys=["seed"], scope="new operator", **kw)


def test_family_detection():
    assert adapters.family("claude-sonnet-5-5") == "anthropic" and adapters.family("anthropic/claude-x") == "anthropic"
    assert adapters.family("gpt-6-astra") == "openai" and adapters.family("o3") == "openai"
    assert adapters.family("mystery-model") is None
    assert adapters.Entry.parse("codex:gpt-6-astra@medium") == adapters.Entry("codex", "gpt-6-astra", "medium")
    with pytest.raises(RLError):
        adapters.Entry.parse("gpt-6-astra")


def test_claude_written_code_goes_to_a_codex_reviewer_and_is_reused_while_unchanged(project, fakes, tmp_path):
    code(tmp_path)
    rv = request(project)
    with project.read() as tx:
        assert tx.require(rv["id"])["candidates"] == ["codex:gpt-6-astra@medium"]
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and out["relation"] == "cross_model"
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["observed_model"] == "gpt-6-astra" and t["host_session_id"] and t["answer_ref"]
    assert (tmp_path / "reports/reviews" / f"{rv['id']}.md").exists()
    code(tmp_path, config={"lr": 0.1, "seed": 7})  # a whitelisted key changed: still the same scope
    again = request(project)
    assert again["reused_from"] == rv["id"]
    code(tmp_path, body="def f(x):\n    return 2 * x\n")  # the code changed: a new review is needed
    fresh = request(project)
    assert "reused_from" not in fresh and fresh["id"] != rv["id"]


def test_codex_written_code_goes_to_a_claude_reviewer(project, fakes, tmp_path):
    code(tmp_path)
    rv = request(project, author="gpt-6-astra")
    with project.read() as tx:
        assert tx.require(rv["id"])["candidates"] == ["claude:claude-opus-5-5@medium"]
    out = reviews.run_ticket(project, rv["id"])
    assert out["relation"] == "cross_model" and "claude-opus-5-5" in out["summary"]


def test_unavailable_reviewer_is_recorded_not_passed(project, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    code(tmp_path)
    rv = request(project)
    out = reviews.run_ticket(project, rv["id"])
    assert out["status"] == "unavailable" and "not a pass" in out["summary"]
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["status"] == "unavailable" and not t.get("verdict") and "not on PATH" in t["error"]


def test_no_cross_family_candidate_means_unavailable_at_request(project, tmp_path):
    cfg = project.project.root / "smooth-research.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["models"]["reviewer"]["candidates"] = ["claude:claude-opus-5-5"]
    cfg.write_text(yaml.safe_dump(data))
    from smooth_research.project import find_project
    from smooth_research.store import Store
    store = Store(find_project())
    code(tmp_path)
    rv = request(store)
    with store.read() as tx:
        assert tx.require(rv["id"])["status"] == "unavailable"


def test_unparsed_answers_and_same_family_imports_are_labelled_honestly(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    monkeypatch.setenv("FAKE_ANSWER", "I think it is fine, no JSON here.")
    rv = request(project)
    assert reviews.run_ticket(project, rv["id"])["verdict"] == "unparsed"
    code(tmp_path, body="x = 1\n")
    rv2 = request(project)
    (tmp_path / "answer.md").write_text('```json\n{"verdict": "issues", "findings": [{"location": "a"}]}\n```')
    out = reviews.import_answer(project, rv2["id"], str(tmp_path / "answer.md"), "claude-opus-5-5", "subagent")
    assert out["relation"] == "same_family_independent_session" and out["verdict"] == "issues"


def test_advice_is_deduplicated_until_the_evidence_changes(project, fakes):
    first = reviews.prepare_advice(project, "why no gain?", "H-0001")
    assert reviews.prepare_advice(project, "why no gain?", "H-0001")["duplicate"]
    out = reviews.run_ticket(project, first["id"])
    assert "2 proposal(s)" in out["summary"]
    research.add_evidence(project, "new negative result", "screening", "valid", challenges=["H-0001"])
    second = reviews.prepare_advice(project, "why no gain?", "H-0001")
    assert second["id"] != first["id"] and not second.get("duplicate")
    h = research.add_hypothesis(project, "advised route", "a different writer", derived_from=[first["id"]])
    assert h["id"] == "H-0002"


def test_manual_advisor_waits_for_an_imported_answer(project, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    ad = reviews.prepare_advice(project, "which route?")
    out = reviews.run_ticket(project, ad["id"])
    assert out["status"] == "pending" and "submit" in out["summary"]
    (tmp_path / "gpt.md").write_text('```json\n{"proposals": [], "pause_recommended": true}\n```')
    done = reviews.import_answer(project, ad["id"], str(tmp_path / "gpt.md"), "gpt-pro", "ChatGPT Pro (manual)")
    assert "pause recommended" in done["summary"]


def test_delegation_records_the_exact_session_and_model(project, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_ANSWER", "claimed and delivered")
    tasks.create_task(project, "implement", "implement", ["E-0001"], to_role="researcher")
    out = delegate.delegate(project, "T-0001", "codex:gpt-6-astra@medium")
    assert out["ok"] and out["observed_model"] == "gpt-6-astra" and out["host_session_id"]
    with project.read() as tx:
        s = tx.require(out["session"])
    assert (s["host"], s["model_requested"], s["model_observed"]) == ("codex", "gpt-6-astra", "gpt-6-astra")
    prompt = delegate.prompt_for(project, "T-0001", "researcher")
    assert "task claim T-0001" in prompt and "Work pack for T-0001" in prompt
