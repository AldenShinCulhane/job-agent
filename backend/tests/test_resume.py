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
