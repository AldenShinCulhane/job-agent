"""Tailoring pipeline tests (PLAN.md §4.5/§4.6, M4). All data fake."""

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
