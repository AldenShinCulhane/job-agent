"""
Job data normalizer. Flattens raw hiring.cafe API responses into a
consistent schema, strips HTML, deduplicates, filters against user's
search criteria, and extracts structured data from the
v5_processed_job_data field that hiring.cafe provides.
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def clean_html(html: str) -> str:
    """Strip HTML tags and decode entities to produce clean text."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_job(raw: dict) -> dict:
    """Flatten a single raw job into the normalized schema.

    hiring.cafe returns rich structured data in v5_processed_job_data and
    v5_processed_company_data. We use those directly when available,
    falling back to manual extraction from description.
    """
    ji = raw.get("job_information", {})
    v5_job = raw.get("v5_processed_job_data", {})
    v5_company = raw.get("v5_processed_company_data", {})

    # Title — prefer v5 core_job_title, fallback to job_information
    title = (
        v5_job.get("core_job_title")
        or ji.get("title")
        or ji.get("job_title_raw")
        or raw.get("title")
        or ""
    )

    # Company — v5 has company_name, also in company_info
    company = (
        v5_job.get("company_name")
        or v5_company.get("name")
        or (ji.get("company_info", {}) or {}).get("name")
        or raw.get("source")
        or ""
    )

    # Location
    location = v5_job.get("formatted_workplace_location", "")
    if not location:
        workplace_countries = v5_job.get("workplace_countries", [])
        workplace_states = v5_job.get("workplace_states", [])
        workplace_cities = v5_job.get("workplace_cities", [])
        parts = workplace_cities + workplace_states + workplace_countries
        location = ", ".join(parts) if parts else ""

    # Workplace type
    workplace_type = v5_job.get("workplace_type", "")

    # Description — raw HTML in job_information.description
    desc_raw = ji.get("description", "") or raw.get("description", "")
    desc_text = clean_html(desc_raw) if "<" in str(desc_raw) else str(desc_raw)

    # Apply URL
    apply_url = raw.get("apply_url", "") or ""

    # Experience level — from v5 structured data
    experience_level = v5_job.get("seniority_level", "")

    # Commitment type
    commitment_list = v5_job.get("commitment", [])
    commitment = commitment_list[0] if commitment_list else ""

    # Salary — v5 provides yearly min/max directly
    salary_min = v5_job.get("yearly_min_compensation")
    salary_max = v5_job.get("yearly_max_compensation")
    salary_currency = v5_job.get("listed_compensation_currency", "USD")
    is_transparent = v5_job.get("is_compensation_transparent", False)

    # Skills — from v5 technical_tools (already extracted by hiring.cafe's AI)
    skills = v5_job.get("technical_tools", [])

    # Experience years — directly available
    min_yoe = v5_job.get("min_industry_and_role_yoe")

    # Education requirements
    education = {}
    for level in ("bachelors", "masters", "doctorate", "associates"):
        req_key = f"{level}_degree_requirement"
        fields_key = f"{level}_degree_fields_of_study"
        req_val = v5_job.get(req_key, "Not Mentioned")
        if req_val and req_val != "Not Mentioned":
            education[level] = {
                "requirement": req_val,
                "fields": v5_job.get(fields_key, []),
            }

    # Company metadata
    company_data = {
        "website": v5_company.get("website") or v5_job.get("company_website", ""),
        "industry": v5_job.get("company_sector_and_industry", ""),
        "tagline": v5_company.get("tagline") or v5_job.get("company_tagline", ""),
        "is_non_profit": v5_company.get("is_non_profit", False),
        "num_employees": v5_company.get("num_employees"),
        "year_founded": v5_company.get("year_founded"),
        "activities": v5_company.get("activities") or v5_job.get("company_activities", []),
    }

    # Engagement stats
    viewed_users = ji.get("viewedByUsers", [])
    applied_users = ji.get("appliedFromUsers", [])
    saved_users = ji.get("savedFromUsers", [])

    # Dates
    date_posted = v5_job.get("estimated_publish_date")

    # Additional structured data from v5
    role_type = v5_job.get("role_type", "")
    role_activities = v5_job.get("role_activities", [])
    requirements_summary = v5_job.get("requirements_summary", "")
    certifications = v5_job.get("licenses_or_certifications", [])
    visa_sponsorship = v5_job.get("visa_sponsorship", False)
    remote_countries = v5_job.get("workplace_countries", [])

    return {
        "id": str(raw.get("id", raw.get("objectID", ""))),
        "requisition_id": raw.get("requisition_id", ""),
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "workplace_type": workplace_type,
        "apply_url": apply_url.strip(),
        "description_text": desc_text,
        "salary_min": float(salary_min) if salary_min else None,
        "salary_max": float(salary_max) if salary_max else None,
        "salary_currency": salary_currency if (salary_min or salary_max) else None,
        "salary_transparent": is_transparent,
        "experience_level": experience_level,
        "commitment_type": commitment,
        "skills": skills,
        "min_years_experience": min_yoe,
        "education_requirements": education,
        "requirements_summary": requirements_summary,
        "role_type": role_type,
        "role_activities": role_activities,
        "certifications_required": certifications,
        "visa_sponsorship": visa_sponsorship,
        "company_data": company_data,
        "date_posted": date_posted,
        "date_fetched": datetime.now(timezone.utc).isoformat(),
        "views": len(viewed_users),
        "applications": len(applied_users),
        "saves": len(saved_users),
        "is_expired": raw.get("is_expired", False),
    }


TITLE_STOP_WORDS = {
    "new", "grad", "graduate", "level", "senior", "junior", "mid",
    "lead", "staff", "principal", "associate", "intern", "entry",
    "i", "ii", "iii", "iv", "v", "the", "a", "an", "and", "or", "of", "for", "in", "at",
}


def _extract_title_keywords(query: str) -> list:
    """Extract substantive keywords from a search query for title matching."""
    words = re.findall(r"\w+", query.lower())
    return [w for w in words if w not in TITLE_STOP_WORDS and len(w) > 1]


def _matches_title(job_title: str, keywords: list) -> bool:
    """Check if job title contains at least one keyword from the search query."""
    if not keywords:
        return True
    title_lower = job_title.lower()
    return any(kw in title_lower for kw in keywords)


def _matches_location(job_location: str, job_workplace: str, config: dict) -> bool:
    """Check if job location matches any of the user's specified locations."""
    config_locations = config.get("locations", [])
    config_workplace_types = [w.lower() for w in config.get("workplace_types", [])]

    # Remote jobs pass if user accepts remote
    if job_workplace.lower() == "remote" and "remote" in config_workplace_types:
        return True

    if not config_locations:
        return True

    job_loc_lower = job_location.lower()
    for loc in config_locations:
        address = loc.get("formatted_address", "").lower()
        # Split address into parts (e.g., "Toronto, Ontario" -> ["toronto", "ontario"])
        parts = [p.strip() for p in address.split(",")]
        # Match if any substantive part appears in the job location
        if any(part in job_loc_lower for part in parts if len(part) > 2):
            return True

    return False


def _matches_workplace_type(job_workplace: str, config: dict) -> bool:
    """Check if job's workplace type matches user's specified types."""
    config_types = config.get("workplace_types", [])
    if not config_types:
        return True
    if not job_workplace:
        return True  # No data on job — benefit of the doubt
    return job_workplace.lower() in [t.lower() for t in config_types]


def _matches_experience_level(job_level: str, config: dict) -> bool:
    """Check if job's experience level matches user's specified levels."""
    config_levels = config.get("experience_levels", [])
    if not config_levels:
        return True
    if not job_level:
        return True  # No data on job — benefit of the doubt
    return job_level.lower() in [l.lower() for l in config_levels]


def _matches_salary(job_salary_max: float | None, config: dict) -> bool:
    """Check if job's salary meets user's minimum. Jobs with no salary data pass."""
    salary_cfg = config.get("salary", {})
    min_annual = salary_cfg.get("min_annual")
    if not min_annual:
        return True
    if job_salary_max is None:
        return True  # No salary data — benefit of the doubt
    return job_salary_max >= min_annual


def _matches_commitment_type(job_commitment: str, config: dict) -> bool:
    """Check if job's commitment type matches user's specified types."""
    config_types = config.get("commitment_types", [])
    if not config_types:
        return True
    if not job_commitment:
        return True  # No data — benefit of the doubt
    return job_commitment.lower() in [t.lower() for t in config_types]


def filter_jobs(jobs: list, config: dict) -> list:
    """Strictly enforce user's search filters. Only jobs passing ALL specified filters survive."""
    query = config.get("search", {}).get("query", "")
    title_keywords = _extract_title_keywords(query)

    filtered = []
    reasons_dropped = {"title": 0, "location": 0, "workplace": 0, "experience": 0, "salary": 0, "commitment": 0}

    for job in jobs:
        if not _matches_title(job["title"], title_keywords):
            reasons_dropped["title"] += 1
            continue
        if not _matches_location(job["location"], job.get("workplace_type", ""), config):
            reasons_dropped["location"] += 1
            continue
        if not _matches_workplace_type(job.get("workplace_type", ""), config):
            reasons_dropped["workplace"] += 1
            continue
        if not _matches_experience_level(job.get("experience_level", ""), config):
            reasons_dropped["experience"] += 1
            continue
        if not _matches_salary(job.get("salary_max"), config):
            reasons_dropped["salary"] += 1
            continue
        if not _matches_commitment_type(job.get("commitment_type", ""), config):
            reasons_dropped["commitment"] += 1
            continue
        filtered.append(job)

    removed = len(jobs) - len(filtered)
    if removed > 0:
        print(f"Filtered: {len(jobs):,} -> {len(filtered):,} jobs ({removed:,} removed)")
        for reason, count in reasons_dropped.items():
            if count > 0:
                print(f"  - {reason}: {count:,} removed")
    else:
        print(f"All {len(jobs):,} jobs passed filters")

    return filtered


def parse_jobs(input_path: str = None, output_path: str = None, config: dict = None) -> list:
    """Load raw jobs, normalize, deduplicate, filter against search criteria, and save."""
    if input_path is None:
        input_path = str(PROJECT_ROOT / ".tmp" / "raw_jobs.json")
    if output_path is None:
        output_path = str(PROJECT_ROOT / ".tmp" / "parsed_jobs.json")

    with open(input_path, "r", encoding="utf-8") as f:
        raw_jobs = json.load(f)

    print(f"Parsing {len(raw_jobs):,} raw jobs...")

    parsed = []
    seen_ids = set()
    for raw in raw_jobs:
        job = normalize_job(raw)
        # Skip empty / expired entries
        if not job["title"] and not job["description_text"]:
            continue
        if job["is_expired"]:
            continue
        # Deduplicate
        dedup_key = job["id"] or f"{job['title']}|{job['company']}"
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)
        parsed.append(job)

    # Enforce search filters
    if config:
        parsed = filter_jobs(parsed, config)

    # Sort by date_posted descending
    parsed.sort(key=lambda j: j.get("date_posted") or "", reverse=True)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2, ensure_ascii=False)

    print(f"Parsed {len(parsed):,} unique jobs -> {output_path}")
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Parse and normalize raw job data")
    parser.add_argument("--input", default=str(PROJECT_ROOT / ".tmp" / "raw_jobs.json"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / ".tmp" / "parsed_jobs.json"))
    parser.add_argument("--config", default=None, help="Path to search_filters.yaml for post-scrape filtering")
    args = parser.parse_args()

    config = None
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

    parse_jobs(args.input, args.output, config)


if __name__ == "__main__":
    main()
