"""Research project discovery, paths and the short configuration file."""

from __future__ import annotations

import copy
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

from .adapters import Entry
from .util import RLError

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
# Where projects made before 2026-10-31 keep them; read while the new file is absent.
EARLIER_PLAN, EARLIER_CONSTRAINTS = "docs/plan/original.md", "docs/plan/constraints.md"

# Model entries are "<entry>:<model>[@effort]"; entry is claude, codex, pi or manual (a person submits the
# package). Researchers and reviewers use the first available entry of their list; every available advisor is
# asked, each on its own. agents.args are extra launch arguments for the sessions `rl agent open` starts in
# herdr tabs (no sandbox or approval flags are added otherwise: each CLI's own settings apply);
# agents.watch_minutes is the time between the watcher's rounds over those sessions (0 turns it off).
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
    "context": {"work_target_tokens": 4000, "advice_target_tokens": 10000},
    "manuscript": {"latex": None},
}
MODEL_ROLES = ("researcher", "reviewer", "advisor")

# Keys that earlier versions wrote into the configuration, and why nothing reads them now.
RETIRED_KEYS = {
    "artifacts": "digests are always blake2b and raw artifacts are always kept",
    "plan": f"the imported plan is {PLAN_PATH}; an earlier project's {EARLIER_PLAN} is still read",
    "constraints_ref": f"the constraints document is {CONSTRAINTS_PATH}; an earlier project's {EARLIER_CONSTRAINTS} "
                       "is still read",
    "code_root": f"experiment code is in {CODE_DIR}/",
    "docs_root": f"documents for people are in {DOCS_DIR}/",
    "reports_root": f"review and advice answers and stage reports are in {RESEARCH_DIR}/, the research graph page "
                    f"in {DOCS_DIR}/",
    "research_root": f"the records are in {RESEARCH_DIR}/",
}

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
        return self._kept(PLAN_PATH, EARLIER_PLAN)

    @property
    def constraints_path(self) -> Path:
        return self._kept(CONSTRAINTS_PATH, EARLIER_CONSTRAINTS)

    def _kept(self, path: str, earlier: str) -> Path:
        """path, or where a project made before 2026-10-31 keeps the file while path does not exist."""
        new, old = self.root / path, self.root / earlier
        return old if old.is_file() and not new.exists() else new

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
    """Keys of a loaded configuration that the CLI does not read (a typo or a retired key), model lists in an
    older form or with an entry that does not parse, and an advice limit that is not a count. None stops a
    command."""
    out = []

    def walk(given: dict, default: dict, prefix: str) -> None:
        for key, value in given.items():
            path = prefix + str(key)
            if path in RETIRED_KEYS:
                out.append(f"{CONFIG_NAME}: {path} is no longer used ({RETIRED_KEYS[path]}); delete it")
            elif key not in default:
                out.append(f"{CONFIG_NAME}: unknown key {path} has no effect (a typo?)")
            elif isinstance(value, dict) and isinstance(default[key], dict):
                walk(value, default[key], path + ".")

    walk(config, DEFAULT_CONFIG, "")
    models = config.get("models")
    for role in MODEL_ROLES if isinstance(models, dict) else ():
        if isinstance(models.get(role), dict) or models.get(role) == "inherit":
            out.append(f"{CONFIG_NAME}: models.{role} has the form used before 2026-10-31; write it as a list of "
                       f"model entries (read now as {model_entries(config, role)})")
        for text in model_entries(config, role):
            try:
                Entry.parse(text)
            except RLError as exc:
                out.append(f"{CONFIG_NAME}: models.{role}: {exc.message}")
    rounds = (config.get("advice") or {}).get("max_rounds") if isinstance(config.get("advice"), dict) else None
    if not _count(rounds):
        out.append(f"{CONFIG_NAME}: advice.max_rounds must be a whole number of rounds, not {rounds!r}; using "
                   f"{DEFAULT_CONFIG['advice']['max_rounds']}")
    agents = config.get("agents")
    watch = agents.get("watch_minutes") if isinstance(agents, dict) else None
    if watch is not None and not _minutes(watch):
        out.append(f"{CONFIG_NAME}: agents.watch_minutes must be a number of minutes (0 turns the watcher off), not "
                   f"{watch!r}; using {DEFAULT_CONFIG['agents']['watch_minutes']}")
    manuscript = config.get("manuscript")
    latex = manuscript.get("latex") if isinstance(manuscript, dict) else None
    if latex is not None and not latex_command(config):
        out.append(f"{CONFIG_NAME}: manuscript.latex must be a build command such as 'latexmk -pdf "
                   f"-interaction=nonstopmode', or null for a Markdown draft, not {latex!r}; writing Markdown")
    return out


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def model_entries(config: dict, role: str) -> list[str]:
    """A role's model entries in order. Forms written before 2026-10-31 are still read: reviewer
    {candidates, relation_to_author} gives its candidates, advisor {primary, fallback} its primary, and
    researcher "inherit" the default list."""
    models = config.get("models")
    value = models.get(role) if isinstance(models, dict) else None
    if value is None or value == "inherit":
        return list(DEFAULT_CONFIG["models"][role])
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(v) for v in value.get("candidates") or [value.get("primary")] if v]
    return [str(v) for v in value]


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


def advice_limit(config: dict) -> int:
    advice = config.get("advice")
    value = advice.get("max_rounds") if isinstance(advice, dict) else None
    return value if _count(value) else DEFAULT_CONFIG["advice"]["max_rounds"]


def load_config(root: Path) -> dict:
    path = root / CONFIG_NAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RLError("INVALID", f"{path} is not valid YAML: {exc}") from None
    if data.get("version") != 1:
        raise RLError("INVALID", f"{path}: unsupported config version {data.get('version')!r}; expected 1")
    moved = data.get("research_root")
    if moved is not None and Path(str(moved)) != Path(RESEARCH_DIR):
        raise RLError("INVALID", f"{path}: research_root is no longer read; the records are always in "
                                 f"{RESEARCH_DIR}/. Rename {moved}/ to {RESEARCH_DIR}/, then delete research_root")
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
                "# manuscript.latex: the command that builds the manuscript (rl manuscript) as a PDF, e.g.\n"
                "# latexmk -pdf -interaction=nonstopmode (null: a Markdown draft, docs/manuscript.md).\n")
        path.write_text(text + yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
