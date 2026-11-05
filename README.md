# Smooth Research

A lean ML-research plugin for Claude Code and Codex. The user's research plan sets the direction; the
agent turns it into testable hypotheses, runs cheap discriminating experiments with its own tools, and keeps
an honest, recoverable record of runs, results, evidence and decisions until a formal-experiment proposal,
a pause or a stop is justified. Design: `design/ml-research-agent-design-2026-10-30 (1).md`.

The plugin records; it never submits, polls, retries or cancels experiments. Local shells, remote tools,
containers and HPC skills (configured separately) stay independent. Where experiments run is configured
in the project (`compute`: this machine, remote hosts and clusters, reached locally, with ssh or pi-env-mcp, jobs started
by a shell, an MCP server for cluster jobs or a skill); `rl compute steps` prints how to stage a commit there
(git push and fetch, or an archive) as a read-only snapshot, and the agent runs those steps with its own tools.

When the coordinating agent is on a cluster login node, set `compute.coordinator: login`. Configure the job
MCP separately and use `connect: local` with an absolute shared workspace and `code: archive`: source snapshots
and results stay on the shared filesystem, while tests and experiments run through the executor. This mode
rejects checkout/shell resources and requires `--resource` on run registration. See
[configuration](docs/configuration.md#计算资源) for the complete example.

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
second go to the coordinator, who approves them with a reason, resolves them by a passing re-check
(`rl review resolve`) or returns the work. A run's code review check gives each reviewed path that no longer
matches a reason: changed (stale), absent from the code, or a test file. Questions, requests for
permission and dialogs from the sessions also go to the coordinator, which decides what it can judge and asks
the user only what only the user can decide. A watcher in its own tab
looks at the sessions and the coordinator every 5 minutes (`agents.watch_minutes`), through herdr and the
Codex session logs: it starts an agent that exited again, retries a turn cut off by a model service error,
reports a turn that stopped making progress, reminds a session that stayed idle with its assignment unfinished,
and leaves recorded waits alone (a task waiting on cluster jobs, a pending review, a run still running); the
coordinator need not poll. When a Claude Code or Codex subscription window is nearly used up (`rl agent list`;
10% left, or 20% on the coordinator's own host), the researchers and reviewers on
that host hand their work over and the coordinator continues it on the next model of their list
(`rl agent switch`); the coordinator and the advisors keep their models. Without herdr, the same roles run
headless (`rl delegate`, `rl review run`, `rl advise run`).

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

Inside a research project `rl` runs on the project's own environment: `rl init [--python 3.13]` first makes the
project directory a uv project (`uv init --no-workspace`), adds rl's dependencies to it (`uv add`, which creates
`.venv`), and only then creates the records with that environment. rl's code comes from the plugin; the project
pins the libraries in its `uv.lock`. Outside a project, and in projects made before this, `rl` runs on the plugin's
own environment, built from the bundled, locked package (needs uv) once per plugin version: the session-start
hook starts the build in the background and the first `rl` command waits for it (`<plugin>/bin/rl setup` rebuilds
it). Skills in Claude Code call `${CLAUDE_PLUGIN_ROOT}/bin/rl`; in Codex the note prints the
absolute path.

A plugin update never stops a running project. The first command that opens an older project upgrades its records
in place (a copy of the old database is kept in `research/state/backups/`); `rl upgrade` then lists what the agent
adjusts by hand (configuration, files an older layout keeps elsewhere: `rl upgrade --move` moves them with their
recorded paths), and the agent does it without asking, leaving recorded facts and sealed documents as they are.
Frozen projects of every earlier record format (`tests/fixtures/projects/`) must keep working in the tests.

## A research project

```
smooth-research.yaml     configuration (model lists per role, advice rounds, agent sessions, context targets,
                         the manuscript's LaTeX command, compute resources)
src/                     experiment code
docs/                    only what people read: plan.md (imported, sealed), constraints.md,
                         research.html (rl view), the manuscript (rl manuscript: manuscript.md, or
                         manuscript/manuscript.tex and its PDF), explanations/ (written when the user asks)
research/                everything else: experiments/ (contracts), tasks/, notes/, reviews/, advice/,
                         reports/ (rl report), and the records: state/ (database, not in git),
                         documents/objects/ (sealed), packages/, artifacts/<attempt>/ (run outputs, not in git)
```

The layout is fixed. Every configuration key, environment variable and command-line override is explained in
[docs/configuration.md](docs/configuration.md) (in Chinese).

Typical loop: `rl init --plan plan.md` (uv init, uv add, then the records) -> commit -> `rl session start --role coordinator ...`
(pushes for remote `code: git` resources; run the clone step it prints) ->
`rl hypothesis add` -> `rl experiment add` -> code in `src/` -> `rl review request/run` for new core
paths -> `rl run register --resource ..` -> `rl compute steps` -> run with your own tool -> `rl run record` ->
`rl run import-result` ->
`rl evidence add` -> `rl decide` -> `rl report` -> `rl manuscript`. `rl resume` rebuilds the working context in a new
session; `rl export` / `rl restore` back up and restore the records. See
`plugin/skills/smooth-research/references/workflow.md` for formats.

Hypotheses and experiments have a label besides their id, given at creation and never changed: H2.1 is a
hypothesis under H2 (its latest hypothesis source, `--parent` another, `--root` a new line), EH2.1/3 the third
experiment anchored at H2.1 (the deepest common ancestor of what it tests, `--anchor` another). People read the
labels (status, lists, reports, the manuscript); the ids stay in relations, paths, job names and commits, and every
command takes either.
