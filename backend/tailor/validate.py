"""Truth validation for both tailored documents (PLAN.md §4.5 step 2).

Deterministic exact checks first (cited IDs exist, structural indexes
valid, every number in a claim is present in its cited facts), then ONE
batched structured entailment pass judging fact→claim support for every
cited pair across both documents — not separate redundant QA prompts.
Verdicts are cached by (claim text, cited facts) content hash so a
revalidation after a pure trim (no text changed) costs zero LLM calls.

Rejection → one repair round (xhigh) → revalidate → still failing →
package failed. Source IDs are internal metadata only.
"""

import hashlib
import json
import re

from backend.core import llm
from backend.tailor import resume as tailor_resume

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numeric_tokens(text: str) -> set[str]:
    return {token.replace(",", "") for token in _NUMBER.findall(text)}


def collect_claims(tailored: dict) -> list[dict]:
    """Every LLM-authored statement in both documents, with a stable
    claim_id used by failures, verdicts, and repair prompts."""
    claims = []
    headline = tailored.get("headline")
    if headline:
        claims.append({"claim_id": "headline", "text": headline["text"],
                       "source_ids": headline["source_ids"]})
    for key, prefix in (("experiences", "exp"), ("projects", "proj")):
        for entry in tailored.get(key, []):
            for i, bullet in enumerate(entry["bullets"]):
                claims.append({
                    "claim_id": f"{prefix}{entry['index']}:b{i}",
                    "text": bullet["text"],
                    "source_ids": bullet["source_ids"],
                })
    for i, paragraph in enumerate(tailored["cover_letter"]["paragraphs"]):
        claims.append({"claim_id": f"cover:p{i}", "text": paragraph["text"],
                       "source_ids": paragraph["source_ids"]})
    return claims


def deterministic_failures(tailored: dict, profile: dict,
                           ledger: dict) -> list[dict]:
    """Exact checks that need no model: unknown source IDs, structural
    indexes, non-profile skill categories, malformed section order, and
    numbers that do not appear in the cited facts."""
    failures = []

    order = tailored.get("section_order") or []
    if sorted(order) != sorted(tailor_resume.SECTIONS):
        failures.append({"claim_id": "section_order",
                         "reason": f"not a permutation of {tailor_resume.SECTIONS}"})
    categories = {g["category"] for g in profile.get("skills", [])}
    skills_order = tailored.get("skills_order", [])
    unknown = [name for name in skills_order if name not in categories]
    if unknown:
        failures.append({"claim_id": "skills_order",
                         "reason": f"unknown skill categories: {unknown}"})
    if len(skills_order) != len(set(skills_order)):
        failures.append({"claim_id": "skills_order",
                         "reason": "duplicate skill categories"})
    for key in ("experiences", "projects"):
        available = len(profile.get(key, []))
        seen: set[int] = set()
        for entry in tailored.get(key, []):
            index = entry["index"]
            if index >= available or index in seen:
                failures.append({"claim_id": f"{key}:{index}",
                                 "reason": "index out of range or duplicated"})
            seen.add(index)

    for claim in collect_claims(tailored):
        missing = [sid for sid in claim["source_ids"] if sid not in ledger]
        if missing:
            failures.append({"claim_id": claim["claim_id"],
                             "reason": f"unknown source_ids: {missing}"})
            continue
        cited_text = " ".join(ledger[sid]["text"] for sid in claim["source_ids"])
        ungrounded = _numeric_tokens(claim["text"]) - _numeric_tokens(cited_text)
        if ungrounded:
            failures.append({
                "claim_id": claim["claim_id"],
                "reason": f"numbers not present in cited facts: {sorted(ungrounded)}",
            })
    return failures


def entailment_key(claim: dict, ledger: dict) -> str:
    """Content hash of (claim text, cited fact texts) — the verdict for a
    pair is stable, so unchanged claims revalidate for free after trims."""
    payload = json.dumps(
        [claim["text"],
         sorted(ledger[sid]["text"] for sid in claim["source_ids"])],
    )
    return hashlib.sha1(payload.encode()).hexdigest()


ENTAILMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "supported", "reason"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

_ENTAIL_SYSTEM = (
    "You are a strict fact checker. For each claim, judge whether every "
    "factual assertion about the candidate is supported by its cited "
    "facts. Reworded or tightened phrasing is fine; new employers, "
    "titles, dates, degrees, technologies, metrics, or scope claims are "
    "not. A claim with no cited facts is supported only if it makes no "
    "factual assertion about the candidate's history (pure motivation "
    "or interest). Return only JSON."
)


async def entailment_failures(claims: list[dict], ledger: dict,
                              cache: dict[str, dict],
                              record=None) -> list[dict]:
    """One batched structured pass over every claim not already covered
    by a cached verdict. Mutates `cache` (persisted by the caller) and
    returns the unsupported claims. A claim the model fails to return a
    verdict for counts as unsupported — never silently trusted.
    `record`, when given, is called as record("entailment") the moment
    the LLM call returns — the caller checkpoints its ledger before any
    later step can fail."""
    pending, failures = [], []
    for claim in claims:
        key = entailment_key(claim, ledger)
        verdict = cache.get(key)
        if verdict is None:
            pending.append((key, claim))
        elif not verdict["supported"]:
            failures.append({"claim_id": claim["claim_id"],
                             "reason": verdict["reason"]})
    if not pending:
        return failures

    lines = []
    for _, claim in pending:
        facts = [ledger[sid]["text"] for sid in claim["source_ids"]]
        lines.append(json.dumps({"claim_id": claim["claim_id"],
                                 "claim": claim["text"],
                                 "cited_facts": facts}))
    prompt = (
        "Judge each claim against its cited facts. Return a verdict for "
        "EVERY claim_id listed.\n\n" + "\n".join(lines)
    )
    result = await llm.structured(prompt, ENTAILMENT_SCHEMA, task="entail",
                                  system_prompt=_ENTAIL_SYSTEM)
    returned = {v["claim_id"]: v for v in result["verdicts"]}
    # EVERY verdict enters the cache before the checkpoint fires — a
    # claim the model skipped gets a cached NEGATIVE verdict (fail
    # closed, stable across restarts). Only then does the recorder
    # persist, so the ledger entry and the verdicts it paid for land in
    # one transition: "entailment ran" can never be on record without
    # its results, and a later kill never repeats this call.
    for key, claim in pending:
        verdict = returned.get(claim["claim_id"])
        if verdict is None:
            cache[key] = {"supported": False,
                          "reason": "no entailment verdict returned"}
        else:
            cache[key] = {"supported": verdict["supported"],
                          "reason": verdict["reason"]}
    if record is not None:
        record("entailment")
    for key, claim in pending:
        verdict = cache[key]
        if not verdict["supported"]:
            failures.append({"claim_id": claim["claim_id"],
                             "reason": verdict["reason"]})
    return failures


def cached_failures(tailored: dict, profile: dict, ledger: dict,
                    cache: dict[str, dict]) -> list[dict]:
    """Full validation from persisted verdicts ONLY — zero LLM calls.
    The pre-publish assertion runs this against the exact shipped
    documents (trimmed resume + condensed cover): every claim was
    entailment-checked on its way here, so its verdict must be in the
    cache — a missing verdict means the content drifted past validation
    and fails closed."""
    failures = deterministic_failures(tailored, profile, ledger)
    if failures:
        return failures
    for claim in collect_claims(tailored):
        verdict = cache.get(entailment_key(claim, ledger))
        if verdict is None:
            failures.append({"claim_id": claim["claim_id"],
                             "reason": "no cached verdict for shipped claim"})
        elif not verdict["supported"]:
            failures.append({"claim_id": claim["claim_id"],
                             "reason": verdict["reason"]})
    return failures


async def validate(tailored: dict, profile: dict, ledger: dict,
                   cache: dict[str, dict], record=None) -> list[dict]:
    """All failures for both documents. Deterministic problems
    short-circuit (no LLM spend on structurally broken output); a clean
    structure gets the batched entailment pass."""
    failures = deterministic_failures(tailored, profile, ledger)
    if failures:
        return failures
    return await entailment_failures(collect_claims(tailored), ledger, cache,
                                     record=record)


_REPAIR_SYSTEM = (
    "You repair tailored resume/cover-letter JSON that failed truth "
    "validation. Fix ONLY the failing claims — rewrite them to be fully "
    "supported by cited ledger facts, or drop them. Never invent facts. "
    "Return the complete corrected JSON in the same schema."
)


async def repair(tailored: dict, failures: list[dict], profile: dict,
                 ledger: dict) -> dict:
    """One xhigh repair round: returns a full corrected tailored JSON
    (same schema), which the caller must revalidate."""
    facts = "\n".join(f"- {fid}: {entry['text']}" for fid, entry in ledger.items())
    prompt = (
        f"# Failed validation\n{json.dumps(failures, indent=1)}\n\n"
        f"# Current tailored JSON\n{json.dumps(tailored, indent=1)}\n\n"
        f"# Fact ledger (the ONLY valid source_ids)\n{facts}\n\n"
        f"# Candidate profile\n{json.dumps(profile, indent=1)}\n\n"
        "Return the corrected tailored JSON."
    )
    return await llm.structured(prompt, tailor_resume.TAILOR_SCHEMA,
                                task="repair", system_prompt=_REPAIR_SYSTEM)
