"""Hierarchical labels: the names people read for hypotheses and experiments.

H2.1.3 is the third hypothesis placed under H2.1; EH2.1/2 is the second experiment anchored at H2.1. The internal
ids (H-0013, E-0016) stay the keys: relations, events, file names, job names, commits, run specs and sealed
documents use only them. A label is given once, when the record is created, and never changes, so text written
with it (decisions, messages, the manuscript) keeps naming the same node when relations are added later; it shows
one primary parent, and the other sources and tested hypotheses stay relations, listed by rl show.

A hypothesis derived from no hypothesis (from nothing, or only from papers or advice answers) starts a new
top-level line: H1, H2, ... One derived from hypotheses goes under its primary parent: the only one, else the
latest created (usually the most specific), unless --parent names another of them. --root starts a new top-level
line anyway, for a hypothesis that opens a new line of research; its relations are kept as usual. An experiment
goes under the deepest common ancestor of the hypotheses it tests (the longest common prefix of their labels),
else under the first of them; --anchor names another hypothesis. Numbers are never reused: the next one is one
more than the largest given under the same parent, and rejected or retired records keep theirs.

An internal id always contains '-'; a label never contains '-', ',' or '@'. So the CLI takes either form wherever it
takes a hypothesis or an experiment (a label becomes the id before any command runs), comma-separated lists and
@revision suffixes work with both, and the two never mix up.
"""

from __future__ import annotations

import re

from .store import Tx, _sort_key
from .util import LABEL_RE, RLError

# the internal id of a hypothesis or an experiment in free text; not inside a path or a file name (E-0016.md)
_ID_IN_TEXT = re.compile(r"(?<![\w/.-])([HE]-\d+)(@\d+)?(?![\w/-]|\.\w)")


def parse(label: str) -> tuple[str, tuple[int, ...], int | None]:
    """("H", (2, 1, 3), None) for H2.1.3; ("EH", (2, 1), 2) for EH2.1/2."""
    kind = "EH" if label.startswith("EH") else "H"
    path, _, n = label[len(kind):].partition("/")
    return kind, tuple(int(x) for x in path.split(".")), int(n) if n else None


def _taken(tx: Tx, entity: str) -> list[str]:
    return [r["l"] for r in tx.q(f"MATCH (n:{entity}) RETURN n.label AS l") if r["l"]]


def _next(taken: list[str], prefix: str) -> int:
    """One more than the largest number given right under prefix ("H2.1." with H2.1.3 given: 4)."""
    numbers = [int(rest) for label in taken if label.startswith(prefix) and (rest := label[len(prefix):]).isdigit()]
    return max(numbers, default=0) + 1


def _label(tx: Tx, hid: str) -> str:
    label = tx.require(hid, "Hypothesis").get("label")
    if not label:
        raise RLError("CORRUPT", f"{hid} has no label; run rl upgrade")
    return label


def for_hypothesis(tx: Tx, sources: list[str], parent: str | None = None, root: bool = False) -> str:
    """The label of a new hypothesis derived from the hypotheses sources (papers and advice do not place it)."""
    if parent and root:
        raise RLError("INVALID", "--parent puts the hypothesis under one of its sources and --root starts a new line: "
                                 "give one of them")
    if parent and parent not in sources:
        raise RLError("INVALID", f"--parent {parent} is not one of the hypotheses it is derived from "
                                 f"({', '.join(sources) or 'none'}); add it to --derived-from")
    taken = _taken(tx, "Hypothesis")
    if root or not sources:
        return f"H{_next(taken, 'H')}"
    base = _label(tx, parent or max(sources, key=_sort_key))  # the ids count up: the largest is the latest
    return f"{base}.{_next(taken, base + '.')}"


def for_experiment(tx: Tx, hypotheses: list[str], anchor: str | None = None) -> str:
    """The label of a new experiment testing hypotheses (in the order given: the first is the fallback anchor)."""
    if anchor:
        base = _label(tx, anchor)
    else:
        names = [_label(tx, hid) for hid in dict.fromkeys(hypotheses)]
        common = []
        for level in zip(*(parse(name)[1] for name in names)):
            if len(set(level)) > 1:
                break
            common.append(level[0])
        base = "H" + ".".join(map(str, common)) if common else names[0]
    return f"E{base}/{_next(_taken(tx, 'Experiment'), f'E{base}/')}"


def resolve(tx: Tx, ref: str) -> str:
    """The internal id (with the @revision given) of a label; anything that is not a label comes back as it is."""
    m = LABEL_RE.match(ref)
    if not m:
        return ref
    entity = "Experiment" if m["label"].startswith("E") else "Hypothesis"
    rows = tx.q(f"MATCH (n:{entity} {{label: $label}}) RETURN n.id AS id", label=m["label"])
    if not rows:
        raise RLError("NOT_FOUND", f"no {entity.lower()} is labelled {m['label']}")
    if len(rows) > 1:
        raise RLError("CORRUPT", f"{m['label']} labels {', '.join(sorted(r['id'] for r in rows))}")
    return rows[0]["id"] + (f"@{m['rev']}" if m["rev"] else "")


def name(rec: dict) -> str:
    """How people read a record: its label, else its id."""
    return rec.get("label") or rec["id"]


def ordered(records: list[dict]) -> list[dict]:
    """By label: H2.9 before H2.10, a hypothesis before the ones under it."""
    return sorted(records, key=lambda r: _sort_key(name(r)))


def names(tx: Tx) -> dict[str, str]:
    """Internal id -> label of every hypothesis and experiment that has one."""
    return {r["id"]: r["l"] for entity in ("Hypothesis", "Experiment")
            for r in tx.q(f"MATCH (n:{entity}) RETURN n.id AS id, n.label AS l") if r["l"]}


def relabel(text: str, known: dict[str, str]) -> str:
    """text with the internal ids of hypotheses and experiments replaced by their labels (paths and file names
    keep the ids), so records written before labels existed read the same way as new ones."""
    def swap(m: re.Match) -> str:
        label = known.get(m[1])
        return label + (m[2] or "") if label else m[0]
    return _ID_IN_TEXT.sub(swap, text)
