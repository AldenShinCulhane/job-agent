"""LinkedIn source tests: selector parsing against sanitized fixtures,
search-run collect/dedupe/resume state, detail-cache TTL + hash change,
and pacing caps. No browser, no network — the supervisor is faked."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import asyncio

from backend.core import db
from backend.sources.linkedin import job_detail, pacing, search, selectors
from backend.sources.linkedin.selectors import is_checkpoint_url
from backend.sources.linkedin.supervisor import CaptchaStop, LinkedInSupervisor

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "linkedin_search_results.html").read_text()
SEARCH_FALLBACK_HTML = (FIXTURES / "linkedin_search_results_fallback.html").read_text()
NO_RESULTS_HTML = (FIXTURES / "linkedin_search_no_results.html").read_text()
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
    # Plain login URLs are a sign-in situation, not a checkpoint.
    assert not is_checkpoint_url("https://www.linkedin.com/uas/login?session=x")


def test_classify_page_states():
    nav = '<html><body><header class="global-nav">x</header></body></html>'
    login = '<html><body><form class="login__form"><input id="username"></form></body></html>'
    challenge = '<html><body><form id="challenge-form"></form></body></html>'
    feed = "https://www.linkedin.com/feed/"
    jobs = "https://www.linkedin.com/jobs/search/?keywords=x"
    assert selectors.classify_page(feed, nav) == "ready"
    assert selectors.classify_page(jobs, nav) == "ready"
    assert selectors.classify_page(feed, login) == "logged_out"
    assert selectors.classify_page("https://www.linkedin.com/uas/login", "<p>x</p>") == "logged_out"
    assert selectors.classify_page(feed, challenge) == "captcha_stop"
    assert selectors.classify_page("https://www.linkedin.com/checkpoint/x", nav) == "captcha_stop"
    # Regression (found in live smoke): a real signed-in feed whose nav
    # DOM matches none of our selectors must still classify as ready —
    # unauthenticated sessions never stay on /feed/.
    assert selectors.classify_page(feed, "<p>unrecognized feed DOM</p>") == "ready"
    # Off-feed pages without markers stay unknown (tolerated by fetch_page).
    assert selectors.classify_page(jobs, "<p>mystery</p>") == "unknown"


def test_classify_empty_results():
    assert selectors.classify_empty_results(NO_RESULTS_HTML) == "no_results"
    assert selectors.classify_empty_results("<div>anything else</div>") == "unknown"


# --- collect / dedupe / resume ----------------------------------------------

async def test_search_collects_dedupes_and_finishes(test_db, no_delays):
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    # Page 1: two parseable jobs. Page 2: same page again (all duplicates).
    # Page 3: LinkedIn's explicit no-results banner -> genuine end.
    fake = FakeSupervisor([SEARCH_HTML, SEARCH_HTML, NO_RESULTS_HTML])
    run = await search.run_search(fake, run_id)
    assert run["status"] == "done"
    assert run["collected"] == 2  # duplicates did not count
    assert len(db.list_jobs(run_id)) == 2
    # The no-results page ends the run before advancing past it.
    assert run["next_start"] == 2 * search.PAGE_SIZE
    assert "start=25" in fake.urls[1] and "start=50" in fake.urls[2]


async def test_empty_page_without_no_results_banner_is_an_error(test_db, no_delays):
    """Selector break / lazy-load failure must never masquerade as
    successful completion."""
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    fake = FakeSupervisor(["<html><body><div>totally unfamiliar</div></body></html>"])
    run = await search.run_search(fake, run_id)
    assert run["status"] == "stopped_error"
    assert "no known selectors" in run["message"]


async def test_previously_cached_jobs_still_count_for_a_new_run(test_db, no_delays):
    """Run accounting is membership-based: a job already in the global
    cache from an earlier run still counts toward this run's collected
    and cap, and /jobs for the run returns exactly this run's results."""
    db.upsert_job(
        {
            "job_id": "8888888888",
            "title": "Old Cached Job",
            "company": "Historic Corp",
            "url": "https://www.linkedin.com/jobs/view/8888888888/",
        }
    )
    first_run = db.create_search_run({"keywords": "engineer", "cap": 50})
    await search.run_search(FakeSupervisor([SEARCH_HTML, NO_RESULTS_HTML]), first_run)

    second_run = db.create_search_run({"keywords": "engineer", "cap": 50})
    run = await search.run_search(
        FakeSupervisor([SEARCH_HTML, NO_RESULTS_HTML]), second_run
    )
    assert run["collected"] == 2  # both jobs known globally — still this run's results
    assert {j["job_id"] for j in db.list_jobs(second_run)} == {
        "9000000001",
        "9000000002",
    }
    assert len(db.list_jobs()) == 3  # global cache keeps the historic job


def test_record_search_page_is_idempotent(test_db):
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    cards = search.parse_results(SEARCH_HTML)
    first = db.record_search_page(run_id, cards, 25, cap=50)
    again = db.record_search_page(run_id, cards, 25, cap=50)  # crash replay
    assert first == again == 2
    assert db.get_search_run(run_id)["collected"] == 2
    assert db.get_search_run(run_id)["next_start"] == 25


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
    fake2 = FakeSupervisor([SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])
    resumed = await search.run_search(fake2, run_id)
    assert resumed["status"] == "done"
    assert resumed["collected"] == 4
    assert f"start={search.PAGE_SIZE}" in fake2.urls[0]
    assert len(db.list_jobs(run_id)) == 4


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


# --- durable state / run bookkeeping ----------------------------------------

def test_supervisor_banner_state_survives_restart(test_db):
    crashed = LinkedInSupervisor()
    crashed._set_state("captcha_stop", "checkpoint hit")

    fresh = LinkedInSupervisor()  # new process
    fresh.restore_state()
    assert (fresh.state, fresh.detail) == ("captcha_stop", "checkpoint hit")

    # Non-durable states reset to idle on restart (a stale 'ready' would
    # lie — no browser is open yet).
    crashed._set_state("ready", None)
    fresh2 = LinkedInSupervisor()
    fresh2.restore_state()
    assert fresh2.state == "idle"


def test_update_search_run_message_none_clears(test_db):
    run_id = db.create_search_run({"keywords": "x"})
    db.update_search_run(run_id, status="stopped_error", message="boom")
    db.update_search_run(run_id, status="running", message=None)
    assert db.get_search_run(run_id)["message"] is None
    db.update_search_run(run_id, collected=5)  # message omitted -> untouched
    assert db.get_search_run(run_id)["message"] is None


# --- startup recovery + worker idempotency (routes layer) --------------------

async def test_startup_recovery_resumes_interrupted_run(
    test_db, no_delays, monkeypatch
):
    """Real restart shape: a run is left 'running' in the DB with a page
    checkpointed, the process dies, startup recovery reclaims it and the
    run completes from the saved offset."""
    from backend.api import routes

    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    db.record_search_page(
        run_id, search.parse_results(SEARCH_HTML), search.PAGE_SIZE, cap=50
    )
    assert db.get_search_run(run_id)["status"] == "running"  # orphaned

    fake_browser = FakeSupervisor([SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])

    class FakeSource:
        def current_state(self):
            return "idle"

        async def connect(self):
            return {"state": "ready"}

        async def resume(self, rid):
            db.update_search_run(rid, status="running")
            return await search.run_search(fake_browser, rid)

    monkeypatch.setattr(routes.sources, "source", FakeSource())
    assert routes.recover_search_runs() == [run_id]
    await routes._search_tasks[run_id]

    run = db.get_search_run(run_id)
    assert run["status"] == "done"
    assert run["collected"] == 4
    assert f"start={search.PAGE_SIZE}" in fake_browser.urls[0]  # resumed at offset


class FakeLifecycleSource:
    """Full JobSource stand-in for lifespan tests — no browser, but the
    real run_search drives the real DB through FakeSupervisor pages."""

    def __init__(self, browser_pages=None, connect_blocks=False, state="idle"):
        self.state = state
        self.connects = 0
        self.browser = FakeSupervisor(browser_pages or [])
        self.connect_blocks = connect_blocks

    def restore_state(self):
        pass

    def current_state(self):
        return self.state

    def mark_connecting(self):
        pass

    async def connect(self, wait_for_user=True):
        self.connects += 1
        if self.connect_blocks:
            await asyncio.Event().wait()  # a user who never signs in
        return {"state": "ready", "detail": None}

    async def resume(self, rid):
        db.update_search_run(rid, status="running")
        return await search.run_search(self.browser, rid)

    async def health(self):
        return {"state": self.state, "detail": None}

    async def close(self):
        pass


def _lifespan_app(monkeypatch, tmp_path, fake_source):
    """Wire the real create_app() (real lifespan startup/shutdown) to a
    temp DB and the fake source."""
    from backend import sources as sources_pkg
    from backend.main import create_app

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(sources_pkg, "source", fake_source)
    return create_app()


def _seed_orphaned_run() -> int:
    """A run mid-flight when its process died: one page checkpointed,
    status still 'running', no task holding it."""
    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    db.record_search_page(
        run_id, search.parse_results(SEARCH_HTML), search.PAGE_SIZE, cap=50
    )
    return run_id


def test_lifespan_recovers_run_and_shuts_down(tmp_path, monkeypatch, no_delays):
    import time

    from fastapi.testclient import TestClient

    fake = FakeLifecycleSource(browser_pages=[SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])
    app = _lifespan_app(monkeypatch, tmp_path, fake)
    run_id = _seed_orphaned_run()

    with TestClient(app):  # lifespan startup fires real recovery
        deadline = time.time() + 10
        while time.time() < deadline:
            if db.get_search_run(run_id)["status"] == "done":
                break
            time.sleep(0.05)
    run = db.get_search_run(run_id)
    assert run["status"] == "done"
    assert run["collected"] == 4
    assert f"start={search.PAGE_SIZE}" in fake.browser.urls[0]  # resumed at offset


def test_lifespan_shutdown_cancels_stuck_connect(tmp_path, monkeypatch, no_delays):
    """Shutdown must cancel a worker parked in connect()'s login polling
    instead of hanging behind it, and leave the run 'running' so the next
    startup recovers it."""
    import time

    from fastapi.testclient import TestClient

    fake = FakeLifecycleSource(connect_blocks=True)
    app = _lifespan_app(monkeypatch, tmp_path, fake)
    run_id = _seed_orphaned_run()

    with TestClient(app):
        deadline = time.time() + 5
        while time.time() < deadline and fake.connects == 0:
            time.sleep(0.05)
        assert fake.connects == 1  # recovery claimed the run, then blocked
    # Context exit returned => shutdown didn't hang. Run stays claimable.
    assert db.get_search_run(run_id)["status"] == "running"


def test_startup_recovery_defers_while_captcha_stopped(
    tmp_path, monkeypatch, no_delays
):
    """A persisted captcha stop must gate recovery — restarting
    navigation against a checkpoint is a bypass-shaped move."""
    import time

    from fastapi.testclient import TestClient

    fake = FakeLifecycleSource(state="captcha_stop")
    app = _lifespan_app(monkeypatch, tmp_path, fake)
    run_id = _seed_orphaned_run()

    with TestClient(app):
        time.sleep(0.3)
        assert fake.connects == 0  # nothing touched the browser
    assert db.get_search_run(run_id)["status"] == "running"  # deferred, not lost


async def test_connect_success_reclaims_stopped_auth_run(
    test_db, no_delays, monkeypatch
):
    """Gate 2 rule: after the user signs back in via Connect, the
    auth-stopped search resumes with no extra Resume click."""
    from backend.api import routes

    run_id = _seed_orphaned_run()
    db.update_search_run(run_id, status="stopped_auth", message="expired")

    fake = FakeLifecycleSource(browser_pages=[SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])
    monkeypatch.setattr(routes.sources, "source", fake)
    await routes._connect_only()
    task = routes._search_tasks.get(run_id)
    assert task is not None  # reclaimed automatically
    await task

    run = db.get_search_run(run_id)
    assert run["status"] == "done"
    assert run["collected"] == 4
    assert run["message"] is None  # stale auth error cleared


async def test_connect_reclaims_only_the_latest_run(test_db, no_delays, monkeypatch):
    """Auto-reclaim is scoped to the CURRENT search: a successful Connect
    must not silently spend LinkedIn actions on historical stopped runs."""
    from backend.api import routes

    old_run = _seed_orphaned_run()
    db.update_search_run(old_run, status="stopped_auth", message="expired")
    new_run = _seed_orphaned_run()
    db.update_search_run(new_run, status="stopped_auth", message="expired")

    fake = FakeLifecycleSource(browser_pages=[SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])
    monkeypatch.setattr(routes.sources, "source", fake)
    await routes._connect_only()

    assert old_run not in routes._search_tasks  # history untouched
    task = routes._search_tasks.get(new_run)
    assert task is not None
    await task
    assert db.get_search_run(new_run)["status"] == "done"
    assert db.get_search_run(old_run)["status"] == "stopped_auth"


async def test_capped_run_resumes_on_startup_when_budget_available(
    test_db, no_delays, monkeypatch
):
    from backend.api import routes

    run_id = _seed_orphaned_run()
    db.update_search_run(run_id, status="stopped_cap", message="cap")

    fake = FakeLifecycleSource(browser_pages=[SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])
    monkeypatch.setattr(routes.sources, "source", fake)
    assert routes.recover_search_runs() == [run_id]
    await routes._search_tasks[run_id]
    assert db.get_search_run(run_id)["status"] == "done"


async def test_capped_run_arms_retry_timer_when_budget_spent(test_db, monkeypatch):
    from backend.api import routes

    run_id = _seed_orphaned_run()
    db.update_search_run(run_id, status="stopped_cap", message="cap")

    fake = FakeLifecycleSource()
    monkeypatch.setattr(routes.sources, "source", fake)
    monkeypatch.setattr(routes.sources, "budget_remaining", lambda: 0)
    monkeypatch.setattr(routes, "_cap_retry_task", None)

    assert routes.recover_search_runs() == []  # nothing spent now
    assert db.get_search_run(run_id)["status"] == "stopped_cap"
    assert routes._cap_retry_task is not None  # resumes itself at UTC reset
    routes._cap_retry_task.cancel()


async def test_driver_arms_cap_retry_when_run_caps(test_db, monkeypatch):
    from backend.api import routes

    run_id = db.create_search_run({"keywords": "x", "cap": 50})

    class CapSource:
        async def connect(self):
            return {"state": "ready"}

        async def resume(self, rid):
            db.update_search_run(rid, status="stopped_cap", message="cap")
            return db.get_search_run(rid)

    monkeypatch.setattr(routes.sources, "source", CapSource())
    monkeypatch.setattr(routes, "_cap_retry_task", None)
    await routes._connect_then_search(run_id)
    assert routes._cap_retry_task is not None
    routes._cap_retry_task.cancel()


async def test_double_start_supersedes_previous_run(test_db, monkeypatch):
    """One active search is enforced, not assumed: a second Start retires
    the first run (terminal 'superseded') and cancels its worker."""
    from backend.api import routes

    release = asyncio.Event()

    class SlowSource:
        def current_state(self):
            return "idle"

        async def connect(self, wait_for_user=True):
            await release.wait()

        async def resume(self, rid):
            db.update_search_run(rid, status="done")
            return db.get_search_run(rid)

    monkeypatch.setattr(routes.sources, "source", SlowSource())
    first = await routes.search_start({"keywords": "a"})
    task1 = routes._search_tasks[first["run_id"]]
    second = await routes.search_start({"keywords": "b"})

    assert db.get_search_run(first["run_id"])["status"] == "superseded"
    await asyncio.gather(task1, return_exceptions=True)
    assert task1.cancelled()

    release.set()
    await routes._search_tasks[second["run_id"]]
    assert db.get_search_run(second["run_id"])["status"] == "done"


async def test_recovery_retires_stale_running_rows(test_db, no_delays, monkeypatch):
    """Restart with two 'running' rows: only the latest is reclaimed and
    the older one is superseded, never stranded."""
    from backend.api import routes

    old_run = _seed_orphaned_run()
    new_run = _seed_orphaned_run()

    fake = FakeLifecycleSource(browser_pages=[SEARCH_FALLBACK_HTML, NO_RESULTS_HTML])
    monkeypatch.setattr(routes.sources, "source", fake)
    assert routes.recover_search_runs() == [new_run]
    await routes._search_tasks[new_run]

    assert db.get_search_run(old_run)["status"] == "superseded"
    assert db.get_search_run(new_run)["status"] == "done"


async def test_connect_arms_cap_retry_when_current_run_capped(test_db, monkeypatch):
    """Captcha + cap can stack: once the checkpoint is resolved via
    Connect and nothing is session-blocked, the cap-stopped run must
    still get its retry timer."""
    from backend.api import routes

    run_id = _seed_orphaned_run()
    db.update_search_run(run_id, status="stopped_cap", message="cap")

    fake = FakeLifecycleSource()
    monkeypatch.setattr(routes.sources, "source", fake)
    monkeypatch.setattr(routes.sources, "budget_remaining", lambda: 0)
    monkeypatch.setattr(routes, "_cap_retry_task", None)
    await routes._connect_only()

    assert routes._cap_retry_task is not None
    routes._cap_retry_task.cancel()
    assert db.get_search_run(run_id)["status"] == "stopped_cap"


async def test_search_start_rejects_bad_cap(test_db):
    from fastapi import HTTPException

    from backend.api import routes

    for bad in ("lots", 0, -5, 1001, True):
        with pytest.raises(HTTPException):
            await routes.search_start({"keywords": "x", "cap": bad})


class FakeCheckpointPage:
    """Playwright page stand-in that lands on a challenge after goto."""

    def __init__(self, landing_url: str, html: str):
        self.url = ""
        self._landing_url = landing_url
        self._html = html
        self.scrolls = 0

    def is_closed(self):
        return False

    async def goto(self, url, wait_until=None):
        self.url = self._landing_url

    async def content(self):
        return self._html

    async def evaluate(self, script):
        self.scrolls += 1


async def test_checkpoint_detected_before_any_scroll(test_db):
    """The latch must fire on the DOM immediately after navigation —
    scripted scrolling on a challenge page is exactly what we promised
    never to do."""
    sup = LinkedInSupervisor()
    sup.state = "ready"
    page = FakeCheckpointPage(
        "https://www.linkedin.com/jobs/search/?keywords=x",  # URL looks fine
        '<html><body><form id="challenge-form"></form></body></html>',
    )
    sup._page = page
    with pytest.raises(CaptchaStop):
        await sup.fetch_page("https://www.linkedin.com/jobs/search/?keywords=x")
    assert page.scrolls == 0
    assert sup.state == "captcha_stop"


class FakeInjectedChallengePage:
    """Clean page that turns into a challenge mid-scroll."""

    def __init__(self, inject_after: int):
        self.url = "https://www.linkedin.com/jobs/search/?keywords=x"
        self.scrolls = 0
        self._inject_after = inject_after

    def is_closed(self):
        return False

    async def goto(self, url, wait_until=None):
        pass

    async def content(self):
        if self.scrolls >= self._inject_after:
            return '<html><body><form id="challenge-form"></form></body></html>'
        return '<html><body><header class="global-nav">x</header></body></html>'

    async def evaluate(self, script):
        self.scrolls += 1


async def test_challenge_injected_midscroll_stops_before_next_scroll(
    test_db, monkeypatch
):
    async def instant():
        pass

    monkeypatch.setattr(pacing, "scroll_delay", instant)
    sup = LinkedInSupervisor()
    sup.state = "ready"
    page = FakeInjectedChallengePage(inject_after=3)
    sup._page = page
    with pytest.raises(CaptchaStop):
        await sup.fetch_page(page.url)
    assert page.scrolls == 3  # detected right after injection, well before 8
    assert sup.state == "captcha_stop"


async def test_duplicate_resume_spawns_single_worker(test_db, monkeypatch):
    from backend.api import routes

    run_id = db.create_search_run({"keywords": "engineer", "cap": 50})
    release = asyncio.Event()
    connects = []

    class SlowSource:
        async def connect(self):
            connects.append(1)
            await release.wait()

        async def resume(self, rid):
            db.update_search_run(rid, status="done")
            return db.get_search_run(rid)

    monkeypatch.setattr(routes.sources, "source", SlowSource())
    assert routes._spawn_search(run_id) is True
    task = routes._search_tasks[run_id]
    assert routes._spawn_search(run_id) is False  # already driven — no-op
    release.set()
    await task
    assert connects == [1]  # exactly one worker ran

    # After the worker finishes, a new spawn is allowed again.
    assert routes._spawn_search(run_id) is True
    release.set()
    await routes._search_tasks.get(run_id, task)
