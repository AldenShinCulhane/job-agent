"""Tailoring pipeline tests (PLAN.md §4.5/§4.6, M4). All data fake."""

import json

import pytest

from backend.core import db

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
    """One package per (job, profile version, JD hash): re-creating
    returns the existing row; changed inputs create a new package."""
    _seed_job()
    pkg1, created1 = db.create_package("p1", 4, "hash-a")
    pkg2, created2 = db.create_package("p1", 4, "hash-a")
    assert created1 is True and created2 is False
    assert pkg1 == pkg2

    pkg3, created3 = db.create_package("p1", 5, "hash-a")  # new profile
    pkg4, created4 = db.create_package("p1", 4, "hash-b")  # edited posting
    assert created3 and created4
    assert len({pkg1, pkg3, pkg4}) == 3


def test_package_update_and_stage_roundtrip(test_db):
    _seed_job()
    pkg, _ = db.create_package("p1", 4, "hash-a")
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
    db.create_package("p1", 4, "hash-a")
    db.create_package("p2", 4, "hash-b")
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
            "closing": "Sincerely,",
        },
    }
    base.update(overrides)
    return base


def test_tailor_schema_is_valid_and_accepts_shape():
    from jsonschema import Draft202012Validator

    from backend.tailor import resume as tailor_resume

    Draft202012Validator.check_schema(tailor_resume.TAILOR_SCHEMA)
    Draft202012Validator(tailor_resume.TAILOR_SCHEMA).validate(_tailored())


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
        closing="Sincerely,",
    )
    assert "Dear Sample Systems \\& Co Hiring Team," in tex
    assert r"C\&C 100\% of the time" in tex
    assert r"\textbackslash{}textbf" in tex  # LLM LaTeX stays literal text
    assert tex.count("Casey Example") == 2  # header + signature
    assert "casey@example.com" in tex
    assert "source_ids" not in tex  # internal metadata never rendered


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

    # Cached verdicts make an unchanged-claims revalidation free.
    failures2 = await validate.validate(tailored, PROFILE, ledger, cache)
    assert calls == ["entail", "entail"]  # cover:p1 was never cached...
    assert "exp0:b0" in {f["claim_id"] for f in failures2}  # ...but this hit cache


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
    """Replace the LaTeX engine with a deterministic page counter."""
    from backend.tailor import compile as latex

    calls = []

    def fake_compile(tex, workdir, jobname="document"):
        calls.append((jobname, tex))
        workdir.mkdir(parents=True, exist_ok=True)
        pdf = workdir / f"{jobname}.pdf"
        pdf.write_text("fake")
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

    async def fake_validate(tailored, profile, ledger, cache):
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

    async def fake_validate(*a):
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
    monkeypatch.setattr(page_limit, "last_page_fullness", lambda p: 0.1)
    tailored, ledger = _cited_tailored()
    fit = await page_limit.fit_resume(tailored, PROFILE, ledger, {}, 2, tmp_path)
    assert len(calls) == 2  # initial + one balance pass
    assert fit.layout.item_sep_pt == 0  # the balance pass tightened spacing
    assert fit.result.pages == 2


async def test_fit_cover_ladder_then_condense_then_fail(tmp_path, monkeypatch):
    import pytest as _pytest

    from backend.core import llm
    from backend.tailor import page_limit, validate

    # Fits only when the letter mentions "short" — layout never fixes it.
    _fake_compiler(
        monkeypatch, lambda tex: 1 if "short" in tex else 2)
    condensed = {"paragraphs": [{"text": "A short letter.", "source_ids": []},
                                {"text": "Thanks.", "source_ids": []}],
                 "closing": "Sincerely,"}
    llm_calls = []

    async def fake_structured(prompt, schema, task, system_prompt=""):
        llm_calls.append(task)
        return condensed

    async def fake_validate(tailored, profile, ledger, cache):
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


def test_packages_to_resume_excludes_terminal_states(test_db):
    """Restart recovery picks up mid-flight packages in batch order and
    never a done/failed one (PLAN §4.6: completed work is never redone)."""
    for i, status in enumerate(
        ("done", "failed", "generating", "queued", "waiting_usage"), start=1
    ):
        _seed_job(f"p{i}")
        pkg, _ = db.create_package(f"p{i}", 4, "hash-a")
        db.update_package(pkg, status=status)
    resumable = db.packages_to_resume()
    assert [p["job_id"] for p in resumable] == ["p3", "p4", "p5"]


# --- Package pipeline (state machine, restart, isolation) --------------------

def _confirm_profile():
    version = db.save_profile(PROFILE)
    db.confirm_profile(version)
    return version


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
    db.save_score("p1", "hash-a", version, 79.8,
                  {"match_reasons": ["Strong Python overlap"],
                   "gap_reasons": []})
    generate_calls, entail_calls = _tailor_env(monkeypatch, tmp_path)
    pkg, _ = db.create_package("p1", version, "hash-a")

    assert await tailor_pipeline.run_package(pkg) == "done"
    row = db.get_package(pkg)
    assert row["status"] == "done"
    out = tmp_path / "output" / "Example_Widgets_Inc_p1"
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
    assert prov["cover_pages"] == 1 and prov["prompt_version"] >= 1
    assert generate_calls == ["p1"] and entail_calls == ["entail"]

    # Completed packages are never redone: no state change, no LLM calls.
    assert await tailor_pipeline.run_package(pkg) == "done"
    assert generate_calls == ["p1"] and entail_calls == ["entail"]


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
        pkg, _ = db.create_package(f"p{i}", version, "hash-a")
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
        pkg, _ = db.create_package(f"p{i}", version, "hash-a")
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
        pkg, _ = db.create_package(f"p{i}", version, "hash-a")
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


async def test_engine_blocked_parks_package_queued_and_stops_batch(
    test_db, tmp_path, monkeypatch
):
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    packages = []
    for i in (1, 2):
        _seed_job(f"p{i}")
        pkg, _ = db.create_package(f"p{i}", version, "hash-a")
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
    pkg, _ = db.create_package("p1", version, "hash-a")
    _tailor_env(monkeypatch, tmp_path)

    async def always_failing(tailored, profile, ledger, cache):
        return [{"claim_id": "exp0:b0", "reason": "fabricated metric"}]

    repairs = []

    async def fake_repair(tailored, failures, profile, ledger):
        repairs.append(1)
        return tailored

    monkeypatch.setattr(validate_mod, "validate", always_failing)
    monkeypatch.setattr(validate_mod, "repair", fake_repair)
    assert await tailor_pipeline.run_package(pkg) == "failed"
    row = db.get_package(pkg)
    assert repairs == [1]  # exactly one repair round
    assert "truth validation failed after repair" in row["error"]
    assert "fabricated metric" in row["error"]


async def test_package_fails_when_posting_changed_before_generation(
    test_db, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline

    version = _confirm_profile()
    _seed_job("p1")
    pkg, _ = db.create_package("p1", version, "hash-a")
    db.save_job_detail("p1", "Rewritten posting.", "hash-new")  # edited
    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)
    assert await tailor_pipeline.run_package(pkg) == "failed"
    assert generate_calls == []  # no LLM spend on stale inputs
    assert "posting changed" in db.get_package(pkg)["error"]


async def test_generate_endpoint_gates_and_creates(test_db, monkeypatch):
    from fastapi import HTTPException

    from backend.api import routes

    spawned = []
    monkeypatch.setattr(routes, "_spawn_package_worker",
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
