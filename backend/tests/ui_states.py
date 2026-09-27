"""Named, deterministic UI states over the fixtures.

Every matrix check — unexpected console errors, horizontal overflow, axe —
runs across all of them, in both colour schemes where colour matters. A
state is a fixture table, a tab, what proves the tab has finished loading,
and any actions that get the page into the state.
"""

from dataclasses import dataclass, field
from typing import Callable

from backend.tests.ui_harness import (
    API_FIXTURES, ENGINE, SIGNED_IN, Response, _job, _package, engines_view,
    profile_payload, route_fixtures,
)

SEARCH_READY = ("text=Backend Engineer a", "text=Ready", "text=Daily cap reached")


@dataclass
class State:
    name: str
    tab: str = "Profile"
    fixtures: dict = field(default_factory=dict)
    ready: tuple[str, ...] = ("text=Your profile",)
    setup: Callable | None = None
    # Endpoints this state deliberately makes fail, whose "Failed to load
    # resource" console error is therefore expected. Nothing else is: a
    # failure on any other endpoint, and every uncaught page error, counts.
    allow_failed_requests: tuple[str, ...] = ()


LOCKDOWN_FAILURE = ("Codex lockdown check failed for gpt-mini: input is not the "
                    "user message alone")


def _engine(codex=None, selection=None, **claude):
    """The engines view with Claude's state changed. A Claude that is not
    asking for a sign-in is signed in."""
    if claude.get("status", "auth_required") != "auth_required":
        claude.setdefault("credentials", SIGNED_IN)
    return {"/api/engines": engines_view(claude=claude, codex=codex,
                                         selection=selection)}


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


def _custom_model(page):
    page.get_by_label("Model for every task").select_option("custom")
    page.get_by_label("Model ID").fill("not a model id")
    page.wait_for_selector("text=Letters, digits")


def _history_open(page):
    page.get_by_text("Earlier packages (25)").click()
    page.wait_for_selector("text=Role 30")


_SCORING_JOBS = {"/api/jobs": dict(API_FIXTURES["/api/jobs"], scoring_pending=2)}
_RUNNING = {"/api/search/latest": dict(API_FIXTURES["/api/search/latest"],
                                       status="running", message=None)}
_QUEUED = {"/api/packages": {"page_limit": 1,
                             "packages": [_package(i, "queued") for i in range(1, 31)]},
           "/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[])}

# Retried packages: another attempt of the same inputs (with the one it
# replaced below it, now history), and a new package because the
# profile changed. Only each job's newest row offers Try again or
# Regenerate.
ATTEMPTS = {"/api/packages": {"page_limit": 1, "packages": [
    _package(12, "failed", "compile failed", job="jx", attempt=2, retry_of=11,
             title="Role X"),
    _package(11, "failed", "compile failed", job="jx", title="Role X"),
    _package(10, "done", job="jy", retry_of=8, inputs_changed=("profile",),
             title="Role Y"),
    _package(9, "generating"),
    _package(8, "done", job="jy", title="Role Y"),
]}}


def _retry_refused(page):
    page.get_by_role("button", name="Try again Role X").click()
    page.wait_for_selector("text=a newer package for this job already exists")


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
          setup=_generate_error, allow_failed_requests=("/api/packages/generate",)),
    State("search-stale-run", tab="Search",
          fixtures={"/api/search/latest": dict(API_FIXTURES["/api/search/latest"],
                                               id=8, status="running", message=None)},
          ready=("text=Backend Engineer a", "text=Searching…")),
    State("packages-bulk", tab="Search", fixtures=_QUEUED,
          ready=("text=Earlier packages (25)", "text=30 in progress")),
    State("packages-history-open", tab="Search", fixtures=_QUEUED,
          ready=("text=Earlier packages (25)",), setup=_history_open),
    State("packages-attempts", tab="Search", fixtures=ATTEMPTS,
          ready=("text=Attempt 2", "text=New package: the profile changed")),
    State("packages-retry-refused", tab="Search",
          fixtures={**ATTEMPTS, ("POST", "/api/packages/12/retry"):
                    Response(409, {"detail": "a newer package for this job already exists"})},
          ready=("text=Attempt 2",), setup=_retry_refused,
          allow_failed_requests=("/api/packages/12/retry",)),
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
    State("settings-engine-codex", tab="Settings",
          fixtures=_engine(status="ok", model="claude-sample", last_error=None,
                           codex={"status": "ok", "model": "gpt-sample",
                                  "verified_at": "2026-09-15T12:00:00Z"},
                           selection={"provider": "codex", "model": "gpt-sample"}),
          ready=("text=Codex ready", "text=Every task uses GPT Sample on Codex.")),
    State("settings-engine-waiting", tab="Settings",
          fixtures=_engine(status="waiting_usage_exhausted", model="claude-sample",
                           retry_in_seconds=1500, last_error="usage limit reached",
                           episode="matrix-episode",
                           next_retry_at="2026-09-15T12:25:00Z",
                           limit_resets_at="2026-09-15T15:00:00Z"),
          ready=("text=switch models to keep working now", "text=Limit resets")),
    State("settings-engine-not-entitled", tab="Settings",
          fixtures=_engine(status="not_entitled", model="claude-small",
                           last_error="model not_found",
                           selection={"provider": "claude", "model": "claude-small"}),
          ready=("text=isn't available on your Claude plan",)),
    State("settings-engine-unavailable", tab="Settings",
          fixtures=_engine(status="ok", model="claude-sample", last_error=None,
                           codex={"status": "unavailable", "model": "gpt-mini",
                                  "last_error": LOCKDOWN_FAILURE,
                                  "models": {"gpt-mini": {"status": "unavailable",
                                                          "last_error": LOCKDOWN_FAILURE,
                                                          "verified_at": None}}},
                           selection={"provider": "codex", "model": "gpt-mini"}),
          ready=("text=can't run under the app's safety lockdown on this version of Codex.",
                 "text=Model unavailable")),
    State("settings-engine-codex-missing", tab="Settings",
          fixtures=_engine(status="ok", model="claude-sample", last_error=None,
                           codex={"status": "config_error", "catalog": None,
                                  "catalog_error": "FileNotFoundError: Codex CLI not found",
                                  "credentials": {
                                      "present": False, "checked_at": "2026-09-15T12:00:00Z",
                                      "problem": "Codex CLI not found on PATH."}}),
          ready=("text=Codex CLI not found on PATH.", "text=Couldn't list Codex models")),
    State("settings-engine-custom", tab="Settings", ready=("text=AI engine",),
          setup=_custom_model),
    State("settings-read-failed", tab="Settings",
          fixtures={"/api/settings": Response(500, {"detail": "settings store unavailable"})},
          ready=("text=settings store unavailable", "text=Appearance"),
          allow_failed_requests=("/api/settings",)),
]

STATE_IDS = [s.name for s in STATES]


def open_state(browser, dist_url, state: State, *, width=1280, height=900,
               scheme="light"):
    """A page in `state`. Returns (context, page, unexpected errors): every
    uncaught page error, and every console error except the HTTP failure
    of an endpoint the state expects to fail."""
    context = browser.new_context(color_scheme=scheme,
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []

    def console(message):
        if message.type != "error":
            return
        url = message.location.get("url", "")
        if any(endpoint in url for endpoint in state.allow_failed_requests):
            return
        errors.append(f"{message.text} ({url})")

    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", console)
    table = dict(API_FIXTURES)
    table.update(state.fixtures)
    route_fixtures(page, table)
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required") if "/api/engines" not in state.fixtures \
        else None
    page.get_by_role("button", name=state.tab, exact=True).click()
    for selector in state.ready:
        page.wait_for_selector(selector)
    if state.setup:
        state.setup(page)
    return context, page, errors
