"""The steps of one hypothesis, from the tree to the phase conclusion, driven through the CLI as each role would.
Tests take the project to the step they need and check what the records and the inboxes say there."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import git

from smooth_research import inbox
from smooth_research.store import Store

SIM = '''import argparse, json, random, statistics, subprocess

def error(arm, seed, n=300):
    rng = random.Random(seed)
    k = 3 if arm == "mean3" else 1
    return statistics.fmean(abs(statistics.fmean(rng.gauss(5, 1) for _ in range(k)) - 5) for _ in range(n))

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--arm"); p.add_argument("--seed", type=int); p.add_argument("--out")
    a = p.parse_args()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    json.dump({"status": "complete", "arm": a.arm, "seed": a.seed, "commit": commit,
               "config": {"k": 3 if a.arm == "mean3" else 1},
               "metrics": {"abs_error": error(a.arm, a.seed)}}, open(a.out, "w"))
'''
TEST = 'from sim import error\nassert error("single", 0, 50) > 0\nprint("ok")\n'
ARMS = ("arms:\n  - name: single\n    seeds: [0]\n    config: {k: 1}\n  - name: mean3\n    seeds: [0]\n"
        "    config: {k: 3}\nmetrics: [abs_error]\nstop_when: the test fails\n")


def ok(result) -> str:
    code, out, err = result
    assert code == 0, f"exit {code}: {err or out}"
    return out


def items(store: Store, who: str) -> list[inbox.Item]:
    with store.read() as tx:
        return inbox.for_recipient(tx, store.project.config, who)


def types(store: Store, who: str) -> list[str]:
    return [i.type for i in items(store, who)]


def worktree(store: Store, tid: str) -> Path:
    return store.project.worktrees_dir / tid


def tree(store, rl) -> None:
    """Coordinator makes and releases the tree task; a scientist builds a one-hypothesis tree; it passes review and
    is delivered."""
    ok(rl("task", "create", "--kind", "tree", "--title", "Tree", who="coordinator"))
    ok(rl("task", "release", "T-0001", who="coordinator"))
    s = "scientist-T-0001"
    ok(rl("task", "claim", "T-0001", who=s))
    ok(rl("hypothesis", "add", "--title", "Averaging helps", "--statement", "mean of 3 beats 1", "--purpose",
          "whether averaging is worth it", "--scope", "N(5,1)", "--source", "Averaging", who=s))
    ok(rl("decide", "--kind", "route", "--targets", "H-0001", "--rationale", "one hypothesis covers it", who=s))
    ok(rl("review", "request", "T-0001", "--kind", "tree", who=s))
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", who="reviewer-RV-0001"))
    ok(rl("task", "deliver", "T-0001", "--conclusion", "one hypothesis", who=s))


def approved(store, rl) -> None:
    """The coordinator receives the tree, queues the hypothesis task in a phase, the user approves, it is released."""
    tree(store, rl)
    c = "coordinator"
    ok(rl("task", "accept", "T-0001", who=c))
    ok(rl("phase", "create", "--title", "Phase 1", "--scientists", "1", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--parallel", "1", "--reason", "only one", "--phase", "PH-0001", who=c))
    ok(rl("request", "--kind", "approval", "--task", "T-0001", "--stuck", "approve the tree", "--options",
          "approve | change", "--recommend", "approve", who=c))
    ok(rl("request", "answer", "RQ-0001", "--by", "user", "--text", "approve", who=c))
    ok(rl("task", "release", "T-0002", who=c))


def designed(store, rl) -> None:
    """The scientist claims the hypothesis task, designs an experiment and its execute task, commits code, tests."""
    approved(store, rl)
    s = "scientist-T-0002"
    ok(rl("task", "claim", "T-0002", who=s))
    ok(rl("decide", "--kind", "route", "--targets", "H-0001", "--rationale", "simulate", who=s))
    ok(rl("experiment", "add", "--title", "Sim", "--question", "is mean3 better?", "--hypotheses", "H-0001",
          "--motivated-by", "D-0002", who=s))
    ok(rl("task", "create", "--kind", "execute", "--title", "Run sim", "--experiment", "E-0001", "--estimate",
          "0.01 cpu·h", who=s))
    wt = worktree(store, "T-0003")
    (wt / "src" / "avg").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "avg" / "sim.py").write_text(SIM)
    (wt / "src" / "avg" / "test_sim.py").write_text(TEST)
    (wt / "src" / "avg" / "arms.yaml").write_text(ARMS)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "design")
    ok(rl("experiment", "set", "E-0001", "--manifest", "src/avg/arms.yaml", "--protected", "src/avg/sim.py::error",
          "--semantic-tests", "cd src/avg && python3 test_sim.py", who=s))
    ok(rl("task", "test", "T-0003", who=s))


def dispatched(store, rl) -> None:
    designed(store, rl)
    ok(rl("review", "request", "T-0003", who="scientist-T-0002"))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", who="reviewer-RV-0002"))
    ok(rl("task", "dispatch", "T-0003", "--note", "both arms", who="scientist-T-0002"))


def run_arm(store, rl, arm: str, seed: int = 0, minutes: float = 1) -> str:
    """The researcher registers, runs and records one evidence run; returns its result id."""
    r = "researcher-T-0003"
    k = 3 if arm == "mean3" else 1
    code, out, err = rl("--json", "run", "register", "--task", "T-0003", "--request-id", f"r-{arm}-{seed}", "--arm",
                        arm, "--seed", seed, "--config", json.dumps({"k": k}), "--minutes", minutes, who=r)
    assert code == 0, err
    att = json.loads(out)["attempt"]
    wt = worktree(store, "T-0003")
    out_file = wt / "research" / "work" / "T-0003" / f"{arm}-{seed}.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    subprocess.run(["python3", "sim.py", "--arm", arm, "--seed", str(seed), "--out", str(out_file)],
                   cwd=wt / "src" / "avg", check=True)
    ok(rl("run", "record", "--attempt", att, "--state", "ended", "--outcome", "success", who=r))
    code, out, err = rl("--json", "run", "import-result", "--attempt", att, "--request-id", f"i-{arm}-{seed}",
                        "--manifest", out_file.read_text(), who=r)
    assert code == 0, err
    return json.loads(out)["result"]


def delivered(store, rl) -> None:
    """The researcher runs both arms, writes evidence, commits outputs and delivers."""
    dispatched(store, rl)
    r = "researcher-T-0003"
    res = [run_arm(store, rl, "single"), run_arm(store, rl, "mean3")]
    ok(rl("evidence", "add", "--task", "T-0003", "--summary", "mean3 error lower", "--level", "diagnostic",
          "--results", ",".join(res), who=r))
    wt = worktree(store, "T-0003")
    git(wt, "add", "research/work/T-0003")
    git(wt, "commit", "-qm", "results")
    ok(rl("task", "deliver", "T-0003", "--conclusion", "mean3 lowers the error (EV-0001)", "--limitations",
          "one seed", "--outputs", "research/work/T-0003/mean3-0.json", who=r))


def concluded(store, rl) -> None:
    """The scientist accepts, writes a verdict, gets it reviewed, finalizes and delivers the hypothesis task."""
    delivered(store, rl)
    s = "scientist-T-0002"
    ok(rl("task", "accept", "T-0003", who=s))
    ok(rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale", "lower",
          "--supports", "EV-0001", "--did", "simulated both arms", "--means", "averaging helps here", who=s))
    ok(rl("review", "request", "D-0003", "--kind", "conclusion", who=s))
    ok(rl("review", "answer", "RV-0003", "--verdict", "pass", who="reviewer-RV-0003"))
    ok(rl("decide", "--final", "D-0003", who=s))
    ok(rl("task", "deliver", "T-0002", "--conclusion", "averaging helps", who=s))
