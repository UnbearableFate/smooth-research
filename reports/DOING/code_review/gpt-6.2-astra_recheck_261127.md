# Smooth Research schema 6 implementation recheck

**Verdict: request changes.** The full suite passed, and the original cancellation, stale-pane and dirty-delivery cases now behave correctly. I still reproduced a schema-5 watcher claiming work after migration, a stale answer passing a different design, and a forced mainline update to a candidate whose tests never ran. These affect the facts recorded by the CLI and the work the plugin can stop.

Reviewed the uncommitted `redesign-v6-impl` tree at `3573dc7` against the plugin principles in `AGENTS.md`, redesign §§5.1, 9, 13.1 and appendix A. Source locations below are relative to `src/smooth_research/` unless another directory is named.

## Disposition of the original findings

| Original finding | Result | What happened on replay |
|---|---|---|
| 1. Schema-5 watcher | **Partly fixed** | Migrating with an established v5 heartbeat terminated the actual v5 watcher with SIGTERM and left its tree task open. Migrating during its first beat still allowed it to claim that task. See A and B. |
| 2. Closing working agents | **Fixed** | After I replaced the recorded pane ID, cancellation rebound the record and left the working agent and its tab open. A separate close with status `unknown` also left it open. |
| 3. Repeat blocker notifications | **Fixed** | Across two block → answer cycles, both questions reached the scientist and both answers reached the researcher. Waking either recipient again sent nothing. The public prep handoff also exposed distinct revisions 5 and 7. |
| 4. Review at the same commit | **Partly fixed** | The original bare commit was refused after retargeting. The qualified value from the first retarget passed a second, different design. See C. |
| 5. Delivery tests on dirty files | **Fixed** | With `test -f marker` and an untracked marker, delivery succeeded, recorded a failed test against the delivered SHA and printed the done-check flag. |
| 6. Concurrent cancellation | **Fixed** | Cancelling during the researcher's completion check and during the scientist's delivery each produced `CONFLICT` on delivery. Both tasks stayed cancelled. Editing the completion check exposes a separate revision gap in D. |
| 7. Run provenance | **Partly fixed** | A nonexistent 40-character SHA was refused. The original dirty evidence retry with `--trial --run` was refused. Supplying the valid SHA of the same dirty worktree reopened that path. See E. |
| 8. Interrupted local acceptance | **Partly fixed** | I exited the accepting subprocess with status 41 after it persisted the candidate and before it moved main. Retrying accepted the private prep and moved main. A recycled PID still prevents recovery. See F. |
| 9. Force after failed merge tests | **Partly fixed** | A failed local public prep stayed delivered and could be force-accepted. A first force in job mode requested a job and kept main in place. An earlier failure can still authorize an untested candidate or a new delivery. The failure handoff can also miss the coordinator. See G and H. |
| 10. Recording tree drafts | **Partly fixed** | The sealed document contained the exact LF draft, including its question. Two synchronized imports returned the same decision and created one hypothesis and one prep. Other orderings still lose the replacement draft or fail the duplicate caller. Valid CRLF drafts cannot be recorded. See I and J. |
| M1. Observer configuration | **Fixed** | Changing `agents.beat_seconds` between real beats produced sleep intervals of 30 and 5 seconds. |
| M2. Printable session command | **Fixed** | I parsed the Claude command emitted by `rl session open coordinator --print`. Its `--session-id` parsed as a UUID. |
| M3. Cold hook launcher | **Fixed** | I invoked `plugin/bin/rl` with an unbuilt plugin environment and an unbuilt project environment. In both, Stop and UserPromptSubmit returned `{}` and SessionStart identified itself correctly. |
| Inbox view cleanup | **Fixed** | A nonempty researcher inbox returned its task card and revision. Both `timed` and `level` were absent from the JSON. |

## Remaining and new findings

### A. High — a starting v5 watcher can acquire a v6 heartbeat

**Locations.** `watch.py:53`, `watch.py:65`, `watch.py:250`, `upgrade.py:145`. The v5 implementations are at `3573dc7:src/smooth_research/watch.py:437` and `:499`.

I started the watcher from `3573dc7` in a disposable schema-5 project and paused it after it had opened its Store, before it wrote its first heartbeat. At that point the legacy launcher had published the opener's PID. I migrated and started the v6 observer against fake Herdr, then let v5 finish one beat. It opened a scientist and changed the legacy task from `open` to `active`, held by `scientist-T-0001`. Its heartbeat contained **the v5 process's PID and `generation: 6`**. V6 reported it alive and `retire_older` found nothing to retire.

V5 preserves unknown fields whenever it refreshes the shared heartbeat. Give v6 its own heartbeat, preserve the legacy heartbeat for identification, and coordinate retirement with a legacy beat already in progress. Exercise the transition before the watcher's first heartbeat as well as between established beats.

### B. High — migration can terminate another project's process

**Location.** `watch.py:68`–`83`. The signal decision uses a PID and substring matches against the entire flattened command line. Neither establishes which project owns the process.

With an established v5 watcher in temporary project A, I copied A to temporary project B, including its heartbeat. Opening **B** for migration terminated **A**'s watcher with SIGTERM. In a second test, a disposable non-watcher with one argument containing `compare smooth watch run logs` also received SIGTERM when its PID appeared in a stale heartbeat. A shared or copied legacy heartbeat lacks a host identifier.

Verify the executable and argument boundaries **and** the process's project binding before signalling. If ownership cannot be established, preserve the process and report the unresolved watcher.

### C. High — the review target contains zero digest digits

**Locations.** `reviews.py:105`, `reviews.py:480`, `research.py:427`. A stored design key embeds `blake2b:<hex>`. Slicing its first eight characters yields `blake2b:` for every design.

On one waiting review I changed `--semantic-tests` to `false`, retargeted, then changed it to `true` and retargeted again. The stored digests began `29daa95a` and `e0074bd9`. Both packs named the target `4262da9d40ac+E-0001@1#blake2b:`. Answering with the first target marked the second design passed. Separately, the second pass of a round accepted the first pass's stale qualified token after another design edit because the current pass had no retarget history.

Use `util.short(digest, 8)` for the hex portion. Validate a supplied design component on every pass, including passes that permit a bare commit. Assert that an old qualified value fails after the second edit.

### D. High — changing a prep's completion check can move main on a superseded test

**Locations.** `tasks.py:644`, `tasks.py:708`, `tasks.py:983`. `task set` changes the prep's requirement and check without advancing the revision used by delivery and acceptance.

I ran `task set T-0003 --done-check false` while delivery was running the previous check, `true`. The revision stayed at 3. Delivery recorded a pass for `true` and no flag for the newly required check. In a separate acceptance I made the same edit after the candidate had been recorded. The revision stayed at 5, the CLI reported `accepted and merged`, and main moved with `done_check=false` and no force flag.

Make edits to the completion criteria invalidate the in-flight operation. Recheck the criteria or their version at the final write, including before moving main. A conflicting edit should leave the task in a state from which the new check can run.

### E. High — an explicit valid SHA bypasses the local evidence provenance check

**Location.** `runs.py:89`–`97`. The dirty-worktree check runs only when the caller omits `--commit`.

I registered a clean evidence run, edited its tracked simulator, then retried with a new request ID, `--trial --run RUN-0001 --commit <original task HEAD>` and no compute resource. The CLI created `ATT-0001-2`. Running the simulator locally executed an observable side effect present only in the dirty file. The record still named the clean SHA, category `evidence`, with no deviation or provenance flag.

Apply the provenance check to local evidence and prep attempts even when the caller spells out the same SHA. An executor that checks out a named commit can establish that separately. Keep the exemption for actual trials.

### F. Medium — PID reuse can leave local acceptance waiting indefinitely

**Location.** `tasks.py:1070`–`1083`. For a local PID, a successful signal-0 lookup alone means that the recorded acceptance is still running. The recorded start time is only displayed.

I simulated the persisted state after PID reuse with a completed delivery, a recorded local acceptance from 2000 and the PID of a newly spawned disposable `sleep`. `task accept` reported that the acceptance was running in that process. The task stayed delivered and main stayed put. The age fallback never runs for this case, including after a reboot that reassigns the PID to a long-lived process.

Record and verify a process identity that distinguishes PID lifetimes, or hold a task-specific operating-system lock for the acceptance. Preserve the existing retry behavior when the original process really is gone.

### G. High — an old failure authorizes force on a different candidate or delivery

**Locations.** `tasks.py:862`, `tasks.py:868`, `tasks.py:1050`. The failure lookup matches only task and delivered Git SHA. A task can be redelivered at the same SHA, and a mainline change produces a different integration candidate for the same delivery.

I failed the job for candidate `ba50142c91ef`, advanced main with another commit, then ran `task accept --force --reason …`. Main moved to `3b253e35f998`. The two Git trees differed and the only merge-test Run named the first candidate. In another project I returned the failed delivery and redelivered at the same SHA with a changed conclusion. The first force of this new delivery also moved main using the old job.

Bind the override to the delivery event or revision and the tested integration candidate. Reuse the failed candidate when its inputs still match. A new delivery or changed mainline should follow the ordinary job path until its own test outcome exists.

### H. High — a failed public prep can wait on a coordinator who never gets the handoff

**Locations.** `runs.py:309`, `runs.py:322`, `tasks.py:719`, `inbox.py:270`.

With fake Herdr and no observer, the researcher recorded a failed public prep merge-test job. The command said it waited on the coordinator, returned `wake=['researcher-T-0001']` and `notified=[]`, and sent zero prompts. The coordinator's inbox had a new merge-conflict item. In a separate local merge, I first handed the public prep to a scientist to answer a blocker. After merge tests failed, the command again named the coordinator, but only the scientist received the handoff and the coordinator's inbox had no item for the task.

Use the same post-failure recipient for the command handoff and the inbox. For the coordinator-owned decision in §13.1, notify the coordinator when local testing or a job records failure, including after a scientist helped with a blocker.

### I. High — tree cleanup can delete the next draft, and a concurrent reader can miss the recorded decision

**Locations.** `tree.py:308`, `tree.py:311`, `tree.py:330`, `tree.py:342`.

I paused cleanup after its digest comparison, atomically replaced `tree.yaml` with a new draft containing a different question, then let cleanup continue. It recursively removed the directory. The old decision survived and the new draft was gone. I also paused one recorder after parsing but before sealing. A second recorder committed `D-0001` and cleaned up. The first then failed with `document not found` before reaching the duplicate lookup, although it had read that exact draft.

Parse, identify and seal one captured version. Give cleanup ownership of that version before removing it, or retain the file when safe removal cannot be established. A recorder that already captured the bytes should be able to resolve their durable decision after another recorder finishes.

### J. Medium — unchanged CRLF drafts always conflict

**Locations.** `tree.py:135`, `tree.py:329`, `documents.py:55`. Text-mode reading normalizes newlines for one digest while document sealing hashes the original bytes.

I wrote the valid fixture draft with CRLF line endings. `rl tree check` exited 0. Without any intervening edit, `rl tree record` exited 3 saying the draft changed while it was read. Retrying the unchanged file gives the same result. Hash and seal the original bytes and decode that same snapshot for YAML parsing. Verify the sealed object's bytes against a CRLF input.

## Validation and architecture

`uv run pytest -p no:cacheprovider -q` passed **222 tests in 203.43 seconds**, including all 19 review regressions. `git diff --check HEAD` passed. Ruff reported 111 diagnostics across source and tests. I ran the behavioral reproductions separately in disposable projects with the five specified session variables unset. Herdr interactions used a fake adapter. The mixed-version tests ran the actual schema-5 watcher code from `3573dc7` in disposable child processes. Live host UI behavior and cross-host process control were outside this verification.

The committed records, derived inbox and command-owned handoffs can support the design without a scheduler. The fixes checked here are weakest when two paths identify the same thing differently. The most useful simplifications are to share one design-token formatter, one public-failure recipient and one captured draft across parsing, sealing and cleanup. The regression tests should also exercise a second retarget, a moved mainline, a concurrent replacement draft and a watcher that has started but has not yet published its own PID.
