"""Research project discovery, paths and the short configuration file."""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

from .adapters import Entry
from .util import RLError, atomic_write

CONFIG_NAME = "smooth-research.yaml"
ENV_PROJECT = "SMOOTH_RESEARCH_PROJECT"
ENV_CHECKOUT = "SMOOTH_RESEARCH_CHECKOUT"

# Project layout, fixed: experiment code in src/; what people read in docs/ (the original plan, the
# constraints, the research page research.html, the manuscript and the explanations the user asked for); everything
# else in research/: the records (database, sealed objects, packages, call logs, run artifacts) and the
# agents' working documents (experiment contracts, task notes, researcher notes, review and advice answers,
# stage reports).
CODE_DIR, DOCS_DIR, RESEARCH_DIR = "src", "docs", "research"
PLAN_PATH, CONSTRAINTS_PATH = "docs/plan.md", "docs/constraints.md"
# The settings read the last time the configuration file parsed: used while it does not.
LAST_READ = "research/state/config.last-read.json"
UNREAD = "_unread"  # in a loaded configuration: why the file could not be read

# Model entries are "<entry>:<model>[@effort]"; entry is claude, codex, pi or manual (a person does the work).
# Each role's sessions use the first available entry of its list; the watcher moves a session to the next one
# when its host's subscription runs low (watch.py). agents.args are extra launch arguments per host for the
# sessions the watcher opens in herdr panes. agents.* times are the watcher's (design 11.2, 11.3, 7.7, 8.8):
# beat_seconds between beats; clear_idle_minutes and clear_context_tokens: a session idle longer than the first
# (the prompt cache is gone) whose context is larger than the second is cleared before it is woken, and reads its
# pack again; user_quiet_minutes: the coordinator's pane gets a reminder only when the user has not typed there for
# this long; heartbeat_minutes: a watcher heartbeat older than this counts as stopped; page_minutes: the research
# page is drawn again at least this often; deadlines: minutes a to-do item may wait before it is escalated, by
# item type (default for the rest). limits: trial_minutes is the longest a trial run may be registered for;
# fast_lane is the largest estimate (by unit) a task may have to skip a new review round (6.6); scientists is the
# phase's default limit of scientists at work; envelope_lines are the shares of a hypothesis task's envelope at
# which its usage is flagged. compute: where experiments run (compute.py). Budgets stay in docs/constraints.md.
# compute.coordinator: login disables checkout/shell resources and requires explicit run placement.
# compute.tests: local (rl runs semantic tests and done checks itself) or job (they run as short jobs).
# compute.repository: when a remote resource fetches code by git and the git remote is missing, rl resume creates
# it (github-private: gh repo create --private) or leaves it (none).
# Every key here is read by the CLI; docs/configuration.md explains each one.
DEFAULT_CONFIG: dict = {
    "version": 1,
    "models": {
        "coordinator": ["claude:claude-opus-5-5@high", "codex:gpt-6-astra@high"],
        "scientist": ["claude:claude-opus-5-5@high", "codex:gpt-6-astra@high"],
        "researcher": ["claude:claude-sonnet-5-5@medium", "codex:gpt-6-astra@medium"],
        "reviewer": ["codex:gpt-6-astra@medium", "claude:claude-opus-5-5@medium"],
        "advisor": ["codex:gpt-6-astra@high", "claude:claude-fable-5-2@high"],
    },
    "agents": {"args": {"claude": ["--permission-mode", "auto"], "codex": [], "pi": []}, "beat_seconds": 30,
               "clear_idle_minutes": 60, "clear_context_tokens": 120000, "user_quiet_minutes": 3,
               "heartbeat_minutes": 5, "page_minutes": 15, "deadlines": {"default": 240}},
    "limits": {"trial_minutes": 30, "fast_lane": {}, "scientists": 2, "envelope_lines": [0.8, 1.0, 1.5]},
    "usage": {"switch_left_percent": 10},
    "compute": {"coordinator": "local", "default": "local", "repository": "none", "tests": "local",
                "resources": {"local": {"kind": "local"}}},
}
MODEL_ROLES = ("coordinator", "scientist", "researcher", "reviewer", "advisor")
# Mappings the file replaces as a whole instead of merging into the defaults: compute.resources names the user's
# own resources (a project without the default local one leaves it out), and their keys are not the defaults';
# deadlines and fast_lane are keyed by item type and by unit.
WHOLE = ("compute.resources", "agents.deadlines", "limits.fast_lane")

RESEARCH_DIRS = ("state", "documents/objects", "snapshots", "literature", "artifacts", "work")
DOCS_DIRS = ("explanations",)


def _merge(base: dict, override: dict, prefix: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        path = f"{prefix}{key}"
        merge = isinstance(value, dict) and isinstance(out.get(key), dict) and path not in WHOLE
        out[key] = _merge(out[key], value, path + ".") if merge else copy.deepcopy(value)
    return out


@dataclass
class Project:
    """root holds the records and the configuration. checkout is the git checkout whose code the caller works
    on: the root itself, or a researcher's worktree of the same repository."""
    root: Path
    config: dict
    checkout: Path | None = None

    def __post_init__(self) -> None:
        self.checkout = self.checkout or self.root

    @property
    def warnings(self) -> list[str]:
        return config_warnings(self.config)

    @property
    def in_worktree(self) -> bool:
        return self.checkout != self.root

    @property
    def research_root(self) -> Path:
        return self.root / RESEARCH_DIR

    @property
    def state_dir(self) -> Path:
        return self.research_root / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "research.lattice"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "write.lock"

    @property
    def objects_dir(self) -> Path:
        return self.research_root / "documents" / "objects"

    @property
    def code_root(self) -> Path:
        return self.root / CODE_DIR

    @property
    def worktrees_dir(self) -> Path:
        """Researcher worktrees: <project>/../<project>-worktrees/ (beside the repository when the project is a
        directory inside it)."""
        top = repo_top(self.root) or self.root
        return top.parent / f"{top.name}-worktrees"

    @property
    def docs_root(self) -> Path:
        return self.root / DOCS_DIR

    @property
    def plan_path(self) -> Path:
        """The imported plan, sealed at import."""
        return self.root / PLAN_PATH

    @property
    def constraints_path(self) -> Path:
        return self.root / CONSTRAINTS_PATH

    def dir(self, name: str, area: str = "research") -> Path:
        """A subdirectory of research/ (records and the agents' working documents) or docs/ (for people)."""
        path = (self.docs_root if area == "docs" else self.research_root) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def rel(self, path: Path | str) -> str:
        """Relative to the project, or to the caller's checkout when the path is there, else absolute."""
        p = Path(path)
        p = p if p.is_absolute() else (Path.cwd() / p)
        p = Path(os.path.normpath(p))
        for base in (self.root, self.checkout):
            try:
                return str(p.relative_to(base))
            except ValueError:
                continue
        return str(p)

    def abs(self, path: str) -> Path:
        """A record path (research/, docs/): relative to the project."""
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    def src(self, path: str) -> Path:
        """A path the caller names (code under review, a file to seal): relative to the caller's checkout."""
        p = Path(path)
        return p if p.is_absolute() else self.checkout / p


def config_warnings(config: dict) -> list[str]:
    """What in a loaded configuration the CLI does not read or reads differently from what it says: a file that
    does not parse, an unknown version, an unknown key, a section that is not a mapping, model lists that are not
    lists or hold entries that do not parse, and values that are not usable. None stops a command; each says what
    is used instead."""
    out = [f"{CONFIG_NAME}: {config[UNREAD]}"] if config.get(UNREAD) else []
    if config.get("version") != 1:
        out.append(f"{CONFIG_NAME}: version {config.get('version')!r} is not known; read as version 1")

    def walk(given: dict, default: dict, prefix: str) -> None:
        for key, value in given.items():
            path = prefix + str(key)
            if str(key).startswith("_"):
                continue
            if key not in default:
                out.append(f"{CONFIG_NAME}: unknown key {path} has no effect (a typo?)")
            elif isinstance(default[key], dict) and not isinstance(value, dict):
                out.append(f"{CONFIG_NAME}: {path} must hold keys like the default, not {value!r}; its settings are "
                           "at their defaults")
            elif isinstance(value, dict) and isinstance(default[key], dict) and path not in WHOLE:
                walk(value, default[key], path + ".")

    walk(config, DEFAULT_CONFIG, "")
    models = config.get("models")
    for role in MODEL_ROLES if isinstance(models, dict) else ():
        value = models.get(role)
        if not isinstance(value, (str, list)):
            out.append(f"{CONFIG_NAME}: models.{role} must be a list of model entries, not {value!r}; using the "
                       f"defaults {model_entries(config, role)}")
        for text in model_entries(config, role):
            try:
                Entry.parse(text)
            except RLError as exc:
                out.append(f"{CONFIG_NAME}: models.{role}: {exc.message}; it is skipped")
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    for key in ("beat_seconds", "clear_idle_minutes", "clear_context_tokens", "user_quiet_minutes",
                "heartbeat_minutes", "page_minutes"):
        value = agents.get(key)
        if value is not None and not (_minutes(value) and value > 0):
            out.append(f"{CONFIG_NAME}: agents.{key} must be a positive number, not {value!r}; using "
                       f"{DEFAULT_CONFIG['agents'][key]}")
    deadlines = agents.get("deadlines")
    for key, value in deadlines.items() if isinstance(deadlines, dict) else ():
        if not (_minutes(value) and value > 0):
            out.append(f"{CONFIG_NAME}: agents.deadlines.{key} must be a positive number of minutes, not {value!r}; "
                       "using the default")
    args = agents.get("args")
    for host, value in args.items() if isinstance(args, dict) else ():
        if value is not None and not isinstance(value, (str, list)):
            out.append(f"{CONFIG_NAME}: agents.args.{host} must be a list of arguments, not {value!r}; using "
                       f"{agent_args(config, host)}")
    limits = config.get("limits") if isinstance(config.get("limits"), dict) else {}
    for key in ("trial_minutes", "scientists"):
        value = limits.get(key)
        if value is not None and not (_minutes(value) and value > 0):
            out.append(f"{CONFIG_NAME}: limits.{key} must be a positive number, not {value!r}; using "
                       f"{DEFAULT_CONFIG['limits'][key]}")
    fast = limits.get("fast_lane")
    for unit, value in fast.items() if isinstance(fast, dict) else ():
        if not _minutes(value):
            out.append(f"{CONFIG_NAME}: limits.fast_lane.{unit} must be a number, not {value!r}; ignored")
    usage = config.get("usage")
    for key in DEFAULT_CONFIG["usage"]:
        value = usage.get(key) if isinstance(usage, dict) else None
        if value is not None and not _percent(value):
            out.append(f"{CONFIG_NAME}: usage.{key} must be a percentage from 0 to 100, not {value!r}; using "
                       f"{DEFAULT_CONFIG['usage'][key]}")
    compute = config.get("compute")
    repository = compute.get("repository") if isinstance(compute, dict) else None
    if repository is not None and repository not in REPOSITORIES:
        out.append(f"{CONFIG_NAME}: compute.repository must be {' or '.join(REPOSITORIES)}, not {repository!r}; "
                   "using none (rl creates no repository)")
    tests = compute.get("tests") if isinstance(compute, dict) else None
    if tests is not None and tests not in ("local", "job"):
        out.append(f"{CONFIG_NAME}: compute.tests must be local or job, not {tests!r}; using local")
    return out + [f"{CONFIG_NAME}: {text}" for text in compute_resources(config)[1]]


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def model_entries(config: dict, role: str) -> list[str]:
    """A role's model entries in order: a list, or one entry written as a string. Anything else gives the
    default list."""
    models = config.get("models")
    value = models.get(role) if isinstance(models, dict) else None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(DEFAULT_CONFIG["models"][role])


def usable_entries(config: dict, role: str) -> list[Entry]:
    """A role's model entries that parse, in order; config_warnings names the others, which are skipped."""
    out = []
    for text in model_entries(config, role):
        try:
            out.append(Entry.parse(text))
        except RLError:
            continue
    return out


def agent_args(config: dict, host: str) -> list[str]:
    """Extra launch arguments for one host's sessions (agents.args.<host>): a list, or a string split as a shell
    would; a value of another kind gives the default."""
    agents = config.get("agents")
    args = agents.get("args") if isinstance(agents, dict) else None
    value = args.get(host) if isinstance(args, dict) else DEFAULT_CONFIG["agents"]["args"].get(host)
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    if isinstance(value, list):
        return [str(v) for v in value]
    return [] if value is None else list(DEFAULT_CONFIG["agents"]["args"].get(host) or [])


def agents_value(config: dict, key: str) -> float:
    """A positive number from agents.* (beat_seconds, clear_idle_minutes, ...), else its default."""
    agents = config.get("agents")
    value = agents.get(key) if isinstance(agents, dict) else None
    return float(value) if _minutes(value) and value > 0 else float(DEFAULT_CONFIG["agents"][key])


def deadline_minutes(config: dict, item_type: str) -> float:
    """How long a to-do item of this type may wait before it is escalated (agents.deadlines)."""
    agents = config.get("agents")
    given = agents.get("deadlines") if isinstance(agents, dict) else None
    given = given if isinstance(given, dict) else {}
    for key in (item_type, "default"):
        value = given.get(key)
        if _minutes(value) and value > 0:
            return float(value)
    return float(DEFAULT_CONFIG["agents"]["deadlines"]["default"])


def limit(config: dict, key: str):
    """limits.trial_minutes, limits.scientists, limits.envelope_lines or limits.fast_lane, else the default."""
    limits = config.get("limits")
    value = limits.get(key) if isinstance(limits, dict) else None
    default = DEFAULT_CONFIG["limits"][key]
    if key == "fast_lane":
        return {str(k): float(v) for k, v in value.items() if _minutes(v)} if isinstance(value, dict) else {}
    if key == "envelope_lines":
        ok = isinstance(value, list) and value and all(_minutes(v) and v > 0 for v in value)
        return sorted(float(v) for v in value) if ok else list(default)
    return value if _minutes(value) and value > 0 else default


def tests_mode(config: dict) -> str:
    compute = config.get("compute")
    value = compute.get("tests") if isinstance(compute, dict) else None
    return value if value in ("local", "job") else "local"


def _minutes(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def usage_percent(config: dict, key: str) -> float:
    """Percent left in a subscription window at which a host's sessions hand over: usage.switch_left_percent, or
    usage.coordinator_host_left_percent on the coordinator's own host."""
    usage = config.get("usage")
    value = usage.get(key) if isinstance(usage, dict) else None
    return float(value) if _percent(value) else float(DEFAULT_CONFIG["usage"][key])


def _percent(value) -> bool:
    return _minutes(value) and value <= 100


RESOURCE_KEYS = ("kind", "host", "connect", "jobs", "workspace", "code", "git_remote", "git_url", "notes")
RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def mcp_tool_prefix(server: str) -> str:
    """The prefix pi-env-mcp gives the tools of an MCP server it runs on a host (<prefix>__<tool>): pi's
    createMcpToolName, where characters other than letters, digits and _ become _."""
    return re.sub(r"[^A-Za-z0-9_]", "_", server)


@dataclass(frozen=True)
class Resource:
    """One entry of compute.resources. local: this machine, code from the caller's checkout. remote: a host the
    agent reaches with ssh or pi-env-mcp (host is the ssh alias, or pi-env-mcp's host name); jobs say what starts
    the work there (shell: a background process; mcp:<server>; skill:<name>); connect: local accesses a shared
    cluster workspace from the login node without a network hop. An MCP server in jobs is reached through connect:
    with pi-env-mcp it runs on the host as one of pi-env-mcp's own servers, and its tools are the pi-env-mcp
    server's <prefix>__<tool> tools (executor()). workspace is the
    absolute remote directory that holds repo.git, snapshots/<commit>/ and runs/<attempt>/; code says how a commit
    gets there (git: pushed to git_remote and fetched by the host from git_url; archive: git archive, uploaded)."""
    name: str
    kind: str
    host: str | None = None
    connect: str = "ssh"
    server: str | None = None
    jobs: str = "shell"
    workspace: str | None = None
    code: str = "git"
    git_remote: str = "origin"
    git_url: str | None = None
    notes: str | None = None

    @property
    def remote(self) -> bool:
        return self.kind == "remote"

    @property
    def jobs_on_pi_env(self) -> bool:
        """jobs is an MCP server that pi-env-mcp runs on the host, reached through the connection itself."""
        return self.remote and self.connect == "pi-env-mcp" and self.jobs.startswith("mcp:")

    def executor(self) -> str:
        """What submits the work, as the agent reaches it. An MCP server in jobs is reached through connect: under
        pi-env-mcp it is one of pi-env-mcp's own servers on the host (pi-env-mcp mcp add <server> --host <host> --
        <command>), so the agent registers no separate server for it; ssh or local: a server of the agent's host."""
        kind, _, name = self.jobs.partition(":")
        if self.jobs_on_pi_env:
            return (f"MCP server {name}, which pi-env-mcp runs on {self.host} (tools {mcp_tool_prefix(name)}__<tool> "
                    f"of pi-env-mcp server {self.server or 'pi-env-mcp'})")
        if kind == "mcp":
            return f"MCP server {name}"
        return f"skill {name} (through {self.connect})" if kind == "skill" else self.jobs

    def describe(self) -> str:
        """One line: where it is, how it is reached, what starts jobs and how the code gets there."""
        if not self.remote:
            text = f"{self.name}: this machine, code from your checkout · jobs {self.jobs}"
        else:
            via = f"pi-env-mcp (server {self.server or 'pi-env-mcp'})" if self.connect == "pi-env-mcp" else self.connect
            code = (f"git (push to {self.git_remote}; the host clones {self.git_url or 'the same URL'})"
                    if self.code == "git" else "archive (git archive, shared filesystem)" if self.connect == "local"
                    else "archive (git archive, uploaded)")
            access = "shared cluster filesystem via local" if self.connect == "local" else f"host {self.host} via {via}"
            jobs = (f"{self.jobs} (run on the host by pi-env-mcp: tools {mcp_tool_prefix(self.jobs[4:])}__<tool>)"
                    if self.jobs_on_pi_env else self.jobs)
            text = (f"{self.name}: {access} · jobs {jobs} · workspace {self.workspace} · "
                    f"code {code}")
        return text + (f" · notes: {self.notes}" if self.notes else "")

    def record(self) -> dict:
        """What an attempt keeps of the resource it was registered for."""
        keys = ("kind", "jobs") if not self.remote else ("kind", "host", "connect", "server", "jobs", "workspace",
                                                          "code")
        return {"name": self.name, **{k: getattr(self, k) for k in keys if getattr(self, k) is not None}}


def _resource(name: str, value, out: list[str]) -> Resource | None:
    where = f"compute.resources.{name}"
    if not RESOURCE_NAME.match(name):
        out.append(f"{where}: a resource name is letters, digits, '.', '_' or '-'; it is skipped")
        return None
    if not isinstance(value, dict):
        out.append(f"{where} must hold keys (kind, host, connect, jobs, workspace, code), not {value!r}; it is "
                   "skipped")
        return None
    for key in value:
        if key not in RESOURCE_KEYS:
            out.append(f"unknown key {where}.{key} has no effect (a typo?); a resource takes "
                       f"{', '.join(RESOURCE_KEYS)}")
    text = {k: (str(v).strip() if v is not None else None) for k, v in value.items() if k in RESOURCE_KEYS}
    kind = text.get("kind") or ("remote" if text.get("host") or text.get("connect") == "local" else "local")
    if kind not in ("local", "remote"):
        out.append(f"{where}.kind must be local or remote, not {value.get('kind')!r}; it is skipped")
        return None
    jobs = text.get("jobs") or "shell"
    if not (jobs == "shell" or re.match(r"^(mcp|skill):\S+$", jobs)):
        out.append(f"{where}.jobs must be shell, mcp:<server> or skill:<name>, not {value.get('jobs')!r}; it is skipped")
        return None
    if kind == "local":
        ignored = [k for k in ("host", "connect", "workspace", "code", "git_remote", "git_url") if k in value]
        if ignored:
            out.append(f"{where}: {', '.join(ignored)} {'has' if len(ignored) == 1 else 'have'} no effect on a local "
                       "resource (code runs from the caller's checkout, outputs go to research/artifacts/)")
        return Resource(name, "local", jobs=jobs, notes=text.get("notes"))
    host, workspace = text.get("host"), text.get("workspace")
    connect, _, server = (text.get("connect") or "ssh").partition(":")
    if connect not in ("ssh", "pi-env-mcp", "local") or (connect != "pi-env-mcp" and server):
        out.append(f"{where}.connect must be ssh, pi-env-mcp, pi-env-mcp:<MCP server name> or local, not "
                   f"{value.get('connect')!r}; it is skipped")
        return None
    if connect == "local" and jobs == "shell":
        out.append(f"{where}: connect: local requires jobs: mcp:<server> or skill:<name>; never run workloads "
                   "on the login node; it is skipped")
        return None
    if (not host and connect != "local") or not workspace:
        missing = " and ".join(k for k, v in (("host", host or connect == "local"), ("workspace", workspace)) if not v)
        out.append(f"{where}: a remote resource needs {missing}; it is skipped")
        return None
    if not workspace.startswith("/"):
        out.append(f"{where}.workspace must be an absolute remote path (relative ones follow the remote shell's "
                   f"cd), not {workspace!r}; it is skipped")
        return None
    code = text.get("code") or ("archive" if connect == "local" else "git")
    if connect == "local" and code != "archive":
        out.append(f"{where}: connect: local requires code: archive (from the local commit, no push); it is skipped")
        return None
    if code not in ("git", "archive"):
        out.append(f"{where}.code must be git or archive, not {value.get('code')!r}; using git")
        code = "git"
    return Resource(name, "remote", host, connect, server or None, jobs, workspace.rstrip("/") or "/", code,
                    text.get("git_remote") or "origin", text.get("git_url"), text.get("notes"))


def coordinator_is_login(config: dict) -> bool:
    """Explicit execution policy, not hostname detection. Unknown values never authorize local workloads."""
    compute = config.get("compute")
    return isinstance(compute, dict) and compute.get("coordinator", "local") != "local"


def compute_resources(config: dict) -> tuple[dict[str, Resource], list[str]]:
    """The usable compute resources by name, and what is wrong with the others (each warning says what is used
    instead). Without a mapping the default is this machine, unless the login policy excludes it."""
    out: list[str] = []
    compute = config.get("compute")
    coordinator = compute.get("coordinator", "local") if isinstance(compute, dict) else "local"
    if coordinator not in ("local", "login"):
        out.append(f"compute.coordinator must be local or login, not {coordinator!r}; treating it as login")
    value = compute.get("resources") if isinstance(compute, dict) else None
    if not isinstance(value, dict) or not value:
        if value is not None and isinstance(compute, dict):
            out.append(f"compute.resources must map resource names to their settings, not {value!r}; using this "
                       "machine only (local)")
        value = DEFAULT_CONFIG["compute"]["resources"]
    found = {}
    for name, item in value.items():
        res = _resource(str(name), item, out)
        if res and coordinator_is_login(config) and (not res.remote or res.jobs == "shell"):
            out.append(f"compute.resources.{name}: coordinator is a login node; checkout and shell execution are disabled. "
                       "Use kind: remote, connect: local, jobs: mcp:<server> and a shared workspace; it is skipped")
        elif res:
            found[res.name] = res
    default = compute.get("default") if isinstance(compute, dict) else None
    if found and default is not None and str(default) not in found:
        out.append(f"compute.default {default!r} is not a usable resource; using {next(iter(found))}")
    return found, out


REPOSITORIES = ("github-private", "none")


def compute_repository(config: dict) -> str:
    """How rl session start creates a missing git remote for the resources that fetch code by git: github-private
    (gh repo create --private) or none. An unknown value creates nothing."""
    compute = config.get("compute")
    value = compute.get("repository", DEFAULT_CONFIG["compute"]["repository"]) if isinstance(compute, dict) \
        else DEFAULT_CONFIG["compute"]["repository"]
    return value if value in REPOSITORIES else "none"


def compute_default(config: dict) -> str | None:
    """The resource experiments run on unless a task or the coordinator says otherwise (compute.default, else the
    first usable resource)."""
    found, _ = compute_resources(config)
    compute = config.get("compute")
    default = str(compute.get("default")) if isinstance(compute, dict) and compute.get("default") else None
    return default if default in found else next(iter(found), None)


def load_config(root: Path, remember: bool = True) -> dict:
    """The configuration over the defaults. Nothing in the file stops a command: while it does not parse, the
    settings read the last time it did are used (the defaults before that), and config_warnings says so. remember
    False (rl --read-only) leaves that fallback as it is."""
    path, last = root / CONFIG_NAME, root / LAST_READ
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"it holds a {type(data).__name__}, not keys and values")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        reason = " ".join(str(exc).split())[:300]
        try:
            data, used = json.loads(last.read_text(encoding="utf-8")), "the settings read the last time it could be"
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data, used = {}, "the defaults"
        return {**_merge(DEFAULT_CONFIG, data), UNREAD: f"the file could not be read ({reason}); {used} are used "
                                                        "until it is fixed"}
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    try:
        if remember and last.parent.is_dir() and (not last.exists() or last.read_text(encoding="utf-8") != text):
            atomic_write(last, text.encode("utf-8"))
    except OSError:
        pass  # a read-only checkout: the fallback stays as it was
    return _merge(DEFAULT_CONFIG, data)


def repo_top(path: Path) -> Path | None:
    """The top of the git checkout that contains path, read from the files (no git process)."""
    for top in (path, *path.parents):
        if (top / ".git").exists():
            return top
    return None


def linked_worktree(path: Path) -> tuple[Path, Path] | None:
    """(top of this checkout, top of the main checkout) when path is inside a linked git worktree."""
    top = repo_top(path)
    dotgit = top / ".git" if top else None
    if not dotgit or not dotgit.is_file():
        return None
    try:
        text = dotgit.read_text(encoding="utf-8").strip()
        gitdir = Path(text.removeprefix("gitdir:").strip())
        gitdir = gitdir if gitdir.is_absolute() else (top / gitdir).resolve()
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        return None  # a submodule, or not a worktree git made
    return (top, common.parent) if common.name == ".git" else None


def _in_main(candidate: Path) -> tuple[Path, Path] | None:
    """(project in the main checkout, candidate) when candidate lies in a linked worktree of that project."""
    linked = linked_worktree(candidate)
    if not linked:
        return None
    top, main_top = linked
    root = main_top / candidate.relative_to(top)
    return (root, candidate) if (root / CONFIG_NAME).is_file() else None


def _checkout(root: Path, here: Path) -> Path:
    if os.environ.get(ENV_CHECKOUT):
        return Path(os.environ[ENV_CHECKOUT]).expanduser().resolve()
    linked = linked_worktree(here)
    if linked and repo_top(root) == linked[1]:
        return linked[0] / root.relative_to(linked[1])
    return root


def find_project(explicit: str | None = None, start: Path | None = None, read_only: bool = False) -> Project:
    """--project, then $SMOOTH_RESEARCH_PROJECT, then the nearest directory above cwd with smooth-research.yaml.
    In a linked git worktree of the project (a researcher's), the project is the one in the main checkout and
    the worktree is the caller's checkout. read_only (rl --read-only) writes nothing, not even the configuration's
    fallback copy."""
    here = (start or Path.cwd()).resolve()
    chosen = explicit or os.environ.get(ENV_PROJECT)
    if chosen:
        root = Path(chosen).expanduser().resolve()
        if not (root / CONFIG_NAME).is_file():
            raise RLError("NO_PROJECT", f"{root} has no {CONFIG_NAME}; run `rl init` there first")
        return Project(root, load_config(root, not read_only), _checkout(root, here))
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            main = _in_main(candidate)
            if main:
                return Project(main[0], load_config(main[0], not read_only), main[1])
            return Project(candidate, load_config(candidate, not read_only))
    linked = linked_worktree(here)
    if linked:  # the configuration is not committed, so the worktree has no copy of it
        top, main_top = linked
        for candidate in (here, *here.parents):
            root = main_top / candidate.relative_to(top)
            if (root / CONFIG_NAME).is_file():
                return Project(root, load_config(root, not read_only), candidate)
            if candidate == top:
                break
    raise RLError("NO_PROJECT", f"no {CONFIG_NAME} in {here} or its parents; run `rl init --plan <plan.md>` "
                                "in the research project directory")


def write_default_config(root: Path) -> Path:
    path = root / CONFIG_NAME
    if not path.exists():
        text = ("# Smooth Research configuration. Resource limits and scope live in docs/constraints.md; the\n"
                "# layout is fixed: experiment code in src/, documents for people in docs/, records in research/.\n"
                "# models: <entry>:<model>[@effort] per role (coordinator, scientist, researcher, reviewer,\n"
                "# advisor); entry is claude, codex, pi or manual; the first available one is used.\n"
                "# agents: launch arguments per host and the watcher's times (docs/configuration.md).\n"
                "# limits: trial_minutes (longest trial run), fast_lane (largest estimate per unit that skips a\n"
                "# new review round, e.g. {MIG·h: 0.5}), scientists (default per phase), envelope_lines.\n"
                "# compute: where experiments run; rl connects to none of them (rl compute steps prints how to\n"
                "# stage a commit there). compute.tests: local, or job when even tests must run as jobs.\n")
        path.write_text(text + yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
