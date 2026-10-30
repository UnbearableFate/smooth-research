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

## RunSpec fields

`arm` (required), `code_ref` = `{commit: <full 40-hex>}` and/or `{content_digest: "blake2b:..."}` (or
`unknown`), `config_digest` or `config_ref`, `data_revision`, `metric` or `metric_spec_ref`, `output_spec`
(all required; write `unknown` if you do not know); optional `seed`, `replicate`, `model_revision`,
`environment_ref`, `params` (object), `resume_from` (checkpoint and its origin), `notes`. The tool adds
`experiment_ref` and `protocol_ref` (the sealed contract). Branch names, HEAD and short hashes are refused.
Same `--request-id` with the same content returns the earlier record; with different content it is refused.

## Observation (flags or `--observation @file.json`)

```json
{"record_id": "qstat-123456-0007", "attempt_id": "ATT-0003-1", "source": "miyabi-pbs-cmd qstat",
 "observed_at": "2026-10-30T09:12:00+09:00", "source_sequence": 7,
 "execution_state": "running", "outcome": "unknown", "raw_state": "R",
 "source_ref": "qstat -f 123456 output", "execution_ref": {"kind": "hpc_job",
 "executor_namespace": "miyabi-g-pbs", "opaque_id": "123456.opbs", "locator": "/work/.../job.sh"}}
```

States: prepared, queued (optional), running, ended, unknown. Outcomes: success, failed, cancelled,
unknown (only `ended` carries an outcome). The attempt's state is derived from the observations: an older
`running` never overrides a recorded end; a failed lookup after the end only adds an issue; conflicting ends
mark the attempt for checking. Correct a wrong record explicitly with `--correction-of <record_id> --note
"<evidence>"`. Several attempts may share one execution ref (one job running several seeds).

## Result manifest

```json
{"status": "complete", "spec_digest": "<from run register, optional>",
 "code_ref": {"content_digest": "blake2b:..."}, "data_revision": "synthetic-v1 (...)",
 "metric": "test accuracy", "metrics": {"test_acc": 0.812, "train_loss": 0.41},
 "artifacts": [{"path": "research/artifacts/ATT-0001-1/metrics.json", "role": "metrics"},
               {"uri": "miyabi:/work/.../ckpt.pt", "digest": "sha256:...", "role": "checkpoint"}],
 "validator": {"name": "check_metrics.py", "passed": true, "source_ref": "stdout"}}
```

`partial` results never replace a `complete` one; a later import is a new revision. Local paths are read and
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
- Owner: `{{rl}} task claim T-x --role researcher` prints the owner epoch; then `{{rl}} task deliver T-x
  --owner-epoch N --role researcher --summary .. --output <files>`, `task wait .. --attempts ATT-x
  --next-action ..`, `task block .. --reason engineering|model_unavailable|budget|science`, `task resume`,
  `task handoff .. --doc docs/tasks/T-x-handoff.md`. A stale epoch is refused; its files are kept for review.
- Coordinator: `{{rl}} inbox --role coordinator` (add `--ack` after reading), `{{rl}} task accept|stop|reopen T-x
  --reason ..`. Stopping a task does not stop external runs: stop them with your tool and record that.
- Rotating the coordinator: write a handoff note, end your session (`{{rl}} session end S-x`); the new session
  registers as coordinator (the epoch increments) and starts from `{{rl}} resume`.

## Literature

`{{rl}} paper search "<query>"` (arXiv; failures are logged as failures) -> `{{rl}} paper add <arXiv id | DOI>
--fetch` -> `{{rl}} paper link H-x P-y --relation nearest_work|motivated_by|cites --assessment
search_hit|metadata_checked|fulltext_read|checked [--locator "Sec 3.2, Eq. 4"] [--note-file docs/notes/..]`.
`checked` needs a locator. "No identical method found" is not a novelty proof.

## Views, backups, integrity

`{{rl}} status`, `{{rl}} resume`, `{{rl}} graph`, `{{rl}} show <id>` (a DOC id prints the sealed text),
`{{rl}} history [id]`, `{{rl}} report`, `{{rl}} view`, `{{rl}} export` (consistent snapshot into
research/snapshots/), `{{rl}} restore <snapshot> --into <new dir>`, `{{rl}} doctor`.
