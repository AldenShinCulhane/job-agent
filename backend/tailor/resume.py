"""Per-job tailoring generation (PLAN.md §4.5 step 1).

One high-effort structured pass produces BOTH documents as JSON — never
raw LaTeX: tailored resume bullets ({text, source_ids, relevance}),
headline, skills ordering, section order, and cover letter paragraphs
({text, source_ids}) under the same truth discipline. Application code
escapes everything and renders LaTeX from templates; source IDs are
internal metadata only and are never rendered into the PDFs.
"""

import json

from backend.core import llm

# Recorded in package provenance; bump when the prompt or schema changes
# in a way that makes previously generated packages non-comparable.
PROMPT_VERSION = 1
SCHEMA_VERSION = 1

# Keep prompts bounded: descriptions beyond this are truncated with a
# marker (the requirements extraction already captured the essentials).
_MAX_JD_CHARS = 6000

_BULLET = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "source_ids", "relevance"],
    "properties": {
        "text": {"type": "string", "minLength": 1},
        # Every resume claim must cite the ledger facts that support it.
        "source_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        # Relevance to THIS job, 0-100 — page_limit trims lowest first.
        "relevance": {"type": "number", "minimum": 0, "maximum": 100},
    },
}

_PARAGRAPH = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "source_ids"],
    "properties": {
        "text": {"type": "string", "minLength": 1},
        # May be empty ONLY for non-factual sentences (motivation, interest);
        # any claim about the candidate's history must cite its facts.
        "source_ids": {"type": "array", "items": {"type": "string"}},
    },
}

_ENTRY_BULLETS = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "bullets"],
        "properties": {
            # Index into the profile's experiences/projects array — the
            # entry's employer/title/dates are rendered from the profile
            # verbatim; only bullets are tailored.
            "index": {"type": "integer", "minimum": 0},
            "bullets": {"type": "array", "items": _BULLET},
        },
    },
}

SECTIONS = ("experience", "projects", "education", "skills")

TAILOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "section_order", "skills_order",
                 "experiences", "projects", "cover_letter"],
    "properties": {
        # Tailored headline is a claim like any other: cited or null.
        "headline": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "source_ids"],
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "source_ids": {"type": "array",
                                       "items": {"type": "string"},
                                       "minItems": 1},
                    },
                },
            ]
        },
        # Layout preference: resume section order (permutation of SECTIONS).
        "section_order": {
            "type": "array",
            "items": {"enum": list(SECTIONS)},
            "minItems": len(SECTIONS),
            "maxItems": len(SECTIONS),
        },
        # Skill CATEGORY names in tailored order — no new skills, ever.
        "skills_order": {"type": "array", "items": {"type": "string"}},
        "experiences": _ENTRY_BULLETS,
        "projects": _ENTRY_BULLETS,
        "cover_letter": {
            "type": "object",
            "additionalProperties": False,
            "required": ["paragraphs", "closing"],
            "properties": {
                "paragraphs": {"type": "array", "items": _PARAGRAPH,
                               "minItems": 2, "maxItems": 5},
                "closing": {"type": "string", "minLength": 1},
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "You tailor resumes and cover letters. You NEVER invent employers, "
    "titles, dates, degrees, skills, metrics, or numbers — every factual "
    "claim must be supported by the cited source facts, and every "
    "source_id you cite must come from the provided fact ledger. You "
    "return only the requested JSON."
)


def build_prompt(profile: dict, ledger: dict, job: dict,
                 requirements: dict | None, breakdown: dict | None,
                 page_limit: int) -> str:
    description = (job.get("description") or "").strip()
    if len(description) > _MAX_JD_CHARS:
        description = description[:_MAX_JD_CHARS] + "\n[...truncated]"
    facts = "\n".join(
        f"- {fact_id}: {entry['text']}" for fact_id, entry in ledger.items()
    )
    sections = [
        f"# Job\n{job.get('title', '')} at {job.get('company', '')}\n\n{description}",
        f"# Normalized requirements\n{json.dumps(requirements or {}, indent=1)}",
        f"# Deterministic score breakdown\n{json.dumps(breakdown or {}, indent=1)}",
        f"# Candidate profile\n{json.dumps(profile, indent=1)}",
        f"# Fact ledger (the ONLY valid source_ids)\n{facts}",
        f"""# Task
Produce tailored content for this specific job as JSON matching the schema.

Resume rules:
- Rewrite/select bullets to be maximally attractive to hiring managers,
  ATS parsers, and AI screening filters for THIS job description.
- Every bullet cites the source_ids of the ledger facts that support it.
  Rewording and tightening is encouraged; adding facts is forbidden.
- Set relevance (0-100) honestly per bullet for this job — the least
  relevant bullets are dropped first if the resume exceeds {page_limit}
  page(s).
- Use whitespace well: prefer bullets that fill their lines; avoid lines
  with only a word or two.
- Order skill categories (skills_order) and sections (section_order) so
  the strongest material for this job leads.
- Keep the candidate's voice factual and specific; no buzzword padding.

Cover letter rules:
- 2-4 short paragraphs plus a closing (e.g. "Sincerely,"). It must fit
  one page. Do not include contact details, date, or greeting — the
  application renders those.
- Factual claims cite source_ids exactly like resume bullets; sentences
  of pure motivation/interest may have empty source_ids.
- Specific to this company and role; never generic boilerplate.""",
    ]
    return "\n\n".join(sections)


async def generate_tailored(profile: dict, ledger: dict, job: dict,
                            requirements: dict | None, breakdown: dict | None,
                            page_limit: int) -> dict:
    """The single generation pass (high effort). Returns schema-valid
    tailored JSON; raises llm.EngineError on engine trouble."""
    prompt = build_prompt(profile, ledger, job, requirements, breakdown,
                          page_limit)
    return await llm.structured(prompt, TAILOR_SCHEMA, task="tailor",
                                system_prompt=_SYSTEM_PROMPT)


def tailored_profile(profile: dict, tailored: dict) -> dict:
    """Merge the tailored selections into a render-ready profile copy.

    Structure comes from the profile (employers, titles, dates, degrees,
    contact — the LLM cannot touch them); only bullets, headline, skills
    order, and section order come from the tailored JSON."""
    merged = json.loads(json.dumps(profile))  # deep copy
    headline = tailored.get("headline")
    if headline:
        merged["headline"] = headline["text"]

    for key in ("experiences", "projects"):
        selections = {
            entry["index"]: [b["text"] for b in entry["bullets"]]
            for entry in tailored.get(key, [])
        }
        for i, item in enumerate(merged.get(key, [])):
            if i in selections:
                item["bullets"] = selections[i]

    order = tailored.get("skills_order") or []
    groups = merged.get("skills", [])
    by_name = {g["category"]: g for g in groups}
    reordered = [by_name[name] for name in order if name in by_name]
    reordered += [g for g in groups if g["category"] not in set(order)]
    merged["skills"] = reordered
    return merged
