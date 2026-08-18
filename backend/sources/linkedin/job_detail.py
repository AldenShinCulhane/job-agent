"""Full job-description fetch per job, cached in SQLite by job id.

Cache policy (PLAN.md §4.3): `fetched_at` + content hash. A stale job
(older than the TTL) viewed again is silently re-fetched; an edited
posting is detected by hash change. Generated packages later pin the
exact description snapshot they were built from.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from bs4 import Comment

from backend.core import db
from backend.sources.linkedin import selectors

log = logging.getLogger(__name__)

DETAIL_TTL = timedelta(days=3)


class DetailParseError(Exception):
    pass


def is_stale(job: dict, now: datetime | None = None) -> bool:
    """True when the cached description is missing or older than the TTL."""
    if not job.get("description") or not job.get("detail_fetched_at"):
        return True
    now = now or datetime.now(timezone.utc)
    fetched = datetime.fromisoformat(job["detail_fetched_at"])
    return now - fetched > DETAIL_TTL


def description_hash(description: str) -> str:
    return hashlib.sha256(description.strip().encode()).hexdigest()[:16]


def parse_detail(html: str) -> str:
    """Detail-page HTML -> plain description text."""
    soup = selectors.parse_html(html)
    node = selectors.select_first(soup, "detail_description")
    text = selectors.text_of(node)
    if text:
        return text
    # Selector-free fallback: LinkedIn's classes are obfuscated and churn,
    # but the description section is headed "About the job". Climb from
    # that anchor to the first ancestor with substantial text.
    anchor = soup.find(
        # Real text only — bs4 string search also matches HTML comments.
        string=lambda s: s and "About the job" in s and not isinstance(s, Comment)
    )
    if anchor is not None:
        el = anchor.parent
        while el is not None and el.name not in ("body", "html", "[document]"):
            text = selectors.text_of(el)
            if text and len(text) > 200:
                return text
            el = el.parent
    raise DetailParseError("no job description found on the page")


async def fetch_detail(supervisor, job_id: str, force: bool = False) -> dict:
    """Return the job with a fresh-enough description, re-fetching when
    stale (TTL) or forced. Reports `changed` when the posting's content
    hash moved — edited postings invalidate downstream caches (M3)."""
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    if not force and not is_stale(job):
        return {**job, "refetched": False, "changed": False}

    html = await supervisor.fetch_page(job["url"])
    try:
        description = parse_detail(html)
    except DetailParseError:
        # Keep the unparseable page locally (gitignored .data/) so the
        # selector chain can be fixed against the real DOM instead of
        # guesswork. Overwritten each failure; never leaves the machine.
        dump = db.DATA_DIR / "debug_last_detail.html"
        dump.write_text(html)
        log.warning("job %s: no description matched; page dumped to %s", job_id, dump)
        raise
    new_hash = description_hash(description)
    changed = bool(job["description_hash"]) and new_hash != job["description_hash"]
    if changed:
        log.info("job %s description changed (hash %s -> %s)",
                 job_id, job["description_hash"], new_hash)
    db.save_job_detail(job_id, description, new_hash)
    return {**db.get_job(job_id), "refetched": True, "changed": changed}
