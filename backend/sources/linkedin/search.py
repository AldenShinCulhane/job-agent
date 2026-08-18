"""LinkedIn job search: URL construction, result parsing, and the
resumable collect loop.

Parsing operates on raw HTML strings (BeautifulSoup) so the exact same
code path is exercised by the sanitized fixtures in tests — the browser
side only navigates, scrolls, and hands back `page.content()`.
"""

import logging
import re
from urllib.parse import urlencode

from backend.core import db
from backend.core.events import broker
from backend.sources.linkedin import pacing, selectors
from backend.sources.linkedin.supervisor import AuthRequired, CaptchaStop

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs/search/"
PAGE_SIZE = 25
DEFAULT_RESULT_CAP = 100

# UI filter values -> LinkedIn query params.
DATE_POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000"}
WORKPLACE = {"onsite": "1", "remote": "2", "hybrid": "3"}
EXPERIENCE = {
    "internship": "1", "entry": "2", "associate": "3",
    "mid_senior": "4", "director": "5", "executive": "6",
}

_JOB_VIEW_RE = re.compile(r"/jobs/view/(\d+)")


class ResultsPageUnrecognized(Exception):
    """A results page parsed to zero cards without LinkedIn's explicit
    no-results banner — selector break or lazy-load failure. Must end
    the run as an error, never as successful completion."""


def build_search_url(config: dict, start: int = 0) -> str:
    params: dict[str, str] = {"keywords": config["keywords"]}
    if config.get("location"):
        params["location"] = config["location"]
    if config.get("date_posted") in DATE_POSTED:
        params["f_TPR"] = DATE_POSTED[config["date_posted"]]
    workplace = [WORKPLACE[w] for w in config.get("workplace", []) if w in WORKPLACE]
    if workplace:
        params["f_WT"] = ",".join(workplace)
    experience = [EXPERIENCE[e] for e in config.get("experience", []) if e in EXPERIENCE]
    if experience:
        params["f_E"] = ",".join(experience)
    if start:
        params["start"] = str(start)
    return f"{SEARCH_URL}?{urlencode(params)}"


def _job_id_of(card, link) -> str | None:
    for attr in ("data-occludable-job-id", "data-job-id"):
        value = card.get(attr)
        if value:
            return str(value)
    if link is not None:
        match = _JOB_VIEW_RE.search(link.get("href") or "")
        if match:
            return match.group(1)
    return None


def _workplace_of(location_text: str | None) -> str | None:
    if not location_text:
        return None
    lowered = location_text.lower()
    for kind in ("remote", "hybrid", "on-site"):
        if kind in lowered:
            return kind.replace("-", "")
    return None


def parse_results(html: str) -> list[dict]:
    """Search-results HTML -> job cards. A card missing its essentials
    (id, title, company) is skipped and logged, never fatal."""
    soup = selectors.parse_html(html)
    cards: list[dict] = []
    for card in selectors.select_all(soup, "search_card"):
        link = selectors.select_first(card, "card_link")
        job_id = _job_id_of(card, link)
        title = selectors.text_of(selectors.select_first(card, "card_title"))
        company = selectors.text_of(selectors.select_first(card, "card_company"))
        if not (job_id and title and company):
            log.warning("skipping unparseable job card (id=%s)", job_id)
            continue
        location = selectors.text_of(selectors.select_first(card, "card_location"))
        posted = selectors.select_first(card, "card_posted")
        footer = " ".join(
            selectors.text_of(item) or ""
            for item in selectors.select_all(card, "card_footer_items")
        )
        cards.append(
            {
                "job_id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "posted_text": (posted.get("datetime") if posted else None)
                or selectors.text_of(posted),
                "workplace": _workplace_of(location),
                "easy_apply": "easy apply" in footer.lower(),
                "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            }
        )
    return cards


async def run_search(supervisor, run_id: int) -> dict:
    """Drive one search run (fresh or resumed) to completion: paginate,
    parse, dedupe into the jobs table, persist progress after every page
    so a restart resumes exactly where it stopped. Terminal states:
    done | stopped_captcha | stopped_cap | stopped_error."""
    run = db.get_search_run(run_id)
    if run is None:
        raise ValueError(f"no search run {run_id}")
    config = run["config"]
    start, collected = run["next_start"], run["collected"]

    def progress(**extra) -> None:
        broker.publish("search", {"run_id": run_id, "collected": collected, **extra})

    status, message = "done", None
    try:
        # Inside the guard: a malformed stored cap must terminate the run
        # as stopped_error, never kill the driver and strand it 'running'.
        cap = int(config.get("cap") or DEFAULT_RESULT_CAP)
        while collected < cap and start < pacing.SEARCH_PAGE_CAP * PAGE_SIZE:
            html = await supervisor.fetch_page(build_search_url(config, start))
            cards = parse_results(html)
            if not cards:
                if selectors.classify_empty_results(html) == "no_results":
                    break  # LinkedIn explicitly says this is the end
                raise ResultsPageUnrecognized(
                    "Results page matched no known selectors — LinkedIn's DOM "
                    "may have changed (or the page failed to load)."
                )
            start += PAGE_SIZE
            # One transaction: job cache + run membership + cursor. A crash
            # between cards and checkpoint can't desync the accounting, and
            # re-recording the same page after a restart is a no-op.
            collected = db.record_search_page(run_id, cards, start, cap)
            progress(event="page", next_start=start)
            if collected < cap:
                await pacing.page_delay()
    except pacing.DailyCapReached as exc:
        status, message = "stopped_cap", str(exc)
    except CaptchaStop as exc:
        status, message = "stopped_captcha", str(exc)
    except AuthRequired as exc:
        status, message = "stopped_auth", str(exc)
    except Exception as exc:  # noqa: BLE001 — a run must always end in a terminal state
        log.exception("search run %s failed", run_id)
        status, message = "stopped_error", f"{type(exc).__name__}: {exc}"

    db.update_search_run(run_id, status=status, message=message)
    progress(event=status)
    return db.get_search_run(run_id)
