"""Labeled scoring fixtures (PLAN.md §6 M3): exact skill match, aliases,
false substring matches, YoE boundaries, missing requirements, education
matching — with expected ordering and filter behavior. All data fake."""

import asyncio

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

    # The cache is content-addressed: a DIFFERENT job with the same
    # description hash reuses the extraction — no second LLM call.
    _seed_job(job_id="n2", description="We need Python.", jd_hash="hash-a")
    assert await norm.normalize_job("n2") == first
    assert calls == ["normalize"]

    # Hash change (edited posting) -> score invalidated, re-extracted;
    # the old cache row survives for jobs still carrying hash-a.
    db.save_score("n1", "hash-a", 1, 88.0, {"skills": 100.0})
    db.invalidate_derived("n1")
    db.save_job_detail("n1", "We need Rust now.", "hash-b")
    assert db.get_score("n1") is None
    assert db.get_requirements(norm.cache_key("hash-a")) is not None
    await norm.normalize_job("n1")
    assert calls == ["normalize", "normalize"]
    assert test_db.get_requirements(norm.cache_key("hash-b")) is not None


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
    db.save_requirements("stale-hash", req(required_skills=["Python"]))
    db.save_score("n1", "stale-hash", 1, 90.0, {"skills": 100.0})

    result = await job_detail.fetch_detail(FakeSup(), "n1", force=True)
    assert result["changed"] is True
    assert db.get_score("n1") is None
    # Content-addressed requirements survive — another job may share the
    # old description; the new hash simply misses the cache.
    assert db.get_requirements("stale-hash") is not None


# --- automatic pipeline ------------------------------------------------------

def _confirm_profile() -> int:
    version = db.save_profile(PROFILE)
    db.confirm_profile(version)
    return version


def _seed_run(job_ids):
    run_id = db.create_search_run({"keywords": "x", "cap": 50})
    db.record_search_page(
        run_id,
        [
            {
                "job_id": j, "title": f"Role {j}", "company": "Example Co",
                "location": None, "posted_text": None, "workplace": None,
                "easy_apply": False,
                "url": f"https://www.linkedin.com/jobs/view/{j}/",
            }
            for j in job_ids
        ],
        next_start=len(job_ids),
        cap=50,
    )
    db.update_search_run(run_id, status="done")
    return run_id


@pytest.fixture
def fake_source(monkeypatch):
    """fetch_detail persists a description like the real one; page_delay
    is instant. Yields the list of fetched job ids."""
    from backend import sources

    fetched = []

    async def fake_fetch_detail(job_id, force=False):
        job = db.get_job(job_id)
        if job.get("description"):
            return {**job, "refetched": False, "changed": False}
        fetched.append(job_id)
        db.save_job_detail(job_id, f"Description for {job_id}", f"hash-{job_id}")
        return {**db.get_job(job_id), "refetched": True, "changed": False}

    async def instant():
        pass

    monkeypatch.setattr(sources.source, "fetch_detail", fake_fetch_detail)
    monkeypatch.setattr(sources, "page_delay", instant)
    return fetched


@pytest.fixture
def fake_normalize_llm(monkeypatch):
    """llm.structured stand-in for normalization; yields call log."""
    from backend.core import llm

    calls = []

    async def fake_structured(prompt, schema, task, system_prompt=""):
        calls.append(task)
        return req(required_skills=["Python"], min_years_experience=2,
                   education_requirement="bachelor")

    monkeypatch.setattr(llm, "structured", fake_structured)
    return calls


async def test_pipeline_scores_run_idempotently(test_db, fake_source,
                                                fake_normalize_llm):
    from backend.scoring import pipeline

    version = _confirm_profile()
    run_id = _seed_run(["p1", "p2"])
    assert pipeline.pending_count(run_id) == 2

    summary = await pipeline.run_pipeline(run_id)
    assert summary == {"scored": 2, "skipped": 0, "failed": 0,
                       "stopped": None, "detail": None}
    assert pipeline.pending_count(run_id) == 0
    assert fake_source == ["p1", "p2"]
    assert fake_normalize_llm == ["normalize", "normalize"]
    score = db.get_score("p1")
    assert score["profile_version"] == version
    assert score["deterministic_score"] > 0

    # Second pass: everything cached — no fetches, no LLM, all skipped.
    summary = await pipeline.run_pipeline(run_id)
    assert summary == {"scored": 0, "skipped": 2, "failed": 0,
                       "stopped": None, "detail": None}
    assert fake_source == ["p1", "p2"]
    assert fake_normalize_llm == ["normalize", "normalize"]


async def test_pipeline_rescore_on_new_profile_costs_no_llm(
    test_db, fake_source, fake_normalize_llm
):
    """A new confirmed profile re-scores from cached requirements —
    deterministic only, zero extraction calls."""
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1"])
    await pipeline.run_pipeline(run_id)
    assert fake_normalize_llm == ["normalize"]

    new_version = _confirm_profile()  # version bump -> scores stale
    assert pipeline.pending_count(run_id) == 1
    summary = await pipeline.run_pipeline(run_id)
    assert summary["scored"] == 1
    assert fake_normalize_llm == ["normalize"]  # cache hit — no new call
    assert db.get_score("p1")["profile_version"] == new_version


async def test_pipeline_stops_on_session_loss_and_isolates_failures(
    test_db, fake_normalize_llm, monkeypatch
):
    from backend import sources
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1", "p2", "p3"])

    calls = []

    async def flaky_fetch_detail(job_id, force=False):
        calls.append(job_id)
        if job_id == "p2":
            raise sources.AuthRequired("session expired")
        db.save_job_detail(job_id, f"Description for {job_id}", f"hash-{job_id}")
        return {**db.get_job(job_id), "refetched": True, "changed": False}

    async def instant():
        pass

    monkeypatch.setattr(sources.source, "fetch_detail", flaky_fetch_detail)
    monkeypatch.setattr(sources, "page_delay", instant)

    summary = await pipeline.run_pipeline(run_id)
    # p1 scored, then the session died on p2 -> pass ends, p3 untouched.
    assert summary["scored"] == 1 and summary["stopped"] == "session"
    assert calls == ["p1", "p2"]
    assert pipeline.pending_count(run_id) == 2  # p2 + p3 remain


async def test_pipeline_needs_confirmed_profile(test_db):
    from backend.scoring import pipeline

    run_id = _seed_run(["p1"])
    assert pipeline.pending_count(run_id) == 0  # nothing scorable yet
    with pytest.raises(pipeline.NoConfirmedProfile):
        await pipeline.run_pipeline(run_id)


async def test_maybe_resume_scoring_spawns_for_done_run(test_db, monkeypatch):
    from backend.api import routes
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1"])
    ran = []

    async def fake_run(rid):
        ran.append(rid)
        return {"scored": 1, "skipped": 0, "failed": 0, "stopped": None}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    monkeypatch.setattr(routes, "_pipeline_task", None)
    assert routes._maybe_resume_scoring() is True
    await routes._pipeline_task
    assert ran == [run_id]
    # Fully scored run -> no spawn.
    db.save_job_detail("p1", "d", "h")
    db.save_score("p1", "h", db.latest_profile()[0], 50.0, {})
    assert routes._maybe_resume_scoring() is False


def test_llm_rerank_never_overwrites_baseline(test_db):
    _seed_job()
    db.save_score("n1", "hash-a", 1, 77.5, {"skills": 90.0})
    assert db.apply_rerank([("n1", "hash-a", 84.0, 1)], 1) is True
    assert db.set_explanation("n1", {"pitch": "solid fit"}, 1, "hash-a") is True
    stored = db.get_score("n1")
    assert stored["deterministic_score"] == 77.5  # baseline untouched
    assert stored["llm_rerank"] == 84.0 and stored["llm_rank"] == 1
    assert stored["explanation"] == {"pitch": "solid fit"}
    # Re-scoring resets the LLM layer — it described the old baseline.
    db.save_score("n1", "hash-b", 2, 61.0, {"skills": 70.0})
    stored = db.get_score("n1")
    assert stored["llm_rerank"] is None and stored["llm_rank"] is None
    assert stored["explanation"] is None


# --- LLM layer: explain + close-match rerank ---------------------------------

async def test_explain_cached_on_score_row(test_db, monkeypatch):
    from backend.core import llm
    from backend.scoring import llm_layer

    _confirm_profile()
    _seed_job()
    db.save_score("n1", "hash-a", 1, 80.0,
                  {"matched_skills": ["python"], "missing_skills": []})
    calls = []

    async def fake_structured(prompt, schema, task, system_prompt=""):
        calls.append(task)
        return {"strengths": ["python"], "gaps": [], "pitch": "good fit"}

    monkeypatch.setattr(llm, "structured", fake_structured)
    first = await llm_layer.explain_job("n1")
    second = await llm_layer.explain_job("n1")  # cached — no second call
    assert first == second
    assert calls == ["score_explain"]
    assert db.get_score("n1")["explanation"]["pitch"] == "good fit"


async def test_rerank_orders_close_matches_within_band(test_db, monkeypatch):
    from backend.core import llm
    from backend.scoring import llm_layer

    _confirm_profile()
    run_id = _seed_run(["r1", "r2", "r3"])
    for job_id, score in (("r1", 90.0), ("r2", 88.0), ("r3", 60.0)):
        db.save_job_detail(job_id, "d", f"h-{job_id}")
        db.save_score(job_id, f"h-{job_id}", 1, score,
                      {"matched_skills": [], "missing_skills": []})

    async def fake_structured(prompt, schema, task, system_prompt=""):
        assert "r3" not in prompt  # 60 is outside the band — never sent
        return {"ordered_job_ids": ["r2", "r1"]}  # LLM prefers r2

    monkeypatch.setattr(llm, "structured", fake_structured)
    result = await llm_layer.rerank_close_matches(run_id)
    assert result["reranked"] == 2
    # Rerank stays inside the cluster's own range; baseline untouched;
    # rank ordinals record the LLM's order explicitly.
    assert db.get_score("r2")["llm_rerank"] == 90.0
    assert db.get_score("r2")["llm_rank"] == 1
    assert db.get_score("r1")["llm_rerank"] == 88.0
    assert db.get_score("r1")["llm_rank"] == 2
    assert db.get_score("r1")["deterministic_score"] == 90.0
    assert db.get_score("r3")["llm_rerank"] is None


async def test_rerank_all_equal_cluster_keeps_order_via_rank(test_db, monkeypatch):
    """Identical cluster scores collapse rerank values to one number —
    the rank ordinal is what preserves the LLM's order."""
    from backend.core import llm
    from backend.scoring import llm_layer

    _confirm_profile()
    run_id = _seed_run(["e1", "e2", "e3"])
    for job_id in ("e1", "e2", "e3"):
        db.save_job_detail(job_id, "d", f"h-{job_id}")
        db.save_score(job_id, f"h-{job_id}", 1, 80.0,
                      {"matched_skills": [], "missing_skills": []})

    async def fake_structured(prompt, schema, task, system_prompt=""):
        return {"ordered_job_ids": ["e3", "e1", "e2"]}

    monkeypatch.setattr(llm, "structured", fake_structured)
    await llm_layer.rerank_close_matches(run_id)
    assert [db.get_score(j)["llm_rerank"] for j in ("e3", "e1", "e2")] == [
        80.0, 80.0, 80.0,
    ]
    assert [db.get_score(j)["llm_rank"] for j in ("e3", "e1", "e2")] == [1, 2, 3]


async def test_explain_refuses_stale_score(test_db, monkeypatch):
    """A score row from an older profile version (or an edited posting)
    reads as unscored — never explained with mixed-era data."""
    from backend.scoring import llm_layer

    _confirm_profile()
    current = db.latest_profile()[0]
    _seed_job()
    db.save_score("n1", "hash-a", current - 1 or 999, 80.0,
                  {"matched_skills": [], "missing_skills": []})
    with pytest.raises(ValueError):
        await llm_layer.explain_job("n1")
    # Right version but stale hash: same refusal.
    db.save_score("n1", "old-hash", current, 80.0,
                  {"matched_skills": [], "missing_skills": []})
    with pytest.raises(ValueError):
        await llm_layer.explain_job("n1")


def test_list_jobs_hides_stale_scores(test_db):
    _confirm_profile()
    version = db.latest_profile()[0]
    run_id = _seed_run(["s1", "s2"])
    db.save_job_detail("s1", "d", "h-s1")
    db.save_job_detail("s2", "d", "h-s2")
    db.save_score("s1", "h-s1", version, 90.0, {})       # fresh
    db.save_score("s2", "h-s2", version + 1, 70.0, {})   # other version
    by_id = {j["job_id"]: j for j in db.list_jobs(run_id, profile_version=version)}
    assert by_id["s1"]["deterministic_score"] == 90.0
    assert by_id["s2"]["deterministic_score"] is None  # stale -> unscored


async def test_pipeline_stops_on_engine_error(test_db, fake_source, monkeypatch):
    """A typed engine failure ends the pass instead of repeating per-job
    across the whole run."""
    from backend.core import llm
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1", "p2", "p3"])
    calls = []

    async def broken_structured(prompt, schema, task, system_prompt=""):
        calls.append(task)
        raise llm.TransientFailure("engine hiccup")

    monkeypatch.setattr(llm, "structured", broken_structured)
    summary = await pipeline.run_pipeline(run_id)
    assert summary["stopped"] == "engine"
    assert summary["failed"] == 0
    assert len(calls) == 1  # stopped at the first failure, not 3


async def test_pipeline_token_stops_stale_profile_write(
    test_db, fake_normalize_llm, monkeypatch
):
    """A profile confirmed mid-pass must not receive old-version scores:
    the generation token check before the write ends the pass."""
    from backend import sources
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1", "p2"])

    async def confirming_fetch_detail(job_id, force=False):
        db.save_job_detail(job_id, f"Description for {job_id}", f"hash-{job_id}")
        # The user confirms a new profile while this job's detail fetch
        # is in flight.
        _confirm_profile()
        return {**db.get_job(job_id), "refetched": True, "changed": False}

    async def instant():
        pass

    monkeypatch.setattr(sources.source, "fetch_detail", confirming_fetch_detail)
    monkeypatch.setattr(sources, "page_delay", instant)

    summary = await pipeline.run_pipeline(run_id)
    assert summary["stopped"] == "stale_profile"
    assert summary["scored"] == 0
    assert db.get_score("p1") is None  # nothing written under the old version


async def test_pipeline_worker_arms_bounded_retry(test_db, monkeypatch):
    """Engine stops arm the shutdown-owned retry timer with backoff, and
    the circuit breaker gives up after the retry budget."""
    from backend.api import routes
    from backend.scoring import pipeline

    delays = []

    async def fake_run(run_id):
        return {"scored": 0, "skipped": 0, "failed": 0, "stopped": "engine",
                "detail": "engine hiccup"}

    def fake_arm(delay):
        delays.append(delay)

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    monkeypatch.setattr(routes, "_arm_pipeline_retry", fake_arm)
    monkeypatch.setattr(routes, "_pipeline_retry_attempts", 0)
    for _ in range(routes._PIPELINE_MAX_RETRIES + 2):
        await routes._pipeline_worker(1)
    assert delays == [60.0, 120.0, 240.0]  # bounded backoff, then break
    # The open circuit is user-visible, not just a log line.
    assert routes._get_scoring_stall() == {
        "reason": "retry_exhausted", "detail": "engine hiccup",
    }


async def test_pipeline_reprocesses_jd_refresh_during_normalization(
    test_db, fake_source, monkeypatch
):
    """A forced detail refresh landing while normalization awaits must
    not strand the job (old-hash score = invisible to the UI but counted
    scored): the hash is re-read before the write and the job reruns
    from the new text."""
    from backend.core import llm
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1"])
    calls = []

    async def mutating_structured(prompt, schema, task, system_prompt=""):
        calls.append(task)
        if len(calls) == 1:
            # User force-refreshes the posting mid-normalization.
            db.save_job_detail("p1", "Rewritten posting.", "hash-new")
        return req(required_skills=["Python"])

    monkeypatch.setattr(llm, "structured", mutating_structured)
    summary = await pipeline.run_pipeline(run_id)
    assert summary["scored"] == 1 and summary["failed"] == 0
    assert calls == ["normalize", "normalize"]  # reprocessed once
    assert db.get_score("p1")["jd_hash"] == "hash-new"  # stamped current
    assert pipeline.pending_count(run_id) == 0  # no endless "Scoring…"


async def test_pipeline_blocked_engine_is_typed(test_db, fake_source, monkeypatch):
    from backend.core import llm
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1", "p2"])

    async def blocked_structured(prompt, schema, task, system_prompt=""):
        raise llm.AuthRequired("Not logged in")

    monkeypatch.setattr(llm, "structured", blocked_structured)
    summary = await pipeline.run_pipeline(run_id)
    assert summary["stopped"] == "engine_blocked"
    assert "Not logged in" in summary["detail"]


async def test_worker_persists_blocked_stall_without_timer(test_db, monkeypatch):
    """Auth/config engine failures are human-actionable: persist the
    reason, arm NO retry timer."""
    from backend.api import routes
    from backend.scoring import pipeline

    armed = []

    async def fake_run(run_id):
        return {"scored": 0, "skipped": 0, "failed": 0,
                "stopped": "engine_blocked", "detail": "Not logged in"}

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    monkeypatch.setattr(routes, "_arm_pipeline_retry", lambda d: armed.append(d))
    monkeypatch.setattr(routes, "_pipeline_retry_attempts", 0)
    await routes._pipeline_worker(1)
    assert armed == []
    assert routes._get_scoring_stall() == {
        "reason": "engine_blocked", "detail": "Not logged in",
    }


async def test_probe_success_clears_stall_and_resumes(test_db, monkeypatch):
    from backend.api import routes
    from backend.core import llm

    routes._set_scoring_stall("engine_blocked", "Not logged in")
    monkeypatch.setattr(routes, "_pipeline_retry_attempts", 2)
    resumed = []

    async def fake_probe():
        return "claude-fable-5"

    monkeypatch.setattr(llm, "probe", fake_probe)
    monkeypatch.setattr(routes, "_maybe_resume_scoring",
                        lambda: resumed.append(True) or True)
    result = await routes.probe_engine()
    assert result["ok"] is True
    assert routes._get_scoring_stall() is None
    assert routes._pipeline_retry_attempts == 0
    assert resumed == [True]


async def test_explain_mid_call_mutation_refused(test_db, monkeypatch):
    """A score row rewritten during the explanation call must not
    receive the old-data explanation."""
    from backend.core import llm
    from backend.scoring import llm_layer

    _confirm_profile()
    version = db.latest_profile()[0]
    _seed_job()
    db.save_score("n1", "hash-a", version, 80.0,
                  {"matched_skills": [], "missing_skills": []})

    async def mutating_structured(prompt, schema, task, system_prompt=""):
        # New profile confirmed + job re-scored while the LLM runs.
        db.save_score("n1", "hash-a", version + 1, 91.0,
                      {"matched_skills": [], "missing_skills": []})
        return {"strengths": ["stale"], "gaps": [], "pitch": "stale"}

    monkeypatch.setattr(llm, "structured", mutating_structured)
    with pytest.raises(ValueError):
        await llm_layer.explain_job("n1")
    assert db.get_score("n1")["explanation"] is None  # newer row untouched


async def test_rerank_mid_call_mutation_aborts_cluster(test_db, monkeypatch):
    """One cluster row moving during the rerank call aborts the whole
    cluster atomically — zero writes, including to unmoved rows."""
    from backend.core import llm
    from backend.scoring import llm_layer

    _confirm_profile()
    run_id = _seed_run(["r1", "r2"])
    for job_id in ("r1", "r2"):
        db.save_job_detail(job_id, "d", f"h-{job_id}")
        db.save_score(job_id, f"h-{job_id}", 1, 85.0,
                      {"matched_skills": [], "missing_skills": []})

    async def mutating_structured(prompt, schema, task, system_prompt=""):
        # r1's posting is refreshed and re-scored under a new hash.
        db.save_job_detail("r1", "changed", "h-changed")
        db.save_score("r1", "h-changed", 1, 70.0,
                      {"matched_skills": [], "missing_skills": []})
        return {"ordered_job_ids": ["r1", "r2"]}

    monkeypatch.setattr(llm, "structured", mutating_structured)
    with pytest.raises(ValueError):
        await llm_layer.rerank_close_matches(run_id)
    assert db.get_score("r1")["llm_rank"] is None
    assert db.get_score("r2")["llm_rank"] is None  # unmoved row also untouched


async def test_spawn_pipeline_recycles_on_profile_change(test_db, monkeypatch):
    """The worker is keyed by (run, profile version): a new confirmed
    version replaces the old worker instead of being refused."""
    from backend.api import routes
    from backend.scoring import pipeline

    _confirm_profile()
    run_id = _seed_run(["p1"])
    started = []
    release = asyncio.Event()

    async def slow_run(rid):
        started.append(db.latest_profile()[0])
        await release.wait()
        return {"scored": 0, "skipped": 0, "failed": 0, "stopped": None}

    monkeypatch.setattr(pipeline, "run_pipeline", slow_run)
    monkeypatch.setattr(routes, "_pipeline_task", None)
    monkeypatch.setattr(routes, "_pipeline_key", None)

    assert routes._spawn_pipeline(run_id) is True
    assert routes._spawn_pipeline(run_id) is False  # same key -> no-op
    first_task = routes._pipeline_task
    _confirm_profile()  # version bump
    assert routes._spawn_pipeline(run_id) is True  # new key -> replaced
    release.set()
    await asyncio.gather(first_task, routes._pipeline_task,
                         return_exceptions=True)
    assert first_task.cancelled()


async def test_rerank_rejects_bad_id_set(test_db, monkeypatch):
    from backend.core import llm
    from backend.scoring import llm_layer

    _confirm_profile()
    run_id = _seed_run(["r1", "r2"])
    for job_id in ("r1", "r2"):
        db.save_job_detail(job_id, "d", f"h-{job_id}")
        db.save_score(job_id, f"h-{job_id}", 1, 85.0,
                      {"matched_skills": [], "missing_skills": []})

    async def fake_structured(prompt, schema, task, system_prompt=""):
        return {"ordered_job_ids": ["r1", "bogus"]}

    monkeypatch.setattr(llm, "structured", fake_structured)
    with pytest.raises(ValueError):
        await llm_layer.rerank_close_matches(run_id)
    assert db.get_score("r1")["llm_rerank"] is None  # nothing written
