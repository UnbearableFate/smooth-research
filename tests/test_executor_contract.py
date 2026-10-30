"""The executor contract with miyabi-mcp: its `ledger_query(format="smooth-research")` lines go to `rl run record`
unchanged. The fixture follows DESIGN.md 7.3, 7.4 and 11.2 of miyabi-mcp, built from the real `qstat -H -f` of a
finished job (3731052.opbs, Exit_status 0, walltime 00:10:17 on 1 node) as a watcher would have seen it."""

import json
from pathlib import Path

from conftest import SPEC
from smooth_research import runs

EXPORT = Path(__file__).parent / "fixtures" / "miyabi_mcp_export.jsonl"


def lines():
    return [json.loads(line) for line in EXPORT.read_text().splitlines() if line.strip()]


def write(path, records):
    path.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
                            for r in records))
    return path


def test_an_export_is_recorded_once_in_source_order_whatever_the_delivery_order(project, rl, tmp_path):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    queued, running, ended = lines()
    code, out, _ = rl("run", "record", "--observation", write(tmp_path / "end.jsonl", [ended]))
    assert code == 0 and "applied" in out
    code, out, _ = rl("run", "record", "--observation", write(tmp_path / "all.jsonl", [queued, running, ended]))
    assert code == 0 and out.count("stale") == 2 and "duplicate" in out
    with project.read() as tx:
        att = tx.require("ATT-0001-1", "Attempt")
        ref = tx.require(tx.out("ATT-0001-1", "bound_to")[0]["id"])
        usage = runs.usage_summary(tx)
    assert (att["execution_state"], att["outcome"], att["last_sequence"]) == ("ended", "success", 3)
    assert (ref["kind"], ref["executor_namespace"], ref["opaque_id"]) == ("hpc_job", "miyabi-pbs", "3731052.opbs")
    assert usage["totals"] == [{"resource_type": "gpu_node", "unit": "node_hours", "hardware": "GH200",
                                "basis": "measured", "quantity": 0.171389}]
    code, out, _ = rl("run", "show", "ATT-0001-1")
    assert "(event at 2026-10-30T05:52:49+09:00)" in out


def test_usage_from_the_export_and_from_the_cli_count_once_and_tokens_stay_separate(project, rl, tmp_path):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    rl("run", "record", "--observation", write(tmp_path / "all.jsonl", lines()))
    same = lines()[2]["usage"]
    code, out, _ = rl("run", "usage", "--accounting-ref", same["accounting_ref"], "--quantity", same["quantity"],
                      "--unit", same["unit"], "--resource-type", same["resource_type"], "--hardware", same["hardware"],
                      "--basis", same["basis"], "--source-ref", same["source_ref"], "--attempts", "ATT-0001-1")
    assert code == 0 and "already recorded" in out
    code, out, _ = rl("run", "usage", "--accounting-ref", "miyabi-pbs:3731052.opbs:token", "--quantity", "0.2",
                      "--unit", "miyabi_token", "--resource-type", "site_allocation", "--basis", "reported",
                      "--attempts", "ATT-0001-1")
    assert code == 0 and "recorded" in out
    with project.read() as tx:
        totals = runs.usage_summary(tx)["totals"]
    assert {(t["unit"], t["basis"]) for t in totals} == {("node_hours", "measured"), ("miyabi_token", "reported")}


def test_a_lookup_after_the_end_and_a_reconciled_correction(project, rl, tmp_path):
    runs.register(project, "E-0001", dict(SPEC), "r1")
    rl("run", "record", "--observation", write(tmp_path / "all.jsonl", lines()))
    base = {k: v for k, v in lines()[2].items() if k not in ("usage", "occurred_at")}
    aged_out = {**base, "record_id": "miyabi-pbs:3731052.opbs:4", "source_sequence": 4,
                "observed_at": "2026-11-05T09:00:00+09:00", "execution_state": "unknown", "outcome": "unknown",
                "raw_state": "not in active table or history", "source_ref": "qstat -H -f 3731052.opbs @miyabi-g2"}
    code, out, _ = rl("run", "record", "--observation", write(tmp_path / "aged.jsonl", [aged_out]))
    assert code == 0 and "observation_issue" in out
    corrected = {**base, "record_id": "miyabi-pbs:3731052.opbs:5", "source_sequence": 5,
                 "observed_at": "2026-11-05T09:01:00+09:00", "outcome": "failed", "raw_state": "F Exit_status=1",
                 "correction_of": "miyabi-pbs:3731052.opbs:3", "note": "re-read qstat -H -f: Exit_status=1 (test)"}
    code, out, _ = rl("run", "record", "--observation", write(tmp_path / "fix.jsonl", [corrected]))
    assert code == 0 and "correction" in out
    with project.read() as tx:
        att = tx.require("ATT-0001-1", "Attempt")
    assert (att["execution_state"], att["outcome"]) == ("ended", "failed")
