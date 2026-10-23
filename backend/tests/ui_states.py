"""Named, deterministic UI states over the fixtures.

Every matrix check — unexpected console errors, horizontal overflow, axe —
runs across all of them, in both colour schemes where colour matters. A
state is a fixture table, a tab, what proves the tab has finished loading,
and any actions that get the page into the state.
"""

from dataclasses import dataclass, field
from typing import Callable

from backend.tests.ui_harness import (
    API_FIXTURES, ENGINE, SIGNED_IN, Response, _job, _member, _package, engines_view,
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


# A posting laid out as the backend keeps it, and the checklist its score
# was built from: every group, and each of met, missing and unknown.
POSTING = ("Example Widgets Inc. is hiring a backend engineer.\n\n"
           "Build APIs with Python.\nOwn services end to end.\n\n"
           "We ship every week.")
CHECKLIST = [
    {"kind": "required", "label": "Python", "met": True},
    {"kind": "required", "label": "Go", "met": False},
    {"kind": "preferred", "label": "Kubernetes", "met": False},
    {"kind": "preferred", "label": "Docker", "met": True},
    {"kind": "experience", "label": "3+ years of experience", "met": None},
    {"kind": "education", "label": "Bachelor's degree", "met": True},
]
CLAIM = ("I bring hands-on evaluation experience from shipping model-graded "
         "checks across every release, and I would carry that rigor into "
         "your platform team from the first week, pairing on reviews and "
         "owning the release checklist end to end.")
REASON = ("The claim asserts hands-on evaluation experience, but no cited "
          "fact mentions evaluating models.")
DETAILS = {
    "/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[
        _job("a", 80, description=POSTING, requirements=CHECKLIST),
        _job("b", 60, description="Build APIs.", requirements=[]),
        _job("c", 30)]),
    "/api/packages": {"page_limit": 1, "packages": [
        _package(9, "done", job="a", title="Backend Engineer a"),
        _package(8, "generating", job="c", title="Backend Engineer c"),
        _package(7, "failed", "truth validation failed after 2 repair round(s): "
                              "cover:p1: " + REASON,
                 job="b", title="Backend Engineer b",
                 unsupported=[{"kind": "claim", "document": "cover letter",
                               "claim": CLAIM, "reason": REASON},
                              {"kind": "structure", "document": "resume",
                               "claim": None,
                               "reason": "not a permutation of the sections"}]),
    ]},
}


# One role listed three times: once in an earlier search, where it got a
# package, and twice in this one — beside two roles listed once.
EARLIER, SEEN_B, SEEN_C = ("2026-08-01T12:00:00Z", "2026-09-14T12:00:00Z",
                           "2026-09-15T12:00:00Z")
ROLE = {"id": "old", "members": [_member("old", EARLIER, False),
                                 _member("b", SEEN_B, True), _member("c", SEEN_C, True)]}
GROUPS = {
    "/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[
        _job("a", 80),
        _job("b", 70, title="Platform Engineer", first_seen_at=SEEN_B, easy=True,
             group=ROLE),
        _job("c", 70, title="Platform Engineer", first_seen_at=SEEN_C, group=ROLE),
        _job("d", 60)]),
    "/api/packages": {"page_limit": 1, "packages": [
        _package(5, "done", job="old", title="Platform Engineer")]},
}


# The same roles against earlier searches: A and a fifth role new, the
# repeated role reposted (its first listing was in an earlier search),
# and D found again.
NEW_ROLES = {
    "/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[
        _job("a", 80, novelty="new"),
        _job("b", 70, title="Platform Engineer", first_seen_at=SEEN_B, easy=True,
             group=ROLE, novelty="reposted"),
        _job("c", 70, title="Platform Engineer", first_seen_at=SEEN_C, group=ROLE,
             novelty="reposted"),
        _job("d", 60),
        _job("e", 50, novelty="new")]),
}


# The application tracker over the roles above: the repeated role applied
# to through its earlier listing, A interviewing through its own package,
# and D's package not tracked yet.
TRACKER = {
    "/api/jobs": GROUPS["/api/jobs"],
    "/api/packages": {"page_limit": 1, "packages": [
        _package(9, "done", job="d", title="Backend Engineer d"),
        _package(8, "done", job="a", title="Backend Engineer a"),
        _package(5, "done", job="old", title="Platform Engineer")]},
    "/api/applications": {"applications": [
        {"event_id": 1, "job_id": "old", "package_id": 5, "status": "applied",
         "occurred_on": "2026-09-20"},
        {"event_id": 2, "job_id": "a", "package_id": 8, "status": "interviewing",
         "occurred_on": "2026-10-02"}]},
}


# Where a finished package's documents mention the posting's skills: in
# each place, nowhere, and a short name the text isn't searched for.
MENTIONS = [
    {"kind": "required", "label": "Python", "listed": True, "resume": True, "letter": True},
    {"kind": "required", "label": "Kafka", "listed": False, "resume": True, "letter": False},
    {"kind": "required", "label": "Go", "listed": True, "resume": None, "letter": None},
    {"kind": "required", "label": "Terraform", "listed": False, "resume": False,
     "letter": False},
    {"kind": "preferred", "label": "Docker", "listed": False, "resume": False,
     "letter": True},
    {"kind": "preferred", "label": "R", "listed": False, "resume": None, "letter": None},
    {"kind": "preferred", "label": "Rust", "listed": False, "resume": False, "letter": False},
]
MENTIONS_SUMMARY = "Mentions 3 of 4 required and 1 of 3 preferred skills"

COVERAGE = {
    "/api/packages": {"page_limit": 1, "packages": [
        _package(9, "done", job="a", title="Backend Engineer a", coverage=MENTIONS),
        _package(8, "done", job="b", title="Backend Engineer b", coverage=[]),
        _package(7, "done", job="c", title="Backend Engineer c"),
        _package(6, "failed", "compile failed", job="d", title="Backend Engineer d")]},
}


def _mentions_open(page):
    page.get_by_text(MENTIONS_SUMMARY).click()
    page.wait_for_selector("text=not proof of experience")


def _tracker_open(page):
    page.locator("summary[aria-label='Update status for Backend Engineer a']").click()
    page.get_by_role("button", name="Save status for Backend Engineer a").wait_for()


def _listings_open(page):
    page.get_by_text("Also listed 2 more times").click()
    page.wait_for_selector("text=from an earlier search")


def _details_open(page):
    page.get_by_role("button", name="Details").first.click()
    page.wait_for_selector("text=Own services end to end.")


def _why_open(page):
    page.get_by_text("Why", exact=True).first.click()
    page.wait_for_selector(f"text={REASON}")


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
    State("search-job-details", tab="Search", fixtures=DETAILS,
          ready=("text=Backend Engineer a", "text=Package ready"), setup=_details_open),
    State("search-job-groups", tab="Search", fixtures=GROUPS,
          ready=("text=Platform Engineer",), setup=_listings_open),
    State("search-new-roles", tab="Search", fixtures=NEW_ROLES,
          ready=("text=2 new roles", "text=Reposted")),
    State("packages-tracker", tab="Search", fixtures=TRACKER,
          ready=("text=Update status",), setup=_tracker_open),
    State("packages-coverage", tab="Search", fixtures=COVERAGE,
          ready=(f"text={MENTIONS_SUMMARY}",), setup=_mentions_open),
    State("packages-truth-check", tab="Search", fixtures=DETAILS,
          ready=("text=Stopped at the truth check",)),
    State("packages-truth-check-why", tab="Search", fixtures=DETAILS,
          ready=("text=Stopped at the truth check",), setup=_why_open),
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
    page.get_by_role("link", name=state.tab, exact=True).click()
    for selector in state.ready:
        page.wait_for_selector(selector)
    if state.setup:
        state.setup(page)
    return context, page, errors
