"""LLM layer over the deterministic baseline (PLAN.md §4.4).

Explains scores on demand and may rerank close matches — jobs whose
deterministic scores sit within a configurable band of the top score.
It never invents the scale: rerank values are confined to the cluster's
own score range, and the deterministic baseline is never overwritten
(separate column, enforced at the db layer).
"""

import logging

from backend.core import db, llm
from backend.scoring.pipeline import confirmed_profile
from backend.scoring.score import flatten_profile_skills, profile_years_experience

log = logging.getLogger(__name__)

# "Close matches" = within this many points of the cluster's top score.
RERANK_BAND = 5.0

EXPLAIN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strengths", "gaps", "pitch"],
    "properties": {
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "pitch": {"type": "string",
                  "description": "One-line pitch for this candidate-job fit."},
    },
}

RERANK_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ordered_job_ids"],
    "properties": {
        "ordered_job_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every provided job id, best fit first.",
        },
    },
}

_SYSTEM_PROMPT = (
    "You assess candidate-job fit inside a job-application assistant. "
    "Ground every statement ONLY in the data provided — never invent "
    "skills, requirements, or facts. Answer only in the requested JSON "
    "structure."
)


def _profile_summary(profile: dict) -> str:
    skills = ", ".join(sorted(flatten_profile_skills(profile)))
    years = profile_years_experience(profile)
    return (
        f"Candidate: {profile.get('headline') or 'software professional'}; "
        f"~{years if years is not None else 'unknown'} years of experience; "
        f"skills: {skills}"
    )


def _job_summary(job: dict, score: dict, requirements: dict | None) -> str:
    breakdown = score["breakdown"]
    req_line = ""
    if requirements:
        req_line = (
            f"  requires: {', '.join(requirements.get('required_skills', [])) or '—'}"
            f" | prefers: {', '.join(requirements.get('preferred_skills', [])) or '—'}"
            f" | min years: {requirements.get('min_years_experience')}\n"
        )
    return (
        f"- id {job['job_id']}: {job['title']} at {job['company']} "
        f"(deterministic score {score['deterministic_score']})\n"
        f"{req_line}"
        f"  matched: {', '.join(breakdown.get('matched_skills', [])) or '—'} "
        f"| missing: {', '.join(breakdown.get('missing_skills', [])) or '—'}"
    )


async def explain_job(job_id: str) -> dict:
    """On-demand strengths/gaps/pitch for one scored job; cached on the
    score row until the cascade invalidates it. Only a score computed
    from the CURRENT profile version and CURRENT description hash is
    explained — a stale row (mid-rescore, edited posting) reads as
    unscored rather than producing an old-profile explanation."""
    version, profile = confirmed_profile()
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    score = db.get_score(job_id)
    if (
        score is None
        or score["profile_version"] != version
        or score["jd_hash"] != job["description_hash"]
    ):
        raise ValueError(f"job {job_id} is not scored for the current profile yet")
    if score["explanation"] is not None:
        return score["explanation"]
    requirements = db.get_requirements(job["description_hash"])
    prompt = (
        "Explain this candidate-job fit: concrete strengths, concrete "
        "gaps, and a one-line pitch the candidate could lead with.\n\n"
        f"{_profile_summary(profile)}\n\n"
        f"{_job_summary(job, score, requirements)}"
    )
    explanation = await llm.structured(
        prompt, EXPLAIN_SCHEMA, task="score_explain", system_prompt=_SYSTEM_PROMPT
    )
    db.set_explanation(job_id, explanation)
    return explanation


async def rerank_close_matches(run_id: int, band: float = RERANK_BAND) -> dict:
    """One LLM pass ordering the jobs within `band` of the run's top
    deterministic score. Rerank values are spaced inside the cluster's
    own [min, max] — the deterministic scale stays the backbone — and a
    1-based rank ordinal is stored alongside, so the LLM's order
    survives even when every cluster score is identical."""
    version, profile = confirmed_profile()
    # Current-version/hash scores only — a mid-rescore run has nothing
    # rerankable, not a mix of old and new numbers.
    scored = [
        j
        for j in db.list_jobs(run_id, profile_version=version)
        if j["deterministic_score"] is not None
    ]
    if len(scored) < 2:
        return {"reranked": 0}
    top = max(j["deterministic_score"] for j in scored)
    cluster = [j for j in scored if top - j["deterministic_score"] <= band]
    if len(cluster) < 2:
        return {"reranked": 0}
    summaries = []
    for job in cluster:
        score = db.get_score(job["job_id"])
        requirements = db.get_requirements(job["description_hash"])
        summaries.append(_job_summary(job, score, requirements))
    prompt = (
        "These jobs scored within a few points of each other — too close "
        "for the deterministic scorer to separate meaningfully. Order "
        "them best fit first, using judgment about the candidate's "
        "actual leverage in each role (not just skill counts). Return "
        "every id exactly once.\n\n"
        f"{_profile_summary(profile)}\n\n" + "\n".join(summaries)
    )
    result = await llm.structured(
        prompt, RERANK_SCHEMA, task="score_explain", system_prompt=_SYSTEM_PROMPT
    )
    ordered = result["ordered_job_ids"]
    expected = {j["job_id"] for j in cluster}
    if set(ordered) != expected or len(ordered) != len(expected):
        raise ValueError("rerank did not return each cluster job exactly once")
    hi = max(j["deterministic_score"] for j in cluster)
    lo = min(j["deterministic_score"] for j in cluster)
    step = (hi - lo) / (len(ordered) - 1)
    for position, job_id in enumerate(ordered):
        db.set_llm_rerank(job_id, round(hi - position * step, 2), position + 1)
    log.info("reranked %d close matches in run %s", len(ordered), run_id)
    return {"reranked": len(ordered), "ordered_job_ids": ordered}
