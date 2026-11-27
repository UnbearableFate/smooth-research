# Claude Code notes

- `rl` is this plugin's `bin/rl`. Claude Code puts the plugin's `bin/` on the Bash PATH, and the sessions rl
  opens get its absolute path in `SMOOTH_RESEARCH_RL` and in their pack.
- A session rl opens is started with `--model`, `--effort`, `--session-id` (or `--resume` with its earlier
  conversation) and `-n <session name>`, rl's status line (it reports the subscription windows),
  `--disallowedTools Edit,Write,NotebookEdit` for a reviewer, and `agents.args.claude` from `smooth-research.yaml`
  (default `--permission-mode auto`).
- Hooks: SessionStart (who you are, read the pack), Stop (hands a session the items it was not told of when it ends
  its turn) and UserPromptSubmit (adds new items to the user's message in the coordinator's pane).
- Claude Code asks whether to trust a new worktree's folder: rl answers that question for the folder it opened the
  session in. Every other dialog is recorded for the coordinator (`rl agent dialog`, `rl agent answer`).
- Subagents (Agent tool `model`) run on a lower tier than the session: Fable → Opus or below, Opus → Sonnet or
  Haiku, Sonnet → Haiku. The coordinator's tree subagent is the exception: `model` is the alias of its
  `models.tree` entry (`claude-fable-5-2` → `fable`) and `effort` is the entry's (`xhigh`).
