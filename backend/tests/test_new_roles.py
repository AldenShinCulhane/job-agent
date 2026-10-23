"""New and reposted postings (PLAN.md P8d), relative to the run the job
list is read for: a posting is new when neither it nor any listing of
its role was in an earlier run, and reposted when it wasn't but another
listing was. All postings here are made up."""

import pytest

from backend.api import routes
from backend.core import db

TEXT = " ".join(f"term{i}" for i in range(400))


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


def _card(job_id, *, title="Backend Engineer"):
    return {"job_id": job_id, "title": title, "company": "Example Widgets Inc.",
            "location": "Toronto, ON", "posted_text": None, "workplace": "onsite",
            "easy_apply": False, "url": f"https://www.linkedin.com/jobs/view/{job_id}/"}


def _run(job_ids, **card):
    run_id = db.create_search_run({"keywords": "x", "cap": 2000})
    db.record_search_page(run_id, [_card(j, **card) for j in job_ids],
                          next_start=len(job_ids), cap=2000)
    return run_id


async def _novelty(run_id):
    return {j["job_id"]: j["novelty"] for j in (await routes.jobs_list(run_id))["jobs"]}


async def test_every_posting_of_a_first_search_is_new(test_db):
    run_id = _run(["1001", "1002"])
    assert await _novelty(run_id) == {"1001": "new", "1002": "new"}
    # The latest run is the default.
    assert {j["job_id"]: j["novelty"] for j in (await routes.jobs_list())["jobs"]} == {
        "1001": "new", "1002": "new"}


async def test_a_posting_found_again_is_neither(test_db):
    _run(["1001", "1002"])
    second = _run(["1002", "2001"])
    assert await _novelty(second) == {"1002": None, "2001": "new"}


async def test_a_new_listing_of_a_role_seen_before_is_reposted(test_db):
    _run(["1001"])
    db.save_job_detail("1001", TEXT, "hash-a")
    second = _run(["2001", "2002"])
    db.save_job_detail("2001", TEXT, "hash-a")
    db.save_job_detail("2002", "A role of its own.", "hash-b")
    assert await _novelty(second) == {"2001": "reposted", "2002": "new"}


async def test_listings_of_one_role_in_one_search_are_new_together(test_db):
    run_id = _run(["2001", "2002"])
    db.save_job_detail("2001", TEXT, "hash-a")
    db.save_job_detail("2002", TEXT, "hash-a")
    jobs = {j["job_id"]: j for j in (await routes.jobs_list(run_id))["jobs"]}
    assert jobs["2001"]["group"] is not None
    assert {k: j["novelty"] for k, j in jobs.items()} == {"2001": "new", "2002": "new"}


async def test_a_listing_found_again_beside_its_repost(test_db):
    """The role's first listing is in this search again, beside a repost:
    the first was seen before, and the repost is a repost."""
    _run(["1001"])
    db.save_job_detail("1001", TEXT, "hash-a")
    second = _run(["1001", "2001"])
    db.save_job_detail("2001", TEXT, "hash-a")
    assert await _novelty(second) == {"1001": None, "2001": "reposted"}


async def test_an_earlier_search_reads_as_it_was_then(test_db):
    """Only runs older than the one asked for count: a later repost does
    not make the first listing anything but new in its own search."""
    first = _run(["1001"])
    db.save_job_detail("1001", TEXT, "hash-a")
    second = _run(["2001"])
    db.save_job_detail("2001", TEXT, "hash-a")
    third = _run(["3001"])
    assert await _novelty(first) == {"1001": "new"}
    assert await _novelty(second) == {"2001": "reposted"}
    assert await _novelty(third) == {"3001": "new"}


async def test_a_description_that_joins_a_role_makes_its_posting_a_repost(test_db):
    """Groups form as descriptions are saved, and the list follows them:
    the same posting is new until its text shows it repeats a role."""
    _run(["1001"])
    db.save_job_detail("1001", TEXT, "hash-a")
    second = _run(["2001"])
    assert await _novelty(second) == {"2001": "new"}
    db.save_job_detail("2001", TEXT, "hash-a")
    assert await _novelty(second) == {"2001": "reposted"}
    other = " ".join(f"other{i}" for i in range(400))
    db.save_job_detail("2001", other, "hash-b")
    assert await _novelty(second) == {"2001": "new"}


def test_earlier_runs_are_read_in_chunks(test_db):
    ids = [f"{n:04d}" for n in range(1, 1102)]
    first = _run(ids)
    second = _run(ids[:3] + ["9001"])
    assert db.in_earlier_runs(second, ids + ["9001"]) == set(ids)
    assert db.in_earlier_runs(first, ids) == set()
