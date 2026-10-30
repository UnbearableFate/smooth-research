# Codex notes

`rl` below is this plugin's `bin/rl` (absolute path in the session-start note; the plugin root is two
directories above the smooth-research SKILL.md). Write the absolute path in each command.

- Hooks: Codex asks once to trust the plugin's hooks. Without them there is no session-start note; run
  `rl resume` yourself and take the thread id from your environment if you know it, else omit it.
- Session: register with `--host codex --host-session-id <thread id>`; use your exact model id (for
  example `gpt-6-astra`) for `--model-observed` and `--author-model`.
- Parallel work: when two tasks can proceed independently, use Codex subagents if they are enabled, or
  `rl delegate T-x --entry codex:<model>@medium --background` for an independent session; its thread id and
  model are recorded. `--entry claude:<model>` delegates to Claude Code instead. Then read
  `rl inbox --role coordinator`, check the outputs and `rl task accept` or ask for changes.
- Default reviewer: code written by an OpenAI model is reviewed by the first non-OpenAI candidate in
  `smooth-research.yaml` (the Claude CLI), so `rl review request --author-model <your model id>`.
- Advisor: the configured primary (default `claude:claude-fable-5-1@high`) runs as a separate
  `claude -p` session that sees only the package; fallbacks follow the configuration.
- Long runs: start them in the background (`nohup ... &` and record the PID), keep working, record the end
  after checking the exit status. Do not sleep-poll.
- Context: at a natural task boundary with a heavy context, write a handoff note in `docs/tasks/`, end the
  session record (`rl session end S-x`) and continue in a new session that starts from `rl resume`.
