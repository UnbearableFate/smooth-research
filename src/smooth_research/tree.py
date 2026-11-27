"""The hypothesis tree from documents: drafted by the coordinator's subagent, approved by the user, recorded at once
(design 2026-11-27, 7.2).

Building the tree is no task and takes no role. `rl tree prompt` prints the instructions for the subagent: what is
a hypothesis and what is a task under it (7.1), the documents to read, the tree as recorded, the draft's format and
the summary to hand back. The subagent writes research/tree-draft/tree.yaml, which is no record: `rl tree check`
checks its format and records nothing, `rl view` draws it on the research page for the user, and it changes as often
as the user asks. Once the user approves, `rl tree record --approval "<their words>"` records in one transaction the
hypotheses (their doubts as the hypothesis's `flag`), one final route decision on why these hypotheses, the shared
preparation work as queued public prep tasks, and the user's approval as an answered Ask on that decision; then
the draft is deleted, its content being in the records. Without --approval it is recorded all the same, flagged
unapproved.

tree.yaml:

    why: why these hypotheses (the route decision's rationale)
    hypotheses:
      - key: a short name, unique in the draft (other entries' parent may name it)
        kind: main | competing
        title, statement, scope, purpose: as `rl hypothesis add` takes them
        source: "<document path> § <section>"
        parent: empty, a recorded hypothesis (H-0003 or H1.2), or another entry's key
        cost: the expected use of its whole test
        doubt: {lack: novelty | feasibility, basis, options}   (only when one falls short)
    prep:
      - {title, requirement, done_check}
    questions: [what only the user can settle]
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from . import documents, labels
from .flags import Flags
from .project import PLAN_PATH, Project
from .research import tree_lines
from .roles import Actor, need
from .store import Store, Tx
from .util import LABEL_RE, RLError, digest_bytes, now_utc, short

DRAFT_DIR = "research/tree-draft"
DRAFT_FILE = "tree.yaml"
SECTION = "§"  # source: "<document path> § <section>"
KINDS = ("main", "competing")
LACKS = ("novelty", "feasibility")
TOP_KEYS = ("why", "hypotheses", "prep", "questions")
HYP_KEYS = ("key", "kind", "title", "statement", "scope", "purpose", "source", "parent", "cost", "doubt")
DOUBT_KEYS = ("lack", "basis", "options")
PREP_KEYS = ("title", "requirement", "done_check")
_ID = re.compile(r"^H-\d+$")


def draft_dir(project: Project) -> Path:
    return project.research_root / "tree-draft"


def draft_path(project: Project) -> Path:
    """research/tree-draft/tree.yaml: the draft the subagent writes (not a record)."""
    return draft_dir(project) / DRAFT_FILE


def has_draft(project: Project) -> bool:
    """Whether an unrecorded draft is waiting for the user (the coordinator's to-do says so instead of 'build the
    tree', so a restarted coordinator does not open a second subagent)."""
    return draft_path(project).is_file()


def source_parts(project: Project, source: str | None) -> tuple[str | None, str | None]:
    """(document, section) a hypothesis's `source` names: "<path> § <section>" as the draft writes it. An older
    record's source is a heading of the plan: (docs/plan.md, heading). A document path comes back as written
    (relative to the project); it may no longer exist."""
    text = (source or "").strip()
    if not text:
        return None, None
    doc, sep, section = text.partition(SECTION)
    if sep:
        return doc.strip() or None, section.strip() or None
    if project.abs(text).is_file():
        return text, None
    return (PLAN_PATH if project.plan_path.is_file() else None), text


# -- the draft ------------------------------------------------------------------------------------------------


@dataclass
class Draft:
    """A draft as read: what it holds, normalized as far as it goes (the page draws it even with problems), the
    problems that keep it from being recorded, and warnings (recorded all the same)."""
    why: str | None = None
    hypotheses: list[dict] = field(default_factory=list)
    prep: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: str | None = None  # the file's text
    data: bytes = b""  # its bytes, as digested and sealed
    parsed: bool = False

    def problem_text(self, rel: str) -> str:
        return (f"{rel} has {len(self.problems)} problem{'s' if len(self.problems) > 1 else ''} (nothing was "
                "recorded; fix the draft and run rl tree check again):\n" + "\n".join(f"- {p}" for p in self.problems))


def _text(value, where: str, problems: list[str], required: bool = False, why: str = "") -> str | None:
    """A text field: a string (numbers and dates are taken as their text); missing or empty is None."""
    if isinstance(value, (bool, list, dict)):
        problems.append(f"{where}: a text, not {type(value).__name__}")
        return None
    value = "" if value is None else str(value).strip()  # YAML reads 2026 and 2026-11-27 as a number and a date
    if not value and required:
        problems.append(f"{where}: missing{(' (' + why + ')') if why else ''}")
    return value or None


def _unknown(obj: dict, keys: tuple[str, ...], where: str, problems: list[str]) -> None:
    for k in obj:
        if k not in keys:
            problems.append(f"{where}: {k!r} is not a field of the draft ({', '.join(keys)}); it would not be "
                            "recorded")


def read(project: Project) -> Draft:
    """Read and check the draft's format (no records needed: the parents it names are checked by resolve)."""
    path = draft_path(project)
    d = Draft()
    if not path.is_file():
        d.problems.append(f"no draft at {DRAFT_DIR}/{DRAFT_FILE}: rl tree prompt says how to write one")
        return d
    d.data = path.read_bytes()  # the bytes as written (CRLF too): what is digested and sealed
    try:
        d.raw = d.data.decode("utf-8")  # strictly: a replaced character would change the claims recorded from it
    except UnicodeDecodeError as exc:
        d.raw = d.data.decode("utf-8", errors="replace")  # to show it on the page
        d.problems.append(f"it is not UTF-8 text (byte {exc.start}): save it as UTF-8")
        return d
    try:
        data = yaml.safe_load(d.raw)
    except yaml.YAMLError as exc:
        d.problems.append(f"it does not parse as YAML: {exc}".replace("\n", " "))
        return d
    d.parsed = True
    p = d.problems
    if not isinstance(data, dict):
        p.append("the draft is a mapping with the keys why, hypotheses, prep and questions")
        return d
    _unknown(data, TOP_KEYS, "the draft", p)
    d.why = _text(data.get("why"), "why", p, required=True, why="why these hypotheses: it becomes the route decision")
    hyps = data.get("hypotheses")
    if not isinstance(hyps, list) or not hyps:
        p.append("hypotheses: a list of at least one hypothesis")
        hyps = hyps if isinstance(hyps, list) else []
    seen: set[str] = set()
    for i, raw in enumerate(hyps, 1):
        where = f"hypotheses[{i}]"
        if not isinstance(raw, dict):
            p.append(f"{where}: a mapping (key, kind, title, statement, ...)")
            continue
        key = _text(raw.get("key"), f"{where}.key", p, required=True, why="a short name, unique in the draft")
        if key:
            where = f"hypothesis {key!r}"
            if key in seen:
                p.append(f"{where}: the key is used twice")
            elif _ID.match(key) or LABEL_RE.match(key):
                p.append(f"{where}: the key looks like a recorded hypothesis's id or label; use a short name such as "
                         "'main'")
            seen.add(key)
        _unknown(raw, HYP_KEYS, where, p)
        h = {"key": key or f"#{i}"}
        h["kind"] = _text(raw.get("kind"), f"{where}.kind", p, required=True, why="main or competing")
        if h["kind"] and h["kind"] not in KINDS:
            p.append(f"{where}.kind: main or competing, not {h['kind']!r}")
        for name, why in (("title", "a short title"), ("statement", "the testable claim with its decision rule"),
                          ("purpose", "one sentence on what testing it answers")):
            h[name] = _text(raw.get(name), f"{where}.{name}", p, required=True, why=why)
        for name in ("scope", "source", "parent", "cost"):
            h[name] = _text(raw.get(name), f"{where}.{name}", p)
        for name, why in (("scope", "where it is claimed to hold"),
                          ("source", f"which document and section: '<path> {SECTION} <section>'"),
                          ("cost", "the expected use of its whole test, for the budget")):
            if not h[name]:
                d.warnings.append(f"{where}: no {name} ({why})")
        if h["source"]:
            doc, sep, _ = h["source"].partition(SECTION)
            if not sep:
                d.warnings.append(f"{where}.source: write it as '<document path> {SECTION} <section>' so the "
                                  "scientist finds the document")
            elif not (project.abs(doc.strip()).is_file() or project.src(doc.strip()).is_file()):
                d.warnings.append(f"{where}.source: {doc.strip()!r} is not a file of the project")
        doubt = raw.get("doubt")
        if doubt not in (None, "", {}):
            if not isinstance(doubt, dict):
                p.append(f"{where}.doubt: a mapping with lack, basis and options")
            else:
                _unknown(doubt, DOUBT_KEYS, f"{where}.doubt", p)
                h["doubt"] = {
                    "lack": _text(doubt.get("lack"), f"{where}.doubt.lack", p, required=True,
                                  why="novelty or feasibility"),
                    "basis": _text(doubt.get("basis"), f"{where}.doubt.basis", p, required=True,
                                   why="the paper and where in it, or the constraint"),
                    "options": _text(doubt.get("options"), f"{where}.doubt.options", p, required=True,
                                     why="drop, narrow, test another way, go ahead")}
                if h["doubt"]["lack"] and h["doubt"]["lack"] not in LACKS:
                    p.append(f"{where}.doubt.lack: novelty or feasibility, not {h['doubt']['lack']!r}")
        d.hypotheses.append(h)
    prep = data.get("prep")
    if prep not in (None, ""):
        if not isinstance(prep, list):
            p.append("prep: a list of {title, requirement, done_check}")
            prep = []
        for i, raw in enumerate(prep, 1):
            where = f"prep[{i}]"
            if not isinstance(raw, dict):
                p.append(f"{where}: a mapping with title, requirement and done_check")
                continue
            _unknown(raw, PREP_KEYS, where, p)
            d.prep.append({
                "title": _text(raw.get("title"), f"{where}.title", p, required=True),
                "requirement": _text(raw.get("requirement"), f"{where}.requirement", p, required=True,
                                     why="what to produce"),
                "done_check": _text(raw.get("done_check"), f"{where}.done_check", p, required=True,
                                    why="a command that exits 0 when it is done")})
    questions = data.get("questions")
    if isinstance(questions, (str, int, float)):
        questions = [questions]
    if questions not in (None, ""):
        if not isinstance(questions, list):
            p.append("questions: a list of questions for the user")
            questions = []
        for i, q in enumerate(questions, 1):
            text = _text(q, f"questions[{i}]", p)
            if text:
                d.questions.append(text)
    return d


def resolve(tx: Tx, d: Draft) -> dict[str, tuple[str, str] | None]:
    """Each entry's parent: ("draft", key), ("record", H-x) or None. What cannot be resolved goes to d.problems."""
    keys = {h["key"] for h in d.hypotheses}
    parents: dict[str, tuple[str, str] | None] = {}
    for h in d.hypotheses:
        ref, where = h.get("parent"), f"hypothesis {h['key']!r}.parent"
        if not ref:
            parents[h["key"]] = None
        elif ref == h["key"]:
            d.problems.append(f"{where}: an entry is not its own parent")
        elif ref in keys:
            parents[h["key"]] = ("draft", ref)
        elif _ID.match(ref) or LABEL_RE.match(ref):
            try:
                hid = labels.resolve(tx, ref).split("@")[0]
                tx.require(hid, "Hypothesis")
                parents[h["key"]] = ("record", hid)
            except RLError as exc:
                d.problems.append(f"{where}: {exc.message}")
        else:
            d.problems.append(f"{where}: {ref!r} is neither another entry's key nor a recorded hypothesis (H-0003 or "
                              "H1.2)")
    for key in parents:  # a cycle among the entries
        seen, cur = {key}, parents.get(key)
        while cur and cur[0] == "draft":
            if cur[1] in seen:
                d.problems.append(f"hypothesis {key!r}.parent: the parents of the entries go round in a circle")
                break
            seen.add(cur[1])
            cur = parents.get(cur[1])
    return parents


def _ordered(d: Draft, parents: dict) -> list[dict]:
    """Parents before their children, otherwise in the draft's order (labels count up the same way)."""
    done: set[str] = set()
    out: list[dict] = []
    while len(out) < len(d.hypotheses):
        for h in d.hypotheses:
            p = parents.get(h["key"])
            if h["key"] not in done and (not p or p[0] == "record" or p[1] in done):
                out.append(h)
                done.add(h["key"])
    return out


def check(store: Store) -> dict:
    """Check the draft's format and the parents it names; records nothing (7.2)."""
    rel = f"{DRAFT_DIR}/{DRAFT_FILE}"
    if not has_draft(store.project):
        raise RLError("NOT_FOUND", f"no draft at {rel}: rl tree prompt says how to write one")
    d = read(store.project)
    if d.parsed:
        with store.read() as tx:
            resolve(tx, d)
    if d.problems:
        raise RLError("INVALID", d.problem_text(rel) + ("\nwarnings:\n" + "\n".join(f"- {w}" for w in d.warnings)
                                                        if d.warnings else ""))
    doubts = sum(1 for h in d.hypotheses if h.get("doubt"))
    return {"draft": rel, "warnings": d.warnings,
            "summary": f"{rel}: no problems. {len(d.hypotheses)} hypotheses ({doubts} with a doubt), "
                       f"{len(d.prep)} shared preparation tasks, {len(d.questions)} questions for the user",
            "next": ["hand back the summary rl tree prompt asks for"]}


# -- recording ------------------------------------------------------------------------------------------------


def _cleanup(project: Project, digest: str, warnings: list[str]) -> None:
    """Delete the draft that was recorded, and only that one: a draft written again meanwhile stays."""
    path = draft_path(project)
    try:
        if path.is_file() and digest_bytes(path.read_bytes()) != digest:
            warnings.append(f"{DRAFT_DIR}/{DRAFT_FILE} changed since it was recorded: it is left for you")
            return
        shutil.rmtree(draft_dir(project))
    except OSError as exc:
        warnings.append(f"the draft could not be deleted ({exc}); delete {DRAFT_DIR}/ yourself: it is recorded")


def record(store: Store, actor: Actor | None, approval: str | None = None) -> dict:
    """Record the approved draft in one transaction (7.2 step 4), then delete it. The exact draft, its questions for
    the user too, is sealed as the route decision's document; its digest names it, so the same draft is recorded
    once, however often (or by how many at once) rl tree record runs on it."""
    fl = Flags(actor.name if actor else "?")
    need(actor, "coordinator", what="recording the hypothesis tree", flags=fl)
    project = store.project
    rel = f"{DRAFT_DIR}/{DRAFT_FILE}"
    if not has_draft(project):
        raise RLError("NOT_FOUND", f"no draft at {rel}: the coordinator's subagent writes it (rl tree prompt)")
    d = read(project)
    if d.problems:
        raise RLError("INVALID", d.problem_text(rel))
    digest = digest_bytes(d.data)
    sealed = documents.prepare(project, draft_path(project), "rationale")
    if sealed.digest != digest:
        raise RLError("CONFLICT", f"{rel} changed while it was read: nothing was recorded; run rl tree record again")
    sealed = replace(sealed, source_path=f"{rel}@{short(digest, 16)}")  # one document per draft text
    words = (approval or "").strip()
    hfl = Flags(actor.name)
    if not words:
        text = "the tree was recorded without the user's approval (rl tree record --approval \"<their words>\")"
        fl.add("unapproved", text)
        hfl.add("unapproved", text)
    made, preps, doubted, competing = [], [], [], []
    with store.write() as tx:
        before = tx.all("Document", path=sealed.source_path)
        if before:  # recorded already (by another rl tree record, or before a crash): its result stands
            dec = before[0]["owner"]
            warnings: list[str] = []
            _cleanup(project, digest, warnings)
            return {"id": dec, "duplicate": True, "warnings": warnings,
                    "summary": f"this draft was recorded already: route decision {dec} (rl show {dec}); nothing "
                               "was recorded again"}
        parents = resolve(tx, d)
        if d.problems:
            raise RLError("INVALID", d.problem_text(rel))
        now = now_utc()
        dec = tx.next_id("Decision")
        tx.create("Decision", {"id": dec, "kind": "route", "state": "final", "rationale": d.why, "task_id": None,
                               "author": actor.name, "final_at": now})
        tx.update(dec, {"rationale_ref": documents.register(tx, sealed, dec)})  # the exact draft, questions too
        ids: dict[str, str] = {}
        for h in _ordered(d, parents):
            p = parents.get(h["key"])
            pid = (ids[p[1]] if p[0] == "draft" else p[1]) if p else None
            label = labels.for_hypothesis(tx, [pid] if pid else [], pid)
            hid = tx.next_id("Hypothesis")
            doubt = h.get("doubt")
            # a competing explanation starts as a candidate, and so does one with a doubt until the user decides
            active = h["kind"] == "main" and not doubt
            tx.create("Hypothesis", {"id": hid, "label": label, "title": h["title"], "statement": h["statement"],
                                     "scope": h.get("scope"), "source": h.get("source"), "purpose": h["purpose"],
                                     "evidence_status": "untested", "disposition": "active" if active else "candidate",
                                     "revision": 1, "cost": h.get("cost"), "kind": h["kind"], "origin": dec,
                                     "flag": {**doubt, "by": actor.name} if doubt else None})
            if pid:
                tx.link("derived_from", hid, pid, primary=True)
            tx.link("decides", dec, hid, revision=1)
            hfl.write(tx, hid)
            tx.event("hypothesis.added", hid, f"{label}: {h['title']} (tree {dec})")
            ids[h["key"]] = hid
            made.append((label, hid, h))
            if doubt:
                doubted.append((hid, doubt["lack"]))
            if h["kind"] == "competing":
                competing.append(hid)
        for item in d.prep:
            tid = tx.next_id("Task")
            # a public preparation task, as rl task create --kind prep makes it for the coordinator: queued until
            # released, then a researcher codes it (3.2)
            tx.create("Task", {"id": tid, "title": item["title"], "kind": "prep", "state": "queued", "revision": 1,
                               "creator": "coordinator", "requirement": item["requirement"],
                               "done_check": item["done_check"], "reason": f"shared preparation of the tree ({dec})"})
            tx.event("task.created", tid, f"prep: {item['title']}")
            preps.append((tid, item["title"]))
        rq = None
        if words:
            rq = tx.next_id("Ask")
            tx.create("Ask", {"id": rq, "kind": "approval", "task_id": None, "decision_id": dec, "state": "answered",
                              "by": actor.name, "stuck": f"approve the hypothesis tree ({len(made)} hypotheses, "
                                                         f"{len(preps)} shared preparation tasks)",
                              "options": "approve | change", "recommend": "approve", "answer": words,
                              "answered_by": "user", "answered_at": now, "forwarded_at": now, "choice": "approve"})
            tx.event("ask.answered", rq, f"by user: {words[:200]}")
        fl.write(tx, dec)
        tx.event("decision", dec, f"route {', '.join(ids.values())}: {d.why}")
        tx.event("tree.recorded", dec, f"{len(made)} hypotheses, {len(preps)} prep tasks"
                                       + (f", approved ({rq})" if rq else ", not approved"))
    warnings = list(d.warnings)
    _cleanup(project, digest, warnings)
    lines = [f"recorded the tree: route decision {dec}" + (f", the user's approval {rq}" if rq else "")]
    for label, hid, h in made:
        state = "active" if h["kind"] == "main" and not h.get("doubt") else "candidate"
        lines.append(f"  {label} {hid} [{state}{', competing' if h['kind'] == 'competing' else ''}"
                     f"{', ' + h['doubt']['lack'] + ' doubt' if h.get('doubt') else ''}] {h['title']}"
                     + (f" · {h['cost']}" if h.get("cost") else ""))
    lines += [f"  prep {tid} queued: {title}" for tid, title in preps]
    if d.questions:
        lines.append(f"  questions for the user, kept in the draft sealed with {dec}: " + " | ".join(d.questions))
    lines.append(f"{DRAFT_DIR}/ is deleted: what it held is in the records (the draft itself is {dec}'s document)")
    nxt = ["make a hypothesis task for each hypothesis to test now: rl task create --kind hypothesis --targets H-x "
           "--priority N --envelope \"<from its cost>\" --reason \"<why now>\" (approving the tree approved testing "
           "them: releasing their tasks needs no further approval)"]
    if preps:
        nxt.append("release the shared preparation tasks: " + ", ".join(f"rl task release {t}" for t, _ in preps))
    nxt.append("usually release one hypothesis task (rl task release T-x): work runs in parallel through the tasks "
               "its scientist opens under it, not through more hypotheses")
    if doubted:
        nxt.append(f"{', '.join(f'{h} ({lack})' for h, lack in doubted)} stay candidates until the user decides on "
                   "the doubt: rl hypothesis keep|drop H-x with their answer")
    if competing:
        nxt.append(f"competing explanations {', '.join(competing)} are candidates: schedule them when the test of "
                   "the main hypothesis calls for them")
    if not words:
        nxt.append("the user has not approved it (flagged unapproved): show them the tree (rl view) before releasing")
    return {"id": dec, "approval": rq, "hypotheses": [hid for _, hid, _ in made], "prep": [t for t, _ in preps],
            "summary": "\n".join(lines), "warnings": warnings, "next": nxt, "flags": fl.items}


# -- the prompt -----------------------------------------------------------------------------------------------


RULES = """\
## What is a hypothesis

A hypothesis is a judgement about the research question: its outcome is not known in advance, and its answer
changes the route of the research or what the work can claim. A competing explanation of the result the main
hypothesis expects is a hypothesis too (kind: competing): it can be tested side by side with the main one.

Everything else that has to be done to test a hypothesis is work under it. The scientist who gets the hypothesis
plans that work and opens it as tasks; none of it goes into the tree:

- Premises and engineering (locking the data, porting a baseline, wiring the method to new data, memory,
  accounting): preparation tasks a researcher codes, each with a done check. Write the ones several hypotheses
  share under `prep`; leave the rest to the hypothesis's scientist.
- Settings that need a run to fix (budget levels, gate thresholds, other hyperparameters): calibration runs on a
  development set under the hypothesis; their results freeze the test.
- Parts of the decision rule (beating each rival separately, a margin, a significance test): the decision rule
  inside the statement.
- Mechanism checks (which step the gain comes from): arms and analyses of the main experiment, reported in the
  verdict's interpretation. If one becomes the question after the verdict, it becomes a new hypothesis then.
- Exploratory variants reported apart from the main decision (another gate, say): one more arm of the main
  experiment.
- Going on to the next scale: a route decision after the verdict.
- Validity checks (was the baseline weakened by the port; recompute it by the paper's protocol): runs under the
  hypothesis whose evidence bounds the verdict's scope.
- "The implementation is correct" is not a hypothesis.

The tree grows with the results: a new hypothesis comes when a verdict raises a new question. Do not turn the
document's sections into hypotheses ahead of time; most sections of a plan are steps of one test. A plan usually
gives one main hypothesis, sometimes a few independent ones, and perhaps a competing explanation or two. Example:
a plan claiming "method M beats baselines A and B under the shared conditions C", with sections on data,
baselines, memory, budget levels, a gate and ablations, has one hypothesis, "M beats both A and B under C" with the
plan's decision rule; the rest are tasks under it. Splitting it into sixteen hypotheses would hand each piece to a
different scientist and leave nobody owning the test of the claim.

## Each hypothesis

- statement: the testable claim with its decision rule: what result counts as supported and what as challenged.
  Take the rule the document gives; if it gives none, do not invent thresholds: ask in `questions`.
- scope: where it is claimed to hold (data, model sizes, conditions).
- purpose: one sentence on what testing it answers (the research page shows it).
- source: which document and which section it comes from, as "<document path> § <section>". The scientist who
  tests it reads the document from there.
- parent: empty for most; to hang it under a hypothesis, a recorded one (its id H-0003 or label H1.2) or another
  entry of this draft (its key).
- cost: the expected compute use of its whole test, in the units of docs/constraints.md ("about 40 GPU·h"). The
  coordinator compares it with the budget.
- kind: main, or competing (an alternative explanation of the same result).

## Doubts

Check two things for each hypothesis. Novelty: has someone already answered it? Feasibility: can it be tested
with the data, compute and time docs/constraints.md allows? When one falls short, do not drop or rewrite the
hypothesis: give it a `doubt` with what is lacking, the basis, and the options (drop, narrow, test another way, go
ahead); the user decides. A search hit is not a check: say a hypothesis was answered before only after reading
the relevant part of the paper, and name the paper and where in it.

What the documents leave unclear, do not guess: put it in `questions`.
"""

FORMAT = """\
## The draft

Write {draft} (make the directory if needed) in this format; quote or fold long texts (`>-`):

```yaml
why: >-
  Why these hypotheses: what the documents claim and why the tree has this shape. It becomes the route
  decision recorded with the tree.
hypotheses:
  - key: main                    # a short name, unique in the draft; other entries' parent may name it
    kind: main                   # main | competing
    title: M beats A and B under C
    statement: >-
      On C, M's accuracy exceeds both A's and B's by at least 1 point on the test split (mean of 3 seeds);
      supported if it does against both, challenged if it falls short of either.
    scope: the test split of dataset D, 1.7B and 8B models
    purpose: whether M is worth adopting over A and B under C
    source: docs/plan.md § 2 Method          # <document path> § <section>
    parent:                      # empty; or H-0003 / H1.2 (recorded), or another entry's key
    cost: about 40 GPU·h         # the whole test, in the units of docs/constraints.md
  - key: shortcut
    kind: competing
    title: The gain comes from longer context, not from M
    statement: ...
    purpose: ...
    source: docs/plan.md § 4 Risks
    cost: about 6 GPU·h
    doubt:                       # only when novelty or feasibility falls short
      lack: novelty              # novelty | feasibility
      basis: <authors, year> (<arXiv id or DOI>), <section>: <what it already shows>
      options: drop | narrow to ... | test another way: ... | go ahead
prep:                            # preparation several hypotheses share (the rest is their scientists')
  - title: Lock the splits of D
    requirement: what to produce, where
    done_check: a command that exits 0 when it is done
questions:                       # what only the user can settle
  - ...
```

Run `rl tree check` after writing; fix what it reports until it reports no problems (its warnings are advice).
"""

HAND_BACK = """\
## Hand back

When `rl tree check` reports no problems, end with this summary and nothing else; the coordinator's context gets
only this, so leave the documents' text out:
- one line per hypothesis: key, kind, title, cost, and its parent if it has one;
- the doubts, one line each: the hypothesis, novelty or feasibility, the options;
- the shared preparation work, one line each;
- the questions for the user.
"""


def _recorded(tx: Tx) -> list[str]:
    shape = tree_lines(tx)
    if not shape:
        return ["No hypotheses are recorded yet."]
    out = ["Recorded hypotheses (label, id, state, title), then their statements. New hypotheses may hang under "
           "them (parent: the id or label); do not repeat one.", ""] + shape + [""]
    for h in labels.ordered(tx.all("Hypothesis")):
        out.append(f"- {h['label']} ({h['id']}): {h['statement']}" + (f" · scope: {h['scope']}" if h.get("scope")
                                                                       else ""))
    return out


def prompt(store: Store, docs: list[str] = ()) -> dict:
    """The instructions for the coordinator's tree subagent (7.2): only the subagent reads them."""
    project = store.project
    warnings = []
    shown = []
    for given in docs or [PLAN_PATH]:
        path = project.src(given) if docs else project.plan_path
        if not path.is_file():
            warnings.append(f"{given} does not exist" + ("" if docs else
                                                         ": name the documents with rl tree prompt --from <doc> .."))
        shown.append(project.rel(path))
    with store.read() as tx:
        recorded = _recorded(tx)
    draft = f"{DRAFT_DIR}/{DRAFT_FILE}"
    exists = has_draft(project)
    task = (f"A draft already exists: {draft}. Change it as the user's words in the message that started you say, "
            "and leave everything they did not mention exactly as it is. Then check it and hand back the summary of "
            "the whole draft, starting with one line on what you changed." if exists else
            f"Read the documents below, write the draft {draft}, check it with `rl tree check`, and hand back a "
            "short summary.")
    constraints = project.rel(project.constraints_path)
    parts = [
        "# Draft the hypothesis tree", "",
        f"You work for the coordinator of the research project at {project.root}. {task}",
        "",
        "The draft is not a record: the user reads it whole on the research page, may ask for changes as often as "
        "they like, and the coordinator records it (`rl tree record`) once they approve. Write only the draft: do not "
        "run commands that write records (`rl hypothesis`, `rl decide`, `rl task`, `rl paper add`, `rl tree "
        "record`), write no code and run no experiments. Searching the literature to judge novelty is part of the "
        "work.",
        "",
        "## Documents to read",
        "",
        *[f"- {s} (read it whole)" for s in shown],
        f"- {constraints}: the budget and other limits, for cost and feasibility"
        + ("" if project.constraints_path.is_file() else " (it does not exist yet)"),
        "",
        "## The tree as recorded",
        "",
        *recorded,
        "",
        RULES, FORMAT.format(draft=draft), HAND_BACK]
    return {"draft": draft, "draft_exists": exists, "documents": shown, "warnings": warnings,
            "summary": "\n".join(parts).rstrip()}
