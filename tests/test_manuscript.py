"""The manuscript in docs/: record blocks refreshed from the records, the writer's prose kept."""

import re
import shutil
import sys

import pytest

from conftest import SPEC, obs
from smooth_research import literature, manuscript, research, runs
from smooth_research.util import RLError

SECTIONS = ["Abstract", "Introduction", "Related work", "Method", "Results", "Discussion", "Conclusion"]

FAKE_LATEX = r"""import pathlib, sys
tex = pathlib.Path(sys.argv[-1])
if "\\fail" in tex.read_text():
    print("This is a fake TeX\n! Undefined control sequence.\nl.12 \\fail\n\nNo pages of output.")
    sys.exit(1)
tex.with_suffix(".pdf").write_bytes(b"%PDF-1.5 fake")
"""


def results(project, *accs):
    for seed, acc in enumerate(accs):
        out = runs.register(project, "E-0001", {**SPEC, "seed": seed}, f"r{seed}")
        runs.record(project, obs(attempt=out["attempt"], state="ended", outcome="success"))
        runs.import_result(project, out["attempt"], {"status": "complete", "metrics": {"acc": acc}}, f"res{seed}")


def test_the_manuscript_keeps_the_prose_and_refreshes_the_record_blocks(project, tmp_path):
    first = manuscript.write(project)
    path = tmp_path / "docs/manuscript.md"
    text = path.read_text(encoding="utf-8")
    assert first["path"] == str(path) and first["format"] == "markdown" and first["to_write"] == SECTIONS
    assert "created docs/manuscript.md" in first["summary"]
    assert "- H1@1 (untested, active) WD helps: weight decay raises test accuracy under 20% noise (scope: toy)" in text
    assert "### EH1/1 WD smoke\n\n- question: does wd beat no wd?\n- intervention: wd" in text
    assert "EH1/1 WD smoke: no runs registered." in text and "## Appendix: records" in text

    text = text.replace("[write: the problem, why it matters, the hypotheses below and what this work adds]",
                        "Label noise hurts; we test whether weight decay helps.")
    path.write_text(text.replace("## Conclusion\n", "## Conclusion\n\nOur own paragraph.\n"), encoding="utf-8")
    results(project, 0.8, 0.9)
    research.add_evidence(project, "wd lifts acc by 5 pp", "exploratory", supports=["H-0001"], experiment="E-0001")
    again = manuscript.write(project, title="ignored")
    text = path.read_text(encoding="utf-8")
    assert "Label noise hurts" in text and "Our own paragraph." in text and "no runs registered" not in text
    assert again["to_write"] == [s for s in SECTIONS if s != "Introduction"]
    assert "refreshed the record blocks of docs/manuscript.md" in again["summary"]
    assert "--title is for a new draft" in again["summary"]
    assert "| wd | @1 | 0,1 | 2 ended/success | 0.85 (n=2) | - |" in text and "- EV-0001 [exploratory, pending]" in text

    path.write_text(re.sub(r"<!-- rl:begin related -->.*?<!-- rl:end related -->\n", "", text, flags=re.S),
                    encoding="utf-8")
    assert "not in the draft (their markers were removed): related" in manuscript.write(project)["summary"]


def test_with_a_latex_command_the_manuscript_is_latex_built_into_a_pdf(project, tmp_path):
    script = tmp_path / "fake_latex.py"
    script.write_text(FAKE_LATEX, encoding="utf-8")
    (tmp_path / "docs/manuscript.md").write_text("an earlier Markdown draft", encoding="utf-8")
    project.project.config["manuscript"]["latex"] = [sys.executable, str(script)]
    research.add_hypothesis(project, "λ_2 & 50% ≥ x", "costs $5 # {a} ~ ^ — see --recheck-of", "toy")
    out = manuscript.write(project)
    folder = tmp_path / "docs/manuscript"
    text = (folder / "manuscript.tex").read_text(encoding="utf-8")
    assert out["format"] == "latex" and out["pdf"] == str(folder / "manuscript.pdf")
    assert "built docs/manuscript/manuscript.pdf" in out["summary"] and out["to_write"] == SECTIONS
    assert "docs/manuscript.md (the Markdown draft) is not converted" in out["summary"]
    assert r"\ensuremath{\lambda}\_2 \& 50\% \ensuremath{\geq} x" in text
    assert r"costs \$5 \# \{a\} \textasciitilde{} \textasciicircum{} --- see -{}-recheck-of" in text
    assert "% rl:begin results\n" in text and "*.aux" in (folder / ".gitignore").read_text(encoding="utf-8")

    (folder / "manuscript.tex").write_text(text.replace("\\section{Conclusion}", "\\section{Conclusion}\n\\fail"),
                                           encoding="utf-8")
    with pytest.raises(RLError) as failed:
        manuscript.write(project)
    assert failed.value.code == "BUILD" and "! Undefined control sequence.\nl.12 \\fail" in failed.value.message
    assert "\\fail" in (folder / "manuscript.tex").read_text(encoding="utf-8")  # the writer's text is kept

    project.project.config["manuscript"]["latex"] = "no-such-latex -pdf"
    with pytest.raises(RLError) as missing:
        manuscript.write(project)
    assert missing.value.code == "UNAVAILABLE" and "no-such-latex" in missing.value.message


@pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex is not installed")
def test_the_latex_scaffold_builds_with_pdflatex(project, tmp_path):
    project.project.config["manuscript"]["latex"] = "pdflatex -interaction=nonstopmode -halt-on-error"
    results(project, 0.8, 0.9)
    research.add_hypothesis(project, "λ_2 & 50% ≥ x", "costs $5 # {a} ~ ^ — → ±0.1 μ “quoted” --flag", "toy")
    literature.add_paper(project, title="A study_of noise", authors=["A. Author", "B. Author"], year=2024)
    out = manuscript.write(project)
    assert (tmp_path / "docs/manuscript/manuscript.pdf").stat().st_size > 1000 and out["pdf"]


def test_cjk_text_missing_from_the_pdf_text_is_a_problem_and_other_sources_are_not_checked():
    sentences = ["权重衰减提高测试准确率", "噪声标签降低训练稳定性", "三个种子的结果一致", "学习率过大时损失发散",
                 "数据增强缓解过拟合", "批量大小影响收敛速度", "早停避免验证集退化", "正则化强度需要调节",
                 "模型宽度改变泛化误差", "梯度裁剪稳定训练过程", "混合精度节省显存占用", "标签平滑改善校准误差"]
    tex = "\\documentclass{article}\n% 注释里的中文不算\n\\begin{document}\n" + "。\n".join(sentences) + \
        "。\n\\end{document}\n"
    samples = manuscript.cjk_samples(tex)
    assert len(samples) == manuscript.CJK_SAMPLES and "注释里的中文不算" not in samples
    clean = "".join(samples)
    assert manuscript.cjk_text_problem(tex, " ".join(clean)) is None  # whitespace in the PDF's text is ignored
    half = "".join(samples[:4])
    assert manuscript.cjk_text_problem(tex, half) is None  # half found: fine
    problem = manuscript.cjk_text_problem(tex, "".join(samples[:3]) + "\x00\x01 garbled")
    assert problem.startswith("5 of 8 runs of Chinese or Japanese text from the source are not in the PDF's text")
    assert manuscript.cjk_text_problem("\\begin{document}\nonly English, 3 Kanji 漢字\n", "") is None


def _cjk_font():
    """A font file with Chinese glyphs (by file: a family name may also name a Latin-only face)."""
    if not shutil.which("fc-list"):
        return None
    import subprocess
    from pathlib import Path
    out = subprocess.run(["fc-list", ":lang=zh", "file"], capture_output=True, text=True).stdout
    files = sorted(Path(line.split(":")[0].strip()) for line in out.splitlines() if line.strip())
    return files[0] if files else None


@pytest.mark.skipif(not shutil.which("xelatex") or not _cjk_font(), reason="xelatex or a Chinese font is missing")
def test_a_chinese_manuscript_built_with_xelatex_is_checked_against_its_pdf_text(project, tmp_path):
    project.project.config["manuscript"]["latex"] = "xelatex -interaction=nonstopmode -halt-on-error"
    manuscript.write(project, title="噪声标签下的权重衰减")
    tex = tmp_path / "docs/manuscript/manuscript.tex"
    font = _cjk_font()
    setup = f"\\setmainfont{{{font.name}}}[Path={font.parent}/]\n\\XeTeXlinebreaklocale \"zh\"\n"
    prose = "权重衰减在百分之二十的噪声标签下提高了测试准确率，三个种子的结果一致。「引号」用这种括号。"
    text = tex.read_text().replace("\\begin{document}", setup + "\\begin{document}", 1)
    text = text.replace("\\section{Introduction}", "\\section{Introduction}\n" + prose, 1)
    tex.write_text(text, encoding="utf-8")
    out = manuscript.write(project)
    assert out["pdf"] and "built docs/manuscript/manuscript.pdf (xelatex)" in out["summary"]
    hidden = "\\iffalse\n" + "\n".join(f"第{i}节没有排进版面的中文文字" for i in range(20)) + "\n\\fi\n"
    tex.write_text(text.replace("\\section{Introduction}", hidden + "\\section{Introduction}", 1), encoding="utf-8")
    with pytest.raises(RLError) as exc:
        manuscript.write(project)
    assert exc.value.code == "BUILD" and "runs of Chinese or Japanese text from the source are not in the PDF's " \
        "text" in exc.value.message
