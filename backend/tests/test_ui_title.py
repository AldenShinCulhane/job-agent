"""How many packages finished while the page was hidden, in the tab's
title (PLAN.md P8d), driven through the built app on a real stream the
test controls. The count is against what the page last showed before it
was hidden; showing the page again clears it.

Hidden and shown are the document's own visibility, set the way a
browser sets it: the property, then the event."""

import json

import pytest

from backend.tests.test_ui_stream import BUILDING, IDLE, SEARCH_READY, Page, _hold, _release
from backend.tests.ui_harness import Response, _package

PROFILE_READY = ("text=Your profile",)


@pytest.fixture
def live(browser, dist_url, events):
    """test_ui_stream's pages, and one that opens hidden."""
    opened: list[Page] = []

    def build(page_class=Page, **kw):
        p = page_class(browser, dist_url, events, **kw)
        opened.append(p)
        return p
    yield build
    for p in opened:
        p.context.close()


def _visibility(page, state):
    page.evaluate("""s => {
      Object.defineProperty(document, 'visibilityState', { get: () => s, configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }""", state)


def _hide(p):
    _visibility(p.page, "hidden")
    p.drain()  # hidden off Search, App reads the list at once


def _show(p):
    _visibility(p.page, "visible")
    p.page.clock.run_for(250)  # showing again re-reads every topic
    p.drain()


def _finish(p, *pids, status="done"):
    for pkg in p.table["/api/packages"]["packages"]:
        if pkg["id"] in pids:
            pkg["status"] = status
            pkg["output_dir"] = "/out" if status == "done" else None


def _on_profile(p):
    """Off the Search page, which unmounts; the list App read stays."""
    p.page.get_by_role("link", name="Profile", exact=True).click()
    p.page.wait_for_selector(PROFILE_READY[0])
    p.drain()


class OpenedHidden(Page):
    """A page that is hidden from the start, as one opened in a
    background tab is."""

    def _start(self, *args):
        self.context.add_init_script("""Object.defineProperty(document,
            'visibilityState', { get: () => 'hidden', configurable: true })""")
        super()._start(*args)


class ShownOnSearch(Page):
    """A page opened at the Search tab's own address."""

    def _start(self, dist_url, *rest):
        super()._start(dist_url + "/?tab=search", *rest)


class OpenedHiddenOnSearch(OpenedHidden):
    """The same, opened in a background tab."""

    def _start(self, dist_url, *rest):
        super()._start(dist_url + "/?tab=search", *rest)


def test_a_package_that_finishes_while_hidden_is_counted(live, events):
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    assert p.page.title() == "Job Agent"
    p.reset()
    _hide(p)
    assert p.paths() == [], "Search reads the list on events itself, and holds what it showed"
    assert p.page.title() == "Job Agent"
    _finish(p, 2)
    # Search reads the list on the event while it is open; App reads
    # nothing more on top.
    assert p.after_event("packages") == ["/api/packages"]
    assert p.page.title() == "(1) Job Agent"
    _hide(p)  # told again: still the same spell, still the same count
    assert p.page.title() == "(1) Job Agent"
    _show(p)
    assert p.page.title() == "Job Agent"


def test_what_finished_before_hiding_but_was_never_shown_counts(live, events):
    """Shown on another page, nothing reads the list, so the event for a
    package that finishes there is nobody's to read. Hiding reads the
    list, with no further event, and counts that package against the
    list Search last showed."""
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _on_profile(p)
    _finish(p, 2)
    assert p.after_event("packages") == [], "shown, and nothing on this page needs it"
    p.reset()
    _hide(p)
    assert p.paths() == ["/api/packages"]
    assert p.page.title() == "(1) Job Agent"
    p.reset()
    p.page.clock.run_for(61_000)
    p.drain()
    assert p.paths() == [], "connected: nothing more until an event"
    assert p.page.title() == "(1) Job Agent"


def test_hidden_with_no_list_read_yet_reads_one_at_once(live, events):
    """Opened on another page, nothing has read the list. Hiding reads it
    at once, as the starting point: what finishes after that counts, and
    the count says it is at least that."""
    p = live(tab="Profile", fixtures=BUILDING, ready=PROFILE_READY)
    assert ("GET", "/api/packages") not in p.log
    p.reset()
    _hide(p)
    assert p.paths() == ["/api/packages"]
    assert p.page.title() == "Job Agent"
    _finish(p, 2)
    assert p.after_event("packages") == ["/api/packages"]
    assert p.page.title() == "(1+) Job Agent"


def test_hiding_overtakes_a_read_still_out(live, events):
    """A read Search started can still be out when its page is left, and
    it went out before the event nobody read. Hiding reads the list
    anyway: the older answer lands second to it in the sequence, and is
    not the one believed."""
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    held = _hold(p.page, "/api/packages")
    events.push("packages")
    p.page.clock.run_for(250)
    p.page.wait_for_timeout(200)
    assert len(held) == 1
    older = json.loads(json.dumps(p.table["/api/packages"]))  # generating
    _on_profile(p)
    _finish(p, 2)
    events.push("packages")  # shown on Profile: nobody reads it
    p.page.clock.run_for(250)
    p.drain()
    assert len(held) == 1
    _hide(p)
    assert len(held) == 2, "hiding reads, though a read is still out"
    _release(held[0], older)
    _release(held[1], p.table["/api/packages"])
    p.page.wait_for_timeout(300)
    assert p.page.title() == "(1) Job Agent"


def test_packages_made_and_finished_while_hidden_count_and_failed_counts(live, events):
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _hide(p)
    p.table["/api/packages"]["packages"].insert(0, _package(3, "done"))
    p.table["/api/packages"]["packages"].insert(0, _package(4, "queued"))
    p.after_event("packages")
    assert p.page.title() == "(1) Job Agent"
    _finish(p, 2, status="failed")
    _finish(p, 4)
    p.after_event("packages")
    assert p.page.title() == "(3) Job Agent"


def test_hiding_again_counts_from_what_was_shown(live, events):
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    p.table["/api/packages"]["packages"].insert(0, _package(3, "generating"))
    p.after_event("packages")
    _hide(p)
    _finish(p, 2)
    p.after_event("packages")
    assert p.page.title() == "(1) Job Agent"
    _show(p)
    _hide(p)
    assert p.page.title() == "Job Agent"
    _finish(p, 3)
    p.after_event("packages")
    assert p.page.title() == "(1) Job Agent"


def test_hidden_on_another_page_app_reads_the_list(live, events):
    """Search is not open to read the list on the event, so App does,
    once, against the list Search last showed."""
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _on_profile(p)
    assert p.after_event("packages") == [], "shown, and nothing on this page needs it"
    _hide(p)
    assert p.page.title() == "Job Agent"
    _finish(p, 2)
    assert p.after_event("packages") == ["/api/packages"]
    assert p.page.title() == "(1) Job Agent"
    _show(p)
    assert p.page.title() == "Job Agent"


def test_a_failed_hidden_read_is_retried_until_one_lands(live, events):
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _on_profile(p)
    good = p.table["/api/packages"]
    p.table["/api/packages"] = Response(503, {"detail": "db locked"})
    p.reset()
    _hide(p)
    assert p.paths() == ["/api/packages"]  # failed
    p.table["/api/packages"] = good
    _finish(p, 2)
    p.reset()
    p.page.clock.run_for(5_100)  # the owed-retry interval, no event
    p.drain()
    assert p.paths() == ["/api/packages"]
    assert p.page.title() == "(1) Job Agent"
    p.reset()
    p.page.clock.run_for(6_000)
    p.drain()
    assert p.paths() == [], "landed: nothing more is owed"


def test_with_the_stream_down_the_hidden_count_polls(live, events):
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _on_profile(p)
    _hide(p)
    events.accepting = False
    events.drop()
    events.wait_for_connections(0, page=p.page)
    p.page.wait_for_timeout(500)  # a rejected reconnect or two
    _finish(p, 2)
    p.reset()
    p.page.clock.run_for(21_000)  # App's status tick
    p.drain()
    assert p.paths() == ["/api/engines", "/api/linkedin/status", "/api/packages"]
    assert p.page.title() == "(1) Job Agent"


def test_a_reconnect_while_hidden_reads_the_list_again(live, events):
    """Events sent while the stream was down are lost; coming back, it
    announces every topic, and the count catches up."""
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _on_profile(p)
    _hide(p)
    events.drop()
    events.wait_for_connections(0, page=p.page)
    _finish(p, 2)
    events.wait_for_connections(1, page=p.page)
    p.page.clock.run_for(250)
    p.drain()
    assert p.page.title() == "(1) Job Agent"


def test_opened_hidden_counts_from_its_first_read_and_says_at_least(live, events):
    """Opened in the background, the page has shown nothing: the first
    read made while hidden stands in for what it showed, and the count
    says it is at least that. None finished says nothing, not "(0+)"."""
    p = live(tab="Profile", fixtures=BUILDING, ready=PROFILE_READY, page_class=OpenedHidden)
    assert ("GET", "/api/packages") in p.log, "App is the reader from the start"
    assert p.page.title() == "Job Agent"
    _finish(p, 2)
    p.after_event("packages")
    assert p.page.title() == "(1+) Job Agent"
    _show(p)
    assert p.page.title() == "Job Agent"


def test_opened_hidden_on_search_only_search_reads_the_list(live, events):
    """Search reads the list when it mounts, hidden or not, and App reads
    nothing on top of it."""
    reads = []
    for page_class, state in ((ShownOnSearch, "visible"), (OpenedHiddenOnSearch, "hidden")):
        p = live(tab="Search", fixtures=IDLE, ready=SEARCH_READY, page_class=page_class)
        assert p.page.evaluate("document.visibilityState") == state
        reads.append(p.log.count(("GET", "/api/packages")))
        p.context.close()  # one stream at a time
        events.drop()
        events.wait_for_connections(0)
    assert reads[0] >= 1
    assert reads[1] == reads[0], "the same reads as a page opened shown"


def test_a_read_from_before_the_page_was_shown_is_dropped(live, events):
    """A read started while hidden that answers once the page is shown
    again is dropped: it brings no count back, and the list it would have
    set is not what the page showed. Hidden again, the count is against
    the list held before that read."""
    p = live(tab="Search", fixtures=BUILDING, ready=SEARCH_READY)
    _on_profile(p)
    held = _hold(p.page, "/api/packages")
    _hide(p)
    assert len(held) == 1
    _finish(p, 2)
    _show(p)  # nothing on this page reads the list while it shows
    assert len(held) == 1
    _release(held[0], p.table["/api/packages"])
    p.page.wait_for_timeout(300)
    assert p.page.title() == "Job Agent"
    p.page.unroute("**/api/packages")
    p.reset()
    _hide(p)
    assert p.paths() == ["/api/packages"]
    assert p.page.title() == "(1) Job Agent"


def test_a_read_from_before_the_page_was_shown_is_no_starting_point(live, events):
    """Hidden with nothing read yet, the first read that lands while
    hidden is the starting point. One started before the page was shown
    again is not, even when it is the first to land."""
    p = live(tab="Profile", fixtures=BUILDING, ready=PROFILE_READY)
    assert ("GET", "/api/packages") not in p.log
    held = _hold(p.page, "/api/packages")
    _hide(p)
    assert len(held) == 1
    _show(p)  # nothing on this page reads the list while it shows
    _release(held[0], json.loads(json.dumps(BUILDING["/api/packages"])))  # generating
    p.page.wait_for_timeout(300)
    p.page.unroute("**/api/packages")
    _finish(p, 2)
    p.reset()
    _hide(p)
    assert p.paths() == ["/api/packages"]
    assert p.page.title() == "Job Agent", "finished before the starting point"
    p.table["/api/packages"]["packages"].insert(0, _package(3, "done"))
    p.after_event("packages")
    assert p.page.title() == "(1+) Job Agent"
