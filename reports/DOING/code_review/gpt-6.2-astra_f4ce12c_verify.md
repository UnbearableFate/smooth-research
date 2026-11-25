# Schema 5 workflow verification, round three

**High-severity issues remain. I would fix them before the live plugin trial.** A reviewer can be shown one design while their verdict clears another. A failed test can still be superseded by an older pass. Recovery can also accept a task with its merge test still running, and competing acceptances can remove the Git ref a remote worker needs.

Reviewed code commit `f4ce12c6885b942ec1a99d232faad1904e4e85a3` and the design update at `76c2611`. Source locations below are relative to `src/smooth_research/` unless indicated. [Round-two findings](gpt-6.2-astra_5cd8f81_verify.md) · [Schema 5 design](../../../design/site/content/smooth-research-redesign-2026-11-10.md)

## High-severity findings

### R4 · Design clearance — partly fixed

The original revision-change race now refuses the evidence write. Retrying a revision-1 run after a revision-2 handoff also returned a conflict and preserved the attempt count. On an ordinary handoff, revision 2 required a fresh test and review. Two sequential CLI workflows still cleared work the reviewer did not see. `runs.py:145–158,289–293`, `tasks.py:397–411`

- **The review ticket and reviewer pack identify different revisions.** While the researcher held a task sealed at revision 1, the scientist opened revision 2 with a different question and protected scope. The researcher's next review request recorded `E-0001@2`, but the reviewer's pack showed revision 1 and its original scope. After the verdict, blocker answer and a fresh test, dispatch and evidence registration both succeeded for revision 2 using that verdict. Store the reviewed design on the pass and use it to render the pack, classify findings and check coverage. `research.py:360–373`, `reviews.py:74–78,169–173,204–208,224–229`, `pack.py:289–299`
- **A reviewed draft can change without changing its revision or Git commit.** I tested and reviewed revision 2 with one manifest, then used `experiment set` to select a different manifest already present in the same commit. Dispatch succeeded and the evidence run used the replacement arm's `k=19`. The reviewed manifest specified `k=1`. Bind clearance to the actual draft contents, or invalidate it on a design edit. The public revision number can keep its existing meaning. `research.py:247–268`, `reviews.py:63–83`, `tasks.py:399–429`

### 5 · Stale test pass permits dispatch — partly fixed

An identical report for one attempt now has no effect, and a contradictory repeat returns a conflict. Ordering **distinct** attempts still falls back to report arrival when their close times tie. I recorded a success observed at 125 ms and a failure at 750 ms within one second. Both attempts acquired the same second-resolution `closed_at`. Reporting the failure first and the older success second allowed dispatch. Both reports accurately reflected their attempts. Use a trustworthy terminal-observation order. When conflicting results cannot be ordered, require another pass before dispatch. `util.py:30–31`, `runs.py:332–338,364–368,454–455`, `tasks.py:289–293,306–317,324–336,402–405`

### R1 · Interrupted acceptance — partly fixed

Retry after an actual fast-forward and interrupted database write recovered the tested candidate, even after another mainline commit introduced a failing test. Watcher reconciliation also recovered an interrupted public-prep acceptance after the move, and retried an old public-prep acceptance after candidate registration but before the move. Two boundaries still fail.

- **A live job can be mistaken for an interrupted acceptance.** When main already contained the delivery, `task accept` requested a merge-test job on the current mainline. With the job recorded as running, reconciliation marked the task accepted. Its eventual failure left it accepted. With the same inputs and the failure recorded before reconciliation, the task stayed delivered with a conflict. Design §6.5 permits immediate acceptance of an already-contained delivery. Apply that shortcut before offering a job, or honor the job once requested. Recovery needs a durable indication that this round passed its check before it treats Git ancestry as proof of completion. `gitops.py:183–185`, `tasks.py:697–718,728–737,791–825`
- **Public prep can still be stranded before candidate registration.** I interrupted delivery after the `task.delivered` transaction and before `_merge`. Two hours later, reconciliation found nothing, the task had no inbox item, and the researcher, coordinator and hypothesis scientist could not accept it. The scan requires `merge_state == "merging"`, while delivery explicitly clears that field. Record the automatic acceptance obligation with delivery, or include these public deliveries in the retry scan using their delivery timestamp and revision. `tasks.py:540–553,667–671,818–837`, `inbox.py:220–231`

### Remote Git staging — partly fixed

On a single acceptance, `rl compute sync` published the candidate to a bare origin and a separate worker clone could read the exact registered commit. A failed merge test also retired its local candidate ref.

**A losing acceptance can delete the winner's ref.** I interleaved two accepts of the same delivered revision before either had published the candidate to the database. One entered job-mode acceptance. The other deleted the winner's local candidate branch while returning a conflict. Merge-test registration still succeeded, but after `compute sync` the origin lacked its commit. Create and retire the ref under the same ownership check used to install the acceptance, or use an immutable per-acceptance ref and retire only the owning round's ref. `gitops.py:192–208`, `tasks.py:697–712`, `compute.py:447–452`

**Medium · Ref cleanup and replacement pushes.** Cancelling a pending acceptance left its **local** candidate branch in the next sync's push set. On a second round after failure, `compute steps` printed an ordinary push to the retained remote branch. Git rejected that non-fast-forward push, while `rl compute sync` published the new candidate successfully. Retire local refs on abandonment and have the generated instructions use the working sync command. Leaving retired **remote** copies in place is acceptable for the trial with those fixes. `tasks.py:215–231,755–766,783–790`, `compute.py:261–275,447–452`

## Other requested checks

| Item | Status | Evidence and remaining action |
|---|---|---|
| 4 · Same-hash redelivery | **Fixed** | Returning and redelivering the same commit with a changed conclusion during candidate creation caused the old acceptance to fail. The revised task remained delivered. Both the initial read and candidate write check the creator's observed revision. `tasks.py:660–671,680–685,697–704` |
| 7 · Notification rows | **Fixed** | A genuine Miyabi failure stayed failed when its note quoted a success row for that job. Conflicting duplicate rows inside the job table yielded unknown, including after a third row. The parser stops at the producer's footer. `runs.py:273–286`, `miyabi-toolbox@6df57e9:miyabi-mcp/src/miyabi_mcp/notify.py:481–501` |
| 9 · Historical verification | **Partly fixed · medium** | One sealed child and a newer draft are now labeled correctly. With two children actually dispatched at revisions 1 and 2, the hypothesis check still called revision 2 “newer draft, not handed over.” The first child's draft annotation occupies the key before the second child's sealed entry. Collect the actual handoffs first, then annotate live drafts. Separately, `rl show` reported passing tests for a revision-1 delivery whose latest revision-1 test failed, because a revision-2 pass on the same commit and command was included. Pass the sealed revision to `test_runs`. `verify.py:71–75,112–126`, `runs.py:118–124` |
| Table 4 · Shared costs | **Fixed for the agreed policy** | A shared 1 CPU·h charge triggered both envelopes' alerts, counted once at project level, and appeared as 1 CPU·h shared in each check. The generated page marked the amounts and explained why they cannot be summed. `usage_view.py:29–55,68–93`, `page.py:290–293` |
| Low-usage host switching | **Fixed** | With every host low, the researcher kept the same session ID across beats. When another host became usable and had quota, the watcher closed the old session and reopened on that host. The configuration guide matches the role restriction and fallback. `watch.py:136–145,280–285`, `docs/configuration.md:144,182` |
| Hypothesis scientist's compute pack | **Fixed** | On a login-node project, both scientist and researcher packs showed the configured allocation and the login policy. Both call the same formatter. `pack.py:146–149,225–229,266–272`, `compute.py:120–135` |
| CLI and job-mode examples | **Partly fixed · low** | The normal job-mode diagnostic and guides now include the request ID and resource guidance. Registration with those arguments succeeded. Giving `task test --record` a trial attempt still printed a replacement registration command missing both. Reuse the corrected example in that diagnostic. `tasks.py:285–298`, `plugin/skills/smooth-research/references/workflow.md:194–199`, `docs/configuration.md:224` |

## Architecture, coverage and validation

The database revision check now protects the delivery correctly. Extending the same ownership rule to its Git ref would simplify cleanup. A persisted review snapshot would likewise give the reviewer, dispatch and historical verification one consistent account of what was approved.

Prioritize regression tests for a changed draft after review, a researcher-requested review during redesign, two terminal observations in one second reported in reverse order, and overlapping accepts followed by a real remote fetch. Exercise public-prep interruption on both sides of candidate registration, and run recovery both before and after a live merge job fails. Assert the recorded design, task state and fetchable Git object.

All **89 repository tests passed** in a detached checkout. **25 disposable probe cases** exercised the listed outcomes using Git, LatticeDB, the CLI, controlled interleavings, a Herdr fake and the local Miyabi renderer. Remote staging used a local bare origin and worker clone. No live cluster job was submitted. CodeGraph supplied caller traces. Pyflakes was clean. Ruff 0.17.6 passed `E9,F401,F63,F7,F82`. Its full configured run reported 80 diagnostics.
