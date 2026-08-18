"""FastAPI app factory. Serves the API and, when built, the React frontend."""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import sources
from backend.api import routes as api_routes
from backend.api.routes import router
from backend.core import db

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

# Local-only by design: resume content and a logged-in LinkedIn session live
# behind this server. Never bind 0.0.0.0.
HOST = "127.0.0.1"
PORT = 8700


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Reload any persisted captcha/error banner, then reclaim search runs
    # a previous process left 'running' — interrupted searches resume
    # across restarts without user action.
    sources.source.restore_state()
    api_routes.recover_search_runs()
    yield
    # Close the LinkedIn browser context cleanly so the persistent
    # profile in .data/browser/ isn't left mid-write.
    await sources.source.close()


def create_app() -> FastAPI:
    db.init()
    app = FastAPI(title="Job Agent", lifespan=_lifespan)
    app.include_router(router, prefix="/api")
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


def run() -> None:
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    run()
