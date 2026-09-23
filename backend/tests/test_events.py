"""SSE coverage: which durable state changes announce themselves, and how
many times. The frontend is allowed to treat the stream as invalidation
only — so a missing event is a UI that never updates, and a duplicated one
is a refetch storm."""

import asyncio

import pytest

from backend.api import routes
from backend.core import db, llm
from backend.tests import engine_fakes
from backend.core import events as events_mod
from backend.core.events import EventBroker, broker
from backend.tailor import pipeline as tailor_pipeline


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()


@pytest.fixture
def published(monkeypatch):
    """Every publish, in order, as (topic, payload)."""
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(broker, "publish",
                        lambda event, data: seen.append((event, data)))
    return seen


def test_idle_stream_heartbeats_instead_of_sitting_silent():
    """A stream with nothing to say is indistinguishable from a dead one
    to every hop in front of it, so the transport gets a keepalive."""
    async def run():
        local = EventBroker()
        stream = local.stream()
        assert await anext(stream) == ": connected\n\n"
        assert await anext(stream) == ": ping\n\n"
        await stream.aclose()

    events_mod.HEARTBEAT_S = 0.01
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=5))
    finally:
        events_mod.HEARTBEAT_S = 15.0


def test_heartbeat_is_a_comment_not_an_event():
    """EventSource never surfaces a comment, so it can never be mistaken
    for an invalidation. A named event here would become one."""
    assert ": ping\n\n".startswith(":")
    assert "event:" not in ": ping\n\n"


def test_package_status_publishes_once_even_through_park(store, published):
    package_id, _ = db.create_package("job-1", 1, "hash-1", 1,
                                      template_version=1, cover_template_version=1)
    tailor_pipeline._transition(package_id, "generating")
    assert published == [("packages", {"event": "status", "id": package_id,
                                       "status": "generating"})]

    published.clear()
    # _park routes through _transition; publishing in both would double it.
    tailor_pipeline._park(package_id, "failed", {}, "boom")
    assert len(published) == 1
    assert published[0][1]["status"] == "failed"


def test_stage_only_writes_stay_silent(store, published):
    """stage_json is internal — nothing the packages list renders moves."""
    package_id, _ = db.create_package("job-2", 1, "hash-2", 1,
                                      template_version=1, cover_template_version=1)
    tailor_pipeline._transition(package_id, stage={"tailored": {}})
    assert published == []


def test_error_only_package_writes_announce_once(store, published):
    """The waiting-for-scoring note is written with no status move — the
    row stays `queued` while the card's visible text changes."""
    package_id, _ = db.create_package("job-3", 1, "hash-3", 1,
                                      template_version=1, cover_template_version=1)
    published.clear()

    tailor_pipeline._transition(package_id, error="waiting for scoring")
    assert published == [("packages", {"event": "error", "id": package_id})]
    assert db.get_package(package_id)["status"] == "queued"

    # Re-entering the same wait on a resume sweep writes the same string.
    published.clear()
    tailor_pipeline._transition(package_id, error="waiting for scoring")
    assert published == []

    published.clear()
    tailor_pipeline._transition(package_id, error=None)
    assert published == [("packages", {"event": "error", "id": package_id})]


def test_engine_announces_changes_and_stays_quiet_otherwise(store, published):
    """A success is recorded on every LLM call; announcing unconditionally
    would put one event per scored job on the wire."""
    engine_fakes.pin("claude-opus-5")
    llm._record_problem(None, "claude", "auth_required", "not logged in")
    assert published == [("engine", {"status": "auth_required"})]

    published.clear()
    llm._record_problem(None, "claude", "auth_required", "not logged in")
    assert published == []  # byte-identical repeat: nothing on screen moved

    published.clear()
    llm._record_success(llm.dispatch_context("extract"))
    assert published == [("engine", {"status": "ok"})]

    published.clear()
    llm._record_success(llm.dispatch_context("extract"))
    assert published == []  # only the verification time moved


def test_engine_announces_metadata_changes_under_an_unchanged_status(
        store, published):
    """The banner renders the message, the model and the countdown — not
    just the status word. Deduping on status alone leaves a stale message
    on screen for the whole of a second, different failure."""
    llm._record_problem(None, "claude", "auth_required", "not logged in")
    published.clear()

    llm._record_problem(None, "claude", "auth_required", "session expired")
    assert published == [("engine", {"status": "auth_required"})]

    # A usage limit opens a quota episode.
    published.clear()
    sent_before = llm.dispatch_context("extract")
    llm._record_usage(llm.dispatch_context("extract"),
                      llm.UsageExhausted("limit reached"))
    assert published == [("engine", {"status": "waiting_usage_exhausted"})]

    # A call sent during the episode, after its cooldown, hits the limit
    # again: the retry moves out under the same status.
    published.clear()
    llm._record_usage(llm.dispatch_context("extract"),
                      llm.UsageExhausted("limit reached"))
    assert published == [("engine", {"status": "waiting_usage_exhausted"})]

    # A call sent before the episode began adds nothing: one limit, one
    # episode, one backoff step.
    published.clear()
    llm._record_usage(sent_before, llm.UsageExhausted("limit reached"))
    assert published == []

    # And so does unpinning the model, which the header badge shows.
    engine_fakes.set_engine(status=None, last_error=None, next_retry_at=None,
                            backoff_minutes=None, episode=None)
    engine_fakes.pin("claude-opus-5")
    published.clear()
    llm.reset_probe()
    assert published == [("engine", {"status": "unprobed"})]


def _route_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app)


def test_search_lifecycle_announces_outside_the_collector(store, published,
                                                          monkeypatch):
    """`run_search` reports its own page-by-page progress, but everything
    that happens around it does not: creating a run, resuming one, and the
    driver failing before collection ever starts. Behind a sign-in the
    first page can be minutes away or never arrive, so a tab that did not
    click would show the previous run the whole time."""
    monkeypatch.setattr(routes, "_spawn_search", lambda run_id: True)
    client = _route_client()

    started = client.post("/api/search/start", json={"keywords": "backend"})
    run_id = started.json()["run_id"]
    assert published == [("search", {"run_id": run_id, "event": "started"})]

    published.clear()
    db.update_search_run(run_id, status="stopped_auth", message="signed out")
    assert client.post("/api/search/resume", json={}).status_code == 200
    assert published == [("search", {"run_id": run_id, "event": "resumed"})]


async def test_driver_failures_announce_before_collection_starts(
        store, published, monkeypatch):
    """`_connect_then_search` writes three terminal statuses of its own,
    all before `run_search` — and therefore before any `progress()` call —
    can happen. Driven for real: a stub that only announced would pass
    while the driver stayed silent."""
    class Failing:
        def __init__(self, exc):
            self._exc = exc

        async def connect(self):
            raise self._exc

        async def resume(self, run_id):  # never reached
            raise AssertionError("collection should not start")

    cases = [
        (routes.sources.CaptchaStop("checkpoint"), "stopped_captcha"),
        (routes.sources.AuthRequired("signed out"), "stopped_auth"),
        (RuntimeError("driver blew up"), "stopped_error"),
    ]
    for exc, expected in cases:
        run_id = db.create_search_run({"keywords": "backend"})
        monkeypatch.setattr(routes.sources, "source", Failing(exc))
        published.clear()

        await routes._connect_then_search(run_id)

        assert db.get_search_run(run_id)["status"] == expected
        assert published == [("search", {"run_id": run_id, "event": expected})]


def test_rerank_announces_the_order_it_rewrote(store, published, monkeypatch):
    """A rerank rewrites llm_rank across a whole cluster — the order every
    other tab is rendering — with no status change to notice."""
    from backend.scoring import llm_layer

    client = _route_client()
    run_id = db.create_search_run({"keywords": "backend"})
    published.clear()

    async def fake_rerank(rid):
        return {"reranked": 4, "ordered_job_ids": ["a", "b", "c", "d"]}

    monkeypatch.setattr(llm_layer, "rerank_close_matches", fake_rerank)
    assert client.post("/api/search/rerank").json()["reranked"] == 4
    assert published == [("scoring", {"event": "reranked", "run_id": run_id,
                                      "reranked": 4})]

    # Nothing moved, nothing to invalidate.
    published.clear()

    async def empty_rerank(rid):
        return {"reranked": 0, "ordered_job_ids": []}

    monkeypatch.setattr(llm_layer, "rerank_close_matches", empty_rerank)
    assert client.post("/api/search/rerank").json()["reranked"] == 0
    assert published == []


def test_scoring_stall_announces_both_edges(store, published):
    """The engine failure fires first and the stall is written later: a
    client that only heard the engine event would look before the stall
    existed and never learn the final state."""
    routes._set_scoring_stall("engine_blocked", "no login", run_id=1,
                              profile_version=1)
    assert published == [("scoring", {"event": "stalled",
                                      "reason": "engine_blocked"})]

    published.clear()
    routes._clear_scoring_stall()
    assert published == [("scoring", {"event": "stall_cleared"})]

    published.clear()
    routes._clear_scoring_stall()
    assert published == []
