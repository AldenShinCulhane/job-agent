#!/usr/bin/env python3
"""Cross-engine geometry, font and text-layer evidence for the retained logs.

Compiles the sample resume and a sample cover letter with every engine
named on the command line, then reports for each:

  * page count and the measured content box
  * last-page fullness under this branch's algorithm and under the
    pre-`11edbca` one (kept here verbatim so the difference stays
    reproducible after the fix)
  * whether the text layer reads back the characters an ATS needs
  * pdffonts, when poppler is installed

Nothing here starts the app, touches `.data`, or calls the LLM.

    ./.venv/bin/python scripts/ci/engine_text_evidence.py \
        --engine pdflatex=pdflatex --engine tectonic=/path/to/tectonic

This host's own tectonic is 0.16.9, which aborts on the resume template;
pass the 0.17.0 binary by path to reproduce the tectonic column.
"""

import argparse
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from pypdf import PdfReader  # noqa: E402

from backend.resume.template import render  # noqa: E402
from backend.tailor import compile as latex  # noqa: E402
from backend.tailor import cover as cover_mod  # noqa: E402
from backend.tailor import page_limit  # noqa: E402
from backend.tests.test_resume import (  # noqa: E402
    _COVER_JOB, _COVER_PARAGRAPHS, _COVER_TEXT, _RESUME_TEXT, SAMPLE_PROFILE,
)


def old_content_bounds(pdf_path: Path) -> list[dict | None]:
    """`content_bounds` as it stood before `11edbca`: measured off the
    matrices pypdf hands `visitor_text`, which are memoized and go stale
    after a rule is drawn inside a text object."""
    bounds: list[dict | None] = []
    for page in PdfReader(str(pdf_path)).pages:
        box = {"x0": float("inf"), "y0": float("inf"),
               "x1": float("-inf"), "y1": float("-inf")}
        seen = False
        origin: list[str] = []

        def visitor(text, cm, tm, font_dict, font_size, box=box, origin=origin):
            nonlocal seen
            if not text.strip():
                return
            x, y = page_limit._transform(cm, *page_limit._transform(tm, 0.0, 0.0))
            _, y_top = page_limit._transform(
                cm, *page_limit._transform(tm, 0.0, float(font_size or 0)))
            if (x, y) == (0.0, 0.0):
                origin.append(text.strip())
            seen = True
            box["x0"] = min(box["x0"], x)
            box["y0"] = min(box["y0"], y)
            box["x1"] = max(box["x1"], x)
            box["y1"] = max(box["y1"], y_top)

        page.extract_text(visitor_text=visitor)
        box["_origin_runs"] = origin
        bounds.append(box if seen else None)
    return bounds


def old_fullness(pdf_path: Path) -> tuple[float, list[str]]:
    reader = PdfReader(str(pdf_path))
    box = old_content_bounds(pdf_path)[-1]
    if box is None:
        return 0.0, []
    height = float(reader.pages[-1].mediabox.height)
    return max(0.0, (box["y1"] - box["y0"]) / height), box["_origin_runs"]


def report(kind: str, tex: str, engine: str, binary: str, expected: tuple,
           workdir: Path) -> None:
    tex_path = workdir / f"{kind}.tex"
    tex_path.write_text(tex)
    try:
        result = latex.compile_with(engine, binary, tex_path, workdir, kind,
                                    timeout_s=latex.FIRST_RUN_TIMEOUT_S)
    except latex.CompileError as exc:
        print(f"=== {engine} / {kind}: COMPILE FAILED — {exc}")
        return

    reader = PdfReader(str(result.pdf_path))
    height = float(reader.pages[-1].mediabox.height)
    box = page_limit.content_bounds(result.pdf_path)[-1]
    new = page_limit.last_page_fullness(result.pdf_path)
    before, at_origin = old_fullness(result.pdf_path)
    packed = "".join(reader.pages[-1].extract_text().split())
    missing = [frag for frag in expected if frag not in packed]

    print(f"=== {engine} / {kind}: {result.pages} page(s), page height {height}")
    if box:
        print(f"    content box   x0={box['x0']:.3f} y0={box['y0']:.3f} "
              f"x1={box['x1']:.3f} y1={box['y1']:.3f}")
    print(f"    fullness, pre-11edbca algorithm = {before:.4f}")
    print(f"    fullness, this branch           = {new:.4f}")
    print(f"    SPARSE_FULLNESS = {page_limit.SPARSE_FULLNESS} -> balance pass "
          f"fires: pre={before < page_limit.SPARSE_FULLNESS} "
          f"now={new < page_limit.SPARSE_FULLNESS}")
    print(f"    runs the old algorithm placed at the page origin: {at_origin}")
    print(f"    text layer: {len(expected) - len(missing)}/{len(expected)} "
          f"required fragments present"
          + (f", MISSING {missing}" if missing else ""))
    for frag in expected:
        mark = "ok  " if frag in packed else "GONE"
        print(f"      [{mark}] {frag}")

    fonts = subprocess.run(["pdffonts", str(result.pdf_path)],
                           capture_output=True, text=True)
    print(fonts.stdout.strip() if fonts.returncode == 0
          else "    (pdffonts not installed)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", action="append", default=[],
                        metavar="NAME=BINARY",
                        help="engine to exercise, e.g. tectonic=/path/to/tectonic")
    args = parser.parse_args()
    engines = [tuple(spec.split("=", 1)) for spec in args.engine] or \
        latex.available_engines()

    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    ref = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Cross-engine geometry, font and text-layer evidence.")
    print()
    print(f"provenance: repo={REPO} ref={ref} head={head} date={now} "
          f"os={platform.system()}-{platform.mac_ver()[0] or platform.release()} "
          f"arch={platform.machine()} "
          f"worktree={'DIRTY — not SHA-bound' if dirty else 'clean'}")
    print("document:   backend/tests/test_resume.py::SAMPLE_PROFILE through "
          "backend/resume/template.py and backend/tailor/cover.py")
    print(f"engines:    {', '.join(f'{n} ({b})' for n, b in engines)}")
    print()

    documents = (
        ("resume", render(SAMPLE_PROFILE), _RESUME_TEXT),
        ("cover", cover_mod.render_cover(SAMPLE_PROFILE, _COVER_JOB,
                                         _COVER_PARAGRAPHS), _COVER_TEXT),
    )
    for engine, binary in engines:
        for kind, tex, expected in documents:
            with tempfile.TemporaryDirectory() as tmp:
                report(kind, tex, engine, binary, expected, Path(tmp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
