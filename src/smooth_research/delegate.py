"""Cross-session delegation: run one task in an independent Codex or Claude CLI session.

The delegated session claims the task, works from the versioned work pack (not this chat), and delivers through
the same task protocol; its exact host session id and the requested/observed model are recorded.
"""

from __future__ import annotations

import os
import subprocess
import sys

from . import adapters
from .context import work_pack
from .store import Store
from .util import RLError, now_utc


def rl_command() -> str:
    return os.environ.get("SMOOTH_RESEARCH_RL") or "rl"


def prompt_for(store: Store, task_id: str, role: str) -> str:
    rl = rl_command()
    pack = work_pack(store, task_id)["summary"]
    return f"""You are a Smooth Research {role} session working on task {task_id} in {store.project.root}.
The research records CLI is `{rl}` (run it from the project directory).

1. Claim the task: `{rl} task claim {task_id} --role {role}` and note the owner epoch it prints.
2. Do the work described below. Experiment code belongs in {store.project.config['code_root']}/, human-facing notes
   in {store.project.config['docs_root']}/notes/. Register every experiment run before executing it
   (`{rl} run register ...`), record what you observed (`{rl} run record ...`) and import results
   (`{rl} run import-result ...`). Never invent results, versions or model identities; unknown stays unknown.
3. Finish with `{rl} task deliver {task_id} --owner-epoch <epoch> --role {role} --summary "<what changed, what was
   found, what is still open>" --output <files>`; if blocked, `{rl} task block ... --reason <engineering|
   model_unavailable|budget|science>`. Stay inside the constraints document.

{pack}
"""


def delegate(store: Store, task_id: str, entry_text: str, role: str = "researcher", timeout: int = 3600) -> dict:
    entry = adapters.Entry.parse(entry_text)
    if entry.entry == "manual":
        raise RLError("INVALID", "a manual entry cannot run a task session")
    with store.read() as tx:
        task = tx.require(task_id, "Task")
    if task["state"] != "ready":
        raise RLError("CONFLICT", f"{task_id} is {task['state']}; only a ready task can be delegated")
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
    return {"session": sid, "ok": res.ok, "host_session_id": res.session_id, "observed_model": res.observed_model,
            "task_state": task["state"], "log": store.project.rel(log),
            "summary": f"{sid} {entry} on {task_id}: {'finished' if res.ok else 'failed: ' + str(res.error)}; task now "
                       f"{task['state']}; session {res.session_id or 'unknown'}; model "
                       f"{res.observed_model or 'unverified'}\n{text[-1500:]}"}


def start_background(store: Store, task_id: str, entry_text: str, role: str, timeout: int) -> dict:
    adapters.Entry.parse(entry_text)
    log = store.project.dir("calls") / f"{task_id}.delegate.worker.log"
    cmd = [sys.executable, "-m", "smooth_research", "--project", str(store.project.root), "delegate", task_id,
           "--entry", entry_text, "--role", role, "--timeout", str(timeout)]
    with open(log, "ab") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                                cwd=store.project.root, env={**os.environ, "SMOOTH_RESEARCH_RL": rl_command()})
    return {"pid": proc.pid, "log": store.project.rel(log),
            "summary": f"delegated {task_id} to {entry_text} in the background (pid {proc.pid}); follow it with "
                       f"`rl inbox --role coordinator` and `rl show {task_id}`"}
