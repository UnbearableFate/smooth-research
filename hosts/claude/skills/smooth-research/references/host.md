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
  folder it opened the session in. Other dialogs and the sessions' questions are yours to decide (`rl agent
  dialog`, `rl agent answer`); only what only the user can decide goes to the user. A watcher tab restarts
  sessions that stopped unexpectedly (team.md). The bullets below are the forms without herdr.
- Researchers: inside herdr, a researcher is always a herdr session (`rl agent open researcher-1 --role
  researcher --task T-x`), never `rl delegate`, which refuses there unless given `--headless`. Without herdr:
  for each implementation or run task, start the native subagent `smooth-research:researcher`
  (Agent tool) with the task id and a role name such as `researcher-1`. Give a short brief; it reads its own
  work pack and has your tools, including the configured transport and job MCP servers. When it reports
  back, read `rl inbox --role coordinator`, check the outputs and records, then `rl task accept` or
  `rl task reopen` with what to change. Start several at once only for tasks that can proceed
  independently; do not split work that needs constant coordination.
- Cross-session work without herdr (another model, or long isolated work): `rl delegate T-x --background` starts an
  independent session with the work pack, using the first available entry of models.researcher in
  `smooth-research.yaml` (`--entry codex:<model>@high` picks one); its session id and model are recorded.
  It gets that host's `agents.args`, so it has the MCP servers a herdr session of the role has.
  To continue a task that role owns (after a WAIT), delegate it again with the same `--role`.
- Reviewer: the first available entry of models.reviewer (by default the Codex CLI, then Claude):
  `rl review request --author-model <model that wrote it>`; your conclusions likewise:
  `rl review conclusion --author-model <your model id>`. The relation to the author's model family is
  recorded with each answer. From a session rl opened, rl adds the model it recorded for that session when
  the ones given leave it out, and says so.
- Advisors: every entry of models.advisor (by default `codex:gpt-6-astra@max` and
  `claude:claude-fable-5-1@high`) answers an advice round in its own session that sees only the package.
- Your own subagents (Agent tool `model`) run on a lower tier than you: Fable → Opus or below, Opus → Sonnet
  or Haiku, Sonnet → Haiku.
- Long runs on a machine permitted to execute workloads: start them with Bash `run_in_background` (or `nohup ... &`), record `running` with the PID,
  keep working, and record the end when you have checked the exit status. Do not sleep-poll.
- Waiting on cluster jobs is yours: a researcher ends with WAIT and reports the completion notification
  target and any listener command returned by the configured executor. Check that the target is you (in herdr:
  the herdr target `rl resume` shows for you; a delivery to any other name never arrives). Follow its
  instructions; for a shell listener use Bash `run_in_background`. When notified, inspect completion and start the researcher
  again with the same role. With `compute.coordinator: login`, all workloads, including short checks,
  use the executor, never local background execution.
- Context: at a natural task boundary with a heavy context, write a handoff note in `research/tasks/`, end the
  session record (`rl session end S-x`) and continue in a new session that starts from `rl resume`.
