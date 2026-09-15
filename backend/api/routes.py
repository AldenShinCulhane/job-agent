import asyncio
import json
import logging
import random
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from jsonschema import Draft202012Validator

from backend import sources
from backend.core import claude_cli, db, llm
from backend.core.events import broker
from backend.resume import facts, ingest, template
from backend.resume.schema import PROFILE_SCHEMA
from backend.scoring import llm_layer
from backend.scoring import pipeline as scoring_pipeline
from backend.tailor import compile as latex
from backend.tailor import pipeline as tailor_pipeline

log = logging.getLogger(__name__)

router = APIRouter()

PREVIEW_DIR = db.DATA_DIR / "previews"

_PROFILE_VALIDATOR = Draft202012Validator(PROFILE_SCHEMA)


def _validate_profile(profile: dict) -> None:
    """422 unless the profile matches PROFILE_SCHEMA and has a name."""
    errors = sorted(
        _PROFILE_VALIDATOR.iter_errors(profile),
        key=lambda e: list(e.absolute_path),
    )
    if errors:
        first = errors[0]
        where = "/" + "/".join(str(p) for p in first.absolute_path)
        raise HTTPException(
            status_code=422, detail=f"Profile invalid at {where}: {first.message}"
        )
    if not (profile.get("name") or "").strip():
        raise HTTPException(status_code=422, detail="Profile needs at least a name.")


@router.get("/status")
async def status():
    return {"app": "job-agent", "engine": llm.engine_status()}


@router.post("/engine/probe")
async def probe_engine():
    # With a model already pinned, probe() just echoes the pin — that is
    # NOT a health check: report the cached state honestly (ok:false
    # when the persisted engine state says the engine is unhealthy) and
    # never clear a scoring stall from here. Only an unpinned probe
    # (fresh start, or /engine/reprobe which resets the pin first)
    # actually exercises the engine and earns verified:true.
    live = db.get_state("pinned_model") is None
    if not live:
        engine = llm.engine_status()
        unhealthy = engine["status"] in ("auth_required", "config_error")
        response = {
            "ok": not unhealthy,
            "verified": False,  # cached pin — nothing was exercised
            "model": engine["model"],
            "engine": engine,
        }
        if unhealthy:
            response["error"] = engine["status"]
            response["detail"] = engine["last_error"]
        return response
    try:
        model = await llm.probe()
    except llm.EngineError as exc:
        return {
            "ok": False,
            "verified": True,
            "error": type(exc).__name__,
            "detail": str(exc),
            "engine": llm.engine_status(),
        }
    # Engine verified healthy for real: clear the stall/breaker and
    # resume scoring without any user gate.
    _on_engine_recovered()
    return {"ok": True, "verified": True, "model": model}


@router.post("/engine/reprobe")
async def reprobe_engine():
    llm.reset_probe()
    return await probe_engine()


@router.get("/settings")
async def get_settings():
    return {
        "resume_page_limit": tailor_pipeline.resume_page_limit(),
        "engine": llm.engine_status(),
        "latex_engines": [name for name, _ in latex.available_engines()],
    }


@router.put("/settings")
async def put_settings(body: dict):
    limit = body.get("resume_page_limit")
    # The page limit is the user's N, not a preset (PLAN.md). Accepted
    # range is 1..2**31-1 — an app-chosen cap (SQLite INTEGER itself is
    # signed 64-bit); rejects zero, negatives, fractions, booleans, and
    # strings.
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2**31 - 1:
        raise HTTPException(
            status_code=422,
            detail="resume_page_limit must be a positive integer (1..2147483647).",
        )
    # The limit is read once at gate 3 and pinned into each package's
    # identity, so this change only shapes future selections — packages
    # already created keep the limit they were born with.
    db.set_state(tailor_pipeline.PAGE_LIMIT_KEY, str(limit))
    return {"resume_page_limit": limit}


@router.get("/events")
async def events():
    return StreamingResponse(
        broker.stream(),
        media_type="text/event-stream",
        # no-cache so a reconnect is never served a stored stream;
        # X-Accel-Buffering is nginx's opt-out, harmless where unread.
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"},
    )


# --- Profile (gate 1: upload -> preview -> unlimited edits -> confirm) ------

def _profile_payload(version: int, profile: dict, confirmed: bool, **extra) -> dict:
    return {
        "version": version,
        "confirmed": confirmed,
        # Newest confirmed version: what the pipeline resumes on and what
        # a discard falls back to. Equals `version` once this row is
        # confirmed; lower while a draft sits on top of it (the pipeline
        # is then paused, not running on it); None before the first
        # confirm.
        "live_version": db.live_profile_version(),
        "profile": profile,
        "ledger": facts.build_ledger(profile),
        **extra,
    }


@router.post("/profile/upload")
async def upload_resume(file: UploadFile):
    suffix = Path(file.filename or "resume.pdf").suffix.lower() or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        text, method = ingest.extract_text(tmp_path)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)

    try:
        profile = await ingest.extract_profile(text)
    except llm.EngineError as exc:
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}")

    _validate_profile(profile)
    version = db.save_profile(profile)
    broker.publish("profile", {"version": version, "event": "extracted"})
    return _profile_payload(version, profile, False, extraction_method=method)


@router.get("/profile")
async def get_profile():
    latest = db.latest_profile()
    if latest is None:
        return {"version": None}
    version, profile, confirmed = latest
    return _profile_payload(version, profile, confirmed)


@router.put("/profile")
async def update_profile(profile: dict):
    _validate_profile(profile)
    version = db.save_profile(profile)
    broker.publish("profile", {"version": version, "event": "edited"})
    return _profile_payload(version, profile, False)


def _ensure_preview(version: int, profile: dict) -> Path:
    """Compile (or reuse) the preview PDF for a version, recording which
    engine produced it and its page count. Raises latex.CompileError."""
    workdir = PREVIEW_DIR / f"v{version}"
    pdf_path = workdir / "resume.pdf"
    if pdf_path.is_file() and db.get_artifact("preview", str(version)) is not None:
        return pdf_path
    result = latex.compile_tex(template.render(profile), workdir, jobname="resume")
    db.record_artifact(
        "preview", str(version), result.engine, result.pages, str(result.pdf_path)
    )
    return result.pdf_path


@router.post("/profile/discard")
async def discard_profile_draft():
    """Drop unconfirmed versions and return to the live profile."""
    result = db.discard_unconfirmed_above_live()
    if result is None:
        raise HTTPException(
            status_code=409,
            detail="No confirmed profile to fall back to — confirm this one "
                   "or upload a different resume.",
        )
    live, discarded = result
    for version in discarded:
        shutil.rmtree(PREVIEW_DIR / f"v{version}", ignore_errors=True)
        db.delete_artifact("preview", str(version))
    profile = db.get_profile(live)
    assert profile is not None, "the live version was read inside the transaction"
    broker.publish("profile", {"version": live, "event": "discarded"})
    # A draft pauses scoring by making every in-flight pass stale, and that
    # stop is deliberately given no retry timer — the profile settling is
    # the trigger. Confirm is one way it settles; discard is the other, so
    # it owes the same kick, or the pending jobs (and the packages parked
    # on their scores) sit still until some unrelated event restarts them.
    _maybe_resume_scoring()
    _maybe_resume_packages()
    return _profile_payload(live, profile, True, discarded=discarded)


@router.get("/profile/preview.pdf")
async def profile_preview(version: int):
    profile = db.get_profile(version)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile version {version}")
    try:
        pdf_path = _ensure_preview(version, profile)
    except latex.CompileError as exc:
        raise HTTPException(status_code=500, detail=f"{exc} | {exc.log_tail[-500:]}")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename="resume-preview.pdf",
        # inline, or the iframe preview triggers a download instead of
        # rendering the PDF in the pane; no-store so a browser can never
        # pin stale headers to this URL (bit us live: a cached
        # 'attachment' response kept downloading after the fix)
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/profile/confirm")
async def confirm_profile(body: dict):
    version = body.get("version")
    if not isinstance(version, int):
        raise HTTPException(status_code=422, detail="Pass the profile version to confirm.")
    profile = db.get_profile(version)
    if profile is None:
        raise HTTPException(status_code=422, detail=f"No profile version {version}")
    # A confirmed profile becomes canonical truth for tailoring — it must
    # be schema-valid and actually render before we accept it.
    _validate_profile(profile)
    try:
        _ensure_preview(version, profile)
    except latex.CompileError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Profile does not compile, fix it before confirming: {exc}",
        )
    superseded = db.confirm_profile(version)
    if superseded is None:
        raise HTTPException(
            status_code=409,
            detail=f"Profile version {version} is no longer the newest — it was "
                   "discarded or replaced while this confirmation was compiling. "
                   "Reload the profile and confirm what is there now.",
        )
    broker.publish("profile", {"version": version, "event": "confirmed"})
    if superseded:
        broker.publish("packages", {"event": "superseded", "ids": superseded})
    # A worker scoring against the previous profile version is now
    # wasted spend — cancel it, then re-score the current run
    # (deterministic-only when requirements are already cached; no LLM).
    _cancel_pipeline()
    _maybe_resume_scoring()
    # Same for a package worker: confirm_profile just superseded every
    # old-profile package, so an in-flight high-effort call would spend
    # subscription time on a row it can no longer finish. Cancel, then
    # restart whatever survived (packages already pinned to this
    # version resume from their persisted stages).
    _cancel_package_worker()
    _maybe_resume_packages()
    return _profile_payload(version, profile, True, superseded=superseded)


# --- LinkedIn session + search (gate 2 lives inside connect) ----------------

_search_tasks: dict[int, asyncio.Task] = {}
_connect_task: asyncio.Task | None = None
_cap_retry_task: asyncio.Task | None = None
_pipeline_task: asyncio.Task | None = None
# Ownership token: (run_id, profile_version) the running pass serves. A
# spawn for a different key cancels the old worker — it would otherwise
# keep spending browser/LLM budget on a run or profile the user moved
# past. The pass also re-verifies the version before every score write.
_pipeline_key: tuple[int, int] | None = None
_pipeline_retry_task: asyncio.Task | None = None
_pipeline_retry_attempts = 0
_PIPELINE_MAX_RETRIES = 3
# Persisted, user-visible reason scoring is stalled (engine login/config
# problem, or the retry budget ran dry). Carries the ownership token
# (run_id, profile_version) of the work it blocks: matching work stays
# suppressed across restarts — the circuit breaker is enforced, not just
# remembered — while a stall for a superseded run/profile is obsolete
# and cleared. Cleared on progress and on a live engine probe.
# The package batch persists the same engine_blocked stall shape under
# its own key: both are served by the same recovery loop, and a
# successful probe from either side recovers both.
_SCORING_STALL_KEY = "scoring_stall"
_PACKAGE_STALL_KEY = "package_stall"


def _stall_schedule(reason: str) -> dict:
    """Fresh recovery bookkeeping for a stall payload. `token` is the
    stall's identity: the recovery loop acts only on the exact stall it
    was armed for, so a stale timer can never clear a replacement stall.
    `next_attempt_at`/`backoff_s` persist the schedule — after a restart
    the loop sleeps only the remaining time, never a fresh full interval."""
    if reason == "engine_blocked":
        delay = _STALL_AUTH_POLL_S + random.uniform(5, 30)
    else:
        delay = _STALL_RETRY_COOLDOWN_S + random.uniform(30, 120)
    return {"token": uuid.uuid4().hex,
            "next_attempt_at": time.time() + delay,
            "backoff_s": _STALL_AUTH_POLL_S}


def _set_scoring_stall(reason: str, detail: str | None,
                       run_id: int, profile_version: int) -> str:
    payload = {"reason": reason, "detail": detail, "run_id": run_id,
               "profile_version": profile_version, **_stall_schedule(reason)}
    db.set_state(_SCORING_STALL_KEY, json.dumps(payload))
    broker.publish("scoring", {"event": "stalled", "reason": reason})
    return payload["token"]


def _set_package_stall(detail: str | None) -> str:
    """Package batches only ever stall on engine problems — usage waits
    have their own timer and other failures are isolated per package."""
    payload = {"reason": "engine_blocked", "detail": detail,
               **_stall_schedule("engine_blocked")}
    db.set_state(_PACKAGE_STALL_KEY, json.dumps(payload))
    return payload["token"]


def _update_stall_schedule(token: str, *, next_attempt_at: float,
                           backoff_s: float | None = None,
                           key: str = _SCORING_STALL_KEY) -> bool:
    """Reschedule the stall's next automatic attempt — only if it is
    still the same stall the caller was working on."""
    stall = _read_stall(key)
    if stall is None or stall.get("token") != token:
        return False
    stall["next_attempt_at"] = next_attempt_at
    if backoff_s is not None:
        stall["backoff_s"] = backoff_s
    db.set_state(key, json.dumps(stall))
    return True


def _clear_scoring_stall() -> None:
    if db.get_state(_SCORING_STALL_KEY) is None:
        return
    db.delete_state(_SCORING_STALL_KEY)
    broker.publish("scoring", {"event": "stall_cleared"})


def _clear_package_stall() -> None:
    db.delete_state(_PACKAGE_STALL_KEY)


def _read_stall(key: str) -> dict | None:
    raw = db.get_state(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _get_scoring_stall() -> dict | None:
    return _read_stall(_SCORING_STALL_KEY)


def _spawn_pipeline(run_id: int) -> bool:
    """One scoring pipeline at a time, keyed by (run, profile version)."""
    global _pipeline_task, _pipeline_key
    try:
        version, _ = scoring_pipeline.confirmed_profile()
    except scoring_pipeline.NoConfirmedProfile:
        return False
    key = (run_id, version)
    if _pipeline_task is not None and not _pipeline_task.done():
        if _pipeline_key == key:
            return False
        _pipeline_task.cancel()
    _pipeline_key = key
    _pipeline_task = asyncio.create_task(_pipeline_worker(run_id, version))
    return True


def _cancel_pipeline() -> None:
    """Stop scoring work immediately: the run set or profile it was
    scoring against is no longer current. Data-derived progress is kept."""
    global _pipeline_retry_attempts
    if _pipeline_task is not None and not _pipeline_task.done():
        _pipeline_task.cancel()
    if _pipeline_retry_task is not None and not _pipeline_retry_task.done():
        _pipeline_retry_task.cancel()
    _pipeline_retry_attempts = 0


async def _pipeline_worker(run_id: int, profile_version: int) -> None:
    global _pipeline_retry_attempts
    try:
        summary = await scoring_pipeline.run_pipeline(run_id)
    except asyncio.CancelledError:
        raise
    except scoring_pipeline.NoConfirmedProfile:
        log.info("scoring skipped for run %s: no confirmed profile", run_id)
        return
    except Exception:  # noqa: BLE001 — idempotent; next trigger retries
        log.exception("scoring pipeline failed for run %s", run_id)
        return
    if summary["scored"] > 0:
        _pipeline_retry_attempts = 0  # progress resets the circuit breaker
        _clear_scoring_stall()
    stopped = summary["stopped"]
    if stopped == "cap":
        _arm_cap_retry()  # the reset callback re-triggers scoring too
    elif stopped == "usage":
        # The engine recorded retry_at; resume once the cooldown passes.
        _arm_pipeline_retry(max(llm.seconds_until_retry(), 60.0))
    elif stopped == "engine_blocked":
        # Missing login / bad config / model entitlement: a timer can't
        # fix it. Persist the reason for the UI; a live engine probe
        # (reprobe after the user acts) resumes automatically.
        _set_scoring_stall("engine_blocked", summary["detail"],
                           run_id, profile_version)
        _arm_stall_recovery()
    elif stopped == "engine" or (stopped is None and summary["failed"] > 0):
        # Transient engine trouble or isolated per-job failures: bounded
        # backoff, circuit-broken so a persistent failure can't loop the
        # LLM budget away. Any later trigger still resumes normally.
        if _pipeline_retry_attempts < _PIPELINE_MAX_RETRIES:
            delay = min(60.0 * (2**_pipeline_retry_attempts), 900.0)
            _pipeline_retry_attempts += 1
            _arm_pipeline_retry(delay)
        else:
            _set_scoring_stall("retry_exhausted", summary["detail"],
                               run_id, profile_version)
            _arm_stall_recovery()
            log.error(
                "scoring run %s: giving up automatic retries after %d attempts",
                run_id, _PIPELINE_MAX_RETRIES,
            )
    elif stopped is None and scoring_pipeline.pending_count(run_id) == 0:
        _clear_scoring_stall()  # fully scored — nothing left to report
        # Scoring just finished deriving requirements/scores — packages
        # created before that were waiting on exactly these inputs.
        _maybe_resume_packages()
    # session/stale_profile stops are owned by connect recovery and by the
    # profile settling (confirm OR discard) respectively — no timer here.


def _on_engine_recovered() -> None:
    """A successful probe/reprobe proves the LLM engine works again:
    drop both stall banners, reset the circuit breaker, resume scoring —
    and any package batch that was parked on an engine problem."""
    global _pipeline_retry_attempts
    _pipeline_retry_attempts = 0
    _clear_scoring_stall()
    _clear_package_stall()
    _maybe_resume_scoring()
    _maybe_resume_packages()


# A persisted stall must never dead-end: this shutdown-owned loop is the
# automatic way out. engine_blocked polls the CLI auth status (free, no
# LLM spend) and only performs a live reprobe once auth looks healthy —
# with backoff on failed reprobes so a broken config can't drip-spend
# requests. retry_exhausted waits out one long cooldown, then re-opens
# the breaker for a fresh pass. The schedule lives in the stall payload
# (next_attempt_at + backoff_s), so restarts wait only the remaining
# time. The task is generation-owned: it serves exactly one stall token
# and goes inert the moment that stall is cleared or replaced. The user
# can always shortcut both with POST /search/retry-scoring (no gate —
# it's error recovery).
# The package stall (engine_blocked only) is served by the same loop
# under its own key and task slot, so each queue's recovery is owned
# independently but recovers through the same probe.
_stall_recovery_task: asyncio.Task | None = None
_stall_recovery_token: str | None = None
_package_stall_task: asyncio.Task | None = None
_package_stall_token: str | None = None
_STALL_AUTH_POLL_S = 300.0
_STALL_RETRY_COOLDOWN_S = 3600.0


def _arm_stall_recovery() -> None:
    global _stall_recovery_task, _stall_recovery_token
    stall = _get_scoring_stall()
    if stall is None:
        return
    token = stall.get("token")
    if _stall_recovery_task is not None and not _stall_recovery_task.done():
        if _stall_recovery_token == token:
            return  # already serving exactly this stall
        # Serving a superseded stall — its timer must not fire against
        # the new one (an old retry_exhausted wake-up would clear a
        # fresh engine_blocked stall and resume into a broken engine).
        _stall_recovery_task.cancel()
    _stall_recovery_token = token
    _stall_recovery_task = asyncio.create_task(_stall_recovery_loop(token))


def _arm_package_stall_recovery() -> None:
    global _package_stall_task, _package_stall_token
    stall = _read_stall(_PACKAGE_STALL_KEY)
    if stall is None:
        return
    token = stall.get("token")
    if _package_stall_task is not None and not _package_stall_task.done():
        if _package_stall_token == token:
            return
        _package_stall_task.cancel()
    _package_stall_token = token
    _package_stall_task = asyncio.create_task(
        _stall_recovery_loop(token, key=_PACKAGE_STALL_KEY))


def _stall_if_still(token: str, key: str = _SCORING_STALL_KEY) -> dict | None:
    """The persisted stall, but only while it is still the one this
    recovery pass owns — None means resolved or replaced, stop."""
    stall = _read_stall(key)
    if stall is None or stall.get("token") != token:
        return None
    return stall


async def _stall_recovery_loop(token: str,
                               key: str = _SCORING_STALL_KEY) -> None:
    while True:
        stall = _stall_if_still(token, key)
        if stall is None:
            return
        remaining = float(stall.get("next_attempt_at", 0.0)) - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)
            stall = _stall_if_still(token, key)
            if stall is None:
                return
        backoff = float(stall.get("backoff_s", _STALL_AUTH_POLL_S))
        if stall.get("reason") == "engine_blocked":
            try:
                problem = await asyncio.to_thread(claude_cli.subscription_problem)
            except asyncio.CancelledError:
                raise
            except (Exception, SystemExit) as exc:  # noqa: BLE001
                # A hung/broken CLI must not kill the recovery task —
                # log, back off (bounded, persisted), keep trying.
                log.warning("stall recovery: auth status check failed: %s: %s",
                            type(exc).__name__, exc)
                backoff = min(backoff * 2, 3600.0)
                _update_stall_schedule(
                    token,
                    next_attempt_at=time.time() + backoff + random.uniform(5, 30),
                    backoff_s=backoff,
                    key=key,
                )
                continue
            if _stall_if_still(token, key) is None:
                return
            if problem is not None:
                # Still logged out / wrong auth — keep polling on the
                # free fixed cadence.
                _update_stall_schedule(
                    token,
                    next_attempt_at=(time.time() + _STALL_AUTH_POLL_S
                                     + random.uniform(5, 30)),
                    key=key,
                )
                continue
            # Auth looks healthy: one live reprobe proves it (or doesn't —
            # config errors and the like back off so retries can't
            # drip-spend the subscription).
            llm.reset_probe()
            try:
                await llm.probe()
            except asyncio.CancelledError:
                raise
            except llm.EngineError:
                backoff = min(backoff * 2, 3600.0)
                _update_stall_schedule(
                    token,
                    next_attempt_at=time.time() + backoff + random.uniform(5, 30),
                    backoff_s=backoff,
                    key=key,
                )
                continue
            if _stall_if_still(token, key) is None:
                # Replaced while probing — the newer stall owns recovery.
                return
            _on_engine_recovered()
            return
        else:  # retry_exhausted: cooldown elapsed — one fresh pass
            global _pipeline_retry_attempts
            _pipeline_retry_attempts = 0
            _clear_scoring_stall()
            _maybe_resume_scoring()
            return


def _arm_pipeline_retry(delay: float) -> None:
    global _pipeline_retry_task
    if _pipeline_retry_task is not None and not _pipeline_retry_task.done():
        return
    _pipeline_retry_task = asyncio.create_task(_pipeline_retry_after(delay))


async def _pipeline_retry_after(delay: float) -> None:
    jittered = delay + random.uniform(5, 30)
    log.info("scoring retries in %.0f s", jittered)
    await asyncio.sleep(jittered)
    _maybe_resume_scoring()


def _maybe_resume_scoring() -> bool:
    """Kick the pipeline when the current run is done collecting but has
    jobs the (current) profile hasn't been scored against. A persisted
    stall for exactly this (run, profile) keeps the circuit open — every
    trigger, startup included, is suppressed until a live probe or a
    login action clears it; a stall for superseded work is obsolete and
    dropped."""
    run = db.latest_search_run()
    if run is None or run["status"] != "done":
        return False
    try:
        version = scoring_pipeline.confirmed_profile()[0]
    except scoring_pipeline.NoConfirmedProfile:
        return False
    stall = _get_scoring_stall()
    if stall is not None:
        if (stall.get("run_id") == run["id"]
                and stall.get("profile_version") == version):
            return False  # breaker enforced across restarts
        _clear_scoring_stall()  # different run/profile — no longer applies
    if scoring_pipeline.pending_count(run["id"]) == 0:
        return False
    return _spawn_pipeline(run["id"])


# --- Package batch (tailoring pipeline, PLAN.md §4.5/§4.6) ------------------
# One sequential batch worker; the batch loop re-reads resumable rows so
# selections queued mid-run are picked up. UsageExhausted arms a durable
# retry timer; auth/config problems park packages 'queued' and the batch
# resumes from engine recovery (probe/reprobe) — no user gate anywhere.

_package_worker_task: asyncio.Task | None = None
_package_retry_task: asyncio.Task | None = None
_PACKAGE_INPUTS_POLL_S = 300.0


def _cancel_package_worker() -> None:
    """Stop in-flight package LLM spend whose inputs were just
    superseded. The reference is dropped so a replacement can spawn
    immediately; the cancelled task unwinds on its own (run_package
    leaves the row's persisted state untouched on cancellation)."""
    global _package_worker_task
    if _package_worker_task is not None and not _package_worker_task.done():
        _package_worker_task.cancel()
    _package_worker_task = None


def _spawn_package_worker() -> bool:
    global _package_worker_task
    current = _package_worker_task
    if (current is not None and not current.done()
            and current is not asyncio.current_task()):
        return False  # running worker re-reads the queue before exiting
    # (An exiting worker may spawn its own replacement — see the
    # last-gap respawn in _package_batch_worker.)
    _package_worker_task = asyncio.create_task(_package_batch_worker())
    return True


async def _package_batch_worker() -> None:
    try:
        outcome = await tailor_pipeline.run_batch()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — worker death must not strand the queue
        log.exception("package batch worker crashed")
        return
    if outcome == "waiting_usage":
        _arm_package_retry(max(llm.seconds_until_retry(), 60.0))
    elif outcome == "engine_blocked":
        # Give the parked queue an autonomous recovery owner: persist an
        # engine stall and arm its recovery loop, exactly like scoring —
        # otherwise, with scoring already finished, nothing would ever
        # trigger the probe that resumes these packages.
        detail = next((p["error"] for p in db.packages_to_resume()
                       if p["error"]), None)
        _set_package_stall(detail)
        _arm_package_stall_recovery()
    elif outcome == "waiting_inputs":
        # Queued behind scoring. Its completion resumes packages; this
        # timer is the fallback in case that trigger is missed (cheap —
        # a not-ready pass spends zero LLM).
        _arm_package_retry(_PACKAGE_INPUTS_POLL_S)
    elif outcome == "idle" and db.packages_to_resume():
        # A selection landed in the gap between the final queue check and
        # task exit — run again rather than strand it.
        _maybe_resume_packages()


def _arm_package_retry(delay: float) -> None:
    global _package_retry_task
    if _package_retry_task is not None and not _package_retry_task.done():
        return
    _package_retry_task = asyncio.create_task(_package_retry_after(delay))


async def _package_retry_after(delay: float) -> None:
    jittered = delay + random.uniform(5, 30)
    log.info("package batch retries in %.0f s", jittered)
    await asyncio.sleep(jittered)
    _maybe_resume_packages()


def _maybe_resume_packages() -> bool:
    """Kick the package batch when resumable rows exist. A persisted
    package stall keeps the circuit open — the batch would only re-hit
    the broken engine; recovery (probe/reprobe) clears it and resumes."""
    if _read_stall(_PACKAGE_STALL_KEY) is not None:
        return False
    if not db.packages_to_resume():
        return False
    return _spawn_package_worker()


def _announce_search(run_id: int, event: str) -> None:
    """Run status changed somewhere other than the collector's own
    progress reporting: creation, an explicit resume, an automatic
    reclaim, or a driver failure before collection ever started. Each is
    invisible to `run_search`, so each has to invalidate here or a tab
    that did not click sits on a stale run indefinitely."""
    broker.publish("search", {"run_id": run_id, "event": event})


def _spawn_search(run_id: int) -> bool:
    """At most one concurrent worker per run: a second Start/Resume for a
    run that is already being driven is a no-op (a finished worker's slot
    is reusable). Returns whether a task was spawned."""
    existing = _search_tasks.get(run_id)
    if existing is not None and not existing.done():
        return False
    # Collection owns the browser/LLM budget while it runs; a scoring
    # pass for a previous run set must not keep spending beside it. The
    # completion/recovery triggers restart scoring afterwards.
    _cancel_pipeline()
    task = asyncio.create_task(_connect_then_search(run_id))
    _search_tasks[run_id] = task

    def _cleanup(finished: asyncio.Task, run_id: int = run_id) -> None:
        # Only remove our own registration — a replacement worker may
        # already occupy the slot by the time this callback runs.
        if _search_tasks.get(run_id) is finished:
            _search_tasks.pop(run_id, None)

    task.add_done_callback(_cleanup)
    return True


def _spawn_connect() -> None:
    global _connect_task
    if _connect_task is not None and not _connect_task.done():
        return
    _connect_task = asyncio.create_task(_connect_only())


async def _connect_then_search(run_id: int) -> None:
    """Background driver for a search run: establish the session (waiting
    for the user's sign-in if needed), then collect. Every failure lands
    in a terminal run status — the task never dies silently. Cancellation
    (shutdown) is the one exception: the run stays 'running' on purpose
    so startup recovery reclaims it."""
    try:
        try:
            await sources.source.connect()
        except sources.CaptchaStop as exc:
            db.update_search_run(run_id, status="stopped_captcha", message=str(exc))
            _announce_search(run_id, "stopped_captcha")
            return
        except sources.AuthRequired as exc:
            db.update_search_run(run_id, status="stopped_auth", message=str(exc))
            _announce_search(run_id, "stopped_auth")
            return
        result = await sources.source.resume(run_id)
        if result and result.get("status") == "stopped_cap":
            _arm_cap_retry()  # resumes by itself when the UTC budget resets
        elif result and result.get("status") == "done":
            _spawn_pipeline(run_id)  # scoring runs automatically — no gate
    except asyncio.CancelledError:
        raise  # shutdown: leave the run 'running' for recovery
    except Exception as exc:  # noqa: BLE001 — terminal state over silent death
        log.exception("search driver failed for run %s", run_id)
        db.update_search_run(
            run_id, status="stopped_error", message=f"{type(exc).__name__}: {exc}"
        )
        _announce_search(run_id, "stopped_error")


async def _connect_only() -> None:
    try:
        await sources.source.connect()
    except (sources.AuthRequired, sources.CaptchaStop):
        return  # state is already persisted and bannered by the supervisor
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("LinkedIn connect failed")
        return
    # Session is good again — automatically reclaim the work that was
    # blocked on it (gate 2 rule: the user performs an action, never
    # approves one; no extra Resume click). If nothing was session-
    # blocked, the current run may still be cap-stopped (captcha and cap
    # can stack): hand it to cap recovery so its retry gets armed.
    if not _reclaim_latest(("running", "stopped_auth", "stopped_captcha")):
        if not _maybe_resume_capped():
            # Collection is complete — but scoring may have been the
            # thing the dead session interrupted.
            _maybe_resume_scoring()


def _reclaim_latest(statuses: tuple[str, ...]) -> list[int]:
    """Reclaim the CURRENT (latest) run only, and only when its status is
    in `statuses`. Older runs are history — auto-resuming them would
    silently spend LinkedIn actions on searches the user moved past."""
    run = db.latest_search_run()
    if run is None or run["status"] not in statuses:
        return []
    if run["status"] != "running":
        db.update_search_run(run["id"], status="running", message=None)
        _announce_search(run["id"], "resumed")
    if _spawn_search(run["id"]):
        log.info("reclaimed search run %s (was %s)", run["id"], run["status"])
        return [run["id"]]
    return []


def _maybe_resume_capped() -> list[int]:
    """A cap-stopped current run resumes with no user input: now if
    today's budget has headroom, otherwise when the UTC counter resets."""
    run = db.latest_search_run()
    if run is None or run["status"] != "stopped_cap":
        return []
    if sources.budget_remaining() > 0:
        return _reclaim_latest(("stopped_cap",))
    _arm_cap_retry()
    return []


def _arm_cap_retry() -> None:
    global _cap_retry_task
    if _cap_retry_task is not None and not _cap_retry_task.done():
        return
    _cap_retry_task = asyncio.create_task(_cap_retry_after_reset())


async def _cap_retry_after_reset() -> None:
    delay = sources.seconds_until_cap_reset() + random.uniform(60, 300)
    log.info("daily action cap spent — search auto-resumes in %.0f min", delay / 60)
    await asyncio.sleep(delay)
    if not _reclaim_latest(("stopped_cap",)):
        # Collection already finished — the cap interrupted scoring.
        _maybe_resume_scoring()


def _supersede_stale_running(except_id: int | None) -> None:
    """One active search, ever: retire other 'running' rows (cancelling
    any worker they still have) so latest-only recovery can't strand
    them."""
    for run_id in db.supersede_running_runs(except_id):
        task = _search_tasks.get(run_id)
        if task is not None:
            task.cancel()
        log.info("superseded stale search run %s", run_id)


def recover_search_runs() -> list[int]:
    """Startup recovery for the current run: reclaim it if a previous
    process left it 'running', or arm/resume a cap-stopped one. Older
    'running' rows are retired — they would otherwise stay stranded
    forever under latest-only recovery. If a persisted captcha stop was
    restored, defer — restarting navigation against a checkpoint is a
    bypass-shaped move; the work is reclaimed when the user resolves it
    and connects."""
    latest = db.latest_search_run()
    _supersede_stale_running(latest["id"] if latest else None)
    # A stall persisted from a previous process must not dead-end:
    # restart its recovery loop before ANY early return — a captcha stop
    # must not silently swallow engine recovery (they can stack). No-op
    # when there is no stall. The package stall gets the same treatment.
    _arm_stall_recovery()
    _arm_package_stall_recovery()
    # An upgrade can land between a package being queued and being built.
    # Retire those before anything resumes them: their identity pins
    # templates this code no longer renders. Done packages are history
    # and stay exactly as published.
    resume_tpl, cover_tpl = tailor_pipeline.template_versions()
    retired = db.supersede_outdated_template_packages(resume_tpl, cover_tpl)
    if retired:
        log.info("superseded %d unfinished package(s) built for older "
                 "document templates", retired)
    # Mid-flight packages resume before the captcha check too: tailoring
    # needs no browser, so a LinkedIn checkpoint must not strand a
    # package queue that was mid-generation when the process died.
    # Completed stages are persisted — nothing is regenerated (§4.6).
    _maybe_resume_packages()
    if sources.source.current_state() == "captcha_stop":
        log.info("captcha stop persisted — deferring search recovery until resolved")
        return []
    claimed = _reclaim_latest(("running",))
    if not claimed:
        claimed = _maybe_resume_capped()
    if not claimed:
        # A previous process may have died mid-scoring: the run is done
        # but jobs remain unscored. Same auto-resume rule as search.
        _maybe_resume_scoring()
    return claimed


async def shutdown_background_tasks() -> None:
    """Cancel and await every background worker before the browser
    closes. connect() can hold the supervisor lock for up to 15 minutes
    of login polling — close() would deadlock behind it, and an
    un-cancelled search task could reopen the browser after close.
    Interrupted runs stay 'running' for startup recovery."""
    tasks = [
        t
        for t in [_connect_task, _cap_retry_task, _pipeline_task,
                  _pipeline_retry_task, _stall_recovery_task,
                  _package_worker_task, _package_retry_task,
                  _package_stall_task,
                  *_search_tasks.values()]
        # Completed tasks may belong to a previous event loop (an earlier
        # lifespan in the same process); awaiting them there raises.
        if t is not None and not t.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@router.get("/linkedin/status")
async def linkedin_status():
    return await sources.source.health()


@router.post("/linkedin/connect")
async def linkedin_connect():
    """Open the headed browser window and detect sign-in in the
    background; the UI follows progress via /linkedin/status and SSE."""
    sources.source.mark_connecting()  # visible before the task even starts
    _spawn_connect()
    return await sources.source.health()


@router.post("/search/start")
async def search_start(body: dict):
    keywords = (body.get("keywords") or "").strip()
    if not keywords:
        raise HTTPException(status_code=422, detail="Search needs keywords.")
    cap = body.get("cap")
    if cap is not None:
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 1000:
            raise HTTPException(
                status_code=422, detail="cap must be an integer between 1 and 1000."
            )
    config = {
        "keywords": keywords,
        "location": (body.get("location") or "").strip() or None,
        "date_posted": body.get("date_posted"),
        "workplace": body.get("workplace") or [],
        "experience": body.get("experience") or [],
        "cap": cap,
    }
    # Starting a search supersedes whatever was active — one active run
    # is an invariant, not an assumption. Any scoring stall described
    # the superseded run and would only mislabel the new one.
    _supersede_stale_running(except_id=None)
    _clear_scoring_stall()
    run_id = db.create_search_run(config)
    # Announce before spawning: collection can sit for minutes behind a
    # sign-in, or never start at all, and the latest run has already moved.
    _announce_search(run_id, "started")
    _spawn_search(run_id)
    return {"run_id": run_id, "status": "running"}


@router.post("/search/resume")
async def search_resume(body: dict | None = None):
    run = (
        db.get_search_run(body["run_id"])
        if body and body.get("run_id")
        else db.latest_search_run()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="No search run to resume.")
    if run["status"] == "done":
        return run
    db.update_search_run(run["id"], status="running", message=None)
    _announce_search(run["id"], "resumed")
    _spawn_search(run["id"])
    return {**run, "status": "running", "message": None}


@router.get("/search/latest")
async def search_latest():
    run = db.latest_search_run()
    return run if run is not None else {"id": None}


@router.get("/jobs")
async def jobs_list(run_id: int | None = None):
    """Jobs for a search run — the latest one by default. The global job
    cache backs detail fetches but is not what the UI lists."""
    if run_id is None:
        latest = db.latest_search_run()
        if latest is None:
            return {"jobs": [], "run_id": None, "scoring_pending": 0,
                    "scoring_stall": None}
        run_id = latest["id"]
    try:
        profile_version = scoring_pipeline.confirmed_profile()[0]
    except scoring_pipeline.NoConfirmedProfile:
        profile_version = None
    # A stall only describes THIS run under the CURRENT profile —
    # anything else is stale ownership and must not label new results.
    stall = _get_scoring_stall()
    if stall is not None and (
        stall.get("run_id") != run_id
        or stall.get("profile_version") != profile_version
    ):
        stall = None
    return {
        # Scores join only against the current profile version + JD hash
        # — mid-rescore the UI sees "unscored", never stale numbers.
        "jobs": db.list_jobs(run_id, profile_version=profile_version),
        "run_id": run_id,
        # Remaining pipeline work — the UI shows "Scoring…" while > 0.
        "scoring_pending": scoring_pipeline.pending_count(run_id),
        # Why scoring is NOT progressing, when it isn't (engine login or
        # config problem, or the automatic retry budget ran dry). The
        # recovery bookkeeping (token/schedule) is internal — not UI.
        "scoring_stall": (
            None if stall is None
            else {"reason": stall.get("reason"), "detail": stall.get("detail")}
        ),
    }


@router.post("/jobs/{job_id}/detail")
async def job_detail_fetch(job_id: str, force: bool = False):
    try:
        return await sources.source.fetch_detail(job_id, force=force)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    except sources.DailyCapReached as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except (sources.AuthRequired, sources.CaptchaStop) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except sources.DetailParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/jobs/{job_id}/explain")
async def job_explain(job_id: str):
    """On-demand LLM explanation (strengths/gaps/pitch) for a scored job."""
    try:
        return await llm_layer.explain_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    except (ValueError, scoring_pipeline.NoConfirmedProfile) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except llm.EngineError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/search/retry-scoring")
async def retry_scoring():
    """Explicit error recovery for a stalled scoring pipeline: clear the
    stall, reset the circuit breaker, resume. Not a gate — recovering
    from an error state, like the search Resume button."""
    global _pipeline_retry_attempts
    _pipeline_retry_attempts = 0
    _clear_scoring_stall()
    return {"resumed": _maybe_resume_scoring()}


@router.post("/packages/generate")
async def packages_generate(body: dict):
    """Gate 3: the user's job selection. Everything after this click is
    autonomous — generation, validation, fitting, publish."""
    job_ids = body.get("job_ids") or []
    if not isinstance(job_ids, list) or not job_ids:
        raise HTTPException(status_code=422, detail="Select at least one job.")
    # Optional, and the UI always sends it: the run the selection was made
    # against. The jobs table is a global cache spanning every run, so
    # without this an id from a result set the user is no longer looking
    # at — a search started in another tab, a click that landed while the
    # list was being replaced — resolves fine and generates the wrong
    # package. Omitted means "no run context", which stays permitted.
    run_id = body.get("run_id")
    if run_id is not None and (not isinstance(run_id, int)
                               or isinstance(run_id, bool)):
        raise HTTPException(status_code=422, detail="run_id must be an integer.")
    if run_id is not None:
        wanted = [str(j) for j in job_ids]
        members = db.job_ids_in_run(run_id, wanted)
        outsiders = [j for j in wanted if j not in members]
        if outsiders:
            raise HTTPException(
                status_code=409,
                detail=f"{len(outsiders)} selected job(s) are not in the "
                       "results you were looking at — the search results "
                       "changed. Re-select from the current list.",
            )
    try:
        version, _ = scoring_pipeline.confirmed_profile()
    except scoring_pipeline.NoConfirmedProfile as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    jobs = []
    for job_id in job_ids:
        job = db.get_job(str(job_id))
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
        if not job.get("description_hash"):
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_id} has no cached description yet — "
                       "wait for scoring or open its details first.",
            )
        jobs.append(job)
    # The page limit is read ONCE here and pinned into each package's
    # identity — generation and fitting use only the pinned value, and a
    # later settings change makes a future selection a new package
    # instead of silently returning this one. The document templates are
    # pinned the same way: published packages are immutable, so a
    # selection made after a template upgrade has to become a new package
    # or the improvement could never reach a job already generated.
    limit = tailor_pipeline.resume_page_limit()
    resume_tpl, cover_tpl = tailor_pipeline.template_versions()
    package_ids = []
    created_ids = []
    for job in jobs:
        package_id, was_created = db.create_package(
            job["job_id"], version, job["description_hash"], limit,
            template_version=resume_tpl, cover_template_version=cover_tpl)
        package_ids.append(package_id)
        if was_created:
            created_ids.append(package_id)
    created = len(created_ids)
    # Creation is idempotent: re-selecting a job returns the existing row.
    # Only the rows that are genuinely new go on the wire — a selection
    # that created nothing added nothing for another tab to fetch.
    if created_ids:
        broker.publish("packages", {"event": "created", "ids": created_ids})
    _maybe_resume_packages()
    return {"package_ids": package_ids, "created": created}


@router.get("/packages")
async def packages_list():
    return {"packages": db.list_packages(),
            "page_limit": tailor_pipeline.resume_page_limit()}


_PACKAGE_FILES = ("resume.pdf", "cover_letter.pdf", "job.md")
# Only these render in the preview pane; job.md has no inline consumer,
# so it never gets an inline disposition.
_INLINE_FILES = ("resume.pdf", "cover_letter.pdf")


@router.get("/packages/{package_id}/file/{name}")
async def package_file(package_id: int, name: str, inline: bool = False):
    if name not in _PACKAGE_FILES:
        raise HTTPException(status_code=404, detail="Unknown package file.")
    package = db.get_package(package_id)
    if package is None or package["status"] != "done" or not package["output_dir"]:
        raise HTTPException(status_code=404, detail="Package not published.")
    path = Path(package["output_dir"]) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact missing on disk.")
    if not inline or name not in _INLINE_FILES:
        return FileResponse(path, filename=name)
    # Inline for the in-page preview pane. no-store because a cached
    # 'attachment' response pins itself to the URL and keeps downloading
    # long after the header is fixed — that bit us on the profile preview.
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=name,
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/search/rerank")
async def search_rerank():
    """LLM-order the close matches (within the band of the top score) of
    the latest run. The deterministic baseline is never overwritten."""
    run = db.latest_search_run()
    if run is None:
        raise HTTPException(status_code=404, detail="No search run to rerank.")
    try:
        result = await llm_layer.rerank_close_matches(run["id"])
        # A successful rerank rewrites llm_rank across a whole cluster —
        # the order every other tab is looking at. Announced here rather
        # than in llm_layer so the scoring code stays transport-free.
        if result.get("reranked"):
            broker.publish("scoring", {"event": "reranked",
                                       "run_id": run["id"],
                                       "reranked": result["reranked"]})
        return result
    except scoring_pipeline.NoConfirmedProfile as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except llm.EngineError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
