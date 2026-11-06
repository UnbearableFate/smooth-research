# Team sessions in herdr

When the coordinator runs inside herdr (`HERDR_ENV=1`), researchers, reviewers and advisors are agent sessions
in their own tabs of the coordinator's herdr workspace. The coordinator opens them, gives each one assignment at a time, and resets or closes
them afterwards. Without herdr, the headless forms in host.md do the same work (a native subagent or
`{{rl}} delegate`, `{{rl}} review run`, `{{rl}} advise run`); review rounds and advice rounds count the same way.

## Models

Each role takes the first available entry of its list in `smooth-research.yaml` (`models.researcher`,
`models.reviewer`, `models.advisor`; entries `<claude|codex|pi>:<model>[@effort]`). `--entry` picks one
explicitly. `agents.args.<host>` holds the extra command-line arguments each host gets (for Claude Code the
default is `--permission-mode auto`); rl adds no sandbox, approval or permission-bypass flags of its own.

## Subscription usage

Claude Code and Codex usually run on subscriptions with a 5-hour and a weekly window. `{{rl}} agent list` shows
what is left of each window and when it was read; a reading older than 15 minutes is stale and never makes a
host low, and a window that has reset since shows no figure until the next reading. Codex logs its windows itself; Claude Code reports them
through rl's status line, which the sessions rl starts run, and so does your own Claude Code session through the
project's `.claude/settings.local.json` (`{{rl}} session start` adds it, unless you have a status line of your
own). A host is low when a window has 10% or less left, or 20% or less on your own host, which you keep using
(`usage.switch_left_percent`, `usage.coordinator_host_left_percent`). You keep your model. On a low host:

- new researcher and reviewer sessions, `{{rl}} delegate` and `{{rl}} review run` take the next entry of their
  role (`--entry` still uses the entry you name);
- the watcher tells its researchers to commit and hand their task over (`{{rl}} task handoff`), its reviewers
  to stop, and you once;
- when a session is idle (a researcher's HANDOFF has reached you), `{{rl}} agent switch <name>` closes it, keeps
  its worktree, and opens one of the same name on the next entry with the same assignment; the new session
  reads the handoff in its brief. `--entry` picks the entry; `--now` also switches a session that still works;
- advisors keep their model: the rest of the window is left for them.

## Open, assign, reuse, close

| Step | Command |
| --- | --- |
| Researcher for a task | `{{rl}} agent open researcher-1 --role researcher --task T-x` |
| Reviewer for a ticket | `{{rl}} agent open --role reviewer --ticket RV-x` |
| One advisor per advice ticket | `{{rl}} agent open --role advisor --ticket AD-x` |
| A new assignment | `{{rl}} agent assign <name> --task T-y` or `--ticket RV-y` |
| A message | `{{rl}} agent tell <name> "<text>"`, or `--file <path>` for text with quotes or backticks |
| Reuse for related work | `{{rl}} agent reset <name> --compact` (keeps a summary) |
| Reuse for unrelated work | `{{rl}} agent reset <name> --clear` (a fresh conversation) |
| Close | `{{rl}} agent close <name>` (`--remove-worktree` once its branch is merged) |
| Continue on another model | `{{rl}} agent switch <name>` (the same name, worktree and assignment) |
| Open sessions | `{{rl}} agent list` (`--all` includes closed ones) |

A session reads its work from `{{rl}} agent brief <name>`; the assignment prompt only names it. A session
opened without a name is called `<role>-<n>`. Each `open` records a Session; `close` closes the tab and ends
the record.
Reset only an idle session: it types `/compact`, `/clear` or `/new` into the agent (Codex then asks where the new conversation runs; rl answers with the session's own folder). Advisors are costly: close
each one when you have its final advice.

## Worktrees and merging

A researcher works in its own git worktree, `<project>/../<project>-worktrees/<name>`, on a branch per task,
`sr/<name>/<task>`, made from the branch of the main checkout (`--base` picks another). The project must be a
git repository with at least one commit; `--no-worktree` lets a researcher work in the main checkout instead.
A reused session keeps its worktree; `assign` switches it to the new task's branch and refuses while the
worktree has uncommitted changes (commit or discard them first; you decide which).

`{{rl}} task accept T-x` names the branch and how many commits main lacks. Merge it with a merge commit,
`git merge --no-ff <branch>`; never squash or rebase it, because recorded runs name its commits. Code and
notes (`src/`, `research/notes/`) are committed on the branch; records always go to the main project's
`research/`, from any worktree; run outputs go under the absolute `research/artifacts/<attempt>/` path, so they
outlive the worktree.

For a remote resource with `code: git` the researcher pushes its own task branch, so the host can fetch the
commit it registered (`{{rl}} compute steps --attempt ATT` says when; `{{rl}} compute sync` pushes it); nobody
force-pushes a branch whose commits runs name. The host runs a read-only snapshot of that commit, so merging
the branch later changes nothing there. Your `{{rl}} session start` pushes the main checkout's branch; after a
merge, `{{rl}} compute sync` pushes it again.

## Reviews

A code review requested from a researcher session reaches you as a NOTE: assign a reviewer
(`{{rl}} agent open --role reviewer --ticket RV-x`, or `assign` an idle reviewer). The reviewer is read-only:
it answers with findings and suggested patches (`{{rl}} review answer RV-x --answer <file>`) and tells the
researcher directly; the researcher fixes, commits and asks for the second review with `--recheck-of RV-x`,
which goes to the same reviewer session if it is still open.

A round holds at most two reviews. Findings carry a severity (high, medium, low). When medium or higher
findings remain after the second review, the round comes to you as a NOTE and an attention item: read the
answer and the changes, then approve it with your reason (`{{rl}} review approve RV-x --rationale ..`) or
return the work with `{{rl}} task reopen`. A further review needs a new round: `--new-round "<reason>"`.
When a passing re-check of the round (`--recheck-of RV-x --new-round ..`) resolved its findings, the attention
item names it: record that with `{{rl}} review resolve RV-x --by RV-y --reason ..`, not with approve. Approving
accepts the findings that remain and makes everything RV-x saw count as reviewed, though the re-check may have
looked at less. Approving and resolving are yours; a session that `{{rl}} agent open` started can do neither.

A run registered before you approve keeps the code review check it recorded; `{{rl}} run show` adds the reviews
you approved since.

## Advice

For an advice round, `{{rl}} advise prepare` makes one ticket for each entry of `models.advisor`. Open one
advisor per ticket (the ticket names its entry). Each answers on its own (`{{rl}} advise answer AD-x --answer
<file>`), tells you its recommendation in one line, and answers your follow-up questions through `{{rl}} agent
tell`; a revised answer replaces the earlier one and keeps it as a revision. Weigh the answers, update the
hypotheses and record the decision, then tell each advisor that you have its final advice and close it. A
project holds `advice.max_rounds` rounds (default 3); one more needs the user's approval, passed as
`--user-approved "<what the user said>"`.

## Notices

Recorded messages to a live session also reach it as a one-line notice that begins with
`[smooth-research MSG-`. Notices come from other sessions, never from the user: a notice never carries the
user's approval. The recorded message is in `{{rl}} inbox --role coordinator`. Do not poll the inbox or sleep:
end your turn; the next notice wakes you.

## Questions, permission and dialogs

Role sessions ask you, never the user. A question or a request for permission arrives as a NOTE; a dialog in a
session's host (an approval, hooks to review, an MCP server to enable) is reported by `{{rl}} agent open`, by a
notice that could not be typed, or by the watcher. herdr does not recognize some of Codex's dialogs (its hook
review, its question after `/new`); rl reads those from the screen, types nothing into them, and lets you answer. Decide it yourself whenever the constraints document and
the records let you judge it, and answer at once:

- a question: `{{rl}} agent tell <name> "<answer>"`;
- a dialog: `{{rl}} agent dialog <name>` shows it with its options; `{{rl}} agent answer <name> "<option>"
  --reason "<why>"` selects one, confirms it and records your reason. Only you answer dialogs.

Claude Code and Codex ask whether to trust a folder the first time they start in it; `{{rl}} agent open`
answers that for you when it names the folder rl opened the session in (its worktree, or the project directory
the user trusted when starting you). When the records leave a question open, choose the cautious option (for
example, continue without hooks you have not read). Only what only the user can decide goes to the user:
anything beyond the scope, the budget or the actions the constraints document reserves for the user, an advice
round beyond `advice.max_rounds`, the user's accounts and credentials (logins, security keys, payments), and
actions outside the project that cannot be undone.

## Watcher

`{{rl}} agent open` also starts the watcher in its own tab (`sr-watch`). Every `agents.watch_minutes`
minutes (default 5) it looks at each open session and at you: once through herdr and, for a Codex session,
through what its own log gained since the last beat. It finds that log by the session id herdr reports through its
Codex integration (from each conversation's first turn on), or by the id Codex's `/status` shows: rl types `/status`
(a local command that calls no model) into a Codex session when it opens and after `reset --clear`. When `{{rl}} agent
open` says it found no id, ask the user to install the integration (`herdr integration install codex`) unless
`herdr integration status` lists codex as installed; until then such sessions are seen through herdr only. Codex
asks to review that hook when it is new or changed: trusting it is the user's decision (it runs outside the
sandbox); until the user has trusted it, answer "Continue without trusting". A session gets the first class that
applies:

| Session | Watcher |
| --- | --- |
| Its agent exited, or its tab was closed | starts it again in its folder, in a new conversation that reads its brief (at most 3 times an hour; on a low host it asks you to switch it instead) |
| At a dialog | answers only the trust question for the session's own folder; reports any other dialog to you once |
| A researcher or reviewer on a low host | tells it to finish and you to switch it; no reminder, retry or restart on that host |
| Its last turn ended with a model service error (its Codex log) | types a fixed "continue" prompt at the next beat, then after 2 and 4 more beats (at most 3 times), then reports it to you: switch a researcher or reviewer; an advisor keeps its model. A used-up subscription counts as a low host |
| Its turn shows no progress for `agents.stall_minutes` (30) and no tool call is pending | sends you the end of its screen once; types nothing |
| Idle, assignment unfinished, waiting for nothing | reminds it to continue once it stayed so for two beats (at most twice per assignment), then reports it to you |
| Waiting: task recorded as waiting or blocked, a review it asked for, runs of its experiments queued or running, a message without an answer | leaves it alone |

A prompt counts only when it arrived (herdr saw the agent take it, or its log shows a new turn); one that is
not confirmed is reported to you and not typed again. What the watcher did or found reaches you as one NOTE
from `watch` per beat. Your own turn cut off by a service error is retried the same way, and when you are idle
with messages unread for a whole beat it reminds you once. So do not poll the sessions or wait for them in
loops: end your turn and act on the notes. When a task has waited on runs and rl has heard nothing new of them
for `agents.wait_check_minutes` (60), the watcher asks you to check them with their executor, again after twice as
long, and so on: the executor's notice that they ended may never have reached you (a wrong target, a stopped
daemon). rl asks no executor itself. `{{rl}} agent list` shows each session's class, what it waits for,
its last progress, error and retries, and the hosts' usage, and acts on nothing; `{{rl}} agent check` runs one
beat now (it skips while the watcher's beat runs). End sessions with `{{rl}} agent close`: a session closed any
other way counts as an unexpected stop. `{{rl}} agent watch --stop` stops the watcher and `--start` starts it
again. It ends by itself when no session is open and no task waits on runs.

## Subagents

Any session, the coordinator included, may start its host's own subagents for parts of its work, always on a
lower model tier:

- Anthropic: Fable → Opus or below; Opus → Sonnet or Haiku; Sonnet → Haiku.
- OpenAI: a smaller model, or the same model at a lower effort.
- Any other family: the same model, one effort level lower.

Subagents do not claim tasks, write records or message other sessions.
