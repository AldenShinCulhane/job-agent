"""Unit tests for resume ingestion building blocks (no LLM calls)."""

import shutil

import pytest

from backend.resume import facts, ingest
from backend.resume.template import DEFAULT_LAYOUT, latex_escape, render, rich
from backend.tailor import compile as latex

SAMPLE_PROFILE = {
    "name": "Jane Doe",
    "headline": "Software Engineer | Full-Stack | AI & LLM Systems",
    "contact": {
        "phone": "555-000-1111",
        "email": "jane@example.com",
        "linkedin": "linkedin.com/in/janedoe",
        "github": "https://github.com/janedoe",
        "website": None,
        "location": "Toronto, ON",
    },
    "experiences": [
        {
            "title": "Software Engineer",
            "company": "Acme & Sons {Ltd}",
            "location": "Toronto, ON",
            "start": "Mar 2024",
            "end": "Present",
            "bullets": [
                "Cut costs by 50% using C# & Rust_v2 ~magic~",
                "Shipped **3 services** with 99.9% uptime",
            ],
        }
    ],
    "projects": [
        {
            "name": "Job Agent",
            "technologies": ["Python", "LaTeX"],
            "date": "Feb 2026",
            "link": "github.com/janedoe/job-agent",
            "bullets": ["Built a $100 pipeline with 1-page enforcement"],
        }
    ],
    "education": [
        {
            "institution": "Toronto Metropolitan University",
            "location": "Toronto, ON",
            "degree": "BSc Computer Science, Dean's List",
            "start": "Sept 2020",
            "end": "Aug 2025",
            "details": [],
        }
    ],
    "skills": [
        {"category": "Languages", "items": ["Python", "TypeScript", "C#"]},
        {"category": "Cloud", "items": ["AWS", "Azure"]},
    ],
}


# --- escaping ---------------------------------------------------------------

def test_escape_special_characters():
    assert latex_escape("A & B_C 100% $5 #1 {x} ~y^") == (
        r"A \& B\_C 100\% \$5 \#1 \{x\} \textasciitilde{}y\textasciicircum{}"
    )


def test_escape_backslash_not_double_escaped():
    assert latex_escape(r"C:\dir") == r"C:\textbackslash{}dir"


def test_typography_normalized():
    assert latex_escape("a—b–c ’d “e” •f") == "a---b--c 'd ``e'' f"


def test_bold_markers_survive_escaping():
    assert rich("Shipped **3 services** & more") == r"Shipped \textbf{3 services} \& more"


# --- truth ledger -----------------------------------------------------------

def test_ledger_ids_stable_under_reorder():
    before = facts.build_ledger(SAMPLE_PROFILE)
    reordered = dict(SAMPLE_PROFILE)
    reordered["skills"] = list(reversed(SAMPLE_PROFILE["skills"]))
    after = facts.build_ledger(reordered)
    assert set(before) == set(after)


def test_ledger_id_changes_when_text_edited():
    before = facts.build_ledger(SAMPLE_PROFILE)
    edited = {**SAMPLE_PROFILE, "name": "Jane A. Doe"}
    after = facts.build_ledger(edited)
    assert set(before) != set(after)
    assert len(before) == len(after)


def test_ledger_duplicate_text_gets_unique_ids():
    profile = {**SAMPLE_PROFILE}
    exp = dict(SAMPLE_PROFILE["experiences"][0])
    exp["bullets"] = ["Same bullet", "Same bullet"]
    profile["experiences"] = [exp]
    ledger = facts.build_ledger(profile)
    paths = [
        e["path"] for e in ledger.values()
        if e["kind"] == "bullet" and e["path"].startswith("/experiences")
    ]
    assert len(paths) == 2 and len(set(paths)) == 2


def test_ledger_same_bullet_under_two_employers_stays_bound_to_its_employer():
    base = SAMPLE_PROFILE["experiences"][0]
    exp_a = {**base, "company": "Acme", "bullets": ["Same bullet"]}
    exp_b = {**base, "company": "Beta", "bullets": ["Same bullet"]}
    profile = {**SAMPLE_PROFILE, "experiences": [exp_a, exp_b], "projects": []}

    def bullet_company(ledger, prof):
        out = {}
        for fact_id, entry in ledger.items():
            if entry["kind"] != "bullet":
                continue
            exp_index = int(entry["path"].split("/")[2])
            out[fact_id] = prof["experiences"][exp_index]["company"]
        return out

    before = bullet_company(facts.build_ledger(profile), profile)
    assert sorted(before.values()) == ["Acme", "Beta"]

    reordered = {**profile, "experiences": [exp_b, exp_a]}
    after = bullet_company(facts.build_ledger(reordered), reordered)
    # Same ID set, and each ID still denotes the same employer's bullet.
    assert before == after


def test_ledger_project_entity_includes_link():
    ledger = facts.build_ledger(SAMPLE_PROFILE)
    entities = [e["text"] for e in ledger.values() if e["kind"] == "project"]
    assert any("github.com/janedoe/job-agent" in text for text in entities)


def test_ledger_covers_every_bullet_and_entity():
    ledger = facts.build_ledger(SAMPLE_PROFILE)
    kinds = [e["kind"] for e in ledger.values()]
    assert kinds.count("bullet") == 3
    assert kinds.count("experience") == 1
    assert kinds.count("project") == 1
    assert kinds.count("education") == 1
    assert kinds.count("skill") == 5


# --- template rendering -----------------------------------------------------

def test_render_escapes_content_and_skips_empty_sections():
    tex = render(SAMPLE_PROFILE)
    assert r"Acme \& Sons \{Ltd\}" in tex
    assert r"\textbf{3 services}" in tex
    assert "Jane Doe" in tex
    profile = {**SAMPLE_PROFILE, "projects": [], "skills": []}
    tex2 = render(profile)
    assert "Personal Projects" not in tex2
    assert "Technical Skills" not in tex2


def test_render_uses_layout_values():
    tex = render(SAMPLE_PROFILE, DEFAULT_LAYOUT)
    assert "\\titlespacing*{\\section}{0pt}{12pt}{10pt}" in tex
    assert "letterpaper,11pt" in tex


# --- weak-text / OCR routing ------------------------------------------------

def test_unsupported_file_type_rejected(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("hello")
    with pytest.raises(ingest.IngestError, match="Unsupported"):
        ingest.extract_text(path)


def test_scanned_pdf_without_ocrmypdf_gives_actionable_error(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    blank = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(blank, "wb") as fh:
        writer.write(fh)

    monkeypatch.setattr(ingest.shutil, "which", lambda _: None)
    with pytest.raises(ingest.IngestError, match="ocrmypdf"):
        ingest.extract_text(blank)


def test_scanned_pdf_triggers_ocr_call(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    blank = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(blank, "wb") as fh:
        writer.write(fh)

    calls = []

    def fake_ocr(path):
        calls.append(path)
        return "OCR RECOVERED TEXT " * 20

    monkeypatch.setattr(ingest, "_ocr_pdf", fake_ocr)
    text, method = ingest.extract_text(blank)
    assert method == "pdf+ocr"
    assert calls == [blank]
    assert "OCR RECOVERED" in text


# --- engine fallback ordering (no real engines involved) --------------------

def _fake_engine_success(engine, binary, tex_path, workdir):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(workdir / f"{tex_path.stem}.pdf", "wb") as fh:
        writer.write(fh)
    return "ok"


def test_cached_engine_failure_falls_back_to_remaining_engines(tmp_path, monkeypatch):
    engines = [("tectonic", "/fake/tectonic"), ("pdflatex", "/fake/pdflatex")]
    calls: list[str] = []

    def run(engine, binary, tex_path, workdir):
        calls.append(engine)
        if engine == "tectonic":
            raise latex.CompileError("tectonic failed (exit -6)")
        return _fake_engine_success(engine, binary, tex_path, workdir)

    monkeypatch.setattr(latex, "available_engines", lambda: list(engines))
    monkeypatch.setattr(latex, "_run_engine", run)
    monkeypatch.setattr(latex, "_working_engine", engines[0])  # cached: tectonic

    result = latex.compile_tex("x", tmp_path, jobname="doc")
    assert calls == ["tectonic", "pdflatex"]
    assert result.engine == "pdflatex"
    assert latex._working_engine == engines[1]


def test_cached_engine_tried_first(tmp_path, monkeypatch):
    engines = [("tectonic", "/fake/tectonic"), ("pdflatex", "/fake/pdflatex")]
    calls: list[str] = []

    def run(engine, binary, tex_path, workdir):
        calls.append(engine)
        return _fake_engine_success(engine, binary, tex_path, workdir)

    monkeypatch.setattr(latex, "available_engines", lambda: list(engines))
    monkeypatch.setattr(latex, "_run_engine", run)
    monkeypatch.setattr(latex, "_working_engine", engines[1])  # cached: pdflatex

    result = latex.compile_tex("x", tmp_path, jobname="doc")
    assert calls == ["pdflatex"]
    assert result.engine == "pdflatex"


def test_cached_engine_timeout_falls_back_to_remaining_engines(tmp_path, monkeypatch):
    """Regression: a hang (subprocess.TimeoutExpired) must become
    CompileError so the fallback loop reaches the next engine, instead of
    escaping as an unhandled exception."""
    import subprocess
    from types import SimpleNamespace

    from pypdf import PdfWriter

    engines = [("tectonic", "/fake/tectonic"), ("pdflatex", "/fake/pdflatex")]
    calls: list[str] = []

    def fake_run(command, **kwargs):
        calls.append(command[0])
        if command[0] == "/fake/tectonic":
            raise subprocess.TimeoutExpired(cmd=command, timeout=1)
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with open(kwargs["cwd"] / "doc.pdf", "wb") as fh:
            writer.write(fh)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex, "available_engines", lambda: list(engines))
    monkeypatch.setattr(latex.subprocess, "run", fake_run)
    monkeypatch.setattr(latex, "_working_engine", engines[0])  # cached: tectonic

    result = latex.compile_tex("x", tmp_path, jobname="doc")
    assert calls[0] == "/fake/tectonic"
    assert calls.count("/fake/pdflatex") == 2  # both pdflatex passes ran
    assert result.engine == "pdflatex"
    assert latex._working_engine == engines[1]


def test_corrupt_pdf_falls_back_to_remaining_engines(tmp_path, monkeypatch):
    engines = [("tectonic", "/fake/tectonic"), ("pdflatex", "/fake/pdflatex")]

    def run(engine, binary, tex_path, workdir):
        if engine == "tectonic":
            # Engine "succeeds" but emits garbage — unreadable PDF.
            (workdir / f"{tex_path.stem}.pdf").write_bytes(b"not a pdf")
            return "ok"
        return _fake_engine_success(engine, binary, tex_path, workdir)

    monkeypatch.setattr(latex, "available_engines", lambda: list(engines))
    monkeypatch.setattr(latex, "_run_engine", run)
    monkeypatch.setattr(latex, "_working_engine", None)

    result = latex.compile_tex("x", tmp_path, jobname="doc")
    assert result.engine == "pdflatex"


def test_launch_oserror_wraps_as_compile_error(tmp_path, monkeypatch):
    def raise_oserror(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(latex.subprocess, "run", raise_oserror)
    with pytest.raises(latex.CompileError, match="failed to launch"):
        latex._run_engine("tectonic", "/gone/tectonic", tmp_path / "doc.tex", tmp_path)


# --- live compile (needs a LaTeX engine; skipped when absent) ---------------

@pytest.mark.skipif(
    shutil.which("tectonic") is None and shutil.which("pdflatex") is None,
    reason="no LaTeX engine installed",
)
def test_sample_profile_compiles_to_one_page(tmp_path):
    result = latex.compile_tex(render(SAMPLE_PROFILE), tmp_path, jobname="resume")
    assert result.pages == 1
    assert result.engine in ("tectonic", "pdflatex")
    assert result.pdf_path.stat().st_size > 1000


@pytest.mark.skipif(
    shutil.which("tectonic") is None and shutil.which("pdflatex") is None,
    reason="no LaTeX engine installed",
)
def test_sample_profile_pdf_extracts_its_special_characters(tmp_path):
    """The PDF has to carry readable Unicode, not just look right — an
    ATS reads the text layer. pdfTeX gets that from glyphtounicode;
    tectonic (XeTeX) gets it from the OpenType fonts, and nothing else
    in the suite would notice a font swap that broke the mapping.

    Whitespace is engine-specific (kerning becomes spaces differently),
    so compare with it removed. Two characters are deliberately absent
    below: pdfTeX draws `\\_` as a rule and `\\~{}` as an accent, so those
    two do not survive extraction on every engine.
    """
    from pypdf import PdfReader

    result = latex.compile_tex(render(SAMPLE_PROFILE), tmp_path, jobname="resume")
    text = PdfReader(str(result.pdf_path)).pages[0].extract_text()
    packed = "".join(text.split())

    for expected in (
        "JaneDoe",
        "AI&LLMSystems",                       # LaTeX-escaped ampersand
        "jane@example.com",
        "Acme&Sons{Ltd}",                      # escaped braces
        "Cutcostsby50%usingC#&Rust",           # percent and hash
        "Shipped3serviceswith99.9%uptime",
        "Builta$100pipelinewith1-pageenforcement",   # escaped dollar
        "Dean’sList",                          # curly apostrophe
        "Mar2024–Present",                     # en dash
        "Python,TypeScript,C#",
    ):
        assert expected in packed, f"{expected!r} missing from extracted text"

    # Markdown emphasis markers are rendered, never printed.
    assert "**" not in packed


# --- engine smoke check (setup.sh's "can this engine build a resume?") ------

def test_engine_smoke_reports_a_failing_engine(tmp_path, monkeypatch):
    """A version number is not an answer: tectonic 0.16.9 typesets a
    one-line article and aborts (SIGABRT) on this template. The smoke
    check compiles the real template, so setup can say so."""
    from backend.tailor import smoke_pdf

    def boom(engine, binary, tex_path, workdir, jobname):
        raise latex.CompileError(f"{engine} failed (exit -6)", "note: Running TeX ...")

    monkeypatch.setattr(smoke_pdf.latex, "compile_with", boom)
    reason = smoke_pdf.check_engine("tectonic", "/fake/tectonic", tmp_path)
    assert reason is not None
    assert "exit -6" in reason and "Running TeX" in reason


def test_engine_smoke_rejects_a_pdf_with_no_text_layer(tmp_path, monkeypatch):
    """An engine that emits a blank page has not typeset anything —
    catching that is the whole point of reading the text back."""
    from pypdf import PdfWriter

    from backend.tailor import smoke_pdf

    blank = tmp_path / "smoke.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(blank, "wb") as fh:
        writer.write(fh)

    monkeypatch.setattr(
        smoke_pdf.latex, "compile_with",
        lambda *a, **k: latex.CompileResult(pdf_path=blank, pages=1, engine="fake"),
    )
    reason = smoke_pdf.check_engine("fake", "/fake/fake", tmp_path)
    assert reason == "produced a PDF with no readable text layer"


def test_engine_smoke_reports_no_engine_installed(monkeypatch, capsys):
    from backend.tailor import smoke_pdf

    monkeypatch.setattr(smoke_pdf.latex, "available_engines", list)
    assert smoke_pdf.main() == 2
    assert "no LaTeX engine installed" in capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("tectonic") is None and shutil.which("pdflatex") is None,
    reason="no LaTeX engine installed",
)
def test_engine_smoke_passes_with_a_real_engine(capsys):
    """The check setup.sh runs, against whatever is actually installed."""
    from backend.tailor import smoke_pdf

    assert smoke_pdf.main() == 0
    assert "ok" in capsys.readouterr().out
