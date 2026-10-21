"""Keyword coverage of a finished package (PLAN.md P8c): which of the
posting's skills its shipped documents mention, and where. Computed when
the packages are read, with no model call and nothing stored.

Every input is the package's own, never what has moved since: the
requirements its generation used (not the cache, which a re-score can
replace), the profile version it is pinned to, and the documents it
shipped — the fitted resume, without the bullets trimmed to fit it, and
the condensed cover letter. A package missing any of them has no
coverage (None), never an empty one.

A mention is a keyword, not proof of experience. The resume's lists (its
skill groups and project technologies) match by the scorer's rules, so a
listed skill is one the requirement checklist marks as met. Prose (the
headline, every bullet, the letter's paragraphs) matches on word
boundaries, by the skill's name or an alias of it: Java is not
JavaScript, and SQL is not MySQL. A name of two characters or fewer (Go,
C, R, AI) reads as other words in prose, and is looked for in the lists
only.
"""

import re
from functools import lru_cache

from backend.scoring import score
from backend.tailor.resume import tailored_profile

# Names prose can't be searched for: any canonical name this short. Go
# starts sentences, C and R are letters, and a two-letter name is as often
# some other abbreviation.
SHORT_NAME = 2

# Spellings of a skill that are everyday words ("the next release", "the
# rest of the team"). Prose is searched for its other spellings: Next.js,
# RESTful.
_EVERYDAY = frozenset({"next", "node", "rest"})


def _prose_forms(canonical: str) -> list[str]:
    """How prose may spell a skill: its canonical name and each alias of
    it, less the spellings too short or too common to mean it. A short
    one with a symbol in it (C#) means nothing else."""
    forms = {canonical} | {alias for alias, name in score.SKILL_ALIASES.items()
                           if name == canonical}
    return sorted(
        (f for f in forms
         if f not in _EVERYDAY and (len(f) > SHORT_NAME or not f.isalpha())),
        key=lambda f: (-len(f), f))


@lru_cache(maxsize=1024)
def _prose_pattern(canonical: str) -> re.Pattern | None:
    """None for a skill looked for in the lists only."""
    if len(canonical) <= SHORT_NAME:
        return None
    spellings = []
    for form in _prose_forms(canonical):
        body = r"\s+".join(re.escape(word) for word in form.split())
        # Its plural, when it ends in a letter: APIs, microservices.
        spellings.append(body + ("s?" if form[-1].isalpha() else ""))
    # Never inside a longer word or name on either side: Java is not
    # JavaScript, SQL is not MySQL, React is not Reactive.
    return re.compile(r"(?<!\w)(?:" + "|".join(spellings) + r")(?![\w+#])",
                      re.IGNORECASE)


def mentioned(canonical: str, texts: list[str]) -> bool | None:
    """Whether any of the texts mentions the skill, None when the skill is
    not looked for in prose."""
    pattern = _prose_pattern(canonical)
    if pattern is None:
        return None
    return any(pattern.search(text) for text in texts)


def package_coverage(package: dict, profile: dict | None) -> list[dict] | None:
    """Each skill the posting states, in its words, and where the
    package's documents mention it: `listed` in the resume's skill groups
    or project technologies, `resume` in its headline or a bullet,
    `letter` in a cover letter paragraph. The last two are None for a
    skill looked for in the lists only.

    None for a package that has not finished, or whose inputs are gone.
    `profile` is the profile version the package is pinned to.
    """
    if package.get("status") != "done" or profile is None:
        return None
    stage = package.get("stage") or {}
    try:
        requirements = stage["generation"]["requirements"]
        shipped = tailored_profile(profile, stage["resume_fit"]["tailored"])
        letter = [p["text"] for p in stage["cover_fit"]["tailored"]["cover_letter"]["paragraphs"]]
        stated = score.stated_skills(requirements)
    except (KeyError, TypeError, AttributeError):
        return None
    # Every section ships: the validator holds section_order to a
    # permutation of them all.
    resume = [shipped.get("headline") or ""]
    for key, lines in (("experiences", "bullets"), ("projects", "bullets"),
                       ("education", "details")):
        for entry in shipped.get(key, []):
            resume += entry.get(lines, [])
    listed = score.flatten_profile_skills(shipped)
    return [
        {"kind": kind, "label": label,
         "listed": score.skill_matches(canonical, listed),
         "resume": mentioned(canonical, resume),
         "letter": mentioned(canonical, letter)}
        for kind, label, canonical in stated
    ]
