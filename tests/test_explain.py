"""The research page docs/research.html: the facts from the records, the words from research/notes/explanation.yaml,
and the basis that tells which explanations may be out of date; and the arm tables it shares with rl report."""

import re

import pytest
import yaml

from conftest import SPEC, obs
from smooth_research import explain, messages, research, runs, view
from smooth_research.util import RLError

PAGE = "docs/research.html"
WORDS = "research/notes/explanation.yaml"
LATER = "2026-10-30T01:00:00+00:00"


def words(tmp_path, data):
    (tmp_path / WORDS).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def page(tmp_path):
    return (tmp_path / PAGE).read_text(encoding="utf-8")


def ran(store, request_id, spec=SPEC, exp="E-0001", run=None):
    """A registered attempt that ended successfully."""
    att = runs.register(store, exp, dict(spec), request_id, run)["attempt"]
    runs.record(store, obs(attempt=att, state="ended", outcome="success", at=LATER))
    return att


def result(store, att, metrics, request_id, status="complete", **fields):
    return runs.import_result(store, att, {"status": status, "metrics": metrics, **fields}, request_id)["result"]


def rows(store, exp="E-0001"):
    with store.read() as tx:
        return view.arm_rows(tx, store, tx.require(exp))


def test_the_page_follows_the_commands_and_is_written_only_when_it_changes(project, tmp_path, rl):
    code, out, _ = rl("view")
    assert code == 0 and out.startswith("wrote docs/research.html: 1 hypotheses and 1 experiments, 0 research line(s)")
    assert "0 current, 3 to write, 0 may be out of date" in out
    text = page(tmp_path)
    assert "<b>1</b> hypotheses: 1 untested; 1 active" in text and "<b>1</b> experiments: 1 E1" in text
    assert "data_version" not in text  # heartbeats and messages change it: the page would never stay the same
    stamp = (tmp_path / PAGE).stat().st_mtime_ns
    code, out, _ = rl("view")
    assert out.startswith("unchanged: docs/research.html") and (tmp_path / PAGE).stat().st_mtime_ns == stamp
    code, _, _ = rl("hypothesis", "revise", "H1", "--reason", "same", "--title", "WD helps")  # changes nothing shown
    assert code == 0 and (tmp_path / PAGE).stat().st_mtime_ns == stamp
    code, _, _ = rl("hypothesis", "add", "--title", "Second", "--statement", "s2", "--derived-from", "H1")
    text = page(tmp_path)
    assert '<li class="node h" id="H-0002">' in text and "<b>2</b> hypotheses: 2 untested; 2 active" in text
    code, _, _ = rl("hypothesis", "set", "H1", "--reason", "seen", "--evidence-status", "supported")
    assert code == 0 and "<b>2</b> hypotheses: 1 untested, 1 supported; 2 active" in page(tmp_path)
    att = ran(project, "r1")
    assert "<b>0</b> runs" in page(tmp_path)  # runs and observations are frequent: the next render shows them
    code, _, _ = rl("run", "import-result", "--attempt", att, "--manifest", '{"status": "complete", "metrics": '
                    '{"acc": 0.9}}', "--request-id", "res1")
    text = page(tmp_path)
    assert code == 0 and "<b>1</b> runs, <b>1</b> attempts: 1 ended/success" in text
    assert "<td>0.9 (n=1)</td>" in text


def test_a_render_that_fails_after_a_command_is_only_a_warning(project, tmp_path, rl, monkeypatch):
    rl("view")

    def broken(store):
        raise RuntimeError("boom")
    monkeypatch.setattr(explain, "render", broken)
    code, out, _ = rl("hypothesis", "add", "--title", "Second", "--statement", "s2")
    assert code == 0 and out.startswith("H2@1 active: Second")
    assert "warning: docs/research.html was not rendered again (RuntimeError: boom); run rl view" in out


def test_explanations_are_to_write_then_current_until_their_facts_change(project, tmp_path, rl):
    att = runs.register(project, "E-0001", dict(SPEC), "r1")["attempt"]
    runs.record(project, obs(attempt=att, state="running"))
    rl("view")
    assert page(tmp_path).count('<span class="mark todo">explanation to write</span>') == 2
    words(tmp_path, {"summary": {"text": "Weight decay looks worth a second seed."},
                     "nodes": {"H1": {"title": "Weight decay", "purpose": "p", "did": "d", "result": "r"},
                               "E-0001": {"purpose": "a smoke test"}}})
    code, out, _ = rl("view")
    assert "0 current, 0 to write, 3 may be out of date" in out  # written, not stamped yet
    code, out, _ = rl("view", "--todo")
    assert "H1 (H-0001) written but not stamped: WD helps" in out and "summary: may be out of date" not in out
    code, out, _ = rl("view", "--stamp", "H1,EH1/1,summary")
    assert out.startswith("stamped H1, EH1/1, summary in research/notes/explanation.basis.yaml\nwrote docs/")
    assert "3 current, 0 to write, 0 may be out of date" in out
    assert yaml.safe_load((tmp_path / "research/notes/explanation.basis.yaml").read_text())["nodes"].keys() == \
        {"H-0001", "E-0001"}
    assert (tmp_path / WORDS).read_text().startswith("summary:")  # rl never writes the words
    # heartbeats, messages and links that the explanations do not rest on leave them current
    runs.record(project, obs(attempt=att, state="running", at=LATER))
    messages.send(project, "NOTE", "coordinator", "researcher-1", "hello")
    research.add_hypothesis(project, "Other", "other holds")
    code, out, _ = rl("view")
    assert "3 current, 1 to write, 0 may be out of date" in out
    # what they rest on: the attempt ended, new evidence, a stage decision
    runs.record(project, obs(attempt=att, state="ended", outcome="success", at="2026-10-30T02:00:00+00:00"))
    research.add_evidence(project, "wd helps", "screening", supports=["H-0001"], experiment="E-0001")
    research.decide(project, ["H-0001"], "continue_probe", "a second seed", kind="stage",
                    implementation_ready="no", evidence_scope="toy")
    code, out, _ = rl("view")
    assert "0 current, 1 to write, 3 may be out of date" in out
    text = page(tmp_path)
    assert text.count('<span class="mark stale">may be out of date</span>') == 2
    assert "the summary may be out of date: stage decision D-0001 came after the one it was stamped with" in text
    code, out, _ = rl("view", "--todo")
    assert "H1 (H-0001) may be out of date: its facts changed since it was stamped: WD helps" in out
    assert "  evidence EV-0001 supports [screening/pending]: wd helps" in out
    assert "summary: may be out of date: a stage decision came after the one it was stamped with; latest stage " \
           "decision D-0001 continue_probe: a second seed" in out
    with pytest.raises(RLError, match="H2 has no explanation in research/notes/explanation.yaml"):
        explain.stamp(project, ["H2"])


def test_one_research_line_holds_a_subtree_and_the_rest_is_unclassified(project, tmp_path, rl):
    research.add_hypothesis(project, "B", "b holds")  # H-0002 H2
    research.add_hypothesis(project, "C", "c holds", derived_from=["H-0001", "H-0002"])  # H-0003 H2.1
    research.add_hypothesis(project, "D", "d holds", derived_from=["H-0003"])  # H-0004 H2.1.1
    research.add_experiment(project, "both", "q?", ["H-0001", "H-0003"])  # E-0002 EH1/2: nothing in common
    words(tmp_path, {"lines": [{"key": "m", "name": "Line M", "desc": "about H1", "roots": ["H2.1"]},
                               {"key": "n", "name": "Line N", "roots": ["H2", "EH1/1", "H9", "H2.1"]}]})
    code, out, _ = rl("view")
    assert code == 0 and "2 research line(s), 1 top-level hypotheses unclassified" in out
    assert "warning: lines: n: root EH1/1 is an experiment; an experiment is shown under the hypothesis it is " \
           "anchored at" in out
    assert "warning: lines: n: root H9: no hypothesis is labelled H9" in out
    assert "warning: lines: n: root H2.1 is also a root of an earlier line, which shows it" in out
    text = page(tmp_path)
    m, n, rest = (text[text.index(f'<section class="line" id="{s}">'):] for s in ("line-m", "line-n", "unclassified"))
    m, n = m[:m.index("</section>")], n[:n.index("</section>")]
    assert '<li class="node h" id="H-0003">' in m and '<li class="node h" id="H-0004">' in m  # H2.1 and below
    assert '<p class="desc">about H1</p>' in m and n.count('href="#H-0003"') == 1  # one stub, inside H2
    assert '<li class="node h" id="H-0002">' in n and '<li class="node stub"><a class="nid" href="#H-0003">H2.1</a>' in n
    assert '<li class="node h" id="H-0001">' in rest and '<li class="node e" id="E-0002">' in rest  # EH1/2 under H1
    # the relations the labels do not show are listed with links
    assert '<dt>derived from</dt><dd><a href="#H-0001">H1</a>, <a href="#H-0002">H2</a></dd>' in m
    assert '<dt>tests</dt><dd><a href="#H-0001">H1@1</a>, <a href="#H-0003">H2.1@1</a></dd>' in rest
    assert '<dt>tested by</dt><dd><a href="#E-0002">EH1/2</a></dd>' in m
    ids = re.findall(r' id="([^"]+)"', text)
    assert len(ids) == len(set(ids)) and set(re.findall(r'href="#([^"]+)"', text)) <= set(ids)


def test_what_the_words_file_gets_wrong_is_reported_and_the_rest_shown(project, tmp_path, rl):
    words(tmp_path, {"summary": {"text": "s", "basis": "D-0001"},
                     "nodes": {"H7": {"purpose": "p"}, "H1": {"purpose": ["a", "list"], "did": "d", "colour": "red",
                                                              "basis": "blake2b:00"}}})
    code, out, _ = rl("view")
    assert code == 0
    for problem in ("summary: basis is not used: rl view --stamp records it in research/notes/explanation.basis.yaml",
                    "nodes: H7: no hypothesis is labelled H7", "nodes: H1: purpose is not text",
                    "nodes: H1: basis is not used: rl view --stamp records it",
                    "nodes: H1: colour is not used (the fields are title, purpose, did, result)"):
        assert f"warning: {problem}" in out
    assert '<span class="wl">What was done</span><p>d</p>' in page(tmp_path)
    (tmp_path / WORDS).write_text("nodes: [unclosed\n", encoding="utf-8")
    code, out, _ = rl("view")
    assert code == 0 and "warning: research/notes/explanation.yaml does not parse (" in out
    assert "the page shows the facts only" in out and '<li class="node h" id="H-0001">' in page(tmp_path)
    code, out, _ = rl("status")
    assert "explanations on docs/research.html: 3 to write, 1 problem(s) in research/notes/explanation.yaml" in out


def test_a_hand_edited_draft_contract_does_not_stop_the_page(project, tmp_path, rl):
    (tmp_path / "research/experiments/E-0001.md").write_text("---\njust a note\n---\nbody\n", encoding="utf-8")
    code, out, err = rl("view")
    assert code == 0 and out.startswith("wrote docs/research.html"), err
    assert '<li class="node e" id="E-0001">' in page(tmp_path)


def test_the_page_is_self_contained_and_escapes_what_it_shows(project, tmp_path, rl):
    research.add_hypothesis(project, "<script>alert(1)</script>", 'a "quoted" & <b>bold</b> claim')
    words(tmp_path, {"lines": [{"key": 'x"><script>', "name": "<i>n</i>", "roots": ["H2"]}],
                     "nodes": {"H2": {"purpose": "<img src=x onerror=alert(1)>\n\nsecond paragraph"}}})
    rl("view")
    text = page(tmp_path)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text and "a &quot;quoted&quot; &amp; &lt;b&gt;bold&lt;/b&gt;" in text
    assert "<p>&lt;img src=x onerror=alert(1)&gt;</p><p>second paragraph</p>" in text
    assert '<section class="line" id="line-x-script"><h2>&lt;i&gt;n&lt;/i&gt;</h2>' in text
    assert "<script" not in text and "<img" not in text and "<i>" not in text
    assert not re.search(r'(src|href)="(?!#)', text) and "http" not in text and "@import" not in text


def test_ids_in_record_text_read_as_linked_labels(project, tmp_path, rl):
    research.add_hypothesis(project, "B", "b holds", derived_from=["H-0001"])  # H1.1
    research.decide(project, ["H-0001"], "continue", "E-0001 shows H-0002@1 holds; see research/experiments/E-0001.md")
    rl("view")
    assert ('— <a href="#E-0001">EH1/1</a> shows <a href="#H-0002">H1.1@1</a> holds; see '
            "research/experiments/E-0001.md") in page(tmp_path)


def test_the_main_metric_is_a_column_even_when_it_sorts_last_and_declared_metrics_are_the_only_ones(project, tmp_path):
    metrics = {**{k: 1.0 for k in "abcdefgh"}, "zz": 0.5}
    research.add_experiment(project, "many metrics", "q?", ["H-0001"], metric="zz")  # E-0002
    result(project, ran(project, "r1", {**SPEC, "metric": "zz"}, "E-0002"), metrics, "res1")
    keys, found = rows(project, "E-0002")
    assert keys == ["zz", "a", "b", "c", "d", "e"] and found[0]["cells"][0] == "0.5 (n=1)"
    research.add_experiment(project, "declared", "q?", ["H-0001"], metric="zz")  # E-0003
    contract = tmp_path / "research/experiments/E-0003.md"
    contract.write_text(contract.read_text().replace("metric: zz\n", "metric: zz\nmetrics:\n- h\n- zz\n"))
    result(project, ran(project, "r2", {**SPEC, "metric": "zz"}, "E-0003"), metrics, "res2")
    assert rows(project, "E-0003")[0] == ["h", "zz"]


def test_rows_keep_conditions_apart_and_each_run_counts_once(project, tmp_path):
    first = ran(project, "r1")  # RUN-0001 under contract revision 1
    result(project, first, {"acc": 0.8}, "res1")
    again = ran(project, "r1-again", run="RUN-0001")  # a re-execution of the same run
    result(project, again, {"acc": 0.9}, "res2")
    late = result(project, again, {"acc": 0.1}, "res3", status="partial")  # a late partial upload
    assert rows(project)[1][0]["cells"] == ["0.9 (n=1)"] and rows(project)[1][0]["attempts"] == "2 ended/success"
    contract = tmp_path / "research/experiments/E-0001.md"
    contract.write_text(contract.read_text() + "\nmore seeds\n")  # the next run seals revision 2
    result(project, ran(project, "r2", {**SPEC, "seed": 1}), {"acc": 0.5}, "res4")
    result(project, ran(project, "r3", {**SPEC, "seed": 2, "code_ref": {"content_digest": "blake2b:" + "1" * 64}}),
           {"acc": 0.7}, "res5", status="partial")
    result(project, ran(project, "r4", {**SPEC, "seed": 3}), {"acc": 0.2}, "res6", seed=9)  # not as registered
    result(project, ran(project, "r5", {**SPEC, "data_revision": "synthetic-v2"}), {"acc": 0.3}, "res7")
    keys, found = rows(project)
    assert [(r["contract"], r["condition"], r["seeds"], r["cells"], r["notes"]) for r in found] == [
        ("@1", "data synthetic-v1", "0", ["0.9 (n=1)"], ""),
        ("@2", "data synthetic-v1", "1,2,3", ["0.6 (n=2)"],
         "1 partial; 1 not usable for the planned comparison; 2 code versions"),
        ("@2", "data synthetic-v2", "0", ["0.3 (n=1)"], "")]
    runs.reject_result(project, "RES-0002", "wrong labels")  # the run's complete result is rejected
    assert rows(project)[1][0]["cells"] == ["0.8 (n=1)"]  # its other complete one counts, not the late partial
    with project.read() as tx:
        lines = view._arm_table(tx, project, tx.require("E-0001"))
    assert "| arm | contract | condition | seeds | attempts | acc | notes |" in lines
    assert any(line.startswith("Resource use: unknown for 6 attempt(s) (ATT-0001-1, ATT-0001-2, ATT-0002-1, "
                               "ATT-0003-1, …).") for line in lines)
    assert late


def test_resource_use_is_reported_shared_or_unknown(project):
    a1, a2 = ran(project, "r1"), ran(project, "r2", {**SPEC, "seed": 1})
    a3 = ran(project, "r3", {**SPEC, "seed": 2})
    research.add_experiment(project, "other", "q?", ["H-0001"])  # E-0002
    b1 = ran(project, "r4", exp="E-0002")
    usage = {"quantity": 2, "unit": "node_h", "resource_type": "compute", "hardware": "gh200"}
    runs.add_usage(project, {**usage, "accounting_ref": "pbs:1"}, [a1])
    runs.add_usage(project, {**usage, "accounting_ref": "pbs:2", "quantity": 4}, [a2, b1])
    with project.read() as tx:
        assert view.costs(tx, "E-0001") == [
            "reported 2 node_h compute on gh200 (reported)",
            "shared job U-0002 4 node_h compute on gh200 (reported), also for 1 attempt(s) of other experiments",
            f"unknown for 1 attempt(s) ({a3})"]


def test_status_counts_the_explanations_once_the_page_exists(project, tmp_path, rl):
    code, out, _ = rl("status")
    assert "explanations on docs/research.html" not in out
    rl("view")
    code, out, _ = rl("status")
    assert "  explanations on docs/research.html: 3 to write: rl view --todo" in out
    code, out, _ = rl("view", "--todo")
    assert "EH1/1 (E-0001) explanation to write: WD smoke" in out and "  tests H1@1" in out
    (tmp_path / "docs/research-graph.html").write_text("old page", encoding="utf-8")
    code, out, _ = rl("upgrade")
    assert "docs/research-graph.html is no longer updated: rl view writes docs/research.html" in out
