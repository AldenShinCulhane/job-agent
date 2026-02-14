"""
Dual-strategy job scraper for hiring.cafe.
Primary: Direct HTTP GET to API endpoints with base64-encoded searchState.
Fallback: Playwright headless browser with stealth plugin.

API format (discovered via browser interception):
  GET /api/search-jobs?s=<base64(urlquote(JSON))>&size=40&page=0
  GET /api/search-jobs/get-total-count?s=<base64(urlquote(JSON))>
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

HIRING_CAFE_BASE = "https://hiring.cafe"
COUNT_ENDPOINT = f"{HIRING_CAFE_BASE}/api/search-jobs/get-total-count"
JOBS_ENDPOINT = f"{HIRING_CAFE_BASE}/api/search-jobs"

# Hardcoded to Windows/Chrome — the most common browser fingerprint globally.
# This minimizes anti-bot detection regardless of which OS runs the scraper.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://hiring.cafe/",
    "Origin": "https://hiring.cafe",
    "sec-ch-ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Default search_state — fields the user doesn't set get these defaults
DEFAULT_SEARCH_STATE = {
    "locations": [],
    "workplaceTypes": ["Remote", "Hybrid", "Onsite"],
    "defaultToUserLocation": False,
    "userLocation": None,
    "physicalEnvironments": ["Office", "Outdoor", "Vehicle", "Industrial", "Customer-Facing"],
    "physicalLaborIntensity": ["Low", "Medium", "High"],
    "physicalPositions": ["Sitting", "Standing"],
    "oralCommunicationLevels": ["Low", "Medium", "High"],
    "computerUsageLevels": ["Low", "Medium", "High"],
    "cognitiveDemandLevels": ["Low", "Medium", "High"],
    "currency": {"label": "Any", "value": None},
    "frequency": {"label": "Any", "value": None},
    "minCompensationLowEnd": None,
    "minCompensationHighEnd": None,
    "maxCompensationLowEnd": None,
    "maxCompensationHighEnd": None,
    "restrictJobsToTransparentSalaries": False,
    "calcFrequency": "Yearly",
    "commitmentTypes": ["Full Time"],
    "jobTitleQuery": "",
    "jobDescriptionQuery": "",
    "associatesDegreeFieldsOfStudy": [],
    "excludedAssociatesDegreeFieldsOfStudy": [],
    "bachelorsDegreeFieldsOfStudy": [],
    "excludedBachelorsDegreeFieldsOfStudy": [],
    "mastersDegreeFieldsOfStudy": [],
    "excludedMastersDegreeFieldsOfStudy": [],
    "doctorateDegreeFieldsOfStudy": [],
    "excludedDoctorateDegreeFieldsOfStudy": [],
    "associatesDegreeRequirements": [],
    "bachelorsDegreeRequirements": [],
    "mastersDegreeRequirements": [],
    "doctorateDegreeRequirements": [],
    "licensesAndCertifications": [],
    "excludedLicensesAndCertifications": [],
    "excludeAllLicensesAndCertifications": False,
    "seniorityLevel": ["Entry Level", "Mid Level"],
    "roleTypes": ["Individual Contributor", "People Manager"],
    "roleYoeRange": [0, 20],
    "excludeIfRoleYoeIsNotSpecified": False,
    "managementYoeRange": [0, 20],
    "excludeIfManagementYoeIsNotSpecified": False,
    "securityClearances": ["None", "Confidential", "Secret", "Top Secret", "Top Secret/SCI", "Public Trust", "Interim Clearances", "Other"],
    "languageRequirements": [],
    "excludedLanguageRequirements": [],
    "languageRequirementsOperator": "OR",
    "excludeJobsWithAdditionalLanguageRequirements": False,
    "airTravelRequirement": ["None", "Minimal", "Moderate", "Extensive"],
    "landTravelRequirement": ["None", "Minimal", "Moderate", "Extensive"],
    "morningShiftWork": [],
    "eveningShiftWork": [],
    "overnightShiftWork": [],
    "weekendAvailabilityRequired": "Doesn't Matter",
    "holidayAvailabilityRequired": "Doesn't Matter",
    "overtimeRequired": "Doesn't Matter",
    "onCallRequirements": ["None", "Occasional (once a month or less)", "Regular (once a week or more)"],
    "benefitsAndPerks": [],
    "applicationFormEase": [],
    "companyNames": [],
    "excludedCompanyNames": [],
    "usaGovPref": None,
    "industries": [],
    "excludedIndustries": [],
    "companyKeywords": [],
    "companyKeywordsBooleanOperator": "OR",
    "excludedCompanyKeywords": [],
    "hideJobTypes": [],
    "encouragedToApply": [],
    "searchQuery": "",
    "dateFetchedPastNDays": 30,
    "hiddenCompanies": [],
    "user": None,
    "searchModeSelectedCompany": None,
    "departments": [],
    "restrictedSearchAttributes": [],
    "sortBy": "default",
    "technologyKeywordsQuery": "",
    "requirementsKeywordsQuery": "",
    "companyPublicOrPrivate": "all",
    "latestInvestmentYearRange": [None, None],
    "latestInvestmentSeries": [],
    "latestInvestmentAmount": None,
    "latestInvestmentCurrency": [],
    "investors": [],
    "excludedInvestors": [],
    "isNonProfit": "all",
    "companySizeRanges": [],
    "minYearFounded": None,
    "maxYearFounded": None,
    "excludedLatestInvestmentSeries": [],
}


def encode_search_state(search_state: dict) -> str:
    """Encode searchState as base64(urlquote(JSON)) for the API's ?s= parameter."""
    json_str = json.dumps(search_state, separators=(",", ":"))
    url_encoded = urllib.parse.quote(json_str)
    b64_encoded = base64.b64encode(url_encoded.encode("utf-8")).decode("utf-8")
    return b64_encoded


def load_search_config(config_path: str) -> dict:
    """Load YAML search config file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_search_state(config: dict) -> dict:
    """Convert user-friendly YAML config into hiring.cafe's searchState format."""
    state = dict(DEFAULT_SEARCH_STATE)

    # Search query
    search = config.get("search", {})
    state["searchQuery"] = search.get("query", "")
    state["technologyKeywordsQuery"] = search.get("technology_keywords", "")

    # Locations
    locations_cfg = config.get("locations", [])
    api_locations = []
    for loc in locations_cfg:
        api_loc = {
            "formatted_address": loc.get("formatted_address", ""),
            "types": loc.get("types", []),
            "geometry": {
                "location": {
                    "lat": str(loc.get("geometry", {}).get("lat", "")),
                    "lon": str(loc.get("geometry", {}).get("lon", "")),
                }
            },
            "id": loc.get("id", "user_location"),
            "address_components": [
                {
                    "long_name": loc.get("formatted_address", ""),
                    "short_name": loc.get("country_code", ""),
                    "types": loc.get("types", []),
                }
            ],
            "options": {
                "flexible_regions": ["anywhere_in_continent", "anywhere_in_world"]
            },
        }
        api_locations.append(api_loc)
    if api_locations:
        state["locations"] = api_locations

    # Workplace types
    workplace = config.get("workplace_types", [])
    if workplace:
        state["workplaceTypes"] = workplace

    # Experience levels
    experience = config.get("experience_levels", [])
    if experience:
        state["seniorityLevel"] = experience

    # Commitment types
    commitment = config.get("commitment_types", [])
    if commitment:
        state["commitmentTypes"] = commitment

    # Company filters
    company = config.get("company_filters", {})
    if company.get("size_ranges"):
        state["companySizeRanges"] = company["size_ranges"]
    if company.get("include_companies"):
        state["companyNames"] = company["include_companies"]
    if company.get("exclude_companies"):
        state["excludedCompanyNames"] = company["exclude_companies"]
    if company.get("industries"):
        state["industries"] = company["industries"]
    if company.get("exclude_industries"):
        state["excludedIndustries"] = company["exclude_industries"]

    # Salary filters
    salary_cfg = config.get("salary", {})
    min_salary = salary_cfg.get("min_annual")
    if min_salary:
        state["minCompensationLowEnd"] = min_salary
        state["calcFrequency"] = "Yearly"
    currency = salary_cfg.get("currency")
    if currency:
        state["currency"] = {"label": currency, "value": currency}
    if salary_cfg.get("only_transparent", False):
        state["restrictJobsToTransparentSalaries"] = True

    # Date filter
    date_filter = config.get("date_filter", {})
    state["dateFetchedPastNDays"] = date_filter.get("days", 30)

    # Sorting
    sorting = config.get("sorting", {})
    state["sortBy"] = sorting.get("sort_by", "default")

    return state


def extract_jobs_from_response(data) -> list:
    """Extract job list from API response."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Primary format: {"results": [...]}
        if "results" in data and isinstance(data["results"], list):
            return data["results"]
        for key in ("jobs", "data", "items", "content"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # Elasticsearch-style
        if "hits" in data:
            hits = data["hits"]
            if isinstance(hits, dict) and "hits" in hits:
                return [hit.get("_source", hit) for hit in hits["hits"]]
    return []


def scrape_via_api(search_state: dict, page_size: int = 40, max_pages: int = 25) -> list:
    """Direct HTTP GET to hiring.cafe API. Returns list of raw job dicts."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    encoded_state = encode_search_state(search_state)

    # Get total count
    print("  Querying total job count...")
    try:
        count_resp = session.get(
            COUNT_ENDPOINT,
            params={"s": encoded_state},
            timeout=30,
        )
        if count_resp.status_code == 200:
            count_data = count_resp.json()
            total = count_data.get("total", 0)
            print(f"  Total jobs matching filters: {total:,}")
        else:
            print(f"  Count endpoint returned {count_resp.status_code} — proceeding anyway")
            total = 0
    except Exception as e:
        print(f"  Count request failed ({e}) — proceeding anyway")
        total = 0

    # Paginate through results
    all_jobs = []
    for page in range(max_pages):
        print(f"  Fetching page {page + 1} (size={page_size})...")

        try:
            resp = session.get(
                JOBS_ENDPOINT,
                params={"s": encoded_state, "size": page_size, "page": page},
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            print(f"  Request failed on page {page + 1}: {e}")
            break

        if resp.status_code == 429:
            print("  Rate limited (429). Stopping API method.")
            raise RateLimitError("hiring.cafe returned 429")

        if resp.status_code != 200:
            print(f"  API returned {resp.status_code}. Response: {resp.text[:200]}")
            # If we got a non-JSON challenge page, signal for browser fallback
            if "challenge" in resp.text.lower() or "<html" in resp.text[:100].lower():
                raise RateLimitError("Got challenge page instead of JSON")
            break

        try:
            data = resp.json()
        except ValueError:
            print("  Non-JSON response. Likely a Vercel challenge page.")
            raise RateLimitError("Non-JSON response — likely blocked")

        batch = extract_jobs_from_response(data)
        if not batch:
            print("  No jobs in response. End of results.")
            break

        all_jobs.extend(batch)
        print(f"  Got {len(batch)} jobs (total so far: {len(all_jobs):,})")

        if len(batch) < page_size:
            break
        if total > 0 and len(all_jobs) >= total:
            break

        time.sleep(1)  # polite delay between pages

    return all_jobs


class RateLimitError(Exception):
    pass


def scrape_via_browser(search_state: dict, page_size: int = 40, max_pages: int = 25) -> list:
    """Playwright headless browser fallback. Intercepts API responses."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        print("  ERROR: playwright/playwright-stealth not installed.")
        print("  Run: uv add playwright playwright-stealth && uv run playwright install chromium")
        return []

    search_state_json = json.dumps(search_state)
    url = f"{HIRING_CAFE_BASE}/?searchState={urllib.parse.quote(search_state_json)}"

    all_jobs = []
    stealth = Stealth()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=BROWSER_HEADERS["User-Agent"],
        )
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        captured_responses = []

        def handle_response(response):
            if "/api/search-jobs" in response.url and "get-total-count" not in response.url:
                try:
                    body = response.json()
                    captured_responses.append(body)
                except (json.JSONDecodeError, ValueError):
                    pass  # Non-JSON response (e.g. HTML error page)

        page.on("response", handle_response)

        print("  Navigating to hiring.cafe (browser mode)...")
        print("  Bypassing security check — this may take 30-60 seconds...")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            print("  Page loaded. Waiting for data...")
        except Exception as e:
            print(f"  Navigation timeout/error: {e}")
            print("  Waiting additional 15s for security challenge to resolve...")
            try:
                page.wait_for_timeout(15000)
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception as retry_err:
                print(f"  Challenge did not resolve: {retry_err}")

        print("  Waiting for API responses...")
        page.wait_for_timeout(5000)

        # Extract from captured responses
        for resp_data in captured_responses:
            batch = extract_jobs_from_response(resp_data)
            all_jobs.extend(batch)

        print(f"  Browser captured {len(all_jobs)} jobs from initial load")

        # Paginate by fetching additional pages from within the browser context
        if len(all_jobs) >= page_size and max_pages > 1:
            encoded_state = encode_search_state(search_state)
            for page_num in range(1, max_pages):
                print(f"  Browser: fetching page {page_num + 1}...")
                fetch_url = f"{JOBS_ENDPOINT}?s={encoded_state}&size={page_size}&page={page_num}"
                try:
                    result = page.evaluate(
                        f"""async () => {{
                            const resp = await fetch("{fetch_url}");
                            return await resp.json();
                        }}"""
                    )
                    batch = extract_jobs_from_response(result)
                    if not batch:
                        break
                    all_jobs.extend(batch)
                    print(f"  Got {len(batch)} jobs (total: {len(all_jobs):,})")
                    if len(batch) < page_size:
                        break
                except Exception as e:
                    print(f"  Browser fetch failed on page {page_num + 1}: {e}")
                    break
                page.wait_for_timeout(2000)

        browser.close()

    return all_jobs


def scrape_jobs(config_path: str, method: str = "auto", output_path: str = None) -> list:
    """
    Main entry point. Scrape jobs based on config file.

    Args:
        config_path: Path to search_filters.yaml
        method: 'api', 'browser', or 'auto' (try API first, fallback to browser)
        output_path: Where to save raw JSON (default: .tmp/raw_jobs.json)
    Returns:
        List of raw job dicts
    """
    config = load_search_config(config_path)
    search_state = build_search_state(config)

    pagination = config.get("pagination", {})
    page_size = pagination.get("page_size", 40)
    max_pages = pagination.get("max_pages", 25)

    if output_path is None:
        output_path = str(PROJECT_ROOT / ".tmp" / "raw_jobs.json")

    print(f"Search query: \"{search_state['searchQuery']}\"")
    print(f"Locations: {[l['formatted_address'] for l in search_state.get('locations', [])]}")
    print(f"Workplace: {search_state.get('workplaceTypes', [])}")
    print(f"Experience: {search_state.get('seniorityLevel', [])}")
    print(f"Method: {method}\n")

    jobs = []

    if method in ("api", "auto"):
        print("[1/2] Trying direct API...")
        try:
            jobs = scrape_via_api(search_state, page_size, max_pages)
        except RateLimitError as e:
            print(f"  API blocked: {e}")
            if method == "auto":
                print("  Falling back to browser method...\n")
            else:
                print("  API method failed. Try --method browser or --method auto")

    if not jobs and method in ("browser", "auto"):
        print("[2/2] Using browser method...")
        jobs = scrape_via_browser(search_state, page_size, max_pages)

    if jobs:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)
        # Save scrape metadata for cache validation
        config_hash = _hash_config(config_path)
        metadata = {
            "config_hash": config_hash,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "job_count": len(jobs),
        }
        metadata_path = str(Path(output_path).parent / "scrape_metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        print(f"\nScraped {len(jobs):,} jobs -> {output_path}")
    else:
        print("\nNo jobs scraped. Check your filters or try a different method.")

    return jobs


def _hash_config(config_path: str) -> str:
    """SHA-256 hash of the search config file contents."""
    with open(config_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Scrape jobs from hiring.cafe")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "search_filters.yaml"),
                        help="Path to search filters YAML")
    parser.add_argument("--output", default=str(PROJECT_ROOT / ".tmp" / "raw_jobs.json"),
                        help="Output path for raw JSON")
    parser.add_argument("--method", choices=["api", "browser", "auto"], default="auto",
                        help="Scraping method (default: auto)")
    args = parser.parse_args()

    scrape_jobs(args.config, args.method, args.output)


if __name__ == "__main__":
    main()
