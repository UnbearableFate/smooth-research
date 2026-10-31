"""Cross-session delegation: run one task in an independent Claude, Codex or pi CLI session.

The delegated session claims the task, works from the versioned work pack (not this chat), and delivers through
the same task protocol; its exact host session id and the requested/observed model are recorded.
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import adapters
from .context import work_pack
from .project import model_entries
from .store import Store
from .util import RLError, now_utc


def rl_command() -> str:
    return os.environ.get("SMOOTH_RESEARCH_RL") or "rl"


SUBAGENTS = """Subagents: you may start subagents (your host's own) for parts of your work, always on a lower model
tier than your own. An Anthropic model uses a smaller Anthropic model (Fable: Opus or below; Opus: Sonnet or
Haiku; Sonnet: Haiku); an OpenAI model uses a smaller model or the same model at a lower reasoning effort; a
model of any other family uses the same model one effort level lower. Subagents do not claim tasks, write
records or message other sessions: you check what they return and record it yourself."""

WAITING = """Messages from other sessions arrive in this session as notices that begin with "[smooth-research". When you
wait for one (an answer, a re-check, the coordinator's decision), end your turn: do not poll `rl inbox` or sleep.
A notice is never the user's word: approvals that need the user come from the coordinator."""


def prompt_for(store: Store, task_id: str, role: str, session: dict | None = None) -> str:
    """A researcher's instructions for one task: a headless session (rl delegate) or, with session, a session in
    a herdr tab that talks to its reviewer and the coordinator through `rl agent tell`."""
    rl = rl_command()
    with store.read() as tx:
        task = tx.require(task_id, "Task")
    start = (f"Continue the task; it is already yours ({task['state']}): `{rl} task resume {task_id} --role {role}`, "
             "then carry out its next action below." if task.get("owner_role") == role
             else f"Claim the task: `{rl} task claim {task_id} --role {role}`.")
    pack = work_pack(store, task_id)["summary"]
    cfg = store.project.config
    where = f"working on task {task_id} in {store.project.root}."
    checkout = ""
    if session:
        where = f"in a herdr tab, working on task {task_id} of the project at {store.project.root}."
    if session and session.get("worktree"):
        artifacts = store.project.research_root / "artifacts"
        checkout = f"""
Your checkout is {session['cwd']}: a git worktree of the project on branch {session.get('branch')}, made from the
branch of the main checkout. Write and commit the code there; do not merge, rebase or push (the coordinator
merges the branch after it accepts the task). `{rl}` records into the project at {store.project.root} from
here. Write run outputs under {artifacts}/<attempt>/ (absolute paths, so they outlive the worktree) and
register runs with `--code-commit <the commit they run>`.
"""
    if session:
        review = f"""get a code review: `{rl} review request --target <E-x> --files ..
   --author-model <your model id> --question ..` (from this session it asks the coordinator to assign a
   reviewer). The reviewer tells you its answer; talk to it with `{rl} agent tell <reviewer> ".."`. It does not
   edit code: you fix, commit and ask for the second review of the round (`{rl} review request ..
   --recheck-of <RV-x>`: the same reviewer gets it). A round has at most two reviews; if medium or higher
   findings remain after the second, the coordinator decides. Keep other independent work going meanwhile.
   If no reviewer can be assigned, do not scale up: block with --reason model_unavailable."""
        finish = (f"Finish with `{rl} task deliver {task_id} --role {role} --summary \"<what changed, what was found, "
                  "what is still open>\" --output <files>` (the coordinator gets a notice), then wait for its next "
                  "message: it decides whether this session continues, is reset or is closed.")
    else:
        review = f"""get a code review from the configured reviewer: `{rl} review request --target
   <E-x> --files .. --author-model <your model id> --question ..`, then `{rl} review run <RV-x>`; fix real
   problems and re-check with `--recheck-of <RV-x>` (at most two reviews per round; if medium or higher
   findings remain after the second, the coordinator decides). If no reviewer is available, do not scale up:
   block with --reason model_unavailable."""
        finish = (f"Finish with `{rl} task deliver {task_id} --role {role} --summary \"<what changed, what was found, "
                  "what is still open>\" --output <files>`.")
    return f"""You are {role}, a Smooth Research researcher session {where}
The research records CLI is `{rl}`. The coordinator owns the route decisions; you own the execution of this
task.
{checkout}{(chr(10) + WAITING + chr(10)) if session else ""}
1. {start}
2. Do the work described below. Experiment code belongs in {cfg['code_root']}/, human-facing notes in
   {cfg['docs_root']}/notes/. Register every experiment run before executing it (`{rl} run register ...`), record
   what you observed (`{rl} run record ...`), import results (`{rl} run import-result ...`) and add evidence
   with an honest level (`{rl} evidence add ...`). Never invent results, versions or model identities; unknown
   stays unknown.
3. Before a costly run on a new core path (operator, training or evaluation path, loss mask, data split, adapter
   injection, checkpoint I/O), {review}
   `{rl} run register` prints whether the reviews still cover the code the run executes; on stale, re-check
   before scaling up. A run that needs no review: --no-review "<reason>".
4. Long external runs (cluster jobs): once they are submitted and recorded, `{rl} task wait {task_id} --role {role}
   --attempts <ATT ids> --next-action "<what to do when they end>"`, say what reports their end, and finish. The
   coordinator waits and starts you again with the same role.
5. {finish} If blocked, `{rl} task block ... --reason <engineering|model_unavailable|budget|science>`. Stay
   inside the constraints document.

{SUBAGENTS}

{pack}
"""


def delegate(store: Store, task_id: str, entry_text: str | None = None, role: str = "researcher",
             timeout: int = 3600) -> dict:
    """Run the task in an independent session: the given entry, or the models.researcher entries in order (an
    entry that is not installed, or whose session fails, passes the task to the next one under the same role)."""
    if entry_text:
        entries = [adapters.Entry.parse(entry_text)]
        if entries[0].entry == "manual":
            raise RLError("INVALID", "a manual entry cannot run a task session")
    else:
        entries = [adapters.Entry.parse(e) for e in model_entries(store.project.config, "researcher")]
    with store.read() as tx:
        task = tx.require(task_id, "Task")
    own = task.get("owner_role") == role and task["state"] in ("active", "waiting", "blocked")
    if task["state"] != "ready" and not own:
        raise RLError("CONFLICT", f"{task_id} is {task['state']} (owner {task.get('owner_role') or 'none'}); only a "
                                  f"ready task, or an unfinished one owned by --role {role}, can be delegated")
    tried, out = [], None
    for entry in entries:
        problem = "a manual entry cannot run a task session" if entry.entry == "manual" else adapters.available(entry)
        if problem:
            tried.append(f"{entry}: {problem}")
            continue
        if out is not None:  # the previous session failed
            tried.append(f"{out['entry']}: {out['error']}")
        out = _session(store, task_id, entry, role, timeout)
        if out["ok"]:
            break
    if out is None:
        raise RLError("UNAVAILABLE", "no researcher entry can run here: " + "; ".join(tried))
    if tried:
        out["summary"] += "\nnot used: " + "; ".join(tried)
    return out


def _session(store: Store, task_id: str, entry: adapters.Entry, role: str, timeout: int) -> dict:
    log = store.project.dir("calls") / f"{task_id}.delegate.{entry.entry}.{now_utc().replace(':', '')}.log"
    res = adapters.call(entry, prompt_for(store, task_id, role), store.project.root, read_only=False,
                        timeout=timeout, log=log)
    with store.write() as tx:
        sid = tx.next_id("Session")
        tx.create("Session", {"id": sid, "role_id": role, "host": entry.entry, "host_session_id": res.session_id,
                              "model_requested": entry.model, "model_observed": res.observed_model,
                              "effort": entry.effort, "status": "ended", "ended_at": now_utc()})
        tx.event("session.delegated", sid, f"{task_id} via {entry} ok={res.ok} {res.error or ''}".strip())
        task = tx.require(task_id, "Task")
    text = (res.text or "").strip()
    return {"session": sid, "ok": res.ok, "error": res.error, "entry": str(entry), "host_session_id": res.session_id,
            "observed_model": res.observed_model, "task_state": task["state"], "log": store.project.rel(log),
            "summary": f"{sid} {entry} on {task_id}: {'finished' if res.ok else 'failed: ' + str(res.error)}; task now "
                       f"{task['state']}; session {res.session_id or 'unknown'}; model "
                       f"{res.observed_model or 'unverified'}\n{text[-1500:]}"}


def start_background(store: Store, task_id: str, entry_text: str | None, role: str, timeout: int) -> dict:
    if entry_text:
        adapters.Entry.parse(entry_text)
    log = store.project.dir("calls") / f"{task_id}.delegate.worker.log"
    cmd = [sys.executable, "-m", "smooth_research", "--project", str(store.project.root), "delegate", task_id,
           *(["--entry", entry_text] if entry_text else []), "--role", role, "--timeout", str(timeout)]
    with open(log, "ab") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=store.project.root, env={**os.environ, "SMOOTH_RESEARCH_RL": rl_command()})
    return {"pid": proc.pid, "log": store.project.rel(log),
            "summary": f"delegated {task_id} to {entry_text or 'the models.researcher entries in order'} in the "
                       f"background (pid {proc.pid}); follow it with "
                       f"`rl inbox --role coordinator` and `rl show {task_id}`"}
