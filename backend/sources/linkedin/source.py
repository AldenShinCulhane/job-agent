"""LinkedIn JobSource implementation — the only v2 provider. Everything
outside `sources/` reaches LinkedIn through this class only."""

from backend.core import db
from backend.sources.base import JobSource
from backend.sources.linkedin import job_detail
from backend.sources.linkedin import search as search_mod
from backend.sources.linkedin.supervisor import supervisor


class LinkedInSource(JobSource):
    def __init__(self) -> None:
        self._supervisor = supervisor

    async def connect(self) -> dict:
        """Gate 2: open the headed window and wait for the user's sign-in."""
        return await self._supervisor.connect(wait_for_user=True)

    async def search(self, config: dict) -> dict:
        run_id = db.create_search_run(config)
        return await search_mod.run_search(self._supervisor, run_id)

    async def resume(self, run_id: int) -> dict:
        db.update_search_run(run_id, status="running")
        return await search_mod.run_search(self._supervisor, run_id)

    async def fetch_detail(self, job_id: str, force: bool = False) -> dict:
        return await job_detail.fetch_detail(self._supervisor, job_id, force=force)

    async def health(self) -> dict:
        return self._supervisor.status()

    async def close(self) -> None:
        await self._supervisor.close()
