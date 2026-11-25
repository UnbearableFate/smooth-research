# Smooth Research: formats and recipes

`rl <command> --help` lists every flag. The role cards in `rl pack` say who runs which command; this file holds
the formats and the recipes for runs and compute resources.

## The experiment manifest

The scientist writes it in the design commit, in the task's worktree, and names it with `{{rl}} experiment set
E-x --manifest <path>`. It is sealed with the design when the task is dispatched; a changed manifest is a new
revision of the experiment.

```yaml
arms:
  - name: base
    seeds: [0, 1, 2]
    split: dev
    config: {lr: 0.0001, steps: 200}
  - name: w02
    seeds: [0, 1, 2]
    split: dev
    config: {lr: 0.0001, steps: 200, write: w02}
metrics: [dev_acc]
allowed: [batch_size]       # parameters a run may change without a new design
stop_when: the positive control of the first seed fails
```

An evidence run names its arm and seed; the arm's config and split are recorded with it. A run with another
configuration or split is refused, or registered with `--deviation "<why>"` and is then not evidence. Each run
keeps the revision and the arm it was registered against: a later revision of the experiment does not change how
its result is checked.

## A local run, end to end (a researcher in its worktree)

```bash
# 1. register (nothing starts); keep the printed RUN / ATT ids
rl run register --task T-0003 --request-id r-base-0 --arm base --seed 0 --minutes 20
# 2. run it with your own tool, writing a result manifest
python src/e1/run.py --arm base --seed 0 --out research/work/T-0003/base-0.json
# 3. record the end you saw (or let the executor's delivery record it: rl run ended)
rl run record --attempt ATT-0001-1 --state ended --outcome success --source local-shell
# 4. import the result manifest; then evidence from usable results
rl run import-result --attempt ATT-0001-1 --request-id i-base-0 --manifest @research/work/T-0003/base-0.json
rl evidence add --task T-0003 --summary "..." --level screening --results RES-0001,RES-0002
```

Trial runs (smoke, timing) are `--trial --minutes N` (at most `limits.trial_minutes`) and are never evidence.
A retry of a run is `--run RUN-x` with a new request id; the same request id with the same content returns the
attempt it made.

## Result manifest

```json
{"status": "complete", "arm": "base", "seed": 0, "commit": "<git rev-parse HEAD>", "split": "dev",
 "config": {"lr": 0.0001, "steps": 200}, "metrics": {"dev_acc": 0.812},
 "artifacts": [{"path": "research/work/T-0003/base-0/metrics.json", "digest": "blake2b:..."},
               {"uri": "cluster:/work/.../ckpt.pt", "role": "checkpoint"}]}
```

`arm`, `seed`, `commit` and `config` (and `split` when the arm has one) say what ran; a result without them is
kept but marked unverified and cannot be evidence, and one that differs from its registration or from the sealed
arm is marked deviating. Local paths are read and digested; remote URIs are only registered. `{{rl}} run
reject-result RES-x --reason ..` when the result itself is wrong: what rests on it is flagged for re-checking.

## Compute resources and remote runs

`compute` in `smooth-research.yaml` names where experiments run; `{{rl}} compute` lists the resources and
checks what it can see locally (ssh, the git remote, whether a commit is pushed as of the last push or fetch):

```yaml
compute:
  default: cluster                    # used unless a task or the coordinator says otherwise
  resources:
    local: {kind: local}             # this machine: code from the task's worktree
    cluster:
      host: cluster-login                 # the ssh alias, or pi-env-mcp's host name
      connect: pi-env-mcp:cluster-login   # ssh, or pi-env-mcp:<MCP server name>
      jobs: mcp:cluster-jobs           # shell (a background process), mcp:<server> or skill:<name>
      workspace: /work/<group>/<user>/<project>   # absolute shared cluster workspace
      code: git                      # git: push, the host fetches (git_remote: origin, git_url: <URL there>)
                                     # archive: git archive, uploaded
```

The jobs MCP server is reached through `connect`, never on a connection of its own. With
`connect: pi-env-mcp:<server>`, pi-env-mcp runs it on the host over the connection it already holds: its tools are
among that pi-env-mcp server's tools, named `<prefix>__<tool>`, where the prefix is the jobs server's name with
characters other than letters, digits and `_` turned into `_` (`mcp:cluster-jobs` → `cluster_jobs__job_submit`).
There is no separate server for it. With `connect: ssh` it is a server of your host started over ssh; with
`connect: local`, one of your host on the login node. `{{rl}} compute steps` names the tools.

A remote run executes one commit, staged once as the read-only snapshot `<workspace>/snapshots/<commit>/`:
the branch or worktree it came from does not matter, and a branch that moves on cannot change a queued job.
Workspace layout: `repo.git` (a bare clone the host fetches into, code: git), `snapshots/<commit>/`,
`runs/<ATT>/` (one directory per attempt). The executor owns its own ledger and scheduler logs; do not
edit them. One workspace per research project, never shared: request keys are unique only inside it.

Code by git, at the start: the coordinator runs `{{rl}} compute sync`. For the resources
with `code: git` it pushes the current branch to their git remote. When that remote is missing and
`compute.repository` is `github-private` (the default), it first creates a private GitHub repository named
after the project directory (`gh repo create --private`) and adds it as the remote. It creates none for a project
that is part of a larger repository, when `git_url` is set, or with `none` (then ask the user where the code
goes). It then prints, per resource, a script that clones the repository into `<workspace>/repo.git` (or
fetches it) and checks that the host has the pushed commit: run it at once through that resource's connection,
so a host that cannot read the repository shows up before the first run (it needs `gh auth login` there, or a
deploy key). Before a run, a researcher runs `{{rl}} compute sync` in its worktree to push its task branch.

1. Commit the code (sources only: no `__pycache__`, outputs or data), then register with `--resource <name>`:
   the run is the task's committed head (the review covers that commit).
2. `{{rl}} compute steps --attempt ATT` prints the steps for that attempt's commit and resource: push your task
   branch (code: git, `{{rl}} compute sync`; the host needs read access to the repository), then one
   script for the host that fetches the commit into `repo.git` and extracts it read-only; or (code: archive)
   `git archive` streamed over ssh, or written locally and uploaded with pi-env-mcp, then extracted. Run them
   with Bash, ssh or the pi-env-mcp tools as printed; each is safe to repeat. rl runs none of it.

### Python environments: one pyproject.toml and one uv.lock for every resource

Keep a single committed `pyproject.toml` and `uv.lock` for this machine and every remote resource. Never keep a
second copy for a host, and never edit or re-lock them there. A run's snapshot carries the lock its commit pinned,
which is what ties the run to its package versions.

- What differs between resources (a CPU build here, a CUDA build on the GPU nodes, another architecture) goes into
  that one file. uv's lock covers every platform, so `uv lock` here also locks what an aarch64 host installs.
  Check each host first: architecture (`uname -m`), CUDA driver and modules (`nvidia-smi`, `module avail`). The
  variants of one package (torch for CPU or for a CUDA version) are mutually exclusive extras, each from its index:

  ```toml
  [project.optional-dependencies]
  cpu = ["torch==2.11.*"]      # this machine
  cuda = ["torch==2.11.*"]   # the GPU nodes: the same version as here
  [tool.uv]
  conflicts = [[{ extra = "cpu" }, { extra = "cuda" }]]
  # uv lock fails here, not on the host, when a package has no build for it
  required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]
  [tool.uv.sources]
  torch = [{ index = "pytorch-cpu", extra = "cpu" }, { index = "pytorch-cu128", extra = "cuda" }]
  [[tool.uv.index]]
  name = "pytorch-cu128"       # the CUDA version the nodes support
  url = "https://download.pytorch.org/whl/cu128"
  explicit = true
  # pytorch-cpu likewise: https://download.pytorch.org/whl/cpu
  ```

  Pin the same version in every variant. Each index has its own newest version (on 2026-10-31 the cu128 index
  stopped at torch 2.11 for aarch64 while the CPU index had 2.15), and different versions here and there make a
  local check say nothing about the remote run. Read the versions the lock chose before you commit it.
- Change dependencies only here: edit `pyproject.toml`, run `uv lock`, commit both; this machine uses
  `uv sync --extra cpu`. rl's own dependencies (added by `{{rl}} init`) stay in the list.
- On the host, build the environment where the site permits package installation, from the snapshot, outside it, keyed by the lock: the same
  lock reuses the environment, a new lock gets a new one, and queued jobs keep theirs. After loading the modules
  the job loads: `UV_PROJECT_ENVIRONMENT=<workspace>/envs/<lock id>-<extra> uv sync --frozen --extra <extra>
  --no-install-project --project <workspace>/snapshots/<sha>`, where `<lock id>` is the first 12 characters of
  `git rev-parse <sha>:uv.lock` (the same for every commit with the same lock). The job script loads the same
  modules, runs that environment's `python`, and writes the torch and CUDA versions it sees into its manifest.
- Record it: `{{rl}} run register ... --param environment="uv.lock <lock id>, extra <extra>, modules <list>"`,
  and have the job write the same into its result manifest.
- Exception: a package that must match the site's own build (mpi4py for the site's MPI, a torch the site compiled
  for its CUDA) stays out of the lock. Use the site's module or prepared environment, read-only, and name it and
  its versions in the environment parameter.

### Coordinator on a cluster login node

Set the dedicated `compute.coordinator: login` option before registering runs. This is an explicit policy,
not hostname detection: check the current shell using the site's tool, and recheck after changing shells.
The CLI refuses resources that execute from the caller's checkout or use `jobs: shell`, and requires an
explicit `--resource` when registering. Invalid coordinator values use login restrictions, never local fallback.

```yaml
compute:
  coordinator: login
  default: cluster
  repository: none
  resources:
    cluster:
      kind: remote
      connect: local
      jobs: mcp:cluster-jobs
      workspace: /shared/user/project
      code: archive
```

`kind: remote` means the workload executes elsewhere, not that SSH is required. `connect: local` means the
agent already has access to the shared filesystem. The workspace must be visible at the same absolute path
from both login and compute nodes. No host, SSH connection, upload, Git remote or push is needed. This access
mode requires a job MCP or skill, and `code: archive` (also its default); it never starts a local shell workload.
If files are not shared, use `connect: ssh` or `pi-env-mcp:<server>` instead.

The login shell is for coordination: edits, records, bounded inspection and small source-only snapshots.
Tests, smoke checks, builds, data analysis and experiment programs go through the configured executor,
including work delegated to subagents or sessions on the same host. A smaller input does not permit running
on the login node. Check site instructions for environment setup: prebuilt packages may be allowed there;
source builds and application imports belong in an allocation. `rl` bookkeeping is control work. It is not a
sandbox and cannot stop arbitrary shell commands; agents must follow this policy. If the MCP is unavailable,
report the missing executor and continue independent control work; do not silently run the workload locally.

With `compute.tests: job` a task's tests are jobs too. Register one with `{{rl}} run register --task T-x
--request-id <new id> --purpose design-test --minutes N` (and `--resource <name>` where the project runs through an
executor); its run keeps the test command, commit and experiment revision of that moment. Run the command at that
commit, then `{{rl}} task test T-x --record pass|fail --attempt <ATT>` (once per job). A command or revision changed
afterwards needs a new job. An acceptance waits on its merge test the same way (`--purpose merge-test`): the
candidate is the branch `sr-accept/<project tag>/T-x`, which `{{rl}} compute sync` pushes for a git resource; the watcher
finishes the acceptance when the job's end is recorded.

### Jobs through an external executor

With `jobs: mcp:<server>` or `skill:<name>`, the external integration owns scheduler semantics, site policy,
allocation checks, cancellation, usage accounting and notifications. Read its actual tool schema and
instructions; the names and fields below describe the information to pass, not a required MCP API. Keep
server registration, launcher paths, queue names, account names and site-specific scripts in the user's
project, host or pi-env-mcp configuration, outside this plugin. Do not bypass the configured executor using raw
scheduler commands. Submit only within the user's authorized resources and budget. Under pi-env-mcp, when the
jobs tools are missing, its `hosts` tool shows the server's state and the end of its stderr, and `hosts` with
`connect: true` starts it again. Report a server missing from pi-env-mcp's configuration as a setup problem; do
not register it over ssh instead. Pass the resource's workspace as the executor's project when its tools take
one.

3. `{{rl}} compute steps --attempt ATT` stages the registered commit as a read-only snapshot. With
   `connect: local`, run its small source archive script directly on the shared filesystem; use an allocation
   if staging is large. Never submit a mutable checkout or replace the run's commit with current HEAD.
   The job script runs from that snapshot and writes only to `<workspace>/runs/$SR_ATTEMPT_ID/`, ending with
   `manifest.json` (format below). Supply `SR_ATTEMPT_ID`; use `blake2b:<hex>` for artifact
   digests (Python: `hashlib.blake2b(data, digest_size=32)`). Verify the actual compute allocation inside the
   job before running the workload. A check on the login-side MCP server cannot verify the job's shell.
4. Submit through the configured executor, using its plan/dry-run and commit flow when supported. Pass the
   snapshot script and workdir, output directory, environment, full commit and attempt correlation. Where
   supported, use `sr:<ATT>` as the idempotency key and the attempt id as the client reference. An uncertain
   submission must be reconciled using the same key before retrying; a new attempt gets a new key.
5. Record observed scheduler facts. Import a compatible observation export with
   `{{rl}} run record --observation @<export.jsonl>`; otherwise translate verified facts into the observation
   format below. Use `hpc_job`, the executor namespace and the full job id, never the login process PID; take the
   id from the executor's output, never from memory (`{{rl}} run record --exec-id` reminds you on such a resource).
   Queued is not running. Import measured usage once; unknown usage is not zero.
6. Nobody waits in a pane. Configure the executor's completion notification with its `command` delivery
   running `rl run ended --namespace <executor namespace>` in the project (the notification JSON on stdin): rl
   finds the attempts by the job ids bound to them in that namespace and ends each job by its own status row; a job whose row says it is still running or unknown
   stays pending, and a state-unknown notice ends nothing. The submitter then sees a `job-ended` item and the
   watcher wakes it. While a task's runs are queued or running, its session has nothing to do and ends its turn.
7. Verify application outputs after completion: scheduler success alone is insufficient. With a shared
   filesystem, import `@<workspace>/runs/<ATT>/manifest.json` directly; use absolute shared paths for artifacts, so no download is needed.
   Relative artifact paths are always resolved from the research project root, not the manifest directory. Otherwise fetch the manifest and its small files into
   `research/work/<T-x>/<ATT>/` before `{{rl}} run import-result`. Large unfetched outputs may be recorded as
   `{"uri": "<resource>:<absolute path>"}` (not read).

## Observation (flags, or `--observation` with one JSON object, a JSON array or JSON Lines)

```json
{"record_id": "cluster-jobs:3731052.opbs:2", "attempt_id": "ATT-0003-1", "source": "cluster-executor",
 "observed_at": "2026-10-30T05:43:10+09:00", "occurred_at": "2026-10-30T05:42:26+09:00", "source_sequence": 2,
 "execution_state": "running", "outcome": "unknown", "raw_state": "R", "source_ref": "qstat -ll @cluster-login1",
 "execution_ref": {"kind": "hpc_job", "executor_namespace": "cluster-jobs", "opaque_id": "3731052.opbs",
                   "locator": "<cluster-executor record>"}}
```

States: prepared, queued (optional), running, ended, unknown. Outcomes: success, failed, cancelled,
unknown (only `ended` carries an outcome). `observed_at` is when the tool looked; `occurred_at` (optional) is
the event time the source itself reports, kept as evidence. The attempt's state is derived from the
observations: within one source the `source_sequence` orders them, otherwise `observed_at`; an older
`running` never overrides a recorded end; a failed lookup after the end only adds an issue; conflicting ends
mark the attempt for checking. Correct a wrong record explicitly with `--correction-of <record_id> --note
"<evidence>"`. An attempt is bound to one execution: the first observation with an execution ref binds it. A job
id recorded by mistake is corrected with `--correction-of <the observation that bound it> --exec-id <the right
id> --note ..`, which moves the binding (an event records the move); any other observation naming another job is
refused, since a re-execution is a new attempt. Several attempts may share one execution ref (one job running
several seeds). A batch is
recorded in order; records already received are skipped, so re-sending a whole export is safe, and flags such
as `--attempt` fill in or override every record.

## Usage

`{{rl}} run usage --accounting-ref "local:$(hostname):ATT-0001-1" --quantity 42 --unit cpu_s --resource-type
compute --hardware "<cpu model>" --basis measured --attempts ATT-0001-1`. A shared job: one accounting ref,
several `--attempts`, counted once; never split it into invented per-run numbers. Different units are never
added, and unreported usage is not zero.

## After a plugin update

The records are upgraded in place the first time a newer CLI opens them, with a copy of the old database in
`research/state/backups/`. `{{rl}} upgrade` shows the format, the plugin build, what this version reads
differently in `smooth-research.yaml`, and what may be adjusted; `{{rl}} upgrade --note "<what and why>"` records
the adjustment. Recorded facts and sealed documents are never rewritten.
