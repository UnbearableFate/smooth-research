"""The manuscript (the paper draft) in docs/: blocks generated from the records, prose written by the
coordinator.

`rl manuscript` writes docs/manuscript.md, or docs/manuscript/manuscript.tex built into a PDF beside it when
manuscript.latex names a build command. Each block between an rl:begin and an rl:end marker is refreshed from
the records on every run; all other text is the writer's and is kept. A section still to write holds a
[write: ...] placeholder.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import labels
from .project import latex_command
from .research import contract_front
from .runs import usage_summary
from .store import Store, Tx
from .util import RLError
from .view import arm_table, conclusion_checks, costs, decision_line, evidence_line

BUILD_TIMEOUT = 300  # seconds for one LaTeX build
# Chinese and Japanese text: kana, CJK ideographs and halfwidth katakana (punctuation is left out: fonts and
# extractors render it variously)
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]+")
CJK_SAMPLES = 8  # runs of CJK text looked up in the PDF's text after a build
METHOD_FIELDS = ("question", "intervention", "comparator", "metric", "scope")

# (heading, what the writer adds there, the record blocks that follow)
SECTIONS = (
    ("Abstract", "the question, the method, the main result with its evidence level and the main limitation, in "
                 "about 150 words", ()),
    ("Introduction", "the problem, why it matters, the hypotheses below and what this work adds", ("hypotheses",)),
    ("Related work", "how the work below relates to this one and what it leaves open", ("related",)),
    ("Method", "the setup of each experiment below (data, model, arms, seeds, metrics), from its contract",
     ("method",)),
    ("Results", "what each table shows and the evidence level of each claim; the means are descriptive, not tests",
     ("results", "evidence")),
    ("Discussion", "what the evidence supports and what it does not, the limitations and threats to validity, and "
                   "the decisions below", ("decisions",)),
    ("Conclusion", "the answer to the question within the scope of the evidence, and the next step", ()),
    ("Records", None, ("records",)),
)

MD_HEADER = """<!-- Manuscript draft. `rl manuscript` refreshes each block between rl:begin and rl:end from the
records on every run; all other text is yours and is kept. Replace each [write: ...] with prose; correct a
block's facts in the records (rl), not in the block. -->"""

TEX_HEADER = r"""% Manuscript draft. `rl manuscript` refreshes each block between "% rl:begin" and "% rl:end" from the
% records on every run and builds the PDF with manuscript.latex; all other text is yours and is kept. Replace
% each [write: ...] placeholder (\towrite) with prose; correct a block's facts in the records (rl), not in the
% block. For Chinese or Japanese text, build with xelatex or lualatex and load a CJK package here (e.g. xeCJK,
% luatexja).
\documentclass[11pt]{article}
\usepackage{iftex}
\ifPDFTeX
  \usepackage[T1]{fontenc}
  \usepackage[utf8]{inputenc}
  \usepackage{lmodern}
\else
  \usepackage{fontspec}
\fi
\usepackage{amsmath}
\usepackage{booktabs}
\usepackage{float}
\usepackage[margin=2.5cm]{geometry}
\usepackage[hidelinks]{hyperref}
\newcommand{\towrite}[1]{\textbf{[write:} #1\textbf{]}}"""

TEX_IGNORE = "*.aux\n*.log\n*.out\n*.toc\n*.fls\n*.fdb_latexmk\n*.synctex.gz\n*.bbl\n*.blg\n*.xdv\n"

_GREEK = dict(zip("αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΥΦΨΩ",
                  "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi pi rho sigma tau upsilon "
                  "phi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split()))
_MATH = {**{c: "\\" + name for c, name in _GREEK.items()},
         "≥": r"\geq", "≤": r"\leq", "≠": r"\neq", "≈": r"\approx", "±": r"\pm", "×": r"\times",
         "÷": r"\div", "−": "-", "·": r"\cdot", "→": r"\rightarrow", "←": r"\leftarrow", "↔": r"\leftrightarrow",
         "⇒": r"\Rightarrow", "↳": r"\hookrightarrow", "∞": r"\infty", "°": r"^{\circ}", "∈": r"\in",
         "∼": r"\sim", "√": r"\surd"}
_TEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
        "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "<": r"\textless{}", ">": r"\textgreater{}",
        "—": "---", "–": "--", "…": r"\ldots{}", "“": "``", "”": "''", "‘": "`", "’": "'", "\u00a0": "~",
        **{c: rf"\ensuremath{{{m}}}" for c, m in _MATH.items()}}
# a hyphen before another one stays a hyphen (--recheck-of), not a dash
_TEX_RE = re.compile(r"-(?=-)|" + "|".join(map(re.escape, _TEX)))


def tex_escape(text) -> str:
    """Record text as LaTeX text: special characters escaped, common symbols and Greek letters as math."""
    return _TEX_RE.sub(lambda m: _TEX.get(m.group(0), "-{}"), " ".join(str(text).split()))


def _md_escape(text) -> str:
    return " ".join(str(text).split())


class Markdown:
    name = "markdown"
    block = re.compile(r"^<!-- rl:begin (?P<name>[\w-]+) -->[ \t]*\n(?P<body>.*?)^<!-- rl:end (?P=name) -->[ \t]*$",
                       re.S | re.M)
    placeholder = re.compile(r"^\[write: ", re.M)
    heading = re.compile(r"^#{1,6}[ \t]+(?P<a>.+?)[ \t]*$", re.M)

    @staticmethod
    def marked(name: str, body: str) -> str:
        return f"<!-- rl:begin {name} -->\n{body}\n<!-- rl:end {name} -->"

    @staticmethod
    def render(parts: list[tuple]) -> str:
        out = []
        for kind, *rest in parts:
            if kind == "heading":
                out.append(f"### {_md_escape(rest[0])}")
            elif kind == "list":
                out.append("\n".join(f"- {_md_escape(t)}" for t in rest[0] or ["none recorded"]))
            elif kind == "table":
                caption, header, rows, note = rest
                line = lambda cells: "| " + " | ".join(_md_escape(c).replace("|", "\\|") for c in cells) + " |"
                out.append("\n".join([f"**{_md_escape(caption)}**", "", line(header), "|" + " --- |" * len(header),
                                      *map(line, rows), "", _md_escape(note)]))
            else:
                out.append(_md_escape(rest[0]))
        return "\n\n".join(out)

    @classmethod
    def scaffold(cls, title: str, blocks: dict[str, str]) -> str:
        out = [MD_HEADER, f"# {_md_escape(title)}"]
        for heading, write, names in SECTIONS:
            out.append(f"## {'Appendix: records' if heading == 'Records' else heading}")
            out += [f"[write: {write}]"] if write else []
            out += [cls.marked(n, blocks[n]) for n in names]
        return "\n\n".join(out) + "\n"


class Latex:
    name = "latex"
    block = re.compile(r"^% rl:begin (?P<name>[\w-]+)[ \t]*\n(?P<body>.*?)^% rl:end (?P=name)[ \t]*$", re.S | re.M)
    placeholder = re.compile(r"\\towrite\{")
    heading = re.compile(r"\\(?:sub)*section\*?\{(?P<a>[^}]*)\}|\\begin\{(?P<b>abstract)\}")

    @staticmethod
    def marked(name: str, body: str) -> str:
        return f"% rl:begin {name}\n{body}\n% rl:end {name}"

    @staticmethod
    def render(parts: list[tuple]) -> str:
        out = []
        for kind, *rest in parts:
            if kind == "heading":
                out.append(f"\\subsection*{{{tex_escape(rest[0])}}}")
            elif kind == "list":
                out.append("\\begin{itemize}\n" + "\n".join(f"\\item {tex_escape(t)}" for t in rest[0] or
                                                             ["none recorded"]) + "\n\\end{itemize}")
            elif kind == "table":
                caption, header, rows, note = rest
                spec = "".join("p{0.2\\linewidth}" if h in ("condition", "attempts", "notes") else "l" for h in header)
                line = lambda cells: " & ".join(tex_escape(c) for c in cells) + r" \\"
                out.append("\n".join([r"\begin{table}[H]", r"\centering\small",
                                      f"\\caption{{{tex_escape(caption)}}}", f"\\begin{{tabular}}{{{spec}}}",
                                      r"\toprule", line(header), r"\midrule", *map(line, rows), r"\bottomrule",
                                      r"\end{tabular}", f"\\par\\smallskip{{\\footnotesize {tex_escape(note)}}}",
                                      r"\end{table}"]))
            else:
                out.append(f"\\noindent {tex_escape(rest[0])}")
        return "\n\n".join(out)

    @classmethod
    def scaffold(cls, title: str, blocks: dict[str, str]) -> str:
        out = [TEX_HEADER, f"\\title{{{tex_escape(title)}}}\n\\author{{}}\n\\date{{\\today}}",
               "\\begin{document}\n\\maketitle"]
        for heading, write, names in SECTIONS:
            if heading == "Abstract":
                out.append(f"\\begin{{abstract}}\n\\towrite{{{tex_escape(write)}}}\n\\end{{abstract}}")
                continue
            out += ["\\appendix"] if heading == "Records" else []
            out.append(f"\\section{{{heading}}}")
            out += [f"\\towrite{{{tex_escape(write)}}}"] if write else []
            out += [cls.marked(n, blocks[n]) for n in names]
        out.append("\\end{document}")
        return "\n\n".join(out) + "\n"


def _contract_source(exp: dict) -> str:
    if exp.get("contract_ref"):
        return f"{exp['id']}@{exp['revision']}, sealed as {exp['contract_ref']} ({exp.get('contract_path')})"
    return f"draft, not sealed ({exp['contract_path']})" if exp.get("contract_path") else "no contract"


def _paper_line(tx: Tx, p: dict) -> str:
    authors = p.get("authors") or []
    who = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "") if authors else "authors unknown"
    where = (f"doi:{p['doi']}" if p.get("doi") else f"arXiv:{p['arxiv_id']}" if p.get("arxiv_id") else p.get("url"))
    uses = []
    for e in tx.inc(p["id"]):
        if e["label"] == "Hypothesis":
            how = ", ".join(x for x in (e.get("assessment"), e.get("locator")) if x)
            uses.append(f"{e['rel'].replace('_', ' ')} of {e['id']}" + (f" ({how})" if how else ""))
    return (f"{p['id']} {who} ({p.get('year') or 'n.d.'}). {p['title']}." + (f" {where}." if where else "")
            + f" Metadata {p['metadata_status']}." + (f" {'; '.join(uses)}." if uses else ""))


def blocks(tx: Tx, store: Store) -> dict[str, list[tuple]]:
    """Each record block as parts: ("heading", text), ("list", items), ("table", caption, header, rows, note) or
    ("text", text); hypotheses and experiments are named by their labels, also in the text of the records."""
    meta, project = tx.meta(), store.project
    hyps, exps, decisions = labels.ordered(tx.all("Hypothesis")), labels.ordered(tx.all("Experiment")), \
        tx.all("Decision")
    method, results = [], []
    for exp in exps:
        front = contract_front(tx, store, exp)
        method += [("heading", f"{exp['id']} {exp['title']}"),
                   ("list", [f"{k}: {front[k]}" for k in METHOD_FIELDS if front.get(k) not in (None, "", "TODO")]
                    + [f"contract: {_contract_source(exp)}"])]
        head, rows = arm_table(tx, store, exp)
        if not rows:
            results.append(("text", f"{exp['id']} {exp['title']}: no runs registered."))
            continue
        spent = "; ".join(costs(tx, exp["id"])) or "no attempt ran"
        results.append(("table", f"{exp['id']} {exp['title']}: descriptive means, one result per run", head, rows,
                        "Rows of different conditions are not averaged together. Per-attempt records: "
                        f"rl run show {exp['id']}. Resource use: {spent}."))
    checks = conclusion_checks(tx, decisions)
    attempts = tx.all("Attempt")
    states = Counter(f"{a['execution_state']}/{a['outcome']}" for a in attempts)
    usage = usage_summary(tx)
    records = [
        f"records at data_version {meta['data_version']}; plan {meta.get('plan_ref') or 'not imported'} "
        f"({project.rel(project.plan_path)}); constraints {project.rel(project.constraints_path)}",
        "experiments: " + (", ".join(f"{e['id']}@{e['revision']} ({e.get('contract_ref') or 'draft'})" for e in exps)
                           or "none"),
        f"attempts: {len(attempts)}" + (f" ({', '.join(f'{n} {s}' for s, n in sorted(states.items()))})"
                                        if attempts else ""),
        "reviews and advice: " + (", ".join(f"{t['id']} {t['status']}" + (f" {t['verdict']}" if t.get("verdict")
                                                                            else "") for t in tx.all("Ticket"))
                                  or "none"),
        "resource use: " + ("; ".join(f"{t['quantity']:g} {t['unit']} {t['resource_type']} on {t['hardware']} "
                                      f"({t['basis']})" for t in usage["totals"]) or "none recorded")]
    out = {
        "hypotheses": [("list", [f"{h['id']}@{h['revision']} ({h['evidence_status']}, {h['disposition']}) "
                                 f"{h['title']}: {h['statement']}"
                                 + (f" (scope: {h['scope']})" if h.get("scope") else "") for h in hyps])],
        "related": [("list", [_paper_line(tx, p) for p in tx.all("Paper")])],
        "method": method or [("text", "no experiments recorded")],
        "results": results or [("text", "no experiments recorded")],
        "evidence": [("text", "Evidence (level, validity):"),
                     ("list", [evidence_line(tx, ev) for ev in tx.all("Evidence")])],
        "decisions": [("list", [decision_line(d, checks.get(d["id"])) for d in decisions])],
        "records": [("list", records)],
    }
    known = labels.names(tx)

    def named(part):
        if isinstance(part, str):
            return labels.relabel(part, known)
        return [named(p) for p in part] if isinstance(part, list) else part
    return {block: [tuple(named(p) for p in part) for part in parts] for block, parts in out.items()}


def _sections_to_write(text: str, fmt) -> list[str]:
    heads = [(m.start(), m.group("a") or "Abstract") for m in fmt.heading.finditer(text)]
    out = []
    for m in fmt.placeholder.finditer(text):
        name = next((h for pos, h in reversed(heads) if pos < m.start()), "(before the first heading)")
        out += [name] if name not in out else []
    return out


def write(store: Store, title: str | None = None) -> dict:
    """Write or refresh the manuscript; with manuscript.latex set, also build the PDF."""
    project = store.project
    command = latex_command(project.config)
    fmt = Latex if command else Markdown
    path = project.docs_root / ("manuscript/manuscript.tex" if command else "manuscript.md")
    with store.read() as tx:
        meta = tx.meta()
        rendered = {name: fmt.render(parts) for name, parts in blocks(tx, store).items()}
    notes = []
    if path.exists():
        text = path.read_text(encoding="utf-8")
        found = []

        def refresh(m: re.Match) -> str:
            name = m.group("name")
            if name not in rendered:
                return m.group(0)
            found.append(name)
            return fmt.marked(name, rendered[name])

        text = fmt.block.sub(refresh, text)
        missing = [n for n in rendered if n not in found]
        verb = "refreshed the record blocks of"
        if title:
            notes.append("--title is for a new draft; change the title in the file")
        if missing:
            notes.append(f"not in the draft (their markers were removed): {', '.join(missing)}")
    else:
        text, verb = fmt.scaffold(title or meta.get("title") or "Research manuscript", rendered), "created"
        if command and (project.docs_root / "manuscript.md").exists():
            notes.append("docs/manuscript.md (the Markdown draft) is not converted: carry its prose over")
    path.parent.mkdir(parents=True, exist_ok=True)
    if command and not (path.parent / ".gitignore").exists():
        (path.parent / ".gitignore").write_text(TEX_IGNORE, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    todo = _sections_to_write(text, fmt)
    mark = "[write: ...]" if fmt is Markdown else "\\towrite{...}"
    lines = [f"{verb} {project.rel(path)} (data_version {meta['data_version']})",
             f"still to write: {', '.join(todo)} — replace each {mark} with prose; text outside the rl blocks is kept"
             if todo else f"no {mark} placeholder left"]
    lines += notes
    pdf = _build(command, path, project) if command else None
    if pdf:
        lines.append(f"built {project.rel(pdf)} ({command[0]})")
    return {"path": str(path), "format": fmt.name, "to_write": todo, "pdf": str(pdf) if pdf else None,
            "summary": "\n".join(lines)}


def _build(command: list[str], tex: Path, project) -> Path:
    """Run the configured LaTeX command in the draft's folder (manuscript.tex is appended unless a .tex is
    named)."""
    args = command if any(a.endswith(".tex") for a in command[1:]) else [*command, tex.name]
    pdf = tex.with_suffix(".pdf")
    start = time.time()
    try:
        done = subprocess.run(args, cwd=tex.parent, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=BUILD_TIMEOUT)
    except FileNotFoundError:
        raise RLError("UNAVAILABLE", f"wrote {project.rel(tex)}, but manuscript.latex names {command[0]}, which is "
                                     "not installed here; install it or change manuscript.latex") from None
    except subprocess.TimeoutExpired:
        raise RLError("BUILD", f"wrote {project.rel(tex)}, but `{' '.join(args)}` did not finish in "
                               f"{BUILD_TIMEOUT} s") from None
    if done.returncode == 0 and pdf.exists() and pdf.stat().st_mtime >= start - 1:
        source = tex.read_text(encoding="utf-8", errors="replace")
        problem = cjk_text_problem(source, _pdf_text(pdf, project)) if cjk_samples(source) else None
        if problem:
            raise RLError("BUILD", f"built {project.rel(pdf)}, but {problem}")
        return pdf
    out = (done.stdout + "\n" + done.stderr).splitlines()
    errors = [i for i, line in enumerate(out) if line.startswith("!")][:3]
    shown = [line for i in errors for line in out[i:i + 3]] or out[-15:]
    log = tex.with_suffix(".log")
    raise RLError("BUILD", f"wrote {project.rel(tex)}, but `{' '.join(args)}` failed (exit {done.returncode})"
                           + (f"; full log {project.rel(log)}" if log.exists() else "") + ":\n" + "\n".join(shown))


def cjk_samples(tex_text: str, n: int = CJK_SAMPLES) -> list[str]:
    """Up to n runs of at least four consecutive CJK characters from the body of a .tex (comments removed), spread
    over the text."""
    body = tex_text.split("\\begin{document}", 1)[-1]
    body = re.sub(r"(?<!\\)%.*", "", body)
    runs = [r for r in CJK.findall(body) if len(r) >= 4]
    return runs if len(runs) <= n else [runs[i * len(runs) // n] for i in range(n)]


def cjk_text_problem(tex_text: str, pdf_text: str) -> str | None:
    """The check after a build: a PDF can look right while its text is garbage (glyphs drawn without a map back to
    the characters), so runs of CJK text from the source are looked up in the PDF's text, whitespace removed. More
    than half missing is a problem; a source without CJK text is not checked."""
    samples = cjk_samples(tex_text)
    if not samples:
        return None
    text = "".join(pdf_text.split())
    missing = [s for s in samples if s not in text]
    if len(missing) * 2 <= len(samples):
        return None
    return (f"{len(missing)} of {len(samples)} runs of Chinese or Japanese text from the source are not in the "
            f"PDF's text (for example {missing[0]!r}): it may look right while its text is garbled, which search, copy "
            "and screen readers see. Build with xelatex or lualatex and a CJK font set up as docs/configuration.md "
            "describes (manuscript)")


def _pdf_text(pdf: Path, project) -> str:
    """The text of a PDF as pypdf extracts it."""
    try:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)
    except Exception as exc:  # noqa: BLE001 - whatever failed, the text was not checked: say so
        raise RLError("BUILD", f"built {project.rel(pdf)}, but its text could not be read to check its Chinese or "
                               f"Japanese text ({type(exc).__name__}: {exc})") from None
