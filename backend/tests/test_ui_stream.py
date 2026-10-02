"""The event stream and the polling that reconciles it, driven end to end
through the built app: a real EventSource on a real stream the test
controls, and a controlled clock for every interval.

The stream is invalidation, never truth: an event makes its owners
refetch; nothing on the page is built from the event itself. Each
expectation below names the requests a topic causes with a given page
mounted — the fan-out as it is, not as it might be coalesced.
"""

import json

import pytest

from backend.tests.ui_harness import (
    API_FIXTURES, SIGNED_IN, Response, _package, engines_view, route_fixtures,
)

# What one event on each topic refetches while the Search page is mounted.
# App owns engine and linkedin; Search re-reads linkedin for its own card,
# and profile changes what scores /api/jobs can show.
FANOUT_SEARCH = {
    "engine": ["/api/engines"],
    "linkedin": ["/api/linkedin/status", "/api/linkedin/status"],
    "search": ["/api/jobs", "/api/search/latest"],
    "scoring": ["/api/jobs"],
    "packages": ["/api/packages"],
    "profile": ["/api/jobs", "/api/profile"],
}
FANOUT_PROFILE = {
    "engine": ["/api/engines"],
    "linkedin": ["/api/linkedin/status"],
    "search": [],
    "scoring": [],
    "packages": [],
    "profile": ["/api/profile"],
}

# Nothing to poll for while the stream is up: session ready, run done,
# every package finished.
IDLE = {
    "/api/linkedin/status": {"state": "ready", "detail": None},
    "/api/search/latest": {"id": 7, "status": "done", "collected": 4,
                           "message": None, "config": {"keywords": "backend"}},
    "/api/packages": {"page_limit": 1, "packages": [
        _package(1, "done"), _package(2, "done")]},
}
# The same, with one package still building: that is what makes the page
# poll fast while the stream is down.
BUILDING = {**IDLE, "/api/packages": {"page_limit": 1, "packages": [
    _package(1, "done"), _package(2, "generating")]}}


class Page:
    """A page on the live stream with a paused clock and a request log."""

    def __init__(self, browser, dist_url, events, *, tab, ready, fixtures=None,
                 status_ready="text=Claude sign-in required"):
        self.events = events
        self.context = browser.new_context(color_scheme="light",
                                           viewport={"width": 1280, "height": 900})
        try:
            self._start(dist_url, tab, ready, fixtures, status_ready)
        except BaseException:
            self.context.close()  # or its stream outlives the failure
            raise

    def _start(self, dist_url, tab, ready, fixtures, status_ready):
        events = self.events
        self.page = self.context.new_page()
        self.page.clock.install()
        self.log: list[tuple[str, str]] = []
        self.page.on("request", self._record)
        table = dict(API_FIXTURES)
        table.update(fixtures or {})
        self.table = route_fixtures(self.page, table, live_events=True)
        self.page.goto(dist_url)
        self.page.wait_for_selector(status_ready)
        if tab != "Profile":
            self.page.get_by_role("link", name=tab, exact=True).click()
        for selector in ready:
            self.page.wait_for_selector(selector)
        events.wait_for_connections(1, page=self.page)
        # The first open bumps every topic; let that round land before
        # anything is counted. The clock keeps running at real pace — the
        # tests jump it forward, never stop it, because a pause fires the
        # page's due timers as a side effect.
        self.page.clock.run_for(300)
        self.drain()

    def _record(self, request):
        url = request.url
        if "/api/" not in url or "/api/events" in url or "preview.pdf" in url:
            return
        path = url.split("?", 1)[0]
        self.log.append((request.method, path[path.index("/api"):]))

    def drain(self, quiet_ms=250):
        """Wait until no new request has appeared for `quiet_ms`."""
        seen = -1
        while True:
            if len(self.log) == seen:
                return
            seen = len(self.log)
            self.page.wait_for_timeout(quiet_ms)

    def paths(self):
        """The reads, sorted. Mutations the test itself made are not reads."""
        return sorted(p for m, p in self.log if m == "GET")

    def reset(self):
        self.log.clear()

    def after_event(self, topic):
        self.reset()
        self.events.push(topic)
        self.page.clock.run_for(250)  # the stream's coalescing window
        self.drain()
        return self.paths()


SEARCH_READY = ("text=Backend Engineer a", "text=Ready")


@pytest.fixture
def live(browser, dist_url, events):
    """Builds pages on the live stream and closes them all afterwards, so a
    failed test's page cannot keep a stream open into the next test."""
    opened: list[Page] = []

    def build(**kw):
        p = Page(browser, dist_url, events, **kw)
        opened.append(p)
        return p
    yield build
    for p in opened:
        p.context.close()


@pytest.mark.parametrize("topic", list(FANOUT_SEARCH))
def test_one_event_refetches_exactly_its_owners_on_search(live, events, topic):
    p = live(tab="Search", ready=SEARCH_READY)
    assert p.after_event(topic) == sorted(FANOUT_SEARCH[topic]), topic


@pytest.mark.parametrize("topic", list(FANOUT_PROFILE))
def test_one_event_refetches_exactly_its_owners_on_profile(live, events, topic):
    p = live(tab="Profile", ready=("text=Your profile",))
    assert p.after_event(topic) == sorted(FANOUT_PROFILE[topic]), topic


def test_a_burst_on_one_topic_is_one_refetch(live, events):
    p = live(tab="Search", ready=SEARCH_READY)
    p.reset()
    for _ in range(5):
        events.push("packages")
    p.page.clock.run_for(250)
    p.drain()
    assert p.paths() == ["/api/packages"]


def test_a_package_moves_on_screen_without_a_reload(live, events):
    p = live(tab="Search", ready=SEARCH_READY)
    row = p.page.get_by_role("listitem").filter(has_text="Role 2")
    assert row.get_by_text("Generating…").is_visible()
    p.table["/api/packages"]["packages"][1]["status"] = "done"
    p.table["/api/packages"]["packages"][1]["output_dir"] = "/out"
    events.push("packages")
    p.page.clock.run_for(250)
    row.get_by_text("Ready").wait_for()


def test_a_reconnect_refetches_everything_once(live, events):
    p = live(tab="Search", ready=SEARCH_READY)
    p.reset()
    events.drop()
    events.wait_for_connections(0, page=p.page)
    events.wait_for_connections(1, page=p.page)  # EventSource came back on its own
    p.page.clock.run_for(250)
    p.drain()
    expected = sorted(path for paths in FANOUT_SEARCH.values() for path in paths)
    assert p.paths() == expected


def test_no_polling_while_connected_and_idle(live, events):
    p = live(tab="Search", fixtures=IDLE,
             ready=("text=LinkedIn connected", "text=Search complete"))
    p.reset()
    p.page.clock.run_for(35_000)
    p.drain()
    assert p.paths() == []


def test_disconnected_polling_reconciles_on_its_own_intervals(live, events):
    p = live(tab="Search", fixtures=IDLE,
             ready=("text=LinkedIn connected", "text=Search complete"))
    events.accepting = False
    events.drop()
    events.wait_for_connections(0, page=p.page)
    p.page.wait_for_timeout(500)  # a rejected reconnect or two
    p.reset()
    p.page.clock.run_for(19_000)
    p.drain()
    assert p.paths() == [], "nothing is due before 20 s"
    p.page.clock.run_for(2_000)  # 21 s: App's status poll
    p.drain()
    assert p.paths() == ["/api/engines", "/api/linkedin/status"]
    p.page.clock.run_for(10_000)  # 31 s: Search's own reconciliation
    p.drain()
    assert p.paths() == ["/api/engines", "/api/jobs", "/api/linkedin/status",
                         "/api/linkedin/status", "/api/packages", "/api/search/latest"]


def test_the_usage_countdown_polls_even_while_connected(live, events):
    waiting = {**IDLE, "/api/engines": engines_view(claude={
        "status": "waiting_usage_exhausted", "model": "claude-sample",
        "retry_in_seconds": 600, "last_error": None, "credentials": SIGNED_IN})}
    p = live(tab="Search", fixtures=waiting,
             ready=("text=Claude usage limit reached",),
             status_ready="text=usage limit")
    p.reset()
    p.page.clock.run_for(20_500)
    p.drain()
    assert p.paths() == ["/api/engines", "/api/linkedin/status"]


def test_a_failed_event_read_retries_without_another_event(live, events):
    p = live(tab="Search", ready=SEARCH_READY)
    good = p.table["/api/packages"]
    p.table["/api/packages"] = Response(503, {"detail": "db locked"})
    assert p.after_event("packages") == ["/api/packages"]  # failed
    p.table["/api/packages"] = good
    good["packages"][1]["status"] = "done"
    good["packages"][1]["output_dir"] = "/out"
    p.reset()
    p.page.clock.run_for(5_100)  # the owed-retry interval, no event
    p.drain()
    assert p.paths() == ["/api/packages"]
    p.page.get_by_role("listitem").filter(has_text="Role 2").get_by_text("Ready").wait_for()


def _hold(page, path):
    held = []

    def handler(route, *_):
        if route.request.method == "GET":
            held.append(route)
        else:
            route.fallback()
    page.route(f"**{path}", handler)
    return held


def _release(route, body):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_a_profile_event_during_a_pending_read_is_not_lost(live, events):
    p = live(tab="Profile", ready=("text=Your profile",))
    held = _hold(p.page, "/api/profile")
    old = dict(p.table["/api/profile"])
    newer = json.loads(json.dumps(old))
    newer["version"] = 6
    newer["profile"]["name"] = "Newer Person"

    events.push("profile")
    p.page.clock.run_for(250)
    p.page.wait_for_timeout(200)
    assert len(held) == 1
    events.push("profile")  # overtakes the read still out
    p.page.clock.run_for(250)
    p.page.wait_for_timeout(200)
    assert len(held) == 2, "the second announcement must issue its own read"

    _release(held[0], old)   # the stale answer lands first
    _release(held[1], newer)
    p.page.wait_for_selector("text=Draft v6")
    assert p.page.get_by_label("Name", exact=True).input_value() == "Newer Person"
    p.reset()
    p.page.clock.run_for(6_000)
    p.drain()
    assert p.paths() == [], "settled: nothing more is owed"


def test_a_dirty_editor_defers_the_refetch_until_clean(live, events):
    p = live(tab="Profile", ready=("text=Your profile",))
    name = p.page.get_by_label("Name", exact=True)
    name.fill("Sample Person B")
    p.page.wait_for_selector("text=Unsaved edits")
    newer = json.loads(json.dumps(p.table["/api/profile"]))
    newer["version"] = 6
    newer["profile"]["name"] = "Newer Person"
    p.table["/api/profile"] = newer

    assert p.after_event("profile") == [], "dirty: adopting would overwrite edits"
    p.page.clock.run_for(6_000)
    p.drain()
    assert p.paths() == [], "and the retry interval does not sneak past that"
    assert name.input_value() == "Sample Person B"

    p.page.get_by_role("button", name="Save & re-render").click()
    p.page.wait_for_selector("text=Draft v6")
    assert name.input_value() == "Newer Person"
    assert p.paths().count("/api/profile") == 1


def test_first_open_and_visibility_return_refetch_everything(live, events):
    p = live(tab="Profile", ready=("text=Your profile",))
    # The constructor did not reset the log: what is there is the initial
    # fetch plus the first-open round for each consumer.
    assert p.paths() == ["/api/engines", "/api/engines",
                         "/api/linkedin/status", "/api/linkedin/status",
                         "/api/profile", "/api/profile"]
    p.reset()
    p.page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }""")
    p.page.clock.run_for(250)
    p.drain()
    assert p.paths() == ["/api/engines", "/api/linkedin/status", "/api/profile"]


def test_a_superseded_poll_answer_never_lands(live, events):
    p = live(tab="Search", fixtures=BUILDING,
             ready=("text=LinkedIn connected", "text=Search complete"))
    row = p.page.get_by_role("listitem").filter(has_text="Role 2")
    events.accepting = False
    events.drop()
    events.wait_for_connections(0, page=p.page)
    p.page.wait_for_timeout(500)  # a rejected reconnect: the page knows
    held = _hold(p.page, "/api/packages")
    p.page.clock.run_for(2_600)  # the fast poll a building package earns
    p.page.wait_for_timeout(300)
    assert len(held) == 1

    events.accepting = True
    p.page.clock.run_for(5_100)  # the refused stream is reopened on a timer
    events.wait_for_connections(1, page=p.page)  # back: every topic is announced
    p.page.clock.run_for(250)
    p.page.wait_for_timeout(300)
    assert len(held) == 2

    done = json.loads(json.dumps(IDLE["/api/packages"]))
    _release(held[1], done)
    row.get_by_text("Ready").wait_for()
    _release(held[0], BUILDING["/api/packages"])  # the older answer, last
    p.page.wait_for_timeout(300)
    assert row.get_by_text("Ready").is_visible()
    assert row.get_by_text("Generating…").count() == 0


def test_a_refused_stream_is_reopened_and_listens_again(live, events):
    """A dropped stream EventSource retries by itself; a REFUSED one (any
    non-200) it abandons for good. The hook opens a new one after a pause,
    and the new one must carry the topic listeners, not just connect."""
    p = live(tab="Search", fixtures=IDLE,
             ready=("text=LinkedIn connected", "text=Search complete"))
    events.accepting = False
    events.drop()
    events.wait_for_connections(0, page=p.page)
    p.page.wait_for_timeout(500)  # the built-in retry meets a 503 and stops
    assert events.connections == 0
    p.page.clock.run_for(4_000)
    p.page.wait_for_timeout(300)
    assert events.connections == 0, "not before the pause is over"

    events.accepting = True
    p.page.clock.run_for(1_100)
    events.wait_for_connections(1, page=p.page)
    p.page.clock.run_for(250)
    p.drain()
    assert p.after_event("packages") == ["/api/packages"]
