"""The research explanation page docs/research.html: the facts from the records, the words from the coordinator.

The facts are read from the records in one read transaction each time the page is rendered, and none of them is
written by hand: labels, titles, states, relations, contract metrics, the arm tables, costs, decisions and every
count. The words are in research/notes/explanation.yaml, written by the coordinator or a writer it asks; rl only
reads that file:

    lines:              # research lines: a name, one sentence, the roots of their subtrees
      - key: m
        name: M line - memory read, repair and V3
        desc: At answer time the backbone cannot see the facts.
        roots: [H3]
    summary:            # judgments only: the page does the counting
      text: ...
    nodes:              # a hypothesis or an experiment, by its label or its id
      H3:
        title: ...
        purpose: ...
        did: ...
        result: ...

Once the words of a node have been checked against its facts, `rl view --stamp H3,EH3.3/3` records a digest of
those facts (facts()) in research/notes/explanation.basis.yaml, rl's own file, so the words file is never rewritten;
when the facts change later, the page marks the node "may be out of date". The summary is checked against the stage
decisions instead: a stage decision after the one it was stamped with makes it out of date. A node without words
still shows its facts, marked "explanation to write"; top-level hypotheses in no research line are listed under
Unclassified. However the records change, rendering does not fail on them; what in the words file cannot be used is
reported, and the rest is shown.

The page is one self-contained file from a fixed template: no external resources, no render time and no
data_version (heartbeats and messages change it), so its bytes change only when the facts or the words do, and an
unchanged page is not written again. The commands that change what it shows render it again when it exists (cli.py).
"""

from __future__ import annotations

import fcntl
import html
import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml

from . import labels
from .project import Project
from .research import contract_front
from .schema import ENUMS
from .store import LOCK_TIMEOUT_S, Store, Tx, _sort_key
from .util import RLError, atomic_write, digest_json
from .view import arm_rows, arm_table, conclusion_checks, costs, cut_cell, decision_line, evidence_line

PAGE = "research.html"  # in docs/
WORDS = "notes/explanation.yaml"  # in research/: the words, read only
BASIS = "notes/explanation.basis.yaml"  # in research/: written by rl view --stamp
NODE_FIELDS = ("title", "purpose", "did", "result")
TEXT_FIELDS = (("purpose", "Purpose"), ("did", "What was done"), ("result", "Result"))
TO_WRITE, STALE, CURRENT = "explanation to write", "may be out of date", "current"
EXCERPT = 240  # characters of a record's text in rl view --todo
BASIS_NOTE = f": rl view --stamp records it in research/{BASIS}"
BASIS_HEADER = ("# Written by rl view --stamp: a digest of the facts each explanation in explanation.yaml was checked\n"
                "# against (summary: the latest stage decision then). rl view marks the ones whose facts changed since.\n")


def page_path(project: Project) -> Path:
    return project.docs_root / PAGE


@contextmanager
def _locked(project: Project) -> Iterator[None]:
    """One rendering or stamping at a time, each reading the records after the one before it wrote: the page that
    stays is the one rendered last, from the latest records."""
    project.state_dir.mkdir(parents=True, exist_ok=True)
    path = project.state_dir / "explanation.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RLError("LOCKED", f"another rl view has held {path} for {LOCK_TIMEOUT_S:.0f}s") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing releases the lock


# -- the words and their basis ------------------------------------------------------------------------------


def _load(project: Project, rel: str, otherwise: str) -> tuple[dict, list[str]]:
    path = project.research_root / rel
    if not path.exists():
        return {}, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        mark = getattr(exc, "problem_mark", None)
        why = (getattr(exc, "problem", None) or str(exc).strip().splitlines()[0]) + \
            (f" at line {mark.line + 1}" if mark else "")
        return {}, [f"research/{rel} does not parse ({why}): {otherwise}"]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, [f"research/{rel} is not a mapping: {otherwise}"]
    return data, []


def _text(value, where: str, problems: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        problems.append(f"{where} is not text")
        return None
    return str(value).strip() or None


def _words(tx: Tx, project: Project) -> dict:
    """The words of explanation.yaml, every node and root named by its internal id, and what could not be used."""
    data, problems = _load(project, WORDS, "the page shows the facts only")

    def record(key, where: str) -> dict | None:
        try:
            return tx.require(labels.resolve(tx, str(key).strip()), "Hypothesis", "Experiment")
        except RLError as exc:
            problems.append(f"{where}: {exc.message}")
            return None

    nodes: dict[str, dict] = {}
    raw = data.get("nodes") or {}
    if not isinstance(raw, dict):
        problems.append("nodes: is a mapping from a label or an id to title, purpose, did and result")
        raw = {}
    for key, value in raw.items():
        rec = record(key, f"nodes: {key}")
        if rec is None:
            continue
        if not isinstance(value, dict):
            problems.append(f"nodes: {key} is not a mapping of {', '.join(NODE_FIELDS)}")
            continue
        for extra in sorted(str(k) for k in value if k not in NODE_FIELDS):
            problems.append(f"nodes: {key}: {extra} is not used"
                            + (BASIS_NOTE if extra == "basis" else f" (the fields are {', '.join(NODE_FIELDS)})"))
        if rec["id"] in nodes:
            problems.append(f"nodes: {key} is {labels.name(rec)} again: the later entry is used")
        nodes[rec["id"]] = {k: text for k in NODE_FIELDS
                            if (text := _text(value.get(k), f"nodes: {key}: {k}", problems))}
    lines, shown = [], set()
    raw = data.get("lines") or []
    if not isinstance(raw, list):
        problems.append("lines: is a list of key, name, desc and roots")
        raw = []
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            problems.append(f"lines: entry {n} is not a mapping of key, name, desc and roots")
            continue
        key = str(item.get("key") or n)
        roots = item.get("roots") or []
        ids = []
        for root in roots if isinstance(roots, list) else [roots]:
            rec = record(root, f"lines: {key}: root {root}")
            if rec is None or rec["id"] in ids:
                continue
            if rec["_label"] == "Experiment":
                problems.append(f"lines: {key}: root {root} is an experiment; an experiment is shown under the "
                                "hypothesis it is anchored at")
                continue
            if rec["id"] in shown:
                problems.append(f"lines: {key}: root {root} is also a root of an earlier line, which shows it")
            shown.add(rec["id"])
            ids.append(rec["id"])
        lines.append({"key": key, "name": _text(item.get("name"), f"lines: {key}: name", problems) or key,
                      "desc": _text(item.get("desc"), f"lines: {key}: desc", problems), "roots": ids})
    summary = data.get("summary")
    if isinstance(summary, dict):
        if "basis" in summary:
            problems.append("summary: basis is not used" + BASIS_NOTE)
        summary = summary.get("text")
    return {"lines": lines, "summary": _text(summary, "summary", problems), "nodes": nodes, "problems": problems}


def _basis(project: Project) -> tuple[dict, list[str]]:
    data, problems = _load(project, BASIS, "every written explanation counts as possibly out of date until "
                                           "rl view --stamp writes it again")
    nodes = data.get("nodes") if isinstance(data.get("nodes"), dict) else {}
    return {"summary": data.get("summary"), "summary_stamped": "summary" in data,
            "nodes": {str(k): str(v) for k, v in nodes.items()}}, problems


def facts(tx: Tx, rec: dict, attempts: list[dict] | None = None) -> dict:
    """What an explanation of a hypothesis or an experiment rests on; its digest is the basis rl view --stamp
    records. Messages, heartbeats, observations that change no state and data_version leave it as it is.
    attempts: an experiment's attempts, when the caller has them."""
    rid = rec["id"]
    decisions = [e["id"] for e in tx.inc(rid, "decides")]
    if rec["_label"] == "Hypothesis":
        evidence = [[e["id"], e["rel"], tx.require(e["id"])["validity"]] for e in tx.inc(rid)
                    if e["rel"] in ("supports", "challenges")]
        return {"statement": rec["statement"], "revision": rec["revision"], "evidence_status": rec["evidence_status"],
                "disposition": rec["disposition"], "evidence": evidence, "decisions": decisions}
    if attempts is None:
        attempts = [a for run in tx.all("Run", experiment_id=rid) for a in tx.all("Attempt", run_id=run["id"])]
    results = sorted(([r["id"], r["status"]] for a in attempts for r in tx.all("Result", attempt_id=a["id"])),
                     key=lambda r: _sort_key(r[0]))
    return {"revision": rec["revision"], "stage": rec.get("stage"),
            "attempts": dict(sorted(Counter(f"{a['execution_state']}/{a['outcome']}" for a in attempts).items())),
            "results": results, "decisions": decisions}


def _check(tx: Tx, project: Project) -> dict:
    """The words, and the state of each explanation: to write, may be out of date, or current."""
    words = _words(tx, project)
    basis, problems = _basis(project)
    nodes = {r["id"]: r for r in tx.all("Hypothesis") + tx.all("Experiment")}
    runs = {rid: tx.all("Run", experiment_id=rid) for rid, r in nodes.items() if r["_label"] == "Experiment"}
    attempts = {rid: [a for run in rr for a in tx.all("Attempt", run_id=run["id"])] for rid, rr in runs.items()}
    state, digests, unstamped = {}, {}, set()
    for rid, rec in nodes.items():
        digests[rid] = digest_json(facts(tx, rec, attempts.get(rid)))
        if not any(words["nodes"].get(rid, {}).get(k) for k, _ in TEXT_FIELDS):
            state[rid] = TO_WRITE
        elif basis["nodes"].get(rid) != digests[rid]:
            state[rid] = STALE
            if rid not in basis["nodes"]:
                unstamped.add(rid)
        else:
            state[rid] = CURRENT
    decisions = tx.all("Decision")
    stages = [d for d in decisions if d["kind"] == "stage"]
    latest = stages[-1]["id"] if stages else None
    if not words["summary"]:
        summary = TO_WRITE
    elif not basis["summary_stamped"] or (latest and basis["summary"] != latest):
        summary = STALE
    else:
        summary = CURRENT
    return {"words": words, "basis": basis, "nodes": nodes, "state": state, "digests": digests,
            "unstamped": unstamped, "summary": summary, "latest_stage": latest, "decisions": decisions,
            "summary_unstamped": bool(words["summary"]) and not basis["summary_stamped"],
            "runs": runs, "attempts": attempts,
            "known": {rid: r["label"] for rid, r in nodes.items() if r.get("label")},
            "problems": words["problems"] + problems}


def _counts(check: dict) -> tuple[int, int]:
    states = [*check["state"].values(), check["summary"]]
    return states.count(TO_WRITE), states.count(STALE)


def attention(tx: Tx, project: Project) -> list[str]:
    """Once the page exists, one item for rl status, rl resume and the session start hook: how many explanations
    are to write or may be out of date. They never block a decision."""
    if not page_path(project).exists():
        return []
    check = _check(tx, project)
    missing, stale = _counts(check)
    parts = ([f"{missing} to write"] if missing else []) + ([f"{stale} may be out of date"] if stale else []) \
        + ([f"{len(check['problems'])} problem(s) in research/{WORDS} (rl view lists them)"] if check["problems"]
           else [])
    return [f"explanations on docs/{PAGE}: {', '.join(parts)}: rl view --todo"] if parts else []


# -- commands -------------------------------------------------------------------------------------------------


def render(store: Store) -> dict:
    """rl view: render docs/research.html from the records and the words; an unchanged page is not written."""
    project = store.project
    with _locked(project):
        with store.read() as tx:
            check = _check(tx, project)
            page, shape = _page(tx, store, check)
        path = page_path(project)
        data = page.encode("utf-8")
        changed = not path.is_file() or path.read_bytes() != data
        if changed:
            atomic_write(path, data)
    missing, stale = _counts(check)
    current = len(check["nodes"]) + 1 - missing - stale
    lines = [f"{'wrote' if changed else 'unchanged:'} docs/{PAGE}: {shape}",
             f"explanations (the summary and each hypothesis and experiment): {current} current, {missing} to write, "
             f"{stale} may be out of date"
             + (f"; rl view --todo lists them, the words go in research/{WORDS}, then rl view --stamp <labels>"
                if missing or stale else "")]
    return {"path": str(path), "changed": changed, "to_write": missing, "out_of_date": stale,
            "summary": "\n".join(lines), "warnings": check["problems"]}


def refresh(store: Store) -> dict | None:
    """After a command that changed the records: render the page again if there is one."""
    return render(store) if page_path(store.project).is_file() else None


def stamp(store: Store, refs: list[str]) -> dict:
    """rl view --stamp: the explanations of refs (hypotheses and experiments by label or id, and `summary`) were
    checked against the current facts; record their basis, then render the page."""
    refs = [r.strip() for r in refs if r.strip()]
    if not refs:
        raise RLError("INVALID", "--stamp names the hypotheses and experiments (labels or ids) and/or summary whose "
                                 "explanations were checked against their current facts")
    project = store.project
    with _locked(project):
        with store.read() as tx:
            check = _check(tx, project)
            done, summary = {}, False
            for ref in refs:
                if ref.lower() == "summary":
                    if not check["words"]["summary"]:
                        raise RLError("INVALID", f"the summary has no text in research/{WORDS}: write it, then "
                                                 "stamp it")
                    summary = True
                    continue
                rec = tx.require(labels.resolve(tx, ref), "Hypothesis", "Experiment")
                if check["state"][rec["id"]] == TO_WRITE:
                    raise RLError("INVALID", f"{labels.name(rec)} has no explanation in research/{WORDS}: write "
                                             "its purpose, did or result, then stamp it")
                done[rec["id"]] = labels.name(rec)
        nodes = {**check["basis"]["nodes"], **{rid: check["digests"][rid] for rid in done}}
        data = {"summary": check["latest_stage"]} if summary else \
            {"summary": check["basis"]["summary"]} if check["basis"]["summary_stamped"] else {}
        data["nodes"] = {rid: nodes[rid] for rid in sorted(nodes, key=_sort_key)}
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=200)
        atomic_write(project.research_root / BASIS, (BASIS_HEADER + text).encode("utf-8"))
    out = render(store)
    names = [*done.values(), *(["summary"] if summary else [])]
    return {**out, "stamped": names, "summary": f"stamped {', '.join(names)} in research/{BASIS}\n{out['summary']}"}


def _excerpt(text) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= EXCERPT else text[:EXCERPT - 1] + "…"


def todo(store: Store) -> dict:
    """rl view --todo: the explanations to write or revise, each with its current facts, for whoever writes them."""
    with store.read() as tx:
        check = _check(tx, store.project)
        known = check["known"]
        lines = []
        if check["summary"] != CURRENT:
            latest = tx.require(check["latest_stage"]) if check["latest_stage"] else None
            stage = (f"latest stage decision {latest['id']} {latest['action']}: "
                     f"{labels.relabel(_excerpt(latest['rationale']), known)}" if latest else "no stage decision yet")
            why = "to write" if check["summary"] == TO_WRITE else "written but not stamped" \
                if check["summary_unstamped"] else "may be out of date: a stage decision came after the one it was " \
                                                   "stamped with"
            lines.append(f"summary: {why}; {stage}")
        for rec in labels.ordered(list(check["nodes"].values())):
            state = check["state"][rec["id"]]
            if state != CURRENT:
                lines += _todo_item(tx, store, rec, state, rec["id"] in check["unstamped"], known)
    missing, stale = _counts(check)
    if not lines:
        return {"to_write": 0, "out_of_date": 0, "summary": f"every explanation on docs/{PAGE} is written and current"
                + "".join(f"\nwarning: {p}" for p in check["problems"])}
    head = (f"{missing} explanation(s) to write and {stale} that may be out of date. They go in research/{WORDS}: "
            "nodes: <label>: title, purpose, did, result; summary: text; lines: key, name, desc, roots. The page "
            "shows the numbers from the records, so the words judge and explain and do not count. Then rl view "
            "--stamp <labels>[,summary], which renders the page too. Full records: rl show <label>.")
    return {"to_write": missing, "out_of_date": stale, "warnings": check["problems"],
            "summary": "\n".join([head, "", *lines])}


def _todo_item(tx: Tx, store: Store, rec: dict, state: str, unstamped: bool, known: dict[str, str]) -> list[str]:
    """The node's heading (label and id) and its facts, in which ids of hypotheses and experiments read as labels."""
    rid = rec["id"]
    why = state if state == TO_WRITE else ("written but not stamped" if unstamped
                                           else "may be out of date: its facts changed since it was stamped")
    out = []
    if rec["_label"] == "Hypothesis":
        out.append(f"  [{rec['disposition']}/{rec['evidence_status']} @{rec['revision']}] {_excerpt(rec['statement'])}")
        sources = ", ".join(e["id"] for e in tx.out(rid, "derived_from"))
        tests = ", ".join(e["id"] for e in tx.inc(rid, "tests"))
        related = "; ".join(x for x in (sources and f"derived from {sources}", tests and f"tested by {tests}") if x)
        out += [f"  {related}"] if related else []
        evidence = [(e["rel"], tx.require(e["id"])) for e in tx.inc(rid) if e["rel"] in ("supports", "challenges")]
    else:
        contract = f"contract @{rec['revision']}" if rec.get("contract_ref") else "draft contract"
        out.append(f"  [{rec.get('stage') or '-'}, {contract}] {_excerpt(rec['question'])}")
        out.append("  tests " + (", ".join(f"{e['id']}@{e.get('revision')}" for e in tx.out(rid, "tests")) or "-"))
        keys, rows = arm_rows(tx, store, rec)
        for r in rows:
            values = ", ".join(f"{k} {c}" for k, c in zip(keys, r["cells"]) if c != "-") or "no numbers"
            out.append(f"  {r['arm']} {r['contract']} seeds {r['seeds']} ({r['attempts']}): {values}"
                       + (f" [{r['notes']}]" if r["notes"] else "") + (f"; {r['condition']}" if r["condition"] else ""))
        evidence = [("about", tx.require(e["id"])) for e in tx.inc(rid, "about")]
    out += [f"  evidence {ev['id']} {rel} [{ev['evidence_level']}/{ev['validity']}]: {_excerpt(ev['summary'])}"
            for rel, ev in evidence]
    out += [f"  decision {d['id']} {d['kind']}:{d['action']}: {_excerpt(d['rationale'])}"
            for d in (tx.require(e["id"]) for e in tx.inc(rid, "decides"))]
    return [f"{labels.name(rec)} ({rid}) {why}: {labels.relabel(rec['title'], known)}",
            *(labels.relabel(line, known) for line in out)]


# -- the page -------------------------------------------------------------------------------------------------


CSS = """
:root{--bg:#f8f7f4;--surface:#fff;--surface-2:#f1efea;--text:#1d1f23;--dim:#5b6067;--border:#e3e0d8;
--accent:#0b6680;--accent-bg:#e3eff3;--ok:#2c7548;--ok-bg:#e5f2e9;--risk:#b42318;--risk-bg:#fbe9e6;
--warn:#8a5300;--warn-bg:#fbf0dc;--neutral-bg:#efeeea;color-scheme:light;
--sans:system-ui,-apple-system,"Segoe UI","Noto Sans CJK SC","Source Han Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme:dark){:root{--bg:#131518;--surface:#1b1e22;--surface-2:#24282d;--text:#e7e5e0;
--dim:#a4a9b0;--border:#30343a;--accent:#62bad4;--accent-bg:#15313a;--ok:#64c690;--ok-bg:#16301f;--risk:#f28b7f;
--risk-bg:#3b1d1a;--warn:#e5ab42;--warn-bg:#382a12;--neutral-bg:#262a30;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.65 var(--sans)}
.wrap{max-width:1080px;margin:0 auto;padding:28px 16px 56px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code{font:12.5px var(--mono)}
.kicker{font:12px/1.4 var(--mono);color:var(--dim);margin:0 0 6px}
h1{font-size:28px;line-height:1.25;margin:0 0 12px}
h2{font-size:19px;line-height:1.3;margin:0 0 2px}
.lede p{margin:0 0 8px;max-width:72ch;font-size:16px}
ul.counts{display:flex;flex-wrap:wrap;gap:4px 20px;list-style:none;margin:14px 0 6px;padding:0;font-size:13.5px;color:var(--dim)}
ul.counts b{color:var(--text);font-variant-numeric:tabular-nums}
.meta{font-size:13.5px;color:var(--dim);margin:4px 0;max-width:96ch}
nav.toc{margin:20px 0 28px;padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:14px}
nav.toc ol{margin:4px 0;padding-left:22px}
.count{font-size:12.5px;color:var(--dim);font-variant-numeric:tabular-nums}
section.line{margin:0 0 32px}
.desc{margin:0;color:var(--dim);max-width:72ch}
ul.tree,ul.kids{list-style:none;margin:0;padding:0}
ul.tree{margin-top:8px}
ul.kids{margin:0 0 0 14px;padding-left:14px;border-left:1px solid var(--border)}
li.node{margin:8px 0}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px}
li.node:target>.card{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-bg)}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.nid{font:600 13px/1.6 var(--mono)}
.e>.card .nid{font-weight:500}
.ttl{font-weight:600;flex:1 1 16em;min-width:0;overflow-wrap:anywhere}
.e>.card .ttl{font-weight:500}
.b,.mark{display:inline-block;padding:0 6px;border:1px solid var(--border);border-radius:4px;font-size:12px;line-height:20px;white-space:nowrap;color:var(--dim)}
.ev-supported{border-color:var(--ok);background:var(--ok-bg);color:var(--ok)}
.ev-challenged{border-color:var(--risk);background:var(--risk-bg);color:var(--risk)}
.ev-inconclusive{border-color:var(--warn);background:var(--warn-bg);color:var(--warn)}
.ev-untested{background:var(--neutral-bg)}
.disp-candidate{border-style:dashed}
.disp-retired{text-decoration:line-through}
.b.warn,.mark.todo{border-color:var(--warn);color:var(--warn)}
.mark.todo{border-style:dashed}
.mark.stale{border-color:var(--warn);background:var(--warn-bg);color:var(--warn)}
p.mark{display:block;width:fit-content;max-width:100%;white-space:normal;margin:6px 0}
.w{margin:6px 0 0;max-width:76ch}
.w p{margin:0 0 4px}
.wl{display:block;font-size:11.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--accent);font-weight:600}
details.facts{margin-top:6px;font-size:13.5px}
details.facts>summary{cursor:pointer;color:var(--dim);font-size:12.5px}
dl.rel{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 14px;margin:8px 0}
dl.rel dt{color:var(--dim)}
dl.rel dd{margin:0;overflow-wrap:anywhere}
dl.rel ul{margin:0;padding-left:18px}
.scroll{overflow-x:auto;max-width:100%}
table.arms{border-collapse:collapse;font-size:12.5px;margin:8px 0 4px}
table.arms th,table.arms td{border:1px solid var(--border);padding:2px 8px;text-align:left;white-space:nowrap}
table.arms th{background:var(--surface-2);font-weight:600}
.note{color:var(--dim);font-size:12.5px;margin:2px 0}
li.stub{font-size:14px;color:var(--dim)}
footer{margin-top:40px;padding-top:12px;border-top:1px solid var(--border);font-size:12.5px;color:var(--dim)}
@media (max-width:600px){dl.rel{grid-template-columns:minmax(0,1fr)}dl.rel dt{margin-top:4px}ul.kids{margin-left:6px;padding-left:10px}}
"""


def _slugs(lines: list[dict]) -> list[str]:
    out = []
    for n, line in enumerate(lines, 1):
        slug = "line-" + (re.sub(r"[^A-Za-z0-9_-]+", "-", line["key"]).strip("-") or str(n))
        out.append(slug if slug not in out else f"{slug}-{n}")
    return out


def _page(tx: Tx, store: Store, check: dict) -> tuple[str, str]:
    """The page, and its shape in one line for the command's summary."""
    known, words, state = check["known"], check["words"], check["state"]
    nodes = list(check["nodes"].values())
    hyps = {h["id"]: h for h in labels.ordered([r for r in nodes if r["_label"] == "Hypothesis"])}
    exps = {e["id"]: e for e in labels.ordered([r for r in nodes if r["_label"] == "Experiment"])}
    decisions = check["decisions"]
    checks = conclusion_checks(tx, decisions)
    by_label = {h["label"]: hid for hid, h in hyps.items() if h.get("label")}
    kids: dict[str | None, list[str]] = {}  # a label -> the hypotheses right under it; None: the top level
    for hid, h in hyps.items():
        parent = (h.get("label") or "").rpartition(".")[0]
        kids.setdefault(parent if parent in by_label else None, []).append(hid)
    anchored: dict[str, list[str]] = {}  # a hypothesis -> the experiments anchored at it
    loose = []  # experiments without a label (an older one that tests nothing)
    for eid, e in exps.items():
        anchor = by_label.get(e["label"][1:].partition("/")[0]) if e.get("label") else None
        if anchor:
            anchored.setdefault(anchor, []).append(eid)
        else:
            loose.append(eid)
    owner: dict[str, int] = {}  # a root of a research line -> the line that shows it (the first)
    for n, line in enumerate(words["lines"]):
        for root in line["roots"]:
            owner.setdefault(root, n)
    slugs = _slugs(words["lines"])

    def t(text) -> str:  # text of a record or of the words: escaped, ids of hypotheses and experiments linked
        return labels.relabel(html.escape(str(text)), known, link=True)

    def paras(text: str) -> str:
        return "".join("<p>" + t(p.strip()).replace("\n", "<br>") + "</p>"
                       for p in re.split(r"\n\s*\n", text) if p.strip())

    def ref(rid: str, rev=None) -> str:
        if rid in hyps or rid in exps:
            return f'<a href="#{rid}">{html.escape(known.get(rid, rid))}{f"@{rev}" if rev else ""}</a>'
        rec = tx.get(rid) or {}
        what = rec.get("title") or rec.get("question") or rec.get("summary") or rec.get("rationale")
        return html.escape(rid) + (f" {t(_excerpt(what))}" if what else "")

    def badge(text: str, cls: str = "") -> str:
        return f'<span class="{" ".join(["b", cls]).strip()}">{html.escape(text)}</span>'

    def head(rec: dict, badges: str) -> str:
        rid = rec["id"]
        title = words["nodes"].get(rid, {}).get("title") or rec["title"]
        mark = {TO_WRITE: f'<span class="mark todo">{TO_WRITE}</span>',
                STALE: f'<span class="mark stale">{STALE}</span>'}.get(state[rid], "")
        return (f'<div class="head"><a class="nid" href="#{rid}">{html.escape(labels.name(rec))}</a>'
                f'<span class="ttl">{html.escape(title)}</span>{badges}{mark}</div>')

    def said(rid: str) -> str:
        w = words["nodes"].get(rid, {})
        return "".join(f'<div class="w"><span class="wl">{name}</span>{paras(w[k])}</div>'
                       for k, name in TEXT_FIELDS if w.get(k))

    def dl(rows: list[tuple[str, str]]) -> str:
        return '<dl class="rel">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows if v) + "</dl>"

    def bullets(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>" if items else ""

    def common(rec: dict, out: list[dict], inc: list[dict]) -> tuple[list, list]:
        rid, title = rec["id"], words["nodes"].get(rec["id"], {}).get("title")
        first = [("id", html.escape(rid) + (f" · recorded title: {html.escape(rec['title'])}"
                                             if title and title != rec["title"] else ""))]
        decided = [tx.require(e["id"]) for e in inc if e["rel"] == "decides"]
        last = [("decisions", bullets([t(decision_line(d, checks.get(d["id"]))) for d in decided]))]
        return first, last

    lines_of: dict[str, str] = {}  # an evidence record, as one line (it can be shown under several nodes)

    def evidence(ev_id: str) -> str:
        if ev_id not in lines_of:
            lines_of[ev_id] = t(evidence_line(tx, tx.require(ev_id)))
        return lines_of[ev_id]

    def related(out: list[dict], rel: str) -> str:
        return ", ".join(ref(e["id"], e.get("revision")) for e in out if e["rel"] == rel)

    def facts_h(h: dict) -> str:
        out, inc = tx.out(h["id"]), tx.inc(h["id"])
        first, last = common(h, out, inc)
        tests = [ref(e["id"]) + (f" (tests @{e['revision']})" if e.get("revision") not in (None, h["revision"]) else "")
                 for e in inc if e["rel"] == "tests"]
        found = [evidence(e["id"]) for e in inc if e["rel"] in ("supports", "challenges")]
        return dl([*first, ("statement", t(h["statement"])), ("scope", t(h["scope"]) if h.get("scope") else ""),
                   ("re-check", t(h.get("recheck_reason") or "needed") if h.get("needs_recheck") else ""),
                   *((name.replace("_", " "), related(out, name))
                     for name in ("derived_from", "combines", "motivated_by", "nearest_work", "cites")),
                   ("tested by", ", ".join(tests)), ("evidence", bullets(found)), *last])

    def arms(e: dict) -> str:
        header, rows = arm_table(tx, store, e)
        if not rows:
            return '<p class="note">No runs registered.</p>'

        def cell(value) -> str:
            full = str(value)
            short = cut_cell(full)
            return f'<td title="{html.escape(full)}">{html.escape(short)}</td>' if short != full \
                else f"<td>{html.escape(full)}</td>"
        table = ('<div class="scroll"><table class="arms"><thead><tr>'
                 + "".join(f"<th>{html.escape(h)}</th>" for h in header) + "</tr></thead><tbody>"
                 + "".join("<tr>" + "".join(cell(c) for c in row) + "</tr>" for row in rows) + "</tbody></table></div>")
        spent = "; ".join(costs(tx, e["id"])) or "no attempt ran"
        return (table + '<p class="note">Means are descriptive, one result per run; rows of different conditions '
                f"are not averaged together. Per-attempt records: <code>rl run show {html.escape(labels.name(e))}"
                f"</code>. Resource use: {t(spent)}.</p>")

    def facts_e(e: dict) -> str:
        out, inc = tx.out(e["id"]), tx.inc(e["id"])
        first, last = common(e, out, inc)
        front = contract_front(tx, store, e)
        declared = front.get("metrics") if isinstance(front.get("metrics"), list) else None
        contract = (f"sealed revision {e['revision']} ({e['contract_ref']})" if e.get("contract_ref")
                    else f"draft {e.get('contract_path') or '-'} (not sealed)")
        contract += "; revised after results: later comparisons are exploratory" if e.get("revised_after_results") \
            else ""
        tests = ", ".join(ref(x["id"], x.get("revision")) for x in out if x["rel"] == "tests")
        found = [evidence(x["id"]) for x in inc if x["rel"] == "about"]
        return dl([*first, ("question", t(e["question"])), ("tests", tests),
                   *((name.replace("_", " "), related(out, name))
                     for name in ("compared_with", "depends_on", "code_from", "motivated_by", "cites")),
                   ("contract", html.escape(contract)),
                   ("metrics", html.escape(", ".join(map(str, declared)) if declared else str(front.get("metric") or ""))),
                   ("evidence", bullets(found)), *last]) + arms(e)

    def node_e(eid: str) -> str:
        e = exps[eid]
        states = Counter(a["execution_state"] for a in check["attempts"][eid])
        badges = badge(e.get("stage") or "no stage", "stage") \
            + badge(f"contract @{e['revision']}" if e.get("contract_ref") else "draft contract") \
            + "".join(badge(f"{n} {s}") for s, n in sorted(states.items())) \
            + (badge("revised after results", "warn") if e.get("revised_after_results") else "")
        return (f'<li class="node e" id="{eid}"><div class="card">{head(e, badges)}{said(eid)}'
                f'<details class="facts"><summary>Records</summary>{facts_e(e)}</details></div></li>\n')

    def stub(hid: str) -> str:
        h, n = hyps[hid], owner[hid]
        title = words["nodes"].get(hid, {}).get("title") or h["title"]
        return (f'<li class="node stub"><a class="nid" href="#{hid}">{html.escape(labels.name(h))}</a> '
                f'{html.escape(title)} <span class="note">in <a href="#{slugs[n]}">'
                f'{html.escape(words["lines"][n]["name"])}</a></span></li>\n')

    def under(hid: str) -> list[str]:
        label = hyps[hid].get("label")
        return kids.get(label, []) if label else []

    def node_h(hid: str) -> str:
        h = hyps[hid]
        inner = "".join(node_e(x) for x in anchored.get(hid, []))
        inner += "".join(stub(c) if c in owner else node_h(c) for c in under(hid))
        badges = badge(h["evidence_status"], f"ev-{h['evidence_status']}") \
            + badge(h["disposition"], f"disp-{h['disposition']}") + badge(f"@{h['revision']}") \
            + (badge("needs re-check", "warn") if h.get("needs_recheck") else "")
        return (f'<li class="node h" id="{hid}"><div class="card">{head(h, badges)}{said(hid)}'
                f'<details class="facts"><summary>Records</summary>{facts_h(h)}</details></div>'
                + (f'\n<ul class="kids">\n{inner}</ul>' if inner else "") + "</li>\n")

    def subtree(hid: str) -> list[str]:  # what node_h(hid) shows in full
        return [hid] + [x for c in under(hid) if c not in owner for x in subtree(c)]

    def everything(hid: str) -> list[str]:  # also what it shows as a stub
        return [hid] + [x for c in under(hid) for x in everything(c)]

    def section(slug: str, name: str, desc: str, body: str, hs: list[str], extra: int = 0) -> tuple[str, str]:
        count = f"{len(hs)} hypotheses, {sum(len(anchored.get(x, [])) for x in hs) + extra} experiments"
        return (f'<section class="line" id="{slug}"><h2>{html.escape(name)}</h2>' + desc
                + f'<p class="count">{count}</p>\n<ul class="tree">\n{body}</ul></section>\n',
                f'<li><a href="#{slug}">{html.escape(name)}</a> <span class="count">{count}</span></li>')

    sections, toc = [], []
    for n, line in enumerate(words["lines"]):
        roots = [r for r in line["roots"] if owner[r] == n]
        inside = {x for r in roots for x in everything(r)}
        body = "".join(node_h(r) for r in roots) + "".join(stub(r) for r in line["roots"]
                                                           if owner[r] != n and r not in inside)
        desc = f'<p class="desc">{t(line["desc"])}</p>' if line["desc"] else ""
        html_, entry = section(slugs[n], line["name"], desc, body or '<li class="note">no hypotheses yet</li>\n',
                               [x for r in roots for x in subtree(r)])
        sections.append(html_)
        toc.append(entry)
    top = [hid for hid in kids.get(None, []) if hid not in owner]
    if top or loose:
        desc = ('<p class="desc">Top-level hypotheses in no research line (lines: roots: in '
                f"research/{html.escape(WORDS)}), and experiments without a label.</p>")
        html_, entry = section("unclassified", "Unclassified", desc,
                               "".join(node_h(r) for r in top) + "".join(node_e(x) for x in loose),
                               [x for r in top for x in subtree(r)], len(loose))
        sections.append(html_)
        toc.append(entry)

    meta = tx.meta()
    title = meta.get("title") or store.project.root.name
    ev, disp = Counter(h["evidence_status"] for h in hyps.values()), Counter(h["disposition"] for h in hyps.values())
    stages = Counter(e.get("stage") or "no stage" for e in exps.values())
    attempts = Counter(f"{a['execution_state']}/{a['outcome']}" for atts in check["attempts"].values() for a in atts)
    validity = Counter(x["validity"] for x in tx.all("Evidence"))

    def listed(counter: Counter, order) -> str:
        return ", ".join(f"{counter[k]} {k}" for k in order if counter[k])
    counts = [f"<b>{len(hyps)}</b> hypotheses" + (f": {listed(ev, ENUMS['evidence_status'])}; "
                                                    f"{listed(disp, ENUMS['disposition'])}" if hyps else ""),
              f"<b>{len(exps)}</b> experiments" + (f": {listed(stages, sorted(stages))}" if exps else ""),
              f"<b>{sum(map(len, check['runs'].values()))}</b> runs, <b>{sum(attempts.values())}</b> attempts"
              + (f": {listed(attempts, sorted(attempts))}" if attempts else ""),
              f"<b>{sum(validity.values())}</b> evidence records"
              + (f": {listed(validity, ENUMS['validity'])}" if validity else ""),
              f"<b>{len(decisions)}</b> decisions"
              + (f", {sum(d['kind'] == 'stage' for d in decisions)} of them on a stage" if decisions else "")]
    missing, stale = _counts(check)
    header = [f'<p class="kicker">Research explanation · rl view renders it from the records and '
              f"research/{html.escape(WORDS)}</p>", f"<h1>{html.escape(title)}</h1>"]
    header.append(f'<div class="lede">{paras(words["summary"])}</div>' if words["summary"]
                  else '<p class="mark todo">summary to write</p>')
    if check["summary"] == STALE:
        header.append('<p class="mark stale">the summary is not stamped yet (rl view --stamp summary)</p>'
                      if check["summary_unstamped"] else '<p class="mark stale">the summary may be out of date: stage '
                      f'decision {html.escape(check["latest_stage"])} came after the one it was stamped with</p>')
    header.append('<ul class="counts">' + "".join(f"<li>{c}</li>" for c in counts) + "</ul>")
    if check["latest_stage"]:
        latest = tx.require(check["latest_stage"])
        header.append(f'<p class="meta">Latest stage decision: {t(decision_line(latest, checks.get(latest["id"])))}'
                      "</p>")
    header.append(f'<p class="meta">Explanations: {len(nodes) + 1 - missing - stale} current, {missing} to write, '
                  f"{stale} may be out of date" + ("; <code>rl view --todo</code> lists them" if missing or stale
                                                   else "")
                  + (f"; research/{html.escape(WORDS)} has {len(check['problems'])} problem(s): <code>rl view</code> "
                     "lists them" if check["problems"] else "") + ".</p>")
    nav = f'<nav class="toc">Research lines<ol>{"".join(toc)}</ol></nav>\n' if toc else ""
    body = "".join(sections) or '<p class="note">No hypotheses yet.</p>'
    page = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}: research explanation</title><style>{CSS}</style></head>\n"
            f'<body><div class="wrap"><header>{"".join(header)}</header>\n{nav}<main>\n{body}</main>'
            "<footer>Generated by <code>rl view</code>: the facts from the research records, the words from "
            f"research/{html.escape(WORDS)}. Change those, not this page; it is rendered again when they change."
            "</footer></div></body></html>\n")
    shape = (f"{len(hyps)} hypotheses and {len(exps)} experiments, {len(words['lines'])} research line(s)"
             + (f", {len(top)} top-level hypotheses unclassified" if top else ""))
    return page, shape
