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
