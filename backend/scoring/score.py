"""Deterministic job scorer — the primary ranking. No LLM calls.

Ported from v1 `tools/score_jobs.py` (weights, YoE curve, education
levels, reasons) and extended per PLAN.md §4.4:

- operates on normalized requirements (backend/normalize) vs. the
  structured profile — never on raw description text;
- skill matching is alias-aware with token-exact comparison. The v1
  substring rule ("java" in "javascript") is deliberately NOT ported —
  it manufactured false matches; the labeled fixture tests pin this.
- experience filter: jobs demanding more YoE than the user has (plus a
  tolerance) are flagged `exceeds_experience` for UI filtering.

Pure functions over plain dicts — reproducible run-to-run by
construction, and cheap enough to re-run for a whole result set.
"""

import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

# Composite weights, unchanged from v1: skills are the strongest fit
# signal (60%), then experience (25%), then education (15%).
WEIGHTS = {"skills": 0.60, "experience": 0.25, "education": 0.15}

EDUCATION_LEVELS = {
    "associate": 1, "associates": 1,
    "bachelor": 2, "bachelors": 2, "b.s": 2, "bsc": 2, "beng": 2, "b.eng": 2,
    "master": 3, "masters": 3, "m.s": 3, "msc": 3, "meng": 3, "m.eng": 3,
    "doctorate": 4, "phd": 4, "ph.d": 4,
}

# Jobs demanding more than user_years + tolerance get flagged (the
# experience filter, PLAN.md §4.4).
EXPERIENCE_TOLERANCE_YEARS = 1

# Canonical names for common spelling/abbreviation variants. Both sides
# of a comparison are canonicalized, so either direction matches. This
# replaces v1's substring rule — "java" and "javascript" stay distinct.
SKILL_ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "k8s": "kubernetes",
    "postgres": "postgresql",
    "node": "node.js",
    "nodejs": "node.js",
    "react.js": "react",
    "reactjs": "react",
    "vue.js": "vue",
    "vuejs": "vue",
    "next": "next.js",
    "nextjs": "next.js",
    "c++": "cpp",
    "c#": "csharp",
    ".net": "dotnet",
    "amazon web services": "aws",
    "google cloud platform": "gcp",
    "google cloud": "gcp",
    "microsoft azure": "azure",
    "scikit learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "tf": "tensorflow",
    "ci/cd": "cicd",
    "ci cd": "cicd",
    "restful": "rest",
    "rest api": "rest",
    "rest apis": "rest",
}


def canonical_skill(skill: str) -> str:
    s = " ".join(skill.lower().strip().split())
    return SKILL_ALIASES.get(s, s)


def _skill_matches(required: str, user_skills: set[str]) -> bool:
    """Alias-aware exact match with a tight fuzzy fallback for typos and
    plural drift. Deliberately no substring rule (Java != JavaScript)."""
    req = canonical_skill(required)
    if req in user_skills:
        return True
    for mine in user_skills:
        if SequenceMatcher(None, req, mine).ratio() >= 0.92:
            return True
    return False


def flatten_profile_skills(profile: dict) -> set[str]:
    """Every skill the profile claims, canonicalized: skill groups plus
    project technologies."""
    skills: set[str] = set()
    for group in profile.get("skills", []):
        for item in group.get("items", []):
            skills.add(canonical_skill(item))
    for project in profile.get("projects", []):
        for tech in project.get("technologies", []):
            skills.add(canonical_skill(tech))
    return skills


# --- years of experience from the profile -----------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_month_year(text: str | None, now: datetime) -> tuple[int, int] | None:
    """'May 2023' / '05/2023' / '2023' / 'Present' -> (year, month)."""
    if not text:
        return None
    lowered = text.lower().strip()
    if "present" in lowered or "current" in lowered or "now" in lowered:
        return now.year, now.month
    year_match = re.search(r"(19|20)\d{2}", lowered)
    if not year_match:
        return None
    year = int(year_match.group())
    for name, number in _MONTHS.items():
        if name in lowered:
            return year, number
    numeric = re.match(r"(\d{1,2})\s*/\s*(19|20)\d{2}", lowered)
    if numeric:
        month = int(numeric.group(1))
        if 1 <= month <= 12:
            return year, month
    return year, 6  # year only — assume mid-year


def profile_years_experience(profile: dict, now: datetime | None = None) -> float | None:
    """Total professional years from the profile's experience date
    ranges — overlapping stints merged so parallel roles don't double
    count. None when nothing parses (the scorer then stays neutral)."""
    now = now or datetime.now(timezone.utc)
    intervals: list[tuple[int, int]] = []  # months since year 0
    for exp in profile.get("experiences", []):
        start = _parse_month_year(exp.get("start"), now)
        end = _parse_month_year(exp.get("end"), now) or (
            (now.year, now.month) if start else None
        )
        if start is None or end is None:
            continue
        a = start[0] * 12 + start[1]
        b = end[0] * 12 + end[1]
        if b >= a:
            intervals.append((a, b))
    if not intervals:
        return None
    intervals.sort()
    merged = [intervals[0]]
    for a, b in intervals[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    months = sum(b - a for a, b in merged)
    return round(months / 12, 2)


# --- component scores (curves ported from v1) --------------------------------

def skill_score(requirements: dict, user_skills: set[str]) -> tuple[float, list, list]:
    """Required skills weigh 2x, preferred 1x. Returns
    (score 0-100, matched, missing) with required-missing first."""
    required = [canonical_skill(s) for s in requirements.get("required_skills", [])]
    preferred = [canonical_skill(s) for s in requirements.get("preferred_skills", [])]
    if not required and not preferred:
        return 50.0, [], []  # no skill data — neutral
    matched, missing = [], []
    total = earned = 0
    for skill in required:
        total += 2
        if _skill_matches(skill, user_skills):
            earned += 2
            matched.append(skill)
        else:
            missing.append(skill)
    for skill in preferred:
        total += 1
        if _skill_matches(skill, user_skills):
            earned += 1
            matched.append(skill)
    return (earned / total) * 100, matched, missing


def experience_score(requirements: dict, user_years: float | None) -> tuple[float, bool]:
    """(score, exceeds_experience). v1's asymmetric curve: overshooting
    the requirement decays gently, undershooting steeply."""
    required = requirements.get("min_years_experience")
    if required is None:
        return 70.0, False  # no data — slightly positive
    if user_years is None:
        return 70.0, False  # profile dates unparseable — stay neutral
    exceeds = required > user_years + EXPERIENCE_TOLERANCE_YEARS
    diff = user_years - required
    if diff == 0:
        return 100.0, exceeds
    if diff > 0:
        return max(40.0, 100.0 - (diff**1.5) * 5), exceeds
    return max(10.0, 100.0 - (abs(diff) ** 1.5) * 12), exceeds


def _profile_education_level(profile: dict) -> int:
    level = 0
    for edu in profile.get("education", []):
        degree = (edu.get("degree") or "").lower()
        for keyword, value in EDUCATION_LEVELS.items():
            if keyword in degree:
                level = max(level, value)
    return level


def education_score(requirements: dict, profile: dict) -> float:
    """100 exact, 90 exceeds, 30 falls short, 80 when unstated (v1)."""
    required_name = requirements.get("education_requirement")
    if not required_name:
        return 80.0
    required = EDUCATION_LEVELS.get(required_name.lower(), 0)
    if required == 0:
        return 80.0
    mine = _profile_education_level(profile)
    if mine >= required:
        return 100.0 if mine == required else 90.0
    return 30.0


def score_job(
    requirements: dict, profile: dict, user_years: float | None = None
) -> dict:
    """Score one job's normalized requirements against the profile.
    Deterministic: same inputs -> identical output dict."""
    user_skills = flatten_profile_skills(profile)
    if user_years is None:
        user_years = profile_years_experience(profile)

    skills, matched, missing = skill_score(requirements, user_skills)
    experience, exceeds = experience_score(requirements, user_years)
    education = education_score(requirements, profile)

    composite = (
        skills * WEIGHTS["skills"]
        + experience * WEIGHTS["experience"]
        + education * WEIGHTS["education"]
    )

    match_reasons, gap_reasons = [], []
    if skills >= 60:
        shown = ", ".join(matched[:5])
        match_reasons.append(f"Strong skill match ({len(matched)} skills: {shown})")
    if experience >= 80:
        match_reasons.append("Experience level aligns well")
    if education >= 80:
        match_reasons.append("Education requirements met")
    if missing:
        gap_reasons.append(f"Missing skills: {', '.join(missing[:5])}")
    if experience < 50:
        gap_reasons.append("Experience level mismatch")
    if education < 50:
        gap_reasons.append("Education requirement not met")

    return {
        "deterministic_score": round(composite, 1),
        "breakdown": {
            "skills": round(skills, 1),
            "experience": round(experience, 1),
            "education": round(education, 1),
            "matched_skills": matched,
            "missing_skills": missing,
            "match_reasons": match_reasons,
            "gap_reasons": gap_reasons,
            "exceeds_experience": exceeds,
            "user_years": user_years,
        },
    }


# --- the requirement checklist ------------------------------------------------

_DEGREE_NAMES = {1: "Associate degree", 2: "Bachelor's degree",
                 3: "Master's degree", 4: "Doctorate"}


def requirement_checklist(requirements: dict, profile: dict,
                          user_years: float | None) -> list[dict]:
    """Each requirement the posting states, in the posting's own words,
    and whether the profile meets it — by the rules `score_job` scores
    with, so the list and the score never disagree.

    `kind` is required, preferred, experience or education. `met` is None
    where the scorer stays neutral: profile dates it cannot read, or a
    degree level it does not know. Pass the `user_years` the score was
    computed with (its breakdown keeps them), not a fresh count.
    """
    user_skills = flatten_profile_skills(profile)
    items: list[dict] = []
    for kind, key in (("required", "required_skills"),
                      ("preferred", "preferred_skills")):
        seen: set[str] = set()
        for skill in requirements.get(key, []):
            canonical = canonical_skill(skill)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            items.append({"kind": kind, "label": skill.strip(),
                          "met": _skill_matches(canonical, user_skills)})
    years = requirements.get("min_years_experience")
    if years is not None:
        items.append({
            "kind": "experience",
            "label": f"{years}+ year{'' if years == 1 else 's'} of experience",
            "met": None if user_years is None else user_years >= years,
        })
    degree = requirements.get("education_requirement")
    level = EDUCATION_LEVELS.get((degree or "").lower(), 0)
    if degree:
        items.append({
            "kind": "education",
            "label": _DEGREE_NAMES.get(level, degree),
            "met": None if level == 0 else _profile_education_level(profile) >= level,
        })
    return items
