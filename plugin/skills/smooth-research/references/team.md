# Team sessions in herdr

When the coordinator runs inside herdr (`HERDR_ENV=1`), researchers, reviewers and advisors are agent sessions
in their own herdr tabs. The coordinator opens them, gives each one assignment at a time, and resets or closes
them afterwards. Without herdr, the headless forms in host.md do the same work (a native subagent or
`{{rl}} delegate`, `{{rl}} review run`, `{{rl}} advise run`); review rounds and advice rounds count the same way.

## Models

Each role takes the first available entry of its list in `smooth-research.yaml` (`models.researcher`,
`models.reviewer`, `models.advisor`; entries `<claude|codex|pi>:<model>[@effort]`). `--entry` picks one
explicitly. `agents.args.<host>` holds the extra command-line arguments each host gets (for Claude Code the
default is `--permission-mode auto`); rl adds no sandbox, approval or permission-bypass flags of its own.

## Open, assign, reuse, close

| Step | Command |
| --- | --- |
| Researcher for a task | `{{rl}} agent open researcher-1 --role researcher --task T-x` |
| Reviewer for a ticket | `{{rl}} agent open --role reviewer --ticket RV-x` |
| One advisor per advice ticket | `{{rl}} agent open --role advisor --ticket AD-x` |
| A new assignment | `{{rl}} agent assign <name> --task T-y` or `--ticket RV-y` |
| A message | `{{rl}} agent tell <name> "<text>"` |
| Reuse for related work | `{{rl}} agent reset <name> --compact` (keeps a summary) |
| Reuse for unrelated work | `{{rl}} agent reset <name> --clear` (a fresh conversation) |
| Close | `{{rl}} agent close <name>` (`--remove-worktree` once its branch is merged) |
| Open sessions | `{{rl}} agent list` (`--all` includes closed ones) |

A session reads its work from `{{rl}} agent brief <name>`; the assignment prompt only names it. A session
opened without a name is called `<role>-<n>`. Each `open` records a Session; `close` closes the tab and ends
the record.
Reset only an idle session: it types `/compact`, `/clear` or `/new` into the agent. Advisors are costly: close
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
Approving is yours; a session that `{{rl}} agent open` started cannot approve.

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
notice that could not be typed, or by the watcher. Decide it yourself whenever the constraints document and
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
minutes (default 20) it looks at each open session:

| Session | Watcher |
| --- | --- |
| Its agent exited, or its tab was closed | starts it again in its folder, in a new conversation that reads its brief (at most 3 times an hour) |
| Idle, assignment unfinished, waiting for nothing | reminds it to continue (at most twice per assignment), then reports it to you |
| Waiting: task recorded as waiting or blocked, a review it asked for, runs of its experiments queued or running, a message without an answer | leaves it alone |
| At a dialog | answers only the trust question for the session's own folder; reports any other dialog to you once |

What it did or found reaches you as one NOTE from `watch` per round. When you are idle with messages unread
for a whole round, it reminds you once. End sessions with `{{rl}} agent close`: a session closed any other way
counts as an unexpected stop. `{{rl}} agent check` runs one round now; `{{rl}} agent watch --stop` stops the
watcher and `--start` starts it again. It ends by itself when no session is open.

## Subagents

Any session, the coordinator included, may start its host's own subagents for parts of its work, always on a
lower model tier:

- Anthropic: Fable → Opus or below; Opus → Sonnet or Haiku; Sonnet → Haiku.
- OpenAI: a smaller model, or the same model at a lower effort.
- Any other family: the same model, one effort level lower.

Subagents do not claim tasks, write records or message other sessions.
