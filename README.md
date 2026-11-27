# Smooth Research

A plugin for Claude Code and Codex, and the `rl` command-line tool behind it, for evidence-driven ML research. The
user's research plan sets the direction. A team of agent sessions turns it into a tree of testable hypotheses,
designs experiments, runs them, and records runs, results, evidence and decisions in one database per project,
until a hypothesis has a reviewed verdict or the research stops. The current design is
`design/smooth-research-redesign-2026-11-27.md` (in Chinese); earlier ones are under `design/old/`.

The plugin only guides and records. It never refuses an agent's work for the process's sake and never schedules
anyone: the CLI refuses only what it cannot record, or what would be recorded falsely; any other step outside the
process is done, flagged on the record and shown to reviewers, the coordinator and the user.

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
| `scientist-T-0002` | a hypothesis task (the whole test of one hypothesis, to a verdict), a phase task (write the phase conclusion), or a preparation task handed to it |
| `researcher-T-0003` | an execute task (run a sealed experiment design, record results and evidence) or a preparation task |
| `reviewer-RV-0004` | a review round |
| `advisor-AD-0001` | a consultation a scientist asked for |

Nothing is passed by chat. What waits on whom is computed from the records (the rules table in `inbox.py`). Work
moves by the agents' own commands: the command that hands work on tells the next session (releasing a task tells
its scientist, dispatching tells the researcher, a delivery tells the task's creator, a review request the
reviewer, and so on). It types a one-line notice into an idle pane; a busy session gets the new items from its Stop
hook when it ends its turn; a session that is not open is opened. When telling fails the command still succeeds
and says how to open the session by hand (`rl session open NAME`, or `rl resume`). Each session starts by reading
`rl pack`: its role card, the card of its task or round, what waits on it, and the material it needs. When nothing
waits on it, it ends its turn. Sessions are closed by the command that ends their work, only while idle.

The observer `rl watch` (in its own tab, started by `rl resume`) is optional and read-only: it redraws the
research page `docs/research.html`, delivers notices that did not get through to open idle sessions, reminds once
of an item waiting past `agents.deadlines`, and records sessions at a dialog for the coordinator to answer
(`rl agent dialog/answer`). It opens, claims and closes nothing; without it research goes on.

The hypothesis tree holds research claims only: what it takes to test one (data, baselines, calibration, checks)
is tasks under its hypothesis task, and one scientist owns the whole test. The coordinator has the tree drafted
from the plan by a subagent (`rl tree prompt`), the user approves the draft on the research page, and
`rl tree record` records it. New hypotheses come when a verdict raises a new question.

A task gets its holder, a branch `sr/T-x` and a worktree beside the project (`<project>-worktrees/T-x`) when it is
released or made; one that depends on tasks not yet accepted waits until they are. The mainline moves only when
the CLI accepts a task: a merge commit of the mainline on the task branch, the task's tests on that commit, then a
fast-forward of the main checkout. Failing tests stop it unless `rl task accept --force --reason ..`; a merge
conflict always does.

A review round is one author, one target (a task's commit or a draft conclusion) and one reviewer session, with at
most two reviews: review, fix, recheck, fix. Findings have a category (method or engineering) and a severity; the
author may reject one with a reason (`rl review reject`). Going on with findings open, or without a review, is
flagged, not refused; a verdict stays a draft until `rl decide --final`.

Which model each role runs on is set per project (`models.<role>`: the first available entry of the list, chosen
when the session opens). A host whose subscription window is nearly used up is tried last; an open session keeps
its model.

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

Codex (0.160 or later; trust the plugin's hooks when asked: Codex asks again, "Hooks need review", after each
plugin update):

```bash
codex plugin marketplace add <repo path>
codex plugin add smooth-research@smooth-research
```

A project also needs herdr to open and tell sessions (`HERDR_ENV=1` in the pane) and uv. Without herdr the
commands still record, and `rl session open NAME --print` prints the command that starts a session by hand.

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
rl resume                     # in a herdr pane: the coordinator's pane, sessions with work, the observer; status
```

The coordinator's first step is the hypothesis tree: its subagent drafts it from the plan, the user reads the
draft on the research page and approves it, and `rl tree record` records it. The coordinator then releases a
hypothesis task; its scientist creates preparation and execute tasks for researchers, many at once.
`rl status` shows the state without starting anything: what waits on the user, what was not done by the process
(flags), sessions with work but none open, sessions whose work ended. `rl resume` runs once and can be run again
whenever something seems stuck. `rl export` and `rl restore` back up and rebuild the records; `rl --read-only
<command>` reads a project without changing anything.

## A research project

```
smooth-research.yaml     configuration: model lists per role and for the tree subagent, launch arguments, the
                         observer's times, envelope lines, the usage threshold, compute resources
src/                     experiment code
docs/                    what people read: plan.md (sealed), constraints.md, research.html (drawn by rl view
                         or the observer, not in git), explanations/, glossary.md
research/                state/ (database, lock, observer heartbeat; not in git), documents/objects/ (sealed),
                         snapshots/ (rl export), literature/, artifacts/<attempt>/ (local run outputs, not in
                         git), work/T-x/ (task documents, written in the task's worktree), tree-draft/ (the
                         tree draft until it is recorded)
../<project>-worktrees/  one git worktree per task, on its branch sr/T-x
```

The layout is fixed. Every configuration key, environment variable and the observer's rules are explained in
[docs/configuration.md](docs/configuration.md) (in Chinese). Formats and recipes for manifests, results,
observations, compute resources and cluster jobs are in `plugin/skills/smooth-research/references/workflow.md`.

## Repository layout

| Path | Role |
| --- | --- |
| `src/smooth_research/` | The Python core and the `rl` CLI (modules below) |
| `plugin/` | Host-independent plugin content: `bin/rl` (environment bootstrap and launcher) and three skills (`smooth-research`, `research-review`, `research-advise`) |
| `hosts/claude/` | Claude Code adapter: `.claude-plugin/plugin.json`, the hooks (SessionStart, Stop, UserPromptSubmit), host notes, placeholder values |
| `hosts/codex/` | Codex adapter: `.codex-plugin/plugin.json`, the hooks (SessionStart, Stop, UserPromptSubmit), host notes, placeholder values |
| `scripts/build_plugins.py` | Assembles the `dist/claude` and `dist/codex` marketplaces (shared core plus one adapter) |
| `tests/` | Tests, with frozen projects of each supported record format in `tests/fixtures/projects/` |

Modules of `src/smooth_research/`:

| Module | What it holds |
| --- | --- |
| `cli.py` | The `rl` entry point: argument parser, `--as`, `--read-only`, output and exit codes |
| `project.py` | Project discovery, the fixed layout, `smooth-research.yaml` with its defaults and warnings, compute resource parsing |
| `store.py` | The embedded LatticeDB store: short serialized write transactions, the lock, format checks |
| `schema.py` | Record types, enumerations and relations (record format 6) |
| `upgrade.py` | Record-format migrations with a backup, `rl upgrade` and `rl upgrade --move` |
| `research.py` | `rl init`, hypotheses, experiments, evidence, decisions, links, the tree view |
| `tree.py` | `rl tree prompt`, `check`, `record`: the tree draft written by the coordinator's subagent |
| `labels.py` | Hierarchical labels (H2.1, EH2.1/3) for hypotheses and experiments |
| `tasks.py` | Tasks (hypothesis, phase, prep, execute), phases, decision and approval requests, release, dispatch, delivery and acceptance |
| `flags.py` | Flags: what was done outside the process, written on the record instead of refusing it |
| `gitops.py` | Task branches and worktrees, protected scope, the merge on acceptance |
| `manifest.py` | The experiment manifest (arms, seeds, split, config, metrics) |
| `reviews.py` | Review rounds (two reviews each) with method and engineering findings, rejections, advice tickets |
| `messages.py` | Questions, answers and review discussion |
| `refs.py` | Locators in notes and messages, checked when written and expanded when read |
| `documents.py` | Sealed, content-addressed documents |
| `runs.py` | Runs, attempts, observations, `rl run ended`, results and usage |
| `compute.py` | `rl compute`: resources, staging steps, `rl compute sync` |
| `usage_view.py` | Compute usage per task against its envelope |
| `verify.py` | Check lines under a conclusion, computed when read |
| `literature.py` | Papers: identifiers, metadata, full-text access, claim checks |
| `inbox.py` | The rules table: to-do items computed from the records, deadlines |
| `pack.py` | `rl pack`: the one screen a session reads |
| `cards.py` | Role cards, the first block of every pack |
| `roles.py` | Who is calling: session names and role checks |
| `sessions.py` | Opening, telling and closing sessions, called by the commands that hand work on; `rl session` |
| `watch.py` | The optional observer: the page redraw, notices that did not get through, reminders, dialogs, the heartbeat |
| `launch.py` | Starting an agent in a herdr tab, launch arguments per host, folder trust, dialogs |
| `herdr.py` | Calls to the herdr CLI |
| `adapters.py` | Model entries (`<claude|codex|pi|manual>:<model>[@effort]`) and model families |
| `usage.py` | Subscription windows of Claude Code and Codex, `rl usage statusline` |
| `page.py`, `page_assets/` | The research page `docs/research.html` |
| `hooks.py` | The SessionStart, Stop and UserPromptSubmit hooks shared by both hosts |
| `snapshot.py` | `rl export`, `rl restore`, `rl doctor` |
| `util.py` | Errors, time, digests, canonical JSON, atomic files |

## Tests

```bash
uv run pytest -q tests/
```

When the record format changes, raise `SCHEMA_VERSION`, add a migration to `upgrade.MIGRATIONS` and freeze a
project of the old format in `tests/fixtures/projects/`; the frozen projects must keep working.
