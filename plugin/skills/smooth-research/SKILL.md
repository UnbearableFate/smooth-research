---
name: smooth-research
description: Plan-driven ML research with an evidence record. Use to start, run or resume a research project from a research plan - turn the plan into testable hypotheses, choose cheap discriminating experiments, run them with your own tools, and record runs, results, evidence and decisions until a formal-experiment proposal, a pause or a stop is justified. Not for ordinary coding outside a research project.
---

# Smooth Research

You coordinate, and usually also execute, evidence-driven ML research. The user's plan sets the
direction; results may change the route. The plugin keeps the record (hypotheses, contracts, runs,
evidence, decisions, reviews, literature); it never runs, polls or cancels experiments for you.

## The CLI

{{cli_note}}
- If it says the environment is missing, run `{{rl}} setup` once (uses uv). Install nothing else for it.
- Commands print a short summary with ids; `--json` gives full records; `{{rl}} <command> --help` shows flags.
- Host-specific notes (sessions, subagents, default reviewer): [references/host.md](references/host.md).
  Exact formats (RunSpec, observation, manifest) and recipes: [references/workflow.md](references/workflow.md).

## Project layout (in the research project root)

- `src/` experiment code · `docs/` human-facing documents (`plan/`, `experiments/`, `tasks/`, `notes/`)
- `reports/` reports (`{{rl}} report`, review and advice answers, `research-graph.html`)
- `research/` machine records: database, sealed documents, packages, `artifacts/<attempt-id>/` run outputs.
  Never edit `research/` by hand.

## Start or resume

1. In the project directory: if `smooth-research.yaml` exists, run `{{rl}} resume --role coordinator` and
   continue from the first unfinished action. Otherwise `{{rl}} init --plan <plan.md>` (the plan is copied to
   `docs/plan/original.md` and sealed; it is never edited afterwards).
2. Register this session: `{{rl}} session start --role coordinator --host {{host}} --host-session-id {{session_id}}
   --model-observed <your model id>`. Pass `--epoch <n>` on coordinator writes
   (decide, task create/accept/stop, hypothesis revise); a stale epoch means another session took over.
3. Fill `docs/plan/constraints.md` from the plan and the user: resource ceiling, allowed environments,
   models. Ask the user only about gaps that change the goal or the budget; fill low-risk details yourself
   and say so.

## From plan to hypotheses

Extract only: the core claim, the key mechanisms and competing explanations, the few experiments that test
them first, the stated resources/metrics/stop conditions, and assumptions not yet shown. Record each claim:
`{{rl}} hypothesis add --title .. --statement "<testable claim>" --scope "<conditions>" [--derived-from H-x]`.
Far branches stay `--disposition candidate`; only near-term work becomes tasks.

## The loop: cheapest test that can change the next decision

Levels are a default order, not a ladder: E0 structure/implementation (shapes, gradients, equivalence,
tiny inputs) · E1 isolated mechanism (synthetic/symbolic data, oracle components, positive and negative
controls) · E2 minimal real model · E3 limited confirmation (pre-fixed comparison, replication, key ablation).

1. Contract: `{{rl}} experiment add --title .. --question .. --hypotheses H-x --stage E1 --intervention ..
   --comparator .. --metric .. --scope .. --outcome-actions "<result -> next step>" --budget .. --stop ..`.
   Edit `docs/experiments/E-x.md` freely until the first run, review or task references it; it is then
   sealed. Later edits become a new revision; edits after seeing results are recorded as exploratory.
2. Write code in `src/`. Before a costly run on a new core operator, training/eval path, loss mask, data
   split, adapter injection or checkpoint I/O, get a cross-model review (skill `research-review`). Smoke tests
   and deterministic checks may run first. Seeds of an already reviewed spec need no new review.
3. Register before running: `{{rl}} run register --experiment E-x --request-id <unique> --arm .. --seed ..
   --code-digest "$({{rl}} digest src/ | cut -d' ' -f1)" (or --code-commit <full sha>) --config-digest ..
   --data-revision .. --metric .. --output-spec research/artifacts/<ATT>/..` -> RUN and ATT ids.
   Write `unknown` for facts you do not have; never guess.
4. Run it with your own tools (shell, background jobs, remote or HPC tools). Record facts as you observe
   them: `{{rl}} run record --attempt ATT --source <tool> --state running|ended [--outcome success|failed|cancelled]
   --exec-namespace host:<hostname> --exec-id <pid or job id> --source-ref <log>`. On a cluster (Miyabi:
   pi-env-mcp and miyabi-mcp) run only from a staged snapshot of the registered commit, submit with the
   attempt id as the request key, record the ledger export (its `export_command` output) with
   `{{rl}} run record --observation @file`, and wait for its notification instead of polling; recipe in
   workflow.md.
5. `{{rl}} run import-result --attempt ATT --request-id <unique> --manifest @<manifest.json>`; optionally
   `{{rl}} run usage ...` with measured time. Unreported usage is not zero.
6. Evidence: `{{rl}} evidence add --summary .. --level diagnostic|screening|exploratory|confirmatory
   --validity valid|invalid|pending --supports|--challenges H-x --attempts ATT --results RES --experiment E-x`.
7. Decide what changes: `{{rl}} decide --targets H-x --action continue|expand|modify|diagnose|consult|pause|stop|reopen
   --rationale .. --based-on EV-x [--set-evidence-status supported|challenged|inconclusive]
   [--reopen-condition ..]`. Every open or uncertain result gets a default next action.

## Keep the record honest

- Execution success, result completeness, evidence validity and scientific support are separate facts.
- An engineering failure, OOM, unused memory or failed positive control is a diagnostic, not a refutation.
- One cheap failure supports "not worth more now", not "the mechanism never works".
- Single-seed or small-validation results are `screening`. Confirmation needs a comparison fixed in advance.
- Never fabricate results, versions, model identities or usage. Unknown stays unknown; a session ending
  is not a cancellation. Rules changed after seeing results are not pre-registration.
- Changing a hypothesis (`{{rl}} hypothesis revise`) leaves old evidence bound to the old revision; re-link
  only after checking that its scope still applies.

## Parallel work, bottlenecks, literature

- Split work into tasks only when it can proceed independently: `{{rl}} task create --title .. --kind ..
  --targets E-x --to-role researcher --next-action ..`. Owners `claim`, then `deliver`, `wait`, `block` or
  `handoff` with `--owner-epoch`; you `accept` or `stop`. See host.md for native subagents and `{{rl}} delegate`.
- Stuck between explanations after valid tests, or before a large cost step: skill `research-advise`.
- `{{rl}} paper search ..`, `{{rl}} paper add <arXiv id|DOI> --fetch`, `{{rl}} paper link H-x P-y --relation nearest_work
  --assessment checked --locator "Sec. 3.2"`. A search hit is not a checked claim.

## Finish a stage

Record the stage delivery with three separate fields:
`{{rl}} decide --stage --targets H-x --action recommend_formal|continue_probe|pause|stop
--implementation-ready "<code, controls, eval, run spec, cost measured?>" --evidence-scope "<what the
evidence supports: diagnostic, exploratory signal or pre-specified comparison>" --rationale ..`,
then `{{rl}} report` (writes `reports/`) and `{{rl}} view`. Report "code ready" and "claim verified" separately,
with costs. At task boundaries `{{rl}} resume` lets a new session continue; `{{rl}} export` makes a backup.
