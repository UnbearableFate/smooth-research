# Smooth Research: formats and recipes

Read the section you need; the main skill has the loop. `{{rl}}` is the CLI (see SKILL.md). The layout is
fixed: `src/` code, `docs/` what the user reads, `research/` everything else (SKILL.md, Project layout).

## A local run, end to end

Only on a machine permitted to execute workloads, with `compute.coordinator: local`. On a login node,
use the cluster recipe below even for tiny checks.

```bash
# 1. register (nothing starts); keep the printed RUN / ATT ids. Its "code review" line says whether an
#    answered review still covers this code, with a reason for each reviewed path that no longer matches
#    (changed, absent, test file); add --no-review "<reason>" when the run needs none
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
that targets something other than E-0001 (or the experiment its code comes from) to the coverage check; the
check names such a review when it saw a changed file as this run executes it, and leaves the choice to you.

## Compute resources and remote runs

`compute` in `smooth-research.yaml` names where experiments run; `{{rl}} compute` lists the resources and
checks what it can see locally (ssh, the git remote, whether a commit is pushed as of the last push or fetch):

```yaml
compute:
  default: cluster                    # used unless a task or the coordinator says otherwise
  resources:
    local: {kind: local}             # this machine: code from your checkout, outputs in research/artifacts/<ATT>/
    cluster:
      host: cluster-login                 # the ssh alias, or pi-env-mcp's host name
      connect: pi-env-mcp:cluster-login   # ssh, or pi-env-mcp:<MCP server name>
      jobs: mcp:cluster-jobs           # shell (a background process), mcp:<server> or skill:<name>
      workspace: /work/<group>/<user>/<project>   # absolute shared cluster workspace
      code: git                      # git: push, the host fetches (git_remote: origin, git_url: <URL there>)
                                     # archive: git archive, uploaded
```

A remote run executes one commit, staged once as the read-only snapshot `<workspace>/snapshots/<commit>/`:
the branch or worktree it came from does not matter, and a branch that moves on cannot change a queued job.
Workspace layout: `repo.git` (a bare clone the host fetches into, code: git), `snapshots/<commit>/`,
`runs/<ATT>/` (one directory per attempt). The executor owns its own ledger and scheduler logs; do not
edit them. One workspace per research project, never shared: request keys are unique only inside it.

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
- Record it: `{{rl}} run register ... --environment-ref "uv.lock <lock id>, extra <extra>, modules <list>"`. Runs
  with a different environment_ref are not the same comparison.
- Exception: a package that must match the site's own build (mpi4py for the site's MPI, a torch the site compiled
  for its CUDA) stays out of the lock. Use the site's module or prepared environment, read-only, and name it and
  its versions in `--environment-ref`.

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

### Jobs through an external executor

With `jobs: mcp:<server>` or `skill:<name>`, the external integration owns scheduler semantics, site policy,
allocation checks, cancellation, usage accounting and notifications. Read its actual tool schema and
instructions; the names and fields below describe the information to pass, not a required MCP API. Keep
server registration, launcher paths, queue names, account names and site-specific scripts in the user's
project or host configuration, outside this plugin. Do not bypass the configured executor using raw scheduler
commands. Submit only within the user's authorized resources and budget.

3. `{{rl}} compute steps --attempt ATT` stages the registered commit as a read-only snapshot. With
   `connect: local`, run its small source archive script directly on the shared filesystem; use an allocation
   if staging is large. Never submit a mutable checkout or replace the run's commit with current HEAD.
   The job script runs from that snapshot and writes only to `<workspace>/runs/$SR_ATTEMPT_ID/`, ending with
   `manifest.json` (format below). Supply `SR_ATTEMPT_ID` and `SR_SPEC_DIGEST`; use `blake2b:<hex>` for artifact
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
6. The coordinator owns waiting (host.md). Configure the executor's completion notification to reach the
   coordinator, start any returned listener/daemon according to that integration's instructions, then the
   researcher uses `{{rl}} task wait` and reports the attempts, continuation and notification target. When
   notified, inspect the job, acknowledge if required, resume the researcher with the same role, and import
   updated observations. Use a bounded wait for short jobs when supported; do not sleep-poll in the agent.
7. Verify application outputs after completion: scheduler success alone is insufficient. With a shared
   filesystem, import `@<workspace>/runs/<ATT>/manifest.json` directly; use absolute shared paths for artifacts, so no download is needed.
   Relative artifact paths are always resolved from the research project root, not the manifest directory. Otherwise fetch the manifest and its small files into
   `research/artifacts/<ATT>/` before `{{rl}} run import-result`. Large unfetched outputs may be recorded as
   `{"uri": "<resource>:<absolute path>"}` (not read).

## RunSpec fields

`arm` (required), `code_ref` = `{commit: <full 40-hex>}` and/or `{content_digest: "blake2b:..."}` (or
`unknown`), `config_digest` or `config_ref`, `data_revision`, `metric` or `metric_spec_ref`, `output_spec`
(all required; write `unknown` if you do not know); optional `seed`, `replicate`, `model_revision`,
`environment_ref`, `params` (object), `resume_from` (checkpoint and its origin), `notes`. The tool adds
`experiment_ref` and `protocol_ref` (the sealed contract). Branch names, HEAD and short hashes are refused.
Same `--request-id` with the same content returns the earlier record; with different content it is refused.

A contract may list its arms in its front matter (`arms: [wd1e-2, no-wd]`): `run register` then refuses any other
arm name, so a misspelt arm cannot start a run that the tables keep apart. To run another arm, add it to the list
(a new contract revision). `metrics: [test_acc, train_loss]` names the metrics the tables show.

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

## Result manifest

```json
{"status": "complete", "spec_digest": "<from run register, optional>",
 "code_ref": {"content_digest": "blake2b:..."}, "data_revision": "synthetic-v1 (...)",
 "metric": "test accuracy", "metrics": {"test_acc": 0.812, "train_loss": 0.41},
 "artifacts": [{"path": "research/artifacts/ATT-0001-1/metrics.json", "role": "metrics"},
               {"uri": "cluster:/work/.../ckpt.pt", "digest": "blake2b:...", "role": "checkpoint"}],
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
- `{{rl}} decide` keeps a short reason in the record and reports: `--rationale "<reason>"`, or, with only
  `--rationale-file <file>` (sealed), the file's first line of text. `--based-on` may also name earlier decisions
  (a stage decision on the route decisions it sums up); that is a reference, so a re-check flag on the earlier
  decision does not pass to the later one, and the references may not form a cycle.
- Key conclusions (`--set-evidence-status supported|challenged`, `--action stop`, a stage `recommend_formal`):
  `{{rl}} review conclusion --target H-x --proposal .. --author-model ..` first, then `{{rl}} decide ...
  --based-on EV-x,RV-x` (skill research-review). A review covers the hypotheses it targets: a decision on H-x
  and H-y needs a passing review of each.
- A conclusion review counts for the revision of each hypothesis it reviewed. After `{{rl}} hypothesis revise`,
  have the conclusion reviewed again, or state in the decision's rationale why the revision does not change it.
  Evidence linked to the hypothesis after the review is pointed out (decide, `{{rl}} status`); it is not a gap.

## Tasks, messages, sessions

- `{{rl}} task create --title .. --kind framework|implement|experiment|analysis|literature|review|advice|other
  --targets E-x --request research/tasks/T-brief.md --to-role researcher --next-action ..`
- Contract binding:
  - The task seals the contract of each experiment it targets, and is bound to that revision. It refuses a contract
    with fields still empty or TODO, and lists them. A task on a hypothesis seals nothing under it.
  - `{{rl}} review request --target E-x` seals the contract the same way.
  - When the contract is revised later, the task goes on. Its work pack (`{{rl}} agent brief`,
    `{{rl}} context --purpose work`) shows both revisions and what changed. Runs registered earlier keep their revision.
- Owner: `{{rl}} task claim T-x --role researcher` (one claimant wins); then `{{rl}} task deliver T-x
  --role researcher --summary .. --output <files>` (a task that changed code adds the test run that checked it,
  `--verified "<command> @<commit>: pass|fail, log <path>"`; `task accept` shows it and notes a branch head that no
  passing run was of), `task wait .. --attempts ATT-x --next-action ..`,
  `task block .. --reason engineering|model_unavailable|budget|science`, `task resume` (also to continue a
  task your role already owns, for example after a WAIT),
  `task handoff .. --doc research/tasks/T-x-handoff.md` (the task is then ready to claim again). Only the
  owner updates a task, and not after the coordinator accepted or stopped it (`task reopen` gives it back).
- Coordinator: `{{rl}} inbox --role coordinator`; after reading, `--ack MSG-x` with the last message you read
  (plain `--ack` acknowledges whatever it returns, also what arrived since you read), `{{rl}} task accept|stop|reopen T-x
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
`{{rl}} history [id]`, `{{rl}} hypothesis list [--status ..] [--disposition ..]`, `{{rl}} evidence list [--validity ..]
[--level ..]` (with `--json`, the records), `{{rl}} report` (research/reports/), `{{rl}} view` (docs/research.html,
below), `{{rl}} export` (consistent snapshot into research/snapshots/; `--records-only` prints its records as JSON and
writes nothing), `{{rl}} restore <snapshot> --into <new dir>`, `{{rl}} doctor`.

A command that finds the records busy waits up to 60 s, then stops with LOCKED, saying how long it waited and which
process held them. Nothing was written: run it again. It is not retried for you, since running a command twice can
record twice.

`{{rl}} --read-only <command>` changes nothing: no upgrade of older records (it says to upgrade first), no update
of the project's environment, no record, file or cache written; a command that would write is refused. Use it to
look at someone else's project or a copy.

Attention items (`{{rl}} status`) are of two kinds. A condition still to meet (a review gap, a re-check, an open
review round, an attempt to check) leaves when it is met. A fact (a contract revised after results, evidence linked
after a review) shows a key: `{{rl}} attention ack <key> --reason "<how it was taken into account>"` takes it out
of attention, records an event and leaves the fact in the records and reports; it comes back when the fact changes.

## The research page

`{{rl}} view` renders `docs/research.html` for the user: the facts from the records (labels, states, relations,
contract metrics, the arm tables, costs, decisions, every count) and the words you write in
`research/notes/explanation.yaml`. It is self-contained and deterministic. The commands that change its facts
render it again while it exists.

```yaml
lines:                      # research lines; top-level hypotheses in none go under Unclassified
  - key: m
    name: M line - memory read and repair
    desc: One sentence on what the line is about.
    roots: [H3]             # the subtrees it shows, by label or id
summary:
  text: Judgments only. The page does the counting, so write no numbers here.
nodes:                      # a hypothesis or an experiment, by label or id
  H3:
    title: A readable title
    purpose: Why this node exists.
    did: What was done.
    result: What came of it, at the evidence level the records carry.
```

- **Listing the work.** `{{rl}} view --todo` lists the explanations to write or revise, each with its current facts.
- **Stamping.** After writing, `{{rl}} view --stamp H3,EH3.3/3[,summary]` records the facts each explanation was
  checked against, in rl's `research/notes/explanation.basis.yaml`, then renders the page.
- **Staleness.** A node whose facts change later is marked "may be out of date". The facts are:
  - for a hypothesis: statement, revision, status, evidence and its validity, decisions;
  - for an experiment: contract revision, stage, attempt states, results, decisions.

  The summary goes out of date when a newer stage decision appears.
- **Never blocking.** Missing or stale explanations never block a decision. `{{rl}} status` counts them in one
  attention item.

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
lualatex and set a font with those glyphs there, by file (docs/configuration.md has a tested setup). After such a
build the PDF's text is checked for the source's Chinese or Japanese text: a PDF that looks right but whose text
is garbled or missing fails with BUILD. The build runs in your shell, also on a login node: run `{{rl}} manuscript`
where the site allows a LaTeX build.
