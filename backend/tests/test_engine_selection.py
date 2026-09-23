"""Engine selection, execution identity and health (PLAN.md §4.1a, P6a).

No real engine is called. Both providers' transports are replaced by a
scripted stand-in that records the context each call was sent with, and
the local sign-in checks by a switchable fake, so every test can say
exactly what was spent.

The acceptance case the reviewer set for P6a: a call to model A is in
flight, the user selects model B, then A succeeds or fails. A's answer
keeps A as its provenance, and nothing A reports can change B's badge or
B's wait.
"""

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes
from backend.core import codex_engine, db, llm
from backend.core.events import broker
from backend.tailor import pipeline as tailor_pipeline
from backend.tests import engine_fakes

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}},
          "required": ["x"]}
ANSWER = {"x": "y"}

CLAUDE_A = "claude-opus-5"
CLAUDE_B = "claude-sonnet-5"
CODEX = "gpt-test-codex"


# --- stand-ins ---------------------------------------------------------------

class Held:
    """One call held open until the test decides how it ends."""

    def __init__(self):
        self.sent = asyncio.Event()
        self._end = asyncio.get_running_loop().create_future()

    def succeed(self, value):
        self._end.set_result(value)

    def fail(self, exc):
        self._end.set_exception(exc)

    async def __call__(self):
        self.sent.set()
        return await self._end


class Calls:
    """Both engines' transports. Each call records the context it was sent
    with; outcomes are consumed in order: an exception is raised, a Held
    waits for the test, anything else is the answer."""

    def __init__(self, monkeypatch):
        self.sent: list[llm.CallContext] = []
        self.outcomes: list = []
        self.verified: list[str] = []

        async def claude_call(ctx, prompt, schema, system_prompt):
            return await self._next(ctx), None

        async def codex_call(ctx, prompt, schema, system_prompt):
            return await self._next(ctx), None

        async def codex_probe(ctx):
            await self._next(ctx)

        async def ensure_verified(model):
            self.verified.append(model)
            return "fp-" + model

        monkeypatch.setattr(llm, "_claude_call", claude_call)
        monkeypatch.setattr(codex_engine, "call", codex_call)
        monkeypatch.setattr(codex_engine, "probe", codex_probe)
        monkeypatch.setattr(codex_engine, "ensure_verified", ensure_verified)

    async def _next(self, ctx):
        self.sent.append(ctx)
        if not self.outcomes:
            raise AssertionError(f"unscripted call on {ctx}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Held):
            outcome = await outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SignIn:
    """The local credential checks: `problem[provider]` is None when signed
    in, else (kind, text) as llm.credential_problem returns it."""

    def __init__(self, monkeypatch):
        self.problem: dict = {"claude": None, "codex": None}
        self.checks: list[str] = []
        self.delay = 0.0
        self.crash: Exception | None = None
        monkeypatch.setattr(llm, "credential_problem", self._check)

    def _check(self, provider):
        self.checks.append(provider)
        if self.delay:
            time.sleep(self.delay)
        if self.crash is not None:
            raise self.crash
        return self.problem[provider]


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    monkeypatch.setattr(llm, "_semaphore", None)
    monkeypatch.setattr(llm, "_credential_checks", {})
    return SimpleNamespace(calls=Calls(monkeypatch), signin=SignIn(monkeypatch))


@pytest.fixture
def published(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(broker, "publish",
                        lambda event, data: seen.append((event, data)))
    return seen


def known_ok(provider, model):
    """A model already proven to answer on this plan."""
    llm._set_model_state(provider, model, {"status": "ok", "last_error": None,
                                           "verified_at": None})


def usage(text="usage limit reached", resets_at=None):
    return llm.UsageExhausted(text, resets_at=resets_at)


async def dispatch_held(engine, task="extract"):
    """Send one structured call and hold it in flight."""
    held = Held()
    engine.calls.outcomes.append(held)
    call = asyncio.create_task(llm.structured("p", SCHEMA, task=task))
    await asyncio.wait_for(held.sent.wait(), 5)
    return held, call


def state_rows():
    with sqlite3.connect(db.DB_PATH) as conn:
        return dict(conn.execute(
            "SELECT key, value FROM engine_state ORDER BY key").fetchall())


# --- execution identity: the acceptance case --------------------------------

async def test_a_call_in_flight_keeps_its_engine_when_the_user_switches(engine):
    engine_fakes.pin(CLAUDE_A)
    held, call = await dispatch_held(engine, task="tailor")

    llm.select("codex", CODEX)
    codex_before = llm.engines_view()["providers"]["codex"]
    held.succeed(ANSWER)
    answer = await call

    assert answer == ANSWER
    assert answer.context == llm.CallContext("claude", CLAUDE_A, "high",
                                             gen=answer.context.gen)
    # The ledger is written from the answer, not from the selection.
    assert tailor_pipeline._ledger_entry("generation", answer) == {
        "call": "generation", "provider": "claude", "model": CLAUDE_A,
        "effort": "high"}
    assert llm.engines_view()["providers"]["codex"] == codex_before
    status = llm.engine_status()
    assert (status["provider"], status["model"], status["status"]) == (
        "codex", CODEX, "unprobed")


@pytest.mark.parametrize("failure, claude_status", [
    (usage(), "waiting_usage_exhausted"),
    (llm.AuthRequired("OAuth session expired"), "auth_required"),
    (llm.ConfigError("tool lockdown breached"), "config_error"),
    (llm.ModelNotEntitled("model not_found"), "not_entitled"),
])
async def test_a_failure_in_flight_cannot_touch_the_newly_selected_engine(
        engine, failure, claude_status):
    engine_fakes.pin(CLAUDE_A)
    held, call = await dispatch_held(engine)

    llm.select("codex", CODEX)
    codex_before = llm.engines_view()["providers"]["codex"]
    held.fail(failure)
    with pytest.raises(type(failure)):
        await call

    # The failure is A's: recorded against Claude and model A.
    assert llm.provider_status("claude", CLAUDE_A)["status"] == claude_status
    # B's badge and wait are exactly as they were.
    assert llm.engines_view()["providers"]["codex"] == codex_before
    status = llm.engine_status()
    assert status["status"] == "unprobed"
    assert status["retry_in_seconds"] is None and status["episode"] is None
    assert llm.seconds_until_retry() == 0.0

    # And B runs: its first call probes it, then answers on B.
    engine.calls.outcomes += ["ok", ANSWER]
    answer = await llm.structured("p", SCHEMA, task="extract")
    assert answer.context == llm.CallContext(
        "codex", CODEX, "medium", gen=answer.context.gen, fingerprint="fp-" + CODEX)
    assert [c.provider for c in engine.calls.sent] == ["claude", "codex", "codex"]


async def test_a_codex_failure_in_flight_cannot_touch_claude(engine):
    llm.select("codex", CODEX)
    known_ok("codex", CODEX)
    held, call = await dispatch_held(engine)

    llm.select("claude", None)
    engine_fakes.pin(CLAUDE_A)
    claude_before = llm.engines_view()["providers"]["claude"]
    held.fail(usage())
    with pytest.raises(llm.UsageExhausted):
        await call

    assert llm.provider_status("codex", CODEX)["status"] == "waiting_usage_exhausted"
    assert llm.engines_view()["providers"]["claude"] == claude_before
    assert llm.engine_status()["status"] == "ok"


async def test_same_provider_switch_keeps_the_new_models_badge(engine):
    """Entitlement is per model: A turning out not to be on the plan says
    nothing about B."""
    engine_fakes.pin(CLAUDE_A)
    known_ok("claude", CLAUDE_B)
    held, call = await dispatch_held(engine)

    llm.select("claude", CLAUDE_B)
    held.fail(llm.ModelNotEntitled("model not_found"))
    with pytest.raises(llm.ModelNotEntitled):
        await call

    assert llm.provider_status("claude", CLAUDE_A)["status"] == "not_entitled"
    status = llm.engine_status()
    assert (status["model"], status["status"], status["last_error"]) == (
        CLAUDE_B, "ok", None)


async def test_same_provider_success_in_flight_is_recorded_for_its_own_model(engine):
    engine_fakes.pin(CLAUDE_A)
    held, call = await dispatch_held(engine)

    llm.select("claude", CLAUDE_B)
    held.succeed(ANSWER)
    answer = await call

    assert answer.context.model == CLAUDE_A
    assert llm.provider_status("claude", CLAUDE_A)["status"] == "ok"
    # B was never sent anything, so nothing is known about it yet.
    assert llm.engine_status()["status"] == "unprobed"


async def test_package_ledger_keeps_each_calls_own_engine(
        engine, monkeypatch, tmp_path):
    """Through the package pipeline: generation dispatched on A keeps A in
    the ledger although the user switched to B while it ran; the checks
    sent afterwards run on B and say so."""
    from backend.tests.test_tailor import (
        _confirm_profile, _create_package, _seed_derived, _seed_job, _tailor_env)

    engine_fakes.pin(CLAUDE_A)
    known_ok("codex", CODEX)
    version = _confirm_profile()
    _seed_job("p1")
    _seed_derived("p1", version)
    pkg, _ = _create_package("p1", version, "hash-a")

    def switch_mid_generation(job):
        from backend.tests.test_tailor import _cited_tailored
        llm.select("codex", CODEX)
        return _cited_tailored()[0]

    _tailor_env(monkeypatch, tmp_path, behavior=switch_mid_generation)
    assert await tailor_pipeline.run_package(pkg) == "done"

    calls = db.get_package(pkg)["stage"]["llm_calls"]
    generation = [c for c in calls if c["call"] == "generation"]
    entailment = [c for c in calls if c["call"] == "entailment"]
    assert [(c["provider"], c["model"]) for c in generation] == [("claude", CLAUDE_A)]
    assert entailment and all((c["provider"], c["model"]) == ("codex", CODEX)
                              for c in entailment)


# --- an explicit choice never falls back -------------------------------------

async def test_an_explicit_claude_model_never_falls_back(engine):
    llm.select("claude", llm.MODEL_PREFERRED)
    engine.calls.outcomes.append(llm.ModelNotEntitled("model not_found"))
    with pytest.raises(llm.ModelNotEntitled):
        await llm.structured("p", SCHEMA, task="extract")

    # One probe, on the chosen model only, at the weakest effort.
    assert [(c.model, c.effort) for c in engine.calls.sent] == [
        (llm.MODEL_PREFERRED, "low")]
    assert llm.selection() == {"provider": "claude", "model": llm.MODEL_PREFERRED}
    assert engine_fakes.engine_value("auto_model") is None
    assert llm.engine_status()["status"] == "not_entitled"

    # Known not to be on the plan: the next call spends nothing.
    with pytest.raises(llm.ModelNotEntitled):
        await llm.structured("p", SCHEMA, task="extract")
    assert len(engine.calls.sent) == 1

    # Re-check forgets it and probes again.
    llm.reset_probe()
    engine.calls.outcomes += ["ok", ANSWER]
    assert await llm.structured("p", SCHEMA, task="extract") == ANSWER
    assert len(engine.calls.sent) == 3


async def test_an_explicit_codex_model_never_falls_back(engine):
    llm.select("codex", CODEX)
    engine.calls.outcomes.append(llm.ModelNotEntitled("model_not_found"))
    with pytest.raises(llm.ModelNotEntitled):
        await llm.structured("p", SCHEMA, task="extract")

    assert [(c.provider, c.model) for c in engine.calls.sent] == [("codex", CODEX)]
    assert engine.calls.verified == []  # never got as far as a real call
    assert llm.selection() == {"provider": "codex", "model": CODEX}
    assert llm.provider_status("claude", None)["status"] == "unprobed"


async def test_only_the_default_selection_probes_fable_then_opus(engine):
    engine.calls.outcomes += [llm.ModelNotEntitled("not_found"), "ok", ANSWER]
    answer = await llm.structured("p", SCHEMA, task="extract")
    assert [c.model for c in engine.calls.sent] == [
        llm.MODEL_PREFERRED, llm.MODEL_FALLBACK, llm.MODEL_FALLBACK]
    assert answer.context.model == llm.MODEL_FALLBACK
    assert llm.selection() == {"provider": "claude", "model": None}


# --- usage-limit episodes -----------------------------------------------------

async def test_concurrent_limit_hits_are_one_episode_and_one_backoff_step(engine):
    engine_fakes.pin(CLAUDE_A)
    first, first_call = await dispatch_held(engine)
    second, second_call = await dispatch_held(engine)

    first.fail(usage())
    with pytest.raises(llm.UsageExhausted):
        await first_call
    episode = llm.engine_status()["episode"]
    retry_at = engine_fakes.engine_value("next_retry_at")
    assert episode is not None

    second.fail(usage())
    with pytest.raises(llm.UsageExhausted):
        await second_call
    assert llm.engine_status()["episode"] == episode
    assert engine_fakes.engine_value("next_retry_at") == retry_at
    assert engine_fakes.engine_value("backoff_minutes") == "10"


async def test_a_success_sent_before_the_limit_cannot_end_it(engine):
    engine_fakes.pin(CLAUDE_A)
    failing, failing_call = await dispatch_held(engine)
    late, late_call = await dispatch_held(engine)

    failing.fail(usage())
    with pytest.raises(llm.UsageExhausted):
        await failing_call
    before = llm.engine_status()

    late.succeed(ANSWER)
    assert await late_call == ANSWER
    after = llm.engine_status()
    assert after["status"] == "waiting_usage_exhausted"
    assert (after["episode"], after["next_retry_at"]) == (
        before["episode"], before["next_retry_at"])


def _cooldown_over():
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.set_state("engine.claude.next_retry_at", past)


async def test_a_hit_after_the_cooldown_extends_the_same_episode(engine):
    engine_fakes.pin(CLAUDE_A)
    engine.calls.outcomes.append(usage())
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    episode = llm.engine_status()["episode"]

    _cooldown_over()
    engine.calls.outcomes.append(usage())
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")

    status = llm.engine_status()
    assert status["episode"] == episode
    assert status["retry_in_seconds"] > 9 * 60  # the doubled step
    assert engine_fakes.engine_value("backoff_minutes") == "20"


async def test_a_success_after_the_wait_ends_the_episode(engine):
    engine_fakes.pin(CLAUDE_A)
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    engine.calls.outcomes.append(usage(resets_at=reset))
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")

    _cooldown_over()
    engine.calls.outcomes.append(ANSWER)
    await llm.structured("p", SCHEMA, task="extract")
    status = llm.engine_status()
    assert status["status"] == "ok"
    assert (status["episode"], status["next_retry_at"], status["limit_resets_at"],
            status["last_error"]) == (None, None, None, None)
    assert engine_fakes.engine_value("backoff_minutes") is None


async def test_switching_models_within_a_provider_keeps_the_limit(engine):
    engine_fakes.pin(CLAUDE_A)
    engine.calls.outcomes.append(usage())
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    episode = llm.engine_status()["episode"]

    llm.select("claude", CLAUDE_B)
    status = llm.engine_status()
    assert status["status"] == "waiting_usage_exhausted"
    assert status["episode"] == episode and status["retry_in_seconds"] > 0
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    assert len(engine.calls.sent) == 1  # nothing spent on B while waiting


async def test_rechecking_the_engine_keeps_the_limit(engine):
    engine_fakes.pin(CLAUDE_A)
    engine.calls.outcomes.append(usage())
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    before = llm.engine_status()

    llm.reset_probe()
    after = llm.engine_status()
    assert after["status"] == "waiting_usage_exhausted"
    assert (after["episode"], after["next_retry_at"]) == (
        before["episode"], before["next_retry_at"])


async def test_a_sign_in_problem_during_a_limit_clears_back_to_waiting(engine):
    """Signing in again ends the sign-in problem, not the limit."""
    engine_fakes.pin(CLAUDE_A)
    engine.calls.outcomes.append(usage())
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    episode = llm.engine_status()["episode"]

    engine.signin.problem["claude"] = ("auth", "not signed in — run: login")
    await llm.refresh_credentials("claude")
    assert llm.engine_status()["status"] == "auth_required"

    engine.signin.problem["claude"] = None
    await llm.refresh_credentials("claude")
    status = llm.engine_status()
    assert (status["status"], status["episode"]) == ("waiting_usage_exhausted", episode)

    # Same through a re-check after the service rejected the login.
    llm._record_problem(None, "claude", "auth_required", "401 Unauthorized")
    llm.reset_probe()
    assert llm.engine_status()["status"] == "waiting_usage_exhausted"


# --- reported reset vs retry estimate ----------------------------------------

async def test_a_reported_reset_is_kept_apart_from_the_retry_estimate(engine):
    engine_fakes.pin(CLAUDE_A)
    far = datetime.now(timezone.utc) + timedelta(hours=3)
    engine.calls.outcomes.append(usage(resets_at=far))
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    status = llm.engine_status()
    assert status["limit_resets_at"] == far.isoformat()
    # Retrying before the reported reset would only spend requests.
    assert status["next_retry_at"] == far.isoformat()


async def test_a_near_reset_keeps_the_backoff_as_the_retry_estimate(engine):
    engine_fakes.pin(CLAUDE_A)
    near = datetime.now(timezone.utc) + timedelta(minutes=1)
    engine.calls.outcomes.append(usage(resets_at=near))
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    status = llm.engine_status()
    assert status["limit_resets_at"] == near.isoformat()
    retry_at = datetime.fromisoformat(status["next_retry_at"])
    assert retry_at - near > timedelta(minutes=3)


async def test_an_unreported_codex_reset_stays_unknown(engine):
    llm.select("codex", CODEX)
    known_ok("codex", CODEX)
    engine.calls.outcomes.append(usage("You've hit your usage limit. Try again later."))
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    status = llm.engine_status()
    assert status["status"] == "waiting_usage_exhausted"
    assert status["limit_resets_at"] is None
    assert status["next_retry_at"] is not None
    assert llm.provider_status("claude", None)["episode"] is None


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def test_claude_reset_epoch_form():
    epoch = int((NOW + timedelta(hours=2)).timestamp())
    assert llm.parse_claude_reset(f"Claude AI usage limit reached|{epoch}", NOW) == (
        NOW + timedelta(hours=2))
    stale = int((NOW - timedelta(hours=2)).timestamp())
    assert llm.parse_claude_reset(f"Claude AI usage limit reached|{stale}", NOW) is None


def test_claude_reset_clock_form_uses_the_named_zone():
    text = "You've hit your limit · resets 3pm (America/Toronto)"
    # 08:00 in Toronto: later the same day.
    assert llm.parse_claude_reset(text, NOW) == datetime(2026, 9, 22, 19, 0,
                                                         tzinfo=timezone.utc)
    # 16:00 in Toronto: already past, so tomorrow.
    assert llm.parse_claude_reset(text, NOW + timedelta(hours=8)) == datetime(
        2026, 9, 23, 19, 0, tzinfo=timezone.utc)
    half = llm.parse_claude_reset("resets at 9:30am (America/Toronto)", NOW)
    assert half == datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)


def test_claude_reset_unknown_zone_falls_back_to_local_time():
    reset = llm.parse_claude_reset("resets 11:30am (Nowhere/Nothing)", NOW)
    local = reset.astimezone()
    assert (local.hour, local.minute) == (11, 30)
    assert timedelta(0) < reset - NOW <= timedelta(days=1)


def test_claude_reset_is_never_guessed():
    for text in ("usage limit reached", "rate limit exceeded, try again soon", ""):
        assert llm.parse_claude_reset(text, NOW) is None


def test_claude_usage_text_carries_its_reset():
    epoch = int(time.time()) + 3600
    exc = llm._engine_error(llm.UsageExhausted, f"Claude AI usage limit reached|{epoch}")
    assert isinstance(exc, llm.UsageExhausted)
    assert exc.resets_at == datetime.fromtimestamp(epoch, timezone.utc)
    assert llm._engine_error(llm.UsageExhausted, "usage limit").resets_at is None


# --- effort -------------------------------------------------------------------

def test_effort_is_clamped_to_what_the_model_supports(engine):
    llm._put_json("claude", "catalog", [
        {"id": "three-levels", "efforts": ["low", "medium", "high"]},
        {"id": "no-effort", "efforts": []},
        {"id": "strong-only", "efforts": ["high", "xhigh"]},
        {"id": "with-max", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    ])
    assert llm.effort_for("repair", "claude", "three-levels") == "high"
    assert llm.effort_for("extract", "claude", "three-levels") == "medium"
    assert llm.effort_for("extract", "claude", "no-effort") is None
    assert llm.effort_for("extract", "claude", "strong-only") == "high"
    assert llm.effort_for("layout_final", "claude", "with-max") == "xhigh"
    # Not in the catalog: the tier is passed as-is.
    assert llm.effort_for("repair", "claude", "uncatalogued") == "xhigh"
    # The probe spends the least the model allows.
    assert llm._weakest("claude", "strong-only") == "high"
    assert llm._weakest("claude", "no-effort") is None


def test_a_model_without_effort_control_is_sent_none():
    assert llm._options("no-effort", None, "s", SCHEMA).effort is None
    assert llm._options("m", "high", "s", SCHEMA).effort == "high"


# --- credentials: local, cheap, coalesced ------------------------------------

async def test_concurrent_credential_checks_are_coalesced(engine):
    engine.signin.delay = 0.2
    results = await asyncio.gather(llm.refresh_credentials("claude"),
                                   llm.refresh_credentials("claude"),
                                   llm.refresh_credentials("codex"))
    assert results == [None, None, None]
    assert sorted(engine.signin.checks) == ["claude", "codex"]


async def test_a_recent_credential_check_is_reused(engine):
    await llm.refresh_credentials("claude", max_age=60)
    await llm.refresh_credentials("claude", max_age=60)
    assert engine.signin.checks == ["claude"]
    await llm.refresh_credentials("claude")  # max_age 0 always checks
    assert engine.signin.checks == ["claude", "claude"]


async def test_a_sign_in_problem_the_check_found_clears_when_it_is_fixed(engine):
    engine_fakes.pin(CLAUDE_A)
    engine.signin.problem["claude"] = ("auth", "not signed in — run: login")
    assert await llm.refresh_credentials("claude") == "not signed in — run: login"
    status = llm.engine_status()
    assert (status["status"], status["last_error"]) == (
        "auth_required", "not signed in — run: login")
    assert llm.credentials_present("claude") is False

    engine.signin.problem["claude"] = None
    assert await llm.refresh_credentials("claude") is None
    assert llm.engine_status()["status"] == "ok"
    assert llm.credentials_present("claude") is True


async def test_a_setup_problem_the_check_found_clears_when_it_is_fixed(engine):
    llm.select("codex", CODEX)
    known_ok("codex", CODEX)
    engine.signin.problem["codex"] = ("setup", "Codex CLI not found on PATH")
    await llm.refresh_credentials("codex")
    assert llm.engine_status()["status"] == "config_error"

    engine.signin.problem["codex"] = None
    await llm.refresh_credentials("codex")
    assert llm.engine_status()["status"] == "ok"


async def test_present_credentials_never_clear_what_the_service_reported(engine):
    engine_fakes.pin(CLAUDE_A)
    engine.calls.outcomes.append(llm.AuthRequired("OAuth token revoked"))
    with pytest.raises(llm.AuthRequired):
        await llm.structured("p", SCHEMA, task="extract")

    await llm.refresh_credentials("claude")
    status = llm.engine_status()
    assert (status["status"], status["last_error"]) == (
        "auth_required", "OAuth token revoked")

    # Only a call that works proves the service accepts the login again.
    engine.calls.outcomes.append(ANSWER)
    await llm.structured("p", SCHEMA, task="extract")
    assert llm.engine_status()["status"] == "ok"


@pytest.mark.parametrize("status, error", [
    ("waiting_usage_exhausted", "usage limit reached"),
    ("config_error", "tool lockdown breached"),
])
async def test_present_credentials_never_clear_a_limit_or_a_breach(
        engine, status, error):
    engine_fakes.pin(CLAUDE_A)
    engine_fakes.set_engine(status=status, last_error=error)
    await llm.refresh_credentials("claude")
    await llm.refresh_credentials("claude")
    assert (llm.engine_status()["status"], llm.engine_status()["last_error"]) == (
        status, error)


async def test_a_crashing_check_is_a_setup_problem(engine):
    engine.signin.crash = RuntimeError("status hung")
    problem = await llm.refresh_credentials("claude")
    assert "credentials check failed" in problem and "status hung" in problem
    assert llm.provider_status("claude", None)["status"] == "config_error"


@pytest.mark.parametrize("provider, kind, raised", [
    ("claude", "auth", llm.AuthRequired),
    ("codex", "auth", llm.AuthRequired),
    ("codex", "setup", llm.ConfigError),
])
async def test_a_missing_login_is_caught_before_anything_is_sent(
        engine, provider, kind, raised):
    if provider == "codex":
        llm.select("codex", CODEX)
        known_ok("codex", CODEX)
    else:
        engine_fakes.pin(CLAUDE_A)
    engine.signin.problem[provider] = (kind, "sign in first")
    with pytest.raises(raised, match="sign in first"):
        await llm.structured("p", SCHEMA, task="extract")
    assert engine.calls.sent == [] and engine.calls.verified == []


async def test_a_tool_call_fails_the_call_but_not_the_engine(engine):
    """The engine refused a tool the model reached for: that prompt fails
    alone; the engine stays healthy and nothing is parked."""
    llm.select("codex", CODEX)
    known_ok("codex", CODEX)
    engine.calls.outcomes.append(llm.ToolCallBlocked("the model tried to call a tool"))
    with pytest.raises(llm.ToolCallBlocked):
        await llm.structured("p", SCHEMA, task="extract")
    status = llm.engine_status()
    assert (status["status"], status["last_error"], status["episode"]) == (
        "ok", None, None)


# --- the engine monitor -------------------------------------------------------

async def test_monitor_keeps_both_logins_current_and_rechecks_a_restored_one(
        engine, monkeypatch):
    rechecks = []

    async def fake_probe_engine():
        rechecks.append(llm.engine_status()["provider"])
        return {"ok": True}

    monkeypatch.setattr(routes, "probe_engine", fake_probe_engine)
    llm.select("codex", CODEX)
    engine.signin.problem["codex"] = ("auth", "not signed in to Codex")
    engine.signin.problem["claude"] = ("auth", "not signed in")

    await routes.engine_monitor_pass()
    assert sorted(engine.signin.checks) == ["claude", "codex"]
    assert llm.engine_status()["status"] == "auth_required"
    assert rechecks == []

    # Claude's login comes back: not selected, so nothing is re-checked.
    engine.signin.problem["claude"] = None
    await routes.engine_monitor_pass()
    assert rechecks == []
    assert llm.provider_status("claude", None)["status"] == "unprobed"

    # The selected engine's login comes back: one live re-check.
    engine.signin.problem["codex"] = None
    await routes.engine_monitor_pass()
    assert rechecks == ["codex"]
    await routes.engine_monitor_pass()
    assert rechecks == ["codex"]  # once per restore, not every pass


async def test_monitor_survives_a_failing_check(engine, monkeypatch):
    async def broken(provider, *, max_age=0.0):
        if provider == "claude":
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(llm, "refresh_credentials", broken)
    await routes.engine_monitor_pass()  # no exception escapes


# --- legacy state --------------------------------------------------------------

def test_legacy_single_engine_state_migrates_once(engine):
    retry_at = (datetime.now(timezone.utc) + timedelta(minutes=7)).isoformat()
    for key, value in {"engine_status": "waiting_usage_exhausted",
                       "pinned_model": CLAUDE_A, "retry_at": retry_at,
                       "last_error": "usage limit reached",
                       "usage_backoff_minutes": "20"}.items():
        db.set_state(key, value)

    llm.migrate_legacy_state()
    status = llm.engine_status()
    assert (status["provider"], status["model"], status["status"]) == (
        "claude", CLAUDE_A, "waiting_usage_exhausted")
    assert status["next_retry_at"] == retry_at and status["episode"]
    assert engine_fakes.engine_value("backoff_minutes") == "20"
    for key in llm._LEGACY_KEYS:
        assert db.get_state(key) is None

    after_first = state_rows()
    llm.migrate_legacy_state()
    assert state_rows() == after_first


def test_legacy_sign_in_problem_and_bad_pin(engine):
    db.set_state("engine_status", "auth_required")
    db.set_state("last_error", "Not logged in")
    db.set_state("pinned_model", "bad model; rm -rf /")
    llm.migrate_legacy_state()
    status = llm.engine_status()
    assert (status["status"], status["last_error"]) == ("auth_required", "Not logged in")
    assert engine_fakes.engine_value("auto_model") is None


# --- selection --------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "{not json",
    json.dumps({"provider": "gemini", "model": "x"}),
    json.dumps({"provider": "codex", "model": None}),
    json.dumps({"provider": "codex", "model": "x; rm -rf /"}),
])
def test_a_damaged_selection_reads_as_the_default(engine, raw):
    db.set_state("engine.selection", raw)
    assert llm.selection() == {"provider": "claude", "model": None}


async def test_a_changed_model_list_announces_and_an_unchanged_one_does_not(
        engine, published, monkeypatch):
    listed = [{"id": CLAUDE_A, "label": "Opus", "description": "", "efforts": []}]

    async def models():
        return listed

    monkeypatch.setattr(llm.claude_cli, "models", models)
    await llm.refresh_catalog("claude")
    assert published == [("engine", {"status": "unprobed"})]
    published.clear()
    await llm.refresh_catalog("claude")
    assert published == []


def test_selecting_announces_once(engine, published):
    llm.select("codex", CODEX)
    assert published == [("engine", {"status": "unprobed"})]
    published.clear()
    llm.select("codex", CODEX)
    assert published == []


# --- routes -------------------------------------------------------------------------

@pytest.fixture
def client(engine, monkeypatch):
    recovered = []
    monkeypatch.setattr(routes, "_on_engine_recovered", lambda: recovered.append(1))
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return SimpleNamespace(http=TestClient(app), recovered=recovered, **vars(engine))


@pytest.mark.parametrize("body", [
    {"provider": "gemini", "model": "x"},
    {"provider": "codex", "model": None},
    {"provider": "claude", "model": "bad id; rm -rf /"},
    {"provider": 3, "model": None},
    {"provider": "claude", "model": 5},
    {},
])
def test_put_selection_rejects_bad_choices(client, body):
    res = client.http.put("/api/engine/selection", json=body)
    assert res.status_code == 422
    assert llm.selection() == {"provider": "claude", "model": None}
    assert client.calls.sent == [] and client.recovered == []


def test_put_selection_on_a_known_good_model_resumes_parked_work(client):
    known_ok("claude", CLAUDE_B)
    res = client.http.put("/api/engine/selection",
                          json={"provider": "claude", "model": CLAUDE_B})
    body = res.json()
    assert res.status_code == 200
    assert (body["ok"], body["verified"], body["model"]) == (True, False, CLAUDE_B)
    assert body["selection"] == {"provider": "claude", "model": CLAUDE_B}
    assert client.recovered == [1]
    assert client.calls.sent == []  # nothing needed proving


def test_put_selection_on_a_new_model_proves_it_first(client):
    client.calls.outcomes.append("ok")
    body = client.http.put("/api/engine/selection",
                           json={"provider": "codex", "model": CODEX}).json()
    assert (body["ok"], body["verified"], body["model"]) == (True, True, CODEX)
    assert [(c.provider, c.model) for c in client.calls.sent] == [("codex", CODEX)]
    assert client.recovered == [1]


def test_put_selection_keeps_the_choice_when_the_model_fails(client):
    client.calls.outcomes.append(llm.ModelNotEntitled("model_not_found"))
    body = client.http.put("/api/engine/selection",
                           json={"provider": "codex", "model": CODEX}).json()
    assert (body["ok"], body["error"]) == (False, "ModelNotEntitled")
    # The app never switches engines by itself.
    assert llm.selection() == {"provider": "codex", "model": CODEX}
    assert client.recovered == []


def test_put_selection_on_a_waiting_provider_spends_nothing(client):
    engine_fakes.pin(CLAUDE_A)
    llm._record_usage(llm.dispatch_context("extract"), usage())
    body = client.http.put("/api/engine/selection",
                           json={"provider": "claude", "model": CLAUDE_B}).json()
    assert (body["ok"], body["error"]) == (False, "waiting_usage_exhausted")
    assert client.calls.sent == [] and client.recovered == []


def test_catalog_refresh_keeps_the_last_good_list(client, monkeypatch):
    listed = [{"id": CLAUDE_A, "label": "Opus", "description": "", "efforts": ["low"]}]

    async def models():
        return listed

    monkeypatch.setattr(llm.claude_cli, "models", models)
    view = client.http.post("/api/engines/claude/catalog").json()
    assert view["providers"]["claude"]["catalog"] == listed

    async def failing():
        raise RuntimeError("handshake timed out")

    monkeypatch.setattr(llm.claude_cli, "models", failing)
    view = client.http.post("/api/engines/claude/catalog").json()
    claude = view["providers"]["claude"]
    assert claude["catalog"] == listed
    assert "handshake timed out" in claude["catalog_error"]

    assert client.http.post("/api/engines/gemini/catalog").status_code == 404
    assert client.calls.sent == []
