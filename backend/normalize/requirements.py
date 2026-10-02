"""Structured requirement extraction from raw job descriptions.

One LLM call (medium effort) per unique job description — cached by JD
hash, so a description never pays for extraction twice. The scorer runs
purely on this normalized form; the raw description text is never
matched against directly (PLAN.md §4.4).
"""

import logging

from backend.core import db, llm

log = logging.getLogger(__name__)

# Part of the cache identity: bump whenever the prompt or schema changes
# meaning, so old extractions aren't served for the new contract.
SCHEMA_VERSION = 1

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


def cache_key(jd_hash: str) -> str:
    """Cache identity = extraction contract version + description hash:
    a prompt/schema change must not serve structurally old extractions."""
    return f"v{SCHEMA_VERSION}:{jd_hash}"


def cached_for(jd_hashes) -> dict[str, dict]:
    """The cached requirements of each description hash that has them,
    by hash — looked up under the same key normalize_job saved them."""
    keys = {jd_hash: cache_key(jd_hash) for jd_hash in jd_hashes if jd_hash}
    found = db.requirements_for(keys.values())
    return {jd_hash: found[key] for jd_hash, key in keys.items() if key in found}


async def normalize_job(job_id: str) -> dict:
    """Return the normalized requirements for a job's description. The
    cache is content-addressed by (schema version, JD hash) — two
    postings with the same description share one extraction, and a
    description never pays for extraction twice."""
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    if not job.get("description") or not job.get("description_hash"):
        raise ValueError(f"job {job_id} has no cached description to normalize")
    key = cache_key(job["description_hash"])
    cached = db.get_requirements(key)
    if cached is not None:
        return cached
    requirements = await llm.structured(
        _prompt(job),
        REQUIREMENTS_SCHEMA,
        task="normalize",
        system_prompt=_SYSTEM_PROMPT,
    )
    db.save_requirements(key, requirements)
    log.info("job %s normalized (hash %s)", job_id, job["description_hash"])
    return requirements
