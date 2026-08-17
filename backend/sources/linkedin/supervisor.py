"""Single persistent-context owner for ALL LinkedIn browser work.

One Playwright persistent context lives in `.data/browser/`; every
action (login, search pages, detail fetches) is serialized through one
asyncio lock — no concurrent profile locks, no separate storage_state
files. All automation runs headed (consistent fingerprint; the window
may be minimized). This class is the only code path that touches the
browser.

On captcha/checkpoint detection we stop cleanly, persist state, and
surface a banner — never any bypass attempt (PLAN.md §4.3).
"""

import asyncio
import logging
from pathlib import Path

from backend.core import db
from backend.core.events import broker
from backend.sources.linkedin import pacing, selectors

log = logging.getLogger(__name__)

BROWSER_DIR = db.DATA_DIR / "browser"
FEED_URL = "https://www.linkedin.com/feed/"
_NAV_TIMEOUT_MS = 45_000
_LOGIN_POLL_S = 3.0
_LOGIN_WAIT_S = 15 * 60  # gate 2: how long we wait for the user to sign in
_SCROLL_STEPS = 8


class SupervisorError(Exception):
    """Base class for supervisor failures."""


class AuthRequired(SupervisorError):
    """LinkedIn session missing/expired — user must sign in (gate 2)."""


class CaptchaStop(SupervisorError):
    """Captcha/checkpoint/restriction hit — stopped cleanly, no bypass."""


class BrowserUnavailable(SupervisorError):
    """Playwright/Chromium not installed or failed to launch."""


class LinkedInSupervisor:
    """States: idle -> ready | logged_out | captcha_stop | error."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._page = None
        self.state = "idle"
        self.detail: str | None = None

    # --- lifecycle ----------------------------------------------------------

    async def _ensure_context(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            self._set_state("error", "Playwright is not installed (uv sync).")
            raise BrowserUnavailable(str(exc))
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            BROWSER_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_DIR),
                headless=False,  # all LinkedIn automation runs headed
                viewport={"width": 1440, "height": 900},
            )
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            self._page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
            return self._page
        except BrowserUnavailable:
            raise
        except Exception as exc:
            self._set_state("error", f"Browser failed to launch: {exc}")
            raise BrowserUnavailable(str(exc))

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:  # noqa: BLE001 — shutdown is best-effort
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
            self._context = self._page = self._playwright = None
            if self.state not in ("captcha_stop",):
                self._set_state("idle", None)

    def _set_state(self, state: str, detail: str | None) -> None:
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            broker.publish("linkedin", {"state": state, "detail": detail})

    # --- session ------------------------------------------------------------

    async def _classify_current_page(self, page) -> str:
        url = page.url or ""
        if selectors.is_checkpoint_url(url):
            return "captcha_stop"
        html = await page.content()
        soup = selectors.parse_html(html)
        if selectors.select_first(soup, "logged_in_marker") is not None:
            return "ready"
        if selectors.select_first(soup, "login_form") is not None or "/login" in url:
            return "logged_out"
        return "unknown"

    async def _ensure_logged_in(self, page, wait_for_user: bool) -> None:
        """Navigate to the feed and verify the session. When logged out
        and `wait_for_user` is set, keep the window open and poll until
        the user finishes signing in (gate 2) — no decision prompt, the
        session is detected automatically."""
        await page.goto(FEED_URL, wait_until="domcontentloaded")
        verdict = await self._classify_current_page(page)
        if verdict == "captcha_stop":
            self._set_state(
                "captcha_stop",
                "LinkedIn is showing a security checkpoint. Complete it in the "
                "browser window; automation stays stopped and never bypasses it.",
            )
            raise CaptchaStop(self.detail)
        if verdict == "ready":
            self._set_state("ready", None)
            return
        self._set_state(
            "logged_out", "Sign in to LinkedIn in the browser window we opened."
        )
        if not wait_for_user:
            raise AuthRequired(self.detail)
        waited = 0.0
        while waited < _LOGIN_WAIT_S:
            await asyncio.sleep(_LOGIN_POLL_S)
            waited += _LOGIN_POLL_S
            verdict = await self._classify_current_page(page)
            if verdict == "ready":
                self._set_state("ready", None)
                return
            if verdict == "captcha_stop":
                self._set_state(
                    "captcha_stop",
                    "LinkedIn is showing a security checkpoint. Complete it in "
                    "the browser window; automation stays stopped.",
                )
                raise CaptchaStop(self.detail)
        raise AuthRequired("Timed out waiting for LinkedIn sign-in.")

    # --- public API (each call serialized; one browser action at a time) ----

    async def connect(self, wait_for_user: bool = True) -> dict:
        """Gate 2 entry point: open the window and verify/establish the
        session."""
        async with self._lock:
            page = await self._ensure_context()
            await self._ensure_logged_in(page, wait_for_user=wait_for_user)
            return self.status()

    async def fetch_page(self, url: str) -> str:
        """Navigate to `url` (logged in, paced, capped), scroll to load
        lazy content, and return the full HTML."""
        async with self._lock:
            pacing.check_daily_cap()
            page = await self._ensure_context()
            if self.state != "ready":
                await self._ensure_logged_in(page, wait_for_user=False)
            await page.goto(url, wait_until="domcontentloaded")
            if selectors.is_checkpoint_url(page.url or ""):
                self._set_state(
                    "captcha_stop",
                    "LinkedIn is showing a security checkpoint. Complete it in "
                    "the browser window; automation stays stopped.",
                )
                raise CaptchaStop(self.detail)
            for _ in range(_SCROLL_STEPS):
                # The jobs list is its own scroll container; nudge it and
                # the window so lazy cards render either way.
                await page.evaluate(
                    "() => { const el = document.querySelector("
                    "'.jobs-search-results-list, .scaffold-layout__list');"
                    " if (el) el.scrollBy(0, 800); window.scrollBy(0, 800); }"
                )
                await pacing.scroll_delay()
            return await page.content()

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail}


# One supervisor per process — module-level singleton, same pattern as
# the LLM engine.
supervisor = LinkedInSupervisor()
