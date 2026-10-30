#!/usr/bin/env python3
"""Assemble an installable marketplace per host from the shared core and that host's thin adapter.

    python3 scripts/build_plugins.py [--target claude|codex|all] [--output dist]

Shared sources: the Python package (pyproject.toml, uv.lock, src/smooth_research) and plugin/ (bin/rl and
the skills). Host adapters: hosts/<host>/ (manifest, hooks, host notes, substitutions.json). Placeholders such
as {{rl}} in skills and agents are filled per host. Only the standard library is used; digests are blake2b.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NAME = "smooth-research"
HOSTS = {"claude": ".claude-plugin/plugin.json", "codex": ".codex-plugin/plugin.json"}
CATALOGS = {"claude": ".claude-plugin/marketplace.json", "codex": ".agents/plugins/marketplace.json"}
PACKAGE_FILES = ("pyproject.toml", "uv.lock", ".python-version", "README.md")
IGNORED = {"__pycache__", ".venv", ".DS_Store", ".pytest_cache"}
PLACEHOLDER = re.compile(r"\{\{[a-z_]+\}\}")


def blake(data: bytes) -> str:
    return "blake2b:" + hashlib.blake2b(data, digest_size=32).hexdigest()


def files_under(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in IGNORED for part in rel.parts) or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are not supported in plugin sources: {path}")
        if path.is_file():
            yield rel, path


def sources(target: str, repo: Path) -> list[tuple[str, Path]]:
    """(destination, source) pairs; a host adapter may add files but never shadow a shared one."""
    pairs: dict[str, Path] = {}

    def add(dest: str, src: Path) -> None:
        if dest in pairs:
            raise ValueError(f"{target}: {dest} comes from both {pairs[dest]} and {src}; keep one source")
        pairs[dest] = src

    for name in PACKAGE_FILES:
        if (repo / name).is_file():
            add(name, repo / name)
    for rel, path in files_under(repo / "src" / "smooth_research"):
        add(f"src/smooth_research/{rel}", path)
    for rel, path in files_under(repo / "plugin"):
        add(str(rel), path)
    for rel, path in files_under(repo / "hosts" / target):
        if str(rel) != "substitutions.json":
            add(str(rel), path)
    return sorted(pairs.items())


def render(dest: str, data: bytes, subs: dict) -> bytes:
    """Fill host placeholders in skills and agents: SKILL.md and agents get the 'skill' map, other docs 'reference'."""
    if not dest.endswith(".md") or not dest.startswith(("skills/", "agents/")):
        return data
    table = subs["skill"] if dest.endswith("SKILL.md") or dest.startswith("agents/") else subs["reference"]
    text = data.decode("utf-8")
    for key, value in table.items():
        text = text.replace(key, value)
    left = PLACEHOLDER.findall(text)
    if left:
        raise ValueError(f"unfilled placeholders {sorted(set(left))} in {dest}")
    return text.encode("utf-8")


def check_versions(repo: Path) -> str:
    version = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    init = (repo / "src/smooth_research/__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in init:
        raise ValueError(f"src/smooth_research/__init__.py __version__ differs from pyproject {version}")
    for target, manifest in HOSTS.items():
        data = json.loads((repo / "hosts" / target / manifest).read_text(encoding="utf-8"))
        if data.get("name") != NAME or data.get("version") != version:
            raise ValueError(f"hosts/{target}/{manifest}: name/version must be {NAME} {version}")
    return version


def assemble(target: str, destination: Path, repo: Path, version: str) -> dict:
    subs = json.loads((repo / "hosts" / target / "substitutions.json").read_text(encoding="utf-8"))
    plugin = destination / "plugins" / NAME
    digests = {}
    for dest, src in sources(target, repo):
        data = render(dest, src.read_bytes(), subs)
        out = plugin / dest
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        out.chmod(0o755 if src.stat().st_mode & 0o111 else 0o644)
        digests[dest] = blake(data)
    for required in ("bin/rl", "skills/smooth-research/SKILL.md", "skills/research-review/SKILL.md",
                     "skills/research-advise/SKILL.md", "skills/smooth-research/references/host.md",
                     "skills/smooth-research/references/workflow.md", HOSTS[target], "hooks/hooks.json",
                     "pyproject.toml", "uv.lock", "src/smooth_research/cli.py"):
        if required not in digests:
            raise ValueError(f"{target} build is missing {required}")
    build_id = hashlib.blake2b(json.dumps(digests, sort_keys=True).encode(), digest_size=8).hexdigest()
    (plugin / ".build-id").write_text(build_id + "\n", encoding="utf-8")
    catalog = json.loads((repo / CATALOGS[target]).read_text(encoding="utf-8"))
    entry = next(p for p in catalog["plugins"] if p["name"] == NAME)
    entry["source"] = (f"./plugins/{NAME}" if target == "claude" else {"source": "local", "path": f"./plugins/{NAME}"})
    catalog["plugins"] = [entry]
    out = destination / CATALOGS[target]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record = {"target": target, "name": NAME, "version": version, "build_id": build_id,
              "files": dict(sorted(digests.items()))}
    (destination / "BUILD.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def archive(source: Path, output: Path, prefix: str) -> None:
    """Stable order, timestamps and modes: equal inputs give equal archives."""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for rel, path in files_under(source):
            info = zipfile.ZipInfo(f"{prefix}/{rel}", date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100000 | (path.stat().st_mode & 0o777)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, path.read_bytes())


def build(targets: list[str], output: Path, repo: Path = REPO) -> list[dict]:
    output, repo = output.resolve(), repo.resolve()
    protected = [repo / d for d in ("src", "plugin", "hosts", "scripts", "tests", "design")]
    if output == repo or any(output == p or p in output.parents or output in p.parents for p in protected):
        raise ValueError(f"output {output} overlaps the sources")
    version = check_versions(repo)
    output.mkdir(parents=True, exist_ok=True)
    for target in targets:
        old = output / target
        if old.is_symlink() or (old.exists() and not (old / "BUILD.json").is_file()):
            raise ValueError(f"refusing to replace a directory that is not a build: {old}")
    results = []
    with tempfile.TemporaryDirectory(prefix=".build-", dir=output) as work:
        staging = Path(work)
        for target in targets:  # assemble and validate every target before replacing any output
            record = assemble(target, staging / target, repo, version)
            name = f"{NAME}-{target}-{version}.zip"
            archive(staging / target, staging / name, f"{NAME}-{target}")
            results.append({**record, "archive": str(output / name)})
        for record in results:
            old = output / record["target"]
            if old.exists():
                shutil.rmtree(old)
            (staging / record["target"]).replace(old)
            name = Path(record["archive"]).name
            (staging / name).replace(output / name)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", choices=[*HOSTS, "all"], default="all")
    parser.add_argument("--output", type=Path, default=REPO / "dist")
    args = parser.parse_args()
    try:
        results = build(list(HOSTS) if args.target == "all" else [args.target], args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"build failed: {exc}\n")
    for r in results:
        print(f"{r['target']}: {len(r['files'])} files, build {r['build_id']} -> "
              f"{args.output / r['target'] / 'plugins' / NAME}  ({r['archive']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
