---
name: smooth-research
description: Evidence-driven ML research that starts from a research plan, with an evidence record. Use to start, run or resume a research project from a research plan - a coordinator, scientists, researchers, reviewers and advisors work as herdr sessions on one record of hypotheses, experiments, runs, evidence and decisions. Not for ordinary coding outside a research project.
---

# Smooth Research

A research project runs as a team of agent sessions in herdr, each one role on one thing: the coordinator on the
project (the user talks to it), a scientist on a tree, hypothesis or phase task, a researcher on a task, a
reviewer on a review round, an advisor on an advice ticket. They share one record kept by the CLI; nothing is
passed by chat. A watcher tab opens the sessions when work waits on them, types a one-line notice into an idle
pane, and closes sessions whose work is done.

## The CLI

{{cli_note}}
- In a project it runs on the project's `.venv` (made by `init`); elsewhere on its own (`{{rl}} setup` rebuilds).
- Commands print a short summary; `--json` the full records; `{{rl}} <command> --help` every flag.
- After a plugin update the records are upgraded on first use (a copy of the old database is kept); `{{rl}}
  upgrade` lists what to adjust. Projects made before the 2026-11 redesign (record format below 5) are not
  opened: continue them with the plugin version that made them, or start a new project from their plan.

## In a session the watcher opened

`SMOOTH_RESEARCH_SESSION` names you (for example `scientist-T-0002`). Run `{{rl}} pack` first and work from what
it prints: your role card, your task or round, what waits on you, the material. Do not reread this skill; the
pack replaces it, also after your conversation is compressed or cleared. When nothing waits on you, end your turn.

## Starting a project (the user, or the coordinator's first turn)

Needs herdr (`HERDR_ENV=1` in the pane) and uv.

1. In the project directory: `{{rl}} init --plan <plan.md>`. It makes the directory a uv project, creates the
   records and seals the plan as `docs/plan.md`. Commit everything (`git init` first if needed): task branches
   start from the mainline.
2. Fill `docs/constraints.md` with what only the user may change: goal and claims in scope, data and
   environments, resource ceiling, actions that need their decision. `smooth-research.yaml` holds the model
   lists per role, the watcher's times and the compute resources ([references/workflow.md](references/workflow.md)).
3. `{{rl}} resume`: starts the watcher tab and the coordinator's pane (or says they run), then prints the status.
   The coordinator's first step is the tree task. The user talks to the coordinator in its pane.

Resuming later is the same `{{rl}} resume`; `{{rl}} status` shows the state without starting anything, and
`docs/research.html` is the research page (drawn again by the watcher; `{{rl}} view` draws it now).

## Where things are

- `docs/`: what the user reads: `plan.md` (sealed), `constraints.md`, `research.html`, `explanations/`,
  `glossary.md`.
- `src/`: experiment code. `research/`: the records (`research/state/`, never edited by hand), sealed documents,
  task work directories (`research/work/T-x/`).
- Each task has a branch `sr/T-x` and a worktree next to the project (`<project>-worktrees/T-x`). The mainline
  moves only when the CLI accepts a task: a merge commit on the task branch, its tests, then a fast-forward.

## References

- Formats and recipes (manifest, result, observation, compute resources, cluster jobs): [references/workflow.md](references/workflow.md).
- Sessions, models, dialogs, the watcher and subagents: [references/team.md](references/team.md).
- Host notes: [references/host.md](references/host.md).
