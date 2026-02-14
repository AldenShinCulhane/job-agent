"""
Summary report generator. Creates a markdown report with scoring breakdown,
skill gap analysis, and top job recommendations. No LLM calls needed.
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_score_distribution(jobs: list) -> str:
    """ASCII histogram of score ranges."""
    buckets = {"0-19": 0, "20-34": 0, "35-49": 0, "50-64": 0, "65-79": 0, "80-100": 0}
    for job in jobs:
        score = job.get("match_score", 0)
        if score < 20:
            buckets["0-19"] += 1
        elif score < 35:
            buckets["20-34"] += 1
        elif score < 50:
            buckets["35-49"] += 1
        elif score < 65:
            buckets["50-64"] += 1
        elif score < 80:
            buckets["65-79"] += 1
        else:
            buckets["80-100"] += 1

    max_count = max(buckets.values()) if buckets.values() else 1
    lines = []
    for range_label, count in buckets.items():
        bar_len = int((count / max_count) * 30) if max_count > 0 else 0
        bar = "#" * bar_len
        marker = " <-- threshold" if range_label == "35-49" else ""
        lines.append(f"  {range_label:>6}% | {bar:<30} {count}{marker}")
    return "\n".join(lines)


def build_top_jobs_table(jobs: list, limit: int = 25) -> str:
    """Markdown table of top-scoring jobs."""
    top = jobs[:limit]
    lines = ["| # | Score | Title | Company | Location | Apply |",
             "|---|-------|-------|---------|----------|-------|"]
    for i, job in enumerate(top):
        title = job.get("title", "N/A")[:40]
        company = job.get("company", "N/A")[:25]
        location = job.get("location", "N/A")[:20]
        score = job.get("match_score", 0)
        apply_url = job.get("apply_url", "")
        apply_link = f"[Apply]({apply_url})" if apply_url else "N/A"
        lines.append(f"| {i + 1} | {score}% | {title} | {company} | {location} | {apply_link} |")
    return "\n".join(lines)


def build_skill_gap_analysis(jobs: list, user_skills: set) -> str:
    """Find skills most commonly requested but missing from user's profile."""
    missing_counter = Counter()
    for job in jobs:
        for skill in job.get("missing_skills", []):
            missing_counter[skill.lower()] += 1

    if not missing_counter:
        return "No significant skill gaps identified."

    lines = ["| Skill | Requested By (jobs) | Priority |",
             "|-------|--------------------:|----------|"]
    for skill, count in missing_counter.most_common(15):
        priority = "HIGH" if count >= 5 else "MEDIUM" if count >= 3 else "LOW"
        lines.append(f"| {skill} | {count} | {priority} |")
    return "\n".join(lines)


def build_company_breakdown(jobs: list) -> str:
    """Which companies appeared most."""
    company_counter = Counter()
    for job in jobs:
        company = job.get("company", "Unknown")
        if company:
            company_counter[company] += 1

    lines = ["| Company | Open Positions | Avg Match |",
             "|---------|---------------:|----------:|"]
    for company, count in company_counter.most_common(15):
        company_jobs = [j for j in jobs if j.get("company") == company]
        avg_score = sum(j.get("match_score", 0) for j in company_jobs) / len(company_jobs)
        lines.append(f"| {company[:35]} | {count} | {avg_score:.0f}% |")
    return "\n".join(lines)


def generate_report(
    jobs_path: str = None,
    config_path: str = None,
    profile_path: str = None,
    output_path: str = None,
) -> str:
    """Generate the summary report markdown."""
    if jobs_path is None:
        jobs_path = str(PROJECT_ROOT / ".tmp" / "scored_jobs.json")
    if config_path is None:
        config_path = str(PROJECT_ROOT / "config" / "search_filters.yaml")
    if profile_path is None:
        profile_path = str(PROJECT_ROOT / "config" / "user_profile.yaml")
    if output_path is None:
        output_path = str(PROJECT_ROOT / "output" / "summary_report.md")

    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    with open(profile_path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    # Gather user skills for gap analysis
    user_skills = set()
    for cat_skills in profile.get("skills", {}).values():
        if isinstance(cat_skills, list):
            for s in cat_skills:
                user_skills.add(s.lower())

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    search_query = config.get("search", {}).get("query", "N/A")
    locations = [l.get("formatted_address", "?") for l in config.get("locations", [])]
    experience = config.get("experience_levels", [])
    workplace = config.get("workplace_types", [])

    total = len(jobs)
    above_35 = sum(1 for j in jobs if j.get("match_score", 0) >= 35)
    above_50 = sum(1 for j in jobs if j.get("match_score", 0) >= 50)
    above_70 = sum(1 for j in jobs if j.get("match_score", 0) >= 70)
    avg_score = sum(j.get("match_score", 0) for j in jobs) / total if total else 0

    report = f"""# Job Search Report

**Generated:** {now}
**Search Query:** "{search_query}"
**Locations:** {', '.join(locations)}
**Experience Levels:** {', '.join(experience)}
**Workplace Types:** {', '.join(workplace)}

---

## Summary

| Metric | Value |
|--------|------:|
| Total jobs found | {total:,} |
| Jobs scoring 35%+ (application threshold) | {above_35} |
| Jobs scoring 50%+ (strong match) | {above_50} |
| Jobs scoring 70%+ (excellent match) | {above_70} |
| Average match score | {avg_score:.1f}% |

## Score Distribution

```
{build_score_distribution(jobs)}
```

---

## Top Job Matches

{build_top_jobs_table(jobs)}

---

## Score Breakdown (Top 10)

"""
    for i, job in enumerate(jobs[:10]):
        breakdown = job.get("match_breakdown", {})
        reasons = job.get("match_reasons", [])
        gaps = job.get("gap_reasons", [])
        report += f"""### {i + 1}. {job.get('title', 'N/A')} at {job.get('company', 'N/A')} — {job.get('match_score', 0)}%

| Category | Score |
|----------|------:|
| Skills (60%) | {breakdown.get('skills', 0)}% |
| Experience (25%) | {breakdown.get('experience', 0)}% |
| Education (15%) | {breakdown.get('education', 0)}% |

"""
        if reasons:
            report += "**Why you match:** " + " | ".join(reasons) + "\n\n"
        if gaps:
            report += "**Gaps:** " + " | ".join(gaps) + "\n\n"

    report += f"""---

## Skill Gap Analysis

These skills are most frequently requested by matching jobs but missing from your profile. Consider acquiring these to improve your match rate.

{build_skill_gap_analysis(jobs, user_skills)}

---

## Company Breakdown

{build_company_breakdown(jobs)}

---

## Recommendations

"""
    if above_70 > 0:
        report += f"- **Prioritize the top {min(above_70, 5)} excellent matches** (70%+) — these roles closely align with your profile\n"
    if above_35 > 0:
        report += f"- **{above_35} jobs** meet the application threshold — tailored applications have been generated for these\n"

    # Most common missing skills
    missing = Counter()
    for job in jobs[:20]:
        for s in job.get("missing_skills", []):
            missing[s.lower()] += 1
    top_missing = missing.most_common(3)
    if top_missing:
        skills_to_learn = ", ".join(s for s, _ in top_missing)
        report += f"- **Skill development:** Learning {skills_to_learn} would significantly improve your match rate\n"

    report += f"\n---\n\n*Report generated by WAT Job Search Automation*\n"

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Report saved -> {output_path}")
    return report


def main():
    parser = argparse.ArgumentParser(description="Generate job search summary report")
    parser.add_argument("--jobs", default=str(PROJECT_ROOT / ".tmp" / "scored_jobs.json"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "search_filters.yaml"))
    parser.add_argument("--profile", default=str(PROJECT_ROOT / "config" / "user_profile.yaml"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "output" / "summary_report.md"))
    args = parser.parse_args()
    generate_report(args.jobs, args.config, args.profile, args.output)


if __name__ == "__main__":
    main()
