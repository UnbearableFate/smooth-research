"""Research project discovery, paths and the short configuration file."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .adapters import Entry
from .util import RLError

CONFIG_NAME = "smooth-research.yaml"
ENV_PROJECT = "SMOOTH_RESEARCH_PROJECT"

# Project layout: experiment code in src/, human-facing documents in docs/, reports in reports/, and machine
# records (database, sealed objects, packages, call logs, run artifacts) in research/.
# Model entries are "<entry>:<model>[@effort]"; entry is claude, codex, pi or manual (a person submits the
# package). Researchers and reviewers use the first available entry of their list; every available advisor is
# asked, each on its own. Every key here is read by the CLI; docs/configuration.md explains each one.
DEFAULT_CONFIG: dict = {
    "version": 1,
    "plan": "docs/plan/original.md",
    "constraints_ref": "docs/plan/constraints.md",
    "code_root": "src",
    "docs_root": "docs",
    "reports_root": "reports",
    "research_root": "research",
    "models": {
        "researcher": ["claude:claude-opus-5-5@high", "codex:gpt-6-astra@high"],
        "reviewer": ["codex:gpt-6-astra@medium", "claude:claude-opus-5-5@medium"],
        "advisor": ["codex:gpt-6-astra@max", "claude:claude-fable-5-1@high"],
    },
    "advice": {"max_rounds": 3},
    "context": {"work_target_tokens": 4000, "advice_target_tokens": 10000},
}
MODEL_ROLES = ("researcher", "reviewer", "advisor")

# Keys that earlier versions wrote into the configuration although nothing read them.
RETIRED_KEYS = {
    "artifacts": "digests are always blake2b and raw artifacts are always kept",
}

RESEARCH_DIRS = ("state", "documents/objects", "packages", "mailbox", "snapshots", "literature", "calls",
                 "artifacts")
DOCS_DIRS = ("plan", "experiments", "tasks", "notes")
REPORTS_DIRS = ("reviews", "advice")


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


@dataclass
class Project:
    root: Path
    config: dict

    @property
    def warnings(self) -> list[str]:
        return config_warnings(self.config)

    @property
    def research_root(self) -> Path:
        return self.root / self.config["research_root"]

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
        return self.root / self.config["code_root"]

    @property
    def docs_root(self) -> Path:
        return self.root / self.config["docs_root"]

    @property
    def reports_root(self) -> Path:
        return self.root / self.config["reports_root"]

    def dir(self, name: str, area: str = "research") -> Path:
        """A subdirectory of research/ (machine records), docs/ (human documents) or reports/."""
        base = {"research": self.research_root, "docs": self.docs_root, "reports": self.reports_root}[area]
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def rel(self, path: Path | str) -> str:
        """Project-relative path when inside the project, else absolute."""
        p = Path(path)
        p = p if p.is_absolute() else (Path.cwd() / p)
        p = Path(os.path.normpath(p))
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    def abs(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root / p


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
    return _merge(DEFAULT_CONFIG, data)


def find_project(explicit: str | None = None, start: Path | None = None) -> Project:
    """--project, then $SMOOTH_RESEARCH_PROJECT, then the nearest directory above cwd with smooth-research.yaml."""
    chosen = explicit or os.environ.get(ENV_PROJECT)
    if chosen:
        root = Path(chosen).expanduser().resolve()
        if not (root / CONFIG_NAME).is_file():
            raise RLError("NO_PROJECT", f"{root} has no {CONFIG_NAME}; run `rl init` there first")
        return Project(root, load_config(root))
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            return Project(candidate, load_config(candidate))
    raise RLError("NO_PROJECT", f"no {CONFIG_NAME} in {here} or its parents; run `rl init --plan <plan.md>` "
                                "in the research project directory")


def write_default_config(root: Path) -> Path:
    path = root / CONFIG_NAME
    if not path.exists():
        text = ("# Smooth Research configuration. Resource limits and scope live in the constraints document.\n"
                "# Model entries: <entry>:<model>[@effort]; entry is claude, codex, pi or manual. Researchers and\n"
                "# reviewers use the first available entry of their list; every available advisor is asked.\n")
        path.write_text(text + yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
