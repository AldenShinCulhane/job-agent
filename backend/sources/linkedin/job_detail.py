"""Full job-description fetch per job, cached in SQLite by job id.

Cache policy (PLAN.md §4.3): `fetched_at` + content hash. A stale job
(older than the TTL) viewed again is silently re-fetched; an edited
posting is detected by hash change. Generated packages later pin the
exact description snapshot they were built from.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

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
    if not text:
        raise DetailParseError("no job description found on the page")
    return text


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
    description = parse_detail(html)
    new_hash = description_hash(description)
    changed = bool(job["description_hash"]) and new_hash != job["description_hash"]
    if changed:
        log.info("job %s description changed (hash %s -> %s)",
                 job_id, job["description_hash"], new_hash)
    db.save_job_detail(job_id, description, new_hash)
    return {**db.get_job(job_id), "refetched": True, "changed": changed}
