import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from backend.core import db, llm
from backend.core.events import broker
from backend.resume import facts, ingest, template
from backend.tailor import compile as latex

router = APIRouter()

PREVIEW_DIR = db.DATA_DIR / "previews"


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
    if not profile.get("name"):
        raise HTTPException(status_code=422, detail="Profile needs at least a name.")
    version = db.save_profile(profile)
    broker.publish("profile", {"version": version, "event": "edited"})
    return _profile_payload(version, profile, False)


@router.get("/profile/preview.pdf")
async def profile_preview(version: int):
    profile = db.get_profile(version)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"No profile version {version}")

    workdir = PREVIEW_DIR / f"v{version}"
    pdf_path = workdir / "resume.pdf"
    if not pdf_path.is_file():
        try:
            result = latex.compile_tex(template.render(profile), workdir, jobname="resume")
        except latex.CompileError as exc:
            raise HTTPException(status_code=500, detail=f"{exc} | {exc.log_tail[-500:]}")
        pdf_path = result.pdf_path
    return FileResponse(pdf_path, media_type="application/pdf", filename="resume-preview.pdf")


@router.post("/profile/confirm")
async def confirm_profile(body: dict):
    version = body.get("version")
    if not isinstance(version, int) or db.get_profile(version) is None:
        raise HTTPException(status_code=422, detail="Pass the profile version to confirm.")
    db.confirm_profile(version)
    broker.publish("profile", {"version": version, "event": "confirmed"})
    return {"ok": True, "version": version}
