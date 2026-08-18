"""Human-ish pacing for every LinkedIn browser action.

Honest posture (PLAN.md §4.3): pacing and daily caps reduce load and
detection surface only — automated access still violates LinkedIn's
User Agreement regardless. On captcha/checkpoint we stop cleanly and
never attempt any bypass.
"""

import asyncio
import random
from datetime import datetime, time, timedelta, timezone

from backend.core import db

# Tunables — deliberately conservative defaults.
PAGE_DELAY_RANGE_S = (4.0, 9.0)       # between result pages / detail visits
SCROLL_DELAY_RANGE_S = (0.6, 1.6)     # between scroll steps inside a page
DAILY_ACTION_CAP = 300                # page loads per calendar day (UTC)
SEARCH_PAGE_CAP = 40                  # hard stop on pagination depth per run


class DailyCapReached(Exception):
    """Raised when today's browser-action budget is spent."""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def check_daily_cap() -> int:
    """Count one browser action against today's cap. Returns actions used
    today; raises DailyCapReached when the budget is spent."""
    used = db.bump_action_count(_today())
    if used > DAILY_ACTION_CAP:
        raise DailyCapReached(
            f"Daily LinkedIn action cap reached ({DAILY_ACTION_CAP}); "
            "the run resumes tomorrow."
        )
    return used


def budget_remaining() -> int:
    """Browser actions left in today's (UTC) budget."""
    return max(0, DAILY_ACTION_CAP - db.get_action_count(_today()))


def seconds_until_cap_reset() -> float:
    """Seconds until the next UTC midnight, when the counter resets."""
    now = datetime.now(timezone.utc)
    reset = datetime.combine(
        (now + timedelta(days=1)).date(), time(0, 0), tzinfo=timezone.utc
    )
    return (reset - now).total_seconds()


async def page_delay() -> None:
    await asyncio.sleep(random.uniform(*PAGE_DELAY_RANGE_S))


async def scroll_delay() -> None:
    await asyncio.sleep(random.uniform(*SCROLL_DELAY_RANGE_S))
