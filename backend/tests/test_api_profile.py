"""Profile API contract tests: schema validation on writes and the
compile-gate on confirmation (a confirmed profile must be schema-valid
and actually render). No LLM calls; compile mocked except where noted."""

import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes
from backend.core import db
from backend.tailor import compile as latex
from backend.tests.test_resume import SAMPLE_PROFILE


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(routes, "PREVIEW_DIR", tmp_path / "previews")
    db.init()
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app)


def test_put_rejects_schema_invalid_profile(client):
    res = client.put("/api/profile", json={"name": "X"})
    assert res.status_code == 422
    assert "Profile invalid at" in res.json()["detail"]

    broken = {**SAMPLE_PROFILE, "experiences": [{"title": "Engineer"}]}
    res = client.put("/api/profile", json=broken)
    assert res.status_code == 422
    assert "/experiences/0" in res.json()["detail"]


def test_put_accepts_valid_profile(client):
    res = client.put("/api/profile", json=SAMPLE_PROFILE)
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body["version"], int)
    assert body["ledger"]


def test_confirm_blocked_when_compile_fails(client, monkeypatch):
    version = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]

    def boom(*args, **kwargs):
        raise latex.CompileError("engine exploded", log_tail="...")

    monkeypatch.setattr(latex, "compile_tex", boom)
    res = client.post("/api/profile/confirm", json={"version": version})
    assert res.status_code == 422
    assert "does not compile" in res.json()["detail"]
    assert client.get("/api/profile").json()["confirmed"] is False


def test_confirm_blocked_for_schema_invalid_stored_version(client):
    version = db.save_profile({"name": "X"})  # bypasses PUT validation
    res = client.post("/api/profile/confirm", json={"version": version})
    assert res.status_code == 422
    assert "Profile invalid at" in res.json()["detail"]

    res = client.post("/api/profile/confirm", json={"version": version + 99})
    assert res.status_code == 422


@pytest.mark.skipif(
    shutil.which("tectonic") is None and shutil.which("pdflatex") is None,
    reason="no LaTeX engine installed",
)
def test_confirm_compiles_and_records_provenance(client):
    version = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    res = client.post("/api/profile/confirm", json={"version": version})
    assert res.status_code == 200

    artifact = db.get_artifact("preview", str(version))
    assert artifact is not None
    assert artifact["engine"] in ("tectonic", "pdflatex")
    assert artifact["pages"] >= 1
    assert (routes.PREVIEW_DIR / f"v{version}" / "resume.pdf").is_file()
    assert client.get("/api/profile").json()["confirmed"] is True


def test_settings_roundtrip_and_validation(client):
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["resume_page_limit"] == 1  # default
    assert "status" in body["engine"]
    assert isinstance(body["latex_engines"], list)

    # Any strictly positive integer is a valid N (PLAN.md), not a
    # preset. The cap is app-chosen 2**31-1 (SQLite INTEGER is signed
    # 64-bit; this is not its limit) — both boundaries accepted.
    for good in (1, 2, 4, 100, 2**31 - 1):
        assert (
            client.put("/api/settings", json={"resume_page_limit": good}).status_code
            == 200
        )
        assert client.get("/api/settings").json()["resume_page_limit"] == good

    from backend.tailor import pipeline as tailor_pipeline

    assert tailor_pipeline.resume_page_limit() == 2**31 - 1  # gate 3 reads this

    for bad in (0, -1, 2.5, "2", True, False, None, 2**31):
        res = client.put("/api/settings", json={"resume_page_limit": bad})
        assert res.status_code == 422, bad
    assert client.get("/api/settings").json()["resume_page_limit"] == 2**31 - 1


def test_status_polls_are_pure_and_cover_recovery_states(client, monkeypatch):
    """The two 20-second polls behind the recovery banners — GET
    /api/status and GET /api/linkedin/status — are read-only: no worker
    spawn, no run resume, no engine-state write, in any app state. They
    also carry every status value the banners key on."""
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from backend.api import routes
    from backend.sources.linkedin.supervisor import supervisor

    def snapshot():
        with sqlite3.connect(db.DB_PATH) as conn:
            return sorted(conn.execute("SELECT key, value FROM engine_state"))

    tasks_before = dict(routes._search_tasks)

    # Engine-side banner states surface verbatim on /api/status.
    for status in ("auth_required", "config_error", "waiting_usage_exhausted"):
        db.set_state("engine_status", status)
        db.set_state("last_error", f"probe says {status}")
        before = snapshot()
        for _ in range(3):  # a poll is repeatable with zero side effects
            body = client.get("/api/status").json()
            assert body["engine"]["status"] == status
            assert body["engine"]["last_error"] == f"probe says {status}"
        assert snapshot() == before

    # The usage countdown derives from persisted retry_at — polling never
    # clears or rearms it.
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    db.set_state("retry_at", retry_at)
    first = client.get("/api/status").json()["engine"]["retry_in_seconds"]
    second = client.get("/api/status").json()["engine"]["retry_in_seconds"]
    assert 0 < second <= first <= 120
    assert db.get_state("retry_at") == retry_at

    # LinkedIn-side banner states surface verbatim on /api/linkedin/status.
    for state in ("captcha_stop", "logged_out"):
        monkeypatch.setattr(supervisor, "state", state)
        monkeypatch.setattr(supervisor, "detail", "checkpoint hit")
        before = snapshot()
        for _ in range(3):
            body = client.get("/api/linkedin/status").json()
            assert body == {"state": state, "detail": "checkpoint hit"}
        assert snapshot() == before

    # No poll ever started or resumed a worker.
    assert dict(routes._search_tasks) == tasks_before
