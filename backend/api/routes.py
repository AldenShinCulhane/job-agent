from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.core import llm
from backend.core.events import broker

router = APIRouter()


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
