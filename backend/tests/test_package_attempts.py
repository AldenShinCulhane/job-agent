"""Package attempts (PLAN.md §4.5a, P6b). All data fake.

Try again on a failed package and Regenerate on a done one are explicit
user actions. Each makes a fresh row with its own stage state and repair
ledger; the row it came from and that row's files never change. A retry
on changed inputs is a package for those inputs, labelled as such. One
click is one answer, however often it reaches the server.
"""

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import db
from backend.tests import engine_fakes
from backend.tests.test_tailor import (
    PROFILE,
    _cited_tailored,
    _confirm_profile,
    _create_package,
    _fake_compiler,
    _legacy_packages_db,
    _seed_derived,
    _seed_job,
    _table_names,
    _tailor_env,
    _two_round_structured,
)

pytestmark = pytest.mark.anyio

KEY = "click-0001-aaaa"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


@pytest.fixture
def client(test_db, monkeypatch):
    """The routes, with the batch worker left to each test: a test runs
    the packages itself so every build is observable."""
    from backend.api import routes

    monkeypatch.setattr(routes, "_maybe_resume_packages", lambda: None)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app)


def _retry(client, package_id, key=KEY):
    return client.post(f"/api/packages/{package_id}/retry", json={"request_key": key})


def _rows() -> list[tuple]:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(
            "SELECT id, attempt, retry_of FROM packages ORDER BY id").fetchall()
    finally:
        conn.close()


def _requests() -> list[tuple]:
    """Each click the server has answered: (key, package clicked, package
    it got)."""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(
            "SELECT request_key, source_id, package_id FROM package_requests "
            "ORDER BY rowid").fetchall()
    finally:
        conn.close()


def _finished(status: str, tmp_path, monkeypatch, version=None) -> int:
    """A package for job p1 built to `status` (done or failed) through the
    real pipeline and the fake engine."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    version = version or _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    engine_fakes.pin("claude-fable-5")
    pkg, _ = _create_package("p1", version, "hash-a")
    if status == "done":
        _tailor_env(monkeypatch, tmp_path)
    else:
        # Generation overclaims and both repair rounds still overclaim:
        # the package fails with its whole repair allowance spent.
        _fake_compiler(monkeypatch, lambda tex: 1)
        monkeypatch.setattr(tailor_pipeline, "OUTPUT_ROOT", tmp_path / "output")
        fixed = _cited_tailored()[0]
        broken = json.loads(json.dumps(fixed))
        broken["experiences"][0]["bullets"][0]["text"] = "Claims too much impact"
        still = json.loads(json.dumps(broken))
        still["experiences"][0]["bullets"][0]["text"] = "Still too much"

        async def fake_generate(*_args):
            return broken

        monkeypatch.setattr(tailor_resume, "generate_tailored",
                            engine_fakes.answering("tailor", fake_generate))
        monkeypatch.setattr(llm, "structured", engine_fakes.structured(
            _two_round_structured([still, still], [],
                                  {"Claims too much impact", "Still too much"})))
    assert asyncio.run(tailor_pipeline.run_package(pkg)) == status
    return pkg


def _calls(package_id) -> list[str]:
    """The package's call ledger: in provenance once published, in the
    stage state before (a failed package never publishes)."""
    pkg = db.get_package(package_id)
    return [c["call"] for c in pkg["provenance"].get("llm_calls")
            or pkg["stage"].get("llm_calls", [])]


# --- The migration ------------------------------------------------------------

# The table as it stood before attempts joined package identity.
_PRE_ATTEMPT_TABLE = """
CREATE TABLE packages (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                TEXT NOT NULL,
    profile_version       INTEGER NOT NULL,
    jd_hash               TEXT NOT NULL,
    page_limit            INTEGER NOT NULL DEFAULT 1,
    template_version      INTEGER NOT NULL DEFAULT 1,
    cover_template_version INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL DEFAULT 'queued',
    stage_json            TEXT,
    error                 TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    provenance_json       TEXT,
    output_dir            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (job_id, profile_version, jd_hash, page_limit,
            template_version, cover_template_version)
);
"""


def _pre_attempt_db(tmp_path, monkeypatch) -> Path:
    """A database from before attempts: a done package with its files on
    disk, a failed one, a queued one — and an id counter past the highest
    id still present, as after a row went away."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pre_attempt.sqlite3")
    out = tmp_path / "output" / "Example_Widgets_Inc_p1_p3"
    out.mkdir(parents=True)
    (out / "resume.pdf").write_bytes(b"%PDF-1.4\n% published resume\n")
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(_PRE_ATTEMPT_TABLE)
    for row in (
        (3, "p1", 4, "hash-a", "done", json.dumps({"llm_calls": [{"call": "generation"}]}),
         str(out)),
        (5, "p2", 4, "hash-b", "failed", None, None),
        (9, "p3", 4, "hash-c", "queued", None, None),
    ):
        conn.execute(
            "INSERT INTO packages (id, job_id, profile_version, jd_hash, "
            "status, provenance_json, output_dir, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 't0', 't1')", row)
    conn.execute("UPDATE sqlite_sequence SET seq = 12 WHERE name = 'packages'")
    conn.commit()
    conn.close()
    return out


def _snapshot() -> list[tuple]:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(
            "SELECT id, job_id, status, output_dir, provenance_json FROM packages "
            "ORDER BY id").fetchall()
    finally:
        conn.close()


def test_existing_packages_become_attempt_one_with_ids_and_downloads_kept(
    tmp_path, monkeypatch
):
    out = _pre_attempt_db(tmp_path, monkeypatch)
    before = _snapshot()

    db.init()  # the upgrade

    assert _snapshot() == before
    assert _rows() == [(3, 1, None), (5, 1, None), (9, 1, None)]
    assert "packages_pre_attempt" not in _table_names()
    assert _requests() == []

    # The published package still downloads, from the same place.
    from backend.api import routes
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    got = TestClient(app).get("/api/packages/3/file/resume.pdf")
    assert got.status_code == 200 and got.content == (out / "resume.pdf").read_bytes()

    # Ids are never handed out twice: the counter moved across, so a new
    # row is not 10, the next id after the highest still present.
    _seed_job("p4", "hash-d")
    fresh, created = db.create_package("p4", 4, "hash-d", 1, template_version=1,
                                       cover_template_version=1)
    assert created and fresh == 13

    # And the attempt is part of identity now: a second attempt of the
    # same inputs is a row of its own.
    second, created = db.retry_package(
        3, KEY, profile_version=4, jd_hash="hash-a", page_limit=1,
        template_version=1, cover_template_version=1)
    assert created and db.get_package(second)["attempt"] == 2


def test_a_failed_attempt_migration_rolls_back_whole(tmp_path, monkeypatch):
    """Dying partway leaves the old table authoritative: no renamed copy
    beside an empty new one, and the next startup migrates again."""
    _pre_attempt_db(tmp_path, monkeypatch)
    before = _snapshot()
    monkeypatch.setattr(db, "_COPY_PACKAGES_PRE_ATTEMPT_SQL",
                        "INSERT INTO packages (id) SELECT no_such_column "
                        "FROM packages_pre_attempt")
    with pytest.raises(sqlite3.OperationalError):
        db.init()

    conn = sqlite3.connect(db.DB_PATH)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(packages)")}
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'packages'").fetchone()
    finally:
        conn.close()
    assert "attempt" not in cols
    assert _snapshot() == before and seq == (12,)
    assert "packages_pre_attempt" not in _table_names()

    monkeypatch.undo()
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pre_attempt.sqlite3")
    db.init()
    assert _snapshot() == before
    assert db.get_package(3)["attempt"] == 1


def test_a_short_attempt_migration_copy_is_refused(tmp_path, monkeypatch):
    _pre_attempt_db(tmp_path, monkeypatch)
    before = _snapshot()
    monkeypatch.setattr(db, "_COPY_PACKAGES_PRE_ATTEMPT_SQL",
                        db._COPY_PACKAGES_PRE_ATTEMPT_SQL + " WHERE id != 9")
    with pytest.raises(sqlite3.IntegrityError, match="copied 2 of 3 rows"):
        db.init()
    assert _snapshot() == before
    assert "packages_pre_attempt" not in _table_names()


def test_a_database_from_before_the_template_versions_reaches_attempts_in_one_step(
    tmp_path, monkeypatch
):
    _legacy_packages_db(tmp_path, monkeypatch)
    db.init()
    assert [(r[0], r[1]) for r in _rows()] == [(7, 1), (8, 1)]
    assert db.get_package(7)["output_dir"] == "/out/p1"
    assert not {"packages_pre_tpl_version", "packages_pre_attempt"} & _table_names()


# --- Try again, Regenerate ----------------------------------------------------

def test_try_again_makes_a_fresh_attempt_and_leaves_the_failed_one_as_it_was(
    client, tmp_path, monkeypatch
):
    """The failed package spent both repair rounds. The new attempt has
    its own stage state and its own two rounds, and needs both."""
    from backend.core import llm
    from backend.tailor import pipeline as tailor_pipeline
    from backend.tailor import resume as tailor_resume

    failed = _finished("failed", tmp_path, monkeypatch)
    old = db.get_package(failed)
    assert _calls(failed).count("truth_repair") == 2

    got = _retry(client, failed)
    assert got.status_code == 200
    assert got.json() == {"package_id": got.json()["package_id"], "created": True,
                          "attempt": 2, "new_inputs": False}
    new = db.get_package(got.json()["package_id"])
    assert new["status"] == "queued" and new["stage"] == {} and new["provenance"] == {}
    assert new["retry_count"] == 0 and new["retry_of"] == failed
    assert new["error"] is None and new["output_dir"] is None

    fixed = _cited_tailored()[0]
    broken = json.loads(json.dumps(fixed))
    broken["experiences"][0]["bullets"][0]["text"] = "Claims too much impact"
    half = json.loads(json.dumps(fixed))
    half["experiences"][0]["bullets"][0]["text"] = "Still too much"

    async def fake_generate(*_args):
        return broken

    repairs: list = []
    monkeypatch.setattr(tailor_resume, "generate_tailored",
                        engine_fakes.answering("tailor", fake_generate))
    monkeypatch.setattr(llm, "structured", engine_fakes.structured(
        _two_round_structured([half, fixed], repairs,
                              {"Claims too much impact", "Still too much"})))
    assert asyncio.run(tailor_pipeline.run_package(new["id"])) == "done"
    assert repairs == [1, 1]
    assert _calls(new["id"]).count("truth_repair") == 2
    assert db.get_package(new["id"])["output_dir"].endswith(f"_p{new['id']}")
    assert db.get_package(failed) == old


def test_regenerate_leaves_the_done_package_and_its_files_as_they_were(
    client, tmp_path, monkeypatch
):
    from backend.tailor import pipeline as tailor_pipeline

    done = _finished("done", tmp_path, monkeypatch)
    old = db.get_package(done)
    old_pdf = Path(old["output_dir"]) / "resume.pdf"
    old_bytes = old_pdf.read_bytes()

    got = _retry(client, done)
    assert got.json()["created"] is True and got.json()["attempt"] == 2
    new_id = got.json()["package_id"]
    assert asyncio.run(tailor_pipeline.run_package(new_id)) == "done"

    new = db.get_package(new_id)
    assert new["output_dir"] != old["output_dir"]
    assert (Path(new["output_dir"]) / "resume.pdf").is_file()
    assert db.get_package(done) == old and old_pdf.read_bytes() == old_bytes
    for package_id in (done, new_id):
        assert client.get(f"/api/packages/{package_id}/file/resume.pdf").status_code == 200


def test_generate_returns_the_latest_attempt(client, tmp_path, monkeypatch):
    done = _finished("done", tmp_path, monkeypatch)
    second = _retry(client, done).json()["package_id"]
    again = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    assert again.json() == {"package_ids": [second], "created": 0}
    assert len(_rows()) == 2


def test_one_click_sent_twice_makes_one_attempt(client, tmp_path, monkeypatch):
    done = _finished("done", tmp_path, monkeypatch)
    first = _retry(client, done).json()
    repeat = _retry(client, done).json()
    assert first["created"] is True and repeat["created"] is False
    assert repeat["package_id"] == first["package_id"] and repeat["attempt"] == 2
    assert [r[1] for r in _rows()] == [1, 2]


def _race(n, submit) -> list:
    """Run `submit(i)` on n threads released together; each result or
    raised exception, in thread order."""
    gate = threading.Barrier(n)
    results: list = [None] * n

    def run(i):
        gate.wait()
        try:
            results[i] = submit(i)
        except Exception as exc:  # noqa: BLE001
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _inputs(version) -> dict:
    from backend.tailor import pipeline as tailor_pipeline

    resume_tpl, cover_tpl = tailor_pipeline.template_versions()
    return {"profile_version": version, "jd_hash": "hash-a", "page_limit": 1,
            "template_version": resume_tpl, "cover_template_version": cover_tpl}


def test_concurrent_submissions_of_one_click_make_one_attempt(
    test_db, tmp_path, monkeypatch
):
    done = _finished("done", tmp_path, monkeypatch)
    version = db.get_package(done)["profile_version"]
    results = _race(8, lambda i: db.retry_package(done, KEY, **_inputs(version)))
    assert all(not isinstance(r, Exception) for r in results), results
    assert {r[0] for r in results} == {results[0][0]}
    assert sorted(r[1] for r in results) == [False] * 7 + [True]
    assert [r[1] for r in _rows()] == [1, 2]


def test_two_clicks_racing_on_one_package_make_one_attempt(
    test_db, tmp_path, monkeypatch
):
    """Different clicks — two tabs — each with its own key. Only one can
    find the package still the job's newest; MAX(attempt)+1 taken by
    both would have made two."""
    done = _finished("done", tmp_path, monkeypatch)
    version = db.get_package(done)["profile_version"]
    results = _race(8, lambda i: db.retry_package(done, f"click-{i:04d}-bbbb",
                                                  **_inputs(version)))
    made = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, db.RetryRefused)]
    assert len(made) == 1 and made[0][1] is True
    assert len(refused) == 7 and {r.reason for r in refused} == {"not_latest"}
    assert [r[1] for r in _rows()] == [1, 2]


def test_two_clicks_racing_on_changed_inputs_make_one_package(client):
    """The same race where the answer is the next attempt of another
    identity's finished package."""
    finished, newest = _two_limits(client, "failed")
    version = db.get_package(finished)["profile_version"]
    results = _race(8, lambda i: db.retry_package(newest, f"click-{i:04d}-ffff",
                                                  **_inputs(version)))
    made = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, db.RetryRefused)]
    assert len(made) == 1 and made[0][1] is True
    assert len(refused) == 7 and {r.reason for r in refused} == {"not_latest"}
    assert len(_rows()) == 3


@pytest.mark.parametrize("body", [
    {}, {"request_key": None}, {"request_key": 7}, {"request_key": "short"},
    {"request_key": "has space in it"}, {"request_key": "x" * 65},
])
def test_a_retry_needs_a_plain_request_key(client, tmp_path, monkeypatch, body):
    done = _finished("done", tmp_path, monkeypatch)
    got = client.post(f"/api/packages/{done}/retry", json=body)
    assert got.status_code == 422
    assert len(_rows()) == 1


def test_what_a_retry_refuses(client, tmp_path, monkeypatch):
    done = _finished("done", tmp_path, monkeypatch)
    assert _retry(client, 999).status_code == 404

    second = _retry(client, done).json()["package_id"]
    # The second attempt is still queued: nothing to try again yet.
    busy = _retry(client, second, "click-0002-cccc")
    assert busy.status_code == 409 and "still being built" in busy.json()["detail"]
    # The first attempt is history once a second exists.
    stale = _retry(client, done, "click-0003-cccc")
    assert stale.status_code == 409 and "newer package" in stale.json()["detail"]
    # One click's key cannot be replayed against another package.
    db.update_package(second, status="failed", error="boom")
    reused = _retry(client, second, KEY)
    assert reused.status_code == 409 and "another package" in reused.json()["detail"]
    assert [r[1] for r in _rows()] == [1, 2]


# --- Changed inputs -----------------------------------------------------------

def _change(what, version):
    """Change one input the way the app does; returns the new version."""
    from backend.resume import template as resume_template

    if what == "profile":
        new = db.save_profile({**PROFILE, "headline": "Engineer, updated"})
        db.confirm_profile(new)
        _seed_derived("p1", new)
        return new
    if what == "posting":
        db.save_job_detail("p1", "We need Python and Go.", "hash-b")
        _seed_derived("p1", version, jd_hash="hash-b")
        return version
    assert what == "templates"
    resume_template.TEMPLATE_VERSION += 1
    return version


@pytest.mark.parametrize("what", ["profile", "posting", "templates"])
def test_a_retry_on_changed_inputs_is_a_new_package_labelled_so(
    client, tmp_path, monkeypatch, what
):
    from backend.resume import template as resume_template

    monkeypatch.setattr(resume_template, "TEMPLATE_VERSION",
                        resume_template.TEMPLATE_VERSION)
    done = _finished("done", tmp_path, monkeypatch)
    old = db.get_package(done)
    _change(what, old["profile_version"])

    got = _retry(client, done).json()
    assert got["created"] is True and got["new_inputs"] is True
    assert got["attempt"] == 1  # of its own inputs, not attempt 2 of the old
    new = db.get_package(got["package_id"])
    assert new["retry_of"] == done
    listed = {p["id"]: p for p in client.get("/api/packages").json()["packages"]}
    assert listed[new["id"]]["inputs_changed"] == [what]
    assert listed[done]["inputs_changed"] == []
    assert db.get_package(done) == old


def test_another_attempt_of_the_same_inputs_is_not_labelled_new(
    client, tmp_path, monkeypatch
):
    done = _finished("done", tmp_path, monkeypatch)
    second = _retry(client, done).json()["package_id"]
    listed = {p["id"]: p for p in client.get("/api/packages").json()["packages"]}
    assert listed[second]["inputs_changed"] == []
    assert (listed[second]["attempt"], listed[second]["retry_of"]) == (2, done)


def _selected(client, limit=None) -> int:
    """Select job p1 the way gate 3 does, first setting the page limit
    when one is given; returns the package the selection got."""
    if limit is not None:
        assert client.put("/api/settings",
                          json={"resume_page_limit": limit}).status_code == 200
    got = client.post("/api/packages/generate", json={"job_ids": ["p1"]})
    return got.json()["package_ids"][0]


def _two_limits(client, first_status) -> tuple[int, int]:
    """Package A at page limit 1 left `first_status`, then package B for
    the same job at limit 2, done and so the job's newest. The limit is
    set back to 1: A's inputs are the current ones again."""
    _seed_job("p1")
    _confirm_profile()
    first = _selected(client, 1)
    if first_status != "queued":
        db.update_package(first, status=first_status)
    second = _selected(client, 2)
    db.update_package(second, status="done")
    assert client.put("/api/settings", json={"resume_page_limit": 1}).status_code == 200
    return first, second


def test_a_retry_on_changed_inputs_returns_their_package_while_it_is_being_built(
    client
):
    being_built, newest = _two_limits(client, "queued")

    got = _retry(client, newest).json()
    assert got == {"package_id": being_built, "created": False, "attempt": 1,
                   "new_inputs": True}
    assert len(_rows()) == 2
    assert _requests() == [(KEY, newest, being_built)]


@pytest.mark.parametrize("status", ["failed", "done"])
def test_a_retry_on_changed_inputs_builds_their_finished_package_again(
    client, status
):
    """A finished at limit 1, B finished at limit 2, the limit back at 1.
    Returning finished A would do nothing, and A's own button is gone —
    B is the job's newest. The click gets A's next attempt instead, now
    the newest and so the row the button moves to."""
    finished, newest = _two_limits(client, status)
    old = db.get_package(finished)

    got = _retry(client, newest).json()
    assert got["created"] is True and got["new_inputs"] is True
    assert got["attempt"] == 2  # of the limit-1 package, which A was
    new = db.get_package(got["package_id"])
    assert all(new[c] == old[c] for c in db.PACKAGE_IDENTITY)
    assert new["retry_of"] == newest and new["status"] == "queued"
    listed = client.get("/api/packages").json()["packages"]
    assert listed[0]["id"] == new["id"]
    assert listed[0]["inputs_changed"] == ["page limit"]
    assert db.get_package(finished) == old

    # B is history now; another click on it is refused, not a third build.
    again = _retry(client, newest, "click-0002-dddd")
    assert again.status_code == 409 and "newer package" in again.json()["detail"]
    assert len(_rows()) == 3


def test_only_the_jobs_newest_package_can_be_retried(client):
    """A is still the latest attempt of its own inputs, but B came after
    it for the same job: the list offers A no button, and a stale tab's
    click on it is refused rather than building around B."""
    older, _newest = _two_limits(client, "done")
    got = _retry(client, older)
    assert got.status_code == 409 and "newer package" in got.json()["detail"]
    assert len(_rows()) == 2 and _requests() == []


# --- An answered click keeps its answer --------------------------------------

def test_a_click_that_found_its_package_keeps_it_when_inputs_change(client):
    """The click got a package it did not make. Sent again after the page
    limit changed, it gets that package, not one for the new limit."""
    being_built, newest = _two_limits(client, "queued")
    first = _retry(client, newest).json()
    assert first["package_id"] == being_built and first["created"] is False

    assert client.put("/api/settings", json={"resume_page_limit": 3}).status_code == 200
    again = _retry(client, newest)
    assert again.status_code == 200 and again.json() == first
    assert len(_rows()) == 2
    assert _requests() == [(KEY, newest, being_built)]


def _draft_profile():
    db.save_profile({**PROFILE, "headline": "Engineer, being edited"})


def _posting_gone():
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("UPDATE jobs SET description_hash = NULL WHERE job_id = 'p1'")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("since", [_draft_profile, _posting_gone],
                         ids=["a new draft profile", "the posting no longer cached"])
def test_an_answered_click_is_answered_before_anything_else_is_checked(
    client, tmp_path, monkeypatch, since
):
    """What a new click needs — a confirmed profile, a cached posting — is
    not asked of a click already answered: its resend gets the package
    it got, not a refusal."""
    done = _finished("done", tmp_path, monkeypatch)
    first = _retry(client, done).json()
    assert first["created"] is True

    since()
    assert _retry(client, done, "click-0002-eeee").status_code == 409  # a new click
    again = _retry(client, done)
    assert again.status_code == 200
    assert again.json() == {**first, "created": False}
    assert [r[1] for r in _rows()] == [1, 2]


def test_concurrent_submissions_of_a_click_that_finds_its_package_agree(client):
    being_built, newest = _two_limits(client, "queued")
    version = db.get_package(being_built)["profile_version"]
    results = _race(8, lambda i: db.retry_package(newest, KEY, **_inputs(version)))
    assert results == [(being_built, False)] * 8
    assert _requests() == [(KEY, newest, being_built)]


# --- Restart ------------------------------------------------------------------

def test_a_restart_right_after_the_attempt_is_created_builds_it_once(
    client, tmp_path, monkeypatch
):
    """The process dies the moment the new row commits, before any worker
    starts (the client fixture's worker never runs). The restart finds it
    queued, builds it once, and the same click sent again afterwards gets
    that row, not a third."""
    from backend.tailor import pipeline as tailor_pipeline

    done = _finished("done", tmp_path, monkeypatch)
    old = db.get_package(done)
    new_id = _retry(client, done).json()["package_id"]

    db.init()  # the restart
    assert [p["id"] for p in db.packages_to_resume()] == [new_id]
    generate_calls, _ = _tailor_env(monkeypatch, tmp_path)
    assert asyncio.run(tailor_pipeline.run_batch()) == "idle"
    assert generate_calls == ["p1"]
    assert db.get_package(new_id)["status"] == "done"

    again = _retry(client, done).json()
    assert again["package_id"] == new_id and again["created"] is False
    assert [r[1] for r in _rows()] == [1, 2]
    assert db.get_package(done) == old
