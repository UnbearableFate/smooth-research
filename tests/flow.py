"""The steps of one hypothesis, from the tree draft to the verdict, driven through the CLI as each role would.
Tests take the project to the step they need and check what the records and the inboxes say there."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import git

from smooth_research import inbox
from smooth_research import tree as tree_draft
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


DRAFT = """\
why: The plan makes one claim, that averaging lowers the error; one hypothesis covers it.
hypotheses:
  - key: main
    kind: main
    title: Averaging helps
    statement: mean of 3 beats 1
    scope: N(5,1)
    purpose: whether averaging is worth it
    source: docs/plan.md § Averaging
    cost: about 0.01 cpu·h
prep:
  - title: Python check
    requirement: a python3 that runs the simulator
    done_check: python3 -c "print('ok')"
"""


def tree(store, rl, draft: str = DRAFT) -> None:
    """The coordinator's subagent drafts a one-hypothesis tree, the user approves it, the coordinator records it:
    H-0001, its route decision D-0001, the queued public prep task T-0001 and the approval RQ-0001."""
    path = tree_draft.draft_path(store.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft, encoding="utf-8")
    ok(rl("tree", "check", who="coordinator"))
    ok(rl("tree", "record", "--approval", "approve", who="coordinator"))


def approved(store, rl) -> None:
    """The coordinator queues the hypothesis task T-0002 in a phase and releases it: scientist-T-0002 holds it."""
    tree(store, rl)
    c = "coordinator"
    ok(rl("phase", "create", "--title", "Phase 1", who=c))
    ok(rl("task", "create", "--kind", "hypothesis", "--targets", "H-0001", "--priority", "1", "--envelope",
          "about 1 cpu·h", "--reason", "the only one", "--phase", "PH-0001", who=c))
    ok(rl("task", "release", "T-0002", who=c))


def designed(store, rl) -> None:
    """The scientist designs an experiment and its execute task T-0003 (held at once), commits code, tests."""
    approved(store, rl)
    s = "scientist-T-0002"
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


def head(store, tid: str) -> str:
    return git(worktree(store, tid), "rev-parse", "HEAD")


def dispatched(store, rl) -> None:
    """The design review RV-0001 passes at the tested commit; the scientist dispatches T-0003 to researcher-T-0003."""
    designed(store, rl)
    ok(rl("review", "request", "T-0003", who="scientist-T-0002"))
    ok(rl("review", "answer", "RV-0001", "--verdict", "pass", "--at", head(store, "T-0003")[:12],
          who="reviewer-RV-0001"))
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
    """The scientist accepts T-0003, writes the verdict D-0003, gets it reviewed (RV-0002), finalizes it and
    delivers the hypothesis task T-0002."""
    delivered(store, rl)
    s = "scientist-T-0002"
    ok(rl("task", "accept", "T-0003", who=s))
    ok(rl("decide", "--kind", "verdict", "--targets", "H-0001", "--status", "supported", "--rationale", "lower",
          "--supports", "EV-0001", "--did", "simulated both arms", "--means", "averaging helps here", who=s))
    ok(rl("review", "request", "D-0003", who=s))
    ok(rl("review", "answer", "RV-0002", "--verdict", "pass", "--at", "D-0003", who="reviewer-RV-0002"))
    ok(rl("decide", "--final", "D-0003", who=s))
    ok(rl("task", "deliver", "T-0002", "--conclusion", "averaging helps", who=s))
