# Claude Code notes

`rl` below is the CLI named in SKILL.md (`<plugin root>/bin/rl`). Claude Code also puts the plugin's
`bin/` on the Bash PATH, so a bare `rl` works unless another `rl` comes first on PATH; when in doubt use
the absolute path from SKILL.md.

- Session: register with `--host claude`; the session id is in SKILL.md and in the session-start note.
  Use your exact model id (for example `claude-sonnet-5-5`) for `--model-observed` and `--author-model`.
- Same-session parallel work: when two tasks can proceed independently, start the native subagent
  `smooth-research:researcher` (Agent tool) with the task id and a role name such as `researcher-1`.
  Give a short brief; it reads its own work pack. When it reports back, read `rl inbox --role coordinator`,
  check the outputs and records, then `rl task accept` or ask for changes. One agent can also do a whole
  small loop alone; do not parallelize work that needs constant coordination.
- Cross-session work (another model family, or long isolated work): `rl delegate T-x --entry
  codex:<model>@medium --background` starts an independent Codex session with the work pack; its session
  id and model are recorded.
- Default reviewer: code you wrote is reviewed by the first non-Anthropic candidate in
  `smooth-research.yaml` (the Codex CLI by default), so `rl review request --author-model <your model id>`.
  A review by your own subagent is a same-family session review, not cross-model.
- Advisor: the configured primary (default `claude:claude-fable-5-1@high`) runs as a separate
  `claude -p` session that sees only the package.
- Long runs: start them with Bash `run_in_background` (or `nohup ... &`), record `running` with the PID,
  keep working, and record the end when you have checked the exit status. Do not sleep-poll.
- Context: at a natural task boundary with a heavy context, write a handoff note in `docs/tasks/`, end the
  session record (`rl session end S-x`) and continue in a new session that starts from `rl resume`.
