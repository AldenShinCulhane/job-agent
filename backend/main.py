"""FastAPI app factory. Serves the API and, when built, the React frontend."""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import sources
from backend.api import routes as api_routes
from backend.api.routes import router
from backend.core import db, llm

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
    # Engine sign-in state is kept current by the backend itself: an idle
    # UI stops polling, so it cannot be the one to notice a lapsed login.
    api_routes.start_engine_monitor()
    yield
    # Cancel background workers FIRST — connect() can hold the supervisor
    # lock for minutes of login polling, and close() would deadlock
    # behind it. Interrupted runs stay 'running' for the next startup's
    # recovery. Then close the browser context cleanly so the persistent
    # profile in .data/browser/ isn't left mid-write.
    await api_routes.shutdown_background_tasks()
    await sources.source.close()


def create_app() -> FastAPI:
    db.init()
    # Engine state from before the second engine lived under global keys.
    llm.migrate_legacy_state()
    app = FastAPI(title="Job Agent", lifespan=_lifespan)
    app.include_router(router, prefix="/api")
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


def run() -> None:
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    run()
