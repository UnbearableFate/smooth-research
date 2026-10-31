# Smooth Research: formats and recipes

Read the section you need; the main skill has the loop. `{{rl}}` is the CLI (see SKILL.md). The layout is
fixed: `src/` code, `docs/` what the user reads, `research/` everything else (SKILL.md, Project layout).

## A local run, end to end

```bash
# 1. register (nothing starts); keep the printed RUN / ATT ids. Its "code review" line says whether an
#    answered review still covers this code; add --no-review "<reason>" when the run needs none
{{rl}} run register --experiment E-0001 --request-id E-0001-wd-s0 --arm wd1e-2 --seed 0 \
  --code-digest "$({{rl}} digest src/ | cut -d' ' -f1)" --config-digest unknown \
  --data-revision "synthetic-v1 (generator in src/data.py, n=2000)" --metric "test accuracy" \
  --output-spec "research/artifacts/ATT-0001-1/metrics.json" --param wd=0.01
# 2. run with your own tool; keep the PID and a log
mkdir -p research/artifacts/ATT-0001-1
nohup python src/train.py --wd 0.01 --seed 0 --out research/artifacts/ATT-0001-1 \
  > research/artifacts/ATT-0001-1/log.txt 2>&1 & echo $!
{{rl}} run record --attempt ATT-0001-1 --source local-shell --state running \
  --exec-kind local_process --exec-namespace "host:$(hostname):$(cat /proc/sys/kernel/random/boot_id)" \
  --exec-id <pid> --source-ref research/artifacts/ATT-0001-1/log.txt
# 3. when the process has exited (check it yourself), record the end with what you saw
{{rl}} run record --attempt ATT-0001-1 --source local-shell --state ended --outcome success \
  --source-ref "exit code 0; research/artifacts/ATT-0001-1/log.txt"
# 4. import the result manifest
{{rl}} run import-result --attempt ATT-0001-1 --request-id ATT-0001-1-result \
  --manifest @research/artifacts/ATT-0001-1/manifest.json
```

Short runs can be recorded after the fact with one `--state ended` record (give `--observed-at` when the
fact was observed earlier). A crash is `--state ended --outcome failed`; a lost process you cannot check is
`--state unknown`. A re-execution of the same RunSpec is a new attempt: `{{rl}} run register --experiment E-0001
--run RUN-0001 --request-id <new>`. A changed config or code is a new run. `--review RV-x` adds a code review
that targets something other than E-0001 (or the experiment its code comes from) to the coverage check.

## Compute resources and remote runs

`compute` in `smooth-research.yaml` names where experiments run; `{{rl}} compute` lists the resources and
checks what it can see locally (ssh, the git remote, whether a commit is pushed as of the last push or fetch):

```yaml
compute:
  default: miyabi                    # used unless a task or the coordinator says otherwise
  resources:
    local: {kind: local}             # this machine: code from your checkout, outputs in research/artifacts/<ATT>/
    miyabi:
      host: miyabi-g                 # the ssh alias, or pi-env-mcp's host name
      connect: pi-env-mcp:miyabi-g   # ssh, or pi-env-mcp:<MCP server name>
      jobs: mcp:miyabi-pbs           # shell (a background process), mcp:<server> or skill:<name>
      workspace: /work/<group>/<user>/<project>   # absolute; for miyabi-mcp its --project-dir
      code: git                      # git: push, the host fetches (git_remote: origin, git_url: <URL there>)
                                     # archive: git archive, uploaded
```

A remote run executes one commit, staged once as the read-only snapshot `<workspace>/snapshots/<commit>/`:
the branch or worktree it came from does not matter, and a branch that moves on cannot change a queued job.
Workspace layout: `repo.git` (a bare clone the host fetches into, code: git), `snapshots/<commit>/`,
`runs/<ATT>/` (one directory per attempt), and for miyabi-mcp `.miyabi-mcp/` (its ledger and PBS logs; do not
edit). One workspace per research project, never shared: request keys are unique only inside it.

Code by git, at the start: the coordinator's `{{rl}} session start` runs `{{rl}} compute sync`. For the resources
with `code: git` it pushes the current branch to their git remote. When that remote is missing and
`compute.repository` is `github-private` (the default), it first creates a private GitHub repository named
after the project directory (`gh repo create --private`) and adds it as the remote. It creates none for a project
that is part of a larger repository, when `git_url` is set, or with `none` (then ask the user where the code
goes). It then prints, per resource, a script that clones the repository into `<workspace>/repo.git` (or
fetches it) and checks that the host has the pushed commit: run it at once through that resource's connection,
so a host that cannot read the repository shows up before the first run (it needs `gh auth login` there, or a
deploy key). Before a run, a researcher runs `{{rl}} compute sync` in its worktree to push its task branch.

1. Commit the code (sources only: no `__pycache__`, outputs or data), then register with `--code-commit
   <full sha> --resource <name>`. The code review check reads the files of that commit, not the working tree.
2. `{{rl}} compute steps --attempt ATT` prints the steps for that attempt's commit and resource: push your task
   branch (code: git, `{{rl}} compute sync`; the host needs read access to the repository), then one
   script for the host that fetches the commit into `repo.git` and extracts it read-only; or (code: archive)
   `git archive` streamed over ssh, or written locally and uploaded with pi-env-mcp, then extracted. Run them
   with Bash, ssh or the pi-env-mcp tools as printed; each is safe to repeat. rl runs none of it.

The rest of this section is Miyabi with miyabi-mcp (`jobs: mcp:<its server>`): it submits, queries, cancels and
waits for PBS jobs and exports observations; pi-env-mcp moves files and runs remote commands; `{{rl}}` only
records. Submit and cancel only through miyabi-mcp: a `qsub` run through pi-env-mcp bypasses its ledger and
its limits. Pass absolute remote paths: relative ones follow the remote shell's `cd`. The Python environment
is built on the login node of the target system, outside the snapshot.

3. The job script is part of the snapshot, runs code from it and writes only to `<workspace>/runs/$SR_ATTEMPT_ID/`,
   ending with a `manifest.json` (format below) that carries `spec_digest` and artifact digests written as
   `blake2b:<hex>` (Python: `hashlib.blake2b(data, digest_size=32)`).
4. Submit with miyabi-mcp `job_submit`: dry run first (show the plan when the user wants to confirm
   submissions), then submit with `script: <workspace>/snapshots/<sha>/<job script>`, `workdir:
   <workspace>/snapshots/<sha>` (both read-only, so miyabi-mcp's check that the code cannot change passes), `name:
   "<ATT>"`, `request_key: "sr:<ATT>"`, `client_ref: "<ATT>"`, `env: {SR_ATTEMPT_ID, SR_SPEC_DIGEST}`,
   `labels: {code_commit: <sha>}` and, for jobs you will analyse together
   (the seeds of a comparison), one notification: `notify: {name: "<E>-<batch>", expected: <number of jobs>,
   message: "<what to do next>"}`. PBS logs go to `.miyabi-mcp/logs/` (`job_get` shows their tail). The same
   request key returns the same job, so a retried submit is safe; a new attempt needs a new key.
5. Record without copying JSON by hand: run the `export_command` from the submit result in a local shell,
   `<export_command> > research/artifacts/miyabi/observations.jsonl`, then
   `{{rl}} run record --observation @research/artifacts/miyabi/observations.jsonl`. It exports every job of the
   remote project and records already received are skipped, so repeat it after submitting and after every
   notification. The ended line carries the measured usage (`node_hours` on G nodes, `mig_instance_hours` on
   MIG queues): do not record it again with `{{rl}} run usage`.
6. Wait without polling; waiting belongs to the coordinator (host.md). After the first submit of a batch a
   researcher ends with `{{rl}} task wait T-x --role .. --attempts ATT-.. --next-action ..` and reports what
   announces the end (the listener command, or the deliver target it set). The coordinator starts the
   listener, then keeps working or ends the turn. The first message is either an early failure of one job
   (start the listener again to wait for the rest) or the batch summary. The coordinator acknowledges it with
   `notify_ack(id, ack_token)` from the message and starts the researcher again with the same role and the
   message; the researcher exports and records (step 5). Jobs of a few minutes can use `job_wait` instead.
7. After the end (PBS exit 0 is not application success): download `<workspace>/runs/<ATT>/manifest.json` and
   the small files it lists into `research/artifacts/<ATT>/` (`{{rl}} compute steps --attempt ATT` prints the
   transfer); list checkpoints and other large files as `{"uri": "<resource>:<absolute path>"}` (recorded as not
   read). Then `{{rl}} run import-result`.

Without miyabi-mcp (`jobs: skill:miyabi-pbs-cmd`), submit with the miyabi skills through pi-env-mcp `bash`, from the
snapshot, and record by hand:
`{{rl}} run record --attempt ATT --source "qstat -H -f" --state ended --outcome success --raw-state "F Exit_status=0"
--exec-kind hpc_job --exec-namespace miyabi-pbs --exec-id <full job id>`, plus `{{rl}} run usage
--accounting-ref miyabi-pbs:<job id> ...` from `resources_used.walltime` x nodes.

## RunSpec fields

`arm` (required), `code_ref` = `{commit: <full 40-hex>}` and/or `{content_digest: "blake2b:..."}` (or
`unknown`), `config_digest` or `config_ref`, `data_revision`, `metric` or `metric_spec_ref`, `output_spec`
(all required; write `unknown` if you do not know); optional `seed`, `replicate`, `model_revision`,
`environment_ref`, `params` (object), `resume_from` (checkpoint and its origin), `notes`. The tool adds
`experiment_ref` and `protocol_ref` (the sealed contract). Branch names, HEAD and short hashes are refused.
Same `--request-id` with the same content returns the earlier record; with different content it is refused.

## Observation (flags, or `--observation` with one JSON object, a JSON array or JSON Lines)

```json
{"record_id": "miyabi-pbs:3731052.opbs:2", "attempt_id": "ATT-0003-1", "source": "miyabi-mcp",
 "observed_at": "2026-10-30T05:43:10+09:00", "occurred_at": "2026-10-30T05:42:26+09:00", "source_sequence": 2,
 "execution_state": "running", "outcome": "unknown", "raw_state": "R", "source_ref": "qstat -ll @miyabi-g1",
 "execution_ref": {"kind": "hpc_job", "executor_namespace": "miyabi-pbs", "opaque_id": "3731052.opbs",
                   "locator": "<miyabi-mcp record>"}}
```

States: prepared, queued (optional), running, ended, unknown. Outcomes: success, failed, cancelled,
unknown (only `ended` carries an outcome). `observed_at` is when the tool looked; `occurred_at` (optional) is
the event time the source itself reports, kept as evidence. The attempt's state is derived from the
observations: within one source the `source_sequence` orders them, otherwise `observed_at`; an older
`running` never overrides a recorded end; a failed lookup after the end only adds an issue; conflicting ends
mark the attempt for checking. Correct a wrong record explicitly with `--correction-of <record_id> --note
"<evidence>"`. Several attempts may share one execution ref (one job running several seeds). A batch is
recorded in order; records already received are skipped, so re-sending a whole export is safe, and flags such
as `--attempt` fill in or override every record.

## Result manifest

```json
{"status": "complete", "spec_digest": "<from run register, optional>",
 "code_ref": {"content_digest": "blake2b:..."}, "data_revision": "synthetic-v1 (...)",
 "metric": "test accuracy", "metrics": {"test_acc": 0.812, "train_loss": 0.41},
 "artifacts": [{"path": "research/artifacts/ATT-0001-1/metrics.json", "role": "metrics"},
               {"uri": "miyabi:/work/.../ckpt.pt", "digest": "blake2b:...", "role": "checkpoint"}],
 "validator": {"name": "check_metrics.py", "passed": true, "source_ref": "stdout"}}
```

`spec_digest` names the RunSpec the job ran. Copy `code_ref`, `data_revision` and `metric` from the RunSpec
exactly (`{{rl}} show RUN-x`), or leave them out: a differently worded field marks the result as not usable for
the planned comparison. `arm`, `seed` and `replicate` may be left out too; a manifest that names them names this
run's. `partial` results never replace a `complete` one, in tables too; a later import is a new revision. Local paths are read and
digested; remote URIs are only registered ("not read"). Fields that differ from the RunSpec are kept, and the
result is marked unusable for the pre-specified comparison. `{{rl}} run reject-result RES-x --reason ..` if
the result itself is wrong: the evidence drawn from it, the hypotheses that evidence bears on and the decisions
based on either are flagged for re-checking.

## Usage

`{{rl}} run usage --accounting-ref "local:$(hostname):ATT-0001-1" --quantity 42 --unit cpu_s --resource-type
compute --hardware "<cpu model>" --basis measured --attempts ATT-0001-1`. A shared job: one accounting ref,
several `--attempts`; never split it into invented per-run numbers. Different units are never added.

## Evidence, decisions, revisions

- `--level`: diagnostic (implementation/measurement checks), screening (cheap direction finding),
  exploratory (in-model signal), confirmatory (pre-fixed comparison with adequate replication).
- `{{rl}} evidence set EV-x --validity invalid --reason ..` flags the hypotheses and decisions that used it.
- `{{rl}} hypothesis revise H-x --reason .. --statement|--scope .. --base-revision <n>`; then re-link evidence
  that still applies: `{{rl}} link EV-x supports H-x` (binds to the new revision).
- `{{rl}} link` relations: derived_from (scientific source), code_from (code start), compared_with
  (comparator), depends_on, combines, tests, supports, challenges, motivated_by, nearest_work, cites,
  based_on, supersedes. Science source, code origin and comparator are separate on purpose.
- Pause needs `--reopen-condition`. A paused or retired hypothesis is reopened only by `--action reopen`.
- Key conclusions (`--set-evidence-status supported|challenged`, `--action stop`, a stage `recommend_formal`):
  `{{rl}} review conclusion --target H-x --proposal .. --author-model ..` first, then `{{rl}} decide ...
  --based-on EV-x,RV-x` (skill research-review). A review covers the hypotheses it targets: a decision on H-x
  and H-y needs a passing review of each.

## Tasks, messages, sessions

- `{{rl}} task create --title .. --kind framework|implement|experiment|analysis|literature|review|advice|other
  --targets E-x --request research/tasks/T-brief.md --to-role researcher --next-action ..`
- Owner: `{{rl}} task claim T-x --role researcher` (one claimant wins); then `{{rl}} task deliver T-x
  --role researcher --summary .. --output <files>`, `task wait .. --attempts ATT-x --next-action ..`,
  `task block .. --reason engineering|model_unavailable|budget|science`, `task resume` (also to continue a
  task your role already owns, for example after a WAIT),
  `task handoff .. --doc research/tasks/T-x-handoff.md` (the task is then ready to claim again). Only the
  owner updates a task, and not after the coordinator accepted or stopped it (`task reopen` gives it back).
- Coordinator: `{{rl}} inbox --role coordinator` (add `--ack` after reading), `{{rl}} task accept|stop|reopen T-x
  --reason ..`. Stopping a task does not stop external runs: stop them with your tool and record that.
- Rotating the coordinator: write a handoff note, end your session (`{{rl}} session end S-x`); the new session
  registers as coordinator and starts from `{{rl}} resume`.

## Literature

`{{rl}} paper search "<query>"` (arXiv; failures are logged as failures) -> `{{rl}} paper add <arXiv id | DOI>
--fetch` -> `{{rl}} paper link H-x P-y --relation nearest_work|motivated_by|cites --assessment
search_hit|metadata_checked|fulltext_read|checked [--locator "Sec 3.2, Eq. 4"] [--note-file research/notes/..]`.
`checked` needs a locator. Linking the same claim again updates its assessment, locator and note; after
`{{rl}} hypothesis revise`, link it again: the new revision gets its own link. "No identical method found" is not a
novelty proof.

## Views, backups, integrity

`{{rl}} status`, `{{rl}} resume`, `{{rl}} graph`, `{{rl}} show <id>` (a DOC id prints the sealed text),
`{{rl}} history [id]`, `{{rl}} report` (research/reports/), `{{rl}} view` (docs/research-graph.html), `{{rl}} export`
(consistent snapshot into research/snapshots/), `{{rl}} restore <snapshot> --into <new dir>`, `{{rl}} doctor`.

## After a plugin update

A plugin update never stops a project.

- **Environment.** The first `{{rl}}` command of a new plugin version builds its environment (the session-start
  hook starts it in the background; concurrent sessions wait for one build).
- **Records.** The first command that opens an older project upgrades its records in place: the database files
  are copied to `research/state/backups/schema-<old>-<time>/` first, the migrations only add or fill in, and a
  `project.upgrade` event says what changed. The command that did it prints a warning; nothing else changes.
- **What is left to you.** `{{rl}} status` and the session note say when the plugin changed. Run `{{rl}} upgrade`: it
  creates directories the new layout adds, lists files an older layout keeps elsewhere, configuration warnings,
  missing contract files and record problems, and the rules for adjusting. Adjust the old research assets
  yourself without asking the user, unless a change would alter the goal, the claims in scope, the data or the
  budget.
- **Moving files.** Records keep the paths of contracts and documents, so a plain `mv` breaks them:
  `{{rl}} upgrade --move <old> <new>` moves a file or directory and rewrites those paths in one transaction (the
  files go back if it fails). Where a file was sealed from stays as recorded. Commit the move.
- **Facts stay.** Never rewrite observations, results, evidence, decisions, reviews or sealed documents; when the
  new version wants them in another form, add a new record or revision that cites the old one.
- **Record it.** `{{rl}} upgrade --note "<what you adjusted and why>"` (a `project.adjusted` event), then commit.
- **A newer project.** A project written by a newer plugin is read but not written (`UNSUPPORTED`): tell the
  user to update the plugin on this machine.

## The manuscript

`{{rl}} manuscript` writes `docs/manuscript.md` from the records: a section per part of a paper (abstract,
introduction, related work, method, results, discussion, conclusion), each with a `[write: ...]` placeholder
for your prose and blocks of record facts (hypotheses, literature, contract fields, per-arm tables, evidence
with levels, decisions, record ids) between `<!-- rl:begin X -->` and `<!-- rl:end X -->`. Each run refreshes
the blocks and keeps everything else, and lists the sections still to write. Write the prose at a stage end
from the records only, at the evidence level they carry; correct a wrong fact in the records, not in a block;
delete a block's markers to drop it. With `manuscript.latex` set in `smooth-research.yaml` (a build command,
e.g. `latexmk -pdf -interaction=nonstopmode`) it writes `docs/manuscript/manuscript.tex` instead (blocks
between `% rl:begin X` and `% rl:end X`, placeholders `\towrite{...}`) and builds the PDF beside it; a failed
build prints the LaTeX errors. The preamble is yours: for Chinese or Japanese text build with xelatex or
lualatex and load a CJK package there.
