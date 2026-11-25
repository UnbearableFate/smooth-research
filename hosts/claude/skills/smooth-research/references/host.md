# Claude Code notes

- `rl` is this plugin's `bin/rl`. Claude Code puts the plugin's `bin/` on the Bash PATH, and the sessions the
  watcher opens get its absolute path in `SMOOTH_RESEARCH_RL` and in their pack.
- A session the watcher opens is started with `--model`, `--effort`, `--session-id` and `-n <session name>`,
  rl's status line (it reports the subscription windows), `--disallowedTools Edit,Write,NotebookEdit` for a
  reviewer, and `agents.args.claude` from `smooth-research.yaml` (default `--permission-mode auto`).
- Claude Code asks whether to trust a new worktree's folder: the watcher answers that question for the folder it
  opened the session in. Every other dialog is recorded for the coordinator (`rl agent dialog`, `rl agent answer`).
- Subagents (Agent tool `model`) run on a lower tier than the session: Fable → Opus or below, Opus → Sonnet or
  Haiku, Sonnet → Haiku.
