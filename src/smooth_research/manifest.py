"""The experiment manifest: the arms of an experiment, written by the scientist in the design commit (design 5.1).

A YAML or JSON file of the project's code, for example src/e1/arms.yaml:

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
    allowed: [batch_size]          # parameters a run may change without a new design (fast lane, 6.6)
    stop_when: the positive control of the first seed fails

The configuration a run is registered with is compared with its arm's: the digest of (arm, seed, split, config)
computed from the manifest must equal the one computed from the run (allowed keys left out of both). Changing
the manifest is changing the experiment: a new revision, by the scientist.
"""

from __future__ import annotations

import json

import yaml

from .util import RLError, digest_json


def parse(text: str, where: str) -> dict:
    try:
        data = yaml.safe_load(text) if not where.endswith(".json") else json.loads(text)
    except (yaml.YAMLError, ValueError) as exc:
        raise RLError("INVALID", f"the experiment manifest {where} does not parse: {' '.join(str(exc).split())[:300]}")
    if not isinstance(data, dict) or not isinstance(data.get("arms"), list) or not data["arms"]:
        raise RLError("INVALID", f"the experiment manifest {where} needs arms: a list of {{name, seeds, split, "
                                 "config}} (see rl experiment add --help)")
    names = []
    for arm in data["arms"]:
        if not isinstance(arm, dict) or not isinstance(arm.get("name"), (str, int)):
            raise RLError("INVALID", f"{where}: each arm is a mapping with a name")
        if not isinstance(arm.get("config", {}), dict):
            raise RLError("INVALID", f"{where}: arm {arm['name']}: config is a mapping of parameters")
        seeds = arm.get("seeds", [None])
        if not isinstance(seeds, list):
            raise RLError("INVALID", f"{where}: arm {arm['name']}: seeds is a list")
        names.append(str(arm["name"]))
    if len(set(names)) != len(names):
        raise RLError("INVALID", f"{where}: arm names repeat")
    if data.get("allowed") is not None and not isinstance(data["allowed"], list):
        raise RLError("INVALID", f"{where}: allowed is a list of parameter names")
    return data


PER_ARM = ("config", "seeds", "split")


def unapplied(data: dict) -> str | None:
    """A warning for top-level keys that belong to each arm (config, seeds, split): they are not applied to the arms,
    so a run's actual configuration would not match its arm's. Warned, not refused: a manifest sealed with them
    stays usable."""
    found = [k for k in PER_ARM if k in data]
    if not found:
        return None
    return (f"the manifest's top-level {', '.join(found)} is not applied to the arms: each arm carries its own "
            f"(arms: - name: .. seeds: [..] config: {{..}}); a run is checked against its arm only")


def arm(manifest: dict, name: str) -> dict | None:
    return next((a for a in manifest.get("arms") or [] if str(a.get("name")) == str(name)), None)


def config_digest(name: str, seed, split, config: dict, allowed: list[str]) -> str:
    kept = {k: v for k, v in (config or {}).items() if k not in allowed}
    return digest_json({"arm": str(name), "seed": None if seed is None else str(seed), "split": split,
                        "config": kept})


def effective(manifest: dict, name: str, config: dict | None, split) -> tuple[dict | None, object]:
    """The configuration and split a run registers with: what it gives, else its arm's (a run that names only its
    arm runs the arm as written, and is recorded so)."""
    a = arm(manifest, name) or {}
    return (dict(a.get("config") or {}) if config is None else config), (a.get("split") if split is None else split)


def check_run(manifest: dict, name: str, seed, config: dict | None, split=None) -> list[str]:
    """Why a run with this arm, seed, configuration and split does not match the manifest; [] when it does. A
    configuration or split not given is the arm's (effective)."""
    a = arm(manifest, name)
    if a is None:
        return [f"arm {name!r} is not in the manifest (arms: {', '.join(str(x['name']) for x in manifest['arms'])})"]
    config, split = effective(manifest, name, config, split)
    out = []
    seeds = a.get("seeds", [None])
    if seeds != [None] and (seed is None or str(seed) not in {str(s) for s in seeds}):
        out.append(f"seed {seed!r} is not one of arm {name}'s seeds ({', '.join(map(str, seeds))})")
    allowed = [str(k) for k in manifest.get("allowed") or []]
    expected = config_digest(name, seed, a.get("split"), a.get("config") or {}, allowed)
    actual = config_digest(name, seed, split, config or {}, allowed)
    if expected != actual:
        want, got = a.get("config") or {}, config or {}
        keys = sorted(k for k in set(want) | set(got) if k not in allowed and want.get(k) != got.get(k))
        out.append("configuration differs from the manifest in " + ", ".join(
            f"{k} ({want.get(k)!r} → {got.get(k)!r})" for k in keys[:6]) if keys else
            f"the split differs ({a.get('split')!r} → {split!r})")
    return out


def describe(manifest: dict) -> str:
    arms = manifest.get("arms") or []
    seeds = {len(a.get("seeds") or [None]) for a in arms}
    metrics = manifest.get("metrics") or []
    return (f"{len(arms)} arm(s) ({', '.join(str(a['name']) for a in arms)})"
            + (f" × {seeds.pop()} seed(s)" if len(seeds) == 1 else "")
            + (f" · metrics {', '.join(map(str, metrics))}" if metrics else ""))
