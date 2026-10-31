"""Run-record invariants: idempotent registration, observation ordering, results, usage, no side effects."""

import subprocess

import pytest

from conftest import SPEC, obs
from smooth_research import adapters, runs
from smooth_research.util import RLError


def register(store, request_id="r1", spec=SPEC, run=None):
    return runs.register(store, "E-0001", dict(spec), request_id, run)


def attempt(store, att="ATT-0001-1"):
    with store.read() as tx:
        return tx.require(att, "Attempt")


def test_same_request_id_is_idempotent_and_conflicts_on_new_content(project):
    first = register(project)
    again = register(project)
    assert again["duplicate"] and (again["run"], again["attempt"]) == (first["run"], first["attempt"])
    with pytest.raises(RLError) as exc:
        register(project, spec={**SPEC, "seed": 1})
    assert exc.value.code == "CONFLICT"
    independent = register(project, request_id="r2")  # same spec, new request id: a legitimate repeat
    assert independent["run"] == "RUN-0002" and independent["spec_digest"] == first["spec_digest"]


def test_runspec_refuses_mutable_code_versions_and_guessing(project):
    for bad in ({"commit": "HEAD"}, {"commit": "main"}, {"commit": "abc123"}):
        with pytest.raises(RLError):
            register(project, request_id=str(bad), spec={**SPEC, "code_ref": bad})
    with pytest.raises(RLError, match="unknown"):
        register(project, request_id="missing", spec={k: v for k, v in SPEC.items() if k != "data_revision"})
    ok = register(project, request_id="full", spec={**SPEC, "code_ref": {"commit": "a" * 40}})
    assert ok["attempt"].startswith("ATT-")


def test_registration_seals_the_contract_and_later_edits_are_new_revisions(project):
    register(project)
    with project.read() as tx:
        exp = tx.require("E-0001")
        run = tx.require("RUN-0001")
    assert exp["contract_ref"] == "DOC-0002@1" and run["spec"]["protocol_ref"] == "DOC-0002@1"
    path = project.project.abs(exp["contract_path"])
    path.write_text(path.read_text() + "\nchanged comparator\n")
    register(project, request_id="r2", spec={**SPEC, "seed": 1})
    with project.read() as tx:
        assert tx.require("RUN-0001")["spec"]["protocol_ref"] == "DOC-0002@1"  # history keeps the old contract
        assert tx.require("RUN-0002")["spec"]["protocol_ref"] == "DOC-0002@2"
        assert not tx.require("E-0001").get("revised_after_results")


def test_contract_edit_after_results_is_flagged_exploratory(project):
    register(project)
    runs.import_result(project, "ATT-0001-1", {"status": "complete", "metrics": {"acc": 0.8}}, "res1")
    with project.read() as tx:
        path = project.project.abs(tx.require("E-0001")["contract_path"])
    path.write_text(path.read_text() + "\nnew threshold after seeing results\n")
    register(project, request_id="r2", spec={**SPEC, "seed": 1})
    with project.read() as tx:
        assert tx.require("E-0001")["revised_after_results"] is True


def test_duplicate_and_out_of_order_observations(project):
    register(project)
    first = runs.record(project, obs(state="running", at="2026-10-30T01:00:00Z", record_id="o1"))
    assert first["effect"] == "applied"
    assert runs.record(project, obs(state="running", at="2026-10-30T01:00:00Z", record_id="o1"))["duplicate"]
    with pytest.raises(RLError) as exc:
        runs.record(project, obs(state="queued", at="2026-10-30T01:00:00Z", record_id="o1"))
    assert exc.value.code == "CONFLICT"
    runs.record(project, obs(state="ended", outcome="success", at="2026-10-30T02:00:00Z", record_id="o2"))
    late = runs.record(project, obs(state="running", at="2026-10-30T01:30:00Z", record_id="o3"))
    assert late["effect"] == "stale"
    att = attempt(project)
    assert (att["execution_state"], att["outcome"]) == ("ended", "success") and not att.get("needs_check")


def test_unknown_before_and_after_a_terminal_state(project):
    register(project)
    runs.record(project, obs(state="running", at="2026-10-30T01:00:00Z"))
    runs.record(project, obs(state="unknown", at="2026-10-30T02:00:00Z", raw_state="qstat: unknown job"))
    att = attempt(project)
    assert att["execution_state"] == "unknown" and att["last_trusted_state"] == "running"
    runs.record(project, obs(state="ended", outcome="failed", at="2026-10-30T03:00:00Z"))
    lookup = runs.record(project, obs(state="unknown", at="2026-10-30T04:00:00Z", raw_state="history expired"))
    assert lookup["effect"] == "observation_issue"
    att = attempt(project)
    assert (att["execution_state"], att["outcome"]) == ("ended", "failed") and att["issues"]


def test_contradicting_ends_need_checking_and_explicit_correction(project):
    register(project)
    runs.record(project, obs(state="ended", outcome="success", at="2026-10-30T01:00:00Z", record_id="e1"))
    clash = runs.record(project, obs(state="ended", outcome="failed", at="2026-10-30T02:00:00Z", record_id="e2"))
    assert clash["effect"] == "conflict" and attempt(project)["needs_check"]
    with pytest.raises(RLError):
        runs.record(project, obs(state="ended", outcome="failed", at="2026-10-30T03:00:00Z", correction_of="e1"))
    fixed = runs.record(project, obs(state="ended", outcome="failed", at="2026-10-30T03:00:00Z",
                                     correction_of="e1", note="exit code 1 in log line 88"))
    att = attempt(project)
    assert fixed["effect"] == "correction" and att["outcome"] == "failed" and not att["needs_check"]


def test_namespaces_keep_identical_external_ids_apart_and_attempts_can_share_one(project):
    register(project)
    register(project, request_id="r2", spec={**SPEC, "seed": 1})
    ref_a = {"kind": "hpc_job", "executor_namespace": "site-a-pbs", "opaque_id": "123"}
    ref_b = {"kind": "hpc_job", "executor_namespace": "site-b-pbs", "opaque_id": "123"}
    x1 = runs.record(project, obs(execution_ref=ref_a))["execution_ref"]
    x2 = runs.record(project, obs(attempt="ATT-0002-1", execution_ref=ref_b))["execution_ref"]
    assert x1 != x2
    register(project, request_id="r3", spec={**SPEC, "seed": 2})
    x3 = runs.record(project, obs(attempt="ATT-0003-1", execution_ref=ref_a))["execution_ref"]
    assert x3 == x1  # one job running several seeds
    with pytest.raises(RLError):  # an attempt is one execution; a re-execution is a new attempt
        runs.record(project, obs(execution_ref=ref_b, at="2026-10-30T05:00:00Z"))


def test_re_execution_is_a_new_attempt_and_keeps_the_failure(project):
    register(project)
    runs.record(project, obs(state="ended", outcome="failed"))
    with project.read() as tx:
        draft = project.project.abs(tx.require("E-0001")["contract_path"])
    draft.write_text(draft.read_text() + "\nedited after the run was registered\n")
    objects = set(project.project.objects_dir.iterdir())
    again = register(project, request_id="r1-retry", run="RUN-0001")
    assert again["attempt"] == "ATT-0001-2" and again["run"] == "RUN-0001"
    assert set(project.project.objects_dir.iterdir()) == objects  # an attempt uses the run's sealed contract
    assert attempt(project)["outcome"] == "failed"
    with pytest.raises(RLError):  # a changed spec is a new run, not an attempt
        register(project, request_id="r1-changed", spec={**SPEC, "seed": 9}, run="RUN-0001")


def test_the_tables_show_the_complete_result_until_it_is_rejected(project):
    from smooth_research import view
    register(project)
    runs.record(project, obs(state="ended", outcome="success"))
    complete = runs.import_result(project, "ATT-0001-1", {"status": "complete", "metrics": {"acc": 0.9}}, "c1")
    runs.import_result(project, "ATT-0001-1", {"status": "partial", "metrics": {"acc": 0.1}}, "p1")

    def cells():
        with project.read() as tx:
            return view.arm_rows(tx, "E-0001")[1][0]["cells"]

    assert cells() == ["0.9 (n=1)"]  # a late partial upload does not replace the complete result
    runs.reject_result(project, complete["result"], "wrong labels")
    assert cells() == ["0.1 (n=1)"]
    runs.import_result(project, "ATT-0001-1", {"status": "complete", "metrics": {"acc": 0.85}}, "c2")
    assert cells() == ["0.85 (n=1)"]


def test_a_result_that_names_another_arm_or_seed_is_kept_but_not_usable(project):
    from smooth_research import research
    register(project)
    runs.record(project, obs(state="ended", outcome="success"))
    wrong = runs.import_result(project, "ATT-0001-1", {"status": "complete", "arm": "control", "seed": 99,
                                                       "metrics": {"acc": 0.9}}, "w1")
    assert [m["field"] for m in wrong["mismatches"]] == ["arm", "seed"] and "differs from RunSpec" in wrong["summary"]
    with pytest.raises(RLError, match="cannot be confirmatory"):
        research.add_evidence(project, "wd wins", "confirmatory", "valid", supports=["H-0001"],
                              results=[wrong["result"]])
    named = runs.import_result(project, "ATT-0001-1", {"status": "complete", "arm": "wd", "seed": "0",
                                                       "metrics": {"acc": 0.8}}, "w2")
    unnamed = runs.import_result(project, "ATT-0001-1", {"status": "complete", "metrics": {"acc": 0.7}}, "w3")
    assert named["mismatches"] == unnamed["mismatches"] == []


def test_result_mismatch_remote_uris_partial_and_late_results(project, tmp_path):
    register(project)
    metrics = tmp_path / "research" / "artifacts" / "m.json"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_text('{"acc": 0.8}')
    partial = runs.import_result(project, "ATT-0001-1", {"status": "partial", "metrics": {"acc": 0.7}}, "p1")
    complete = runs.import_result(project, "ATT-0001-1", {
        "status": "complete", "data_revision": "synthetic-v2", "metrics": {"acc": 0.8},
        "artifacts": [{"path": "research/artifacts/m.json"}, {"uri": "remote:/ckpt.pt", "digest": "md5:ab"}]},
        "c1")
    assert complete["mismatches"][0]["field"] == "data_revision"
    assert "copies RunSpec fields exactly or leaves them out" in complete["summary"]
    assert "__pycache__/" in (tmp_path / "src" / ".gitignore").read_text()  # init keeps caches out of snapshots
    assert "remote_reference_not_read" in complete["verification_scope"]
    runs.import_result(project, "ATT-0001-1", {"status": "partial", "metrics": {"acc": 0.1}}, "p2")
    assert attempt(project)["result_status"] == "complete"  # partial never replaces complete
    assert runs.import_result(project, "ATT-0001-1", {"status": "partial", "metrics": {"acc": 0.7}}, "p3")["duplicate"]
    with project.read() as tx:
        res = tx.require(complete["result"])
    assert res["usable_for_planned_comparison"] is False and res["spec_match"] is False
    assert partial["result"] != complete["result"]
    # late result after the attempt closed stays bound to that attempt
    runs.record(project, obs(state="ended", outcome="success"))
    late = runs.import_result(project, "ATT-0001-1", {"status": "complete", "metrics": {"acc": 0.81},
                                                      "data_revision": "synthetic-v1"}, "late")
    with project.read() as tx:
        assert tx.require(late["result"])["attempt_id"] == "ATT-0001-1"


def test_shared_usage_counts_once_and_unreported_is_not_zero(project):
    register(project)
    register(project, request_id="r2", spec={**SPEC, "seed": 1})
    for a in ("ATT-0001-1", "ATT-0002-1"):
        runs.record(project, obs(attempt=a, state="ended", outcome="success"))
    usage = {"accounting_ref": "pbs:42", "quantity": 2.0, "unit": "node_h", "resource_type": "compute"}
    runs.add_usage(project, usage, ["ATT-0001-1", "ATT-0002-1"])
    assert runs.add_usage(project, usage, ["ATT-0001-1", "ATT-0002-1"])["duplicate"]
    with pytest.raises(RLError, match=r"already recorded as U-0001 \(2 node_h"):
        runs.add_usage(project, {**usage, "quantity": 3.0}, ["ATT-0001-1", "ATT-0002-1"])
    runs.add_usage(project, {**usage, "quantity": 3.0}, ["ATT-0001-1", "ATT-0002-1"], revise=True)
    register(project, request_id="r3", spec={**SPEC, "seed": 2})
    runs.record(project, obs(attempt="ATT-0003-1", state="ended", outcome="success"))
    with project.read() as tx:
        summary = runs.usage_summary(tx)
    assert summary["totals"] == [{"resource_type": "compute", "unit": "node_h", "hardware": "-", "basis": "reported",
                                  "quantity": 3.0}]
    assert summary["unreported_attempts"] == ["ATT-0003-1"]
    runs.add_usage(project, {**usage, "quantity": 4.0}, ["ATT-0001-1", "ATT-0002-1"], revise=True)
    with project.read() as tx:
        revisions = sorted((u["id"], u["revision"]) for u in tx.all("Usage"))
    assert revisions == [("U-0001", 1), ("U-0002", 2), ("U-0003", 3)]


def test_record_operations_start_query_or_retry_nothing(project, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("record operations must not launch processes or call models")
    from smooth_research import documents
    monkeypatch.setattr(documents, "_git_commit", lambda path: None)  # the only process: a local git lookup
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapters, "call", forbidden)
    register(project)
    runs.record(project, obs(state="unknown"))
    runs.import_result(project, "ATT-0001-1", {"status": "partial", "artifacts": [{"uri": "ssh://x/y"}]}, "x")
    runs.show(project, active=True)
    from smooth_research.context import resume_pack
    assert "nothing was queried" in resume_pack(project)["summary"]


def test_a_batch_of_observations_from_an_executor_is_recorded_in_order_and_resent_safely(project, rl, tmp_path):
    import json
    register(project)
    ref = {"kind": "hpc_job", "executor_namespace": "miyabi-g-pbs", "opaque_id": "123456.opbs"}
    lines = [obs(None, "queued", at="2026-10-30T00:00:00+00:00", record_id="miyabi-g-pbs:123456.opbs:1",
                 source_sequence=1, raw_state="Q", execution_ref=ref),
             obs(None, "running", at="2026-10-30T00:01:00+00:00", record_id="miyabi-g-pbs:123456.opbs:2",
                 source_sequence=2, raw_state="R", execution_ref=ref),
             obs(None, "ended", "success", at="2026-10-30T00:03:00+00:00", record_id="miyabi-g-pbs:123456.opbs:3",
                 source_sequence=3, raw_state="F Exit_status=0", execution_ref=ref,
                 usage={"accounting_ref": "miyabi-pbs:123456.opbs", "quantity": 0.05, "unit": "node_h",
                        "resource_type": "gpu_node", "hardware": "GH200", "basis": "reported"})]
    for line in lines:
        line.pop("attempt_id")
    batch = tmp_path / "obs.jsonl"
    batch.write_text("".join(json.dumps(line) + "\n" for line in lines))
    code, out, _ = rl("run", "record", "--observation", batch, "--attempt", "ATT-0001-1")
    assert code == 0 and out.count("applied") == 3
    att = attempt(project)
    assert (att["execution_state"], att["outcome"], att["last_sequence"]) == ("ended", "success", 3)
    code, out, _ = rl("run", "record", "--observation", batch, "--attempt", "ATT-0001-1")
    assert code == 0 and out.count("duplicate") == 3
    lines[1]["execution_state"] = "finished"
    batch.write_text(json.dumps([{**line, "record_id": line["record_id"] + "b"} for line in lines]))
    code, _, err = rl("run", "record", "--observation", batch, "--attempt", "ATT-0001-1")
    assert code == 1 and "observation 2 of 3" in err and "1 before it" in err
