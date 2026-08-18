import asyncio
import logging
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
    return FileResponse(pdf_path, media_type="application/pdf", filename="resume-preview.pdf")


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
    return {"ok": True, "version": version}


# --- LinkedIn session + search (gate 2 lives inside connect) ----------------

_search_tasks: dict[int, asyncio.Task] = {}
_connect_task: asyncio.Task | None = None


def _spawn_search(run_id: int) -> bool:
    """One worker per run, ever: a second Start/Resume for a run that is
    already being driven is a no-op. Returns whether a task was spawned."""
    existing = _search_tasks.get(run_id)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(_connect_then_search(run_id))
    _search_tasks[run_id] = task
    task.add_done_callback(lambda t: _search_tasks.pop(run_id, None))
    return True


def _spawn_connect() -> None:
    global _connect_task
    if _connect_task is not None and not _connect_task.done():
        return
    _connect_task = asyncio.create_task(_connect_only())


async def _connect_then_search(run_id: int) -> None:
    """Background driver for a search run: establish the session (waiting
    for the user's sign-in if needed), then collect. Every failure lands
    in a terminal run status — the task never dies silently."""
    try:
        await sources.source.connect()
    except sources.CaptchaStop as exc:
        db.update_search_run(run_id, status="stopped_captcha", message=str(exc))
        return
    except sources.AuthRequired as exc:
        db.update_search_run(run_id, status="stopped_auth", message=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 — terminal state over silent death
        log.exception("LinkedIn connect failed for run %s", run_id)
        db.update_search_run(
            run_id, status="stopped_error", message=f"{type(exc).__name__}: {exc}"
        )
        return
    await sources.source.resume(run_id)


async def _connect_only() -> None:
    try:
        await sources.source.connect()
    except (sources.AuthRequired, sources.CaptchaStop):
        pass  # state is already persisted and bannered by the supervisor
    except Exception:  # noqa: BLE001
        log.exception("LinkedIn connect failed")


def recover_search_runs() -> list[int]:
    """Startup recovery: runs left in 'running' by a previous process are
    orphans — reclaim each with a fresh worker so an interrupted search
    resumes across restarts without user action. Returns claimed ids."""
    claimed = []
    for run in db.running_search_runs():
        if _spawn_search(run["id"]):
            claimed.append(run["id"])
            log.info("recovered interrupted search run %s", run["id"])
    return claimed


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
    config = {
        "keywords": keywords,
        "location": (body.get("location") or "").strip() or None,
        "date_posted": body.get("date_posted"),
        "workplace": body.get("workplace") or [],
        "experience": body.get("experience") or [],
        "cap": body.get("cap"),
    }
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
            return {"jobs": [], "run_id": None}
        run_id = latest["id"]
    return {"jobs": db.list_jobs(run_id), "run_id": run_id}


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
