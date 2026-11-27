# Code review of the `rl` CLI, hooks and observer

**Verdict — request changes before upgrading a running project.** The full suite passes. In isolated projects I reproduced a schema-5 watcher claiming work after migration to schema 6, a cancellation closing a working agent, and a blocker answer that never reached an idle researcher. I also found paths that record a passing check or review against work that was never checked.

Reviewed the uncommitted `redesign-v6-impl` tree at `3573dc7`, including the new modules, tests and schema-6 fixture, against the plugin principles in `AGENTS.md` and the redesign through §13.1 and appendix A. Python file locations below are relative to `src/smooth_research/`.

## Findings

### 1. High — a schema-5 watcher keeps scheduling after migration

`watch.py:199`, `upgrade.py:122`, `store.py:395`. Both versions use the same heartbeat. A fresh v5 heartbeat makes v6 report that its observer is running. The migration lock protects the database conversion, but the existing watcher keeps a Store that has already cached its schema check. The next v5 beat still runs the old scheduling logic.

I opened a project with the actual code from `3573dc7`, released a legacy tree task, and migrated it with the working tree. V6 reported schema 6 and “the observer runs”. One subsequent v5 beat against fake Herdr opened a scientist and changed the task from `open` to `active`, with `scientist-T-0001` as holder. Under §9 the coordinator should instead see the legacy task and be able to retire it. Identify the observer generation in its heartbeat and provide a live-upgrade transition that retires the old scheduler or isolates its writes. Preserve the research sessions. Test the transition with both generations alive.

### 2. High — automatic cleanup can close a working agent

`sessions.py:358`, `launch.py:138`, `launch.py:151`. Cleanup exempts only the reported states `working` and `blocked`. A stale pane binding becomes `gone`, and `unknown` also falls through to `tab close`. The tab ownership check verifies the agent's name without checking its current activity.

With fake Herdr I left the scientist working in its owned tab and made only its recorded pane ID stale. Cancelling its task closed the tab and removed the working agent. An agent reporting `unknown` was also closed. Resolve the live binding before automatic cleanup and require positive evidence of `idle` or `done`. Preserve the tab if the lookup is inconclusive.

### 3. High — blocker answers and repeat blockers reuse an already-delivered notification

`inbox.py:246`, `inbox.py:281`, `sessions.py:82`. Dispatch marks the researcher's ordinary task card as told. Answering a blocker changes its note, but the card keeps the same key. Blocking again for the same reason also reuses the scientist's first blocker key. The command, Stop hook and observer all consult the same told map.

In a dispatch → block → answer → block sequence with idle fake agents, the first blocker arrived. The first answer and the second blocker both returned “told already”. The changed notes remained visible to someone who manually opened the inbox. Include the handoff's committed revision or event sequence in these keys. Test the actual recipient prompt across two cycles, including deduplication on a repeated read of the same event.

### 4. High — a stale review answer can pass a revised design at the same Git commit

`reviews.py:413`, `reviews.py:419`, `reviews.py:462`, `pack.py:317`. Retargeting replaces the pending design key and snapshot when experiment metadata changes. The only identifier the reviewer is asked to return is the Git commit. Changing the semantic test with `rl experiment set` requires no Git change.

I requested review, changed the semantic test to `false`, retargeted the pending round, and submitted the original reviewer's `--at` value. The answer passed, the two design keys differed, and `review_flags` returned no flag for the revised design. Give the reviewer an immutable identifier for the requested commit and design revision, and verify it under the answer transaction. Preserve the ability to read existing rounds.

### 5. High — delivery credits a committed revision with a check run on dirty files

`tasks.py:674`, `tasks.py:689`, `tasks.py:704`. A preparation delivery captures the branch head, runs its completion check in the mutable task worktree, and records the outcome against that head. I used `test -f marker` with the marker present only as an untracked file. The task's test history recorded a pass for the delivered commit. Reading that commit with Git confirmed it contained no marker.

This corrupts the delivery evidence even if a later merge check catches the failure. Run the delivery check in a clean checkout of the captured commit, as the local merge check already does. Continue accepting delivery of the committed portion and warning about the dirty files.

### 6. High — a delivery can overwrite a concurrent cancellation

`tasks.py:660`, `tasks.py:697`, `tasks.py:705`. The command checks whether delivery is allowed before running the completion check. Afterward it rereads the task but unconditionally writes `delivered` using the newer revision. I cancelled the task while the check was in progress. Both commands succeeded, history contained `task.cancelled` followed by `task.delivered`, and the final state was `delivered`. The coordinator's cancellation was lost and the task could be accepted.

Carry the revision seen at the start through the final write and compare it under the lock, as acceptance already does. Apply the same check to the scientist delivery write at `tasks.py:760`.

### 7. High — run registration can record evidence with invalid code provenance

`runs.py:82`, `runs.py:87`, `runs.py:94`. The dirty-worktree check uses the requested trial flag before the existing run determines the retry's actual category. Separately, a full-length hexadecimal commit string skips Git resolution entirely.

I edited a tracked file after registering a clean evidence run. Ordinary registration rejected the dirty tree. Retrying that run with `--trial --run RUN-0001` created a second **evidence** attempt with `deviates=false` and no flag. Supplying forty `f` characters as `--commit` also created an evidence run although Git could not resolve the object. Resolve the effective category first, apply the provenance check to retries, and verify every explicit commit against the repository. Retain the clean-tree exemption for actual trials.

### 8. High — an interrupted local acceptance waits for a job that cannot exist

`tasks.py:790`, `tasks.py:858`, `tasks.py:1067`, `runs.py:75`. A local acceptance persists `state=delivered, merge_state=merging` before testing its candidate. If interrupted before the mainline moves, retry enters reconciliation. Only public preparations restart from this point. A scientist's preparation or execution task falls through to the message that its merge-test job is pending.

I interrupted a private preparation's local check, then retried `task accept`. It reported a pending job. Registering that job returned `CONFLICT` because the task was still `delivered`. Resume local testing of the recorded candidate, with the existing revision checks before advancing. Reserve the job-wait path for acceptances actually configured to run as jobs.

### 9. High — the force path mishandles both failed and unrun merge tests

`tasks.py:875`, `tasks.py:997`, `tasks.py:798`, `tasks.py:847`. On a public preparation, a failing merge check sends the task back to `active` and prints an instruction to use `task accept --force --reason`. Following that instruction immediately returned `CONFLICT: T-0001 is active, not delivered`. The tested delivery and its explicit override should remain addressable after a test failure. Keep merge conflicts on their repair path.

Conversely, under `compute.tests: job`, passing `--force --reason` on the *first* acceptance moved the mainline with zero merge-test jobs. The CLI reported “the mainline moved past failing tests”, while its stored flag said the tests had never run. Tie the override and its event to the failed candidate and actual test outcome. Cover public preparations and job mode alongside the existing local, scientist-created preparation test.

### 10. High — recording a tree loses questions and can import one draft twice

`tree.py:232`, `tree.py:312`, `tree.py:322`, `tree.py:379`. The parser reads the draft's questions, but recording writes no copy of them before deleting the directory. I recorded a draft without approval that included “Which holdout remains eligible?” The draft was gone and the question was absent from the complete records export.

The database transaction serializes insertion but does not claim the draft it consumes. I synchronized two recorders immediately after they read one draft. Both succeeded, returning `D-0001` and `D-0002` and creating two identical hypotheses and two preparation tasks. A crash after commit and before cleanup leaves the same retry window. Persist the exact draft, including its questions, with the route decision. Give that draft a durable import identity, return its original result on retry, and clean up only the version that was recorded.

## Additional integration findings

| Severity | Location | Observed behavior and fix |
| --- | --- | --- |
| Medium | `watch.py:174`, `watch.py:188` | The loop keeps its initial `Project.config`. Changing `agents.beat_seconds` from 30 to 5 between two beats produced sleep intervals of 30 and 30. The same stale configuration controls reminders and coordinator quiet time. Reload configuration once per beat and use that snapshot through the following sleep. |
| Medium | `sessions.py:428`, `launch.py:112` | The no-Herdr fallback printed the literal argument `--session-id '<new uuid>'` for a Claude coordinator. The installed Claude CLI requires a valid UUID, so the advertised copy-and-run recovery command needs hand editing. Generate the UUID or omit the optional argument. Check the parsed command in the fallback test. |
| Medium | `plugin/bin/rl:239`, `plugin/bin/rl:259`, both host hook manifests | With a cold plugin environment, invoking the repository's plugin launcher for each of `hook stop` and `hook user-prompt` returned a payload identifying the event as `SessionStart`. The project-environment bootstrap uses the same unconditional response. Dispatch bootstrap output by hook event and return an empty response for a Stop hook that cannot read records yet. Exercise the shell launcher as well as the Python handlers. |

## Architecture and validation

The command → committed record → recipient notification split is a sound basis for running without an observer. The failures above occur where the implementation loses an identity already available to it, such as a delivery revision, a design revision or a specific handoff. Reusing those values would simplify recovery. The legacy `Item.timed` and `Item.level` fields at `inbox.py:40` also still appear in `rl inbox --json` after the slot and escalation rules were removed. Retire them from the v6 view after checking its consumers.

The full suite passed **203 tests in 180.77 seconds**, including the frozen schema-5 and schema-6 projects. `git diff --check HEAD` passed. Ruff 0.17.6 reported 107 diagnostics across source and tests. The behavioral findings above came from separate temporary projects, Git checkouts and fake Herdr, including a mixed-version run using the code from `3573dc7`. No live research project was used.

The most valuable missing regressions are a running v5 watcher crossing migration, repeated blocker handoffs to the same live session, stale-pane cleanup, and a process interruption between candidate creation and the mainline update. The Herdr skill requires `HERDR_ENV=1` for pane control. That variable was unset in the execution shell, so the live Codex and Claude hook trials and a separate Herdr reviewer session remain unverified.
