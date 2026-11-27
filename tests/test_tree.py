"""The hypothesis tree drafted by the coordinator's subagent and recorded once the user approves (design 2026-11-27,
7.2), and the hypothesis, experiment and decision commands that follow the tree: no tree task, no composite
hypotheses, process rules as flags (5.1, 5.2)."""

from __future__ import annotations

import pytest

from smooth_research import tree
from smooth_research.store import Store

C = "coordinator"

DRAFT = """\
why: >-
  The plan makes one claim, that averaging lowers the error; seed luck is the one competing explanation worth
  testing beside it.
hypotheses:
  - key: main
    kind: main
    title: Averaging helps
    statement: >-
      The mean of three noisy estimates has a smaller absolute error than one estimate; supported if lower over
      300 draws.
    scope: N(5,1)
    purpose: whether averaging is worth it
    source: docs/plan.md § Averaging
    cost: about 0.1 cpu·h
  - key: tails
    kind: main
    parent: main
    title: Averaging helps in the tails too
    statement: The 95th percentile of the error is lower with the mean of three.
    scope: N(5,1)
    purpose: whether the gain holds for bad draws
    source: docs/plan.md § Averaging
    cost: about 0.1 cpu·h
  - key: luck
    kind: competing
    title: The gain is seed luck
    statement: With other seeds the single estimate is as good.
    scope: N(5,1)
    purpose: whether the result is real
    source: docs/plan.md § Averaging
    cost: about 0.05 cpu·h
    doubt:
      lack: novelty
      basis: a textbook result (the variance of a mean)
      options: drop | go ahead
prep:
  - title: Simulator
    requirement: src/sim.py with error(arm, seed, n)
    done_check: python -c "print('ok')"
questions:
  - Are 300 draws enough?
"""


def ok(result) -> str:
    code, out, err = result
    assert code == 0, f"exit {code}: {err or out}"
    return out


def write_draft(store: Store, text: str = DRAFT) -> None:
    path = tree.draft_path(store.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def records(store: Store, label: str) -> list[dict]:
    with store.read() as tx:
        return tx.all(label)


# -- prompt ---------------------------------------------------------------------------------------------------


def test_the_prompt_gives_the_rules_the_documents_the_tree_and_what_to_hand_back(project, rl):
    out = ok(rl("tree", "prompt", who=C))
    for text in ("What is a hypothesis", "is work under it", "docs/plan.md (read it whole)", "docs/constraints.md",
                 "No hypotheses are recorded yet", "research/tree-draft/tree.yaml", "rl tree check", "## Hand back",
                 "doubt", "prep:", "questions:"):
        assert text in out, text
    assert "A draft already exists" not in out


def test_the_prompt_reads_other_documents_and_says_which_are_missing(project, rl):
    (project.project.root / "notes.md").write_text("# More\n", encoding="utf-8")
    out = ok(rl("tree", "prompt", "--from", "notes.md", "gone.md", who=C))
    assert "- notes.md (read it whole)" in out and "docs/plan.md (read it whole)" not in out
    assert "warning: gone.md does not exist" in out


def test_with_a_draft_the_prompt_says_to_change_it_as_the_user_said(project, rl):
    write_draft(project)
    assert tree.has_draft(project.project)
    out = ok(rl("tree", "prompt", who=C))
    assert "A draft already exists" in out and "leave everything they did not mention" in out


# -- check ----------------------------------------------------------------------------------------------------


def test_check_passes_a_good_draft_and_records_nothing(project, rl):
    write_draft(project)
    out = ok(rl("tree", "check", who=C))
    assert "no problems" in out and "3 hypotheses (1 with a doubt)" in out and "1 shared preparation" in out
    assert "no cost" not in out and "warning" not in out
    assert records(project, "Hypothesis") == [] and tree.has_draft(project.project)


def test_check_reports_every_problem_and_records_nothing(project, rl):
    write_draft(project, """\
why: ''
hypotheses:
  - key: H1
    kind: side
    title: x
    purpose: p
    parent: nowhere
    colour: red
    doubt: {lack: taste, basis: b}
prep:
  - title: t
extra: 1
""")
    code, _, err = rl("tree", "check", who=C)
    assert code == 1
    for text in ("why: missing", "looks like a recorded hypothesis", "kind: main or competing", "statement: missing",
                 "'colour' is not a field", "doubt.lack: novelty or feasibility", "doubt.options: missing",
                 "prep[1].requirement: missing", "prep[1].done_check: missing", "'extra' is not a field",
                 "'nowhere' is neither"):
        assert text in err, text
    assert "no cost" in err  # warnings come along
    assert records(project, "Hypothesis") == []


def test_check_without_a_draft_or_with_broken_yaml(project, rl):
    code, _, err = rl("tree", "check", who=C)
    assert code == 1 and "no draft" in err
    write_draft(project, "why: [unclosed\n")
    code, _, err = rl("tree", "check", who=C)
    assert code == 1 and "does not parse as YAML" in err


def test_the_draft_names_its_parents_among_its_entries_without_a_circle(project, rl):
    write_draft(project, DRAFT.replace("    title: Averaging helps\n", "    parent: tails\n    title: Averaging helps\n"))
    code, _, err = rl("tree", "check", who=C)
    assert code == 1 and "go round in a circle" in err


# -- record ---------------------------------------------------------------------------------------------------


def test_record_writes_the_tree_its_decision_prep_and_the_approval_in_one_go(project, rl):
    write_draft(project)
    out = ok(rl("tree", "record", "--approval", "Looks good, go ahead", who=C))
    hyps = {h["title"]: h for h in records(project, "Hypothesis")}
    main, tails, luck = hyps["Averaging helps"], hyps["Averaging helps in the tails too"], hyps["The gain is seed luck"]
    assert (main["label"], tails["label"], luck["label"]) == ("H1", "H1.1", "H2")
    assert (main["disposition"], tails["disposition"], luck["disposition"]) == ("active", "active", "candidate")
    assert (main["kind"], luck["kind"]) == ("main", "competing")
    assert main["source"] == "docs/plan.md § Averaging" and main["cost"] == "about 0.1 cpu·h"
    assert luck["flag"]["lack"] == "novelty" and luck["flag"]["options"] == "drop | go ahead"
    assert not main.get("flag") and not main.get("flags")
    [dec] = records(project, "Decision")
    assert dec["kind"] == "route" and dec["state"] == "final" and "averaging lowers the error" in dec["rationale"]
    assert main["origin"] == dec["id"]
    with project.read() as tx:
        assert {e["id"] for e in tx.out(dec["id"], "decides")} == set(h["id"] for h in hyps.values())
        assert [e["id"] for e in tx.out(tails["id"], "derived_from") if e.get("primary")] == [main["id"]]
    [prep] = records(project, "Task")
    assert (prep["kind"], prep["state"], prep["creator"]) == ("prep", "queued", "coordinator")
    assert prep["requirement"].startswith("src/sim.py") and prep["done_check"] == "python -c \"print('ok')\""
    [ask] = records(project, "Ask")
    assert (ask["kind"], ask["state"], ask["answered_by"], ask["choice"]) == ("approval", "answered", "user", "approve")
    assert ask["answer"] == "Looks good, go ahead" and ask["decision_id"] == dec["id"] and not ask.get("task_id")
    assert not tree.draft_dir(project.project).exists()
    assert "flag:" not in out
    assert "next: make a hypothesis task" in out and f"rl task release {prep['id']}" in out
    assert "usually release one hypothesis task" in out and "rl hypothesis keep|drop" in out


def test_record_without_the_approval_records_all_the_same_flagged_unapproved(project, rl):
    write_draft(project)
    out = ok(rl("tree", "record", who=C))
    assert "flag: [unapproved]" in out
    [dec] = records(project, "Decision")
    assert [f["code"] for f in dec["flags"]] == ["unapproved"]
    assert all([f["code"] for f in h["flags"]] == ["unapproved"] for h in records(project, "Hypothesis"))
    assert records(project, "Ask") == []


def test_record_by_another_session_is_flagged_not_refused(project, rl):
    write_draft(project)
    out = ok(rl("tree", "record", "--approval", "ok", who="scientist-T-0009"))
    assert "flag: [role]" in out
    [dec] = records(project, "Decision")
    assert [f["code"] for f in dec["flags"]] == ["role"]
    assert records(project, "Task")[0]["creator"] == "coordinator"  # a public prep task all the same


def test_a_refused_record_writes_nothing_and_keeps_the_draft(project, rl):
    write_draft(project, DRAFT.replace("    statement: With other seeds the single estimate is as good.\n", ""))
    code, _, err = rl("tree", "record", "--approval", "ok", who=C)
    assert code == 1 and "statement: missing" in err
    assert records(project, "Hypothesis") == [] and records(project, "Decision") == []
    assert tree.has_draft(project.project)


def test_a_new_document_hangs_new_hypotheses_under_recorded_ones(project, rl):
    write_draft(project)
    ok(rl("tree", "record", "--approval", "ok", who=C))
    out = ok(rl("tree", "prompt", "--from", "docs/plan.md", who=C))
    assert "H1 H-0001 [active] Averaging helps" in out and "- H1 (H-0001): The mean of three" in out
    write_draft(project, """\
why: The new note asks about medians.
hypotheses:
  - key: median
    kind: main
    parent: H1
    title: The median helps more
    statement: The median of three beats the mean of three.
    scope: N(5,1)
    purpose: which estimator to use
    source: docs/plan.md § Averaging
    cost: about 0.1 cpu·h
""")
    out = ok(rl("tree", "record", "--approval", "yes", who=C))
    median = next(h for h in records(project, "Hypothesis") if h["title"] == "The median helps more")
    assert median["label"] == "H1.2"
    assert "prep" not in out.split("next:")[0]


def test_rl_tree_prints_the_tree_and_points_at_an_unrecorded_draft(project, rl):
    assert "no hypotheses yet" in ok(rl("tree"))
    write_draft(project)
    assert "an unrecorded draft is in research/tree-draft/tree.yaml" in ok(rl("tree"))


def test_read_only_runs_prompt_and_check_but_not_record(project, rl):
    write_draft(project)
    assert ok(rl("--read-only", "tree", "prompt"))
    assert "no problems" in ok(rl("--read-only", "tree", "check"))
    code, _, err = rl("--read-only", "tree", "record", who=C)
    assert code == 1 and "READ_ONLY" in err


def test_the_research_page_draws_the_draft_as_a_draft(project, rl):
    write_draft(project)
    ok(rl("view"))
    page = (project.project.root / "docs" / "research.html").read_text(encoding="utf-8")
    assert "建树草稿" in page and "草稿，还没有记录" in page and "The gain is seed luck" in page
    assert "新颖性不足" in page and "Are 300 draws enough?" in page and "Simulator" in page
    assert "rl view 刷新" in page and "rl resume" not in page
    ok(rl("tree", "record", "--approval", "ok", who=C))
    ok(rl("view"))
    assert "建树草稿" not in (project.project.root / "docs" / "research.html").read_text(encoding="utf-8")


def test_a_broken_draft_is_still_shown_on_the_page(project, rl):
    write_draft(project, "why: [unclosed\n")
    ok(rl("view"))
    page = (project.project.root / "docs" / "research.html").read_text(encoding="utf-8")
    assert "建树草稿" in page and "does not parse as YAML" in page and "why: [unclosed" in page


def test_source_parts_finds_the_document_of_a_hypothesis(project):
    p = project.project
    assert tree.source_parts(p, "docs/plan.md § Averaging") == ("docs/plan.md", "Averaging")
    assert tree.source_parts(p, "Averaging") == ("docs/plan.md", "Averaging")  # an older record: a plan heading
    assert tree.source_parts(p, "docs/plan.md") == ("docs/plan.md", None)
    assert tree.source_parts(p, None) == (None, None)


# -- hypotheses, experiments and decisions after the tree --------------------------------------------------------


def test_any_session_adds_a_hypothesis_without_a_task(project, rl):
    out = ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    assert "active" in out and "flag:" not in out
    out = ok(rl("hypothesis", "add", "--title", "B", "--statement", "s", "--purpose", "p", "--parent", "H1",
                who="scientist-T-0007"))
    assert "candidate" in out and "flag:" not in out
    out = ok(rl("hypothesis", "add", "--title", "C", "--statement", "s", "--purpose", "p", who="researcher-T-0002"))
    assert "flag: [role]" in out
    hyps = {h["title"]: h for h in records(project, "Hypothesis")}
    assert hyps["B"]["label"] == "H1.1" and not hyps["A"].get("source")
    assert [f["code"] for f in hyps["C"]["flags"]] == ["role"]


def test_composite_is_no_longer_an_option(project, rl):
    with pytest.raises(SystemExit):
        rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", "--composite", who=C)


def test_an_experiment_needs_no_route_decision(project, rl):
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    out = ok(rl("experiment", "add", "--title", "E", "--question", "q", "--hypotheses", "H1", who="scientist-T-0001"))
    assert "EH1/1" in out and "flag:" not in out
    ok(rl("decide", "--kind", "verdict", "--targets", "H1", "--status", "supported", "--rationale", "r", "--did", "d",
          "--means", "m", who="scientist-T-0001"))
    ok(rl("experiment", "add", "--title", "F", "--question", "q", "--hypotheses", "H1", "--motivated-by", "D-0001",
          who="scientist-T-0001"))
    with project.read() as tx:
        assert [e["id"] for e in tx.out("E-0002", "motivated_by")] == ["D-0001"]


def test_finalize_without_review_or_evidence_is_final_and_flagged(project, rl):
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    ok(rl("decide", "--kind", "verdict", "--targets", "H1", "--status", "supported", "--rationale", "r", "--did", "d",
          "--means", "m", who="scientist-T-0001"))
    out = ok(rl("decide", "--final", "D-0001", who="scientist-T-0001"))
    assert "flag: [unreviewed]" in out and "flag: [no-evidence]" in out
    [h] = records(project, "Hypothesis")
    [d] = records(project, "Decision")
    assert h["evidence_status"] == "supported" and d["state"] == "final"
    assert {f["code"] for f in d["flags"]} == {"unreviewed", "no-evidence"}


def test_a_stop_without_its_basis_is_final_and_flagged(project, rl):
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    ok(rl("decide", "--kind", "stop", "--targets", "H1", "--status", "paused", "--rationale", "r",
          who="scientist-T-0001"))
    out = ok(rl("decide", "--final", "D-0001", who="scientist-T-0001"))
    assert "flag: [no-basis]" in out and records(project, "Hypothesis")[0]["disposition"] == "paused"


def test_a_verdict_written_for_another_statement_is_still_refused(project, rl):
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    ok(rl("decide", "--kind", "verdict", "--targets", "H1", "--status", "supported", "--rationale", "r", "--did", "d",
          "--means", "m", who="scientist-T-0001"))
    ok(rl("hypothesis", "revise", "H1", "--statement", "s2", "--reason", "sharper", who="scientist-T-0001"))
    code, _, err = rl("decide", "--final", "D-0001", who="scientist-T-0001")
    assert code == 1 and "another statement" in err
    assert records(project, "Hypothesis")[0]["evidence_status"] == "untested"


def test_a_decided_hypothesis_keeps_its_statement(project, rl):
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    ok(rl("decide", "--kind", "verdict", "--targets", "H1", "--status", "supported", "--rationale", "r", "--did", "d",
          "--means", "m", who="scientist-T-0001"))
    ok(rl("decide", "--final", "D-0001", who="scientist-T-0001"))
    code, _, err = rl("hypothesis", "revise", "H1", "--statement", "s2", "--reason", "x", who=C)
    assert code == 1 and "keeps its statement" in err


def test_the_coordinator_writes_a_route_decision_unflagged(project, rl):
    ok(rl("hypothesis", "add", "--title", "A", "--statement", "s", "--purpose", "p", who=C))
    out = ok(rl("decide", "--kind", "route", "--targets", "H1", "--rationale", "scale up next", who=C))
    assert "flag:" not in out
    out = ok(rl("decide", "--kind", "verdict", "--targets", "H1", "--status", "supported", "--rationale", "r",
                "--did", "d", "--means", "m", who=C))
    assert "flag: [role]" in out and "next: rl review request D-0002 --kind conclusion" in out
