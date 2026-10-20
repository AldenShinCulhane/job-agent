"""The application tracker (PLAN.md P8b): an append-only record of what
the user did with a posting. The latest event decides, Clear status is
an event of its own, and nothing the grouping does reaches it."""

import sqlite3
from datetime import date

import pytest
from fastapi import HTTPException

from backend.core import db
from backend.tests.test_duplicates import TEXT, _edited, _run, _shared


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


def _events():
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(
            "SELECT id, job_id, package_id, status, occurred_on FROM application_events "
            "ORDER BY id").fetchall()
    finally:
        conn.close()


def _table(name):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
    finally:
        conn.close()


def _current():
    return {a["job_id"]: (a["status"], a["occurred_on"], a["package_id"])
            for a in db.current_applications()}


def _package(job_id):
    package, _ = db.create_package(job_id, 1, f"hash-{job_id}", 1, template_version=1,
                                   cover_template_version=1)
    return package


async def _post(**body):
    from backend.api import routes
    return await routes.applications_record(body)


async def _refused(**body) -> int:
    with pytest.raises(HTTPException) as refused:
        await _post(**body)
    return refused.value.status_code


def test_a_status_is_recorded_with_its_package(test_db):
    _run(["1001"])
    package = _package("1001")
    assert db.record_application("1001", "applied", "2026-10-02", package) is True
    assert db.current_applications() == [{
        "event_id": 1, "job_id": "1001", "package_id": package,
        "status": "applied", "occurred_on": "2026-10-02"}]


def test_the_latest_event_decides_not_the_day_it_names(test_db):
    """A correction recorded later names an earlier day: it still wins."""
    _run(["1001"])
    db.record_application("1001", "applied", "2026-10-10")
    db.record_application("1001", "interviewing", "2026-10-01")
    assert _current() == {"1001": ("interviewing", "2026-10-01", None)}
    db.record_application("1001", "applied", "2026-10-03")
    assert _current() == {"1001": ("applied", "2026-10-03", None)}


def test_clear_status_untracks_and_keeps_the_history(test_db):
    _run(["1001"])
    db.record_application("1001", "applied", "2026-10-02")
    db.record_application("1001", "rejected", "2026-10-09")
    assert db.record_application("1001", "cleared", "2026-10-10") is True
    assert _current() == {}
    # Not an undo: everything before the clear is still there, and the
    # next status starts from the clear, not from what it cleared.
    assert [e[3] for e in _events()] == ["applied", "rejected", "cleared"]
    db.record_application("1001", "applied", "2026-10-11")
    assert _current() == {"1001": ("applied", "2026-10-11", None)}


def test_clearing_one_listing_leaves_another_listings_status(test_db):
    """Two listings of one role, each with its own record: clearing one
    leaves the other's application intact."""
    _run(["1001", "1002"])
    db.save_job_detail("1001", TEXT, "hash-a")
    db.save_job_detail("1002", TEXT, "hash-a")
    db.record_application("1001", "applied", "2026-10-01")
    db.record_application("1002", "interviewing", "2026-10-05")
    db.record_application("1001", "cleared", "2026-10-06")
    assert _current() == {"1002": ("interviewing", "2026-10-05", None)}


def test_an_event_that_changes_nothing_is_not_appended(test_db):
    _run(["1001"])
    package = _package("1001")
    # Clearing what isn't tracked.
    assert db.record_application("1001", "cleared", "2026-10-01") is False
    assert db.record_application("1001", "applied", "2026-10-02", package) is True
    # The same click again.
    assert db.record_application("1001", "applied", "2026-10-02", package) is False
    # Each of status, day and package is a change of its own.
    assert db.record_application("1001", "applied", "2026-10-03", package) is True
    assert db.record_application("1001", "applied", "2026-10-03", None) is True
    assert db.record_application("1001", "offer", "2026-10-03", None) is True
    assert db.record_application("1001", "cleared", "2026-10-04") is True
    assert db.record_application("1001", "cleared", "2026-10-05") is False
    assert [e[3] for e in _events()] == ["applied", "applied", "applied", "offer", "cleared"]


def test_a_package_must_be_the_postings_own(test_db):
    _run(["1001", "1002"])
    package = _package("1001")
    for job_id, package_id in (("1002", package), ("1001", package + 50)):
        with pytest.raises(db.ApplicationRefused) as refused:
            db.record_application(job_id, "applied", "2026-10-02", package_id)
        assert refused.value.reason == "foreign_package"
    with pytest.raises(db.ApplicationRefused) as refused:
        db.record_application("9999", "applied", "2026-10-02")
    assert refused.value.reason == "unknown_job"
    assert _events() == []


async def test_the_route_answers_what_it_was_given(test_db):
    _run(["1001", "1002"])
    package = _package("1001")
    assert await _refused(job_id="1002", status="applied", package_id=package) == 422
    assert await _refused(job_id="9999", status="applied") == 404
    for body in ({"status": "applied"}, {"job_id": 1001, "status": "applied"},
                 {"job_id": "1001", "status": "hired"}, {"job_id": "1001"},
                 {"job_id": "1001", "status": "applied", "occurred_on": "2026-13-01"},
                 {"job_id": "1001", "status": "applied", "occurred_on": "10/11/2026"},
                 {"job_id": "1001", "status": "applied", "occurred_on": "2026-10-1"},
                 {"job_id": "1001", "status": "applied", "package_id": True},
                 {"job_id": "1001", "status": "applied", "package_id": str(package)}):
        assert await _refused(**body) == 422, body
    assert _events() == []

    answer = await _post(job_id="1001", status="applied", package_id=package)
    today = date.today().isoformat()
    assert answer == {"created": True, "application": {
        "event_id": 1, "job_id": "1001", "package_id": package,
        "status": "applied", "occurred_on": today}}
    again = await _post(job_id="1001", status="applied", package_id=package,
                        occurred_on=today)
    assert again == dict(answer, created=False)
    assert await _post(job_id="1001", status="cleared") == {
        "created": True, "application": None}

    from backend.api import routes
    assert await routes.applications_list() == {"applications": []}


async def test_a_recorded_status_announces_the_tracker(test_db, monkeypatch):
    from backend.core.events import broker

    published = []
    monkeypatch.setattr(broker, "publish", lambda topic, data: published.append(topic))
    _run(["1001"])
    await _post(job_id="1001", status="applied", occurred_on="2026-10-02")
    await _post(job_id="1001", status="applied", occurred_on="2026-10-02")
    assert published == ["applications"]


def test_the_history_survives_regrouping(test_db):
    """Listings join a role and leave it again; what the user recorded on
    each stays exactly as it was, under its own posting."""
    _run(["1001", "1002"])
    db.save_job_detail("1001", TEXT, "hash-a")
    db.record_application("1001", "applied", "2026-10-01")
    db.record_application("1002", "withdrawn", "2026-10-02")
    before = _table("application_events")

    db.save_job_detail("1002", TEXT, "hash-a")
    assert _shared() == {frozenset({"1001", "1002"})}
    db.save_job_detail("1001", _edited(0, 200), "hash-b")
    assert _shared() == set()
    db.save_job_detail("1001", TEXT, "hash-a")
    assert _shared() == {frozenset({"1001", "1002"})}
    _run(["1001"], title="Platform Engineer")  # renamed: another bucket
    assert _shared() == set()
    assert _table("application_events") == before
    assert _current() == {"1001": ("applied", "2026-10-01", None),
                          "1002": ("withdrawn", "2026-10-02", None)}


def test_an_older_database_gains_the_tracker_and_nothing_else_changes(test_db):
    _run(["1001"])
    db.save_job_detail("1001", TEXT, "hash-a")
    _package("1001")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DROP TABLE application_events")
    conn.commit()
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    conn.close()
    before = {name: _table(name) for name in names}

    db.init()
    assert {name: _table(name) for name in names} == before
    assert _table("application_events") == []
    db.record_application("1001", "applied", "2026-10-02")
    assert _current() == {"1001": ("applied", "2026-10-02", None)}
