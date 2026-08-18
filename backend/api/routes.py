import asyncio
import logging
import random
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from jsonschema import Draft202012Validator

from backend import sources
from backend.core import db, llm
from backend.core.events import broker
from backend.resume import facts, ingest, template
from backend.resume.schema import PROFILE_SCHEMA
from backend.scoring import llm_layer
from backend.scoring import pipeline as scoring_pipeline
from backend.tailor import compile as latex

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
    try:
        model = await llm.probe()
        return {"ok": True, "model": model}
    except llm.EngineError as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "detail": str(exc),
            "engine": llm.engine_status(),
        }


@router.post("/engine/reprobe")
async def reprobe_engine():
    llm.reset_probe()
    return await probe_engine()


@router.get("/events")
async def events():
    return StreamingResponse(broker.stream(), media_type="text/event-stream")


# --- Profile (gate 1: upload -> preview -> unlimited edits -> confirm) ------

def _profile_payload(version: int, profile: dict, confirmed: bool, **extra) -> dict:
    return {
        "version": version,
        "confirmed": confirmed,
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
    db.confirm_profile(version)
    broker.publish("profile", {"version": version, "event": "confirmed"})
    # A worker scoring against the previous profile version is now
    # wasted spend — cancel it, then re-score the current run
    # (deterministic-only when requirements are already cached; no LLM).
    _cancel_pipeline()
    _maybe_resume_scoring()
    return {"ok": True, "version": version}


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
    _pipeline_task = asyncio.create_task(_pipeline_worker(run_id))
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


async def _pipeline_worker(run_id: int) -> None:
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
    stopped = summary["stopped"]
    if stopped == "cap":
        _arm_cap_retry()  # the reset callback re-triggers scoring too
    elif stopped == "usage":
        # The engine recorded retry_at; resume once the cooldown passes.
        _arm_pipeline_retry(max(llm.seconds_until_retry(), 60.0))
    elif stopped == "engine" or (stopped is None and summary["failed"] > 0):
        # Transient engine trouble or isolated per-job failures: bounded
        # backoff, circuit-broken so a persistent failure can't loop the
        # LLM budget away. Any later trigger still resumes normally.
        if _pipeline_retry_attempts < _PIPELINE_MAX_RETRIES:
            delay = min(60.0 * (2**_pipeline_retry_attempts), 900.0)
            _pipeline_retry_attempts += 1
            _arm_pipeline_retry(delay)
        else:
            log.error(
                "scoring run %s: giving up automatic retries after %d attempts",
                run_id, _PIPELINE_MAX_RETRIES,
            )
    # session/stale_profile stops are owned by connect recovery and the
    # profile-confirm trigger respectively — no timer here.


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
    jobs the (current) profile hasn't been scored against."""
    run = db.latest_search_run()
    if run is None or run["status"] != "done":
        return False
    if scoring_pipeline.pending_count(run["id"]) == 0:
        return False
    return _spawn_pipeline(run["id"])


def _spawn_search(run_id: int) -> bool:
    """One worker per run, ever: a second Start/Resume for a run that is
    already being driven is a no-op. Returns whether a task was spawned."""
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
            return
        except sources.AuthRequired as exc:
            db.update_search_run(run_id, status="stopped_auth", message=str(exc))
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
                  _pipeline_retry_task, *_search_tasks.values()]
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
    # is an invariant, not an assumption.
    _supersede_stale_running(except_id=None)
    run_id = db.create_search_run(config)
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
            return {"jobs": [], "run_id": None, "scoring_pending": 0}
        run_id = latest["id"]
    try:
        profile_version = scoring_pipeline.confirmed_profile()[0]
    except scoring_pipeline.NoConfirmedProfile:
        profile_version = None
    return {
        # Scores join only against the current profile version + JD hash
        # — mid-rescore the UI sees "unscored", never stale numbers.
        "jobs": db.list_jobs(run_id, profile_version=profile_version),
        "run_id": run_id,
        # Remaining pipeline work — the UI shows "Scoring…" while > 0.
        "scoring_pending": scoring_pipeline.pending_count(run_id),
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


@router.post("/search/rerank")
async def search_rerank():
    """LLM-order the close matches (within the band of the top score) of
    the latest run. The deterministic baseline is never overwritten."""
    run = db.latest_search_run()
    if run is None:
        raise HTTPException(status_code=404, detail="No search run to rerank.")
    try:
        return await llm_layer.rerank_close_matches(run["id"])
    except scoring_pipeline.NoConfirmedProfile as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except llm.EngineError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
