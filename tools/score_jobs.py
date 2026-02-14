"""
Deterministic resume-job scoring engine. No LLM calls — purely algorithmic.
Scores each job against the user's profile and ranks by match percentage.

Jobs are already filtered by the user's search criteria (location, title,
experience level, salary, etc.) during parsing. This scorer only measures
how well the candidate's skills, experience, and education match each job's
requirements.
"""

import argparse
import json
import os
from difflib import SequenceMatcher
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Composite score weights: skills matter most (60%), then experience (25%),
# then education (15%). Skills are the strongest predictor of job fit,
# while experience and education are secondary signals.
WEIGHTS = {
    "skills": 0.60,
    "experience": 0.25,
    "education": 0.15,
}

# Maps education keywords to numeric levels for comparison.
# Handles both singular ("bachelor") and plural ("bachelors") forms
# so it works with both LLM analysis strings and v5 structured data.
EDUCATION_LEVELS = {
    "associate": 1, "associates": 1,
    "bachelor": 2, "bachelors": 2,
    "master": 3, "masters": 3,
    "doctorate": 4, "phd": 4,
}


def load_profile(profile_path: str) -> dict:
    with open(profile_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def flatten_skills(profile: dict) -> set:
    """Extract all skills from profile as a lowercase set, including project technologies."""
    skills = set()
    for category_skills in profile.get("skills", {}).values():
        if isinstance(category_skills, list):
            for s in category_skills:
                skills.add(s.lower().strip())
    for project in profile.get("projects", []):
        for tech in project.get("technologies", []):
            skills.add(tech.lower().strip())
    return skills


def calculate_skill_match(job: dict, user_skills: set) -> tuple:
    """
    Score skill match using v5 technical_tools + LLM analysis if available.
    Required skills (from analysis) count 2x, others 1x.
    Returns (score 0-100, matched_skills, missing_skills).
    """
    # Primary: v5 extracted skills
    v5_skills = [s.lower().strip() for s in job.get("skills", [])]

    # LLM analysis supplements
    analysis = job.get("analysis", {})
    required = [s.lower().strip() for s in analysis.get("required_skills", [])]
    preferred = [s.lower().strip() for s in analysis.get("preferred_skills", [])]

    # If no analysis, treat all v5 skills as required
    if not required and not preferred:
        required = v5_skills
    else:
        # Merge v5 skills into required if not already there
        for skill in v5_skills:
            if skill not in required and skill not in preferred:
                required.append(skill)

    if not required and not preferred:
        return 50.0, [], []  # No skill data — neutral score

    matched = []
    missing = []
    total_weight = 0
    earned_weight = 0

    for skill in required:
        weight = 2
        total_weight += weight
        if _skill_in_set(skill, user_skills):
            earned_weight += weight
            matched.append(skill)
        else:
            missing.append(skill)

    for skill in preferred:
        weight = 1
        total_weight += weight
        if _skill_in_set(skill, user_skills):
            earned_weight += weight
            matched.append(skill)

    if total_weight == 0:
        return 50.0, matched, missing

    return (earned_weight / total_weight) * 100, matched, missing


def _skill_in_set(skill: str, skill_set: set) -> bool:
    if skill in skill_set:
        return True
    for user_skill in skill_set:
        if skill in user_skill or user_skill in skill:
            return True
        if SequenceMatcher(None, skill, user_skill).ratio() > 0.85:
            return True
    return False


def calculate_experience_match(job: dict, user_years: int) -> float:
    """Gaussian proximity score for years of experience."""
    # Use v5 min_years_experience first, fallback to analysis
    required_yoe = job.get("min_years_experience")
    if required_yoe is None:
        analysis = job.get("analysis", {})
        required_yoe = analysis.get("years_experience_required")

    if required_yoe is None:
        return 70.0  # No data — slightly positive default

    required_yoe = int(required_yoe)
    diff = user_years - required_yoe

    if diff == 0:
        return 100.0
    elif diff > 0:
        return max(40.0, 100.0 - (diff ** 1.5) * 5)
    else:
        return max(10.0, 100.0 - (abs(diff) ** 1.5) * 12)


def calculate_education_match(job: dict, user_education: list) -> float:
    """Score how well the user's education matches the job's requirements.

    Compares the highest education level the user has against what the job
    requires. Returns 100 if they match exactly, 90 if user exceeds the
    requirement, 30 if user falls short, or 80 if the job has no stated
    education requirement.

    Uses v5 structured data when available, falls back to parsing the
    LLM analysis string.
    """
    edu_reqs = job.get("education_requirements", {})

    # Determine required education level
    if not edu_reqs:
        # Fallback: parse from LLM analysis string (e.g., "Bachelor's in CS")
        analysis = job.get("analysis", {})
        requirement = analysis.get("education_requirement")
        if not requirement:
            return 80.0  # No requirement stated
        requirement_lower = requirement.lower()
        required_level = 0
        for keyword, level in EDUCATION_LEVELS.items():
            if keyword in requirement_lower:
                required_level = max(required_level, level)
        if required_level == 0:
            return 80.0
    else:
        # Primary: use v5 structured data (keys like "bachelors", "masters")
        required_level = 0
        for level_name in edu_reqs:
            if level_name in EDUCATION_LEVELS:
                required_level = max(required_level, EDUCATION_LEVELS[level_name])
        if required_level == 0:
            return 80.0

    # Determine user's highest education level
    user_max_level = 0
    for edu in user_education:
        degree = edu.get("degree", "").lower()
        for keyword, level in EDUCATION_LEVELS.items():
            if keyword in degree:
                user_max_level = max(user_max_level, level)

    if user_max_level >= required_level:
        return 100.0 if user_max_level == required_level else 90.0
    return 30.0


def score_job(job: dict, profile: dict) -> dict:
    """Score a single job against the user's profile.

    Calculates skill, experience, and education sub-scores, then combines
    them using WEIGHTS into a composite match_score (0-100). Returns the
    job dict enriched with scoring fields.
    """
    user_skills = flatten_skills(profile)
    user_years = profile.get("experience", {}).get("total_years", 0)
    user_education = profile.get("education", [])

    skill_score, matched_skills, missing_skills = calculate_skill_match(job, user_skills)
    exp_score = calculate_experience_match(job, user_years)
    education_score = calculate_education_match(job, user_education)

    composite = (
        skill_score * WEIGHTS["skills"]
        + exp_score * WEIGHTS["experience"]
        + education_score * WEIGHTS["education"]
    )

    match_reasons = []
    if skill_score >= 60:
        match_reasons.append(f"Strong skill match ({len(matched_skills)} skills: {', '.join(matched_skills[:5])})")
    if exp_score >= 80:
        match_reasons.append("Experience level aligns well")
    if education_score >= 80:
        match_reasons.append("Education requirements met")

    gap_reasons = []
    if missing_skills:
        gap_reasons.append(f"Missing skills: {', '.join(missing_skills[:5])}")
    if exp_score < 50:
        gap_reasons.append("Experience level mismatch")
    if education_score < 50:
        gap_reasons.append("Education requirement not met")

    result = dict(job)
    result["match_score"] = round(composite, 1)
    result["match_breakdown"] = {
        "skills": round(skill_score, 1),
        "experience": round(exp_score, 1),
        "education": round(education_score, 1),
    }
    result["match_reasons"] = match_reasons
    result["gap_reasons"] = gap_reasons
    result["matched_skills"] = matched_skills
    result["missing_skills"] = missing_skills
    return result


def score_jobs(jobs_path: str = None, profile_path: str = None, output_path: str = None) -> list:
    """Score all jobs against the user profile, sort by match, and save results."""
    if jobs_path is None:
        jobs_path = str(PROJECT_ROOT / ".tmp" / "analyzed_jobs.json")
    if profile_path is None:
        profile_path = str(PROJECT_ROOT / "config" / "user_profile.yaml")
    if output_path is None:
        output_path = str(PROJECT_ROOT / ".tmp" / "scored_jobs.json")

    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    profile = load_profile(profile_path)

    print(f"Scoring {len(jobs):,} jobs against profile...")

    scored = [score_job(job, profile) for job in jobs]
    scored.sort(key=lambda j: j["match_score"], reverse=True)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(scored, f, indent=2, ensure_ascii=False)

    print(f"\nTop 5 matches:")
    for i, job in enumerate(scored[:5]):
        print(f"  {i + 1}. [{job['match_score']}%] {job['title']} at {job['company']}")
        if job["match_reasons"]:
            print(f"     {job['match_reasons'][0]}")

    above_35 = sum(1 for j in scored if j["match_score"] >= 35)
    print(f"\n{above_35} jobs scoring 35%+ (application threshold)")
    print(f"Scored {len(scored):,} jobs -> {output_path}")
    return scored


def main():
    parser = argparse.ArgumentParser(description="Score jobs against user profile")
    parser.add_argument("--jobs", default=str(PROJECT_ROOT / ".tmp" / "analyzed_jobs.json"))
    parser.add_argument("--profile", default=str(PROJECT_ROOT / "config" / "user_profile.yaml"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / ".tmp" / "scored_jobs.json"))
    args = parser.parse_args()
    score_jobs(args.jobs, args.profile, args.output)


if __name__ == "__main__":
    main()
