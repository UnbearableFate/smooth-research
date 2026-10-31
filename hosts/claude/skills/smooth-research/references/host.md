# Claude Code notes

`rl` below is the CLI named in SKILL.md (`<plugin root>/bin/rl`). Claude Code also puts the plugin's
`bin/` on the Bash PATH, so a bare `rl` works unless another `rl` comes first on PATH; when in doubt use
the absolute path from SKILL.md.

- Session: register with `--host claude`; the session id is in SKILL.md and in the session-start note.
  Use your exact model id (for example `claude-sonnet-5-5`) for `--model-observed` and `--author-model`.
- Inside herdr (`HERDR_ENV=1`): researchers, reviewers and advisors are agent sessions in their own tabs,
  opened with `rl agent open` ([team.md](team.md)). They get the arguments in `agents.args.<host>` of
  `smooth-research.yaml` (for Claude Code `--permission-mode auto` by default) and no permission-bypass flags.
  Claude Code asks whether to trust a new worktree's folder; `rl agent open` answers that question for the
  folder it opened the session in. Any other dialog is reported, not answered: ask the user. A watcher tab
  restarts sessions that stopped unexpectedly (team.md). The bullets below are the forms without herdr.
- Researchers: for each implementation or run task, start the native subagent `smooth-research:researcher`
  (Agent tool) with the task id and a role name such as `researcher-1`. Give a short brief; it reads its own
  work pack and has your tools, including MCP servers such as pi-env-mcp and miyabi-mcp. When it reports
  back, read `rl inbox --role coordinator`, check the outputs and records, then `rl task accept` or
  `rl task reopen` with what to change. Start several at once only for tasks that can proceed
  independently; do not split work that needs constant coordination.
- Cross-session work (another model, or long isolated work): `rl delegate T-x --background` starts an
  independent session with the work pack, using the first available entry of models.researcher in
  `smooth-research.yaml` (`--entry codex:<model>@high` picks one); its session id and model are recorded.
  To continue a task that role owns (after a WAIT), delegate it again with the same `--role`.
- Reviewer: the first available entry of models.reviewer (by default the Codex CLI, then Claude):
  `rl review request --author-model <model that wrote it>`; your conclusions likewise:
  `rl review conclusion --author-model <your model id>`. The relation to the author's model family is
  recorded with each answer.
- Advisors: every entry of models.advisor (by default `codex:gpt-6-astra@max` and
  `claude:claude-fable-5-1@high`) answers an advice round in its own session that sees only the package.
- Your own subagents (Agent tool `model`) run on a lower tier than you: Fable → Opus or below, Opus → Sonnet
  or Haiku, Sonnet → Haiku.
- Long runs: start them with Bash `run_in_background` (or `nohup ... &`), record `running` with the PID,
  keep working, and record the end when you have checked the exit status. Do not sleep-poll.
- Waiting on cluster jobs is yours: a researcher that submitted Miyabi jobs ends with WAIT and reports the
  `notify.daemon.listen_command` returned by the first `job_submit` of the batch. Run it with Bash
  `run_in_background`; it exits when the notification arrives, which wakes you whether you are working or
  idle. Then start the researcher again with the same role; it resumes the task.
- Context: at a natural task boundary with a heavy context, write a handoff note in `docs/tasks/`, end the
  session record (`rl session end S-x`) and continue in a new session that starts from `rl resume`.
