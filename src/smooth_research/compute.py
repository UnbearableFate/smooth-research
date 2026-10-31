"""Compute resources (compute in smooth-research.yaml): where experiments run, how the agent reaches each one and
how the code of a commit gets there.

rl connects to, copies to and starts nothing on any of them. It reads the configuration, checks what it can see
locally (the ssh command, the git remote, whether a commit is on a branch and pushed as of the last push or
fetch) and prints the steps for the agent's own tools: Bash, ssh, pi-env-mcp, and the MCP server or skill that
submits jobs. A remote run always executes a read-only snapshot of one full commit,
<workspace>/snapshots/<commit>/, so the branch or worktree it came from does not matter and a branch that moves
on cannot change a queued job. An attempt registered with --resource keeps the resource and its planned
locations.
"""

from __future__ import annotations

import re
import shlex
import shutil

from .project import Project, Resource, compute_default, compute_resources, repo_top
from .store import Store
from .util import RLError
from .worktrees import _git

FULL_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def resource(project: Project, name: str | None) -> Resource:
    """A configured resource by name (the default when name is None)."""
    found, _ = compute_resources(project.config)
    name = name or compute_default(project.config)
    if name not in found:
        raise RLError("NOT_FOUND", f"no usable compute resource {name!r} in smooth-research.yaml (compute.resources"
                                   f"); configured: {', '.join(found) or 'none'}. rl compute lists them")
    return found[name]


def _sh(text: str) -> str:
    return shlex.quote(text)


def _git_facts(project: Project, res: Resource, commit: str | None) -> dict:
    """What the caller's checkout says about the commit and the git remote. Reads only local refs: 'pushed' is as
    of the last push or fetch."""
    checkout = project.checkout
    facts: dict = {"checkout": str(checkout)}
    if repo_top(checkout) is None:
        facts["problem"] = f"{checkout} is not in a git repository: commit the project to run it elsewhere"
        return facts
    if res.remote and res.code == "git":
        facts["url"] = res.git_url or _git(checkout, "remote", "get-url", res.git_remote, check=False) or None
        if not _git(checkout, "remote", "get-url", res.git_remote, check=False):
            facts["problem"] = (f"no git remote {res.git_remote!r} in {checkout}: add one (git remote add "
                                f"{res.git_remote} <GitHub URL>) or set compute.resources.{res.name}.git_remote")
    if not commit:
        return facts
    full = _git(checkout, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
    if not full:
        facts["problem"] = f"commit {commit} is not in {checkout}: commit the code first"
        return facts
    facts["commit"] = full
    current = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    holding = _git(checkout, "branch", "--contains", full, "--format=%(refname:short)", check=False).split()
    facts["branch"] = current if current in holding else (holding[0] if holding else None)
    if res.remote and res.code == "git":
        pushed = _git(checkout, "branch", "-r", "--contains", full, "--format=%(refname:short)", check=False).split()
        facts["pushed"] = [b for b in pushed if b.startswith(f"{res.git_remote}/") and not b.endswith("/HEAD")]
    if full == _git(checkout, "rev-parse", "HEAD", check=False):
        facts["dirty"] = bool(_git(checkout, "status", "--porcelain", "--untracked-files=no", check=False))
    return facts


def show(store: Store, commit: str | None = None) -> dict:
    """rl compute: the configured resources and the local checks."""
    project = store.project
    found, warnings = compute_resources(project.config)
    default = compute_default(project.config)
    lines = [f"compute resources (default: {default or 'none'}; rl connects to none of them, it prints the steps: "
             "rl compute steps <name> --attempt ATT):"]
    items = []
    for res in found.values():
        checks = []
        if res.remote and res.connect == "ssh":
            checks.append("ssh on PATH" if shutil.which("ssh") else "ssh not found on PATH")
        if res.remote and res.connect == "pi-env-mcp":
            checks.append(f"pi-env-mcp server {res.server or '(any)'}: an MCP tool, checked by the agent (hosts)")
        if res.jobs.startswith(("mcp:", "skill:")):
            checks.append(f"{res.jobs}: checked by the agent")
        facts = _git_facts(project, res, commit) if res.remote else {}
        if facts.get("url"):
            checks.append(f"git remote {res.git_remote}: {facts['url']}")
        if facts.get("problem"):
            checks.append(facts["problem"])
        if commit and "pushed" in facts:
            checks.append(f"{facts['commit'][:12]} on {', '.join(facts['pushed'])}" if facts["pushed"] else
                          f"{facts['commit'][:12]} not pushed to {res.git_remote} (as of the last push or fetch)")
        lines.append(f"  {res.describe()}" + ("".join(f"\n    {c}" for c in checks)))
        items.append({**res.record(), "describe": res.describe(), "checks": checks})
    return {"default": default, "resources": items, "warnings": warnings, "summary": "\n".join(lines)}


def pack_text(project: Project) -> str:
    """The compute section of work and resume packs."""
    found, _ = compute_resources(project.config)
    default = compute_default(project.config)
    lines = [f"default: {default or 'none'} · register with --resource <name>; rl compute steps --attempt ATT "
             "prints how to stage the commit there, run it and fetch the results (rl runs nothing)"]
    lines += [f"- {res.describe()}" for res in found.values()]
    return "\n".join(lines)


def _attempt_facts(store: Store, attempt: str) -> tuple[str | None, str | None, str]:
    """(resource name recorded on the attempt, the full commit of its RunSpec, the RunSpec digest)."""
    with store.read() as tx:
        att = tx.require(attempt, "Attempt")
        run = tx.require(att["run_id"])
    code = run["spec"].get("code_ref")
    commit = code.get("commit") if isinstance(code, dict) else None
    return (att.get("resource") or {}).get("name"), commit, run["spec_digest"]


def locations(project: Project, res: Resource, commit: str | None, attempt: str | None) -> dict:
    """Where the code and the outputs of an attempt are on the resource (planned, not checked)."""
    att = attempt or "<ATT>"
    if not res.remote:
        return {"checkout": str(project.checkout), "run_dir": f"research/artifacts/{att}"}
    top = repo_top(project.root) or project.root
    sub = project.root.relative_to(top).as_posix()
    snapshot = f"{res.workspace}/snapshots/{commit or '<commit>'}"
    return {"snapshot": snapshot, "project": snapshot if sub == "." else f"{snapshot}/{sub}",
            "run_dir": f"{res.workspace}/runs/{att}"}


def steps(store: Store, name: str | None = None, commit: str | None = None, attempt: str | None = None) -> dict:
    """rl compute steps: how to get one commit's code to a resource, start it there and bring the results back."""
    project = store.project
    recorded, problems, digest = None, [], "<spec_digest>"
    if attempt:
        recorded, spec_commit, digest = _attempt_facts(store, attempt)
        if name and recorded and name != recorded:
            raise RLError("CONFLICT", f"{attempt} was registered for resource {recorded}, not {name}")
        if commit and spec_commit and commit != spec_commit:
            raise RLError("CONFLICT", f"{attempt} runs commit {spec_commit}, not {commit}")
        commit = commit or spec_commit
        if not commit:
            problems.append(f"the RunSpec of {attempt} names no commit (only a content digest or unknown): a "
                            "remote run stages one commit, so register the run with --code-commit <full id>")
    res = resource(project, name or recorded)
    if commit is None and not attempt:
        commit = _git(project.checkout, "rev-parse", "--verify", "--quiet", "HEAD", check=False) or None
    facts = _git_facts(project, res, commit)
    if commit and facts.get("commit"):
        commit = facts["commit"]
    if facts.get("problem"):
        problems.append(facts["problem"])
    if commit and not FULL_COMMIT.match(commit):
        problems.append(f"{commit} is not a full commit id")
    out: list[str] = [f"# {res.describe()}"]
    if attempt and not recorded:
        out.append(f"note: {attempt} was registered without --resource; this assumes {res.name}")
    out += [f"problem: {p}" for p in problems if res.remote or "no commit" not in p]
    where = locations(project, res, commit, attempt)
    att = attempt or "<ATT>"
    env = f"SR_ATTEMPT_ID={att} SR_SPEC_DIGEST={digest}"
    if not res.remote:
        out += _local_steps(project, res, commit, facts, env, att, where)
    else:
        out += _sync_steps(project, res, commit, facts, where)
        out += _run_steps(res, env, att, where)
        out += _fetch_steps(project, res, att, where)
    return {"resource": res.record(), "commit": commit, "locations": where, "problems": problems,
            "summary": "\n".join(out)}


def _local_steps(project: Project, res: Resource, commit: str | None, facts: dict, env: str, att: str,
                 where: dict) -> list[str]:
    head = "" if commit is None else commit
    lines = [f"1. Code: your checkout {where['checkout']}"
             + (f" at {head[:12]}" if head else "") + "; nothing to copy."]
    if facts.get("dirty"):
        lines.append("   The checkout has uncommitted changes to tracked files: commit them, or register with "
                     "--code-digest instead of --code-commit.")
    if head and facts.get("commit") and facts["commit"] != _git(project.checkout, "rev-parse", "HEAD", check=False):
        lines.append(f"   HEAD is not {head[:12]}: check it out (or use a worktree of it) before running.")
    start = ("with Bash run_in_background (or nohup ... &)" if res.jobs == "shell"
             else f"with {res.jobs}")
    lines += [f"2. Run {start}, with {env}; write outputs and manifest.json (carrying the spec digest) to "
              f"{where['run_dir']}/.",
              f"3. Record: rl run record --attempt {att} --source local-shell --state running --exec-kind "
              "local_process --exec-namespace host:$(hostname) --exec-id <pid>; at the end --state ended "
              "--outcome success|failed.",
              f"4. rl run import-result --attempt {att} --manifest @{where['run_dir']}/manifest.json "
              "--request-id <unique>"]
    return lines


def _remote(res: Resource, script: str) -> str:
    """How to run a shell script on the resource's host."""
    if res.connect == "pi-env-mcp":
        return (f"   pi-env-mcp bash (server {res.server or 'pi-env-mcp'}, host {res.host}), command:\n"
                + _indent(script))
    return f"   ssh -T -o BatchMode=yes {_sh(res.host)} sh -s <<'SR_EOF'\n{_indent(script)}\n   SR_EOF"


def _indent(text: str) -> str:
    return "\n".join(f"   {line}" if line else "" for line in text.splitlines())


def _sync_steps(project: Project, res: Resource, commit: str | None, facts: dict, where: dict) -> list[str]:
    sha = commit or "<commit>"
    ws, snap = res.workspace, where["snapshot"]
    head = (f"ws={_sh(ws)}; sha={sha}; snap=\"$ws/snapshots/$sha\"\n"
            "if [ -d \"$snap\" ]; then echo \"already staged: $snap\"; exit 0; fi\n"
            "mkdir -p \"$ws/snapshots\" \"$ws/runs\"; tmp=\"$ws/snapshots/.$sha.partial\"; rm -rf \"$tmp\"; "
            "mkdir \"$tmp\"\n")
    tail = "mv \"$tmp\" \"$snap\" && chmod -R a-w \"$snap\" && echo \"staged: $snap\""
    lines = [f"1. Stage {sha[:12]} once as {snap}/ (read-only; PBS and background jobs read the code when they "
             "start, so a snapshot is never edited; the same commit is staged once):"]
    if res.code == "git":
        branch = facts.get("branch")
        pushed = facts.get("pushed") or []
        if pushed:
            lines.append(f"   a. Already on {', '.join(pushed)} (as of the last push or fetch).")
        elif branch:
            lines.append(f"   a. Push it (in {facts['checkout']}): git push {res.git_remote} {branch}")
        else:
            lines.append(f"   a. Push a branch that contains it: git push {res.git_remote} <branch> (no local branch "
                         "contains it)")
        url = facts.get("url") or "<repository URL>"
        script = ("set -eu\n" + head
                  + f"[ -d \"$ws/repo.git\" ] || git clone --bare --quiet {_sh(url)} \"$ws/repo.git\"\n"
                  "git -C \"$ws/repo.git\" fetch --quiet --prune origin '+refs/heads/*:refs/heads/*'\n"
                  "git -C \"$ws/repo.git\" cat-file -e \"$sha^{commit}\"\n"
                  "git -C \"$ws/repo.git\" archive \"$sha\" | tar -x -C \"$tmp\"\n" + tail)
        lines.append("   b. Fetch it on the host and extract that commit (the host needs read access to the "
                     "repository: a deploy key or token there):")
        lines.append(_remote(res, script))
        return lines
    tar = project.research_root / "artifacts" / "snapshots" / f"{sha}.tar"
    archive = f"git -C {_sh(facts['checkout'])} archive --format=tar {sha}"
    if res.connect == "ssh":
        script = ("set -eu\n" + head.replace("exit 0", "cat >/dev/null; exit 0") + "tar -x -C \"$tmp\"\n" + tail)
        script = "; ".join(script.splitlines())  # one line to copy
        # ssh joins its arguments into one command line for the remote shell: quote the script for that shell too
        lines.append(f"   {archive} | ssh -T -o BatchMode=yes {_sh(res.host)} {_sh('sh -c ' + _sh(script))}")
        return lines
    script = ("set -eu\n" + head.replace("exit 0", "rm -f \"$ws/snapshots/$sha.tar\"; exit 0")
              + "tar -xf \"$ws/snapshots/$sha.tar\" -C \"$tmp\"\n" + tail + " && rm -f \"$ws/snapshots/$sha.tar\"")
    lines += [f"   a. Locally: mkdir -p {_sh(str(tar.parent))} && {archive} -o {_sh(str(tar))}",
              f"   b. pi-env-mcp upload (server {res.server or 'pi-env-mcp'}, host {res.host}): {tar} -> "
              f"{ws}/snapshots/{sha}.tar",
              "   c. Extract it:", _remote(res, script)]
    return lines


def _run_steps(res: Resource, env: str, att: str, where: dict) -> list[str]:
    run_dir, workdir = where["run_dir"], where["project"]
    if res.jobs == "shell":
        how = ("pi-env-mcp bash with run_in_background" if res.connect == "pi-env-mcp"
               else f"ssh {_sh(res.host)} 'nohup ... &'")
        record = (f"rl run record --attempt {att} --source {res.connect} --state running --exec-kind remote_process "
                  f"--exec-namespace host:{res.host} --exec-id <pid>")
        return [f"2. Run from {workdir} ({how}), with {env}, writing only to {run_dir}/ and ending with "
                f"{run_dir}/manifest.json (carrying the spec digest); then {record}."]
    kind, _, tool = res.jobs.partition(":")
    via = f"MCP server {tool}" if kind == "mcp" else f"skill {tool} (through {res.connect})"
    lines = [f"2. Submit with {via}: job script and workdir in {workdir} (read-only), {env}, request key sr:{att}, "
             f"outputs only in {run_dir}/ ending with manifest.json (carrying the spec digest)."]
    if kind == "mcp":
        lines.append(f"   Its project directory must be {res.workspace}. Record with its export (`rl run record "
                     "--observation @file`); for miyabi-mcp see workflow.md (A remote or HPC run).")
    else:
        lines.append(f"   Record the job: rl run record --attempt {att} --source {tool} --state running --exec-kind "
                     "hpc_job --exec-namespace <scheduler> --exec-id <full job id>")
    return lines


def _fetch_steps(project: Project, res: Resource, att: str, where: dict) -> list[str]:
    run_dir = where["run_dir"]
    local = project.research_root / "artifacts" / att
    if res.connect == "pi-env-mcp":
        fetch = (f"pi-env-mcp download (server {res.server or 'pi-env-mcp'}, host {res.host}): {run_dir}/manifest.json "
                 f"and the small files it lists -> {local}/")
    else:
        fetch = (f"mkdir -p {_sh(str(local))} && scp -q -o BatchMode=yes {_sh(res.host)}:{run_dir}/manifest.json "
                 f"{_sh(str(local))}/ (and the small files it lists)")
    return [f"3. After the end (exit 0 is not application success): {fetch}; list checkpoints and other large "
            f"files as {{\"uri\": \"{res.name}:<absolute path>\"}} (recorded as not read).",
            f"4. rl run import-result --attempt {att} --manifest @{local}/manifest.json --request-id <unique>"]
