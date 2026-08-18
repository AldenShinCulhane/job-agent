"""Labeled scoring fixtures (PLAN.md §6 M3): exact skill match, aliases,
false substring matches, YoE boundaries, missing requirements, education
matching — with expected ordering and filter behavior. All data fake."""

import pytest

from backend.core import db
from backend.scoring import score as scoring
from backend.scoring.score import (
    education_score,
    experience_score,
    flatten_profile_skills,
    profile_years_experience,
    score_job,
    skill_score,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# A fake profile: ~2 years of experience, bachelor's, JS/TS/Python stack.
PROFILE = {
    "name": "Casey Example",
    "headline": None,
    "contact": {"phone": None, "email": None, "linkedin": None,
                "github": None, "website": None, "location": "Toronto, ON"},
    "experiences": [
        {"title": "Software Engineer", "company": "Example Widgets Inc.",
         "location": None, "start": "Jun 2024", "end": "Present", "bullets": []},
        {"title": "Engineering Intern", "company": "Sample Systems",
         "location": None, "start": "May 2023", "end": "Aug 2023", "bullets": []},
    ],
    "projects": [
        {"name": "Side Project", "technologies": ["React", "Node.js"],
         "date": None, "link": None, "bullets": []},
    ],
    "education": [
        {"institution": "Example University", "location": None,
         "degree": "Bachelor of Science, Computer Science",
         "start": "2019", "end": "2023", "details": []},
    ],
    "skills": [
        {"category": "Languages", "items": ["JavaScript", "TypeScript", "Python"]},
        {"category": "Tools", "items": ["PostgreSQL", "Docker", "AWS"]},
    ],
}

NOW = __import__("datetime").datetime(2026, 8, 1,
                                      tzinfo=__import__("datetime").timezone.utc)


def req(**overrides):
    base = {
        "required_skills": [], "preferred_skills": [],
        "min_years_experience": None, "education_requirement": None,
        "seniority": None, "workplace": None, "compensation": None,
    }
    base.update(overrides)
    return base


# --- skill matching ----------------------------------------------------------

def test_exact_skill_match():
    s, matched, missing = skill_score(
        req(required_skills=["Python", "PostgreSQL"]),
        flatten_profile_skills(PROFILE),
    )
    assert s == 100.0
    assert sorted(matched) == ["postgresql", "python"]
    assert missing == []


def test_skill_aliases_match():
    # User lists JavaScript/PostgreSQL/AWS; posting uses variants.
    s, matched, _ = skill_score(
        req(required_skills=["JS", "Postgres", "Amazon Web Services"]),
        flatten_profile_skills(PROFILE),
    )
    assert s == 100.0
    assert len(matched) == 3


def test_java_is_not_javascript():
    """The v1 substring rule made 'Java' match 'JavaScript'. It must not."""
    user = flatten_profile_skills(PROFILE)  # has javascript, NOT java
    s, matched, missing = skill_score(req(required_skills=["Java"]), user)
    assert s == 0.0
    assert matched == []
    assert missing == ["java"]
    # And the reverse: a Java-only profile must not satisfy JavaScript.
    java_only = {"skills": [{"category": "L", "items": ["Java"]}], "projects": []}
    s2, _, missing2 = skill_score(
        req(required_skills=["JavaScript"]), flatten_profile_skills(java_only)
    )
    assert s2 == 0.0
    assert missing2 == ["javascript"]


def test_required_skills_weigh_double():
    user = flatten_profile_skills(PROFILE)
    # required Python (matched, 2) + preferred Rust (missed, 1) = 2/3
    s, _, _ = skill_score(
        req(required_skills=["Python"], preferred_skills=["Rust"]), user
    )
    assert s == pytest.approx(66.7, abs=0.1)


def test_no_skill_data_is_neutral():
    s, matched, missing = skill_score(req(), flatten_profile_skills(PROFILE))
    assert s == 50.0 and matched == [] and missing == []


# --- years of experience -----------------------------------------------------

def test_profile_years_merges_overlaps():
    years = profile_years_experience(PROFILE, now=NOW)
    # Jun 2024 -> Aug 2026 = 26 months, plus May-Aug 2023 = 3 months.
    assert years == pytest.approx(29 / 12, abs=0.1)


def test_experience_boundaries():
    assert experience_score(req(min_years_experience=2), 2.0) == (100.0, False)
    over, flag_over = experience_score(req(min_years_experience=2), 4.0)
    under, flag_under = experience_score(req(min_years_experience=4), 2.0)
    assert over > under  # overshoot decays gentler than undershoot
    assert flag_over is False
    assert flag_under is True  # 4 > 2 + tolerance(1) -> flagged
    # Within tolerance: demanding one more year than the user has is NOT
    # flagged, though it scores below a perfect match.
    within, flag_within = experience_score(req(min_years_experience=3), 2.0)
    assert flag_within is False and within < 100.0


def test_experience_missing_data_is_neutral():
    assert experience_score(req(), 2.0) == (70.0, False)
    assert experience_score(req(min_years_experience=3), None) == (70.0, False)


# --- education ---------------------------------------------------------------

def test_education_matrix():
    assert education_score(req(education_requirement="bachelor"), PROFILE) == 100.0
    assert education_score(req(education_requirement="associate"), PROFILE) == 90.0
    assert education_score(req(education_requirement="master"), PROFILE) == 30.0
    assert education_score(req(), PROFILE) == 80.0


# --- composite: labeled ordering + filter behavior ---------------------------

LABELED_JOBS = {
    # Perfect fit: exact stack, exact YoE, bachelor.
    "fit": req(required_skills=["Python", "TypeScript"],
               min_years_experience=2, education_requirement="bachelor"),
    # Good but missing one skill.
    "near": req(required_skills=["Python", "Rust"],
                min_years_experience=2, education_requirement="bachelor"),
    # Wrong stack + senior YoE demand.
    "far": req(required_skills=["Java", "Spring", "Scala"],
               min_years_experience=8, education_requirement="master"),
}


def test_labeled_ordering_and_filter():
    scores = {
        name: score_job(r, PROFILE, user_years=2.0)
        for name, r in LABELED_JOBS.items()
    }
    ordered = sorted(scores, key=lambda n: scores[n]["deterministic_score"],
                     reverse=True)
    assert ordered == ["fit", "near", "far"]
    assert scores["fit"]["breakdown"]["exceeds_experience"] is False
    assert scores["far"]["breakdown"]["exceeds_experience"] is True
    assert "Missing skills" in scores["far"]["breakdown"]["gap_reasons"][0]


def test_scoring_is_reproducible():
    a = score_job(LABELED_JOBS["near"], PROFILE, user_years=2.0)
    b = score_job(LABELED_JOBS["near"], PROFILE, user_years=2.0)
    assert a == b


# --- normalization cache + invalidation cascade ------------------------------

@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


def _seed_job(job_id="n1", description="We need Python.", jd_hash="hash-a"):
    db.upsert_job({
        "job_id": job_id, "title": "Backend Engineer",
        "company": "Example Widgets Inc.", "location": "Toronto, ON",
        "posted_text": None, "workplace": None, "easy_apply": False,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
    })
    db.save_job_detail(job_id, description, jd_hash)


async def test_normalization_cached_by_jd_hash(test_db, monkeypatch):
    from backend.core import llm
    from backend.normalize import requirements as norm

    calls = []

    async def fake_structured(prompt, schema, task, system_prompt=""):
        calls.append(task)
        return req(required_skills=["Python"])

    monkeypatch.setattr(llm, "structured", fake_structured)
    _seed_job()

    first = await norm.normalize_job("n1")
    second = await norm.normalize_job("n1")  # same hash -> cache hit
    assert first == second
    assert calls == ["normalize"]

    # Hash change (edited posting) -> cascade invalidated, re-extracted.
    db.save_score("n1", "hash-a", 1, 88.0, {"skills": 100.0})
    db.invalidate_derived("n1")
    db.save_job_detail("n1", "We need Rust now.", "hash-b")
    assert db.get_requirements("n1") is None
    assert db.get_score("n1") is None
    await norm.normalize_job("n1")
    assert calls == ["normalize", "normalize"]
    assert test_db.get_requirements("n1")["jd_hash"] == "hash-b"


async def test_detail_hash_change_invalidates_cascade(test_db):
    """fetch_detail must drop stale requirements/scores when the posting
    content moved (PLAN.md §4.4)."""
    from backend.sources.linkedin import job_detail

    class FakeSup:
        async def fetch_page(self, url, *, mode="results"):
            return (
                "<html><body><div id='JobDetails_AboutTheJob_1'>"
                "<h2>About the job</h2><p>" + "Changed content. " * 30
                + "</p></div></body></html>"
            )

    _seed_job(description="Original text.", jd_hash="stale-hash")
    db.save_requirements("n1", "stale-hash", req(required_skills=["Python"]))
    db.save_score("n1", "stale-hash", 1, 90.0, {"skills": 100.0})

    result = await job_detail.fetch_detail(FakeSup(), "n1", force=True)
    assert result["changed"] is True
    assert db.get_requirements("n1") is None
    assert db.get_score("n1") is None


def test_llm_rerank_never_overwrites_baseline(test_db):
    _seed_job()
    db.save_score("n1", "hash-a", 1, 77.5, {"skills": 90.0})
    db.set_llm_rerank("n1", 84.0, {"pitch": "solid fit"})
    stored = db.get_score("n1")
    assert stored["deterministic_score"] == 77.5  # baseline untouched
    assert stored["llm_rerank"] == 84.0
    assert stored["explanation"] == {"pitch": "solid fit"}
    # Re-scoring resets the LLM layer — it described the old baseline.
    db.save_score("n1", "hash-b", 2, 61.0, {"skills": 70.0})
    stored = db.get_score("n1")
    assert stored["llm_rerank"] is None and stored["explanation"] is None
