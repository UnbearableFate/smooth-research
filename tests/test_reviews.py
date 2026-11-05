"""Review and advice tickets through fake host CLIs: configured order, advice rounds, reuse, honesty when
unavailable."""

import json
import os
import stat

import pytest
import yaml

from smooth_research import (adapters, delegate, documents, literature, messages, research, reviews, runs, scope,
                            tasks, view)
from smooth_research.util import RLError

FAKE_CODEX = r"""#!{python}
import json, os, sys, uuid, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
if os.environ.get("FAKE_PROMPT_OUT"):
    pathlib.Path(os.environ["FAKE_PROMPT_OUT"]).write_text(prompt)
if os.environ.get("FAKE_ARGS_OUT"):
    pathlib.Path(os.environ["FAKE_ARGS_OUT"]).write_text(" ".join(args))
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
import json, os, sys, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
if os.environ.get("FAKE_PROMPT_OUT"):
    pathlib.Path(os.environ["FAKE_PROMPT_OUT"]).write_text(prompt)
if os.environ.get("FAKE_ARGS_OUT"):
    pathlib.Path(os.environ["FAKE_ARGS_OUT"]).write_text(" ".join(args))
model = args[args.index("--model") + 1]
answer = os.environ.get("FAKE_ANSWER", 'Proposals.\n```json\n{"proposals": [{"title": "x"}, {"title": "y"}], "pause_recommended": false}\n```')
print(json.dumps({"type": "result", "is_error": False, "result": answer, "session_id": args[args.index("--session-id") + 1],
                  "modelUsage": {model: {"inputTokens": 5}}, "total_cost_usd": 0.02, "usage": {"input_tokens": 5}}))
"""


FAKE_PI = r"""#!{python}
import json, os, sys, pathlib
args = sys.argv[1:]
sys.stdin.read()
if os.environ.get("FAKE_ARGS_OUT"):
    pathlib.Path(os.environ["FAKE_ARGS_OUT"]).write_text(" ".join(args))
provider, model = args[args.index("--model") + 1].split("/", 1)
answer = os.environ.get("FAKE_ANSWER", 'Fine.\n```json\n{"verdict": "pass", "findings": [], "checked": ["all"], "unchecked": []}\n```')
failed = bool(os.environ.get("FAKE_PI_FAIL"))
print(json.dumps({"type": "session", "id": args[args.index("--session-id") + 1]}))
print(json.dumps({"type": "message_end", "message": {
    "role": "assistant", "content": [{"type": "text", "text": "" if failed else answer}], "model": model,
    "provider": provider, "usage": {"totalTokens": 9, "cost": {"total": 0.01}},
    "stopReason": "error" if failed else "stop", "errorMessage": "quota exceeded" if failed else None}}))
"""


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE), ("pi", FAKE_PI)):
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


def test_an_answer_block_may_hold_a_fenced_patch():
    patch = {"verdict": "issues", "findings": [{"fix": "Patch:\n```\n-    return a\n+    return b\n```"}]}
    text = f"Earlier draft:\n```json\n{{\"verdict\": \"pass\"}}\n```\nFinal:\n```json\n{json.dumps(patch, indent=1)}\n```\n"
    assert adapters.last_json_block(text) == patch
    assert adapters.last_json_block("```json\n{\"verdict\": \"pass\"}\n```\n```json\nnot json\n```") == {"verdict": "pass"}
    assert adapters.last_json_block("no block {\"verdict\": \"pass\"}") is None


def test_family_detection():
    assert adapters.family("claude-sonnet-5-5") == "anthropic" and adapters.family("anthropic/claude-x") == "anthropic"
    assert adapters.family("gpt-6-astra") == "openai" and adapters.family("o3") == "openai"
    assert adapters.family("mystery-model") is None
    assert adapters.Entry.parse("codex:gpt-6-astra@medium") == adapters.Entry("codex", "gpt-6-astra", "medium")
    assert adapters.Entry.parse("pi:openai/gpt-6-astra@max").family == "openai"
    assert adapters.family("deepseek/deepseek-flash") == "deepseek" and adapters.family("openrouter/google/gemini-3") == "google"
    assert reviews._relation(["mystery-1"], "mystery-1", "pi:x/mystery-1") == "same_family_independent_session"
    assert reviews._relation(["deepseek/deepseek-flash"], None, "deepseek-flash") == "same_family_unverified"
    with pytest.raises(RLError):
        adapters.Entry.parse("gpt-6-astra")


def configure(tmp_path, **models):
    """Rewrite the model lists and open the project again."""
    from smooth_research.project import find_project
    from smooth_research.store import Store
    cfg = tmp_path / "smooth-research.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["models"].update(models)
    cfg.write_text(yaml.safe_dump(data))
    return Store(find_project())


def test_the_first_reviewer_answers_and_is_reused_while_the_code_is_unchanged(project, fakes, tmp_path):
    code(tmp_path)
    rv = request(project)
    with project.read() as tx:
        assert tx.require(rv["id"])["candidates"] == ["codex:gpt-6-astra@medium", "claude:claude-opus-5-5@medium"]
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and out["relation"] == "cross_model"
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["observed_model"] == "gpt-6-astra" and t["host_session_id"] and t["answer_ref"]
    assert (tmp_path / "research/reviews" / f"{rv['id']}.md").exists()
    code(tmp_path, config={"lr": 0.1, "seed": 7})  # a whitelisted key changed: still the same scope
    again = request(project)
    assert again["reused_from"] == rv["id"]
    code(tmp_path, body="def f(x):\n    return 2 * x\n")  # the code changed: a new review is needed
    fresh = request(project)
    assert "reused_from" not in fresh and fresh["id"] != rv["id"]


PASS = 'Fine.\n```json\n{"verdict": "pass", "findings": [], "checked": ["all"], "unchecked": []}\n```'


def test_reviewers_are_tried_in_order_whatever_model_wrote_the_code(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    rv = request(project, author="gpt-6-astra")
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and out["relation"] == "same_family_independent_session"
    check = scope.code_coverage(project, "E-0001", {"content_digest": "unknown"})
    assert check["status"] == "covered" and check["reviews"] == [f"{rv['id']} pass"]  # the relation is not a gate
    (fakes / "codex").unlink()  # the first reviewer is not installed: the next one answers
    monkeypatch.setenv("FAKE_ANSWER", PASS)
    code(tmp_path, body="def f(x):\n    return 3 * x\n")
    rv2 = request(project, author="gpt-6-astra")
    out = reviews.run_ticket(project, rv2["id"])
    assert out["relation"] == "cross_model" and "claude-opus-5-5" in out["summary"]
    with project.read() as tx:
        assert "codex CLI is not on PATH" in tx.require(rv2["id"])["calls"][0]["error"]


def finding(severity):
    return ('Problems.\n```json\n{"verdict": "issues", "findings": [{"location": "src/model.py:2", "evidence": "e", '
            f'"impact": "result_error", "severity": "{severity}", "fix": "f", "verify": "v"}}], "checked": ["all"], '
            '"unchecked": []}\n```')


def test_a_review_round_holds_two_reviews_and_then_goes_to_the_coordinator(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    monkeypatch.setenv("FAKE_ANSWER", finding("medium"))
    first = request(project)
    out = reviews.run_ticket(project, first["id"])
    assert "medium open" in out["summary"] and not out["escalated"]
    assert scope.code_coverage(project, "E-0001", {"content_digest": "x"})["status"] == "not_passed"
    code(tmp_path, body="def f(x):\n    return 2 * x\n")
    second = request(project, recheck_of=first["id"])
    assert "(review 2 of 2 in round RV-0001)" in second["summary"]
    out = reviews.run_ticket(project, second["id"])
    assert out["escalated"] and "the coordinator decides" in out["summary"]
    assert (f"{second['id']}: the review round RV-0001 used its 2 reviews and medium finding(s) remain"
            in messages.inbox(project, "coordinator")["summary"])
    for named in (second["id"], first["id"]):  # the round is full whichever of its reviews a re-check names
        with pytest.raises(RLError) as exc:
            request(project, recheck_of=named)
        assert exc.value.code == "LIMIT" and f"--recheck-of {second['id']} --new-round" in exc.value.message
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    with pytest.raises(RLError):
        reviews.approve(project, second["id"], "fine by me")  # approving is the coordinator's
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    with project.read() as tx:
        assert any(i.startswith(f"{second['id']} review round RV-0001 used its two reviews") for i in research.attention(tx))
    before = scope.code_coverage(project, "E-0001", {"content_digest": "x"})  # a run registered now records this
    reviews.approve(project, second["id"], "the shift is a documented choice; keep it")
    with project.read() as tx:
        assert not any(i.startswith(second["id"]) for i in research.attention(tx))
        assert runs.code_review_note(before, tx).endswith(f"; approved by the coordinator since: {second['id']}")
        assert "since" not in runs.code_review_note(before)
    covered = scope.code_coverage(project, "E-0001", {"content_digest": "x"})
    assert covered["status"] == "covered"
    assert covered["reviews"] == [f"{second['id']} issues, medium open, approved by the coordinator"]
    third = request(project, recheck_of=second["id"], new_round="the coordinator wants the loss path checked again")
    with project.read() as tx:
        t = tx.require(third["id"])
    assert (t["round_of"], t["round_index"], t["recheck_of"]) == (third["id"], 1, second["id"])


def test_findings_of_low_severity_do_not_hold_a_review_back(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    monkeypatch.setenv("FAKE_ANSWER", finding("low"))
    rv = request(project)
    assert reviews.run_ticket(project, rv["id"])["verdict"] == "issues"
    assert scope.code_coverage(project, "E-0001", {"content_digest": "x"})["status"] == "covered"
    assert reviews.open_severity({"verdict": "issues", "findings": [{"impact": "resource_waste"}]}) == "medium"
    assert reviews.open_severity({"verdict": "issues", "findings": [{"impact": "nonblocking"}]}) == "low"
    assert reviews.open_severity({"verdict": "issues", "findings": [{"evidence": "?"}]}) == "medium"
    assert reviews.open_severity({"verdict": "blocking", "findings": []}) == "high"


def test_a_pi_reviewer_reports_its_session_model_and_cost(project, fakes, tmp_path, monkeypatch):
    store = configure(tmp_path, reviewer=["pi:openai/gpt-6-astra@high"])
    monkeypatch.setenv("FAKE_ARGS_OUT", str(tmp_path / "args.txt"))
    code(tmp_path)
    rv = request(store)
    out = reviews.run_ticket(store, rv["id"])
    assert out["verdict"] == "pass" and out["relation"] == "cross_model"
    with store.read() as tx:
        t = tx.require(rv["id"])
    assert (t["observed_model"], t["provider"], t["cost_usd"]) == ("gpt-6-astra", "openai", 0.01)
    assert t["host_session_id"] and "--thinking high --tools read" in (tmp_path / "args.txt").read_text()


def test_unavailable_reviewer_is_recorded_not_passed(project, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    code(tmp_path)
    rv = request(project)
    out = reviews.run_ticket(project, rv["id"])
    assert out["status"] == "unavailable" and "not a pass" in out["summary"]
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["status"] == "unavailable" and not t.get("verdict") and "not on PATH" in t["error"]


def test_no_configured_reviewer_means_unavailable_at_request(project, tmp_path):
    store = configure(tmp_path, reviewer=[])
    code(tmp_path)
    rv = request(store)
    with store.read() as tx:
        t = tx.require(rv["id"])
    assert t["status"] == "unavailable" and "models.reviewer is empty" in t["error"]


def test_the_package_names_the_files_and_the_reviewer_reads_them(project, fakes, tmp_path, monkeypatch):
    code(tmp_path, body="def operator(x):\n    return x * 3  # unique-marker\n")
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_ARGS_OUT", str(tmp_path / "args.txt"))
    rv = request(project)
    reviews.run_ticket(project, rv["id"])
    prompt = (tmp_path / "prompt.txt").read_text()
    assert f"- {tmp_path / 'src/model.py'}" in prompt and f"- {tmp_path / 'configs/run.yaml'}" in prompt
    assert "unique-marker" not in prompt and "lr: 0.1" not in prompt and "Method definition" in prompt
    assert "--sandbox" not in (tmp_path / "args.txt").read_text()  # Codex starts under the user's own settings


def test_an_answer_given_while_the_files_changed_does_not_count(project, fakes, tmp_path):
    code(tmp_path)
    rv = request(project)
    code(tmp_path, body="def f(x):\n    return x + 1\n")  # edited before the answer arrived
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and "src/model.py changed after the request" in out["summary"]
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["changed_during_review"] == ["src/model.py"] and not research.review_passes(t)
    coverage = scope.code_coverage(project, "E-0001", {"content_digest": "x"})
    assert coverage["status"] == "not_passed" and "the files changed during the review" in coverage["open"][0]
    assert request(project)["id"] != rv["id"]  # never reused
    again = request(project, recheck_of=rv["id"])
    with project.read() as tx:
        assert tx.require(again["id"])["round_index"] == 1  # it takes the place of the answer that did not count
    assert reviews.run_ticket(project, again["id"])["verdict"] == "pass"
    assert scope.code_coverage(project, "E-0001", {"content_digest": "x"})["status"] == "covered"


def test_a_reviewer_reads_the_files_in_a_worktree_outside_the_project(project, fakes, tmp_path, tmp_path_factory,
                                                                       monkeypatch):
    from smooth_research.project import Project
    from smooth_research.store import Store
    worktree = tmp_path_factory.mktemp("worktree")
    (worktree / "src").mkdir()
    (worktree / "src/model.py").write_text("def f(x):\n    return x\n")
    store = Store(Project(project.project.root, project.project.config, checkout=worktree), "test")
    rv = reviews.request_review(store, "E-0001", ["src/model.py"], "is it right?", ["claude-sonnet-5-5"])
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_ARGS_OUT", str(tmp_path / "args.txt"))
    monkeypatch.setenv("FAKE_ANSWER", PASS)
    assert reviews.run_ticket(project, rv["id"], only="claude:claude-opus-5-5")["verdict"] == "pass"
    assert f"--add-dir {worktree}" in (tmp_path / "args.txt").read_text()
    prompt = (tmp_path / "prompt.txt").read_text()
    assert f"- {worktree / 'src/model.py'}" in prompt and "the requester's worktree" in prompt

    (worktree / "src/model.py").write_text("def f(x):\n    return 2 * x\n")
    rv2 = reviews.request_review(store, "E-0001", ["src/model.py"], "is it right?", ["claude-sonnet-5-5"])
    reviews.run_ticket(project, rv2["id"], only="codex:gpt-6-astra")
    assert "--add-dir" not in (tmp_path / "args.txt").read_text()  # for Codex it would make the folder writable


def test_unparsed_answers_and_same_family_imports_are_labelled_honestly(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    monkeypatch.setenv("FAKE_ANSWER", "I could not read the package because the sandbox failed.")
    rv = request(project)
    out = reviews.run_ticket(project, rv["id"])
    assert out["status"] == "failed" and "not a pass" in out["summary"] and "sandbox failed" in out["summary"]
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["status"] == "failed" and not t.get("verdict") and not t.get("answer_ref")
    code(tmp_path, body="x = 1\n")
    rv2 = request(project)
    (tmp_path / "answer.md").write_text('```json\n{"verdict": "issues", "findings": [{"location": "a"}]}\n```')
    out = reviews.import_answer(project, rv2["id"], str(tmp_path / "answer.md"), "claude-opus-5-5", "subagent")
    assert out["relation"] == "same_family_independent_session" and out["verdict"] == "issues"


def test_an_imported_review_answer_is_final_too(project, fakes, tmp_path):
    code(tmp_path)
    rv = request(project)["id"]
    (tmp_path / "medium.md").write_text(finding("medium"))
    reviews.import_answer(project, rv, str(tmp_path / "medium.md"), "gpt-6-astra", "subagent")
    reviews.approve(project, rv, "accept only the original medium issue")
    (tmp_path / "blocking.md").write_text('```json\n{"verdict": "blocking", "findings": []}\n```')
    with pytest.raises(RLError) as exc:
        reviews.import_answer(project, rv, str(tmp_path / "blocking.md"), "gpt-6-astra", "subagent")
    assert exc.value.code == "CONFLICT" and f"--recheck-of {rv}" in exc.value.message
    with project.read() as tx:
        t = tx.require(rv)
    assert (t["verdict"], t["open_severity"], t.get("earlier_answers")) == ("issues", "medium", None)


PROPOSALS = 'Proposals.\n```json\n{"proposals": [{"title": "x"}, {"title": "y"}], "pause_recommended": false}\n```'


def test_an_advice_round_asks_every_advisor_on_its_own(project, fakes, monkeypatch):
    first = reviews.prepare_advice(project, "why no gain?", "H-0001")
    assert first["round"] == 1 and len(first["ids"]) == 2
    with project.read() as tx:
        tickets = [tx.require(i) for i in first["ids"]]
    assert [t["candidates"] for t in tickets] == [["codex:gpt-6-astra@max"], ["claude:claude-fable-5-1@high"]]
    assert tickets[0]["request_ref"] == tickets[1]["request_ref"]
    with project.read() as tx:
        package = documents.read(project.project, documents.resolve(tx, first["request_ref"]))
    assert "Does weight decay help" not in package  # the plan was the starting point; the records are the plan
    assert reviews.prepare_advice(project, "why no gain?", "H-0001")["duplicate"]
    monkeypatch.setenv("FAKE_ANSWER", PROPOSALS)
    reviews.run_many(project, first["ids"], timeout=60)
    with project.read() as tx:
        done = [tx.require(i) for i in first["ids"]]
    assert all(t["status"] == "answered" and t["summary"] == "2 proposal(s)" for t in done)
    assert {t["observed_model"] for t in done} == {"gpt-6-astra", "claude-fable-5-1"}
    research.add_evidence(project, "new negative result", "screening", "valid", challenges=["H-0001"])
    second = reviews.prepare_advice(project, "why no gain?", "H-0001")
    assert second["round"] == 2 and not second.get("duplicate")
    h = research.add_hypothesis(project, "advised route", "a different writer", derived_from=[first["ids"][0]])
    assert h["id"] == "H-0002"


def test_new_literature_makes_a_new_advice_round(project):
    reviews.prepare_advice(project, "is our direction novel?", "H-0001")
    paper = literature.add_paper(project, title="A checked, identical prior method", authors=["A. Author"],
                                 year=2025)["id"]
    literature.link_paper(project, "H-0001", paper, "nearest_work", "checked", locator="section 2")
    second = reviews.prepare_advice(project, "is our direction novel?", "H-0001")
    assert second["round"] == 2 and not second.get("duplicate")
    with project.read() as tx:
        assert paper in documents.read(project.project, documents.resolve(tx, second["request_ref"]))
    assert reviews.prepare_advice(project, "is our direction novel?", "H-0001")["duplicate"]


def test_advice_rounds_beyond_the_limit_need_the_users_approval(project, monkeypatch):
    for i in range(2):
        reviews.prepare_advice(project, f"question {i}")
    monkeypatch.setenv("PATH", "/nonexistent")
    unanswered = reviews.prepare_advice(project, "question 2")
    for aid in unanswered["ids"]:
        reviews.run_ticket(project, aid)  # no advisor could answer: the round does not count
    assert reviews.prepare_advice(project, "question 3")["round"] == 4
    with pytest.raises(RLError) as exc:
        reviews.prepare_advice(project, "question 4")
    assert exc.value.code == "LIMIT" and "3 of 3 advice rounds used" in exc.value.message
    out = reviews.prepare_advice(project, "question 4", user_approved="user: one more round is fine")
    assert out["round"] == 5 and "(4 of 3 used)" in out["summary"]
    with project.read() as tx:
        assert tx.require(out["ids"][1])["user_approved"] == "user: one more round is fine"


def test_manual_advisor_waits_for_an_imported_answer(project, tmp_path, monkeypatch):
    store = configure(tmp_path, advisor=["manual:gpt-pro"])
    monkeypatch.setenv("PATH", "/nonexistent")
    ad = reviews.prepare_advice(store, "which route?")
    out = reviews.run_ticket(store, ad["ids"][0])
    assert out["status"] == "pending" and "submit" in out["summary"]
    (tmp_path / "gpt.md").write_text('```json\n{"proposals": [], "pause_recommended": true}\n```')
    done = reviews.import_answer(store, ad["ids"][0], str(tmp_path / "gpt.md"), "gpt-pro", "ChatGPT Pro (manual)")
    assert "recommends pause" in done["summary"]
    (tmp_path / "gpt.md").write_text(PROPOSALS)
    assert "2 proposal(s)" in reviews.import_answer(store, ad["ids"][0], str(tmp_path / "gpt.md"), "gpt-pro",
                                                    "ChatGPT Pro (manual)")["summary"]  # an advisor may revise
    with store.read() as tx:
        assert len(tx.require(ad["ids"][0])["earlier_answers"]) == 1


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


def test_delegation_tries_the_researcher_entries_in_order(project, fakes, tmp_path, monkeypatch):
    store = configure(tmp_path, researcher=["manual:someone", "pi:openai/gpt-6-astra@high", "codex:gpt-6-astra@high"])
    monkeypatch.setenv("FAKE_ANSWER", "claimed and delivered")
    monkeypatch.setenv("FAKE_PI_FAIL", "1")
    tasks.create_task(store, "implement", "implement", ["E-0001"], to_role="researcher")
    out = delegate.delegate(store, "T-0001")
    assert out["ok"] and out["entry"] == "codex:gpt-6-astra@high"
    assert "manual:someone: a manual entry cannot run a task session" in out["summary"]
    assert "pi:openai/gpt-6-astra@high: quota exceeded" in out["summary"]
    with store.read() as tx:
        sessions = [(s["host"], s["model_requested"]) for s in tx.all("Session")]
    assert sessions == [("pi", "openai/gpt-6-astra"), ("codex", "gpt-6-astra")]


def evidence(project, accs=(0.8, 0.9)):
    """Two seeds of E-0001 and screening evidence that they support H-0001."""
    from conftest import SPEC, obs
    atts, results = [], []
    for seed, acc in enumerate(accs):
        out = runs.register(project, "E-0001", {**SPEC, "seed": seed}, f"r{seed}")
        runs.record(project, obs(attempt=out["attempt"], state="ended", outcome="success"))
        res = runs.import_result(project, out["attempt"], {"status": "complete", "metrics": {"acc": acc}}, f"res{seed}")
        atts.append(out["attempt"])
        results.append(res["result"])
    return research.add_evidence(project, "wd beats no wd on 2 seeds", "screening", "valid", "toy, 20% noise",
                                 supports=["H-0001"], attempts=atts, results=results, experiment="E-0001")["id"]


def test_a_key_conclusion_is_checked_against_the_records_behind_it(project, fakes, tmp_path, monkeypatch):
    ev = evidence(project)
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    claim = "H-0001 is supported at screening level only (unique-proposal)"
    rv = reviews.request_conclusion_review(project, ["H-0001"], claim, ["claude-opus-5-5"])
    with project.read() as tx:
        t = tx.require(rv["id"])
    assert t["subject"] == "conclusion" and t["candidates"][0] == "codex:gpt-6-astra@medium"
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and out["relation"] == "cross_model"
    prompt = (tmp_path / "prompt.txt").read_text()
    for part in ("unique-proposal", "weight decay raises test accuracy", f"{ev} supports H-0001", "arm wd seed 1",
                 '"acc": 0.9', "| wd | @1 | 0,1 |", "Code reviews: none recorded", "overclaim"):
        assert part in prompt, part
    assert reviews.request_conclusion_review(project, ["H-0001"], claim, ["claude-opus-5-5"])["reused_from"] == rv["id"]
    research.set_evidence(project, ev, "invalid", "metric bug")  # the records behind it changed: check again
    assert "reused_from" not in reviews.request_conclusion_review(project, ["H-0001"], claim, ["claude-opus-5-5"])


def test_a_conclusion_package_shows_the_nearest_work_of_the_reviewed_revision(project, fakes):
    asked = iter(range(10))

    def package():
        rv = reviews.request_conclusion_review(project, ["H-0001"], f"claim {next(asked)}", ["claude-opus-5-5"])["id"]
        return (project.project.research_root / "packages" / f"{rv}.request.md").read_text()

    gap = "Nearest work: none linked to this revision of the hypothesis, so its novelty and the closest prior " \
          "results are unchecked here"
    assert gap + "\n" in package()
    paper = literature.add_paper(project, title="Decay under noise", authors=["A. Author"], year=2024)["id"]
    literature.link_paper(project, "H-0001", paper, "nearest_work", "checked", locator="section 2",
                          note="same update rule, clean labels only")
    assert f"Nearest work (literature linked to this revision):\n- {paper} Decay under noise (2024) [checked] at " \
           "section 2: same update rule, clean labels only" in package()
    research.revise_hypothesis(project, "H-0001", "narrow", scope="toy, 20% noise only", base_revision=1)
    assert gap + " (1 link(s) to earlier revisions, not carried over)" in package()


def test_key_conclusions_without_a_passing_review_stay_in_attention(project, fakes, monkeypatch):
    from smooth_research import view
    ev = evidence(project)
    d1 = research.decide(project, ["H-0001"], "continue", "worth more seeds", [ev], set_evidence_status="supported")
    assert d1["conclusion_review_gap"] == "no conclusion review" and "rl review conclusion" in d1["summary"]
    with project.read() as tx:
        assert tx.require(d1["id"])["evidence_status_set"] == "supported"
        items = research.attention(tx)
    assert any(i.startswith(f"{d1['id']} key conclusion on H1: no conclusion review") for i in items)
    rv = reviews.request_conclusion_review(project, ["H-0001"], "supported at screening level", ["claude-opus-5-5"])
    reviews.run_ticket(project, rv["id"])
    research.link(project, d1["id"], "based_on", rv["id"])
    with project.read() as tx:
        assert not [i for i in research.attention(tx) if "key conclusion" in i]
    assert research.decide(project, ["H-0001"], "diagnose", "check the metric", [ev])["conclusion_review_gap"] is None
    monkeypatch.setenv("PATH", "/nonexistent")
    rv2 = reviews.request_conclusion_review(project, ["H-0001"], "retire H-0001", ["claude-opus-5-5"])
    reviews.run_ticket(project, rv2["id"])
    d3 = research.decide(project, ["H-0001"], "stop", "not worth more", [ev, rv2["id"]])
    assert d3["conclusion_review_gap"] == f"conclusion review {rv2['id']} is unavailable, not a pass"
    with project.read() as tx:
        items = [i for i in research.attention(tx) if "key conclusion" in i]
    assert len(items) == 1 and items[0].startswith(d3["id"])  # the standing conclusion on H-0001 is now d3
    text = open(view.report(project)["path"], encoding="utf-8").read()
    assert f"[conclusion review {rv['id']} pass (cross_model)]" in text and "is unavailable, not a pass]" in text


def test_delegation_continues_a_task_its_role_already_owns(project, fakes, monkeypatch):
    monkeypatch.setenv("FAKE_ANSWER", "continued")
    tasks.start_session(project, "coordinator", "claude", "sess-main")
    tasks.create_task(project, "run E-0001", "experiment", ["E-0001"], to_role="researcher")
    tasks.claim(project, "T-0001", "researcher-1")
    tasks.progress(project, "T-0001", "waiting", "researcher-1", next_action="import the results when the jobs end")
    prompt = delegate.prompt_for(project, "T-0001", "researcher-1")
    assert "task resume T-0001 --role researcher-1" in prompt and "task claim" not in prompt
    assert "coordinator: S-0001 (claude session sess-main)" in prompt
    assert delegate.delegate(project, "T-0001", "codex:gpt-6-astra@medium", role="researcher-1")["ok"]
    with pytest.raises(RLError) as err:
        delegate.delegate(project, "T-0001", "codex:gpt-6-astra@medium", role="researcher-2")
    assert err.value.code == "CONFLICT"


def test_a_run_records_whether_code_reviews_still_cover_its_code(project, fakes, tmp_path, rl):
    from conftest import SPEC
    from smooth_research import view
    code(tmp_path)

    def check(n, experiment="E-0001", **kw):
        out = runs.register(project, experiment, {**SPEC, "seed": n}, f"cov{n}", **kw)
        return out["code_review"], out["summary"]

    first, text = check(0)
    assert first == {"status": "none"} and "code review: not reviewed" in text and "--no-review" in text
    rv = request(project)["id"]
    assert check(1)[0] == {"status": "not_passed", "open": [f"{rv} pending"]}
    reviews.run_ticket(project, rv)
    covered, text = check(2)
    assert covered == {"status": "covered", "source": "working tree", "reviews": [f"{rv} pass"]}
    assert f"code review (working tree): covered by {rv} pass" in text
    code(tmp_path, config={"lr": 0.1, "seed": 9})  # only a key the review allowed to vary
    assert check(3)[0]["status"] == "covered"
    code(tmp_path, body="def f(x):\n    return 2 * x\n")  # the reviewed code changed
    stale, text = check(4)
    assert stale["status"] == "stale" and stale["changed"] == {"src/model.py": [rv]} and "--recheck-of" in text
    fixed = request(project, recheck_of=rv)["id"]
    reviews.run_ticket(project, fixed)
    covered = check(5)[0]
    assert covered["status"] == "covered" and f"{fixed} pass" in covered["reviews"]
    with project.read() as tx:
        att = tx.require("ATT-0006-1")
        assert att["code_review"] == covered and "code review: covered by" in runs.describe_attempt(tx, att)
    # the experiment whose code comes from E-0001 is covered by E-0001's reviews
    research.add_experiment(project, "WD, more noise", "does wd still help at 40%?", ["H-0001"], "E1",
                            code_from="E-0001", intervention="wd", comparator="no wd", metric="acc", scope="toy",
                            outcome_actions="a", budget="b", stop="c")
    assert check(6, "E-0002")[0]["status"] == "covered"
    code_, out, _ = rl("run", "register", "--experiment", "E-0001", "--request-id", "cov9", "--spec",
                       json.dumps({**SPEC, "seed": 9}), "--no-review", "smoke test")
    assert code_ == 0 and "code review: not needed: smoke test" in out
    with pytest.raises(RLError, match="not a code review"):
        runs.register(project, "E-0001", SPEC, "cov10",
                      reviews=[reviews.request_conclusion_review(project, ["H-0001"], "x", ["claude-opus-5-5"])["id"]])
    ev = evidence(project)
    report = open(view.report(project)["path"], encoding="utf-8").read()
    line = next(l for l in report.splitlines() if l.startswith(f"- {ev} "))
    assert "[code review: " in line and "covered by" in line, line


def test_a_run_from_a_commit_is_checked_against_that_commit(project, fakes, tmp_path):
    import subprocess
    from conftest import SPEC

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    code(tmp_path)
    git("init", "-q")
    git("add", "src", "configs")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "reviewed code")
    commit = git("rev-parse", "HEAD")
    rv = request(project)["id"]
    reviews.run_ticket(project, rv)
    code(tmp_path, body="def f(x):\n    return 3 * x\n")  # edited after the commit
    at_commit = runs.register(project, "E-0001", {**SPEC, "code_ref": {"commit": commit}}, "c1")["code_review"]
    assert at_commit == {"status": "covered", "source": f"commit {commit[:12]}", "reviews": [f"{rv} pass"]}
    assert runs.register(project, "E-0001", SPEC, "c2")["code_review"]["status"] == "stale"  # the working tree
    elsewhere = runs.register(project, "E-0001", {**SPEC, "code_ref": {"commit": "f" * 40}}, "c3")["code_review"]
    assert elsewhere["status"] == "unchecked" and "not in this repository" in elsewhere["reason"]


def test_a_review_against_a_base_commit_has_the_diff_beside_its_package(project, fakes, tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    code(tmp_path)
    git("init", "-q")
    git("add", "src", "configs")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    code(tmp_path, body="def f(x):\n    return 2 * x\n")
    rv = request(project, diff_base=base)["id"]
    packages = tmp_path / "research" / "packages"
    diff = (packages / f"{rv}.diff").read_text()
    assert "-    return x\n+    return 2 * x\n" in diff and "src/model.py" in diff
    assert f"## Diff against {base}\n\n{packages / f'{rv}.diff'}" in (packages / f"{rv}.request.md").read_text()


def another_experiment(project, title):
    return research.add_experiment(project, title, "q?", ["H-0001"], "E1", intervention="i", comparator="c",
                                   metric="acc", scope="s", outcome_actions="a", budget="b", stop="c")["id"]


def test_a_path_that_matches_no_passing_review_says_why(project, fakes, tmp_path, rl):
    from conftest import SPEC
    code(tmp_path)
    (tmp_path / "src/helper.py").write_text("H = 1\n")
    (tmp_path / "src/test_model.py").write_text("def test_f():\n    pass\n")  # a test by its name, in src/
    rv = reviews.request_review(project, "E-0001", ["src/model.py", "src/helper.py", "src/test_model.py"], "right?",
                                ["claude-sonnet-5-5"], configs=["configs/run.yaml"], ignore_keys=["seed"])["id"]
    reviews.run_ticket(project, rv)

    def check(n, experiment="E-0001", **kw):
        return runs.register(project, experiment, {**SPEC, "seed": n}, f"why{n}", **kw)

    (tmp_path / "src/test_model.py").write_text("def test_f():\n    assert True\n")
    (tmp_path / "src/helper.py").unlink()  # not in the code the run executes
    out = check(1)
    assert out["code_review"] == {"status": "covered", "source": "working tree", "reviews": [f"{rv} pass"],
                                  "absent": {"src/helper.py": [rv]}, "tests": {"src/test_model.py": [rv]}}
    assert f"\n  not in this code, so not executed: src/helper.py ({rv})\n" in out["summary"]
    assert f"\n  test file, changed: src/test_model.py ({rv}); a test does not change" in out["summary"]
    other = another_experiment(project, "another question")  # its code does not come from E-0001
    code(tmp_path, body="def f(x):\n    return 2 * x\n")
    seen = reviews.request_review(project, other, ["src/model.py"], "right?", ["claude-sonnet-5-5"])["id"]
    reviews.run_ticket(project, seen)
    stale = check(2)
    assert stale["code_review"]["status"] == "stale" and stale["code_review"]["changed"] == {"src/model.py": [rv]}
    assert stale["code_review"]["seen_by"] == {"src/model.py": [seen]}  # named, not counted
    assert f"\n  changed: src/model.py (since {rv})\n    as run here, it was seen by {seen}" in stale["summary"]
    code_, shown, _ = rl("run", "show", stale["attempt"])
    assert code_ == 0 and "code review: stale: 1 path(s) changed since a passing review saw them\n" in shown
    assert f"\n    changed: src/model.py (since {rv})\n" in shown and f"--review {seen} counts it" in shown
    assert f"\n    still matching: {rv} pass" in shown
    both = check(3, reviews=[seen])["code_review"]  # each saw a part: the combination was not reviewed
    assert both["status"] == "covered" and both["reviews"] == [f"{rv} pass", f"{seen} pass"] and both["per_file"]
    assert runs.code_review_note(both).startswith(f"covered by {rv} pass, {seen} pass (file by file); test files")
    tests_only = another_experiment(project, "the tests")
    only = reviews.request_review(project, tests_only, ["src/test_model.py"], "tests right?", ["claude-sonnet-5-5"])["id"]
    reviews.run_ticket(project, only)
    (tmp_path / "src/test_model.py").write_text("def test_f():\n    assert 1\n")
    none = check(4, tests_only)
    assert none["code_review"] == {"status": "none", "source": "working tree", "tests": {"src/test_model.py": [only]}}
    assert "code review (working tree): not reviewed: what the passing reviews saw is not in this code, or is tests. " \
           "A costly run" in none["summary"]


def test_the_reviews_that_did_not_pass_are_listed_apart_from_the_passing_ones(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    (tmp_path / "src/stage_b.py").write_text("B = 1\n")
    rv = request(project)["id"]
    reviews.run_ticket(project, rv)
    monkeypatch.setenv("FAKE_ANSWER", finding("medium"))
    b = reviews.request_review(project, "E-0001", ["src/stage_b.py"], "stage B right?", ["claude-sonnet-5-5"])["id"]
    reviews.run_ticket(project, b)
    checked = scope.code_coverage(project, "E-0001", {"content_digest": "x"})
    assert checked == {"status": "covered", "source": "working tree", "reviews": [f"{rv} pass"],
                       "not_passed": [f"{b} issues, medium open"]}
    assert runs.code_review_note(checked) == f"covered by {rv} pass; not passed: {b} issues, medium open"
    monkeypatch.setenv("FAKE_ANSWER", PASS)
    (tmp_path / "src/stage_b.py").write_text("B = 2\n")
    fixed = reviews.request_review(project, "E-0001", ["src/stage_b.py"], "stage B right?", ["claude-sonnet-5-5"],
                                   recheck_of=b)["id"]
    reviews.run_ticket(project, fixed)
    checked = scope.code_coverage(project, "E-0001", {"content_digest": "x"})  # the passing re-check follows it
    assert checked == {"status": "covered", "source": "working tree", "reviews": [f"{rv} pass", f"{fixed} pass"],
                       "per_file": True}


def test_a_round_its_recheck_resolved_leaves_attention_without_passing(project, fakes, tmp_path, monkeypatch, rl):
    code(tmp_path)
    monkeypatch.setenv("FAKE_ANSWER", finding("medium"))
    first = request(project)["id"]
    reviews.run_ticket(project, first)
    code(tmp_path, body="def f(x):\n    return 2 * x\n")
    second = request(project, recheck_of=first)["id"]
    assert reviews.run_ticket(project, second)["escalated"]
    monkeypatch.setenv("FAKE_ANSWER", PASS)
    third = reviews.request_review(project, "E-0001", ["src/model.py"], "is the fix right?", ["claude-sonnet-5-5"],
                                   recheck_of=second, new_round="the fix needs its own look")["id"]  # not the config
    reviews.run_ticket(project, third)
    with project.read() as tx:
        item = next(i for i in research.attention(tx) if i.startswith(second))
    assert item == (f"{second} review round {first} used its two reviews with medium findings open; {third} "
                    f"re-checked it and passed: when that resolved the findings, rl review resolve {second} --by "
                    f"{third} --reason ..; to accept findings that remain, rl review approve {second} --rationale ..")
    for args, why in (((second, third, " "), "--reason"), ((second, first, "r"), f"{first} is not a re-check of"),
                      ((first, second, "r"), f"{second} does not pass"), ((third, third, "r"), "nothing to resolve")):
        with pytest.raises(RLError, match=why):
            reviews.resolve(project, *args)
    monkeypatch.setenv("SMOOTH_RESEARCH_ROLE", "researcher-1")
    with pytest.raises(RLError, match="the coordinator's decision"):
        reviews.resolve(project, second, third, "r")
    monkeypatch.delenv("SMOOTH_RESEARCH_ROLE")
    code_, out, _ = rl("review", "resolve", second, "--by", third, "--reason", "the re-check looked at the fix")
    assert code_ == 0 and out.startswith(f"{second} resolved by {third}: the re-check looked at the fix. Its verdict "
                                         "stays (issues, medium open)")
    with project.read() as tx:
        assert not any(i.startswith(second) for i in research.attention(tx))
        assert research.review_mark(tx.require(second)) == f"issues, medium open, resolved by {third}"
    with pytest.raises(RLError) as err:
        reviews.resolve(project, second, third, "again")
    assert err.value.code == "CONFLICT"
    code(tmp_path, body="def f(x):\n    return 2 * x\n", config={"lr": 0.5, "seed": 0})  # only the old round saw it
    assert scope.code_coverage(project, "E-0001", {"content_digest": "x"}) == {
        "status": "covered", "source": "working tree", "reviews": [f"{third} pass"]}
    approved = reviews.approve(project, first, "the remaining finding is a documented choice")["summary"]
    assert f"\nnote: {third} re-checked it and passed. Approving accepts the findings that remain" in approved


DATED = "lr: 0.1\nseed: {seed}\nsnapshot: {day}\nstarted: 2026-10-01T08:00:00Z\n3: three\n"


def test_a_config_with_dates_and_number_keys_is_compared_like_any_other(project, fakes, tmp_path):
    from conftest import SPEC
    code(tmp_path)
    rv = request(project)
    (tmp_path / "configs/run.yaml").write_text(DATED.format(seed=0, day="2026-10-01"))  # before the answer came
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and "configs/run.yaml changed after the request" in out["summary"]
    dated = request(project)
    assert reviews.run_ticket(project, dated["id"])["verdict"] == "pass"
    (tmp_path / "configs/run.yaml").write_text(DATED.format(seed=7, day="2026-10-01"))  # only the ignored key
    assert request(project)["reused_from"] == dated["id"]
    assert runs.register(project, "E-0001", SPEC, "d1")["code_review"]["status"] == "covered"
    (tmp_path / "configs/run.yaml").write_text(DATED.format(seed=7, day="2026-10-02"))
    assert runs.register(project, "E-0001", {**SPEC, "seed": 1}, "d2")["code_review"]["status"] == "stale"


def test_one_process_at_a_time_answers_a_ticket(project, fakes, tmp_path):
    import subprocess
    import sys
    code(tmp_path)
    rv = request(project)["id"]

    def held_by(proc):
        with project.write() as tx:
            tx.update(rv, {"status": "running", "calls": [{"pid": proc.pid, "started_at": "2026-10-31T00:00:00Z"}]})

    sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
    worker = subprocess.Popen(sleep + [rv])  # a worker names its ticket on its command line
    try:
        held_by(worker)
        with pytest.raises(RLError) as exc:
            reviews.run_ticket(project, rv)
        assert exc.value.code == "CONFLICT" and f"being answered by process {worker.pid}" in exc.value.message
        assert f"being answered by process {worker.pid}" in reviews.start_background(project, rv, 60)["summary"]
        assert "worker not running" not in reviews.show(project, rv, "review")["summary"]
    finally:
        worker.kill()
        worker.wait()
    assert "worker not running: run again" in reviews.show(project, rv, "review")["summary"]
    other = subprocess.Popen(sleep)  # the pid now belongs to another program
    try:
        held_by(other)
        assert reviews.run_ticket(project, rv)["verdict"] == "pass"
    finally:
        other.kill()
        other.wait()
    background = reviews.start_background(project, rv, 60)
    assert "already answered" in background["summary"] and "pid" not in background
    with project.read() as tx:
        t = tx.require(rv)
    assert t["status"] == "answered" and [c for c in t["calls"] if "pid" in c] == []


def test_a_background_run_keeps_the_entry_it_was_given(project, fakes, tmp_path, monkeypatch, rl):
    import subprocess
    from types import SimpleNamespace
    code(tmp_path)
    tickets = {"review": request(project)["id"], "advise": reviews.prepare_advice(project, "which route?")["ids"][0]}
    for kind, tid in tickets.items():
        started = []
        with monkeypatch.context() as m:
            m.setattr(subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or SimpleNamespace(pid=4242))
            assert rl(kind, "run", tid, "--background", "--entry", "manual:chosen-model")[0] == 0
        worker = started[0][3:]  # the worker's rl arguments, without the interpreter
        assert worker[-2:] == ["--entry", "manual:chosen-model"]
        assert "to chosen-model yourself" in rl(*worker)[1]  # not to the configured reviewer or advisor


def test_a_request_whose_package_cannot_be_sealed_leaves_no_ticket(project, fakes, tmp_path, monkeypatch):
    code(tmp_path)
    sealed = documents.prepare

    def disk_full(project_, path, doc_type):
        if doc_type.endswith("_request"):
            raise OSError("disk full")
        return sealed(project_, path, doc_type)

    monkeypatch.setattr(documents, "prepare", disk_full)
    for ask in (lambda: request(project), lambda: reviews.prepare_advice(project, "which route?"),
                lambda: reviews.request_conclusion_review(project, ["H-0001"], "WD helps", ["claude-sonnet-5-5"])):
        with pytest.raises(OSError):
            ask()
    with project.read() as tx:
        assert tx.all("Ticket") == []
    monkeypatch.setattr(documents, "prepare", sealed)
    assert reviews.prepare_advice(project, "which route?")["round"] == 1  # the failed round neither counts nor blocks
    assert request(project)["id"] == "RV-0001"


def test_a_code_review_of_an_experiment_seals_its_contract(project, fakes, tmp_path):
    code(tmp_path)
    rv = request(project)
    with project.read() as tx:
        exp, edge = tx.require("E-0001"), tx.out(rv["id"], "targets")[0]
        package = documents.read(project.project, documents.resolve(tx, rv["request_ref"]))
    assert (exp["revision"], edge["revision"], edge["contract_ref"]) == (1, 1, exp["contract_ref"])
    assert rv["summary"].endswith(f"; contract E-0001@1 ({exp['contract_ref']}) sealed now")
    assert f"(contract {exp['contract_ref']}: research/experiments/E-0001.md)" in package


def test_a_conclusion_review_counts_for_the_revision_it_reviewed_and_points_out_later_evidence(project, fakes):
    ev = evidence(project)
    rv = reviews.request_conclusion_review(project, ["H-0001"], "H-0001 is supported", ["claude-opus-5-5"])["id"]
    reviews.run_ticket(project, rv)
    first = research.decide(project, ["H-0001"], "continue", "x", [ev, rv], set_evidence_status="supported")
    assert first["conclusion_review_gap"] is None and first["review_hints"] == []
    later = research.add_evidence(project, "a third seed agrees", "screening", "valid", supports=["H-0001"],
                                  experiment="E-0001")["id"]
    second = research.decide(project, ["H-0001"], "continue", "y", [ev, later, rv], set_evidence_status="supported")
    hint = f"{later} was linked to H-0001 after {rv} reviewed it: check that it does not change the conclusion"
    assert second["conclusion_review_gap"] is None and second["review_hints"] == [hint]
    assert f"\nnote: {hint}" in second["summary"]
    with project.read() as tx:
        assert f"{second['id']} key conclusion: {later} was linked to H1 after {rv} reviewed it" in \
            "\n".join(research.attention(tx))
    research.revise_hypothesis(project, "H-0001", "sharper", statement="weight decay raises test accuracy under "
                                                                        "20% and 40% noise")
    third = research.decide(project, ["H-0001"], "continue", "z", [ev, later, rv], set_evidence_status="supported")
    assert third["conclusion_review_gap"] == (
        f"{rv} was of H-0001@1, the conclusion is on @2: have the conclusion reviewed at this revision, or state in "
        "the decision's rationale why the revision does not change it")
    with project.read() as tx:
        items = research.attention(tx)
        checks = view.conclusion_checks(tx, [tx.require(d["id"]) for d in (first, third)])
    assert [i for i in items if "key conclusion" in i] == [
        f"{third['id']} key conclusion on H1: {rv} was of H1@1, the conclusion is on @2: have the conclusion reviewed "
        "at this revision, or state in the decision's rationale why the revision does not change it — rl review "
        f"conclusion --target H1 --proposal .. --author-model .., then rl link {third['id']} based_on RV-x"]
    assert checks[first["id"]].startswith(f"conclusion review {rv} pass")  # it was of the revision decided on
    assert checks[third["id"]].startswith(f"{rv} was of H-0001@1")


def test_a_conclusion_review_covers_only_the_hypotheses_it_reviewed(project, fakes):
    research.add_hypothesis(project, "WD helps more", "weight decay helps more under 40% noise", "toy")
    ev = evidence(project)
    rv = reviews.request_conclusion_review(project, ["H-0001"], "H-0001 is supported", ["claude-opus-5-5"])["id"]
    reviews.run_ticket(project, rv)
    other = research.decide(project, ["H-0002"], "continue", "x", [ev, rv], set_evidence_status="supported")
    assert other["conclusion_review_gap"] == f"no passing conclusion review covers H-0002 ({rv} reviewed H-0001)"
    both = research.decide(project, ["H-0001", "H-0002"], "continue", "y", [ev, rv], set_evidence_status="supported")
    assert both["conclusion_review_gap"] == other["conclusion_review_gap"]
    with project.read() as tx:
        items = [i for i in research.attention(tx) if "key conclusion" in i]
    assert len(items) == 1 and items[0].startswith(f"{both['id']} key conclusion on H1, H2: no passing")
    assert research.decide(project, ["H-0001"], "continue", "z", [ev, rv],
                           set_evidence_status="supported")["conclusion_review_gap"] is None
