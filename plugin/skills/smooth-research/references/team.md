# Sessions in herdr

A session is one role on one thing, named `<role>-<subject>`: `coordinator`, `scientist-T-0002`,
`researcher-T-0003`, `reviewer-RV-0004`, `advisor-AD-0001`. Each runs in its own herdr tab of the project's
workspace with `SMOOTH_RESEARCH_SESSION` set; a scientist or researcher works in its task's worktree. herdr's
agent name for it is the session name in lower case behind a short tag of the project, so two projects keep apart.
Commands outside a pane name the session with `--as <name>`.

## Who tells whom

The command that hands work on tells the session that takes it, and opens that session when it has none:

| Command | Tells |
| --- | --- |
| `{{rl}} task release T-x` | `scientist-T-x` (hypothesis, phase task) or `researcher-T-x` (public prep task) |
| `{{rl}} task dispatch` / `answer` | `researcher-T-x` |
| `{{rl}} task block` | the task's creator (or the scientist a public prep task was handed to) |
| `{{rl}} task deliver` | the task's creator; a public prep task is accepted on delivery when its done check passes |
| `{{rl}} task return` | the holder |
| `{{rl}} task accept` | the creators or holders of the tasks that depend on it |
| `{{rl}} task cancel` / `claim` | the holder, when it is busy (cancel) or the task was taken from it (claim) |
| `{{rl}} review request` / `answer` | the reviewer (a recheck wakes the same session) / the author |
| `{{rl}} discuss`, `reply`, `question` | the other party |
| `{{rl}} request` / `request answer` | the coordinator / the scientist that asked |
| `{{rl}} advise ask` / `answer` | the advisor / the scientist |
| `{{rl}} run ended` | the submitter |
| `{{rl}} run usage` | the hypothesis task's holder at an envelope line; the coordinator too at the last one |
| `{{rl}} decide --final` | the holders of the tasks of hypotheses that depend on it |

How a session is told depends on its state: idle, one `[smooth-research]` line per item is typed into its pane;
working, its Stop hook hands it the new items when it ends its turn (once each); at a dialog, nothing is typed and
the coordinator gets the dialog; no open session, it is opened (going on with its earlier host conversation) and
its first prompt tells it to run `{{rl}} pack`. Opening waits for the agent to start, up to about 90 seconds. The
coordinator's pane is the user's too: while the user typed there in the last `agents.user_quiet_minutes`, nothing
is typed; the UserPromptSubmit hook adds the items to the user's next message, the Stop hook hands them over.

When telling fails (no herdr, a timeout) the command still succeeds and prints `could not notify X: ...; remedy:
{{rl}} session open X or {{rl}} resume`. The item stays in X's inbox. Without herdr, `{{rl}} session open X --print`
prints the command that starts the session in a terminal.

## Holders and sessions

- Release writes the holder (hypothesis and phase tasks: the scientist; public prep tasks: the researcher) and
  makes the task's branch and worktree; a scientist holds the execute and prep tasks it makes, and dispatch hands
  them to a researcher. Opening a session claims nothing.
- A task that depends on tasks not yet accepted (`--depends-on`) waits without holder or worktree: its branch is
  made from a mainline that has its prerequisites. `{{rl}} task accept` of the last one tells its creator. Claiming
  it earlier is allowed and flagged `prerequisite`: merge the mainline into the branch once they are accepted.
- `{{rl}} task claim T-x` also takes a task back: its creator or the coordinator becomes the holder and the earlier
  holder is told.
- Sessions close only through the command that ends their work, and only while idle: accepting or cancelling a
  task closes its researcher and the reviewers of its rounds; a hypothesis or phase task also its scientist and the
  sessions under it. Others stay open (idle costs no tokens); `{{rl}} status` lists those whose work ended:
  `{{rl}} session close NAME`.
- Waiting: when nothing waits on a session it ends its turn; a command tells it when something does. Waiting for
  its own short job is fine.

## Models

Each role takes the first available entry of its list in `smooth-research.yaml` (`models.coordinator`,
`models.scientist`, `models.researcher`, `models.reviewer`, `models.advisor`; entries
`<claude|codex|pi>:<model>[@effort]`). A host whose subscription window is nearly used up
(`usage.switch_left_percent`) is tried last. The model is chosen once, when the session opens; an open session
keeps it. The configuration is read by every command, so a change applies to the next session opened.
`agents.args.<host>` holds extra arguments for each host's sessions (for Claude Code `--permission-mode auto` by
default). rl adds no sandbox or permission-bypass flags of its own.

## Dialogs

rl answers the folder trust question for the folder it opened a session in. Any other dialog (an approval, a
question in the pane) is recorded as needing a decision: the coordinator answers another session's with
`{{rl}} agent dialog <name>` and `{{rl}} agent answer <name> "<option>" --reason ..`; the coordinator's own is the
user's, listed in `{{rl}} status`.

## The observer (optional)

`{{rl}} resume` starts it in its own tab (`{{rl}} watch run`); `{{rl}} watch once` runs one beat by hand. Every
`agents.beat_seconds` it redraws `docs/research.html` when the records changed (or every `agents.page_minutes`),
types into open idle sessions the items they were not told of (a notice that did not get through, items the
coordinator's pane held back, a job past its expected end), reminds once of an item waiting longer than
`agents.deadlines`, and records sessions at a dialog. It opens, claims and closes nothing and changes no research
record. When it is not running, nothing stops: `{{rl}} view` draws the page, `{{rl}} inbox` shows what waits.

## Subagents

Any session may start its host's own subagents for parts of its work, on a lower model tier (Anthropic:
Fable → Opus or below, Opus → Sonnet or Haiku, Sonnet → Haiku; OpenAI: a smaller model or a lower effort).
Subagents do not claim tasks, write records or talk to other sessions. The coordinator answers the user's
questions about code, logs and outputs through a read-only subagent, so the material stays out of its context.
One exception: the coordinator's tree subagent runs on the best model of its family at effort xhigh
(`models.tree`), reads the documents and writes the tree draft (`{{rl}} tree prompt` tells it how).
