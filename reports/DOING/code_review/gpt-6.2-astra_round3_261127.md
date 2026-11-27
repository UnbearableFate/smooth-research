# Smooth Research implementation review, round 3 of 3

**All six requested fixes passed their reproductions. Two new High regressions remain.** One prevents an explicit handoff from reaching the scientist. The other records an approved hypothesis with a changed scientific claim.

Reviewed the uncommitted `redesign-v6-impl` tree at `3573dc7`. All reproductions used disposable projects with the five specified session variables unset. Handoff tests used fake Herdr with the observer stopped. The full suite passed **226 tests in 208.06 seconds**, including all 23 tests in `tests/test_review_fixes.py`.

## Fix verification

| Fix | Result | Observed behavior |
|---|---|---|
| A/B — heartbeat separation and watcher ownership | **Fixed** | With an established watcher running code from `3573dc7`, migrating a copy of its project left the process running and reported the foreign heartbeat. Writing `observer.json` left `watch.json` byte-for-byte intact. Migrating the original project terminated its watcher with SIGTERM and left the legacy task open. A separate process with the argument `compare smooth watch run logs` survived. |
| C — design token | **Fixed** | Two retargets at one commit produced digest suffixes `29daa95a` and `e0074bd9`. The first token was refused on the second design. A later pass that permitted a bare commit also refused the stale qualified token and accepted its current target. |
| E — explicit SHA on a dirty local run | **Fixed** | With no resource and a modified tracked simulator, both a fresh evidence registration and an evidence retry using `--trial --run … --commit <HEAD>` were refused. The attempt count stayed at one. A genuine trial succeeded. |
| F — recycled acceptance PID | **Fixed** | A recent `task.merging` event and a live PID left main in place. With the same PID alive and the event aged to 31 minutes, retrying completed acceptance and moved main to the recorded merge commit. |
| H — failed public merge tests | **Fixed** | After a scientist answered a blocker, both a local merge-test failure and a failed job produced the coordinator's inbox item and delivered a prompt to its pane. The item offered force with a reason or return. |
| J — CRLF draft | **Fixed** | A 503-byte CRLF draft containing a question passed `tree check` and `tree record`. The route decision's sealed object matched every input byte. |

## New High findings

### 1. Explicitly handing a failed delivery check to a scientist leaves the scientist without work

`src/smooth_research/inbox.py:271` routes every failed public prep delivery to the coordinator. That includes a failed **delivery** check, for which the CLI expressly offers delegation at `tasks.py:736`. The separate scientist inbox rule at `inbox.py:221` handles only blocked tasks.

**Reproduction.** Create and release a public prep with `--done-check false`. Commit work and deliver it as the researcher. No merge test runs. Follow the delivery message as coordinator with `rl task release T-0001 --to scientist`. The command recorded `scientist_session=scientist-T-0001` and reported both “handed to scientist-T-0001” and “scientist-T-0001: nothing waits on it”. No scientist session opened. Its inbox was empty, and the coordinator still owned the only task-conclusion item. Restoring just the previous recipient expression in the isolated probe opened the scientist and delivered the item.

Keep the coordinator as the recipient of failed merge tests. For an explicit delegation after a failed delivery check, give the scientist an actionable inbox item so the release command can notify it.

### 2. Tree recording silently changes an approved hypothesis when its text cannot be decoded

`src/smooth_research/tree.py:137` decodes the draft with `errors="replace"`. The new digest at `tree.py:331` checks the original bytes against the sealed file, while `tree.py:369` writes the lossy decoded statement into the hypothesis.

**Reproduction.** In separate projects, write a tree draft in Latin-1 with the statement `runtime grows as O(n²)` in one and `runtime grows as O(n³)` in the other. Run `rl tree check` and `rl tree record --approval "approve this scaling claim"`. Both succeeded without warnings and recorded user approval. Both stored the identical hypothesis statement `runtime grows as O(n�)`. The sealed documents preserved the original bytes, but the structured claims lost the distinction between quadratic and cubic growth. Before the byte-digest change, the disagreement between the decoded text and sealed bytes prevented recording.

Decode the snapshot strictly before creating research records. Report an unreadable encoding from both check and record, leaving the draft available for correction. Continue hashing and sealing the original bytes for valid drafts, including CRLF.
