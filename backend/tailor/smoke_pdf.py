"""Prove every installed LaTeX engine can typeset BOTH real templates.

`tectonic --version` says nothing about whether tectonic can build a
resume. 0.16.9 compiles a one-line article happily and aborts on the
template's fonts, so a version check calls it good on a machine where
every package would fail to produce a PDF. This renders the real resume
template — fontspec/glyphtounicode branch, FontAwesome contact icons,
LaTeX-escaped `& % # _ $`, markdown emphasis — and the real cover-letter
template through each engine in turn, and checks each result is a
one-page PDF whose text layer reads back what was typeset.

    uv run python -m backend.tailor.smoke_pdf

Exit codes: 0 at least one engine works · 1 engines present, none work ·
2 no engine installed. No LLM and no `.data`, so it is safe to run at
setup time and in CI. It is not offline: tectonic downloads its support
bundle during its first compile on a machine, which is why the compiles
below get FIRST_RUN_TIMEOUT_S rather than the usual budget.
"""

import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader

from backend.resume import template
from backend.tailor import compile as latex
from backend.tailor import cover as cover_mod

# Sanitized throughout: invented person, invented company. The strings
# carry the characters the template has to escape.
SMOKE_PROFILE = {
    "name": "Smoke Test",
    "headline": "Engine Check | PDF Generation",
    "contact": {"phone": "555-000-0000", "email": "smoke@example.com",
                "linkedin": "linkedin.com/in/example", "github": None,
                "website": None, "location": "Somewhere, ON"},
    "experiences": [
        {"title": "Engineer", "company": "Example & Co {Ltd}", "location": None,
         "start": "Jan 2025", "end": "Present",
         "bullets": ["Cut costs by 50% with C# and Rust_v2 for $100 (~2 hours)",
                     "Shipped **3 services** at 99.9% uptime"]},
    ],
    "projects": [],
    "education": [
        {"institution": "Example University", "location": None,
         "degree": "BSc Computer Science", "start": None, "end": "2024",
         "details": []},
    ],
    "skills": [{"category": "Languages", "items": ["Python", "C#"]}],
}

SMOKE_JOB = {"company": "Example & Co {Ltd}", "title": "Engineer"}

SMOKE_PARAGRAPHS = [
    {"text": "I cut costs by 50% with C# and Rust_v2 for $100 (~2 hours).",
     "source_ids": []},
    {"text": "Shipping 3 services at 99.9% uptime is the work I want more of.",
     "source_ids": []},
]

# What the text layer must read back. An ATS reads these characters, not
# the glyphs: in pdfTeX's OT1 encoding `_` is a rule and `~` an accent,
# and both disappear here rather than in anything a human would notice.
_MUST_EXTRACT = ("Rust_v2", "$100(~2hours)")


def _check_document(kind: str, tex: str, engine: str, binary: str,
                    workdir: Path) -> str | None:
    """None when this engine typesets `tex` into a readable page."""
    tex_path = workdir / f"{kind}.tex"
    tex_path.write_text(tex)
    try:
        result = latex.compile_with(engine, binary, tex_path, workdir, kind,
                                    timeout_s=latex.FIRST_RUN_TIMEOUT_S)
    except latex.CompileError as exc:
        tail = " | ".join(exc.log_tail.strip().splitlines()[-3:])
        return f"{kind}: {exc}{' — ' + tail if tail else ''}"
    if result.pages != 1:
        return f"{kind}: produced {result.pages} pages for a one-page document"
    packed = "".join(PdfReader(str(result.pdf_path)).pages[0].extract_text().split())
    missing = [frag for frag in _MUST_EXTRACT if frag not in packed]
    if missing:
        if not packed:
            return f"{kind}: produced a PDF with no readable text layer"
        return (f"{kind}: text layer drops {', '.join(missing)} — an ATS would "
                "read the same PDF with those characters missing")
    return None


def check_engine(engine: str, binary: str, workdir: Path) -> str | None:
    """None when the engine typesets both templates; otherwise the reason.

    Both, because a package is a resume AND a cover letter: an engine that
    builds one and fails the other still ships nothing.
    """
    documents = (
        ("resume", template.render(SMOKE_PROFILE)),
        ("cover", cover_mod.render_cover(SMOKE_PROFILE, SMOKE_JOB,
                                         SMOKE_PARAGRAPHS)),
    )
    for kind, tex in documents:
        reason = _check_document(kind, tex, engine, binary, workdir)
        if reason is not None:
            return reason
    return None


def main() -> int:
    engines = latex.available_engines()
    if not engines:
        print("no LaTeX engine installed (tectonic or pdflatex)")
        return 2

    working = []
    for engine, binary in engines:
        with tempfile.TemporaryDirectory() as tmp:
            reason = check_engine(engine, binary, Path(tmp))
        if reason is None:
            working.append(engine)
            print(f"{engine}: ok")
        else:
            print(f"{engine}: FAILED — {reason}")
    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
