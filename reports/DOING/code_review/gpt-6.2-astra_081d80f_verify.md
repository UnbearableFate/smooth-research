# Schema 5 workflow verification, round six

**Keep draft edits separate from local semantic tests. Hold job-mode and remote rollout.** At `081d80f`, a test can still authorize a manifest it never ran. Job-mode acceptance now keeps main in place while a retry is pending, but that ordinary wait can stop an entire watcher beat. Candidate isolation between projects and cross-timezone result ordering passed verification.

Reviewed `081d80f45d9808631b0dea611c3559c04009681c` in a detached checkout. Source locations below refer to `src/smooth_research/` at that commit. [Round-five findings](gpt-6.2-astra_b47907a_verify.md) · [Schema 5 design, §§6.5, 8.8 and 9.1](../../../design/site/content/smooth-research-redesign-2026-11-10.md)

## A · Local trial

**A1 · Partly fixed · High.** When a manifest changed during a local test and stayed changed, the CLI returned `CONFLICT`, preserved the prior test history and refused dispatch. When the scientist restored the original manifest **before the subprocess finished**, the beginning and ending keys matched. The subprocess had already read and passed on `k=1`. The CLI credited the pass to the reviewed `k=19` draft, then allowed dispatch and an evidence run with `k=19`. Running the same check directly on that draft failed.

There is also a second path around the check. Supplying a valid `--attempt` without `--record` still ran the local command and recorded its pass after a persistent draft change. Use a monotonically increasing draft edit counter or give the subprocess an immutable design snapshot. Validate the local and job-report argument combinations at entry. The round-five precaution for the ongoing local trial still applies. `tasks.py:287–305,312–333,427`, `research.py:247–267`

## B · Job-mode and remote use

**B1 · Fixed.** A pass at `19:00+09:00` no longer cleared a failure at `10:01+00:00`. Moving the failure to `10:00+00:00` also blocked dispatch because the instants were equal. Reversing the reporting order of tied results kept the gate closed, and a genuinely later pass opened it. Both sorting and the tie check use the parsed instant. `tasks.py:338–361`

**B2 · Partly fixed · High.** Both write-time guards held. A running retry kept the task in `merging` and main at its previous commit. Registration that raced with acceptance was refused, as was registration overtaken by a new round with the identical candidate hash. Once the last attempt reported normally, two successes accepted the task and a failure recorded a conflict.

The regression is in returning `CONFLICT` for a successful observation while another attempt is pending. The observation had already committed, yet `rl run record` reported failure. Two successive `rl watch once` calls then stopped before inbox delivery. A two-success executor notification recorded only its first row until the notification was replayed. After an interruption with both a success and a failure already persisted, the watcher repeatedly visited the success first and never applied the failure. Under the write lock, record a conflict on any terminal failure and accept when all attempts succeed. Return normally while attempts are pending so the batch and watcher can continue. `tasks.py:819–822,874–880,887–896`, `runs.py:250–263,477–479`, `watch.py:307–314`

**B3 · Fixed for recorded running jobs. A submission-recovery gap remains · Medium.** An overdue running merge test produced a `job-overdue` item for the private scientist and for the public prep researcher. A stop between external execution and the first `rl run record` still strands the round. Two hours after its deadline the attempt remained `prepared`. Registration had cleared the merge-test item, and the overdue rule required `last_observed_at`. Give the submitter a deadline item to check whether a prepared job was submitted and record what happened. `runs.py:173–180`, `inbox.py:329–339,345–362`

**B4 · Fixed.** Two stores in the same Git repository held different candidates for `T-0003`. Each listed its own ref. Sync published only the calling project's commit, and cancelling its task preserved the sibling's ref. Running the sibling watcher between the first project's repair and push also preserved the exact commit registered for the worker. `gitops.py:200–232`, `compute.py:450–463`

**B5 · Partly fixed · Medium.** Expected-tip deletion preserved a ref that changed during cleanup and surfaced the Git error. A locked ref made cancellation fail with the task still `merging`. From the main checkout, an error repairing an orphan ref appeared in the sync result and the bare origin received no candidate. Remote publication still has three gaps.

- From a linked checkout of the locked orphan branch, sync pushed the current branch **before** checking candidate refs. The origin received the cancelled candidate even though the command reported that none was pushed. Route any current branch in the candidate namespace through candidate reconciliation. `compute.py:435–459`
- After a successful repair, I interrupted a concurrent acceptance after Git created its ref and before the database committed. Sync then enumerated the leftover ref, published it and reported success. The task was still `delivered`. Take the candidate names and commit IDs to publish from one locked read of committed records and push those exact IDs. `tasks.py:736–741,788–806`, `compute.py:453–463`
- After normal cancellation removed a previously published ref locally, two successful syncs left it on the origin. Reconcile retired remote refs within the project's namespace using an expected-tip check. `compute.py:450–463`

## Validation and regression coverage

All **97 repository tests passed**. I also ran **26 disposable cases** using the CLI, Git repositories, bare origins, controlled interruptions and a fake Herdr. The serial all-local workflow reached an accepted execute task and a final reviewed verdict. Pyflakes and Ruff's `E9,F401,F63,F7,F82` checks passed. The live trial on `b47907a` was outside this verification.

The highest-value permanent tests are a draft restored before test exit, a two-attempt notification and watcher restart with mixed outcomes, a submission interrupted before its first observation, and a ref created by a rolled-back transaction during sync. Assert the next watcher action and the origin's ref alongside the database state.
