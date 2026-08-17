"""LinkedIn source tests: selector parsing against sanitized fixtures,
search-run collect/dedupe/resume state, detail-cache TTL + hash change,
and pacing caps. No browser, no network — the supervisor is faked."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core import db
from backend.sources.linkedin import job_detail, pacing, search
from backend.sources.linkedin.selectors import is_checkpoint_url
from backend.sources.linkedin.supervisor import CaptchaStop

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "linkedin_search_results.html").read_text()
SEARCH_FALLBACK_HTML = (FIXTURES / "linkedin_search_results_fallback.html").read_text()
DETAIL_HTML = (FIXTURES / "linkedin_job_detail.html").read_text()


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()


@pytest.fixture
def no_delays(monkeypatch):
    async def instant():
        pass

    monkeypatch.setattr(pacing, "page_delay", instant)
    monkeypatch.setattr(pacing, "scroll_delay", instant)


class FakeSupervisor:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls: list[str] = []

    async def fetch_page(self, url: str) -> str:
        self.urls.append(url)
        item = self.pages.pop(0) if self.pages else ""
        if isinstance(item, Exception):
            raise item
        return item


# --- selector parsing (sanitized fixtures) ----------------------------------

def test_parse_results_primary_selectors():
    cards = search.parse_results(SEARCH_HTML)
    assert len(cards) == 2  # third card is a lazy placeholder — skipped
    first = cards[0]
    assert first["job_id"] == "9000000001"
    assert first["title"] == "Backend Engineer"
    assert first["company"] == "Example Widgets Inc."
    assert first["location"] == "Toronto, ON (Hybrid)"
    assert first["workplace"] == "hybrid"
    assert first["posted_text"] == "2026-08-14"
    assert first["easy_apply"] is True
    assert first["url"] == "https://www.linkedin.com/jobs/view/9000000001/"
    assert cards[1]["easy_apply"] is False
    assert cards[1]["workplace"] == "remote"


def test_parse_results_fallback_selectors():
    cards = search.parse_results(SEARCH_FALLBACK_HTML)
    assert [c["job_id"] for c in cards] == ["9000000004", "9000000005"]
    assert cards[0]["title"] == "Platform Engineer"
    assert cards[0]["company"] == "Fixture Systems Corp."
    assert cards[0]["posted_text"] == "3 weeks ago"
    assert cards[1]["workplace"] == "onsite"


def test_parse_detail_extracts_description():
    text = job_detail.parse_detail(DETAIL_HTML)
    # h1 title is outside the description container — not prepended.
    assert text.startswith("Example Widgets Inc. is hiring")
    assert "Build APIs with Python and FastAPI." in text
    assert "3+ years of experience" in text


def test_parse_detail_raises_on_empty_page():
    with pytest.raises(job_detail.DetailParseError):
        job_detail.parse_detail("<html><body><p>nothing here</p></body></html>")


def test_checkpoint_urls_detected():
    assert is_checkpoint_url("https://www.linkedin.com/checkpoint/challenge/x")
    assert is_checkpoint_url("https://www.linkedin.com/authwall?x=1")
    assert is_checkpoint_url("https://www.linkedin.com/security/CHALLENGE")
    assert not is_checkpoint_url("https://www.linkedin.com/jobs/search/?keywords=x")
    assert not is_checkpoint_url("https://www.linkedin.com/feed/")


# --- collect / dedupe / resume ----------------------------------------------

async def test_search_collects_dedupes_and_finishes(test_db, no_delays):
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    # Page 1: two parseable jobs. Page 2: same page again (all duplicates).
    # Page 3: empty -> end of results.
    fake = FakeSupervisor([SEARCH_HTML, SEARCH_HTML])
    run = await search.run_search(fake, run_id)
    assert run["status"] == "done"
    assert run["collected"] == 2  # duplicates did not count
    assert len(db.list_jobs()) == 2
    # Empty page 3 ends the run before advancing past it.
    assert run["next_start"] == 2 * search.PAGE_SIZE
    assert "start=25" in fake.urls[1] and "start=50" in fake.urls[2]


async def test_search_respects_cap(test_db, no_delays):
    run_id = db.create_search_run({"keywords": "engineer", "cap": 1})
    fake = FakeSupervisor([SEARCH_HTML])
    run = await search.run_search(fake, run_id)
    assert run["status"] == "done"
    assert run["collected"] == 1
    assert len(db.list_jobs()) == 1


async def test_search_stops_cleanly_on_captcha_and_resumes(test_db, no_delays):
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    fake = FakeSupervisor([SEARCH_HTML, CaptchaStop("checkpoint hit")])
    run = await search.run_search(fake, run_id)
    assert run["status"] == "stopped_captcha"
    assert run["collected"] == 2
    assert run["next_start"] == search.PAGE_SIZE  # progress persisted

    # Resume (fresh process, same DB): picks up at the saved offset,
    # collects the fallback page, then finishes — no duplicated work.
    db.update_search_run(run_id, status="running")
    fake2 = FakeSupervisor([SEARCH_FALLBACK_HTML])
    resumed = await search.run_search(fake2, run_id)
    assert resumed["status"] == "done"
    assert resumed["collected"] == 4
    assert f"start={search.PAGE_SIZE}" in fake2.urls[0]
    assert len(db.list_jobs()) == 4


async def test_search_daily_cap_is_terminal_but_resumable(test_db, no_delays):
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    fake = FakeSupervisor([SEARCH_HTML, pacing.DailyCapReached("cap")])
    run = await search.run_search(fake, run_id)
    assert run["status"] == "stopped_cap"
    assert run["collected"] == 2


# --- detail cache: TTL + hash-change re-fetch --------------------------------

def _seed_job(job_id="9000000001"):
    db.upsert_job(
        {
            "job_id": job_id,
            "title": "Backend Engineer",
            "company": "Example Widgets Inc.",
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        }
    )
    return job_id


def test_is_stale_ttl():
    now = datetime.now(timezone.utc)
    fresh = {
        "description": "x",
        "detail_fetched_at": (now - timedelta(days=1)).isoformat(),
    }
    stale = {
        "description": "x",
        "detail_fetched_at": (now - timedelta(days=4)).isoformat(),
    }
    assert not job_detail.is_stale(fresh, now)
    assert job_detail.is_stale(stale, now)
    assert job_detail.is_stale({"description": None, "detail_fetched_at": None}, now)


async def test_fetch_detail_uses_cache_when_fresh(test_db):
    job_id = _seed_job()
    db.save_job_detail(job_id, "cached description", "abc123")
    fake = FakeSupervisor([])
    result = await job_detail.fetch_detail(fake, job_id)
    assert result["refetched"] is False
    assert result["description"] == "cached description"
    assert fake.urls == []  # no browser action


async def test_fetch_detail_refetches_stale_and_detects_hash_change(
    test_db, monkeypatch
):
    job_id = _seed_job()
    db.save_job_detail(job_id, "old description", "oldhash")
    monkeypatch.setattr(job_detail, "is_stale", lambda job, now=None: True)
    fake = FakeSupervisor([DETAIL_HTML])
    result = await job_detail.fetch_detail(fake, job_id)
    assert result["refetched"] is True
    assert result["changed"] is True  # posting edited -> hash moved
    assert "Build APIs with Python and FastAPI." in result["description"]

    # Same content fetched again (force): hash unchanged.
    fake2 = FakeSupervisor([DETAIL_HTML])
    result2 = await job_detail.fetch_detail(fake2, job_id, force=True)
    assert result2["refetched"] is True
    assert result2["changed"] is False


async def test_fetch_detail_unknown_job(test_db):
    with pytest.raises(KeyError):
        await job_detail.fetch_detail(FakeSupervisor([]), "nope")


# --- pacing ------------------------------------------------------------------

def test_daily_action_cap(test_db, monkeypatch):
    monkeypatch.setattr(pacing, "DAILY_ACTION_CAP", 3)
    for _ in range(3):
        pacing.check_daily_cap()
    with pytest.raises(pacing.DailyCapReached):
        pacing.check_daily_cap()
