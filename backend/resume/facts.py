"""Truth ledger: a stable source ID for every fact in the profile.

IDs are content-hashed, not positional, so reordering entries never
changes an ID, and editing a fact's text produces a *new* ID — exactly
the semantics tailoring needs: a claim citing an old ID after an edit
fails validation instead of silently pointing at different text.

IDs are internal metadata only — never rendered into any document.
"""

import hashlib


def _fact_id(kind: str, text: str, seen: set[str]) -> str:
    digest = hashlib.sha1(text.strip().lower().encode()).hexdigest()[:10]
    fact_id = f"{kind}-{digest}"
    # Identical text in two places (rare but legal) gets a counted suffix
    # so every ledger entry stays individually addressable.
    n = 1
    unique = fact_id
    while unique in seen:
        n += 1
        unique = f"{fact_id}-{n}"
    seen.add(unique)
    return unique


def build_ledger(profile: dict) -> dict[str, dict]:
    """profile JSON -> {fact_id: {kind, text, path}}.

    `path` is a JSON-pointer-ish locator back into the profile, used by
    the UI and validator to show where a fact lives.
    """
    ledger: dict[str, dict] = {}
    seen: set[str] = set()

    def add(kind: str, text: str | None, path: str) -> None:
        if not text or not text.strip():
            return
        fact_id = _fact_id(kind, text, seen)
        ledger[fact_id] = {"kind": kind, "text": text.strip(), "path": path}

    add("name", profile.get("name"), "/name")
    add("headline", profile.get("headline"), "/headline")
    for key, value in (profile.get("contact") or {}).items():
        add("contact", value, f"/contact/{key}")

    for i, exp in enumerate(profile.get("experiences", [])):
        entity = " | ".join(
            str(exp.get(k) or "") for k in ("title", "company", "location", "start", "end")
        )
        add("experience", entity, f"/experiences/{i}")
        for j, bullet in enumerate(exp.get("bullets", [])):
            add("bullet", bullet, f"/experiences/{i}/bullets/{j}")

    for i, proj in enumerate(profile.get("projects", [])):
        entity = " | ".join(
            [str(proj.get("name") or ""), ", ".join(proj.get("technologies", [])), str(proj.get("date") or "")]
        )
        add("project", entity, f"/projects/{i}")
        for j, bullet in enumerate(proj.get("bullets", [])):
            add("bullet", bullet, f"/projects/{i}/bullets/{j}")

    for i, edu in enumerate(profile.get("education", [])):
        entity = " | ".join(
            str(edu.get(k) or "") for k in ("institution", "degree", "location", "start", "end")
        )
        add("education", entity, f"/education/{i}")
        for j, detail in enumerate(edu.get("details", [])):
            add("edu-detail", detail, f"/education/{i}/details/{j}")

    for i, group in enumerate(profile.get("skills", [])):
        for j, item in enumerate(group.get("items", [])):
            add("skill", f"{group.get('category', '')}: {item}", f"/skills/{i}/items/{j}")

    return ledger
