"""Review and advice tickets through fake host CLIs: family rule, fallbacks, reuse, honesty when unavailable."""

import json
import os
import stat

import pytest
import yaml

from smooth_research import adapters, delegate, research, reviews, runs, tasks
from smooth_research.util import RLError

FAKE_CODEX = r"""#!{python}
import json, os, sys, uuid, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
if os.environ.get("FAKE_PROMPT_OUT"):
    pathlib.Path(os.environ["FAKE_PROMPT_OUT"]).write_text(prompt)
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


def test_codex_written_code_goes_to_a_claude_reviewer(project, fakes, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ANSWER", '```json\n{"verdict": "issues", "findings": [{"location": "src/model.py:2"}]}\n```')
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


def test_the_prompt_carries_the_package_with_the_code_inline(project, fakes, tmp_path, monkeypatch):
    code(tmp_path, body="def operator(x):\n    return x * 3  # unique-marker\n")
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    rv = request(project)
    reviews.run_ticket(project, rv["id"])
    prompt = (tmp_path / "prompt.txt").read_text()
    assert "unique-marker" in prompt and "lr: 0.1" in prompt and "Method definition" in prompt


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
    assert t["subject"] == "conclusion" and t["candidates"] == ["codex:gpt-6-astra@medium"]
    out = reviews.run_ticket(project, rv["id"])
    assert out["verdict"] == "pass" and out["relation"] == "cross_model"
    prompt = (tmp_path / "prompt.txt").read_text()
    for part in ("unique-proposal", "weight decay raises test accuracy", f"{ev} supports H-0001", "arm wd seed 1",
                 '"acc": 0.9', "| wd | 0,1 |", "Code reviews: none recorded", "overclaim"):
        assert part in prompt, part
    assert reviews.request_conclusion_review(project, ["H-0001"], claim, ["claude-opus-5-5"])["reused_from"] == rv["id"]
    research.set_evidence(project, ev, "invalid", "metric bug")  # the records behind it changed: check again
    assert "reused_from" not in reviews.request_conclusion_review(project, ["H-0001"], claim, ["claude-opus-5-5"])


def test_key_conclusions_without_a_passing_review_stay_in_attention(project, fakes, monkeypatch):
    from smooth_research import view
    ev = evidence(project)
    d1 = research.decide(project, ["H-0001"], "continue", "worth more seeds", [ev], set_evidence_status="supported")
    assert d1["conclusion_review_gap"] == "no conclusion review" and "rl review conclusion" in d1["summary"]
    with project.read() as tx:
        assert tx.require(d1["id"])["evidence_status_set"] == "supported"
        items = research.attention(tx)
    assert any(i.startswith(f"{d1['id']} key conclusion on H-0001: no conclusion review") for i in items)
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
    with project.write() as tx:  # reviews requested before scopes were recorded
        for rid in (rv, fixed):
            tx.update(rid, {"code_scope": None})
    old = check(7)[0]
    assert old["status"] == "unchecked" and "recorded no scope" in old["reason"]
    assert request(project)["reused_from"] == fixed  # same scope: the answer is reused and the scope recorded
    assert check(8)[0]["status"] == "covered"
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
