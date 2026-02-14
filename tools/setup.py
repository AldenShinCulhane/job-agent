"""
Interactive setup wizard for the job search pipeline.
Prompts the user for profile info, search criteria, and API key,
then writes config/user_profile.yaml, config/search_filters.yaml, and .env.

Usage:
    uv run python tools/setup.py
"""

import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = PROJECT_ROOT / "config" / "user_profile.yaml"
FILTERS_PATH = PROJECT_ROOT / "config" / "search_filters.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

# Placeholder strings that should be treated as empty defaults
PLACEHOLDER_PATTERNS = [
    "brief professional summary",
    "background, expertise, and career goals",
    "your full name",
    "your.email@example.com",
]

# City lookup table for search location coordinates (~50 major cities)
CITY_COORDS = {
    # Canada
    "toronto, ontario": {"lat": "43.6532", "lon": "-79.3832", "country_code": "CA"},
    "vancouver, british columbia": {"lat": "49.2827", "lon": "-123.1207", "country_code": "CA"},
    "montreal, quebec": {"lat": "45.5017", "lon": "-73.5673", "country_code": "CA"},
    "calgary, alberta": {"lat": "51.0447", "lon": "-114.0719", "country_code": "CA"},
    "edmonton, alberta": {"lat": "53.5461", "lon": "-113.4938", "country_code": "CA"},
    "ottawa, ontario": {"lat": "45.4215", "lon": "-75.6972", "country_code": "CA"},
    "winnipeg, manitoba": {"lat": "49.8951", "lon": "-97.1384", "country_code": "CA"},
    "quebec city, quebec": {"lat": "46.8139", "lon": "-71.2080", "country_code": "CA"},
    "hamilton, ontario": {"lat": "43.2557", "lon": "-79.8711", "country_code": "CA"},
    "kitchener, ontario": {"lat": "43.4516", "lon": "-80.4925", "country_code": "CA"},
    "london, ontario": {"lat": "42.9849", "lon": "-81.2453", "country_code": "CA"},
    "victoria, british columbia": {"lat": "48.4284", "lon": "-123.3656", "country_code": "CA"},
    "halifax, nova scotia": {"lat": "44.6488", "lon": "-63.5752", "country_code": "CA"},
    "saskatoon, saskatchewan": {"lat": "52.1332", "lon": "-106.6700", "country_code": "CA"},
    "regina, saskatchewan": {"lat": "50.4452", "lon": "-104.6189", "country_code": "CA"},
    "waterloo, ontario": {"lat": "43.4643", "lon": "-80.5204", "country_code": "CA"},
    "mississauga, ontario": {"lat": "43.5890", "lon": "-79.6441", "country_code": "CA"},
    # US - Major cities
    "new york, new york": {"lat": "40.7128", "lon": "-74.0060", "country_code": "US"},
    "los angeles, california": {"lat": "34.0522", "lon": "-118.2437", "country_code": "US"},
    "chicago, illinois": {"lat": "41.8781", "lon": "-87.6298", "country_code": "US"},
    "houston, texas": {"lat": "29.7604", "lon": "-95.3698", "country_code": "US"},
    "phoenix, arizona": {"lat": "33.4484", "lon": "-112.0740", "country_code": "US"},
    "san antonio, texas": {"lat": "29.4241", "lon": "-98.4936", "country_code": "US"},
    "san diego, california": {"lat": "32.7157", "lon": "-117.1611", "country_code": "US"},
    "dallas, texas": {"lat": "32.7767", "lon": "-96.7970", "country_code": "US"},
    "austin, texas": {"lat": "30.2672", "lon": "-97.7431", "country_code": "US"},
    "san francisco, california": {"lat": "37.7749", "lon": "-122.4194", "country_code": "US"},
    "seattle, washington": {"lat": "47.6062", "lon": "-122.3321", "country_code": "US"},
    "denver, colorado": {"lat": "39.7392", "lon": "-104.9903", "country_code": "US"},
    "boston, massachusetts": {"lat": "42.3601", "lon": "-71.0589", "country_code": "US"},
    "atlanta, georgia": {"lat": "33.7490", "lon": "-84.3880", "country_code": "US"},
    "miami, florida": {"lat": "25.7617", "lon": "-80.1918", "country_code": "US"},
    "detroit, michigan": {"lat": "42.3314", "lon": "-83.0458", "country_code": "US"},
    "minneapolis, minnesota": {"lat": "44.9778", "lon": "-93.2650", "country_code": "US"},
    "portland, oregon": {"lat": "45.5152", "lon": "-122.6784", "country_code": "US"},
    "raleigh, north carolina": {"lat": "35.7796", "lon": "-78.6382", "country_code": "US"},
    "nashville, tennessee": {"lat": "36.1627", "lon": "-86.7816", "country_code": "US"},
    "salt lake city, utah": {"lat": "40.7608", "lon": "-111.8910", "country_code": "US"},
    "pittsburgh, pennsylvania": {"lat": "40.4406", "lon": "-79.9959", "country_code": "US"},
    "charlotte, north carolina": {"lat": "35.2271", "lon": "-80.8431", "country_code": "US"},
    "washington, district of columbia": {"lat": "38.9072", "lon": "-77.0369", "country_code": "US"},
    "philadelphia, pennsylvania": {"lat": "39.9526", "lon": "-75.1652", "country_code": "US"},
}

# Country-level fallbacks
COUNTRY_COORDS = {
    "CA": {"lat": "56.1304", "lon": "-106.3468", "formatted_address": "Canada"},
    "US": {"lat": "39.8283", "lon": "-98.5795", "formatted_address": "United States"},
}

# All Canadian provinces/territories for country detection in fallback
CANADIAN_INDICATORS = [
    "canada", "ontario", "quebec", "british columbia", "alberta",
    "manitoba", "saskatchewan", "nova scotia", "new brunswick",
    "newfoundland", "labrador", "prince edward island",
    "northwest territories", "nunavut", "yukon",
]


def _is_placeholder(text: str) -> bool:
    """Check if text matches a known placeholder pattern."""
    if not text:
        return False
    lower = text.lower().strip()
    return any(p in lower for p in PLACEHOLDER_PATTERNS)


def _lookup_city(location_str: str) -> dict | None:
    """Look up a location string in the city coordinates table. Returns coords dict or None."""
    loc_lower = location_str.lower().strip()
    # Exact match
    if loc_lower in CITY_COORDS:
        return CITY_COORDS[loc_lower]
    # Fuzzy match — find best match above 0.7 threshold
    best_match = None
    best_ratio = 0
    for city_key, coords in CITY_COORDS.items():
        ratio = SequenceMatcher(None, loc_lower, city_key).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = (city_key, coords)
    if best_match and best_ratio >= 0.7:
        return best_match[1]
    return None


def _validate_email(value: str) -> str | None:
    """Return error message if email is invalid, or None if OK."""
    if value and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
        return "Invalid email format. Example: name@example.com"
    return None


def _validate_date(value: str) -> str | None:
    """Return error message if date is not YYYY-MM or 'present', or None if OK."""
    if value and value.lower() != "present" and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", value):
        return "Use YYYY-MM format (e.g., 2023-06) or 'present'"
    return None


def _validate_url(value: str) -> str | None:
    """Return error message if URL doesn't start with http(s)://, or None if OK."""
    if value and not re.match(r"^https?://", value):
        return "URL must start with http:// or https://"
    return None


def prompt(label: str, default: str = "", required: bool = False, validate=None) -> str:
    """Prompt user with optional default value shown in brackets.

    Args:
        validate: Optional callable(str) -> str|None. Returns error message or None.
    """
    while True:
        if default:
            raw = input(f"  {label} [{default}]: ").strip()
            value = raw if raw else default
        else:
            raw = input(f"  {label}: ").strip()
            if not raw and required:
                print("    This field is required.")
                continue
            value = raw

        if validate and value:
            error = validate(value)
            if error:
                print(f"    {error}")
                continue

        return value


def prompt_int(label: str, default: int = 0, min_val: int = None, max_val: int = None) -> int:
    """Prompt for an integer value with optional range validation."""
    while True:
        raw = prompt(label, str(default) if default else "")
        if not raw:
            return default
        try:
            val = int(raw)
        except ValueError:
            print("    Please enter a number.")
            continue
        if min_val is not None and val < min_val:
            print(f"    Must be at least {min_val}.")
            continue
        if max_val is not None and val > max_val:
            print(f"    Must be at most {max_val}.")
            continue
        return val


def prompt_float(label: str, default: float = 0, min_val: float = None, max_val: float = None) -> float:
    """Prompt for a float value with optional range validation."""
    while True:
        raw = prompt(label, str(int(default)) if default else "")
        if not raw:
            return default
        try:
            val = float(raw)
        except ValueError:
            print("    Please enter a number.")
            continue
        if min_val is not None and val < min_val:
            print(f"    Must be at least {min_val}.")
            continue
        if max_val is not None and val > max_val:
            print(f"    Must be at most {max_val}.")
            continue
        return val


def prompt_list(label: str, default: list = None) -> list:
    """Prompt for comma-separated values, returns a list."""
    default = default or []
    default_str = ", ".join(str(d) for d in default) if default else ""
    raw = prompt(f"{label} (comma-separated)", default_str)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def prompt_yes_no(label: str, default: bool = False) -> bool:
    """Prompt for yes/no."""
    default_str = "Y/n" if default else "y/N"
    raw = input(f"  {label} [{default_str}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def load_existing_profile() -> dict:
    """Load existing profile for defaults, or return empty dict."""
    if PROFILE_PATH.exists():
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_existing_filters() -> dict:
    """Load existing search filters for defaults, or return empty dict."""
    if FILTERS_PATH.exists():
        with open(FILTERS_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}



def collect_personal_info(existing: dict) -> dict:
    """Collect personal information."""
    print("\n--- Personal Information ---")
    personal = existing.get("personal", {})
    return {
        "name": prompt("Full name", personal.get("name", ""), required=True),
        "email": prompt("Email", personal.get("email", ""), required=True, validate=_validate_email),
        "phone": prompt("Phone", personal.get("phone", "")),
        "location": prompt("Location (City, Province/State)", personal.get("location", "")),
        "linkedin": prompt("LinkedIn URL", personal.get("linkedin", ""), validate=_validate_url),
        "github": prompt("GitHub URL (optional)", personal.get("github", ""), validate=_validate_url),
        "website": prompt("Website (optional)", personal.get("website", ""), validate=_validate_url),
    }


def collect_summary(existing: dict) -> str:
    """Collect professional summary."""
    print("\n--- Professional Summary ---")
    default = existing.get("summary", "").strip()
    # Don't show placeholder text as a default
    if _is_placeholder(default):
        default = ""
    if default:
        print(f"  Current: {default[:80]}{'...' if len(default) > 80 else ''}")
        if not prompt_yes_no("Update summary?"):
            return default
    raw = prompt("Professional summary (2-3 sentences)", default)
    return raw if raw else default


def collect_skills(existing: dict) -> dict:
    """Collect skills grouped by category."""
    print("\n--- Skills ---")
    skills = existing.get("skills", {})
    return {
        "programming_languages": prompt_list(
            "Programming languages",
            skills.get("programming_languages", [])
        ),
        "frameworks": prompt_list(
            "Frameworks & libraries",
            skills.get("frameworks", [])
        ),
        "tools": prompt_list(
            "Tools & platforms",
            skills.get("tools", [])
        ),
        "soft_skills": prompt_list(
            "Soft skills",
            skills.get("soft_skills", [])
        ),
    }


def collect_experience(existing: dict) -> dict:
    """Collect work experience."""
    print("\n--- Work Experience ---")
    exp = existing.get("experience", {})
    total_years = prompt_int("Total years of experience", exp.get("total_years", 0), min_val=0, max_val=50)

    existing_positions = exp.get("positions", [])
    if existing_positions:
        print(f"\n  You have {len(existing_positions)} position(s) on file:")
        for i, pos in enumerate(existing_positions):
            print(f"    {i + 1}. {pos.get('title', '?')} at {pos.get('company', '?')} ({pos.get('start_date', '?')} - {pos.get('end_date', '?')})")
        if not prompt_yes_no("Re-enter positions?"):
            return {"total_years": total_years, "positions": existing_positions}

    positions = []
    print("\n  Enter your positions (press Enter on title to stop):")
    while True:
        idx = len(positions) + 1
        print(f"\n  Position {idx}:")
        title = prompt("Job title (or Enter to finish)")
        if not title:
            break
        company = prompt("Company", required=True)
        start_date = prompt("Start date (YYYY-MM)", required=True, validate=_validate_date)
        end_date = prompt("End date (YYYY-MM or 'present')", "present", validate=_validate_date)
        print("    Enter highlights/achievements (one per line, empty line to finish):")
        highlights = []
        while True:
            h = input("      - ").strip()
            if not h:
                break
            highlights.append(h)
        positions.append({
            "title": title,
            "company": company,
            "start_date": start_date,
            "end_date": end_date,
            "highlights": highlights,
        })

    return {"total_years": total_years, "positions": positions}


def collect_education(existing: dict) -> list:
    """Collect education."""
    print("\n--- Education ---")
    existing_edu = existing.get("education", [])
    if existing_edu:
        print(f"  You have {len(existing_edu)} education entry(ies) on file:")
        for edu in existing_edu:
            print(f"    - {edu.get('degree', '?')} in {edu.get('field', '?')} from {edu.get('institution', '?')}")
        if not prompt_yes_no("Re-enter education?"):
            return existing_edu

    education = []
    print("\n  Enter your education (press Enter on degree to stop):")
    while True:
        degree = prompt("Degree (e.g., Bachelor of Science) (or Enter to finish)")
        if not degree:
            break
        field = prompt("Field of study", required=True)
        institution = prompt("Institution", required=True)
        year = prompt_int("Graduation year", min_val=1950, max_val=2035)
        entry = {
            "degree": degree,
            "field": field,
            "institution": institution,
            "graduation_year": year,
        }
        education.append(entry)

    return education


def collect_projects(existing: dict) -> list:
    """Collect personal/academic projects with dates and bullet-point highlights."""
    print("\n--- Projects ---")
    existing_projects = existing.get("projects", [])
    if existing_projects:
        print(f"  You have {len(existing_projects)} project(s) on file:")
        for proj in existing_projects:
            dates = f" ({proj.get('start_date', '?')} - {proj.get('end_date', '?')})"
            print(f"    - {proj.get('name', '?')}{dates}")
        if not prompt_yes_no("Re-enter projects?"):
            return existing_projects

    projects = []
    print("\n  Enter your projects (press Enter on name to stop):")
    while True:
        idx = len(projects) + 1
        print(f"\n  Project {idx}:")
        name = prompt("Project name (or Enter to finish)")
        if not name:
            break
        start_date = prompt("Start date (YYYY-MM)", required=True, validate=_validate_date)
        end_date = prompt("End date (YYYY-MM or 'present')", "present", validate=_validate_date)
        technologies = prompt_list("Technologies used", [])
        url = prompt("URL (optional)", validate=_validate_url)
        print("    Enter highlights (one per line, empty line to finish):")
        highlights = []
        while True:
            h = input("      - ").strip()
            if not h:
                if not highlights:
                    print("      At least one highlight is required.")
                    continue
                break
            highlights.append(h)
        projects.append({
            "name": name,
            "start_date": start_date,
            "end_date": end_date,
            "technologies": technologies,
            "url": url if url else "",
            "highlights": highlights,
        })

    return projects



def collect_search_filters(existing: dict) -> dict:
    """Collect search filter preferences."""
    print("\n--- Search Filters ---")
    search = existing.get("search", {})

    query = prompt("Search query (job title)", search.get("query", "software engineer"))
    tech_keywords = prompt("Technology keywords (optional)", search.get("technology_keywords", ""))

    # Search locations — with city coordinate lookup
    print("\n  Where are you searching for jobs?")
    existing_locations = existing.get("locations", [])
    existing_loc_names = [loc.get("formatted_address", "") for loc in existing_locations]
    loc_input = prompt_list(
        "Search locations (City, Province/State)",
        existing_loc_names if existing_loc_names != ["United States"] else []
    )

    # Build locations for search config.
    # hiring.cafe API works best with country-level coordinates.
    # City-level coordinates cause 500 errors, so we always use
    # country-level coords and let the post-scrape filter enforce city matching.
    locations = []
    for loc_str in loc_input:
        coords = _lookup_city(loc_str)
        loc_lower = loc_str.lower()
        if coords:
            country_code = coords["country_code"]
        elif any(p in loc_lower for p in CANADIAN_INDICATORS):
            country_code = "CA"
        else:
            country_code = "US"
        # Always use country-level coords for the API query
        country_fallback = COUNTRY_COORDS.get(country_code, COUNTRY_COORDS["US"])
        locations.append({
            "formatted_address": loc_str,
            "types": ["country"],
            "geometry": {"lat": country_fallback["lat"], "lon": country_fallback["lon"]},
            "id": f"user_{loc_str.lower().replace(' ', '_').replace(',', '')}",
            "country_code": country_code,
        })

    if not locations:
        # Default to Canada if no locations provided
        locations = [{
            "formatted_address": "Canada",
            "types": ["country"],
            "geometry": {"lat": "56.1304", "lon": "-106.3468"},
            "id": "user_country",
            "country_code": "CA",
        }]

    print("\n  Workplace types (enter numbers, comma-separated):")
    wp_options = ["Remote", "Hybrid", "Onsite"]
    existing_wp = existing.get("workplace_types", ["Remote"])
    for i, opt in enumerate(wp_options):
        marker = "*" if opt in existing_wp else " "
        print(f"    {i + 1}. [{marker}] {opt}")
    raw = prompt("Select", ", ".join(str(wp_options.index(w) + 1) for w in existing_wp if w in wp_options))
    workplace_types = []
    invalid_nums = []
    for num in raw.split(","):
        num = num.strip()
        if num.isdigit() and 1 <= int(num) <= len(wp_options):
            choice = wp_options[int(num) - 1]
            if choice not in workplace_types:
                workplace_types.append(choice)
        elif num:
            invalid_nums.append(num)
    if invalid_nums:
        print(f"    Ignored invalid selection(s): {', '.join(invalid_nums)} (valid: 1-{len(wp_options)})")
    if not workplace_types:
        workplace_types = existing_wp

    print("\n  Experience levels (enter numbers, comma-separated):")
    exp_options = [
        "No Prior Experience Required", "Entry Level", "Mid Level",
        "Senior Level", "Lead", "Manager", "Director", "VP", "C-Suite"
    ]
    existing_exp = existing.get("experience_levels", ["Entry Level", "Mid Level"])
    for i, opt in enumerate(exp_options):
        marker = "*" if opt in existing_exp else " "
        print(f"    {i + 1}. [{marker}] {opt}")
    raw = prompt("Select", ", ".join(str(exp_options.index(e) + 1) for e in existing_exp if e in exp_options))
    experience_levels = []
    invalid_nums = []
    for num in raw.split(","):
        num = num.strip()
        if num.isdigit() and 1 <= int(num) <= len(exp_options):
            choice = exp_options[int(num) - 1]
            if choice not in experience_levels:
                experience_levels.append(choice)
        elif num:
            invalid_nums.append(num)
    if invalid_nums:
        print(f"    Ignored invalid selection(s): {', '.join(invalid_nums)} (valid: 1-{len(exp_options)})")
    if not experience_levels:
        experience_levels = existing_exp

    date_days = prompt_int(
        "Jobs posted within last N days",
        existing.get("date_filter", {}).get("days", 30),
        min_val=1, max_val=365,
    )

    salary_cfg = existing.get("salary", {})
    min_annual_filter = prompt_float(
        "Minimum salary filter for search (0 = no filter)",
        salary_cfg.get("min_annual", 0),
        min_val=0, max_val=2000000,
    )
    only_transparent = prompt_yes_no(
        "Only show jobs with disclosed salaries?",
        salary_cfg.get("only_transparent", False)
    )

    return {
        "search": {
            "query": query,
            "technology_keywords": tech_keywords,
        },
        "locations": locations,
        "workplace_types": workplace_types,
        "experience_levels": experience_levels,
        "commitment_types": existing.get("commitment_types", ["Full Time"]),
        "salary": {
            "min_annual": int(min_annual_filter) if min_annual_filter else None,
            "currency": salary_cfg.get("currency", "USD"),
            "only_transparent": only_transparent,
        },
        "company_filters": existing.get("company_filters", {
            "size_ranges": [],
            "include_companies": [],
            "exclude_companies": [],
        }),
        "date_filter": {"days": date_days},
        "sorting": existing.get("sorting", {"sort_by": "default"}),
        "pagination": existing.get("pagination", {"page_size": 40, "max_pages": 25}),
    }


def check_llm_setup() -> bool:
    """Check if an LLM API key is configured (Gemini preferred, Groq as fallback).

    Prompts the user to enter their key if not found in .env.
    Returns True if a valid key is configured.
    """
    print("\n--- LLM API Key ---")

    existing_gemini = os.getenv("GEMINI_API_KEY", "")
    existing_groq = os.getenv("GROQ_API_KEY", "")

    if existing_gemini:
        masked = existing_gemini[:8] + "..." + existing_gemini[-4:]
        print(f"  Gemini API key found: {masked}")
        return True
    if existing_groq:
        masked = existing_groq[:8] + "..." + existing_groq[-4:]
        print(f"  Groq API key found: {masked}")
        return True

    print("  LLM features use Google Gemini (free, fast, generous limits).")
    print("  Get a free API key at: https://aistudio.google.com/apikeys")
    print("  (Alternative: Groq at https://console.groq.com)")

    key = input("  Enter your Gemini API key (or press Enter to skip): ").strip()
    if key:
        _save_env_var("GEMINI_API_KEY", key)
        print("  Saved GEMINI_API_KEY to .env")
        return True

    print("  Skipping — the pipeline will still scrape, filter, score, and generate reports.")
    print("  Add GEMINI_API_KEY to .env later to enable tailored resume/cover letter generation.")
    return False


def _save_env_var(key: str, value: str):
    """Update or add a variable in .env, preserving other content."""
    lines = []
    found = False
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def write_profile(profile: dict):
    """Write user profile YAML."""
    os.makedirs(PROFILE_PATH.parent, exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write("# ============================================\n")
        f.write("# User Profile Configuration\n")
        f.write("# ============================================\n")
        f.write("# Generated by setup wizard. Edit freely.\n\n")
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"\n  Saved: {PROFILE_PATH}")


def write_filters(filters: dict):
    """Write search filters YAML."""
    os.makedirs(FILTERS_PATH.parent, exist_ok=True)
    with open(FILTERS_PATH, "w", encoding="utf-8") as f:
        f.write("# ============================================\n")
        f.write("# Job Search Filters Configuration\n")
        f.write("# ============================================\n")
        f.write("# Generated by setup wizard. Edit freely.\n\n")
        yaml.dump(filters, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"  Saved: {FILTERS_PATH}")


def write_env():
    """Ensure .env exists. API key is saved inline by check_llm_setup()."""
    if not ENV_PATH.exists():
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("# Get a free API key at https://aistudio.google.com/apikeys\n")
            f.write("# GEMINI_API_KEY=AIza...\n")
        print(f"  Created: {ENV_PATH}")
    else:
        print(f"  Exists: {ENV_PATH}")


def interactive_setup():
    """Run the full interactive setup wizard."""
    print("=" * 55)
    print("  Job Search — Setup Wizard")
    print("=" * 55)
    print("\nPress Enter to keep the default value shown in [brackets].")

    existing_profile = load_existing_profile()
    existing_filters = load_existing_filters()

    if existing_profile:
        name = existing_profile.get("personal", {}).get("name", "")
        if name and name != "Your Full Name":
            print(f"\nExisting profile found for: {name}")
            if not prompt_yes_no("Update your profile?", default=True):
                print("Keeping existing profile.")
                # Still offer to update search filters and check Groq
                filters = collect_search_filters(existing_filters)
                llm_configured = check_llm_setup()
                write_filters(filters)
                write_env()
                print("\nSetup complete!")
                return

    personal = collect_personal_info(existing_profile)
    summary = collect_summary(existing_profile)
    skills = collect_skills(existing_profile)
    experience = collect_experience(existing_profile)
    education = collect_education(existing_profile)
    projects = collect_projects(existing_profile)
    filters = collect_search_filters(existing_filters)
    llm_configured = check_llm_setup()

    profile = {
        "personal": personal,
        "summary": summary,
        "skills": skills,
        "experience": experience,
        "education": education,
        "projects": projects,
    }

    # Review & edit loop
    sections = {
        "1": ("Personal info", lambda: collect_personal_info(existing_profile), "personal"),
        "2": ("Summary", lambda: collect_summary(existing_profile), "summary"),
        "3": ("Skills", lambda: collect_skills(existing_profile), "skills"),
        "4": ("Experience", lambda: collect_experience(existing_profile), "experience"),
        "5": ("Education", lambda: collect_education(existing_profile), "education"),
        "6": ("Projects", lambda: collect_projects(existing_profile), "projects"),
        "7": ("Search filters", None, None),
        "8": ("Groq API key", None, None),
    }

    while True:
        print("\n" + "=" * 55)
        print("  Review Your Configuration")
        print("=" * 55)

        print(f"\n  [1] Personal:    {personal.get('name', '')} — {personal.get('email', '')}")
        print(f"  [2] Summary:     {(summary or '')[:60]}{'...' if len(summary or '') > 60 else ''}")
        all_skills = []
        for cat_skills in skills.values():
            if isinstance(cat_skills, list):
                all_skills.extend(cat_skills)
        print(f"  [3] Skills:      {', '.join(all_skills[:6])}{'...' if len(all_skills) > 6 else ''}")
        positions = experience.get("positions", [])
        print(f"  [4] Experience:  {experience.get('total_years', 0)} years, {len(positions)} position(s)")
        print(f"  [5] Education:   {len(education)} entry(ies)")
        print(f"  [6] Projects:    {len(projects)} project(s)")
        search_locs = ', '.join(loc.get('formatted_address', '') for loc in filters.get('locations', []))
        print(f"  [7] Search:      \"{filters.get('search', {}).get('query', '')}\" in {search_locs}")
        llm_display = "Configured" if llm_configured else "No API key"
        print(f"  [8] LLM:         {llm_display}")

        print(f"\n  Enter a number (1-8) to edit that section, or press Enter to save.")
        choice = input("  Edit section: ").strip()

        if not choice:
            break

        if choice in sections and choice not in ("7", "8"):
            label, collector, key = sections[choice]
            result = collector()
            if key == "personal":
                personal = result
                profile["personal"] = result
            elif key == "summary":
                summary = result
                profile["summary"] = result
            elif key == "skills":
                skills = result
                profile["skills"] = result
            elif key == "experience":
                experience = result
                profile["experience"] = result
            elif key == "education":
                education = result
                profile["education"] = result
            elif key == "projects":
                projects = result
                profile["projects"] = result
        elif choice == "7":
            filters = collect_search_filters(existing_filters)
        elif choice == "8":
            llm_configured = check_llm_setup()
        else:
            print("  Invalid choice. Enter 1-8 or press Enter to save.")

    print("\n--- Saving Configuration ---")
    write_profile(profile)
    write_filters(filters)
    write_env()

    print("\n" + "=" * 55)
    print("  Setup complete!")
    print("=" * 55)
    print("\nNext steps:")
    print("  1. Run the pipeline:")
    print("     uv run python tools/run_pipeline.py")
    print("  2. (Optional) Provide your resume for better results:")
    print("     uv run python tools/run_pipeline.py --resume path/to/resume.pdf")
    print("\n  Tip: Re-run this wizard anytime to update your config.")
    print("  You can also edit the YAML files directly in config/")


def main():
    interactive_setup()


if __name__ == "__main__":
    main()
