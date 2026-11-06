# Codex notes

`rl` below is this plugin's `bin/rl` (absolute path in the session-start note; the plugin root is two
directories above the smooth-research SKILL.md). Write the absolute path in each command.

- Hooks: Codex asks once to trust the plugin's hooks. Without them there is no session-start note; run
  `rl resume` yourself and take the thread id from your environment if you know it, else omit it.
- Session: register with `--host codex --host-session-id <thread id>`; use your exact model id (for
  example `gpt-6-astra`) for `--model-observed` and `--author-model`.
- Inside herdr (`HERDR_ENV=1`): researchers, reviewers and advisors are agent sessions in their own tabs,
  opened with `rl agent open` ([team.md](team.md)). A Codex session gets `-m <model>`,
  `-c model_reasoning_effort=..` for an `@effort`, `--search` when it is an advisor, and the arguments in
  `agents.args.codex` of `smooth-research.yaml`; rl adds no sandbox or approval flags (set them there if you
  want them). `rl agent open` answers Codex's question whether to trust the folder it opened the session in;
  other dialogs (hooks to review, an approval) and the sessions' questions are yours to decide (`rl agent
  dialog`, `rl agent answer`); only what only the user can decide goes to the user. A watcher tab restarts
  sessions that stopped unexpectedly (team.md). The bullets below are the forms without herdr.
- Researchers: inside herdr, a researcher is always a herdr session (`rl agent open researcher-1 --role
  researcher --task T-x`), never `rl delegate`, which refuses there unless given `--headless`. Without herdr:
  for each implementation or run task, use a Codex subagent if they are enabled, or
  `rl delegate T-x --background` for an independent session with the first available entry of
  models.researcher in `smooth-research.yaml` (`--entry codex:<model>@high` or `--entry claude:<model>` picks
  one); its thread id and model are recorded. It gets that host's `agents.args`, so it has the MCP servers a
  herdr session of the role has. Then read
  `rl inbox --role coordinator`, check the outputs and `rl task accept` or `rl task reopen` with what to
  change. To continue a task that role owns (after a WAIT), delegate it again with the same `--role`.
  Start several at once only for tasks that can proceed independently.
- Reviewer: the first available entry of models.reviewer (by default the Codex CLI, then Claude):
  `rl review request --author-model <model that wrote it>`; your conclusions likewise:
  `rl review conclusion --author-model <your model id>`. The relation to the author's model family is
  recorded with each answer; list another family first for reviews from outside your own. From a session rl
  opened, rl adds the model it recorded for that session when the ones given leave it out, and says so.
- Advisors: every entry of models.advisor (by default `codex:gpt-6-astra@max` and
  `claude:claude-fable-5-1@high`) answers an advice round in its own session that sees only the package.
- Your own subagents run on a lower tier than you: a smaller OpenAI model, or your model at a lower effort.
- Long runs on a machine permitted to execute workloads: start them in the background (`nohup ... &` and record the PID), keep working, record the end
  after checking the exit status. Do not sleep-poll.
- Cluster jobs: waiting is the coordinator's. Follow the configured executor's notification schema and
  choose a delivery mechanism that can reach the coordinator's session (the work pack names its thread id and,
  in herdr, its herdr target: use exactly that, not a role name). Start the listener/daemon it requires; a
  researcher then ends with WAIT. Check that the target it reports is you (`rl resume` shows yours).
  When the notice arrives, inspect completion and start the researcher again with the same role. With
  `compute.coordinator: login`, all workloads, including short checks, use the executor, never local nohup.
- Context: at a natural task boundary with a heavy context, write a handoff note in `research/tasks/`, end the
  session record (`rl session end S-x`) and continue in a new session that starts from `rl resume`.
