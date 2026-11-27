"""Locators: the refs of a note or a dialogue entry, checked when written and expanded when read (design 8.6).

Three kinds:
- a record, `T-0042`, or a sub-item `RV-0036#F2`: the record (and the finding) exists;
- code, `<commit>:<path>::<symbol>` (or `<commit>:<path>`): the commit exists and has the path; for a Python file
  the symbol too. A branch name is replaced by the commit it points at when written;
- a file, `DOC-0031` (a record) or an output a task registered, `research/work/T-0042/report.md`.

A locator that does not resolve is refused (it cannot be recorded: it points at nothing), with a hint of where the
content belongs instead. How many locators and how long a note are not checked here: a long note is kept and flagged
by the command that writes it (2026-11-27, 5.1). note_cap is for a caller that still wants the old cap.
"""

from __future__ import annotations

import re

from . import gitops
from .project import Project
from .store import Tx, label_of
from .util import RLError

NOTE_CHARS = 300
RECORD = re.compile(r"^([A-Z]+-[0-9]{3,6}(?:\.[0-9]+)*)(?:#F([0-9]+))?$")
CODE = re.compile(r"^([^\s:]+):([^\s:][^\s]*?)(?:::([A-Za-z_][\w.]*))?$")
WHERE = "write it into a record instead: a finding, evidence, a decision, or a document on the subject"


def _outputs(tx: Tx) -> set[str]:
    out = set()
    for t in tx.all("Task"):
        for o in t.get("outputs") or []:
            out.add(o["path"] if isinstance(o, dict) else o)
    return out


def check(tx: Tx, project: Project, refs: list[str], note: str | None = None, note_cap: int | None = None
          ) -> list[str]:
    """Validate locators (and a note's length); return them normalised: branch names pinned to commits."""
    refs = [r.strip() for r in refs if r and r.strip()]
    if note and note_cap and len(note) > note_cap:
        raise RLError("INVALID", f"the note has {len(note)} characters, at most {note_cap}; {WHERE}")
    out, bad = [], []
    outputs = None
    for ref in refs:
        m = RECORD.match(ref)
        if m:
            rec = _record(tx, m[1])
            if rec is None:
                bad.append(f"{ref}: no such record")
            elif m[2] and not any(f.get("n") == int(m[2]) for f in rec.get("findings") or []):
                bad.append(f"{ref}: {m[1]} has no finding F{m[2]}")
            else:
                out.append(ref)
            continue
        if ref.startswith("research/") or ref.startswith("docs/"):
            outputs = outputs if outputs is not None else _outputs(tx)
            if ref in outputs or tx.all("Document", path=ref):
                out.append(ref)
            else:
                bad.append(f"{ref}: not a registered task output or document")
            continue
        m = CODE.match(ref)
        if m:
            commit = gitops.rev(project, m[1])
            if commit is None:
                bad.append(f"{ref}: no commit or branch {m[1]}")
                continue
            text = gitops.show(project, commit, m[2])
            if text is None:
                bad.append(f"{ref}: {m[2]} is not in {commit[:9]}")
                continue
            if m[3] and m[2].endswith(".py") and gitops.symbol_source(text, m[3]) is None:
                bad.append(f"{ref}: {m[2]} at {commit[:9]} has no {m[3]}")
                continue
            out.append(f"{commit[:12]}:{m[2]}" + (f"::{m[3]}" if m[3] else ""))
            continue
        bad.append(f"{ref}: not a locator (a record T-0042 or RV-0036#F2, code <commit>:<path>::<symbol>, or a "
                   "registered output research/work/T-xxxx/...)")
    if bad:
        raise RLError("INVALID", "; ".join(bad) + f". {WHERE}")
    return out


def _record(tx: Tx, rid: str) -> dict | None:
    try:
        label_of(rid)
    except RLError:
        return None
    return tx.get(rid)


def expand(tx: Tx, project: Project, refs: list[str], since: str | None = None) -> list[str]:
    """One line per locator from the records as they are now; what changed after `since` is marked."""
    from .research import brief
    lines = []
    width = max((len(r) for r in refs), default=0)
    for ref in refs:
        m = RECORD.match(ref)
        try:
            if m:
                rec = tx.get(m[1])
                if rec is None:
                    line = "gone"
                elif m[2]:
                    f = next((x for x in rec.get("findings") or [] if x.get("n") == int(m[2])), {})
                    line = f"{f.get('category')} · {f.get('severity')} · {f.get('status')} · {f.get('text', '')[:80]}"
                else:
                    line = brief(rec)
                    if since and rec.get("updated_at", "") > since:
                        line += "  (changed since)"
            else:
                cm = CODE.match(ref)
                if cm and not ref.startswith("research/"):
                    line = _code_line(tx, project, cm[1], cm[2])
                else:
                    line = "registered output" if ref in _outputs(tx) else "document"
        except RLError as e:
            line = f"unreadable: {e.message}"
        lines.append(f"{ref:<{width}}  {line}")
    return lines


def _code_line(tx: Tx, project: Project, commit: str, path: str) -> str:
    for t in tx.all("Task"):
        branch = t.get("branch")
        if branch and t["state"] not in ("accepted", "cancelled") and gitops.rev(project, branch) \
                and gitops.is_ancestor(project, commit, branch):
            n = gitops.commits_after(project, commit, branch)
            if n:
                return f"{branch} has {n} commit(s) after it"
    n = gitops.commits_after(project, commit, gitops.mainline(project))
    return f"{path} at {commit[:9]}" + (f"; the mainline has {n} commit(s) after it" if n else "")
