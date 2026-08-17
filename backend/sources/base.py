"""JobSource interface. LinkedIn is the only v2 implementation, but
nothing outside `sources/` imports LinkedIn directly — the provider
stays swappable if LinkedIn becomes untenable."""

from abc import ABC, abstractmethod


class JobSource(ABC):
    @abstractmethod
    async def search(self, config: dict) -> dict:
        """Run (or resume) a search described by `config`, collecting
        results into the jobs table up to the configured cap. Returns the
        final search-run record."""

    @abstractmethod
    async def fetch_detail(self, job_id: str) -> dict:
        """Return the job with a fresh-enough full description, fetching
        and caching it if missing or stale."""

    @abstractmethod
    async def health(self) -> dict:
        """Current source status: {'state': ..., 'detail': ...}."""
