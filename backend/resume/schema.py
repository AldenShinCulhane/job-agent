"""Profile JSON schema — the structured form of the user's resume.

One schema, two jobs: it is the LLM extraction contract (structured
output) and the shape stored per profile version. Extraction must be
verbatim — the prompt and the descriptions below both say so, because
everything here becomes ground truth in the ledger (facts.py).
"""

_CONTACT = {
    "type": "object",
    "properties": {
        "phone": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "linkedin": {"type": ["string", "null"], "description": "URL or handle"},
        "github": {"type": ["string", "null"], "description": "URL or handle"},
        "website": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"], "description": "City, region"},
    },
    "required": ["phone", "email", "linkedin", "github", "website", "location"],
    "additionalProperties": False,
}

_EXPERIENCE = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "company": {"type": "string"},
        "location": {"type": ["string", "null"]},
        "start": {"type": "string", "description": "As written, e.g. 'Mar 2026'"},
        "end": {"type": "string", "description": "As written, e.g. 'Present'"},
        "bullets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "company", "location", "start", "end", "bullets"],
    "additionalProperties": False,
}

_PROJECT = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "technologies": {"type": "array", "items": {"type": "string"}},
        "date": {"type": ["string", "null"]},
        "link": {"type": ["string", "null"]},
        "bullets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "technologies", "date", "link", "bullets"],
    "additionalProperties": False,
}

_EDUCATION = {
    "type": "object",
    "properties": {
        "institution": {"type": "string"},
        "location": {"type": ["string", "null"]},
        "degree": {"type": "string", "description": "Degree line as written, incl. honors"},
        "start": {"type": ["string", "null"]},
        "end": {"type": ["string", "null"]},
        "details": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["institution", "location", "degree", "start", "end", "details"],
    "additionalProperties": False,
}

_SKILL_GROUP = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "description": "e.g. 'Languages'"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["category", "items"],
    "additionalProperties": False,
}

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "headline": {
            "type": ["string", "null"],
            "description": "One-line professional headline if present",
        },
        "contact": _CONTACT,
        "experiences": {"type": "array", "items": _EXPERIENCE},
        "projects": {"type": "array", "items": _PROJECT},
        "education": {"type": "array", "items": _EDUCATION},
        "skills": {"type": "array", "items": _SKILL_GROUP},
    },
    "required": ["name", "headline", "contact", "experiences", "projects", "education", "skills"],
    "additionalProperties": False,
}

EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured resume content. Copy text verbatim — never "
    "paraphrase, summarize, invent, or merge. Keep every bullet exactly as "
    "written (drop only bullet glyphs and layout artifacts). If a field is "
    "absent in the resume, use null or an empty list. Dates stay in the "
    "format the resume uses."
)


def extraction_prompt(resume_text: str) -> str:
    return (
        "Extract this resume's content into the requested JSON structure, "
        "verbatim:\n\n---\n" + resume_text + "\n---"
    )
