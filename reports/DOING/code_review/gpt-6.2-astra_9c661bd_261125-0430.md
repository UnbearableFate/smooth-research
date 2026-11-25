# Schema 5 workflow review

**Verdict — Request changes before the first unattended project.** The watcher cannot launch the first scientist with the installed Herdr. Separate failures can move the mainline to code whose acceptance test never passed, mark a running job successful, and turn an unverified result into confirmatory evidence. These failures also occur when the underlying Git and database operations succeed.

Reviewed `src/smooth_research` at `9c661bd82693f0c44a0842b0759d48840c2bfbc3` on `redesign-v5`, against the November 10 redesign and the November 25 implementation plan. Locations below refer to that commit, relative to `src/smooth_research/`. Reviewer model was `gpt-6.2-astra` with xhigh reasoning.

## Session startup and recovery

### 1. Critical — Herdr rejects the names of every task and ticket session

`roles.py:32` preserves the uppercase record prefix in names such as `scientist-T-0001` and `reviewer-RV-0002`. `launch.py:135` sends that string verbatim as the Herdr agent name. Herdr 0.9.5 returned `invalid_agent_name` for the first example during argument validation. Its grammar is `[a-z][a-z0-9_-]{0,31}`. The coordinator happens to have a valid name. On the first tree assignment, the watcher claims the task, creates the tab, handles the launch failure by ending the session record, and tries again on a later beat (`watch.py:167–188`, `launch.py:137–141`). The tree stays active without a scientist.

Give each live Herdr agent a valid, project-scoped name and retain the existing role-and-record name in the database, session environment and pack. Include the coordinator in the mapping so two projects can run in the same Herdr server.

### 2. High — Recovery can operate on another tab after Herdr restarts

`watch.py:216–224` closes any existing tab at the saved ID. `watch.py:247` and `watch.py:393` likewise accept a live occupant at the saved pane ID. The adapter itself documents ID reuse across restarts at `herdr.py:166–174`. The session has a saved host session ID, but these paths do not compare it. With a fake Herdr returning a tab labeled for a different project at the saved ID, cleanup issued a close for that tab. The same identity error can direct a project prompt to another conversation or make resume report that an unrelated coordinator is running.

Before reading, prompting, answering or closing, reconcile the live workspace, tab, pane and agent with the project session. On a mismatch, expire the database binding and leave the live occupant alone.

### 3. High — One unaccepted dependency can stop scheduling for the whole project

`inbox.py:165–168` offers every open hypothesis, tree or phase task to its scientist. `watch.py:167–172` immediately calls `claim`, which correctly rejects unaccepted dependencies at `tasks.py:165–167`. The exception escapes the per-session loop at `watch.py:317`. In a disposable project, releasing a hypothesis task that depended on a queued public prep task caused the beat to exit before a later independent tree task. The heartbeat was never written. The next beat selects the same dependent task again.

Use the same prerequisite-readiness predicate for inbox eligibility and claim. Handle a lost claim or a dependency that changed during the beat locally, so the watcher still visits other recipients and updates its heartbeat.

## Commits, tests and acceptance

### 4. High — Acceptance merges the current branch head, even after it moves beyond the delivery

Delivery records `delivered_commit` after checking the worktree and, for scientist tasks, the allowed directory (`tasks.py:489–500,547`). Acceptance calls `gitops.merge_on_task_branch` at `tasks.py:608`. That helper checks for dirty tracked files and merges main into the current task HEAD (`gitops.py:175–185`). A clean later commit passes. After delivering a reviewed tree, I committed a file outside its allowed directory on the task branch. Coordinator acceptance put that file on main and marked the task accepted. The stored delivery and accepted tip differed even though main had not diverged.

Construct the acceptance candidate from the recorded delivery and the current mainline commit. Reject a moved task head or require redelivery. Before moving main, confirm the task still has the same delivery and state so a concurrent return or cancellation remains effective.

### 5. High — An earlier test pass authorizes dispatch after the current test fails

`tasks.py:284–285` looks for any passing result with the same commit. The record includes the command and outcome, but the predicate ignores command changes and later failures. In a CLI workflow, a scientist passed the original test, changed the draft experiment's semantic-test command to `false`, ran it, and got a recorded failure. With the review already complete and the Git commit unchanged, `task dispatch` still sealed the experiment at `tasks.py:348`. The same predicate gates job-mode prep delivery at `tasks.py:470`.

Bind a test result to the commit and the applicable test definition. Evaluate the latest applicable completed test when dispatching or delivering. A changed definition should require a new passing result.

### 6. High — Job-mode acceptance can consume the wrong test or strand the right one

`runs.py:56–67` accepts an explicit commit for a merge-test registration. On completion, `runs.py:192–200` checks only the purpose and that the task is currently merging. `tasks.py:658` advances main to the task's current merge candidate. I registered a test for the previous mainline commit and reported success with `run ended`. This accepted the task and advanced main to a different commit.

The reverse failure is reachable through a supported observation path. Recording success for the correct attempt with `run record`, then delivering `run ended` for it, left the task in `merging` and main unchanged. The duplicate-end check at `runs.py:171–179` skips the only reconciliation call. A crash between persisting the end and processing acceptance has the same effect. The doctor exempts every merging task at `inbox.py:583–584`.

Bind the acceptance attempt to the exact merge candidate and delivery generation. Reconcile from the persisted terminal observation on either input path and on restart. Make reconciliation idempotent, including the case where Git already advanced before the database was updated.

## Run provenance and scientific decisions

### 7. High — Miyabi notifications can report success for a job whose state is unknown

`runs.py:161–175` finds every bound job ID mentioned in a message and assigns the same outcome to each, always writing an ended observation. `_outcome_in` at `runs.py:183–189` searches the entire text. The Miyabi command-delivery producer includes a footer containing the word “success” even on a state-unknown notice. Its batch notifications include individual job outcomes. I passed messages generated by the local Miyabi renderer through this handler. A running attempt became `ended/success` on an unknown notice. In a fresh mixed batch, one success and one failure became two failures. A later correct notification cannot repair an already-ended attempt through this path.

Consume structured per-job observations, or decode the individual status rows with an explicit terminal-state rule. Leave unknown and still-running jobs pending. Only an authoritative successful test observation for the bound candidate should trigger acceptance. Producer evidence is in `miyabi-toolbox` at `6df57e9`, `miyabi-mcp/src/miyabi_mcp/notify.py:472–500` and `deliver.py:137–143`.

### 8. High — Missing configuration and missing result provenance still yield valid evidence

`manifest.py:77–83` skips the digest comparison entirely when config is omitted, including comparison of an explicitly supplied split. A run registered against a sealed control arm with `lr=1` and split `dev` accepted a different split and omitted config as non-deviating evidence work. Import then treats missing fields as informational at `runs.py:429–445,482–490`. A result containing only `{"status":"complete"}` was marked usable and `rl evidence add` accepted it as confirmatory evidence. Conversely, an explicit config mismatch produced the unhelpful `error[INVALID]: False` because of the comparison expression in the diagnostic at `runs.py:94–96`.

Materialize the effective arm configuration at registration or require it explicitly, and always check the split. Require the evidence-producing result to report the fields needed to verify its execution against the registration. Keep incomplete imports readable with an explicit unverified status that prevents citation.

### 9. High — Revising an experiment changes how earlier runs are validated

`research.py:258–267` clears the sealed manifest in place when a scientist starts a new revision. Registration records the experiment ID, but no immutable revision or manifest reference (`runs.py:115–124`). Import reads whichever manifest the experiment holds at that moment (`runs.py:439–444`). I registered an arm with `lr=1`, opened revision 2, then imported its outstanding revision-1 result with `lr=99`. The importer recorded zero mismatches and marked it usable. Once revision 2 is sealed, a late result from revision 1 is instead checked against revision 2. Researcher and conclusion-review packs also read the current experiment (`pack.py:231–237,305–310`).

Give dispatched tasks and registered runs an immutable reference to their sealed revision. Use its commit, manifest and test definition in import, verification and recovery packs. Preserve each old revision while the next one is being edited.

### 10. High — A retry or the fast lane can ignore a known blocking review

`runs.py:76` skips review and manifest checks for a new attempt of an existing run. With a new open high method finding, registering a new run was refused, yet registering the same spec with `--run` created another evidence attempt. Separately, `runs.py:83–88` replaces a failed review check with a protected-code comparison when the task was dispatched on the fast lane. On such a task I opened a new review, recorded a high method finding and verified that `covers` reported it as blocking. Registration still created a non-deviating evidence run.

Check current blockers for every new evidence attempt. The fast lane can supply prior commit coverage under the §6.6 conditions. An unresolved finding still needs a resolution. Reserve idempotent replay for returning an already-created attempt with the same request ID.

### 11. High — A reviewed verdict can be applied to a different hypothesis statement

Draft creation pins the hypothesis revision on its `DECIDES` edge (`research.py:409–410`). Finalization reads the current hypotheses, updates their state and attaches evidence at their current revisions (`research.py:432,452–461`). The intervening change is legal while the hypothesis is untested. I obtained a passing conclusion review, revised the hypothesis statement, then finalized the original decision. Revision 2 became supported. The final decision's `DECIDES` edge still named revision 1, and its evidence's `SUPPORTS` edge named revision 2.

Compare all target revisions with the reviewed decision in the finalization transaction. A changed scientific statement or scope should invalidate that approval and require a draft and review for the new revision.

### 12. High — Compute instructions lose the registered commit

`runs.py:98,120` stores the commit at `spec["commit"]` and `Run.code_commit`. The retained compute adapter at `compute.py:139–146` looks only for the old `spec["code_ref"]["commit"]`. After registering a real Git commit, `rl compute steps --attempt` returned `commit: null`. Remote staging consequently substitutes the literal `<commit>` in the snapshot path and commands (`compute.py:166–175,244–253`). The advertised registration-to-staging path loses the identity needed to run the reviewed code.

Have registration and the compute adapter read one schema-5 commit representation. Exercise the generated staging instructions for an attempt after advancing the caller's checkout.

## Other verified findings

| Severity | Location | Observed behavior and impact | Recommended fix |
|---|---|---|---|
| High | `watch.py:308–326`, `inbox.py:493–500` | An open coordinator with an empty inbox and a newly blocked host was never queried in a beat with a fake Herdr. Its user inbox stayed empty. The watcher checks a session without items only when it already has a recorded dialog, so the first dialog can go undiscovered indefinitely. | Observe the state of every open session independently of whether work is due. Limit prompting to sessions with actionable items. |
| High | `inbox.py:56–58,199–205`, `watch.py:208–212,234–239` | Task-card age uses the latest event for the task. Each reminder writes a new notice event against the same task. Across three simulated four-hour deadlines with no work performed, each reminder reset the age to four hours and the creator received zero escalations. | Derive waiting time from the transition that created the obligation. Track reminder delivery and retry times separately. |
| Medium | `cli.py:277–279`, `tasks.py:157–169`, `inbox.py:192–205` | An existing researcher claimed an open public prep task assigned to a different researcher identity. The claim succeeded, the intended watcher recipient lost the task, and doctor reported that the active prep task waited on nobody. | Validate the claimant against the expected role and subject, including the creator scientist for a dependency-delayed child task. Share the rule with the watcher. |
| Medium | `usage_view.py:32–38`, `runs.py:561–563,579–584` | One accounting record for 3 GPUh covering two attempts of the same task yielded 3 GPUh project-wide and 6 GPUh on its hypothesis. The inflated amount feeds envelope alerts as well as the page. | Deduplicate current usage IDs across the aggregation scope. Define how charges shared between tasks are displayed before comparing them with a per-task budget. |
| Medium | `watch.py:327–328,332–345`, `page.py:77–105` | Two consecutive idle beats rendered the whole research page twice. The second heartbeat write discards both `page_seq` and `page_at`. Every beat therefore reloads the graph, including all events, and rewrites the page while the records are unchanged. | Persist the heartbeat and redraw metadata together. Test both an event-driven redraw and the configured interval with an unchanged project. |

## Architecture and simplification

The database transaction is a sensible boundary for records and graph edges. The derived inbox lets status, packs and the research page read the same obligations. Task-specific Git worktrees and sealed document objects give code and prose identifiable versions. The failures concentrate where a consumer substitutes mutable or external state for a recorded identity, particularly at dispatch, acceptance, result import and Herdr recovery.

The smallest simplifications target those boundaries. Use one reader for a registered run's commit, one immutable design reference for all readers of that revision, and one reconciler for a pending acceptance. Let one function write the watcher's heartbeat. Two small deletion candidates are `reviews.current_round` (`reviews.py:65`) and `reviews.parse_findings` (`reviews.py:393`). Neither has a production caller in this commit. The active CLI already parses findings at `cli.py:345–349`.

## Test gaps for the schema-5 suite

| Area | Scenario and expected observable result |
|---|---|
| Herdr lifecycle | Run the first tree assignment with a fake that enforces Herdr's real name grammar and uniqueness. Start two projects with overlapping record IDs. Restart with recycled pane/tab IDs and a missing coordinator. Only the correct live occupant receives work. An idle coordinator's first dialog appears in the user's inbox. |
| Scheduling and time | Release an independent task beside a hypothesis waiting on public prep. Verify that the independent one starts, the heartbeat advances, and the dependent one starts after acceptance. Use a controllable clock to verify reminder, creator, coordinator and user escalation deadlines. |
| Delivery and Git | Advance a task branch after delivery, move main concurrently, and return the task during acceptance. Verify the accepted content and task state. Exercise conflicts, a newer failing test at the same commit, and a crash on either side of moving main. |
| Job completion | Feed real-format unknown, mixed-outcome, duplicate and out-of-order notifications through the stdin entry point. Complete the same merge test via observation import and via notification. Verify the exact candidate accepted, and recovery after the terminal observation was already committed. |
| Evidence and reviews | Vary seed, split, effective config and reported fields independently. Import old results while a new design revision is open and again after it seals. Check normal registration, fast-lane dispatch and retries against an open method finding. Exercise three-pass escalation, advisor withdrawal and public prep escalation with the assigned short scientist. |
| Decisions, packs and page | Revise a target between review and finalization. Reopen each role's session and inspect its pack for the original sealed material, assigned work and waiting jobs. Check page content, timer-driven staleness, and usage when several attempts share one charge. |
| Infrastructure | Stage from a recorded attempt in a disposable repository whose HEAD has moved. Round-trip a schema-5 snapshot into a new root, verify sealed content, and exercise the pre-5 rejection and future migration mechanism described in §10.4. |

## Validation and limits

I used CodeGraph to trace entry points and dependencies, then ran the probes against a Git archive of the requested commit with Python 3.13.14, real LatticeDB stores and disposable Git repositories. The probes drove the CLI through tree review and acceptance, hypothesis release, design review and dispatch, run registration, result import, evidence creation and verdict finalization. Herdr name validation used the installed CLI. Session recovery and reminder timing used controlled fakes. Miyabi status probes used the local producer's renderer and command-delivery format. No live model assignment or cluster job was run.

Ruff 0.17.6 passed the syntax and undefined-name checks (`E9,F821,F822,F823`). The configured full lint run reported 77 diagnostics. The legacy `tests/` suite was excluded as requested. The findings apply to the pinned commit and do not assess concurrent edits in the working tree.
