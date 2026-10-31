# Smooth Research

A lean ML-research plugin for Claude Code and Codex. The user's research plan sets the direction; the
agent turns it into testable hypotheses, runs cheap discriminating experiments with its own tools, and keeps
an honest, recoverable record of runs, results, evidence and decisions until a formal-experiment proposal,
a pause or a stop is justified. Design: `design/ml-research-agent-design-2026-10-30 (1).md`.

The plugin records; it never submits, polls, retries or cancels experiments. Local shells, remote tools,
containers and HPC skills (for example miyabi-toolbox) stay independent.

## Repository layout

| Path | Role |
| --- | --- |
| `src/smooth_research/` | Shared Python core and the `rl` CLI: LatticeDB store, sealed documents, graph, tasks and sessions, run records, context packs, review/advice tickets, agent sessions in herdr, literature, snapshots, views |
| `plugin/` | Shared, host-independent plugin content: `bin/rl` (environment bootstrap + launcher) and three skills (`smooth-research`, `research-review`, `research-advise`) |
| `hosts/claude/` | Claude Code adapter: `.claude-plugin/plugin.json`, SessionStart hook, `researcher` subagent, host notes, placeholder values |
| `hosts/codex/` | Codex adapter: `.codex-plugin/plugin.json`, SessionStart hook, host notes, placeholder values |
| `scripts/build_plugins.py` | Assembles `dist/claude` and `dist/codex` marketplaces (shared core + one adapter) |
| `tests/` | Contract tests for the invariants in design §16.2 |

Model calls for reviews, advice and delegated sessions go through `src/smooth_research/adapters.py`
(`claude -p`, `codex exec --json`, `pi -p --mode json`, or a manual entry a person submits); the requested
model, the observed model (or "unverified"), the exact session id, usage and cost are recorded with each
ticket. Which models are called is set in the project's `smooth-research.yaml`: researchers and reviewers
use the first available entry of their list, and every listed advisor answers each advice round on its own.

When the coordinator runs inside herdr, a terminal multiplexer for coding agents (`HERDR_ENV=1`),
researchers, reviewers and advisors are interactive Claude Code, Codex or pi sessions in their own herdr
tabs (`rl agent open`, see `src/smooth_research/agents.py`). Each researcher session works in its own git
worktree at `<project>/../<project>-worktrees/<name>`, on one branch per task, and the coordinator merges
accepted branches with a merge commit. Sessions exchange recorded messages; a live recipient also gets a
one-line notice. A review round holds two reviews; findings of medium or higher severity left after the
second go to the coordinator, who approves them with a reason or returns the work. Questions, requests for
permission and dialogs from the sessions also go to the coordinator, which decides what it can judge and asks
the user only what only the user can decide. A watcher in its own tab
looks at the sessions every 20 minutes (`agents.watch_minutes`): it starts an agent that exited again and
reminds an idle session whose assignment is unfinished, and leaves recorded waits alone (a task waiting on
cluster jobs, a pending review, a run still running). Without herdr, the same roles run headless
(`rl delegate`, `rl review run`, `rl advise run`).

## Build and install

```bash
uv sync && uv run pytest -q          # develop and test the core
python3 scripts/build_plugins.py     # -> dist/claude, dist/codex (+ zip archives)
```

Claude Code (2.1.91 or later):

```bash
claude --plugin-dir dist/claude/plugins/smooth-research            # one session, no install
# or: /plugin marketplace add <repo path>  then  /plugin install smooth-research@smooth-research
```

Codex (0.160 or later; trust the plugin's hooks when asked):

```bash
codex plugin marketplace add <repo path>
codex plugin add smooth-research@smooth-research
```

The first time, the CLI environment is built once from the bundled, locked package (needs uv):
`<plugin>/bin/rl setup`. The session-start note says so when it is missing. Skills in Claude Code call
`${CLAUDE_PLUGIN_ROOT}/bin/rl`; in Codex the note prints the absolute path.

## A research project

```
smooth-research.yaml     configuration (layout, model lists per role, advice rounds, context targets)
src/                     experiment code
docs/                    human-facing documents: plan/original.md (imported, sealed), plan/constraints.md,
                         experiments/E-xxxx.md (contracts), tasks/, notes/
reports/                 reports: rl report, reviews/RV-xxxx.md, advice/AD-xxxx.md, research-graph.html
research/                machine records: state/ (database, not in git), documents/objects/ (sealed),
                         packages/, artifacts/<attempt>/ (run outputs, not in git), snapshots/
```

The directory names are defaults that `smooth-research.yaml` can change before `rl init`. Every
configuration key, environment variable and command-line override is explained in
[docs/configuration.md](docs/configuration.md) (in Chinese).

Typical loop: `rl init --plan plan.md` -> `rl session start --role coordinator ...` ->
`rl hypothesis add` -> `rl experiment add` -> code in `src/` -> `rl review request/run` for new core
paths -> `rl run register` -> run with your own tool -> `rl run record` -> `rl run import-result` ->
`rl evidence add` -> `rl decide` -> `rl report`. `rl resume` rebuilds the working context in a new
session; `rl export` / `rl restore` back up and restore the records. See
`plugin/skills/smooth-research/references/workflow.md` for formats.
