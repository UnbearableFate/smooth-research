# Smooth Research: formats and recipes

Read the section you need; the main skill has the loop. `{{rl}}` is the CLI (see SKILL.md).

## A local run, end to end

```bash
# 1. register (nothing starts); keep the printed RUN / ATT ids
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
--run RUN-0001 --request-id <new>`. A changed config or code is a new run.

## A remote or HPC run (Miyabi: pi-env-mcp and miyabi-mcp)

pi-env-mcp moves files and runs remote commands; miyabi-mcp submits, queries, cancels and waits for PBS
jobs and exports observations; `{{rl}}` only records. Submit and cancel only through miyabi-mcp: a `qsub` run
through pi-env-mcp bypasses its ledger and its limits. The remote root is the miyabi-mcp project directory
(its `--project-dir`, by default `/work/<group>/<user>/<project>`), one per research project and never
shared: request keys are unique only inside it. Write it in `docs/plan/constraints.md`, and pass absolute
remote paths: relative ones follow the remote shell's `cd`. Remote layout: `.miyabi-mcp/` (miyabi-mcp's
ledger and PBS logs; do not edit), `snapshots/<commit>/` read-only code, the Python environment built on the
login node of the target system, `runs/<ATT>/` one directory per attempt.

1. Commit the code (sources only: no `__pycache__`, outputs or data) and register with
   `--code-commit <full sha>`; add `--json` to get the full `spec_digest`.
2. Stage the commit once, never edit it afterwards (PBS reads the code when the job starts, not when it is
   submitted): `git archive --format=tar -o research/artifacts/snapshots/<sha>.tar <sha> src configs`, upload
   that one file with pi-env-mcp to `<root>/snapshots/<sha>.tar`, then in pi-env-mcp bash:
   `mkdir <root>/snapshots/<sha> && tar -xf <root>/snapshots/<sha>.tar -C <root>/snapshots/<sha> && chmod -R a-w <root>/snapshots/<sha>`.
3. The job script is part of the snapshot, runs code from it and writes only to `<root>/runs/$SR_ATTEMPT_ID/`,
   ending with a `manifest.json` (format below) that carries `spec_digest` and artifact digests written as
   `blake2b:<hex>` (Python: `hashlib.blake2b(data, digest_size=32)`).
4. Submit with miyabi-mcp `job_submit`: dry run first (show the plan when the user wants to confirm
   submissions), then submit with `script: <root>/snapshots/<sha>/<job script>`, `workdir:
   <root>/snapshots/<sha>` (both read-only, so miyabi-mcp's check that the code cannot change passes), `name:
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
6. Wait without polling: after the first submit of a batch, start the listener as host.md says, then keep
   working or end the turn. Its first message is either an early failure of one job (start the listener again
   to wait for the rest) or the batch summary. Acknowledge with `notify_ack(id, ack_token)` from the message,
   then export and record (step 5). Jobs of a few minutes can use `job_wait` instead.
7. After the end (PBS exit 0 is not application success): download `<root>/runs/<ATT>/manifest.json` and the
   small files it lists into `research/artifacts/<ATT>/`; list checkpoints and other large files as
   `{"uri": "miyabi:<absolute path>"}` (recorded as not read). Then `{{rl}} run import-result`.

Without miyabi-mcp, submit with the miyabi skills through pi-env-mcp `bash` and record by hand:
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
the planned comparison. `partial` results never replace a `complete` one; a later import is a new revision. Local paths are read and
digested; remote URIs are only registered ("not read"). Fields that differ from the RunSpec are kept, and the
result is marked unusable for the pre-specified comparison. `{{rl}} run reject-result RES-x --reason ..` if
the result itself is wrong.

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

## Tasks, messages, sessions

- `{{rl}} task create --title .. --kind framework|implement|experiment|analysis|literature|review|advice|other
  --targets E-x --request docs/tasks/T-brief.md --to-role researcher --next-action ..`
- Owner: `{{rl}} task claim T-x --role researcher` (one claimant wins); then `{{rl}} task deliver T-x
  --role researcher --summary .. --output <files>`, `task wait .. --attempts ATT-x --next-action ..`,
  `task block .. --reason engineering|model_unavailable|budget|science`, `task resume`,
  `task handoff .. --doc docs/tasks/T-x-handoff.md` (the task is then ready to claim again).
- Coordinator: `{{rl}} inbox --role coordinator` (add `--ack` after reading), `{{rl}} task accept|stop|reopen T-x
  --reason ..`. Stopping a task does not stop external runs: stop them with your tool and record that.
- Rotating the coordinator: write a handoff note, end your session (`{{rl}} session end S-x`); the new session
  registers as coordinator and starts from `{{rl}} resume`.

## Literature

`{{rl}} paper search "<query>"` (arXiv; failures are logged as failures) -> `{{rl}} paper add <arXiv id | DOI>
--fetch` -> `{{rl}} paper link H-x P-y --relation nearest_work|motivated_by|cites --assessment
search_hit|metadata_checked|fulltext_read|checked [--locator "Sec 3.2, Eq. 4"] [--note-file docs/notes/..]`.
`checked` needs a locator. "No identical method found" is not a novelty proof.

## Views, backups, integrity

`{{rl}} status`, `{{rl}} resume`, `{{rl}} graph`, `{{rl}} show <id>` (a DOC id prints the sealed text),
`{{rl}} history [id]`, `{{rl}} report`, `{{rl}} view`, `{{rl}} export` (consistent snapshot into
research/snapshots/), `{{rl}} restore <snapshot> --into <new dir>`, `{{rl}} doctor`.
