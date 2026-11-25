# All-Claude trial, 2026-11-25

The plugin built from `redesign-v5` was installed in Claude Code (0.1.0 → 0.2.0, from `dist/`). A minimal project then ran end to end in a separate herdr workspace. All five roles were `claude:claude-haiku-5-5@medium`. The project's plan was one claim: the mean of three N(5,1) draws has a smaller absolute error than one draw. Stop after this one hypothesis.

## What ran

| Time (UTC) | Step |
|---|---|
| 06:15 | `rl resume`: the watcher and the coordinator started. As the user, I asked for the tree. The coordinator created and released T-0001. |
| 06:16–06:35 | The tree scientist made H-0001 and D-0001. RV-0001 passed and T-0001 was delivered. The scientist then waited **30 min**: no item woke it (bug 1), and the coordinator's session had ended (bug 2). |
| 07:04 | The fixed watcher bound the coordinator to its live agent again and told it about T-0001. It accepted the tree, created PH-0001, queued T-0002 and asked for approval. I approved as the user. |
| 07:05–07:08 | The scientist designed E-0001. RV-0002 failed with three findings, and the recheck passed. The watcher told the reviewer about pass #2. T-0003 was dispatched. |
| 07:08–07:12 | The researcher ran both arms for seeds 0 and 1, wrote EV-0001 and delivered. T-0003 was accepted and merged. |
| 07:12–07:13 | Verdict D-0003 was drafted. Conclusion review RV-0003 passed, the verdict was made final and T-0002 was delivered. The coordinator accepted it. |
| 07:14–07:15 | Phase ended. The coordinator created and released T-0004. The phase scientist wrote D-0004 and delivered. The phase report reached the user's inbox. |
| 07:22 | As the user, I said in the coordinator's pane that I had read the report. The coordinator recorded it as RQ-0002 (bug 6, after its fix). Nothing waits on anyone. |

Result: H-0001 is **supported** in the simulated model. The mean of three draws had 0.580× (seed 0) and 0.564× (seed 1) the absolute error of a single draw, against an expected 1/√3 ≈ 0.577. The scope is simulation only, with two seeds. From the restart at 07:04 to the phase report took 11 minutes. The watcher opened and closed every session itself, and told each item once per pass. `rl doctor` reported nothing that waits on nobody.

## Problems found and fixed

1. **A passed tree review woke nobody.** There was no item for "deliver T-x" after a passed tree round. Fixed in `550228a`, which also covers a researcher-segment design pass.
2. **The watcher ended the coordinator `rl resume` was starting.** The session record is written before the agent starts. The watcher saw a record with no pane and closed it 1 s after it opened, so the delivered tree was never told. Fixed in `5e6ee89`: a session being started is left alone, and an ended record whose agent lives on in rl's tab is bound to it again.
3. **Ctrl-C on the watcher printed a traceback.** Fixed in `298955e`.
4. **The trust-dialog messages named the pane, not the session.** Fixed in `5e6ee89`.
5. **The researcher's first result imports were all marked deviating.** The CLI was right to refuse them, but part of the cause was ours:
   - The scientist's manifest put `config` and `seeds` at the top level, and the parser silently does not apply them to the arms. `rl task test` and dispatch now warn about it.
   - The researcher reported the commit of its outputs rather than the run's commit, merged the shared config into the arm's, and gave a bare SHA-256 digest. The researcher card and the workflow reference now say what to report.
   - Artifact paths in the task worktree were reported as missing locally. They are now looked up there.

   All of this is in `5f07cb9`.
6. **The coordinator could not record that the user had read the phase report.** The report waited on the user under the task id, and no command was named. It now waits under its RQ id, and the coordinator's item and card give `rl request answer RQ-x --by user`. Fixed in `5f07cb9` and checked live.

## Still open

- The spend shows "—": local runs record no usage, because usage comes only from accounting records. Decided 11-25 by the user: local runs do not count usage time. They are no longer reported as missing usage, and the check line lists them as not counted.
- Clearing a session's context never triggers: there is still no source for its context size.
- The Codex-roles trial ran the same day: [codex-2026-11-25.md](codex-2026-11-25.md).
