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


def _transform(matrix, x: float, y: float) -> tuple[float, float]:
    """Apply a PDF affine matrix [a b c d e f] to a point."""
    a, b, c, d, e, f = (float(v) for v in matrix[:6])
    return a * x + c * y + e, b * x + d * y + f


# Operators that paint glyphs. `'` and `"` also move to the next line
# first, so their glyphs land one leading BELOW the matrix we are handed.
_SHOW_TEXT_OPS = (b"Tj", b"TJ", b"'", b'"')


def _shows_glyphs(operands: list) -> bool:
    """True when a text-showing operator carries at least one glyph.
    `TJ` mixes glyph strings with kerning numbers, and a run can be all
    kerning; the strings are font-encoded bytes, so this deliberately
    stops at "some glyph" rather than trying to decode them."""
    for operand in operands:
        if isinstance(operand, (bytes, str)) and operand:
            return True
        if isinstance(operand, list) and _shows_glyphs(operand):
            return True
    return False


def content_bounds(pdf_path: Path) -> list[dict | None]:
    """Per-page content bounding box {x0, y0, x1, y1} from text
    coordinates (PDF user space, bottom-left origin); None for a page
    with no text.

    Measured at the text-showing operators themselves, off pypdf's live
    graphics/text matrices. The obvious route — `visitor_text` — hands
    back MEMOIZED matrices that are only refreshed on some operators,
    and a `q .. cm .. Q` block inside a text object (pdfTeX emits one for
    every rule) leaves them stale at the identity: the following run then
    measures at the page origin, y0 collapses to 0, and last-page
    fullness reads ~0.96 for any document — silently disabling the
    sparse-page balance pass on pdfTeX output.
    """
    bounds: list[dict | None] = []
    for page in PdfReader(str(pdf_path)).pages:
        box = {"x0": float("inf"), "y0": float("inf"),
               "x1": float("-inf"), "y1": float("-inf")}
        seen = False
        state = {"font_size": 0.0, "leading": 0.0}

        def before(operator, operands, cm, tm,
                   box=box, state=state) -> None:  # noqa: ANN001 — pypdf callback shape
            nonlocal seen
            if operator == b"Tf" and len(operands) >= 2:
                state["font_size"] = float(operands[1])
                return
            if operator == b"TL" and operands:
                state["leading"] = float(operands[0])
                return
            if operator == b"TD" and len(operands) >= 2:
                state["leading"] = -float(operands[1])
                return
            if operator not in _SHOW_TEXT_OPS or not _shows_glyphs(operands):
                return
            tm = [float(v) for v in tm[:6]]
            if operator in (b"'", b'"'):  # implicit T* before showing
                tm[4] -= tm[2] * state["leading"]
                tm[5] -= tm[3] * state["leading"]
            # Text-space -> user space via the text matrix, then user space
            # -> page space via the current transformation matrix. Skipping
            # the cm step only looks right for engines that leave it at
            # identity: pdfTeX does, xdvipdfmx (XeTeX/tectonic) does not,
            # and its pages then measure as a third as full as they are.
            x, y = _transform(cm, *_transform(tm, 0.0, 0.0))
            _, y_top = _transform(cm, *_transform(tm, 0.0, state["font_size"]))
            seen = True
            box["x0"] = min(box["x0"], x)
            box["y0"] = min(box["y0"], y)
            box["x1"] = max(box["x1"], x)
            box["y1"] = max(box["y1"], y_top)

        page.extract_text(visitor_operand_before=before)
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


def resume_relaxed(layout: template.Layout) -> template.Layout:
    """One bounded step LOOSER — the balance direction for an underfilled
    page (tightening only empties it further). Ceilings sit one gentle
    notch past the proven defaults; font and margins never change."""
    return replace(
        layout,
        section_before_pt=min(14, layout.section_before_pt + 2),
        section_after_pt=min(12, layout.section_after_pt + 2),
        item_sep_pt=min(2, layout.item_sep_pt + 1),
        item_gap_pt=min(-1, layout.item_gap_pt + 1),
        itemlist_end_gap_pt=min(-5, layout.itemlist_end_gap_pt + 1),
    )


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
    condensed: bool = False  # cover only: the condense LLM call ran


# --- Persisted-fit recompiles (restart path, PLAN.md §4.6) -------------------
# A completed fit persists {tailored, layout, trimmed} in stage_json;
# these rebuild the PDF from exactly that state — pure template render +
# local compile, zero LLM — so a restart after fitting never repeats the
# condense/entailment spend.

def fit_state(fit: FitResult) -> dict:
    from dataclasses import asdict

    return {"tailored": fit.tailored, "layout": asdict(fit.layout),
            "trimmed": fit.trimmed, "condensed": fit.condensed}


def compile_resume_state(state: dict, profile: dict,
                         workdir: Path) -> latex.CompileResult:
    layout = template.Layout(**state["layout"])
    return _compile_resume(state["tailored"], profile, layout, workdir)


def compile_cover_state(state: dict, profile: dict, job: dict,
                        workdir: Path) -> latex.CompileResult:
    layout = cover_mod.CoverLayout(**state["layout"])
    letter = state["tailored"]["cover_letter"]
    tex = cover_mod.render_cover(profile, job, letter["paragraphs"], layout)
    return latex.compile_tex(tex, workdir, jobname="cover_letter")


def _compile_resume(tailored: dict, profile: dict, layout: template.Layout,
                    workdir: Path) -> latex.CompileResult:
    merged = tailor_resume.tailored_profile(profile, tailored)
    tex = template.render(merged, layout,
                          section_order=tailored["section_order"])
    return latex.compile_tex(tex, workdir, jobname="resume")


async def fit_resume(tailored: dict, profile: dict, ledger: dict,
                     entail_cache: dict, page_limit: int,
                     workdir: Path, *, record=None) -> FitResult:
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
                                           entail_cache, record=record)
        if failures:
            raise ValidationFailed(failures)
        result = _compile_resume(tailored, profile, layout, workdir)

    fullness = last_page_fullness(result.pdf_path)
    if fullness < SPARSE_FULLNESS:
        # One balance pass, kept only on MEASURED improvement — fewer
        # pages, or the same page count with a fuller last page. A
        # sparse multi-page tail gets one step TIGHTER (pull the orphan
        # up); a sparse page on an already-fitting document — including
        # the default one-page resume, the main user case — gets one
        # bounded step LOOSER (spread the content). Anything that fails
        # to measure better, or would exceed the limit, is discarded.
        if result.pages > 1:
            balanced_layout = resume_ladder(layout)[0]
        else:
            balanced_layout = resume_relaxed(layout)
        if balanced_layout != layout:  # at the ceilings: nothing to try
            balanced = _compile_resume(tailored, profile, balanced_layout,
                                       workdir)
            # A looser layout that spills onto a new page is never an
            # improvement (pages went up), so this also enforces the
            # page limit for the relax direction.
            improved = balanced.pages < result.pages or (
                balanced.pages == result.pages
                and last_page_fullness(balanced.pdf_path) > fullness)
            if improved:
                layout, result = balanced_layout, balanced
            else:
                # Restore the kept layout's PDF — the balanced compile
                # overwrote it at the same path.
                result = _compile_resume(tailored, profile, layout, workdir)

    return FitResult(result=result, layout=layout, tailored=tailored,
                     trimmed=trimmed)


# --- Cover fit ----------------------------------------------------------------

# Same paragraph shape as the generation schema — condensing must keep
# the citation discipline. (The closing is application-owned and not
# part of any LLM schema.)
_COVER_LETTER_SCHEMA = tailor_resume.TAILOR_SCHEMA["properties"]["cover_letter"]
_CONDENSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs"],
    "properties": {
        "paragraphs": _COVER_LETTER_SCHEMA["properties"]["paragraphs"],
    },
}

_CONDENSE_SYSTEM = (
    "You shorten a cover letter that overflows one page. Keep the same "
    "message and the same source_ids discipline — cut length, never add "
    "facts. Return only JSON."
)


async def fit_cover(tailored: dict, profile: dict, ledger: dict,
                    entail_cache: dict, job: dict,
                    workdir: Path, *, record=None, precondensed=None,
                    on_condensed=None) -> FitResult:
    """Cover letter must be exactly one page: bounded layout first, then
    one xhigh condense round (revalidated), then failure.

    `precondensed` is a previously persisted condense output: a resume
    after the condense call succeeded but a later step failed reuses it
    instead of paying the xhigh call again. `on_condensed(cover_letter)`
    fires the moment a LIVE condense call returns, BEFORE revalidation —
    the caller checkpoints the JSON and its call metadata so no
    downstream failure can lose the spend."""
    tailored = json.loads(json.dumps(tailored))
    layout = cover_mod.DEFAULT_COVER_LAYOUT
    condensed = False

    if precondensed is not None:
        # An already-paid condense output is AUTHORITATIVE: install it
        # before the first compile, so a changed engine or template that
        # now fits the original letter can never quietly ship content
        # the condense call was paid to replace. Layout is recomputed
        # freely below; the content is settled. Its claims still go
        # through validation (cached verdicts make this free unless the
        # original attempt died before they persisted).
        tailored["cover_letter"] = precondensed
        condensed = True
        failures = await validate.validate(tailored, profile, ledger,
                                           entail_cache, record=record)
        if failures:
            raise ValidationFailed(failures)

    def _compile(current_layout) -> latex.CompileResult:
        letter = tailored["cover_letter"]
        tex = cover_mod.render_cover(profile, job, letter["paragraphs"],
                                     current_layout)
        return latex.compile_tex(tex, workdir, jobname="cover_letter")

    result = _compile(layout)
    if result.pages > 1:
        for step in cover_ladder(layout):
            layout = step
            result = _compile(layout)
            if result.pages == 1:
                break

    if result.pages > 1 and not condensed:
        letter = tailored["cover_letter"]
        prompt = (
            f"# Cover letter to shorten ({result.pages} pages; must be 1)\n"
            f"{json.dumps(letter, indent=1)}\n\n"
            "# Valid source_ids\n"
            + "\n".join(f"- {fid}: {e['text']}" for fid, e in ledger.items())
            + "\n\nReturn the shortened paragraphs."
        )
        shorter = await llm.structured(prompt, _CONDENSE_SCHEMA,
                                       task="repair",
                                       system_prompt=_CONDENSE_SYSTEM)
        if on_condensed is not None:
            on_condensed(shorter)  # checkpoint BEFORE revalidation
        condensed = True
        tailored["cover_letter"] = shorter
        # Content rewrite: full revalidation before continuing.
        failures = await validate.validate(tailored, profile, ledger,
                                           entail_cache, record=record)
        if failures:
            raise ValidationFailed(failures)
        result = _compile(layout)

    if result.pages != 1:
        raise FitError(
            f"cover letter is {result.pages} pages at every readability "
            "floor after condensing (must be exactly 1)"
        )
    return FitResult(result=result, layout=layout, tailored=tailored,
                     trimmed=[], condensed=condensed)
