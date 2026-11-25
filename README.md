# Smooth Research

A plugin for Claude Code and Codex, and the `rl` command-line tool behind it, for evidence-driven ML research. The
user's research plan sets the direction. A team of agent sessions turns it into a tree of testable hypotheses,
designs experiments, runs them, and records runs, results, evidence and decisions in one database per project,
until a hypothesis has a reviewed verdict or the research stops. The design documents are under
`design/site/content/` (`bash design/site/build.sh` renders them).

rl records; it never submits, polls, retries or cancels experiments. The agents run experiments with their own
tools: a local shell, ssh, pi-env-mcp, or an MCP server or skill that submits cluster jobs. Where experiments run is
configured per project (`compute` in `smooth-research.yaml`); `rl compute steps` prints how to stage a commit there
as a read-only snapshot, run it and fetch the results, and the agent runs those steps.

## How a project runs

A project runs as five roles, each an interactive Claude Code, Codex or pi session in its own tab of a
herdr workspace (herdr is a terminal multiplexer for coding agents). A session is one role on one thing, named `<role>-<subject>`:

| Session | Works on |
| --- | --- |
| `coordinator` | the project; the user talks to it in its pane |
| `scientist-T-0002` | a tree task (build the hypothesis tree from the plan), a hypothesis task (test one hypothesis to a verdict), a phase task (write the phase conclusion), or a preparation task handed to it |
| `researcher-T-0003` | an execute task (run a sealed experiment design, record results and evidence) or a preparation task |
| `reviewer-RV-0004` | a review round |
| `advisor-AD-0001` | an advice ticket: a consultation, or a ruling on a review round that ran out of passes |

Nothing is passed by chat. A watcher (`rl watch run`, in its own tab, started by `rl resume`) computes from the
records what waits on whom (the rules table in `inbox.py`) and acts every `agents.beat_seconds` (30 s by default): it opens a session for a
recipient that has none, types a one-line notice into an idle pane, records a dialog in a pane for the coordinator
to answer (`rl agent dialog/answer`), closes the sessions whose task or round is finished, escalates items that
wait past their deadline (the holder, then whoever made the task, then the coordinator, then the user) and redraws
the research page `docs/research.html`. Each session starts by reading `rl pack`: its role card, the card of its
task or round, what waits on it, and the material it needs. When nothing waits on it, it ends its turn.

Each task the scientist or researcher claims gets a branch `sr/T-x` and a worktree beside the project
(`<project>-worktrees/T-x`). The mainline moves only when the CLI accepts a task: a merge commit of the mainline on
the task branch, the task's tests on that commit, then a fast-forward of the main checkout.

A review round is one ticket with one reviewer session. Findings have a category (method or engineering), a
severity and a status that only the reviewer (or an advisor's ruling) closes. A design round blocks the dispatch of
an experiment while a method finding is open, and its evidence runs while a method finding or one of medium or
higher severity is open; a verdict stays a draft until a conclusion round passes. A round segment holds at most 3
passes; a round still blocked after them escalates.

Which model each role runs on is set per project (`models.<role>`: the first available entry of the list). A host
whose subscription window is nearly used up gets no new sessions while another entry is available; its idle
sessions other than the coordinator's are closed and reopened on the next entry.

## Build and install

```bash
uv sync && uv run pytest -q tests/      # develop and test the core
python3 scripts/build_plugins.py        # -> dist/claude, dist/codex (+ zip archives)
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

A project also needs herdr (`rl resume` and the watcher run only inside a herdr pane, `HERDR_ENV=1`) and uv.

Inside a research project `rl` runs on the project's own environment: `rl init [--python 3.13]` first makes the
project directory a uv project (`uv init --no-workspace`), adds rl's dependencies to it (`uv add`, which creates
`.venv`), and only then creates the records with that environment. rl's code comes from the plugin; the project
pins the libraries in its `uv.lock`. Outside a project, `rl` runs on the plugin's own environment, built from the
bundled, locked package (needs uv) once per plugin version: the session-start hook starts the build in the
background and the first `rl` command waits for it (`<plugin>/bin/rl setup` rebuilds it). Skills in Claude Code
call `${CLAUDE_PLUGIN_ROOT}/bin/rl`; in Codex the host note prints the absolute path.

A plugin update never stops a running project. The first command that opens a project in an older record format
upgrades it in place (a copy of the old database is kept in `research/state/backups/`); `rl upgrade` lists what
the agent adjusts by hand. Projects made before the 2026-11 redesign (record format below 5) are not opened:
continue them with the plugin version that made them, or start a new project from their plan.

## A typical start

```bash
rl init --plan plan.md        # uv init, uv add, then the records; the plan is sealed as docs/plan.md
git add -A && git commit -m "Start the research project"   # task branches start from the mainline
$EDITOR docs/constraints.md   # goal and claims in scope, data, resource ceiling, actions that need the user
rl resume                     # in a herdr pane: the watcher tab and the coordinator's pane, then the status
```

The coordinator's first step is the tree task: a scientist builds the hypothesis tree from the plan, a reviewer
checks it, and the coordinator asks the user to approve it and the first phase. From then on the watcher opens
scientists for the released hypothesis tasks, and they create execute and preparation tasks for researchers.
`rl status` shows the state without starting anything; `rl resume` later continues the project. `rl export` and
`rl restore` back up and rebuild the records; `rl --read-only <command>` reads a project without changing anything.

## A research project

```
smooth-research.yaml     configuration: model lists per role, launch arguments and the watcher's times,
                         limits, the usage threshold, compute resources
src/                     experiment code
docs/                    what people read: plan.md (sealed), constraints.md, research.html (drawn by the
                         watcher, not in git), explanations/, glossary.md
research/                state/ (database, lock, watcher heartbeat; not in git), documents/objects/ (sealed),
                         snapshots/ (rl export), literature/, artifacts/<attempt>/ (local run outputs, not in
                         git), work/T-x/ (task documents, written in the task's worktree)
../<project>-worktrees/  one git worktree per task, on its branch sr/T-x
```

The layout is fixed. Every configuration key, environment variable and the watcher's rules are explained in
[docs/configuration.md](docs/configuration.md) (in Chinese). Formats and recipes for manifests, results,
observations, compute resources and cluster jobs are in `plugin/skills/smooth-research/references/workflow.md`.

## Repository layout

| Path | Role |
| --- | --- |
| `src/smooth_research/` | The Python core and the `rl` CLI (modules below) |
| `plugin/` | Host-independent plugin content: `bin/rl` (environment bootstrap and launcher) and three skills (`smooth-research`, `research-review`, `research-advise`) |
| `hosts/claude/` | Claude Code adapter: `.claude-plugin/plugin.json`, the SessionStart hook, host notes, placeholder values |
| `hosts/codex/` | Codex adapter: `.codex-plugin/plugin.json`, the SessionStart hook, host notes, placeholder values |
| `scripts/build_plugins.py` | Assembles the `dist/claude` and `dist/codex` marketplaces (shared core plus one adapter) |
| `tests/` | Tests, with frozen projects of each supported record format in `tests/fixtures/projects/` |

Modules of `src/smooth_research/`:

| Module | What it holds |
| --- | --- |
| `cli.py` | The `rl` entry point: argument parser, `--as`, `--read-only`, output and exit codes |
| `project.py` | Project discovery, the fixed layout, `smooth-research.yaml` with its defaults and warnings, compute resource parsing |
| `store.py` | The embedded LatticeDB store: short serialized write transactions, the lock, format checks |
| `schema.py` | Record types, enumerations and relations (record format 5) |
| `upgrade.py` | Record-format migrations with a backup, `rl upgrade` and `rl upgrade --move` |
| `research.py` | `rl init`, hypotheses, experiments, evidence, decisions, links, the tree |
| `labels.py` | Hierarchical labels (H2.1, EH2.1/3) for hypotheses and experiments |
| `tasks.py` | Tasks (tree, hypothesis, phase, prep, execute), phases, decision requests, dispatch, delivery and acceptance |
| `gitops.py` | Task branches and worktrees, protected scope, the merge on acceptance |
| `manifest.py` | The experiment manifest (arms, seeds, split, config, metrics) |
| `reviews.py` | Review rounds with method and engineering findings, advice tickets and rulings |
| `messages.py` | Questions, answers and review discussion |
| `refs.py` | Locators in notes and messages, checked when written and expanded when read |
| `documents.py` | Sealed, content-addressed documents |
| `runs.py` | Runs, attempts, observations, `rl run ended`, results and usage |
| `compute.py` | `rl compute`: resources, staging steps, `rl compute sync` |
| `usage_view.py` | Compute usage per task against its envelope |
| `verify.py` | Check lines under a conclusion, computed when read |
| `literature.py` | Papers: identifiers, metadata, full-text access, claim checks |
| `inbox.py` | The rules table: to-do items computed from the records, deadlines and escalation |
| `pack.py` | `rl pack`: the one screen a session reads |
| `cards.py` | Role cards, the first block of every pack |
| `roles.py` | Who is calling: session names and role checks |
| `watch.py` | The watcher: opening, telling, clearing and closing sessions, the heartbeat, the page redraw |
| `launch.py` | Starting an agent in a herdr tab, launch arguments per host, folder trust, dialogs |
| `herdr.py` | Calls to the herdr CLI |
| `adapters.py` | Model entries (`<claude|codex|pi|manual>:<model>[@effort]`) and model families |
| `usage.py` | Subscription windows of Claude Code and Codex, `rl usage statusline` |
| `page.py`, `page_assets/` | The research page `docs/research.html` |
| `hooks.py` | The SessionStart hook shared by both hosts |
| `snapshot.py` | `rl export`, `rl restore`, `rl doctor` |
| `util.py` | Errors, time, digests, canonical JSON, atomic files |

## Tests

```bash
uv run pytest -q tests/
```

When the record format changes, raise `SCHEMA_VERSION`, add a migration to `upgrade.MIGRATIONS` and freeze a
project of the old format in `tests/fixtures/projects/`; the frozen projects must keep working.
