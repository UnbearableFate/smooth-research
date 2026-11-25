# Sessions in herdr

A session is one role on one thing, named `<role>-<subject>`: `coordinator`, `scientist-T-0002`,
`researcher-T-0003`, `reviewer-RV-0004`, `advisor-AD-0001`. Each runs in its own herdr tab of the project's
workspace with `SMOOTH_RESEARCH_SESSION` set; a scientist or researcher works in its task's worktree. herdr's
agent name for it is the session name in lower case behind a short tag of the project, so two projects keep apart.
Commands outside a pane name the session with `--as <name>`.

## Models

Each role takes the first available entry of its list in `smooth-research.yaml` (`models.coordinator`,
`models.scientist`, `models.researcher`, `models.reviewer`, `models.advisor`; entries
`<claude|codex|pi>:<model>[@effort]`). `agents.args.<host>` holds extra arguments for each host's sessions (for
Claude Code `--permission-mode auto` by default). rl adds no sandbox or permission-bypass flags of its own.

## What the watcher does

`{{rl}} resume` starts it in its own tab (`rl watch`); `{{rl}} watch once` runs one beat by hand. Every
`agents.beat_seconds` it computes the to-do items from the records (`{{rl}} inbox --for <name>` shows them) and:

- opens a session for a recipient that has none, within the phase's scientist limit and the hypothesis task's
  parallel limit; the session takes its open task, and its first prompt tells it to run `{{rl}} pack`;
- types one line per new item into an idle pane, and a reminder when an item waits longer than its deadline;
  nothing is typed while an agent works, and nothing into the coordinator's pane while the user typed there in
  the last `agents.user_quiet_minutes`;
- clears the conversation of a session idle longer than `agents.clear_idle_minutes` with a large context before
  waking it; it reads its pack again;
- records a dialog (an approval or a question in the pane) as needing a decision: the coordinator answers it with
  `{{rl}} agent dialog <name>` and `{{rl}} agent answer <name> "<option>" --reason ..`, the user answers the
  coordinator's own;
- closes the session of a task accepted or cancelled, or of a round passed or closed, and ends the record of a
  session whose agent is gone (the next beat opens it again when it still has work);
- finishes an acceptance whose test job already ended, and draws the research page again when the records
  changed.

Items past their deadline climb a ladder: the session is reminded, then the one who made its task, then the
coordinator, then the user (`agents.deadlines`). Every rl command warns when the watcher's heartbeat is stale;
`{{rl}} resume` starts it again.

## Subscription usage

A host whose subscription window is nearly used up (`usage.switch_left_percent`) gets no new researcher or
reviewer sessions; open ones are closed when idle and reopened on the next entry of their role. The coordinator,
scientists and advisors keep their model.

## Subagents

Any session may start its host's own subagents for parts of its work, always on a lower model tier (Anthropic:
Fable → Opus or below, Opus → Sonnet or Haiku, Sonnet → Haiku; OpenAI: a smaller model or a lower effort).
Subagents do not claim tasks, write records or talk to other sessions. The coordinator answers the user's
questions about code, logs and outputs through a read-only subagent, so the material stays out of its context.
