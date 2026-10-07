"""Which postings are the same role (PLAN.md P8a).

LinkedIn gives a reposted role a new posting id, and a search that runs
again finds it as a new job. Grouping says which postings repeat each
other. It is derived data, computed here from the postings alone: no
model call, no fetch. Nothing that references a posting — packages,
run membership, the tracker — is ever rewritten, so a grouping that
changes loses nothing.

The rule is deliberately narrow. Two postings are the same role when
they have the same company, title and location, and their descriptions
are the same text or nearly so. A shared name alone is not enough: the
same title at one company is often several teams, with different text.
"""

import re
import unicodedata

# Part of every stored grouping: a change to the rule below rebuilds all
# groups on the next start.
RULE_VERSION = 1

# Reposts in the data this was set from scored 0.947 and up; different
# roles under the same name, 0.33. A starting point inside that gap, not
# a measured accuracy.
SAME_TEXT = 0.9
_SHINGLE = 5
_WORD = re.compile(r"\w+")


def _normalized(text: str | None) -> str:
    """Case and spacing only. Punctuation carries meaning in a title —
    "C", "C++" and "C#" are three roles — so none of it is removed."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def match_key(posting: dict) -> str | None:
    """The bucket a posting can only match inside, or None when it can't
    be grouped: no description to compare, or no location to tell two
    cities' openings apart."""
    location = _normalized(posting.get("location"))
    if not posting.get("description") or not location:
        return None
    return "\x1f".join((_normalized(posting.get("company")),
                        _normalized(posting.get("title")), location))


def shingles(description: str) -> frozenset[tuple[str, ...]]:
    words = _WORD.findall(description.casefold())
    if len(words) < _SHINGLE:
        return frozenset({tuple(words)})
    return frozenset(tuple(words[i:i + _SHINGLE])
                     for i in range(len(words) - _SHINGLE + 1))


def similarity(a: frozenset, b: frozenset) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 1.0


def group(postings: list[dict]) -> dict[str, str]:
    """{job_id: group_id} for postings that share one match key.

    Every pair inside a group passes the rule: a chain where A matches B
    and B matches C, but A does not match C, does not join A to C.
    Postings are taken oldest first; each joins the first group whose
    every member it matches, or starts its own. The group id is its
    first member — derived, and not stable over time: an older posting
    that gains its description later can become the first member.
    """
    ordered = sorted(postings, key=lambda p: (p["first_seen_at"], p["job_id"]))
    texts = {p["job_id"]: shingles(p["description"]) for p in ordered}

    def same_role(a: dict, b: dict) -> bool:
        if a.get("description_hash") and a["description_hash"] == b.get("description_hash"):
            return True
        return similarity(texts[a["job_id"]], texts[b["job_id"]]) >= SAME_TEXT

    groups: list[list[dict]] = []
    for posting in ordered:
        for members in groups:
            if all(same_role(posting, member) for member in members):
                members.append(posting)
                break
        else:
            groups.append([posting])
    return {m["job_id"]: members[0]["job_id"] for members in groups for m in members}
