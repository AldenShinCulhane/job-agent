"""Structured requirement extraction from raw job descriptions.

One LLM call (medium effort) per unique job description — cached by JD
hash, so a description never pays for extraction twice. The scorer runs
purely on this normalized form; the raw description text is never
matched against directly (PLAN.md §4.4).
"""

import logging

from backend.core import db, llm

log = logging.getLogger(__name__)

# The scorer compares levels numerically; extraction normalizes free-text
# education requirements onto this fixed scale (or null when unstated).
EDUCATION_LEVELS = ("associate", "bachelor", "master", "doctorate")

SENIORITY_LEVELS = (
    "internship", "entry", "associate", "mid_senior", "director", "executive",
)

REQUIREMENTS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "required_skills",
        "preferred_skills",
        "min_years_experience",
        "education_requirement",
        "seniority",
        "workplace",
        "compensation",
    ],
    "properties": {
        "required_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Skills/technologies the posting requires.",
        },
        "preferred_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Nice-to-have skills/technologies.",
        },
        "min_years_experience": {
            "type": ["integer", "null"],
            "minimum": 0,
            "description": "Minimum years of experience demanded, or null.",
        },
        "education_requirement": {
            "type": ["string", "null"],
            "enum": [*EDUCATION_LEVELS, None],
            "description": "Lowest degree level that satisfies the posting.",
        },
        "seniority": {
            "type": ["string", "null"],
            "enum": [*SENIORITY_LEVELS, None],
        },
        "workplace": {
            "type": ["string", "null"],
            "enum": ["remote", "hybrid", "onsite", None],
        },
        "compensation": {
            "type": ["string", "null"],
            "description": "Stated compensation, verbatim, or null.",
        },
    },
}

_SYSTEM_PROMPT = (
    "You extract structured hiring requirements from job postings. "
    "Report only what the posting states — never infer or embellish. "
    "Answer only in the requested JSON structure."
)


def _prompt(job: dict) -> str:
    return (
        "Extract the hiring requirements from this job posting.\n"
        "- required_skills: concrete skills/technologies the posting demands "
        "(deduplicated, short canonical names like 'Python', 'React', 'AWS').\n"
        "- preferred_skills: explicitly nice-to-have items, same form.\n"
        "- min_years_experience: the minimum years demanded, or null if none "
        "stated.\n"
        "- education_requirement: the LOWEST degree level that satisfies the "
        "posting (associate/bachelor/master/doctorate), or null if none "
        "stated.\n"
        "- seniority, workplace, compensation: only if stated, else null.\n\n"
        f"Title: {job['title']}\n"
        f"Company: {job['company']}\n\n"
        f"Description:\n{job['description']}"
    )


async def normalize_job(job_id: str) -> dict:
    """Return the normalized requirements for a job, extracting (and
    caching by JD hash) only when the cache is missing or stale."""
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    if not job.get("description") or not job.get("description_hash"):
        raise ValueError(f"job {job_id} has no cached description to normalize")
    cached = db.get_requirements(job_id)
    if cached is not None and cached["jd_hash"] == job["description_hash"]:
        return cached["requirements"]
    requirements = await llm.structured(
        _prompt(job),
        REQUIREMENTS_SCHEMA,
        task="normalize",
        system_prompt=_SYSTEM_PROMPT,
    )
    db.save_requirements(job_id, job["description_hash"], requirements)
    log.info("job %s normalized (hash %s)", job_id, job["description_hash"])
    return requirements
