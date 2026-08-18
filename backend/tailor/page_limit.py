"""Geometry-aware N-page enforcement (PLAN.md §4.5 step 3).

Both documents compile with the actual selected engine and are measured
from the PDF itself — page count and content bounding boxes from text
coordinates, never line counts. Over the limit, bounded layout
parameters tighten first (each with a readability floor it never
crosses), then content trimming drops the LOWEST-RELEVANCE resume
bullets for this job (from the generation pass), recompiling and
re-measuring after every change. Every content change goes back through
truth validation before continuing; unchanged claims revalidate free
via the entailment cache. v1's shrink logic (shortest-bullet removal,
spacing string-replace) is explicitly not ported.
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

from pypdf import PdfReader

from backend.core import llm
from backend.resume import template
from backend.tailor import compile as latex
from backend.tailor import cover as cover_mod
from backend.tailor import resume as tailor_resume
from backend.tailor import validate

# Last-page fullness below this (content bbox height / page height) on a
# multi-page resume triggers one balance pass (PLAN.md §8: tune in M4).
SPARSE_FULLNESS = 0.35


class FitError(Exception):
    """The document cannot meet its page limit within readability floors
    and available trims — the package fails rather than ships oversized."""


class ValidationFailed(Exception):
    def __init__(self, failures: list[dict]):
        super().__init__(f"{len(failures)} claim(s) failed truth validation")
        self.failures = failures


def content_bounds(pdf_path: Path) -> list[dict | None]:
    """Per-page content bounding box {x0, y0, x1, y1} from text
    coordinates (PDF user space, bottom-left origin); None for a page
    with no text."""
    bounds: list[dict | None] = []
    for page in PdfReader(str(pdf_path)).pages:
        box = {"x0": float("inf"), "y0": float("inf"),
               "x1": float("-inf"), "y1": float("-inf")}
        seen = False

        def visitor(text, cm, tm, font_dict, font_size,
                    box=box) -> None:  # noqa: ANN001 — pypdf callback shape
            nonlocal seen
            if not text.strip():
                return
            x, y = float(tm[4]), float(tm[5])
            seen = True
            box["x0"] = min(box["x0"], x)
            box["y0"] = min(box["y0"], y)
            box["x1"] = max(box["x1"], x)
            box["y1"] = max(box["y1"], y + float(font_size or 0))

        page.extract_text(visitor_text=visitor)
        bounds.append(box if seen else None)
    return bounds


def last_page_fullness(pdf_path: Path) -> float:
    """Content-height coverage of the last page, 0..1."""
    reader = PdfReader(str(pdf_path))
    box = content_bounds(pdf_path)[-1]
    if box is None:
        return 0.0
    height = float(reader.pages[-1].mediabox.height)
    return max(0.0, (box["y1"] - box["y0"]) / height)


# --- Bounded layout ladders (readability floors from the proven template) ----

def resume_ladder(layout: template.Layout) -> list[template.Layout]:
    """Successively tighter layouts, floors never crossed: spacing first,
    margins second, font size last."""
    spacing = replace(
        layout,
        section_before_pt=max(8, layout.section_before_pt - 2),
        section_after_pt=max(6, layout.section_after_pt - 2),
        item_sep_pt=0,
        item_gap_pt=max(-4, layout.item_gap_pt - 1),
        itemlist_end_gap_pt=max(-8, layout.itemlist_end_gap_pt - 1),
    )
    margins = replace(
        spacing,
        side_margin_add_in=min(0.6, spacing.side_margin_add_in + 0.1),
        top_margin_add_in=min(0.6, spacing.top_margin_add_in + 0.1),
        text_height_add_in=min(1.45, spacing.text_height_add_in + 0.2),
    )
    font = replace(margins, font_pt=max(10, margins.font_pt - 1))
    return [spacing, margins, font]


def cover_ladder(layout: cover_mod.CoverLayout) -> list[cover_mod.CoverLayout]:
    tighter = replace(layout, par_skip_pt=max(4, layout.par_skip_pt - 2))
    narrow = replace(tighter, margin_in=max(0.85, tighter.margin_in - 0.15))
    narrower = replace(narrow, margin_in=max(0.7, narrow.margin_in - 0.15))
    small = replace(narrower, font_pt=max(10, narrower.font_pt - 1))
    return [tighter, narrow, narrower, small]


# --- Resume fit ---------------------------------------------------------------

def _trimmable(tailored: dict) -> list[tuple[float, str, int, int]]:
    """(relevance, section key, entry position, bullet position) for every
    bullet that may still be dropped — entries keep at least one bullet."""
    candidates = []
    for key in ("experiences", "projects"):
        for pos, entry in enumerate(tailored.get(key, [])):
            if len(entry["bullets"]) <= 1:
                continue
            for i, bullet in enumerate(entry["bullets"]):
                candidates.append((bullet["relevance"], key, pos, i))
    return sorted(candidates)


@dataclass
class FitResult:
    result: latex.CompileResult
    layout: object
    tailored: dict
    trimmed: list[str]


def _compile_resume(tailored: dict, profile: dict, layout: template.Layout,
                    workdir: Path) -> latex.CompileResult:
    merged = tailor_resume.tailored_profile(profile, tailored)
    tex = template.render(merged, layout,
                          section_order=tailored["section_order"])
    return latex.compile_tex(tex, workdir, jobname="resume")


async def fit_resume(tailored: dict, profile: dict, ledger: dict,
                     entail_cache: dict, page_limit: int,
                     workdir: Path) -> FitResult:
    """Compile → tighten layout within floors → trim lowest-relevance
    bullets (revalidating after every trim) → sparse-page balance.
    Raises FitError when the limit is unreachable."""
    tailored = json.loads(json.dumps(tailored))  # owned copy; trims mutate
    layout = template.DEFAULT_LAYOUT
    result = _compile_resume(tailored, profile, layout, workdir)
    trimmed: list[str] = []

    if result.pages > page_limit:
        for step in resume_ladder(layout):
            layout = step
            result = _compile_resume(tailored, profile, layout, workdir)
            if result.pages <= page_limit:
                break

    while result.pages > page_limit:
        candidates = _trimmable(tailored)
        if not candidates:
            raise FitError(
                f"resume still {result.pages} pages at every readability "
                f"floor with nothing left to trim (limit {page_limit})"
            )
        _, key, pos, i = candidates[0]
        dropped = tailored[key][pos]["bullets"].pop(i)
        trimmed.append(dropped["text"])
        # A trim is a content change: revalidate before continuing.
        # Unchanged claims hit the entailment cache — zero LLM calls.
        failures = await validate.validate(tailored, profile, ledger,
                                           entail_cache)
        if failures:
            raise ValidationFailed(failures)
        result = _compile_resume(tailored, profile, layout, workdir)

    if result.pages > 1 and last_page_fullness(result.pdf_path) < SPARSE_FULLNESS:
        # One balance pass: the next unused ladder step, kept only if it
        # actually helps (fewer pages, or the same).
        ladder = resume_ladder(layout)
        balanced_layout = ladder[0]
        balanced = _compile_resume(tailored, profile, balanced_layout, workdir)
        if balanced.pages <= result.pages:
            layout, result = balanced_layout, balanced

    return FitResult(result=result, layout=layout, tailored=tailored,
                     trimmed=trimmed)


# --- Cover fit ----------------------------------------------------------------

# Same paragraph shape as the generation schema — condensing must keep
# the citation discipline.
_COVER_LETTER_SCHEMA = tailor_resume.TAILOR_SCHEMA["properties"]["cover_letter"]
_CONDENSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs", "closing"],
    "properties": {
        "paragraphs": _COVER_LETTER_SCHEMA["properties"]["paragraphs"],
        "closing": {"type": "string", "minLength": 1},
    },
}

_CONDENSE_SYSTEM = (
    "You shorten a cover letter that overflows one page. Keep the same "
    "message and the same source_ids discipline — cut length, never add "
    "facts. Return only JSON."
)


async def fit_cover(tailored: dict, profile: dict, ledger: dict,
                    entail_cache: dict, job: dict,
                    workdir: Path) -> FitResult:
    """Cover letter must be exactly one page: bounded layout first, then
    one xhigh condense round (revalidated), then failure."""
    tailored = json.loads(json.dumps(tailored))
    layout = cover_mod.DEFAULT_COVER_LAYOUT

    def _compile(current_layout) -> latex.CompileResult:
        letter = tailored["cover_letter"]
        tex = cover_mod.render_cover(profile, job, letter["paragraphs"],
                                     letter["closing"], current_layout)
        return latex.compile_tex(tex, workdir, jobname="cover_letter")

    result = _compile(layout)
    if result.pages > 1:
        for step in cover_ladder(layout):
            layout = step
            result = _compile(layout)
            if result.pages == 1:
                break

    if result.pages > 1:
        letter = tailored["cover_letter"]
        prompt = (
            f"# Cover letter to shorten ({result.pages} pages; must be 1)\n"
            f"{json.dumps(letter, indent=1)}\n\n"
            "# Valid source_ids\n"
            + "\n".join(f"- {fid}: {e['text']}" for fid, e in ledger.items())
            + "\n\nReturn the shortened paragraphs and closing."
        )
        condensed = await llm.structured(prompt, _CONDENSE_SCHEMA,
                                         task="repair",
                                         system_prompt=_CONDENSE_SYSTEM)
        tailored["cover_letter"] = condensed
        # Content rewrite: full revalidation before continuing.
        failures = await validate.validate(tailored, profile, ledger,
                                           entail_cache)
        if failures:
            raise ValidationFailed(failures)
        result = _compile(layout)

    if result.pages != 1:
        raise FitError(
            f"cover letter is {result.pages} pages at every readability "
            "floor after condensing (must be exactly 1)"
        )
    return FitResult(result=result, layout=layout, tailored=tailored,
                     trimmed=[])
