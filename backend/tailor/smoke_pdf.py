"""Prove every installed LaTeX engine can typeset THIS template.

`tectonic --version` says nothing about whether tectonic can build a
resume. 0.16.9 compiles a one-line article happily and aborts on the
template's fonts, so a version check calls it good on a machine where
every package would fail to produce a PDF. This renders the real
template — fontspec/glyphtounicode branch, FontAwesome contact icons,
LaTeX-escaped `& % # _ $`, markdown emphasis — through each engine in
turn and checks the result is a readable one-page PDF with a text layer.

    uv run python -m backend.tailor.smoke_pdf

Exit codes: 0 at least one engine works · 1 engines present, none work ·
2 no engine installed. No network, no LLM, no `.data` — safe to run at
setup time and in CI.
"""

import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader

from backend.resume import template
from backend.tailor import compile as latex

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
         "bullets": ["Cut costs by 50% with C# and Rust_v2 for $100",
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


def check_engine(engine: str, binary: str, workdir: Path) -> str | None:
    """None when the engine typesets the template; otherwise the reason."""
    tex_path = workdir / "smoke.tex"
    tex_path.write_text(template.render(SMOKE_PROFILE))
    try:
        result = latex.compile_with(engine, binary, tex_path, workdir, "smoke")
    except latex.CompileError as exc:
        tail = " | ".join(exc.log_tail.strip().splitlines()[-3:])
        return f"{exc}{' — ' + tail if tail else ''}"
    if result.pages != 1:
        return f"produced {result.pages} pages for a one-page document"
    text = PdfReader(str(result.pdf_path)).pages[0].extract_text()
    if "Smoke" not in "".join(text.split()):
        return "produced a PDF with no readable text layer"
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
