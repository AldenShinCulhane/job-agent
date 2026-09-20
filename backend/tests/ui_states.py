"""Named, deterministic UI states over the fixtures.

Every matrix check — unexpected console errors, horizontal overflow, axe —
runs across all of them, in both colour schemes where colour matters. A
state is a fixture table, a tab, what proves the tab has finished loading,
and any actions that get the page into the state.
"""

from dataclasses import dataclass, field
from typing import Callable

from backend.tests.ui_harness import (
    API_FIXTURES, ENGINE, Response, _job, _package, profile_payload,
    route_fixtures,
)

SEARCH_READY = ("text=Backend Engineer a", "text=Ready", "text=Daily cap reached")


@dataclass
class State:
    name: str
    tab: str = "Profile"
    fixtures: dict = field(default_factory=dict)
    ready: tuple[str, ...] = ("text=Your profile",)
    setup: Callable | None = None
    # Substrings of console errors this state is EXPECTED to log — the
    # deliberately failing fixtures. Anything else is unexpected.
    allow_console: tuple[str, ...] = ()


def _engine(**over):
    return {"/api/status": {"app": "job-agent", "engine": {**ENGINE, **over}}}


def _dirty(page):
    page.get_by_label("Name", exact=True).fill("Sample Person B")
    page.wait_for_selector("text=Unsaved edits")


def _busy(page):
    # A save that never answers: the editor is disabled for the duration.
    page.route("**/api/profile", lambda route, *_: (
        None if route.request.method == "PUT" else route.fallback()))
    _dirty(page)
    page.get_by_role("button", name="Save & re-render").click()
    page.wait_for_selector("text=Re-rendering preview…")


def _replacing(page):
    page.get_by_role("button", name="Replace resume").click()
    page.wait_for_selector("text=Choose a new resume")


def _fields_open(page):
    page.get_by_role("button", name="Edit search").click()
    page.wait_for_selector("[placeholder]")


def _selected(page):
    page.get_by_role("checkbox", name="Select Backend Engineer a", exact=True).check()
    page.get_by_role("checkbox", name="Select Backend Engineer b", exact=True).check()
    page.wait_for_selector("text=2 selected")


def _expanded(page):
    page.get_by_role("button", name="Details").first.click()
    page.wait_for_selector("text=Build APIs.")
    page.get_by_role("button", name="Explain fit").click()
    page.wait_for_selector("text=A close fit on the backend work.")


def _generate_error(page):
    page.get_by_role("checkbox", name="Select Backend Engineer a", exact=True).check()
    page.get_by_role("button", name="Generate packages").click()
    page.get_by_role("alert").wait_for()


def _history_open(page):
    page.get_by_text("Earlier packages (25)").click()
    page.wait_for_selector("text=Role 30")


_SCORING_JOBS = {"/api/jobs": dict(API_FIXTURES["/api/jobs"], scoring_pending=2)}
_RUNNING = {"/api/search/latest": dict(API_FIXTURES["/api/search/latest"],
                                       status="running", message=None)}
_QUEUED = {"/api/packages": {"page_limit": 1,
                             "packages": [_package(i, "queued") for i in range(1, 31)]},
           "/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[])}

STATES: list[State] = [
    State("profile-upload-prompt", fixtures={"/api/profile": {"version": None}},
          ready=("text=Upload your resume",)),
    State("profile-draft", ready=("text=Your profile", "text=Draft v5")),
    State("profile-confirmed",
          fixtures={"/api/profile": profile_payload(confirmed=True, live=5)},
          ready=("text=Live v5", "text=Confirmed")),
    State("profile-dirty", ready=("text=Draft v5",), setup=_dirty),
    State("profile-busy", ready=("text=Draft v5",), setup=_busy),
    State("profile-replacing", ready=("text=Draft v5",), setup=_replacing),
    State("search-empty", tab="Search",
          fixtures={"/api/search/latest": {"id": None},
                    "/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[], run_id=None)},
          ready=("text=No jobs collected yet.",)),
    State("search-populated", tab="Search", ready=SEARCH_READY),
    State("search-fields-open", tab="Search", ready=SEARCH_READY, setup=_fields_open),
    State("search-running", tab="Search", fixtures={**_RUNNING, **_SCORING_JOBS},
          ready=("text=Scoring… 2/4", "text=Searching…")),
    State("search-stalled", tab="Search",
          fixtures={"/api/jobs": dict(API_FIXTURES["/api/jobs"], scoring_pending=2,
                                      scoring_stall={"reason": "engine_blocked",
                                                     "detail": "sign in"})},
          ready=("text=Scoring paused",)),
    State("search-selected", tab="Search", ready=SEARCH_READY, setup=_selected),
    State("search-expanded", tab="Search", ready=SEARCH_READY, setup=_expanded),
    State("search-generate-error", tab="Search", ready=SEARCH_READY,
          fixtures={("POST", "/api/packages/generate"):
                    Response(503, {"detail": "engine unavailable"})},
          setup=_generate_error, allow_console=("status of 503",)),
    State("search-stale-run", tab="Search",
          fixtures={"/api/search/latest": dict(API_FIXTURES["/api/search/latest"],
                                               id=8, status="running", message=None)},
          ready=("text=Backend Engineer a", "text=Searching…")),
    State("packages-bulk", tab="Search", fixtures=_QUEUED,
          ready=("text=Earlier packages (25)", "text=30 in progress")),
    State("packages-history-open", tab="Search", fixtures=_QUEUED,
          ready=("text=Earlier packages (25)",), setup=_history_open),
    State("banner-config-error", tab="Search",
          fixtures=_engine(status="config_error", last_error="bad model pin"),
          ready=("text=Claude engine configuration error", *SEARCH_READY)),
    State("banner-usage-wait", tab="Search",
          fixtures=_engine(status="waiting_usage_exhausted", retry_in_seconds=600,
                           last_error=None),
          ready=("text=usage limit", *SEARCH_READY)),
    State("banner-captcha", tab="Search",
          fixtures={"/api/linkedin/status": {"state": "captcha_stop",
                                             "detail": "checkpoint"}},
          ready=("text=LinkedIn checkpoint hit", "text=Backend Engineer a")),
    State("linkedin-ready", tab="Search",
          fixtures={"/api/linkedin/status": {"state": "ready", "detail": None}},
          ready=("text=Browser access and account risk", "text=Backend Engineer a")),
    State("settings-default", tab="Settings",
          ready=("text=Documents", "text=No LaTeX engine found")),
    State("settings-engines", tab="Settings",
          fixtures={"/api/settings": {"resume_page_limit": 2, "engine": ENGINE,
                                      "latex_engines": ["tectonic", "pdflatex"]},
                    **_engine(status="ok", model="claude-sample", last_error=None)},
          ready=("text=Available: tectonic, pdflatex", "text=Claude ready")),
    State("settings-read-failed", tab="Settings",
          fixtures={"/api/settings": Response(500, {"detail": "settings store unavailable"})},
          ready=("text=settings store unavailable", "text=Appearance"),
          allow_console=("status of 500",)),
]

STATE_IDS = [s.name for s in STATES]


def open_state(browser, dist_url, state: State, *, width=1280, height=900,
               scheme="light"):
    """A page in `state`. Returns (context, page, unexpected console errors)."""
    context = browser.new_context(color_scheme=scheme,
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []

    def record(text: str):
        if not any(allowed in text for allowed in state.allow_console):
            errors.append(text)

    page.on("pageerror", lambda e: record(str(e)))
    page.on("console", lambda m: record(m.text) if m.type == "error" else None)
    table = dict(API_FIXTURES)
    table.update(state.fixtures)
    route_fixtures(page, table)
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required") if "/api/status" not in state.fixtures \
        else None
    page.get_by_role("button", name=state.tab, exact=True).click()
    for selector in state.ready:
        page.wait_for_selector(selector)
    if state.setup:
        state.setup(page)
    return context, page, errors
