"""Research project discovery, paths and the short configuration file."""

from __future__ import annotations

import copy
import json
import os
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
# constraints, the research graph page, the manuscript and the explanations the user asked for); everything
# else in research/: the records (database, sealed objects, packages, call logs, run artifacts) and the
# agents' working documents (experiment contracts, task notes, researcher notes, review and advice answers,
# stage reports).
CODE_DIR, DOCS_DIR, RESEARCH_DIR = "src", "docs", "research"
PLAN_PATH, CONSTRAINTS_PATH = "docs/plan.md", "docs/constraints.md"
# The settings read the last time the configuration file parsed: used while it does not.
LAST_READ = "research/state/config.last-read.json"
UNREAD = "_unread"  # in a loaded configuration: why the file could not be read

# Model entries are "<entry>:<model>[@effort]"; entry is claude, codex, pi or manual (a person submits the
# package). Researchers and reviewers use the first available entry of their list; every available advisor is
# asked, each on its own. agents.args are extra launch arguments for the sessions `rl agent open` starts in
# herdr tabs (no sandbox or approval flags are added otherwise: each CLI's own settings apply);
# agents.watch_minutes is the time between the watcher's rounds over those sessions (0 turns it off).
# usage: a Claude or Codex subscription is low when a 5-hour or weekly window has switch_left_percent or less left
# (coordinator_host_left_percent on the coordinator's own host); its researchers and reviewers then hand over to
# another entry of their role, and advisors keep the rest (usage.py).
# manuscript.latex is the command that builds the manuscript (the paper draft) as a PDF (null: Markdown).
# Every key here is read by the CLI; docs/configuration.md explains each one.
DEFAULT_CONFIG: dict = {
    "version": 1,
    "models": {
        "researcher": ["claude:claude-opus-5-5@high", "codex:gpt-6-astra@high"],
        "reviewer": ["codex:gpt-6-astra@medium", "claude:claude-opus-5-5@medium"],
        "advisor": ["codex:gpt-6-astra@max", "claude:claude-fable-5-1@high"],
    },
    "advice": {"max_rounds": 3},
    "agents": {"args": {"claude": ["--permission-mode", "auto"], "codex": [], "pi": []}, "watch_minutes": 20},
    "usage": {"switch_left_percent": 10, "coordinator_host_left_percent": 20},
    "context": {"work_target_tokens": 4000, "advice_target_tokens": 10000},
    "manuscript": {"latex": None},
}
MODEL_ROLES = ("researcher", "reviewer", "advisor")

RESEARCH_DIRS = ("state", "documents/objects", "packages", "mailbox", "snapshots", "literature", "calls",
                 "artifacts", "experiments", "tasks", "notes", "reviews", "advice", "reports")
DOCS_DIRS = ("explanations",)


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
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
            elif isinstance(value, dict) and isinstance(default[key], dict):
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
    rounds = (config.get("advice") or {}).get("max_rounds") if isinstance(config.get("advice"), dict) else None
    if not _count(rounds):
        out.append(f"{CONFIG_NAME}: advice.max_rounds must be a whole number of rounds, not {rounds!r}; using "
                   f"{DEFAULT_CONFIG['advice']['max_rounds']}")
    agents = config.get("agents")
    watch = agents.get("watch_minutes") if isinstance(agents, dict) else None
    if watch is not None and not _minutes(watch):
        out.append(f"{CONFIG_NAME}: agents.watch_minutes must be a number of minutes (0 turns the watcher off), not "
                   f"{watch!r}; using {DEFAULT_CONFIG['agents']['watch_minutes']}")
    args = agents.get("args") if isinstance(agents, dict) else None
    for host, value in args.items() if isinstance(args, dict) else ():
        if value is not None and not isinstance(value, (str, list)):
            out.append(f"{CONFIG_NAME}: agents.args.{host} must be a list of arguments, not {value!r}; using "
                       f"{agent_args(config, host)}")
    usage = config.get("usage")
    for key in DEFAULT_CONFIG["usage"]:
        value = usage.get(key) if isinstance(usage, dict) else None
        if value is not None and not _percent(value):
            out.append(f"{CONFIG_NAME}: usage.{key} must be a percentage from 0 to 100, not {value!r}; using "
                       f"{DEFAULT_CONFIG['usage'][key]}")
    context = config.get("context")
    for key in DEFAULT_CONFIG["context"]:
        value = context.get(key) if isinstance(context, dict) else None
        if value is not None and not _tokens(value):
            out.append(f"{CONFIG_NAME}: context.{key} must be a positive whole number of tokens, not {value!r}; "
                       f"using {DEFAULT_CONFIG['context'][key]}")
    manuscript = config.get("manuscript")
    latex = manuscript.get("latex") if isinstance(manuscript, dict) else None
    if latex is not None and not latex_command(config):
        out.append(f"{CONFIG_NAME}: manuscript.latex must be a build command such as 'latexmk -pdf "
                   f"-interaction=nonstopmode', or null for a Markdown draft, not {latex!r}; writing Markdown")
    return out


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


def context_target(config: dict, key: str) -> int:
    """A context pack's token target: context.work_target_tokens or context.advice_target_tokens."""
    context = config.get("context")
    value = context.get(key) if isinstance(context, dict) else None
    return value if _tokens(value) else DEFAULT_CONFIG["context"][key]


def _tokens(value) -> bool:
    return _count(value) and value > 0


def watch_minutes(config: dict) -> float:
    """Minutes between the watcher's rounds (agents.watch_minutes); 0 turns the watcher off."""
    agents = config.get("agents")
    value = agents.get("watch_minutes") if isinstance(agents, dict) else None
    return float(value) if _minutes(value) else float(DEFAULT_CONFIG["agents"]["watch_minutes"])


def latex_command(config: dict) -> list[str] | None:
    """The command that builds the manuscript as a PDF (manuscript.latex, a string or a list), or None: Markdown."""
    manuscript = config.get("manuscript")
    value = manuscript.get("latex") if isinstance(manuscript, dict) else None
    if isinstance(value, str):
        try:
            words = shlex.split(value)
        except ValueError:
            return None
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        words = list(value)
    else:
        return None
    return words or None


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


def advice_limit(config: dict) -> int:
    advice = config.get("advice")
    value = advice.get("max_rounds") if isinstance(advice, dict) else None
    return value if _count(value) else DEFAULT_CONFIG["advice"]["max_rounds"]


def load_config(root: Path) -> dict:
    """The configuration over the defaults. Nothing in the file stops a command: while it does not parse, the
    settings read the last time it did are used (the defaults before that), and config_warnings says so."""
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
        if last.parent.is_dir() and (not last.exists() or last.read_text(encoding="utf-8") != text):
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


def find_project(explicit: str | None = None, start: Path | None = None) -> Project:
    """--project, then $SMOOTH_RESEARCH_PROJECT, then the nearest directory above cwd with smooth-research.yaml.
    In a linked git worktree of the project (a researcher's), the project is the one in the main checkout and
    the worktree is the caller's checkout."""
    here = (start or Path.cwd()).resolve()
    chosen = explicit or os.environ.get(ENV_PROJECT)
    if chosen:
        root = Path(chosen).expanduser().resolve()
        if not (root / CONFIG_NAME).is_file():
            raise RLError("NO_PROJECT", f"{root} has no {CONFIG_NAME}; run `rl init` there first")
        return Project(root, load_config(root), _checkout(root, here))
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            main = _in_main(candidate)
            if main:
                return Project(main[0], load_config(main[0]), main[1])
            return Project(candidate, load_config(candidate))
    linked = linked_worktree(here)
    if linked:  # the configuration is not committed, so the worktree has no copy of it
        top, main_top = linked
        for candidate in (here, *here.parents):
            root = main_top / candidate.relative_to(top)
            if (root / CONFIG_NAME).is_file():
                return Project(root, load_config(root), candidate)
            if candidate == top:
                break
    raise RLError("NO_PROJECT", f"no {CONFIG_NAME} in {here} or its parents; run `rl init --plan <plan.md>` "
                                "in the research project directory")


def write_default_config(root: Path) -> Path:
    path = root / CONFIG_NAME
    if not path.exists():
        text = ("# Smooth Research configuration. Resource limits and scope live in docs/constraints.md; the\n"
                "# layout is fixed: experiment code in src/, documents for people in docs/, records in research/.\n"
                "# Model entries: <entry>:<model>[@effort]; entry is claude, codex, pi or manual. Researchers and\n"
                "# reviewers use the first available entry of their list; every available advisor is asked.\n"
                "# agents.args: extra launch arguments for sessions opened in herdr tabs (rl agent open);\n"
                "# agents.watch_minutes: minutes between the watcher's rounds over those sessions (0: off).\n"
                "# usage: when a Claude or Codex 5-hour or weekly window has this percent left or less, its\n"
                "# researchers and reviewers hand over to the next model entry (more is kept on the coordinator's\n"
                "# host); advisors keep the rest. rl usage shows the windows.\n"
                "# manuscript.latex: the command that builds the manuscript (rl manuscript) as a PDF, e.g.\n"
                "# latexmk -pdf -interaction=nonstopmode (null: a Markdown draft, docs/manuscript.md).\n")
        path.write_text(text + yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
