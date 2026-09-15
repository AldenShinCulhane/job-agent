"""Tailoring pipeline tests (PLAN.md §4.5/§4.6, M4). All data fake."""

import json
import sqlite3

import pytest

from backend.core import db
from backend.tests.pdfbuild import one_page_pdf

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


def _create_package(job_id, profile_version, jd_hash, page_limit=1):
    """db.create_package pinned to the running code's template versions —
    what the gate-3 route does, so recovery does not read these packages
    as built by a superseded template."""
    from backend.tailor import pipeline as tailor_pipeline

    resume_tpl, cover_tpl = tailor_pipeline.template_versions()
    return db.create_package(job_id, profile_version, jd_hash, page_limit,
                             template_version=resume_tpl,
                             cover_template_version=cover_tpl)


def _seed_job(job_id="p1", jd_hash="hash-a"):
    db.upsert_job({
        "job_id": job_id, "title": "Backend Engineer",
        "company": "Example Widgets Inc.", "location": "Toronto, ON",
        "posted_text": None, "workplace": None, "easy_apply": False,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
    })
    db.save_job_detail(job_id, "We need Python.", jd_hash)
    return job_id


# --- Package rows: identity, idempotence, state persistence ------------------

def test_package_identity_is_job_profile_hash(test_db):
    """One package per (job, profile version, JD hash, page limit,
    template versions): re-creating returns the existing row; any
    changed input creates a new package."""
    _seed_job()
    pkg1, created1 = _create_package("p1", 4, "hash-a")
    pkg2, created2 = _create_package("p1", 4, "hash-a")
    assert created1 is True and created2 is False
    assert pkg1 == pkg2

    pkg3, created3 = _create_package("p1", 5, "hash-a")  # new profile
    pkg4, created4 = _create_package("p1", 4, "hash-b")  # edited posting
    pkg5, created5 = _create_package("p1", 4, "hash-a", 2)  # new page limit
    # An upgraded resume template, then an upgraded cover template. Each
    # is a different document, so each is a different package.
    pkg6, created6 = db.create_package("p1", 4, "hash-a", 1,
                                       template_version=99,
                                       cover_template_version=1)
    pkg7, created7 = db.create_package("p1", 4, "hash-a", 1,
                                       template_version=99,
                                       cover_template_version=99)
    assert all([created3, created4, created5, created6, created7])
    assert len({pkg1, pkg3, pkg4, pkg5, pkg6, pkg7}) == 6

    row = db.get_package(pkg6)
    assert row["template_version"] == 99 and row["cover_template_version"] == 1


def test_package_update_and_stage_roundtrip(test_db):
    _seed_job()
    pkg, _ = _create_package("p1", 4, "hash-a")
    row = db.get_package(pkg)
    assert row["status"] == "queued"
    assert row["stage"] == {} and row["provenance"] == {}

    db.update_package(pkg, status="generating")
    db.update_package(pkg, status="validating",
                      stage={"tailored": {"headline": None}})
    db.update_package(pkg, provenance={"model": "claude-fable-5",
                                       "engine": "pdflatex"})
    row = db.get_package(pkg)
    assert row["status"] == "validating"
    assert row["stage"]["tailored"] == {"headline": None}
    assert row["provenance"]["engine"] == "pdflatex"

    # error uses the sentinel: set, then explicitly clear.
    db.update_package(pkg, status="failed", error="boom")
    assert db.get_package(pkg)["error"] == "boom"
    db.update_package(pkg, status="queued", error=None, retry_count=1)
    row = db.get_package(pkg)
    assert row["error"] is None and row["retry_count"] == 1


def test_list_packages_carries_job_display_fields(test_db):
    _seed_job("p1")
    _seed_job("p2", jd_hash="hash-b")
    _create_package("p1", 4, "hash-a")
    _create_package("p2", 4, "hash-b")
    rows = db.list_packages()
    assert [r["job_id"] for r in rows] == ["p2", "p1"]  # newest first
    assert rows[0]["company"] == "Example Widgets Inc."
    assert rows[0]["title"] == "Backend Engineer"


# --- Generation pass (schema, prompt, merge) ---------------------------------

PROFILE = {
    "name": "Casey Example",
    "headline": "Software Engineer",
    "contact": {"phone": None, "email": "casey@example.com", "linkedin": None,
                "github": None, "website": None, "location": "Toronto, ON"},
    "experiences": [
        {"title": "Software Engineer", "company": "Example Widgets Inc.",
         "location": None, "start": "Jun 2024", "end": "Present",
         "bullets": ["Built internal tools with Python", "Ran deployments"]},
    ],
    "projects": [
        {"name": "Sample Project", "technologies": ["Python"], "date": None,
         "link": None, "bullets": ["Wrote a CLI tool"]},
    ],
    "education": [
        {"institution": "Example University", "location": None,
         "degree": "BSc Computer Science", "start": None, "end": "2024",
         "details": []},
    ],
    "skills": [
        {"category": "Languages", "items": ["Python", "TypeScript"]},
        {"category": "Tools", "items": ["Docker"]},
    ],
}

JOB = {"job_id": "p1", "title": "Backend Engineer",
       "company": "Sample Systems & Co", "url": "https://example.invalid/p1",
       "description": "We need Python."}


def _tailored(**overrides):
    base = {
        "headline": None,
        "section_order": ["experience", "projects", "skills", "education"],
        "skills_order": ["Tools", "Languages"],
        "experiences": [{"index": 0, "bullets": [
            {"text": "Built internal tools with Python",
             "source_ids": ["b-1"], "relevance": 90},
        ]}],
        "projects": [{"index": 0, "bullets": [
            {"text": "Wrote a CLI tool", "source_ids": ["b-2"], "relevance": 40},
        ]}],
        "cover_letter": {
            "paragraphs": [
                {"text": "I build backend tools in Python.", "source_ids": ["b-1"]},
                {"text": "I would love to join your team.", "source_ids": []},
            ],
        },
    }
    base.update(overrides)
    return base


def test_tailor_schema_is_valid_and_accepts_shape():
    from jsonschema import Draft202012Validator

    from backend.tailor import resume as tailor_resume

    Draft202012Validator.check_schema(tailor_resume.TAILOR_SCHEMA)
    Draft202012Validator(tailor_resume.TAILOR_SCHEMA).validate(_tailored())
    # The closing is application-owned: no LLM schema may accept one.
    letter = tailor_resume.TAILOR_SCHEMA["properties"]["cover_letter"]
    assert "closing" not in letter["properties"]


async def test_generate_tailored_calls_llm_with_ledger_and_rules(monkeypatch):
    from backend.core import llm
    from backend.resume import facts
    from backend.tailor import resume as tailor_resume

    seen = {}

    async def fake_structured(prompt, schema, task, system_prompt=""):
        seen.update(prompt=prompt, schema=schema, task=task,
                    system_prompt=system_prompt)
        return _tailored()

    monkeypatch.setattr(llm, "structured", fake_structured)
    ledger = facts.build_ledger(PROFILE)
    result = await tailor_resume.generate_tailored(
        PROFILE, ledger, JOB, {"required_skills": ["Python"]},
        {"matched_skills": ["Python"]}, page_limit=1,
    )
    assert result == _tailored()
    assert seen["task"] == "tailor"
    assert seen["schema"] is tailor_resume.TAILOR_SCHEMA
    assert "never invent" in seen["system_prompt"].lower()
    assert "We need Python." in seen["prompt"]
    for fact_id in ledger:  # every valid source_id is offered to the model
        assert fact_id in seen["prompt"]


def test_tailored_profile_merges_only_llm_owned_fields():
    from backend.tailor import resume as tailor_resume

    merged = tailor_resume.tailored_profile(PROFILE, _tailored())
    # Structure the LLM cannot touch is verbatim from the profile.
    assert merged["experiences"][0]["company"] == "Example Widgets Inc."
    assert merged["education"] == PROFILE["education"]
    assert merged["contact"] == PROFILE["contact"]
    # LLM-owned: bullets replaced, skills reordered, headline kept (null).
    assert merged["experiences"][0]["bullets"] == [
        "Built internal tools with Python"]
    assert [g["category"] for g in merged["skills"]] == ["Tools", "Languages"]
    assert merged["headline"] == "Software Engineer"
    # And the original profile object is never mutated.
    assert PROFILE["skills"][0]["category"] == "Languages"

    with_headline = tailor_resume.tailored_profile(
        PROFILE,
        _tailored(headline={"text": "Backend Engineer", "source_ids": ["h-1"]}),
    )
    assert with_headline["headline"] == "Backend Engineer"


def test_render_honors_section_order_and_ignores_unknown():
    from backend.resume import template

    tex = template.render(PROFILE, section_order=["skills", "experience",
                                                  "bogus", "education",
                                                  "projects"])
    positions = [tex.index(marker) for marker in
                 ("Technical Skills", "Experience", "Education",
                  "Personal Projects")]
    assert positions == sorted(positions)


def test_cover_render_escapes_and_structures():
    from backend.tailor import cover

    tex = cover.render_cover(
        PROFILE, JOB,
        paragraphs=[{"text": "I use C&C 100% of the time.", "source_ids": []},
                    {"text": r"No \textbf{injection} allowed.", "source_ids": []}],
    )
    assert "Dear Sample Systems \\& Co Hiring Team," in tex
    assert r"C\&C 100\% of the time" in tex
    assert r"\textbackslash{}textbf" in tex  # LLM LaTeX stays literal text
    assert tex.count("Casey Example") == 2  # header + signature
    assert "casey@example.com" in tex
    assert "source_ids" not in tex  # internal metadata never rendered
    assert cover.CLOSING in tex  # app-owned closing, not LLM output


# --- Truth validation (deterministic + batched entailment) -------------------

def _ledger_and_ids():
    from backend.resume import facts

    ledger = facts.build_ledger(PROFILE)
    by_text = {entry["text"]: fid for fid, entry in ledger.items()}
    return ledger, by_text


def test_validator_rejects_unknown_source_ids():
    from backend.tailor import validate

    ledger, _ = _ledger_and_ids()
    tailored = _tailored()
    tailored["experiences"][0]["bullets"][0]["source_ids"] = ["nope-123"]
    failures = validate.deterministic_failures(tailored, PROFILE, ledger)
    assert any("unknown source_ids" in f["reason"] for f in failures)


def test_validator_grounds_numbers_in_cited_facts():
    from backend.tailor import validate

    ledger, by_text = _ledger_and_ids()
    bullet_id = by_text["Built internal tools with Python"]

    tailored = _tailored()
    tailored["experiences"][0]["bullets"][0] = {
        "text": "Cut costs by 300% with Python tools",
        "source_ids": [bullet_id], "relevance": 90,
    }
    failures = validate.deterministic_failures(tailored, PROFILE, ledger)
    assert any("300" in f["reason"] for f in failures)

    # An uncited number in a cover paragraph is equally rejected.
    tailored = _tailored()
    tailored["cover_letter"]["paragraphs"][1] = {
        "text": "I shipped 12 services.", "source_ids": []}
    failures = validate.deterministic_failures(tailored, PROFILE, ledger)
    assert any(f["claim_id"] == "cover:p1" for f in failures)

    # Numbers present in the cited fact pass ("30" must not match "300").
    tailored, ledger_full = _cited_tailored()
    ledger_full["m-1"] = {"kind": "bullet",
                          "text": "Reduced load time 300 ms", "path": "/x"}
    tailored["experiences"][0]["bullets"][0] = {
        "text": "Reduced load time by 300 ms", "source_ids": ["m-1"],
        "relevance": 80,
    }
    assert validate.deterministic_failures(tailored, PROFILE, ledger_full) == []
    tailored["experiences"][0]["bullets"][0]["text"] = "Reduced load time 30 ms"
    failures = validate.deterministic_failures(tailored, PROFILE, ledger_full)
    assert any("30" in f["reason"] for f in failures)


def test_validator_rejects_structural_drift():
    from backend.tailor import validate

    ledger, _ = _ledger_and_ids()
    tailored = _tailored(section_order=["experience", "experience",
                                        "projects", "skills"])
    assert any(f["claim_id"] == "section_order"
               for f in validate.deterministic_failures(tailored, PROFILE, ledger))

    tailored = _tailored(skills_order=["Languages", "Made Up"])
    assert any(f["claim_id"] == "skills_order"
               for f in validate.deterministic_failures(tailored, PROFILE, ledger))

    tailored = _tailored()
    tailored["experiences"][0]["index"] = 7
    assert any("out of range" in f["reason"]
               for f in validate.deterministic_failures(tailored, PROFILE, ledger))


def _cited_tailored():
    """A _tailored() whose source_ids all exist in the real ledger."""
    ledger, by_text = _ledger_and_ids()
    tailored = _tailored()
    tailored["experiences"][0]["bullets"][0]["source_ids"] = [
        by_text["Built internal tools with Python"]]
    tailored["projects"][0]["bullets"][0]["source_ids"] = [
        by_text["Wrote a CLI tool"]]
    tailored["cover_letter"]["paragraphs"][0]["source_ids"] = [
        by_text["Built internal tools with Python"]]
    return tailored, ledger


async def test_entailment_batches_caches_and_fails_closed(monkeypatch):
    from backend.core import llm
    from backend.tailor import validate

    tailored, ledger = _cited_tailored()
    calls = []

    async def fake_structured(prompt, schema, task, system_prompt=""):
        calls.append(task)
        # Support everything except the first experience bullet; return
        # NO verdict for cover:p1 — silence must fail closed.
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [
            {"claim_id": cid,
             "supported": cid != "exp0:b0",
             "reason": "checked"}
            for cid in claim_ids if cid != "cover:p1"
        ]}

    monkeypatch.setattr(llm, "structured", fake_structured)
    cache: dict[str, dict] = {}
    failures = await validate.validate(tailored, PROFILE, ledger, cache)
    assert calls == ["entail"]  # ONE batched pass for both documents
    failed_ids = {f["claim_id"] for f in failures}
    assert "exp0:b0" in failed_ids  # unsupported verdict
    assert "cover:p1" in failed_ids  # missing verdict fails closed

    # EVERY verdict was cached — including the negative one for the
    # claim the model skipped — so revalidation is fully free and
    # stable: same failures, zero LLM calls.
    failures2 = await validate.validate(tailored, PROFILE, ledger, cache)
    assert calls == ["entail"]  # no second call for anything
    assert {"exp0:b0", "cover:p1"} <= {f["claim_id"] for f in failures2}


async def test_validate_short_circuits_on_deterministic_failure(monkeypatch):
    from backend.core import llm
    from backend.tailor import validate

    async def exploding_structured(*a, **k):
        raise AssertionError("LLM must not be called for structural failures")

    monkeypatch.setattr(llm, "structured", exploding_structured)
    ledger, _ = _ledger_and_ids()
    tailored = _tailored()
    tailored["experiences"][0]["bullets"][0]["source_ids"] = ["nope-1"]
    failures = await validate.validate(tailored, PROFILE, ledger, {})
    assert failures  # reported without any LLM spend


async def test_repair_round_uses_xhigh_and_full_schema(monkeypatch):
    from backend.core import llm
    from backend.tailor import resume as tailor_resume
    from backend.tailor import validate

    tailored, ledger = _cited_tailored()
    seen = {}

    async def fake_structured(prompt, schema, task, system_prompt=""):
        seen.update(schema=schema, task=task, prompt=prompt)
        return tailored

    monkeypatch.setattr(llm, "structured", fake_structured)
    result = await validate.repair(
        _tailored(), [{"claim_id": "exp0:b0", "reason": "unsupported"}],
        PROFILE, ledger,
    )
    assert result == tailored
    assert seen["task"] == "repair"
    assert seen["schema"] is tailor_resume.TAILOR_SCHEMA
    assert "exp0:b0" in seen["prompt"]


# --- Geometry-aware page limits (fit loops with a fake engine) ---------------

def _fake_compiler(monkeypatch, pages_fn):
    """Replace the LaTeX engine with a deterministic page counter. Fake
    PDFs are plain text pypdf cannot parse, so fullness defaults to
    "not sparse" — tests exercising the balance pass override it."""
    from backend.tailor import compile as latex
    from backend.tailor import page_limit

    monkeypatch.setattr(page_limit, "last_page_fullness", lambda p: 0.8)
    calls = []

    def fake_compile(tex, workdir, jobname="document"):
        calls.append((jobname, tex))
        workdir.mkdir(parents=True, exist_ok=True)
        pdf = workdir / f"{jobname}.pdf"
        pdf.write_text(tex)  # content-bearing: byte comparisons see input
        return latex.CompileResult(pdf_path=pdf, pages=pages_fn(tex),
                                   engine="fake")

    monkeypatch.setattr(latex, "compile_tex", fake_compile)
    return calls


def _rich_tailored():
    """Four resume bullets with distinct relevance for trim ordering."""
    tailored, ledger = _cited_tailored()
    sid = tailored["experiences"][0]["bullets"][0]["source_ids"]
    tailored["experiences"][0]["bullets"] = [
        {"text": "Most relevant work", "source_ids": sid, "relevance": 95},
        {"text": "Least relevant work", "source_ids": sid, "relevance": 10},
        {"text": "Mid relevant work", "source_ids": sid, "relevance": 50},
    ]
    return tailored, ledger


async def test_fit_resume_within_limit_compiles_once(tmp_path, monkeypatch):
    from backend.tailor import page_limit

    calls = _fake_compiler(monkeypatch, lambda tex: 1)
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    assert len(calls) == 1
    assert fit.trimmed == [] and fit.result.pages == 1
    assert fit.layout == __import__(
        "backend.resume.template", fromlist=["DEFAULT_LAYOUT"]).DEFAULT_LAYOUT


async def test_fit_resume_tightens_layout_before_touching_content(
    tmp_path, monkeypatch
):
    from backend.tailor import page_limit

    # Fits as soon as item spacing hits its floor — a layout-only fix.
    calls = _fake_compiler(
        monkeypatch, lambda tex: 1 if "itemsep=0pt" in tex else 2)
    tailored, ledger = _rich_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    assert fit.trimmed == []  # content untouched
    assert fit.layout.item_sep_pt == 0
    assert fit.layout.font_pt == 11  # later ladder steps never ran
    assert len(calls) == 2


async def test_fit_resume_trims_lowest_relevance_and_revalidates(
    tmp_path, monkeypatch
):
    from backend.tailor import page_limit, validate

    # Only fits once two bullets are gone — layout alone can't fix it.
    calls = _fake_compiler(
        monkeypatch,
        lambda tex: 1 if tex.count("\\resumeItem{") <= 2 else 2)
    revalidations = []

    async def fake_validate(tailored, profile, ledger, cache, record=None):
        revalidations.append(len(revalidations))
        return []

    monkeypatch.setattr(validate, "validate", fake_validate)
    tailored, ledger = _rich_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    assert fit.trimmed == ["Least relevant work", "Mid relevant work"]
    assert len(revalidations) == 2  # every trim went back through validation
    remaining = [b["text"] for b in fit.tailored["experiences"][0]["bullets"]]
    assert remaining == ["Most relevant work"]


async def test_fit_resume_fails_when_floors_and_trims_run_out(
    tmp_path, monkeypatch
):
    import pytest as _pytest

    from backend.tailor import page_limit, validate

    _fake_compiler(monkeypatch, lambda tex: 3)  # never fits

    async def fake_validate(*a, **k):
        return []

    monkeypatch.setattr(validate, "validate", fake_validate)
    tailored, ledger = _rich_tailored()
    with _pytest.raises(page_limit.FitError):
        await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    # The original tailored JSON was never mutated by the failed fit.
    assert len(tailored["experiences"][0]["bullets"]) == 3


async def test_fit_resume_balances_sparse_last_page(tmp_path, monkeypatch):
    from backend.tailor import page_limit

    calls = _fake_compiler(monkeypatch, lambda tex: 2)
    # Same page count, but the balanced layout measurably fills the last
    # page better — adopted.
    fullness = iter([0.1, 0.5])
    monkeypatch.setattr(page_limit, "last_page_fullness",
                        lambda p: next(fullness))
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 2, tmp_path)
    assert len(calls) == 2  # initial + one balance pass
    assert fit.layout.item_sep_pt == 0  # the balance pass tightened spacing
    assert fit.result.pages == 2


async def test_sparse_balance_rejected_without_measured_improvement(
    tmp_path, monkeypatch
):
    """Regression: tightening a sparse layout can make the last page even
    emptier — same page count with lower fullness must be discarded."""
    from backend.resume import template
    from backend.tailor import page_limit

    calls = _fake_compiler(monkeypatch, lambda tex: 2)
    fullness = iter([0.1, 0.05])  # balanced compile came out SPARSER
    monkeypatch.setattr(page_limit, "last_page_fullness",
                        lambda p: next(fullness))
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 2, tmp_path)
    assert fit.layout == template.DEFAULT_LAYOUT  # kept the original
    # initial + balance attempt + recompile of the kept layout (the
    # balanced pass overwrote the PDF at the same path).
    assert len(calls) == 3
    assert calls[0][1] == calls[2][1]  # shipped PDF is the kept layout's


async def test_sparse_balance_accepts_page_count_reduction(tmp_path, monkeypatch):
    from backend.tailor import page_limit

    # Balance step (itemsep floor) actually removes the sparse page.
    _fake_compiler(monkeypatch, lambda tex: 1 if "itemsep=0pt" in tex else 2)
    monkeypatch.setattr(page_limit, "last_page_fullness", lambda p: 0.1)
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 2, tmp_path)
    assert fit.layout.item_sep_pt == 0
    assert fit.result.pages == 1


async def test_one_page_sparse_resume_gets_relaxed_balance(tmp_path, monkeypatch):
    """The main user case: a default one-page resume that is badly
    underfilled gets one bounded LOOSER step, kept on measured gain."""
    from backend.tailor import page_limit

    calls = _fake_compiler(monkeypatch, lambda tex: 1)
    fullness = iter([0.2, 0.6])  # sparse; relaxed layout measures fuller
    monkeypatch.setattr(page_limit, "last_page_fullness",
                        lambda p: next(fullness))
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    assert fit.layout.section_before_pt == 14  # one notch past default
    assert fit.layout.item_sep_pt == 2
    assert fit.layout.font_pt == 11  # font/margins never touched
    assert len(calls) == 2  # initial + adopted relax pass


async def test_one_page_relax_rejected_without_fullness_gain(
    tmp_path, monkeypatch
):
    from backend.resume import template
    from backend.tailor import page_limit

    calls = _fake_compiler(monkeypatch, lambda tex: 1)
    fullness = iter([0.2, 0.15])  # relaxing measured WORSE
    monkeypatch.setattr(page_limit, "last_page_fullness",
                        lambda p: next(fullness))
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    assert fit.layout == template.DEFAULT_LAYOUT
    assert len(calls) == 3  # initial + attempt + kept-layout recompile


async def test_one_page_relax_rejected_when_it_spills_a_page(
    tmp_path, monkeypatch
):
    from backend.resume import template
    from backend.tailor import page_limit

    # The looser layout pushes onto a second page — never an improvement.
    calls = _fake_compiler(
        monkeypatch, lambda tex: 2 if "itemsep=2pt" in tex else 1)
    monkeypatch.setattr(page_limit, "last_page_fullness", lambda p: 0.2)
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 1, tmp_path)
    assert fit.layout == template.DEFAULT_LAYOUT
    assert fit.result.pages == 1
    assert len(calls) == 3


async def test_fit_cover_ladder_then_condense_then_fail(tmp_path, monkeypatch):
    import pytest as _pytest

    from backend.core import llm
    from backend.tailor import page_limit, validate

    # Fits only when the letter mentions "short" — layout never fixes it.
    _fake_compiler(
        monkeypatch, lambda tex: 1 if "short" in tex else 2)
    condensed = {"paragraphs": [{"text": "A short letter.", "source_ids": []},
                                {"text": "Thanks.", "source_ids": []}]}
    llm_calls = []

    async def fake_structured(prompt, schema, task, system_prompt=""):
        llm_calls.append(task)
        return condensed

    async def fake_validate(tailored, profile, ledger, cache, record=None):
        return []

    monkeypatch.setattr(llm, "structured", fake_structured)
    monkeypatch.setattr(validate, "validate", fake_validate)
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_cover(tailored, PROFILE, ledger, {}, JOB, tmp_path)
    assert llm_calls == ["repair"]  # one xhigh condense round
    assert fit.result.pages == 1
    assert fit.tailored["cover_letter"] == condensed
    # Floors were reached before condensing was attempted.
    assert fit.layout.margin_in == 0.7 and fit.layout.font_pt == 10

    # And when even condensing can't reach one page: FitError.
    _fake_compiler(monkeypatch, lambda tex: 2)
    with _pytest.raises(page_limit.FitError):
        await page_limit.fit_cover(tailored, PROFILE, ledger, {}, JOB, tmp_path)


def test_content_bounds_ignores_a_rule_drawn_mid_line(tmp_path):
    """Regression: pdfTeX renders every `\\rule` as a `q .. cm .. Q`
    block. Landing back in a text object after one used to leave pypdf's
    memoized text matrix at the identity, so the following run measured
    at the page origin, y0 collapsed to 0 and last-page fullness read
    ~0.96 no matter what the page held. This content stream is that exact
    shape, byte for byte in structure.

    The template no longer feeds it an underscore to draw — T1 has a real
    glyph — but rules themselves are not going anywhere, and the fix has
    to hold for any PDF the fitter measures. Hence a hand-built page: no
    engine, no encoding, just the shape."""
    from backend.tailor import page_limit

    pdf = tmp_path / "rule.pdf"
    pdf.write_bytes(one_page_pdf(
        "BT\n/F1 12 Tf\n100 700 Td [(Top)] TJ\nET\n"
        "BT\n/F1 12 Tf\n100 500 Td [(Left)] TJ\nET\n"
        "q\n1 0 0 1 200 500 cm\n[]0 d 0 J 0.4 w 0 0 m 3 0 l S\nQ\n"
        "BT\n/F1 12 Tf\n220 500 Td [(Right)] TJ\nET\n"
    ))

    box = page_limit.content_bounds(pdf)[0]
    assert box is not None
    # The run after the rule is at y=500, not at the origin.
    assert box["y0"] == 500.0 and box["x0"] == 100.0
    assert box["x1"] == 220.0 and box["y1"] == 712.0
    # Measured height, not a full page: 212pt of content on 792pt.
    assert page_limit.last_page_fullness(pdf) == pytest.approx(212 / 792)


def test_content_bounds_skips_kerning_only_runs(tmp_path):
    """A `TJ` can carry nothing but kerning numbers; that paints no ink
    and must not stretch the box."""
    from backend.tailor import page_limit

    pdf = tmp_path / "kern.pdf"
    pdf.write_bytes(one_page_pdf(
        "BT\n/F1 12 Tf\n100 700 Td [(Top)] TJ\nET\n"
        "BT\n/F1 12 Tf\n100 100 Td [-278] TJ\nET\n"
    ))
    box = page_limit.content_bounds(pdf)[0]
    assert box is not None and box["y0"] == 700.0


def test_content_bounds_measures_real_pdf(tmp_path):
    from backend.tailor import compile as latex
    from backend.tailor import page_limit

    if not latex.available_engines():
        import pytest as _pytest

        _pytest.skip("no LaTeX engine installed")
    tex = ("\\documentclass{article}\\begin{document}"
           "Hello geometry\\end{document}")
    result = latex.compile_tex(tex, tmp_path, jobname="geom")
    bounds = page_limit.content_bounds(result.pdf_path)
    assert len(bounds) == 1 and bounds[0] is not None
    box = bounds[0]
    assert box["x1"] > box["x0"] and box["y1"] > box["y0"]
    fullness = page_limit.last_page_fullness(result.pdf_path)
    assert 0.0 < fullness < 1.0

    # Whatever engine produced it, the box is in page coordinates. XeTeX
    # output measured a third of a page too short until the current
    # transformation matrix was applied, and its box ran negative.
    from pypdf import PdfReader

    page = PdfReader(str(result.pdf_path)).pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    assert 0 <= box["x0"] and box["x1"] <= width
    assert 0 <= box["y0"] and box["y1"] <= height


def test_pdf_matrix_transform_composes_translation_and_scale():
    """content_bounds maps text-space points through the text matrix and
    then the page CTM; pdfTeX leaves the CTM at identity, xdvipdfmx does
    not, so the composition is what makes the two engines comparable."""
    from backend.tailor.page_limit import _transform

    identity = [1, 0, 0, 1, 0, 0]
    assert _transform(identity, 3.0, 4.0) == (3.0, 4.0)
    # Pure translation, as pdfTeX emits it.
    assert _transform([1, 0, 0, 1, 72, 720], 0.0, 0.0) == (72.0, 720.0)
    # Scale + translation, as xdvipdfmx emits it: a point one font-size up
    # must land one scaled font-size up.
    assert _transform([2, 0, 0, 2, 10, 20], 0.0, 5.0) == (10.0, 30.0)
    # Composition order matters: text matrix first, then the CTM.
    tm, cm = [1, 0, 0, 1, 100, 200], [1, 0, 0, 1, -36, -231]
    assert _transform(cm, *_transform(tm, 0.0, 0.0)) == (64.0, -31.0)


def test_sample_resume_fullness_comes_from_its_real_content(tmp_path):
    """The whole sample resume through the real engine: the measured box
    must sit inside the text block, not start at the page origin. With
    the stale-matrix bug this page measured 0.963 full on pdflatex — over
    SPARSE_FULLNESS, so the balance pass below never ran on any
    document."""
    from backend.tailor import compile as latex
    from backend.tailor import page_limit
    from backend.resume.template import render
    from backend.tests.test_resume import SAMPLE_PROFILE

    if not latex.available_engines():
        pytest.skip("no LaTeX engine installed")

    result = latex.compile_tex(render(SAMPLE_PROFILE), tmp_path, jobname="resume")
    box = page_limit.content_bounds(result.pdf_path)[-1]
    assert box is not None

    from pypdf import PdfReader

    page = PdfReader(str(result.pdf_path)).pages[-1]
    height = float(page.mediabox.height)
    # No run at the origin: the lowest line is inside the bottom margin,
    # and the highest is under the top edge.
    assert box["y0"] > 72 and box["y1"] < height
    fullness = page_limit.last_page_fullness(result.pdf_path)
    assert fullness == pytest.approx((box["y1"] - box["y0"]) / height)
    # A one-page resume this short is nowhere near full; 0.963 was the
    # phantom's signature.
    assert 0.2 < fullness < 0.6


async def test_real_fit_balances_a_sparse_page(tmp_path):
    """End-to-end on the real engine, no LLM: a short resume measures
    sparse, so fit_resume runs its one balance pass, and keeps the looser
    layout because the page measurably fills more."""
    from backend.resume import template
    from backend.tailor import compile as latex
    from backend.tailor import page_limit

    if not latex.available_engines():
        pytest.skip("no LaTeX engine installed")

    tailored = _tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, {}, {}, 1, tmp_path)

    assert fit.result.pages == 1 and fit.trimmed == []
    # The balance pass ran and its result was kept: nothing is trimmed
    # here, so the only way the layout can differ from the default is the
    # measured sparse-page relax.
    assert fit.layout == page_limit.resume_relaxed(template.DEFAULT_LAYOUT)
    assert page_limit.last_page_fullness(fit.result.pdf_path) < 1.0


def test_packages_to_resume_excludes_terminal_states(test_db):
    """Restart recovery picks up mid-flight packages in batch order and
    never a done/failed one (PLAN §4.6: completed work is never redone)."""
    for i, status in enumerate(
        ("done", "failed", "generating", "queued", "waiting_usage"), start=1
    ):
        _seed_job(f"p{i}")
        pkg, _ = _create_package(f"p{i}", 4, "hash-a")
        db.update_package(pkg, status=status)
    resumable = db.packages_to_resume()
    assert [p["job_id"] for p in resumable] == ["p3", "p4", "p5"]


# --- Package pipeline (state machine, restart, isolation) --------------------

def _confirm_profile():
    version = db.save_profile(PROFILE)
    db.confirm_profile(version)
    return version


def _seed_derived(job_id, version, jd_hash="hash-a", score=50.0,
                  breakdown=None):
    """The derived inputs generation waits for: cached requirements and a
    current (profile, hash) score."""
    from backend.normalize.requirements import cache_key

    db.save_requirements(cache_key(jd_hash), {"required_skills": ["Python"]})
    db.save_score(job_id, jd_hash, version, score,
                  breakdown if breakdown is not None
                  else {"match_reasons": [], "gap_reasons": []})


def _tailor_env(monkeypatch, tmp_path, behavior=None):
    """Fake engine + generation for pipeline tests. `behavior(job)`
    returns tailored JSON or raises; defaults to a fully cited doc.
    Entailment supports everything. Returns (generate_calls, entail_calls)."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    _fake_compiler(monkeypatch, lambda tex: 1)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")
    generate_calls, entail_calls = [], []

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        generate_calls.append(job["job_id"])
        if behavior is not None:
            return behavior(job)
        return _cited_tailored()[0]

    async def fake_structured(prompt, schema, task, system_prompt=""):
        assert task == "entail"  # only validation should reach the LLM here
        entail_calls.append(task)
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [{"claim_id": cid, "supported": True,
                              "reason": "ok"} for cid in claim_ids]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)
    return generate_calls, entail_calls


async def test_package_end_to_end_publishes_and_never_regenerates(
    test_db, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version, score=79.8,
                  breakdown={"match_reasons": ["Strong Python overlap"],
                             "gap_reasons": []})
    generate_calls, entail_calls = _tailor_env(monkeypatch, tmp_path)
    pkg, _ = _create_package("p1", version, "hash-a")

    assert await tailor_pipeline.run_package(pkg) == "done"
    row = db.get_package(pkg)
    assert row["status"] == "done"
    out = tmp_path / "output" / f"Example_Widgets_Inc_p1_p{pkg}"
    assert row["output_dir"] == str(out)
    assert (out / "resume.pdf").is_file()
    assert (out / "cover_letter.pdf").is_file()
    job_md = (out / "job.md").read_text()
    assert "We need Python." in job_md  # pinned posting snapshot
    assert "linkedin.com/jobs/view/p1" in job_md
    assert "79.8" in job_md and "Strong Python overlap" in job_md
    assert "source_ids" not in job_md  # internal metadata stays internal
    prov = row["provenance"]
    assert prov["engine_resume"] == "fake" and prov["resume_pages"] == 1
    assert prov["cover_pages"] == 1 and prov["prompt_version"] >= 2
    assert prov["page_limit"] == 1  # the pinned limit, not a live read
    assert [c["call"] for c in prov["llm_calls"]] == ["generation",
                                                      "entailment"]
    assert prov["llm_calls"][0]["effort"] == "high"
    assert prov["template_version"] >= 1
    assert generate_calls == ["p1"] and entail_calls == ["entail"]

    # Completed packages are never redone: no state change, no LLM calls.
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert generate_calls == ["p1"] and entail_calls == ["entail"]


def test_template_upgrade_makes_the_next_selection_a_new_package(
    test_db, tmp_path, monkeypatch
):
    """A published package is immutable, so an improved template can only
    reach a job the user already generated by making the next selection a
    NEW package. Through the gate-3 route, end to end: v1 completes, the
    templates advance, the same job/profile/posting/page-limit selection
    creates a different package, the v1 row and its PDF are untouched,
    and the new provenance records the new versions."""
    import asyncio
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import routes
    from backend.resume import template as resume_template
    from backend.tailor import cover as cover_mod
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)
    # The route spawns the worker; this test drives the package itself so
    # each run is observable.
    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    client = TestClient(app)

    first = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    assert first.status_code == 200 and first.json()["created"] == 1
    v1 = first.json()["package_ids"][0]
    assert asyncio.run(tailor_pipeline.run_package(v1)) == "done"

    v1_row = db.get_package(v1)
    assert v1_row["template_version"] == resume_template.TEMPLATE_VERSION
    assert v1_row["cover_template_version"] == cover_mod.TEMPLATE_VERSION
    v1_pdf = Path(v1_row["output_dir"]) / "resume.pdf"
    v1_bytes = v1_pdf.read_bytes()
    v1_updated, v1_prov = v1_row["updated_at"], v1_row["provenance"]

    # Same inputs, same templates: still one package, nothing regenerated.
    repeat = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    assert repeat.json() == {"package_ids": [v1], "created": 0}

    # The templates are upgraded (this round: T1 encoding, so the text
    # layer carries characters the v1 documents dropped).
    monkeypatch.setattr(resume_template, "TEMPLATE_VERSION",
                        resume_template.TEMPLATE_VERSION + 1)
    monkeypatch.setattr(cover_mod, "TEMPLATE_VERSION",
                        cover_mod.TEMPLATE_VERSION + 1)

    second = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    v2 = second.json()["package_ids"][0]
    assert second.json()["created"] == 1 and v2 != v1
    assert asyncio.run(tailor_pipeline.run_package(v2)) == "done"
    assert generate_calls == ["p1", "p1"]  # genuinely rebuilt, not reused

    # The v1 package is history: same status, same row, same bytes.
    old = db.get_package(v1)
    assert old["status"] == "done" and old["updated_at"] == v1_updated
    assert old["provenance"] == v1_prov
    assert old["template_version"] == resume_template.TEMPLATE_VERSION - 1
    assert v1_pdf.read_bytes() == v1_bytes

    new = db.get_package(v2)
    assert new["output_dir"] != old["output_dir"]
    assert new["template_version"] == resume_template.TEMPLATE_VERSION
    assert new["provenance"]["template_version"] == resume_template.TEMPLATE_VERSION
    assert (new["provenance"]["cover_template_version"]
            == cover_mod.TEMPLATE_VERSION)


def test_generate_announces_only_genuinely_created_packages(
    test_db, monkeypatch
):
    """Creation is idempotent, so `package_ids` is every requested job —
    reused rows included. Announcing that set says new work exists when a
    repeat selection added none, and every other tab refetches for it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import routes
    from backend.core.events import broker

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(broker, "publish",
                        lambda event, data: published.append((event, data)))

    version = _confirm_profile()
    _seed_job("p1")
    _seed_job("p2", jd_hash="hash-b")
    _seed_derived("p1", version)
    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    client = TestClient(app)

    first = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    assert first.status_code == 200 and first.json()["created"] == 1
    p1 = first.json()["package_ids"][0]
    assert published == [("packages", {"event": "created", "ids": [p1]})]

    # Same selection again: one existing row back, nothing new to fetch.
    published.clear()
    repeat = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    assert repeat.json() == {"package_ids": [p1], "created": 0}
    assert published == []

    # Mixed selection announces the new row only.
    published.clear()
    mixed = client.post("/api/packages/generate",
                        json={"job_ids": ["p1", "p2"]})
    ids = mixed.json()["package_ids"]
    assert mixed.json()["created"] == 1 and ids[0] == p1
    assert published == [("packages", {"event": "created", "ids": [ids[1]]})]


def test_generate_rejects_jobs_outside_the_run_the_user_saw(test_db, monkeypatch):
    """The jobs table is a global cache spanning every run, so an id from
    a superseded result set resolves there perfectly well. The run the
    selection was made against is sent with it and checked for membership
    — otherwise a click that landed while the list was being replaced
    generates a package for a job the user was never shown."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api import routes

    version = _confirm_profile()
    _seed_job("p1")
    _seed_job("p2", jd_hash="hash-b")
    _seed_derived("p1", version)
    run_id = db.create_search_run({"keywords": "backend"})
    db.record_search_page(run_id, [{
        "job_id": "p1", "title": "Backend Engineer",
        "company": "Example Widgets Inc.", "location": "Toronto, ON",
        "posted_text": None, "workplace": None, "easy_apply": False,
        "url": "https://www.linkedin.com/jobs/view/p1/",
    }], 0, 25)

    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    client = TestClient(app)

    ok = client.post("/api/packages/generate",
                     json={"job_ids": ["p1"], "run_id": run_id})
    assert ok.status_code == 200 and ok.json()["created"] == 1

    stale = client.post("/api/packages/generate",
                        json={"job_ids": ["p2"], "run_id": run_id})
    assert stale.status_code == 409
    assert "not in the results" in stale.json()["detail"]
    assert [row["id"] for row in db.list_packages()] == ok.json()["package_ids"]

    # A mixed selection is refused whole — no partial generation.
    mixed = client.post("/api/packages/generate",
                        json={"job_ids": ["p1", "p2"], "run_id": run_id})
    assert mixed.status_code == 409

    # Omitting run_id means "no run context" and stays permitted.
    loose = client.post("/api/packages/generate", json={"job_ids": ["p2"]})
    assert loose.status_code == 200

    bad = client.post("/api/packages/generate",
                      json={"job_ids": ["p1"], "run_id": "3"})
    assert bad.status_code == 422


def test_template_upgrade_supersedes_only_unfinished_packages(
    test_db, tmp_path, monkeypatch
):
    """Startup after an upgrade: a package queued under the old templates
    would otherwise finish by rendering the new ones into a row whose
    identity says otherwise. It is superseded with a way out; the done
    package beside it is untouched and still downloadable."""
    from backend.api import routes
    from backend.resume import template as resume_template
    from backend.tailor import cover as cover_mod

    version = _confirm_profile()
    _seed_job("p1")
    _seed_job("p2", jd_hash="hash-b")
    _seed_derived("p1", version)
    finished, _ = _create_package("p1", version, "hash-a")
    db.update_package(finished, status="done", output_dir="/tmp/old-package")
    unfinished, _ = _create_package("p2", version, "hash-b")
    db.update_package(unfinished, status="generating")

    monkeypatch.setattr(resume_template, "TEMPLATE_VERSION",
                        resume_template.TEMPLATE_VERSION + 1)
    monkeypatch.setattr(cover_mod, "TEMPLATE_VERSION",
                        cover_mod.TEMPLATE_VERSION + 1)
    monkeypatch.setattr(routes.sources.source, "current_state", lambda: "idle")
    monkeypatch.setattr(routes, "_package_worker_task", None)
    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: None)

    routes.recover_search_runs()

    stale = db.get_package(unfinished)
    assert stale["status"] == "failed"
    assert "re-select the job" in stale["error"]
    kept = db.get_package(finished)
    assert kept["status"] == "done" and kept["output_dir"] == "/tmp/old-package"
    assert kept["error"] is None


def _legacy_packages_db(tmp_path, monkeypatch) -> None:
    """A database as it stood before the template versions joined package
    identity: two rows, one carrying provenance and one predating it."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "legacy.sqlite3")
    db.init()

    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript("""
        DROP TABLE packages;
        CREATE TABLE packages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id          TEXT NOT NULL,
            profile_version INTEGER NOT NULL,
            jd_hash         TEXT NOT NULL,
            page_limit      INTEGER NOT NULL DEFAULT 1,
            status          TEXT NOT NULL DEFAULT 'queued',
            stage_json      TEXT,
            error           TEXT,
            retry_count     INTEGER NOT NULL DEFAULT 0,
            provenance_json TEXT,
            output_dir      TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (job_id, profile_version, jd_hash, page_limit)
        );
    """)
    conn.execute(
        "INSERT INTO packages (id, job_id, profile_version, jd_hash, "
        "page_limit, status, provenance_json, output_dir, created_at, "
        "updated_at) VALUES (7, 'p1', 4, 'hash-a', 1, 'done', ?, "
        "'/out/p1', 't0', 't1')",
        (json.dumps({"template_version": 1, "cover_template_version": 1,
                     "resume_pages": 1}),),
    )
    conn.execute(
        "INSERT INTO packages (id, job_id, profile_version, jd_hash, "
        "page_limit, status, created_at, updated_at) "
        "VALUES (8, 'p2', 4, 'hash-b', 2, 'queued', 't0', 't1')"
    )
    conn.commit()
    conn.close()


def _table_names() -> set[str]:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def test_package_identity_migration_keeps_published_rows(tmp_path, monkeypatch):
    """Databases written before the template versions joined package
    identity hold real packages whose PDFs are on disk. The migration
    rebuilds the table without losing one, pinning each row to the
    versions its own provenance records — or to 1 where it predates the
    record."""
    _legacy_packages_db(tmp_path, monkeypatch)

    db.init()  # the upgrade

    kept = db.get_package(7)
    assert kept["status"] == "done" and kept["output_dir"] == "/out/p1"
    assert kept["provenance"]["resume_pages"] == 1
    assert kept["template_version"] == 1 and kept["cover_template_version"] == 1
    predating = db.get_package(8)
    assert predating["page_limit"] == 2 and predating["status"] == "queued"
    assert predating["template_version"] == 1
    assert {row["id"] for row in db.list_packages()} <= {7, 8}
    assert "packages_pre_tpl_version" not in _table_names()

    # And identity is genuinely wider now: same four old inputs, newer
    # templates, new row.
    fresh, created = db.create_package("p1", 4, "hash-a", 1,
                                       template_version=2,
                                       cover_template_version=2)
    assert created is True and fresh != 7


def test_failed_package_migration_rolls_back_whole(tmp_path, monkeypatch):
    """A migration that dies partway must leave the OLD table
    authoritative, not a renamed table beside an empty new one.

    That half-state is unrecoverable by design: `packages` would carry
    `template_version`, so the next startup would skip the migration
    forever while every published row sat in `packages_pre_tpl_version`,
    invisible to the app, its PDFs still on disk. Rolling back whole
    means the next startup just tries again.
    """
    _legacy_packages_db(tmp_path, monkeypatch)

    monkeypatch.setattr(db, "_COPY_PACKAGES_SQL",
                        "INSERT INTO packages (id) SELECT no_such_column "
                        "FROM packages_pre_tpl_version")
    with pytest.raises(sqlite3.OperationalError):
        db.init()

    # Old table, old shape, both rows visible, nothing left half-renamed.
    conn = sqlite3.connect(db.DB_PATH)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(packages)")}
        rows = conn.execute(
            "SELECT id, status, output_dir FROM packages ORDER BY id").fetchall()
    finally:
        conn.close()
    assert "template_version" not in cols
    assert rows == [(7, "done", "/out/p1"), (8, "queued", None)]
    assert "packages_pre_tpl_version" not in _table_names()

    # And the retry succeeds: a rolled-back migration is not a dead end.
    monkeypatch.undo()
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "legacy.sqlite3")
    db.init()
    assert db.get_package(7)["output_dir"] == "/out/p1"
    assert db.get_package(8)["template_version"] == 1
    assert "packages_pre_tpl_version" not in _table_names()


def test_short_package_migration_copy_is_refused(tmp_path, monkeypatch):
    """Counting the copy is the other half of the guard: a statement that
    succeeds while dropping rows would otherwise commit, and the dropped
    packages would be gone with the old table."""
    _legacy_packages_db(tmp_path, monkeypatch)

    monkeypatch.setattr(
        db, "_COPY_PACKAGES_SQL",
        db._COPY_PACKAGES_SQL + " WHERE id = 7")  # loses row 8
    with pytest.raises(sqlite3.IntegrityError, match="copied 1 of 2 rows"):
        db.init()

    conn = sqlite3.connect(db.DB_PATH)
    try:
        rows = conn.execute("SELECT id FROM packages ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [(7,), (8,)]
    assert "packages_pre_tpl_version" not in _table_names()


async def test_restart_mid_batch_completes_without_duplicate_generation(
    test_db, tmp_path, monkeypatch
):
    """M4 criterion: kill the app mid-generation, restart, batch
    completes with no duplicated work. The kill lands between stages
    (generation persisted, validation in flight), so every job is
    generated exactly once across both processes."""
    import asyncio

    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    packages = []
    for i in (1, 2, 3):
        _seed_job(f"p{i}")
        _seed_derived(f"p{i}", version)
        pkg, _ = _create_package(f"p{i}", version, "hash-a")
        packages.append(pkg)
    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)

    # Block the SECOND package's entailment call: package 1 completes,
    # package 2 dies mid-validation with its generation already persisted.
    release = asyncio.Event()
    entail_started = asyncio.Event()
    original_structured = llm.structured

    async def gated_structured(prompt, schema, task, system_prompt=""):
        if task == "entail" and db.get_package(packages[1])["status"] == "validating":
            entail_started.set()
            await release.wait()
        return await original_structured(prompt, schema, task,
                                         system_prompt=system_prompt)

    monkeypatch.setattr(llm, "structured", gated_structured)
    batch = asyncio.create_task(tailor_pipeline.run_batch())
    await asyncio.wait_for(entail_started.wait(), timeout=5)
    batch.cancel()  # the "kill": mid-batch, mid-validation
    await asyncio.gather(batch, return_exceptions=True)

    assert db.get_package(packages[0])["status"] == "done"
    interrupted = db.get_package(packages[1])
    assert interrupted["status"] == "validating"  # mid-flight, persisted
    assert "tailored" in interrupted["stage"]  # generation survived the kill
    assert db.get_package(packages[2])["status"] == "queued"

    # "Restart": a fresh batch over packages_to_resume().
    release.set()
    assert await tailor_pipeline.run_batch() == "idle"
    assert [db.get_package(p)["status"] for p in packages] == ["done"] * 3
    # No duplicated work: each job generated exactly once across restarts.
    assert generate_calls == ["p1", "p2", "p3"]


async def test_batch_failure_is_isolated_to_its_package(
    test_db, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    packages = []
    for i in (1, 2, 3):
        _seed_job(f"p{i}")
        _seed_derived(f"p{i}", version)
        pkg, _ = _create_package(f"p{i}", version, "hash-a")
        packages.append(pkg)

    def behavior(job):
        if job["job_id"] == "p2":
            raise ValueError("induced generation failure")
        return _cited_tailored()[0]

    _tailor_env(monkeypatch, tmp_path, behavior=behavior)
    assert await tailor_pipeline.run_batch() == "idle"
    statuses = [db.get_package(p)["status"] for p in packages]
    assert statuses == ["done", "failed", "done"]
    assert "induced generation failure" in db.get_package(packages[1])["error"]


async def test_usage_exhausted_is_durable_and_batch_resumes(
    test_db, tmp_path, monkeypatch
):
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    packages = []
    for i in (1, 2, 3):
        _seed_job(f"p{i}")
        _seed_derived(f"p{i}", version)
        pkg, _ = _create_package(f"p{i}", version, "hash-a")
        packages.append(pkg)
    healed = False

    def behavior(job):
        if job["job_id"] == "p2" and not healed:
            raise llm.UsageExhausted("weekly limit reached")
        return _cited_tailored()[0]

    _tailor_env(monkeypatch, tmp_path, behavior=behavior)
    assert await tailor_pipeline.run_batch() == "waiting_usage"
    assert db.get_package(packages[0])["status"] == "done"
    assert db.get_package(packages[1])["status"] == "waiting_usage"
    assert db.get_package(packages[2])["status"] == "queued"  # batch stopped

    healed = True  # cooldown passed — the retry timer re-runs the batch
    assert await tailor_pipeline.run_batch() == "idle"
    assert [db.get_package(p)["status"] for p in packages] == ["done"] * 3


async def test_usage_retry_timer_resumes_batch_without_manual_kick(
    test_db, tmp_path, monkeypatch
):
    """Timer-driven recovery: after a usage limit parks the batch, the
    armed retry task ALONE — no manual run_batch call, no user action —
    respawns the worker once the cooldown elapses and the package
    completes. (Scoring uses the same mechanism via _arm_pipeline_retry.)"""
    from backend.api import routes
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")

    calls = []

    def behavior(job):
        calls.append(1)
        if len(calls) == 1:
            raise llm.UsageExhausted("usage limit cooldown: retry in 60s")
        return _cited_tailored()[0]

    _tailor_env(monkeypatch, tmp_path, behavior=behavior)
    monkeypatch.setattr(routes, "_package_retry_task", None)
    monkeypatch.setattr(routes, "_package_worker_task", None)
    monkeypatch.setattr(routes.random, "uniform", lambda a, b: 0.0)

    armed = []
    real_arm = routes._arm_package_retry

    def capture_arm(delay):
        armed.append(delay)
        real_arm(0.0)  # same mechanism, cooldown treated as elapsed

    monkeypatch.setattr(routes, "_arm_package_retry", capture_arm)

    await routes._package_batch_worker()  # hits the limit, arms the timer
    assert armed and armed[0] >= 60.0  # delay covers the cooldown floor
    assert db.get_package(pkg)["status"] == "waiting_usage"

    await routes._package_retry_task  # the timer itself drives recovery
    worker = routes._package_worker_task
    assert worker is not None  # ...by respawning the batch worker
    await worker
    assert db.get_package(pkg)["status"] == "done"
    assert calls == [1, 1]  # one failed attempt, one timer-driven success


async def test_engine_blocked_parks_package_queued_and_stops_batch(
    test_db, tmp_path, monkeypatch
):
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    packages = []
    for i in (1, 2):
        _seed_job(f"p{i}")
        _seed_derived(f"p{i}", version)
        pkg, _ = _create_package(f"p{i}", version, "hash-a")
        packages.append(pkg)

    def behavior(job):
        raise llm.AuthRequired("Not logged in")

    generate_calls, _ = _tailor_env(monkeypatch, tmp_path, behavior=behavior)
    assert await tailor_pipeline.run_batch() == "engine_blocked"
    parked = db.get_package(packages[0])
    assert parked["status"] == "queued"  # nothing this package did wrong
    assert "AuthRequired" in parked["error"]
    assert db.get_package(packages[1])["status"] == "queued"
    assert generate_calls == ["p1"]  # batch stopped at the first hit


async def test_validation_failure_after_repair_fails_package(
    test_db, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import validate as validate_mod

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    _tailor_env(monkeypatch, tmp_path)

    async def always_failing(tailored, profile, ledger, cache, record=None):
        return [{"claim_id": "exp0:b0", "reason": "fabricated metric"}]

    repairs = []

    async def fake_repair(tailored, failures, profile, ledger):
        repairs.append(1)
        return tailored

    monkeypatch.setattr(validate_mod, "validate", always_failing)
    monkeypatch.setattr(validate_mod, "repair", fake_repair)
    assert await tailor_pipeline.run_package(pkg) == "failed"
    row = db.get_package(pkg)
    assert repairs == [1, 1]  # bounded: exactly two repair rounds, never more
    ledger = [c["call"] for c in row["stage"]["llm_calls"]]
    assert ledger.count("truth_repair") == 2
    assert "truth validation failed after 2 repair round(s)" in row["error"]
    assert "fabricated metric" in row["error"]


async def test_package_fails_when_posting_changed_before_generation(
    test_db, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    pkg, _ = _create_package("p1", version, "hash-a")
    db.save_job_detail("p1", "Rewritten posting.", "hash-new")  # edited
    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)
    assert await tailor_pipeline.run_package(pkg) == "failed"
    assert generate_calls == []  # no LLM spend on stale inputs
    assert "posting changed" in db.get_package(pkg)["error"]


async def test_generate_endpoint_gates_and_creates(test_db, monkeypatch):
    from fastapi import HTTPException

    from backend.api import routes

    spawned = []
    monkeypatch.setattr(routes, "_maybe_resume_packages",
                        lambda: spawned.append(True) or True)

    with pytest.raises(HTTPException) as exc:
        await routes.packages_generate({"job_ids": ["p1"]})
    assert exc.value.status_code == 409  # no confirmed profile

    version = _confirm_profile()
    with pytest.raises(HTTPException) as exc:
        await routes.packages_generate({"job_ids": ["ghost"]})
    assert exc.value.status_code == 404

    db.upsert_job({"job_id": "nodesc", "title": "T", "company": "C",
                   "url": "https://example.invalid/x"})
    with pytest.raises(HTTPException) as exc:
        await routes.packages_generate({"job_ids": ["nodesc"]})
    assert exc.value.status_code == 409  # no cached description yet

    _seed_job("p1")
    result = await routes.packages_generate({"job_ids": ["p1", "p1"]})
    assert result["created"] == 1  # duplicate selection dedupes
    assert len(result["package_ids"]) == 2
    assert spawned == [True]
    assert db.get_package(result["package_ids"][0])["profile_version"] == version


# --- Review round 1: publish immutability, fit persistence, recovery ---------

async def test_publish_dirs_are_immutable_per_package(
    test_db, tmp_path, monkeypatch
):
    """A new package for the same job (here: edited posting) publishes
    beside the old one — the first package's artifacts never change and
    its row keeps serving exactly what it built."""
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    _tailor_env(monkeypatch, tmp_path)
    pkg1, _ = _create_package("p1", version, "hash-a")
    assert await tailor_pipeline.run_package(pkg1) == "done"
    dir1 = db.get_package(pkg1)["output_dir"]
    from pathlib import Path
    baseline = {name: (Path(dir1) / name).read_bytes()
                for name in ("resume.pdf", "cover_letter.pdf", "job.md")}
    assert b"We need Python." in baseline["job.md"]

    # The posting is edited: new hash, new derived inputs, new package.
    db.save_job_detail("p1", "New text.", "hash-b")
    _seed_derived("p1", version, jd_hash="hash-b")
    pkg2, created = _create_package("p1", version, "hash-b")
    assert created and pkg2 != pkg1
    assert await tailor_pipeline.run_package(pkg2) == "done"
    dir2 = db.get_package(pkg2)["output_dir"]

    assert dir2 != dir1
    assert b"New text." in (Path(dir2) / "job.md").read_bytes()
    for name, before in baseline.items():
        assert (Path(dir1) / name).read_bytes() == before  # untouched
    assert db.get_package(pkg1)["output_dir"] == dir1


async def test_restart_during_cover_condense_reuses_persisted_fits(
    test_db, tmp_path, monkeypatch
):
    """A kill mid-condense resumes without repeating generation, the
    resume fit, or any cached entailment — only the interrupted condense
    runs again."""
    import asyncio

    from backend.core import llm
    from backend.tailor import page_limit as pl
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    db.set_state("pinned_model", "claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")

    def pages(tex):
        if "Hiring Team" in tex:  # the cover letter
            return 1 if "A short letter." in tex else 2
        return 1  # the resume always fits
    _fake_compiler(monkeypatch, pages)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    generate_calls, condense_calls = [], []
    release = asyncio.Event()
    condense_started = asyncio.Event()

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        generate_calls.append(job["job_id"])
        return _cited_tailored()[0]

    async def fake_structured(prompt, schema, task, system_prompt=""):
        if task == "entail":
            claim_ids = [json.loads(line)["claim_id"]
                         for line in prompt.splitlines() if line.startswith("{")]
            return {"verdicts": [{"claim_id": cid, "supported": True,
                                  "reason": "ok"} for cid in claim_ids]}
        assert task == "repair"  # the condense pass
        condense_calls.append(1)
        condense_started.set()
        await release.wait()
        return {"paragraphs": [{"text": "A short letter.", "source_ids": []},
                               {"text": "Thanks.", "source_ids": []}]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    resume_fit_calls = []
    original_fit_resume = pl.fit_resume

    async def counting_fit_resume(*args, **kwargs):
        resume_fit_calls.append(1)
        return await original_fit_resume(*args, **kwargs)

    monkeypatch.setattr(pl, "fit_resume", counting_fit_resume)

    worker = asyncio.create_task(tailor_pipeline.run_package(pkg))
    await asyncio.wait_for(condense_started.wait(), timeout=5)
    worker.cancel()  # the "kill": mid-condense
    await asyncio.gather(worker, return_exceptions=True)

    row = db.get_package(pkg)
    assert row["status"] == "fitting"
    assert "resume_fit" in row["stage"]  # persisted before the cover fit
    assert "cover_fit" not in row["stage"]
    assert generate_calls == ["p1"] and resume_fit_calls == [1]

    # "Restart" — on a recovered engine running a DIFFERENT model: only
    # the interrupted condense re-runs, and provenance must attribute
    # each stage's call to the model that actually made it.
    db.set_state("pinned_model", "claude-opus-5")
    release.set()
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert generate_calls == ["p1"]  # generation never repeated
    assert resume_fit_calls == [1]  # resume fit never repeated
    assert len(condense_calls) == 2  # the interrupted one, once more
    prov = db.get_package(pkg)["provenance"]
    assert prov["cover_pages"] == 1
    # The ledger attributes every call to the model that actually made
    # it, in order — the first condense died mid-call (no entry), the
    # restart's condense and its revalidation ran on the new model.
    assert [(c["call"], c["model"]) for c in prov["llm_calls"]] == [
        ("generation", "claude-fable-5"),
        ("entailment", "claude-fable-5"),
        ("cover_condense", "claude-opus-5"),
        ("entailment", "claude-opus-5"),
    ]
    assert prov["llm_calls"][2]["effort"] == "xhigh"


@pytest.mark.parametrize("interrupt_at", ["asserting", "publishing"])
async def test_restart_after_fitting_needs_no_llm(
    test_db, tmp_path, monkeypatch, interrupt_at
):
    """Once both fits are persisted, a kill during asserting or
    publishing resumes with ZERO LLM calls — pure recompile + publish."""
    import asyncio
    import shutil as shutil_mod

    from backend.tailor import page_limit as pl
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    generate_calls, entail_calls = _tailor_env(monkeypatch, tmp_path)

    tripped = []
    if interrupt_at == "asserting":
        original = pl.compile_cover_state

        def dying_compile(*args, **kwargs):
            if not tripped:
                tripped.append(1)
                raise asyncio.CancelledError
            return original(*args, **kwargs)

        monkeypatch.setattr(pl, "compile_cover_state", dying_compile)
    else:
        original = shutil_mod.copy2

        def dying_copy(*args, **kwargs):
            if not tripped:
                tripped.append(1)
                raise asyncio.CancelledError
            return original(*args, **kwargs)

        monkeypatch.setattr(shutil_mod, "copy2", dying_copy)

    with pytest.raises(asyncio.CancelledError):
        await tailor_pipeline.run_package(pkg)
    row = db.get_package(pkg)
    assert row["status"] == interrupt_at
    assert "resume_fit" in row["stage"] and "cover_fit" in row["stage"]
    calls_before = (list(generate_calls), list(entail_calls))

    assert await tailor_pipeline.run_package(pkg) == "done"
    assert (generate_calls, entail_calls) == calls_before  # zero LLM spent
    out = db.get_package(pkg)["output_dir"]
    from pathlib import Path
    assert (Path(out) / "resume.pdf").is_file()
    assert (Path(out) / "cover_letter.pdf").is_file()


async def test_package_pins_page_limit_at_creation(
    test_db, tmp_path, monkeypatch
):
    """The page limit is package identity: pinned at gate 3, immune to a
    later settings change, and a changed limit is a NEW package."""
    from backend.api import routes
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: True)

    db.set_state(tailor_pipeline.PAGE_LIMIT_KEY, "2")
    first = await routes.packages_generate({"job_ids": ["p1"]})
    pkg = first["package_ids"][0]
    assert db.get_package(pkg)["page_limit"] == 2

    db.set_state(tailor_pipeline.PAGE_LIMIT_KEY, "1")
    second = await routes.packages_generate({"job_ids": ["p1"]})
    assert second["created"] == 1  # different limit = different package
    assert second["package_ids"][0] != pkg

    # The old package still generates and fits against ITS pinned limit.
    _fake_compiler(monkeypatch, lambda tex: 1)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")
    seen_limits = []

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        seen_limits.append(page_limit)
        return _cited_tailored()[0]

    async def fake_structured(prompt, schema, task, system_prompt=""):
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [{"claim_id": cid, "supported": True,
                              "reason": "ok"} for cid in claim_ids]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert seen_limits == [2]  # pinned, not the current global (1)
    assert db.get_package(pkg)["provenance"]["page_limit"] == 2


async def test_resumed_package_keeps_pinned_limit_after_settings_change(
    test_db, tmp_path, monkeypatch
):
    """A package interrupted mid-flight and resumed AFTER a settings
    change still fits against its pinned limit — the new global limit
    shapes only packages selected after the change."""
    from backend.api import routes
    from backend.core import llm
    from backend.tailor import page_limit as page_limit_mod
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: True)

    db.set_state(tailor_pipeline.PAGE_LIMIT_KEY, "1")
    pkg = (await routes.packages_generate({"job_ids": ["p1"]}))["package_ids"][0]
    assert db.get_package(pkg)["page_limit"] == 1

    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)

    async def dying_structured(prompt, schema, task, system_prompt=""):
        raise llm.AuthRequired("Not logged in")

    monkeypatch.setattr(llm, "structured", dying_structured)
    assert await tailor_pipeline.run_package(pkg) == "engine_blocked"
    parked = db.get_package(pkg)
    assert parked["status"] == "queued"
    assert "tailored" in parked["stage"]  # generation checkpointed

    # The user raises the global limit while the package is parked.
    db.set_state(tailor_pipeline.PAGE_LIMIT_KEY, "2")

    async def healthy_structured(prompt, schema, task, system_prompt=""):
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [{"claim_id": cid, "supported": True,
                              "reason": "ok"} for cid in claim_ids]}

    monkeypatch.setattr(llm, "structured", healthy_structured)
    seen_limits = []
    real_fit = page_limit_mod.fit_resume

    async def spy_fit(tailored, profile, ledger, cache, limit, workdir, **kw):
        seen_limits.append(limit)
        return await real_fit(tailored, profile, ledger, cache, limit,
                              workdir, **kw)

    monkeypatch.setattr(page_limit_mod, "fit_resume", spy_fit)
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert seen_limits == [1]  # pinned, not the new global
    assert generate_calls == ["p1"]  # resume never regenerates
    assert db.get_package(pkg)["provenance"]["page_limit"] == 1

    # A NEW selection after the change pins the new limit.
    second = await routes.packages_generate({"job_ids": ["p1"]})
    assert second["created"] == 1
    new_pkg = second["package_ids"][0]
    assert new_pkg != pkg
    assert db.get_package(new_pkg)["page_limit"] == 2


async def test_package_waits_for_derived_inputs(test_db, tmp_path, monkeypatch):
    """A package selected before scoring finished waits — zero LLM spend,
    no permanent requirements=None package — and resumes once the
    derived inputs exist."""
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")  # description cached, but no requirements/score yet
    pkg, _ = _create_package("p1", version, "hash-a")
    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)

    assert await tailor_pipeline.run_batch() == "waiting_inputs"
    row = db.get_package(pkg)
    assert row["status"] == "queued"
    assert "waiting for scoring" in row["error"]
    assert generate_calls == []  # nothing was spent on missing inputs

    _seed_derived("p1", version)  # scoring finished deriving inputs
    assert await tailor_pipeline.run_batch() == "idle"
    row = db.get_package(pkg)
    assert row["status"] == "done" and row["error"] is None
    assert generate_calls == ["p1"]


async def test_scoring_completion_resumes_waiting_packages(
    test_db, monkeypatch
):
    from backend.api import routes

    resumed = []
    monkeypatch.setattr(routes, "_maybe_resume_packages",
                        lambda: resumed.append(True) or True)

    async def fake_run_pipeline(run_id):
        return {"scored": 1, "failed": 0, "stopped": None, "detail": None}

    monkeypatch.setattr(routes.scoring_pipeline, "run_pipeline",
                        fake_run_pipeline)
    monkeypatch.setattr(routes.scoring_pipeline, "pending_count",
                        lambda run_id: 0)
    await routes._pipeline_worker(1, 1)
    assert resumed == [True]  # scoring completion kicks waiting packages


async def test_engine_blocked_packages_recover_automatically(
    test_db, tmp_path, monkeypatch
):
    """Finding 3: with scoring already finished, a package batch parked
    on an auth failure still has an autonomous way back — the persisted
    package stall polls auth, reprobes once healthy, and the SAME
    package resumes to done with no manual action."""
    import asyncio

    from backend.api import routes
    from backend.core import llm

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")

    blocked = True

    def behavior(job):
        if blocked:
            raise llm.AuthRequired("Not logged in")
        return _cited_tailored()[0]

    _tailor_env(monkeypatch, tmp_path, behavior=behavior)
    monkeypatch.setattr(routes, "_STALL_AUTH_POLL_S", 0.01)
    monkeypatch.setattr(routes.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(routes, "_package_stall_task", None)
    monkeypatch.setattr(routes, "_package_stall_token", None)
    monkeypatch.setattr(routes, "_package_worker_task", None)
    monkeypatch.setattr(routes.claude_cli, "subscription_problem",
                        lambda: "Not logged in" if blocked else None)

    async def ok_probe():
        return "claude-fable-5"

    monkeypatch.setattr(routes.llm, "probe", ok_probe)
    monkeypatch.setattr(routes.llm, "reset_probe", lambda: None)

    await routes._package_batch_worker()  # hits AuthRequired
    stall = routes._read_stall(routes._PACKAGE_STALL_KEY)
    assert stall is not None and stall["reason"] == "engine_blocked"
    assert "AuthRequired" in (stall["detail"] or "")
    assert routes._maybe_resume_packages() is False  # breaker holds
    task = routes._package_stall_task
    assert task is not None and not task.done()  # armed by the worker

    # While auth stays broken the loop keeps polling, never clears.
    await asyncio.sleep(0.05)
    assert routes._read_stall(routes._PACKAGE_STALL_KEY) is not None
    assert db.get_package(pkg)["status"] == "queued"

    blocked = False  # the user logged back in
    await asyncio.wait_for(task, timeout=5)  # poll → healthy → reprobe
    assert routes._read_stall(routes._PACKAGE_STALL_KEY) is None
    worker = routes._package_worker_task
    assert worker is not None  # recovery respawned the batch
    await asyncio.wait_for(worker, timeout=5)
    assert db.get_package(pkg)["status"] == "done"


# --- Review round 2: supersede on input change, captcha, snapshots -----------

async def test_posting_change_mid_generation_supersedes_package(
    test_db, tmp_path, monkeypatch
):
    """The posting is edited WHILE generation is in flight: the package
    must end superseded, never done — the worker's guarded transitions
    cannot resurrect the row the JD change failed."""
    import asyncio

    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    _tailor_env(monkeypatch, tmp_path)

    release = asyncio.Event()
    started = asyncio.Event()

    async def gated_generate(profile, ledger, job, requirements, breakdown,
                             page_limit):
        started.set()
        await release.wait()
        return _cited_tailored()[0]

    monkeypatch.setattr(tailor_resume, "generate_tailored", gated_generate)
    worker = asyncio.create_task(tailor_pipeline.run_package(pkg))
    await asyncio.wait_for(started.wait(), timeout=5)
    db.save_job_detail("p1", "Rewritten mid-flight.", "hash-new")  # the edit
    release.set()
    assert await asyncio.wait_for(worker, timeout=5) == "superseded"

    row = db.get_package(pkg)
    assert row["status"] == "failed"
    assert "superseded: posting changed" in row["error"]
    assert row["output_dir"] is None  # nothing stale was ever published

    # The new posting is a fresh input set — a new package is available.
    pkg2, created = _create_package("p1", version, "hash-new")
    assert created and pkg2 != pkg


async def test_profile_change_mid_fitting_supersedes_package(
    test_db, tmp_path, monkeypatch
):
    """Confirming a new profile version supersedes unfinished packages
    pinned to the old one — including a worker mid-fitting — and
    unstrands would-be-forever waiting_inputs rows."""
    import asyncio

    from backend.tailor import page_limit as pl
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    _seed_job("p2", jd_hash="hash-a")  # no derived inputs: waits
    waiting_pkg, _ = _create_package("p2", version, "hash-a")
    _tailor_env(monkeypatch, tmp_path)

    release = asyncio.Event()
    started = asyncio.Event()
    original_fit_resume = pl.fit_resume

    async def gated_fit(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_fit_resume(*args, **kwargs)

    monkeypatch.setattr(pl, "fit_resume", gated_fit)
    worker = asyncio.create_task(tailor_pipeline.run_package(pkg))
    await asyncio.wait_for(started.wait(), timeout=5)
    version2 = db.save_profile({**PROFILE, "name": "Casey Two"})
    db.confirm_profile(version2)  # the profile change
    release.set()
    assert await asyncio.wait_for(worker, timeout=5) == "superseded"

    for stale in (pkg, waiting_pkg):
        row = db.get_package(stale)
        assert row["status"] == "failed"
        assert "superseded: profile changed" in row["error"]


async def test_posting_change_leaves_done_packages_alone(
    test_db, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    _tailor_env(monkeypatch, tmp_path)
    pkg, _ = _create_package("p1", version, "hash-a")
    assert await tailor_pipeline.run_package(pkg) == "done"

    db.save_job_detail("p1", "Edited later.", "hash-later")
    row = db.get_package(pkg)
    assert row["status"] == "done"  # history is never rewritten
    assert row["output_dir"] is not None


async def test_captcha_stop_does_not_block_package_recovery(
    test_db, tmp_path, monkeypatch
):
    """Startup with a persisted captcha stop AND a mid-flight package:
    search recovery stays deferred (no bypass-shaped navigation), but the
    package — which needs no browser — completes."""
    import asyncio

    from backend.api import routes

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    db.update_package(pkg, status="generating")  # died mid-generation
    _tailor_env(monkeypatch, tmp_path)
    monkeypatch.setattr(routes.sources.source, "current_state",
                        lambda: "captcha_stop")
    monkeypatch.setattr(routes, "_package_worker_task", None)

    assert routes.recover_search_runs() == []  # search recovery deferred
    worker = routes._package_worker_task
    assert worker is not None  # ...but the package queue resumed anyway
    await asyncio.wait_for(worker, timeout=5)
    assert db.get_package(pkg)["status"] == "done"


async def test_shipped_documents_must_match_cached_verdicts(
    test_db, tmp_path, monkeypatch
):
    """Finding 3: the pre-publish assertion binds to the SHIPPED
    documents. Persisted fitted content that drifted past validation
    (no cached verdict) fails closed — with zero LLM calls."""
    import asyncio
    import shutil as shutil_mod

    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    generate_calls, entail_calls = _tailor_env(monkeypatch, tmp_path)

    tripped = []
    original_copy = shutil_mod.copy2

    def dying_copy(*args, **kwargs):
        if not tripped:
            tripped.append(1)
            raise asyncio.CancelledError
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(shutil_mod, "copy2", dying_copy)
    with pytest.raises(asyncio.CancelledError):
        await tailor_pipeline.run_package(pkg)  # dies in publishing

    # Tamper with the persisted fitted cover — an uncached claim.
    row = db.get_package(pkg)
    stage = row["stage"]
    stage["cover_fit"]["tailored"]["cover_letter"]["paragraphs"][0]["text"] = (
        "I won a Nobel Prize.")
    db.update_package(pkg, stage=stage)
    calls_before = (list(generate_calls), list(entail_calls))

    assert await tailor_pipeline.run_package(pkg) == "failed"
    row = db.get_package(pkg)
    assert "ValidationFailed" in row["error"]
    assert (generate_calls, entail_calls) == calls_before  # zero LLM


async def test_provenance_and_jobmd_publish_from_snapshots(
    test_db, tmp_path, monkeypatch
):
    """Finding 4: provenance and job.md describe what the LLM stages
    actually used — a model change or re-score between generation and
    publish never rewrites them."""
    import asyncio
    import shutil as shutil_mod
    from pathlib import Path

    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version, score=77.7,
                  breakdown={"match_reasons": ["Original overlap"],
                             "gap_reasons": []})
    db.set_state("pinned_model", "claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")
    _tailor_env(monkeypatch, tmp_path)

    tripped = []
    original_copy = shutil_mod.copy2

    def dying_copy(*args, **kwargs):
        if not tripped:
            tripped.append(1)
            raise asyncio.CancelledError
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(shutil_mod, "copy2", dying_copy)
    with pytest.raises(asyncio.CancelledError):
        await tailor_pipeline.run_package(pkg)  # dies in publishing

    # Between the crash and the resume: engine model changes AND the job
    # is re-scored. Neither may leak into the published record.
    db.set_state("pinned_model", "claude-opus-5")
    db.save_score("p1", "hash-a", version, 33.3,
                  {"match_reasons": ["Different reasons"], "gap_reasons": []})

    assert await tailor_pipeline.run_package(pkg) == "done"
    row = db.get_package(pkg)
    calls = row["provenance"]["llm_calls"]
    assert [(c["call"], c["model"], c["effort"]) for c in calls] == [
        ("generation", "claude-fable-5", "high"),  # at call time
        ("entailment", "claude-fable-5", "medium"),
    ]  # no repair-tier call ever ran, so no such entry exists
    job_md = (Path(row["output_dir"]) / "job.md").read_text()
    assert "77.7" in job_md and "Original overlap" in job_md
    assert "33.3" not in job_md and "Different reasons" not in job_md


# --- Review round 3: job-card pinning, worker cancel, skills dedupe ----------

async def test_job_card_change_after_generation_stays_pinned(
    test_db, tmp_path, monkeypatch
):
    """A search refresh can rewrite company/title/URL without moving the
    JD hash. Everything after generation — cover greeting, output path,
    job.md — must describe the pinned card, not the refreshed one."""
    import asyncio
    from pathlib import Path

    from backend.tailor import page_limit as pl
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    _tailor_env(monkeypatch, tmp_path)

    tripped = []
    original_fit = pl.fit_resume

    async def dying_fit(*args, **kwargs):
        if not tripped:
            tripped.append(1)
            raise asyncio.CancelledError  # crash right after generation
        return await original_fit(*args, **kwargs)

    monkeypatch.setattr(pl, "fit_resume", dying_fit)
    with pytest.raises(asyncio.CancelledError):
        await tailor_pipeline.run_package(pkg)
    row = db.get_package(pkg)
    assert row["status"] == "fitting"
    assert row["stage"]["job_snapshot"]["company"] == "Example Widgets Inc."

    # The refresh: same job id, same description hash, new card fields.
    db.upsert_job({"job_id": "p1", "title": "Renamed Role",
                   "company": "Renamed Corp",
                   "url": "https://www.linkedin.com/jobs/view/p1-renamed/"})
    calls = _fake_compiler(monkeypatch, lambda tex: 1)  # fresh tex capture
    assert await tailor_pipeline.run_package(pkg) == "done"

    row = db.get_package(pkg)
    assert "Example_Widgets_Inc" in row["output_dir"]
    assert "Renamed" not in row["output_dir"]
    job_md = (Path(row["output_dir"]) / "job.md").read_text()
    assert "Example Widgets Inc." in job_md
    assert "Backend Engineer" in job_md
    assert "linkedin.com/jobs/view/p1/" in job_md
    assert "Renamed" not in job_md
    cover_tex = next(tex for name, tex in calls if name == "cover_letter")
    assert "Example Widgets Inc" in cover_tex  # pinned greeting
    assert "Renamed" not in cover_tex


async def test_profile_confirm_cancels_package_worker(test_db, monkeypatch):
    """Confirming a profile supersedes every old-profile package — the
    in-flight worker is cancelled (no wasted high-effort spend) and the
    surviving queue restarts."""
    import asyncio

    from backend.api import routes

    version = db.save_profile(PROFILE)
    monkeypatch.setattr(routes, "_validate_profile", lambda p: None)
    monkeypatch.setattr(routes, "_ensure_preview", lambda v, p: None)
    monkeypatch.setattr(routes, "_cancel_pipeline", lambda: None)
    monkeypatch.setattr(routes, "_maybe_resume_scoring", lambda: False)
    resumed = []
    monkeypatch.setattr(routes, "_maybe_resume_packages",
                        lambda: resumed.append(True) or True)
    worker = asyncio.get_running_loop().create_task(asyncio.sleep(30))
    monkeypatch.setattr(routes, "_package_worker_task", worker)

    await routes.confirm_profile({"version": version})
    await asyncio.gather(worker, return_exceptions=True)
    assert worker.cancelled()  # in-flight spend stopped
    assert resumed == [True]  # surviving queue restarted


# --- Review round 4: immediate checkpoints for LLM sub-stages ----------------

async def test_condense_survives_entailment_failure_without_respend(
    test_db, tmp_path, monkeypatch
):
    """Round-4 blocker: condense succeeds, the FOLLOWING entailment hits
    UsageExhausted, the engine model changes — recovery must reuse the
    persisted condense output (count stays 1) and the ledger must
    attribute every call to the model that made it."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    db.set_state("pinned_model", "claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")

    def pages(tex):
        if "Hiring Team" in tex:  # the cover letter
            return 1 if "A short letter." in tex else 2
        return 1
    _fake_compiler(monkeypatch, pages)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    condense_calls, entail_n, healed = [], [0], [False]

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        return _cited_tailored()[0]

    async def fake_structured(prompt, schema, task, system_prompt=""):
        if task == "repair":  # the condense
            condense_calls.append(1)
            return {"paragraphs": [
                {"text": "A short letter.", "source_ids": []},
                {"text": "Thanks.", "source_ids": []}]}
        assert task == "entail"
        entail_n[0] += 1
        if entail_n[0] == 2 and not healed[0]:
            raise llm.UsageExhausted("weekly limit reached")
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [{"claim_id": cid, "supported": True,
                              "reason": "ok"} for cid in claim_ids]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    assert await tailor_pipeline.run_package(pkg) == "waiting_usage"
    row = db.get_package(pkg)
    assert condense_calls == [1]
    assert "cover_condensed" in row["stage"]  # output survived the crash
    assert [c["call"] for c in row["stage"]["llm_calls"]] == [
        "generation", "entailment", "cover_condense"]  # spend on record

    # Cooldown passes; recovery happens to come back on another model.
    db.set_state("pinned_model", "claude-opus-5")
    healed[0] = True
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert condense_calls == [1]  # the xhigh call was never repeated
    prov = db.get_package(pkg)["provenance"]
    assert [(c["call"], c["model"]) for c in prov["llm_calls"]] == [
        ("generation", "claude-fable-5"),
        ("entailment", "claude-fable-5"),
        ("cover_condense", "claude-fable-5"),
        ("entailment", "claude-opus-5"),  # only the retried entailment
    ]
    assert prov["cover_pages"] == 1


async def test_repair_metadata_survives_revalidation_interruption(
    test_db, tmp_path, monkeypatch
):
    """Round-4 blocker, repair flavor: the repaired JSON and its call
    metadata persist together BEFORE revalidation, so an engine failure
    there can never make provenance claim truth_repair never ran."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    db.set_state("pinned_model", "claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")
    _fake_compiler(monkeypatch, lambda tex: 1)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    fixed = _cited_tailored()[0]
    broken = json.loads(json.dumps(fixed))
    broken["experiences"][0]["bullets"][0]["text"] = "Claims too much impact"
    repair_calls, entail_n, healed = [], [0], [False]

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        return broken

    async def fake_structured(prompt, schema, task, system_prompt=""):
        if task == "repair":
            repair_calls.append(1)
            return fixed
        assert task == "entail"
        entail_n[0] += 1
        if entail_n[0] == 2 and not healed[0]:
            raise llm.AuthRequired("Not logged in")  # dies re-validating
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [
            {"claim_id": cid,
             "supported": not (entail_n[0] == 1 and cid == "exp0:b0"),
             "reason": "checked"}
            for cid in claim_ids]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    assert await tailor_pipeline.run_package(pkg) == "engine_blocked"
    row = db.get_package(pkg)
    assert repair_calls == [1]
    assert row["stage"]["tailored"] == fixed  # repaired JSON persisted...
    assert [c["call"] for c in row["stage"]["llm_calls"]] == [
        "generation", "entailment", "truth_repair"]  # ...with its record

    healed[0] = True
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert repair_calls == [1]  # never repeated
    prov = db.get_package(pkg)["provenance"]
    assert [c["call"] for c in prov["llm_calls"]] == [
        "generation", "entailment", "truth_repair", "entailment"]
    assert prov["llm_calls"][2] == {"call": "truth_repair",
                                    "model": "claude-fable-5",
                                    "effort": "xhigh"}


def _two_round_structured(repair_docs, repair_calls, entail_fail_texts,
                          fail_at=None, healed=None):
    """Fake llm.structured for the bounded-repair tests: each repair call
    pops the next doc from `repair_docs`; entailment supports every claim
    whose text is not in `entail_fail_texts`; the `fail_at`-th entailment
    call raises AuthRequired until `healed[0]` flips."""
    from backend.core import llm

    entail_n = [0]

    async def fake_structured(prompt, schema, task, system_prompt=""):
        if task == "repair":
            repair_calls.append(1)
            return repair_docs.pop(0)
        assert task == "entail"
        entail_n[0] += 1
        if fail_at is not None and entail_n[0] == fail_at and not healed[0]:
            raise llm.AuthRequired("Not logged in")
        rows = [json.loads(line) for line in prompt.splitlines()
                if line.startswith("{")]
        return {"verdicts": [
            {"claim_id": r["claim_id"],
             "supported": r["claim"] not in entail_fail_texts,
             "reason": "checked"}
            for r in rows]}

    return fake_structured


async def test_second_repair_round_recovers_package(
    test_db, tmp_path, monkeypatch
):
    """Round-7 change: a first repair that still fails validation gets
    exactly one more xhigh round; a second repair that grounds the doc
    lets the package finish instead of failing closed."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    db.set_state("pinned_model", "claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")
    _fake_compiler(monkeypatch, lambda tex: 1)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    fixed = _cited_tailored()[0]
    broken = json.loads(json.dumps(fixed))
    broken["experiences"][0]["bullets"][0]["text"] = "Claims too much impact"
    half_fixed = json.loads(json.dumps(fixed))
    half_fixed["experiences"][0]["bullets"][0]["text"] = "Still too much"

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        return broken

    repair_calls = []
    fake_structured = _two_round_structured(
        [half_fixed, fixed], repair_calls,
        {"Claims too much impact", "Still too much"})
    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    assert await tailor_pipeline.run_package(pkg) == "done"
    assert repair_calls == [1, 1]
    prov = db.get_package(pkg)["provenance"]
    assert [c["call"] for c in prov["llm_calls"]] == [
        "generation", "entailment", "truth_repair", "entailment",
        "truth_repair", "entailment"]
    assert prov["llm_calls"][4] == {"call": "truth_repair",
                                    "model": "claude-fable-5",
                                    "effort": "xhigh"}


async def test_restart_after_second_repair_never_repeats_repairs(
    test_db, tmp_path, monkeypatch
):
    """Round-7 change, durability leg: a crash after the second repair
    returned (and checkpointed) resumes with both rounds already on the
    ledger — the restart revalidates but never buys a third repair."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    db.set_state("pinned_model", "claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")
    _fake_compiler(monkeypatch, lambda tex: 1)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    fixed = _cited_tailored()[0]
    broken = json.loads(json.dumps(fixed))
    broken["experiences"][0]["bullets"][0]["text"] = "Claims too much impact"
    half_fixed = json.loads(json.dumps(fixed))
    half_fixed["experiences"][0]["bullets"][0]["text"] = "Still too much"

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        return broken

    repair_calls, healed = [], [False]
    # Entailment call 3 is the revalidation AFTER the second repair —
    # the engine dies there, after the repair itself was checkpointed.
    fake_structured = _two_round_structured(
        [half_fixed, fixed], repair_calls,
        {"Claims too much impact", "Still too much"},
        fail_at=3, healed=healed)
    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    assert await tailor_pipeline.run_package(pkg) == "engine_blocked"
    row = db.get_package(pkg)
    assert repair_calls == [1, 1]
    assert row["stage"]["tailored"] == fixed  # second repair checkpointed
    assert [c["call"] for c in row["stage"]["llm_calls"]] == [
        "generation", "entailment", "truth_repair", "entailment",
        "truth_repair"]

    healed[0] = True
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert repair_calls == [1, 1]  # zero repeated repair calls
    prov = db.get_package(pkg)["provenance"]
    assert [c["call"] for c in prov["llm_calls"]] == [
        "generation", "entailment", "truth_repair", "entailment",
        "truth_repair", "entailment"]


# --- Review round 5: verdicts persist with the checkpoint; condensed wins ----

async def test_entailment_verdicts_persist_with_checkpoint(
    test_db, tmp_path, monkeypatch
):
    """Round-5 blocker: the verdict cache fills BEFORE the entailment
    ledger entry persists, so a kill mid-repair resumes from cached
    verdicts — the already-successful entailment never re-runs, only the
    interrupted repair does."""
    import asyncio

    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")
    _fake_compiler(monkeypatch, lambda tex: 1)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    fixed = _cited_tailored()[0]
    broken = json.loads(json.dumps(fixed))
    broken["experiences"][0]["bullets"][0]["text"] = "Claims too much impact"
    entail_calls, entail_seen_at_repair = [], []
    release = asyncio.Event()
    repair_started = asyncio.Event()

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        return broken

    async def fake_structured(prompt, schema, task, system_prompt=""):
        if task == "repair":
            entail_seen_at_repair.append(len(entail_calls))
            repair_started.set()
            await release.wait()
            return fixed
        assert task == "entail"
        entail_calls.append(1)
        bad = "Claims too much impact" in prompt
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [
            {"claim_id": cid, "supported": not (bad and cid == "exp0:b0"),
             "reason": "checked"} for cid in claim_ids]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    worker = asyncio.create_task(tailor_pipeline.run_package(pkg))
    await asyncio.wait_for(repair_started.wait(), timeout=5)
    worker.cancel()  # the kill: entailment succeeded, repair in flight
    await asyncio.gather(worker, return_exceptions=True)

    row = db.get_package(pkg)
    assert row["status"] == "validating"
    assert [c["call"] for c in row["stage"]["llm_calls"]] == [
        "generation", "entailment"]  # repair never returned: no entry
    cached = row["stage"]["entail_cache"].values()
    assert any(not v["supported"] for v in cached)  # verdicts ON DISK

    # Restart: cached verdicts carry the first validation — only the
    # interrupted repair runs again.
    release.set()
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert entail_seen_at_repair == [1, 1]  # both repair starts saw ONE
    assert len(entail_calls) == 2  # initial + post-repair only
    prov = db.get_package(pkg)["provenance"]
    assert [c["call"] for c in prov["llm_calls"]] == [
        "generation", "entailment", "truth_repair", "entailment"]


async def test_persisted_condense_authoritative_when_engine_changes(
    test_db, tmp_path, monkeypatch
):
    """Round-5 Medium: recovery installs the paid condense output before
    the first compile — even when a changed engine would now fit the
    ORIGINAL letter, the shipped PDF carries the condensed text."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")

    def overflow_pages(tex):
        if "Hiring Team" in tex:  # the cover letter
            return 1 if "A short letter." in tex else 2
        return 1
    _fake_compiler(monkeypatch, overflow_pages)
    monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")

    condense_calls, healed = [], [False]

    async def fake_generate(profile, ledger, job, requirements, breakdown,
                            page_limit):
        return _cited_tailored()[0]

    async def fake_structured(prompt, schema, task, system_prompt=""):
        if task == "repair":
            condense_calls.append(1)
            return {"paragraphs": [
                {"text": "A short letter.", "source_ids": []},
                {"text": "Thanks.", "source_ids": []}]}
        assert task == "entail"
        if not healed[0] and "A short letter." in prompt:
            raise llm.UsageExhausted("weekly limit reached")  # post-condense
        claim_ids = [json.loads(line)["claim_id"]
                     for line in prompt.splitlines() if line.startswith("{")]
        return {"verdicts": [{"claim_id": cid, "supported": True,
                              "reason": "ok"} for cid in claim_ids]}

    monkeypatch.setattr(tailor_resume, "generate_tailored", fake_generate)
    monkeypatch.setattr(llm, "structured", fake_structured)

    assert await tailor_pipeline.run_package(pkg) == "waiting_usage"
    assert condense_calls == [1]
    assert "cover_condensed" in db.get_package(pkg)["stage"]

    # "Engine change": on the restarted process EVERYTHING fits one
    # page — the original letter would compile fine now.
    calls = _fake_compiler(monkeypatch, lambda tex: 1)
    healed[0] = True
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert condense_calls == [1]  # still exactly one paid condense
    cover_texes = [tex for name, tex in calls if name == "cover_letter"]
    assert cover_texes  # cover really compiled in the restarted process
    assert all("A short letter." in tex for tex in cover_texes)
    assert "I would love to join your team." not in cover_texes[-1]
    row = db.get_package(pkg)
    letter = row["stage"]["cover_fit"]["tailored"]["cover_letter"]
    assert letter["paragraphs"][0]["text"] == "A short letter."


def test_skills_order_duplicates_rejected_everywhere():
    """Schema forbids duplicate categories, the deterministic validator
    flags them, and the renderer never duplicates a group even if both
    somehow miss."""
    from jsonschema import Draft202012Validator, ValidationError

    from backend.tailor import resume as tailor_resume
    from backend.tailor import validate

    dup = _tailored(skills_order=["Tools", "Tools"])
    with pytest.raises(ValidationError):
        Draft202012Validator(tailor_resume.TAILOR_SCHEMA).validate(dup)

    ledger, _ = _ledger_and_ids()
    failures = validate.deterministic_failures(dup, PROFILE, ledger)
    assert any(f["claim_id"] == "skills_order"
               and "duplicate" in f["reason"] for f in failures)

    merged = tailor_resume.tailored_profile(PROFILE, dup)
    assert [g["category"] for g in merged["skills"]] == ["Tools", "Languages"]
