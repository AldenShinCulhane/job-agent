"""Profile API contract tests: schema validation on writes and the
compile-gate on confirmation (a confirmed profile must be schema-valid
and actually render). No LLM calls; compile mocked except where noted."""

import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes
from backend.core import db
from backend.tests.engine_fakes import engine_value, set_engine
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


@pytest.fixture
def no_compile(tmp_path, monkeypatch):
    """Confirm without typesetting: these tests assert version bookkeeping,
    and requiring a LaTeX engine for that would skip them on machines that
    have none."""
    stub = tmp_path / "stub-preview.pdf"
    stub.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(routes, "_ensure_preview", lambda version, profile: stub)
    return stub


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


def test_upload_usage_failure_is_request_scoped_and_retriable(client, monkeypatch):
    """A usage limit during resume extraction is request-scoped: the
    upload fails 503 with nothing persisted and NO retry timer armed —
    the user retries after the countdown (only queued scoring/package
    work has an automatic retry owner)."""
    from backend.api import routes
    from backend.core import llm
    from backend.resume import ingest

    armed = []
    monkeypatch.setattr(routes, "_arm_pipeline_retry", lambda d: armed.append(d))
    monkeypatch.setattr(routes, "_arm_package_retry", lambda d: armed.append(d))
    monkeypatch.setattr(ingest, "extract_text", lambda path: ("resume text", "text"))
    calls = []

    async def flaky_extract(text):
        calls.append(1)
        if len(calls) == 1:
            raise llm.UsageExhausted("usage limit cooldown: retry in 300s")
        return SAMPLE_PROFILE

    monkeypatch.setattr(ingest, "extract_profile", flaky_extract)
    files = {"file": ("resume.pdf", b"%PDF-fake", "application/pdf")}
    res = client.post("/api/profile/upload", files=files)
    assert res.status_code == 503
    assert "usage limit cooldown" in res.json()["detail"]
    assert client.get("/api/profile").json()["version"] is None  # nothing persisted
    assert armed == []  # no automatic retry owner for this path

    # After the cooldown, the user's own retry succeeds cleanly.
    files = {"file": ("resume.pdf", b"%PDF-fake", "application/pdf")}
    res = client.post("/api/profile/upload", files=files)
    assert res.status_code == 200
    assert res.json()["profile"]["name"] == SAMPLE_PROFILE["name"]


def test_explain_and_rerank_usage_failures_are_request_scoped(client, monkeypatch):
    """On-demand explain and rerank surface a usage limit as 503 without
    arming any retry timer; the same action succeeds when repeated after
    the cooldown."""
    from backend.api import routes
    from backend.core import llm

    armed = []
    monkeypatch.setattr(routes, "_arm_pipeline_retry", lambda d: armed.append(d))
    monkeypatch.setattr(routes, "_arm_package_retry", lambda d: armed.append(d))

    async def usage_boom(*args, **kwargs):
        raise llm.UsageExhausted("usage limit cooldown: retry in 300s")

    monkeypatch.setattr(routes.llm_layer, "explain_job", usage_boom)
    res = client.post("/api/jobs/j1/explain")
    assert res.status_code == 503
    assert "usage limit cooldown" in res.json()["detail"]

    db.create_search_run({"keywords": "engineer"})
    monkeypatch.setattr(routes.llm_layer, "rerank_close_matches", usage_boom)
    res = client.post("/api/search/rerank")
    assert res.status_code == 503
    assert armed == []  # request-scoped on both paths

    async def ok_explain(job_id):
        return {"job_id": job_id, "strengths": [], "gaps": [], "pitch": ""}

    async def ok_rerank(run_id):
        return {"reranked": 0}

    monkeypatch.setattr(routes.llm_layer, "explain_job", ok_explain)
    monkeypatch.setattr(routes.llm_layer, "rerank_close_matches", ok_rerank)
    assert client.post("/api/jobs/j1/explain").status_code == 200
    assert client.post("/api/search/rerank").status_code == 200


def test_invalid_upload_returns_422_and_persists_nothing(client):
    """PLAN.md M5 recovery case 'invalid upload': the real ingest path
    rejects an unsupported file with a 422 whose detail the Onboarding
    page shows inline; no profile row is created and the app needs no
    recovery — the user just uploads a supported file."""
    files = {"file": ("resume.xyz", b"not a resume", "application/octet-stream")}
    res = client.post("/api/profile/upload", files=files)
    assert res.status_code == 422
    assert "Unsupported" in res.json()["detail"]
    assert client.get("/api/profile").json()["version"] is None


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
        set_engine(status=status, last_error=f"probe says {status}")
        before = snapshot()
        for _ in range(3):  # a poll is repeatable with zero side effects
            body = client.get("/api/status").json()
            assert body["engine"]["status"] == status
            assert body["engine"]["last_error"] == f"probe says {status}"
        assert snapshot() == before

    # The usage countdown derives from persisted retry_at — polling never
    # clears or rearms it.
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    set_engine(next_retry_at=retry_at)
    first = client.get("/api/status").json()["engine"]["retry_in_seconds"]
    second = client.get("/api/status").json()["engine"]["retry_in_seconds"]
    assert 0 < second <= first <= 120
    assert engine_value("next_retry_at") == retry_at

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


def test_discard_returns_to_live_and_unblocks_the_pipeline(client, no_compile):
    """An unconfirmed version is a dead end without this: it becomes the
    newest row, so `confirmed_profile()` refuses and scoring/generation
    stay blocked with no way back to the profile that was working."""
    live = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    client.post("/api/profile/confirm", json={"version": live})

    draft = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    assert draft > live
    body = client.get("/api/profile").json()
    assert (body["version"], body["confirmed"], body["live_version"]) == (draft, False, live)

    res = client.post("/api/profile/discard")
    assert res.status_code == 200
    assert res.json()["version"] == live
    assert res.json()["confirmed"] is True
    assert res.json()["discarded"] == [draft]

    after = client.get("/api/profile").json()
    assert (after["version"], after["confirmed"], after["live_version"]) == (live, True, live)
    assert db.get_profile(draft) is None


def test_discard_resumes_what_the_draft_stopped(client, no_compile, monkeypatch):
    """A draft pauses scoring by making every pass stale, and that stop is
    given no retry timer on purpose — the profile settling is the trigger.
    Discard settles it exactly as confirm does, so it owes the same kick;
    without it the pending jobs, and the packages parked on their scores,
    sit still until something unrelated happens to restart them."""
    kicked: list[str] = []
    monkeypatch.setattr(routes, "_maybe_resume_scoring",
                        lambda: kicked.append("scoring") or False)
    monkeypatch.setattr(routes, "_maybe_resume_packages",
                        lambda: kicked.append("packages") or False)

    live = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    client.post("/api/profile/confirm", json={"version": live})
    assert kicked == ["scoring", "packages"]  # confirm already did

    kicked.clear()
    client.put("/api/profile", json=SAMPLE_PROFILE)
    assert kicked == []  # a draft starts nothing

    assert client.post("/api/profile/discard").status_code == 200
    assert kicked == ["scoring", "packages"]


def test_discard_refuses_when_nothing_is_live(client, no_compile):
    """Nothing to fall back to — deleting the only version would leave the
    app with no profile at all, so the draft stands and the user is told."""
    version = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    res = client.post("/api/profile/discard")
    assert res.status_code == 409
    assert "No confirmed profile" in res.json()["detail"]
    assert client.get("/api/profile").json()["version"] == version


def test_discard_spares_confirmed_versions_and_older_drafts(client, no_compile):
    """Only unconfirmed rows ABOVE the live one are drafts. An older
    unconfirmed version is history that some later confirm stepped over,
    and deleting it would rewrite the record for nothing."""
    old_draft = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    first = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    client.post("/api/profile/confirm", json={"version": first})
    live = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    client.post("/api/profile/confirm", json={"version": live})
    new_draft = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]

    assert client.post("/api/profile/discard").json()["discarded"] == [new_draft]
    assert db.get_profile(old_draft) is not None
    assert db.get_profile(first) is not None
    assert db.get_profile(live) is not None
    assert db.get_profile(new_draft) is None


@pytest.mark.skipif(
    shutil.which("tectonic") is None and shutil.which("pdflatex") is None,
    reason="no LaTeX engine installed",
)
def test_discard_clears_the_preview_it_leaves_behind(client):
    """The artifact row is the preview cache key: leaving it while the PDF
    is gone makes `_ensure_preview` hand back a path to nothing."""
    live = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    client.post("/api/profile/confirm", json={"version": live})
    draft = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    assert client.get(f"/api/profile/preview.pdf?version={draft}").status_code == 200
    assert db.get_artifact("preview", str(draft)) is not None

    client.post("/api/profile/discard")
    assert db.get_artifact("preview", str(draft)) is None
    assert not (routes.PREVIEW_DIR / f"v{draft}").exists()
    assert db.get_artifact("preview", str(live)) is not None


def test_package_file_is_a_download_by_default_and_inline_on_request(client, tmp_path):
    """An iframe preview needs `inline`; a browser handed `attachment`
    downloads the file instead of rendering it. The existing links are
    downloads, so the default must not move."""
    out = tmp_path / "package"
    out.mkdir()
    (out / "resume.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    package_id, _ = db.create_package("job-1", 1, "hash-1", 1,
                                      template_version=1, cover_template_version=1)
    db.update_package(package_id, status="done", output_dir=str(out))

    res = client.get(f"/api/packages/{package_id}/file/resume.pdf")
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("attachment")

    res = client.get(f"/api/packages/{package_id}/file/resume.pdf?inline=1")
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("inline")
    # A cached 'attachment' response pins itself to the URL and keeps
    # downloading after the header is fixed — it bit the profile preview.
    assert res.headers["cache-control"] == "no-store"

    assert client.get(f"/api/packages/{package_id}/file/passwd?inline=1").status_code == 404


def test_confirm_loses_to_a_discard_that_lands_mid_compile(client, tmp_path, monkeypatch):
    """The confirm route reads the profile and compiles a preview before it
    writes — a window seconds wide that a discard can land inside.

    The old code ignored the UPDATE's row count: the confirm of a deleted
    version wrote nothing, then superseded every package whose version was
    not the vanished one — which is all of them — and returned 200.
    """
    live = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    stub = tmp_path / "stub-preview.pdf"
    stub.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(routes, "_ensure_preview", lambda version, profile: stub)
    client.post("/api/profile/confirm", json={"version": live})

    package_id, _ = db.create_package("job-1", live, "hash-1", 1,
                                      template_version=1, cover_template_version=1)
    draft = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]

    def discard_mid_compile(version, profile):
        db.discard_unconfirmed_above_live()
        return stub

    monkeypatch.setattr(routes, "_ensure_preview", discard_mid_compile)
    res = client.post("/api/profile/confirm", json={"version": draft})

    assert res.status_code == 409
    assert "no longer the newest" in res.json()["detail"]
    assert db.get_package(package_id)["status"] == "queued"
    assert client.get("/api/profile").json()["version"] == live


def test_confirm_returns_the_payload_the_client_needs(client, no_compile):
    """Patching `confirmed` alone left the header showing the old live
    version after a confirm; the new live version only exists server-side."""
    live = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]
    client.post("/api/profile/confirm", json={"version": live})
    draft = client.put("/api/profile", json=SAMPLE_PROFILE).json()["version"]

    body = client.post("/api/profile/confirm", json={"version": draft}).json()
    assert body["version"] == draft
    assert body["confirmed"] is True
    assert body["live_version"] == draft
    assert body["profile"]
