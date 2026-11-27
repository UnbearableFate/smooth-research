---
name: smooth-research
description: Evidence-driven ML research that starts from a research plan, with an evidence record. Use to start, run or resume a research project from a research plan - a coordinator, scientists, researchers, reviewers and advisors work as herdr sessions on one record of hypotheses, experiments, runs, evidence and decisions. Not for ordinary coding outside a research project.
---

# Smooth Research

A research project runs as a team of agent sessions in herdr, each one role on one thing: the coordinator on the
project (the user talks to it), a scientist on a hypothesis or phase task, a researcher on a task, a reviewer on a
review round, an advisor on a consultation. They share one record kept by the CLI; nothing is passed by chat. The
plugin only guides and records: it never refuses work for the process's sake and never schedules anyone.

Work moves by the agents' own commands. The command that hands work on tells the next session: it types a
one-line notice into its idle pane, its Stop hook hands it the new items when it ends its turn, or the command
opens its session. A notice is only a pointer; the facts are in the records, so a missed one loses nothing.

## The CLI

{{cli_note}}
- In a project it runs on the project's `.venv` (made by `init`); elsewhere on its own (`{{rl}} setup` rebuilds).
- Commands print a short summary; `--json` the full records; `{{rl}} <command> --help` every flag.
- After a plugin update the records are upgraded on first use (a copy of the old database is kept); `{{rl}}
  upgrade` lists what to adjust. Projects made before the 2026-11 redesign (record format below 5) are not
  opened: continue them with the plugin version that made them, or start a new project from their plan.

## In a session rl opened

`SMOOTH_RESEARCH_SESSION` names you (for example `scientist-T-0002`). Run `{{rl}} pack` first and work from what
it prints: your role card, your task or round, what waits on you, the material. Do not reread this skill; the
pack replaces it, also after your conversation is compressed. When nothing waits on you, end your turn: the
command that hands you work tells you.

## Starting a project (the user, or the coordinator's first turn)

Needs herdr (`HERDR_ENV=1` in the pane) and uv.

1. In the project directory: `{{rl}} init --plan <plan.md>`. It makes the directory a uv project, creates the
   records and seals the plan as `docs/plan.md`. Commit everything (`git init` first if needed): task branches
   start from the mainline.
2. Fill `docs/constraints.md` with what only the user may change: goal and claims in scope, data and
   environments, resource ceiling, actions that need their decision. `smooth-research.yaml` holds the model
   lists per role, the observer's times and the compute resources ([references/workflow.md](references/workflow.md)).
3. `{{rl}} resume`: opens the coordinator's pane and every session that has work but none open, starts the
   observer, then prints the status. The user talks to the coordinator in its pane.

The coordinator's first step is the hypothesis tree: a subagent drafts it from the plan (`{{rl}} tree prompt`),
the user reads the draft on the research page, asks for changes until it is right, and approves it; then
`{{rl}} tree record` records it and the coordinator releases the first hypothesis task.

`{{rl}} resume` does its work once and can be run again whenever something seems stuck. `{{rl}} status` shows the
state without starting anything: what waits on the user, what was not done by the process (flags), sessions with
work but none open, sessions whose work ended, dialogs, compute used per phase. `docs/research.html` is the research page (`{{rl}} view`
draws it; the observer redraws it while it runs).

## Sessions by hand

- `{{rl}} session open NAME` opens a session (or tells an open one what waits on it); `--print` prints the
  command to start it in a terminal without herdr.
- `{{rl}} session close NAME [--force]` closes one; `{{rl}} session list` lists them. Sessions are closed by the
  command that ends their work (accepting or cancelling its task), only while idle.
- `{{rl}} watch` is the optional observer (`{{rl}} resume` starts it): it redraws the research page, delivers
  notices that did not get through to open idle sessions, reminds once of what waits longer than
  `agents.deadlines`, and reports sessions at a dialog. It changes no record and opens no session; research goes on
  without it.

## Where things are

- `docs/`: what the user reads: `plan.md` (sealed), `constraints.md`, `research.html`, `explanations/`,
  `glossary.md`.
- `src/`: experiment code. `research/`: the records (`research/state/`, never edited by hand), sealed documents,
  task work directories (`research/work/T-x/`), the tree draft before it is recorded (`research/tree-draft/`).
- Each task has a branch `sr/T-x` and a worktree next to the project (`<project>-worktrees/T-x`), made when the
  task gets its holder. The mainline moves only when the CLI accepts a task: a merge commit on the task branch, its
  tests, then a fast-forward.

## References

- Formats and recipes (manifest, result, observation, compute resources, cluster jobs): [references/workflow.md](references/workflow.md).
- Sessions, notices, models, dialogs, the observer and subagents: [references/team.md](references/team.md).
- Host notes: [references/host.md](references/host.md).
