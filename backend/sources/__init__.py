"""Job-source registry. Routes import from here only — provider
internals (LinkedIn today) stay swappable behind the JobSource
interface."""

from backend.sources.linkedin.job_detail import DetailParseError
from backend.sources.linkedin.pacing import (
    DailyCapReached,
    budget_remaining,
    seconds_until_cap_reset,
)
from backend.sources.linkedin.source import LinkedInSource
from backend.sources.linkedin.supervisor import AuthRequired, CaptchaStop

source = LinkedInSource()

__all__ = [
    "AuthRequired",
    "CaptchaStop",
    "DailyCapReached",
    "DetailParseError",
    "LinkedInSource",
    "budget_remaining",
    "seconds_until_cap_reset",
    "source",
]