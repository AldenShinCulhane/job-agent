"""Try again and Regenerate on the package list (PLAN.md §4.5a, P6b),
driven through the built app over fixtures.

Each is one explicit click on a job's newest package, never automatic,
and one click is one attempt however often it reaches the server. A row
a retry made says what it is: another attempt, or a new package because
inputs changed.
"""

import json
import re

import pytest

from backend.tests.test_ui_engine import READY, _limit_starts, _on_settings
from backend.tests.test_ui_keyboard import _assert_stop, _focused, _tab_to
from backend.tests.ui_harness import _package, engines_view, route_fixtures
from backend.tests.ui_states import ATTEMPTS

KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _open(browser, dist_url, claude=None, *, live_events=False):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page, live_events=live_events)
    table.update(json.loads(json.dumps(ATTEMPTS)))
    if claude is not None:
        table["/api/engines"] = engines_view(claude=claude)
    page.goto(dist_url)
    page.get_by_role("link", name="Search", exact=True).click()
    page.wait_for_selector("text=Attempt 2")
    return context, page, table, errors


def _row(page, pid):
    return page.locator(f"li:has(a[href$='/jobs/{_package(pid, 'x')['job_id']}'])")


def _retries(page, table, pid, answer=None):
    """Answer the retry of package `pid`, holding it until `release`; the
    new row appears in the list only when the request arrives."""
    bodies: list[dict] = []
    held: list = []

    def handler(route, *_):
        bodies.append(json.loads(route.request.post_data))
        held.append(route)

    def release():
        route = held.pop(0)
        if answer is not None:
            route.fulfill(status=answer[0], content_type="application/json",
                          body=json.dumps(answer[1]))
            return
        table["/api/packages"]["packages"].insert(0, _package(
            13, "queued", job="jx", attempt=3, retry_of=12, title="Role X"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"package_id": 13, "created": True, "attempt": 3, "new_inputs": False}))

    page.route(f"**/api/packages/{pid}/retry", handler)
    return bodies, held, release


def test_only_each_jobs_newest_package_offers_a_retry(browser, dist_url):
    context, page, _table, errors = _open(browser, dist_url)
    rows = page.locator("[aria-label='Package list'] li")
    texts = [rows.nth(i).inner_text() for i in range(rows.count())]
    by_label = {("Attempt 2" in t, "New package" in t, "Role 9" in t, "Role X" in t,
                 "Role Y" in t): t for t in texts}
    newest_failed = by_label[(True, False, False, True, False)]
    assert "Try again" in newest_failed and "Regenerate" not in newest_failed
    older_failed = by_label[(False, False, False, True, False)]
    assert "Try again" not in older_failed  # history, not a place to start from
    changed = by_label[(False, True, False, False, True)]
    assert "New package: the profile changed since the one you retried" in changed
    assert "Regenerate" in changed and "Attempt" not in changed
    older_done = by_label[(False, False, False, False, True)]
    assert "Regenerate" not in older_done
    in_flight = by_label[(False, False, True, False, False)]
    assert "Try again" not in in_flight and "Regenerate" not in in_flight
    assert errors == []
    context.close()


def test_a_click_sent_twice_is_one_request_with_one_key(browser, dist_url):
    """Two clicks inside one task: the second lands before the button can
    render disabled, and must still not send a second request."""
    context, page, table, errors = _open(browser, dist_url)
    bodies, _held, release = _retries(page, table, 12)
    button = page.get_by_role("button", name="Try again Role X")
    button.evaluate("b => { b.click(); b.click() }")
    page.wait_for_selector("text=Starting…")
    assert len(bodies) == 1 and KEY.fullmatch(bodies[0]["request_key"])
    release()
    page.wait_for_selector("text=Attempt 3")
    # A click that never focused the button (as in Safari) still ends on
    # the list, not the top of the page.
    page.wait_for_function(
        "() => document.activeElement.textContent === 'Packages'", timeout=2_000)
    page.wait_for_timeout(300)
    assert len(bodies) == 1
    assert errors == []
    context.close()


def test_try_again_by_keyboard_lands_on_the_list(browser, dist_url):
    """The button goes away with its row's turn as newest; focus goes to
    the Packages heading instead of the top of the page."""
    context, page, table, errors = _open(browser, dist_url)
    bodies, _held, release = _retries(page, table, 12)
    _assert_stop(_tab_to(page, "Try again"))
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Starting…")
    release()
    page.wait_for_selector("text=Attempt 3")
    page.wait_for_function("() => document.activeElement.textContent === 'Packages'")
    _assert_stop(_focused(page), "Packages")
    # The attempt it replaced is history now, and the new one is still
    # queued: nothing for Role X to try again yet.
    assert page.get_by_role("button", name="Try again Role X").count() == 0
    assert "Queued" in page.locator("li", has_text="Attempt 3").inner_text()
    assert len(bodies) == 1
    assert errors == []
    context.close()


def test_a_refused_retry_says_why_beside_its_row(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url)
    bodies, _held, release = _retries(
        page, table, 10, answer=(409, {"detail": "the posting for this package is gone"}))
    button = page.get_by_role("button", name="Regenerate Role Y")
    button.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Starting…")
    release()
    row = page.locator("li", has_text="New package")
    row.get_by_text("the posting for this package is gone").wait_for()
    # Nothing moved: the button is back, still focused, ready to try again.
    page.wait_for_selector("text=Regenerate")
    assert button.get_attribute("aria-disabled") == "false"
    _assert_stop(_focused(page), "Regenerate")
    assert len(bodies) == 1
    assert errors == []
    context.close()


def _refused_on_row_y(page, table):
    """Regenerate Role Y by keyboard, refused with the button kept."""
    _bodies, _held, release = _retries(
        page, table, 10, answer=(409, {"detail": "the posting for this package is gone"}))
    page.get_by_role("button", name="Regenerate Role Y").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Starting…")
    release()
    page.get_by_text("the posting for this package is gone").wait_for()
    _assert_stop(_focused(page), "Regenerate")


def _another_tab_regenerates_row_y(page, table):
    table["/api/packages"]["packages"].insert(0, _package(
        14, "queued", job="jy", attempt=2, retry_of=10, title="Role Y"))
    # The next read the page makes (the stream reconnecting) shows it.
    page.get_by_role("button", name="Regenerate Role Y").wait_for(
        state="detached", timeout=10_000)
    page.wait_for_timeout(300)


@pytest.mark.parametrize("case", [
    "refused, still on the button", "refused, moved on", "refused, clicked away",
    "never clicked",
])
def test_a_button_that_goes_later_lands_focus_only_if_it_had_it(
    browser, dist_url, case
):
    """The button goes after the fact — another tab's click made a newer
    package. Focus still on it goes to the Packages heading, whether or
    not it was ever clicked; a user who moved on, by keyboard or by
    clicking elsewhere, stays where they went."""
    context, page, table, errors = _open(browser, dist_url)
    if case.startswith("refused"):
        _refused_on_row_y(page, table)
    else:
        _assert_stop(_tab_to(page, "Regenerate Role Y"))
    if case == "refused, moved on":
        page.keyboard.press("Shift+Tab")
        _assert_stop(_focused(page), "LinkedIn")
    if case == "refused, clicked away":
        # The page margin: nothing there takes focus, so it goes to the body.
        page.mouse.click(2, 450)
        assert page.evaluate("() => document.activeElement === document.body")

    _another_tab_regenerates_row_y(page, table)
    if case == "refused, moved on":
        _assert_stop(_focused(page), "LinkedIn")
        assert "New package" in page.evaluate(
            "() => document.activeElement.closest('li').innerText")
    elif case == "refused, clicked away":
        assert page.evaluate("() => document.activeElement === document.body")
    else:
        _assert_stop(_focused(page), "Packages")
    assert errors == []
    context.close()


def test_overlapping_refusals_keep_focus_with_the_button_that_has_it(
    browser, dist_url
):
    """Two rows' clicks out at once, answered out of order. Focus stays
    on the second row's button through both refusals; when that button
    goes, focus goes to the Packages heading — whatever the other row's
    answer did last."""
    context, page, table, errors = _open(browser, dist_url)
    _bx, _hx, release_x = _retries(
        page, table, 12, answer=(409, {"detail": "no room for Role X"}))
    _by, _hy, release_y = _retries(
        page, table, 10, answer=(409, {"detail": "no room for Role Y"}))
    page.get_by_role("button", name="Try again Role X").focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Starting… Role X").wait_for()
    page.get_by_role("button", name="Regenerate Role Y").focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Starting… Role Y").wait_for()

    release_y()
    page.get_by_text("no room for Role Y").wait_for()
    release_x()
    page.get_by_text("no room for Role X").wait_for()
    _assert_stop(_focused(page), "Regenerate Role Y")

    _another_tab_regenerates_row_y(page, table)
    _assert_stop(_focused(page), "Packages")
    # Role X's button stayed, and nothing moved it.
    assert page.get_by_role("button", name="Try again Role X").count() == 1
    assert errors == []
    context.close()


def test_a_retry_in_flight_holds_the_engine_picker(browser, dist_url):
    """A usage limit opens the picker once, but never over a click of the
    user's still on its way: the tab switch would drop its answer."""
    context, page, table, errors = _open(browser, dist_url, {**READY, "episode": None})
    _bodies, _held, release = _retries(page, table, 12)
    page.get_by_role("button", name="Try again Role X").click()
    page.wait_for_selector("text=Starting…")
    _limit_starts(page, table, "ep-retry")
    page.wait_for_timeout(300)
    assert not _on_settings(page)
    release()
    page.wait_for_selector("text=Attempt 3")
    assert errors == []
    context.close()


@pytest.mark.parametrize("lost", ["no answer", "server error"])
def test_a_lost_answer_still_shows_the_row_it_made(browser, dist_url, events, lost):
    """The server made the attempt, but no answer says so: none arrived,
    or a server error came instead. The page says it could not confirm
    the click, not that it failed, and reads the list again, so the row
    is there — and the button with it has moved to the new row's turn.
    Focus the button had goes to the Packages heading, not the top of
    the page.

    The stream stays connected, so the next background read is a 15 s
    reconciliation poll away: the row showing sooner is the page's own
    read after the failure."""
    context, page, table, errors = _open(browser, dist_url, live_events=True)
    # Connected, and the read every new connection makes has landed.
    events.wait_for_connections(1, page=page)
    page.wait_for_timeout(1_000)

    def made_then_lost(route, *_):
        table["/api/packages"]["packages"].insert(0, _package(
            13, "queued", job="jx", attempt=3, retry_of=12, title="Role X"))
        if lost == "no answer":
            route.abort()
        else:
            route.fulfill(status=500, body="Internal Server Error")
    page.route("**/api/packages/12/retry", made_then_lost)
    page.get_by_role("button", name="Try again Role X").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Attempt 3", timeout=5_000)
    assert page.get_by_role("button", name="Try again Role X").count() == 0
    page.wait_for_function(
        "() => document.activeElement.textContent === 'Packages'", timeout=2_000)
    _assert_stop(_focused(page), "Packages")
    page.locator("li", has_text="Attempt 2").get_by_text(
        "Couldn't confirm the retry response; checking package status",
        exact=True).wait_for(timeout=2_000)
    assert errors == []
    context.close()


def test_a_refusal_that_shows_a_newer_package_moves_focus_to_the_list(
    browser, dist_url
):
    """Another tab got there first. The refusal says so beside the row,
    and the list read after it shows the newer row, which takes the
    button: focus goes to the Packages heading, not the top of the page.
    A refusal that leaves the button keeps focus on it (above)."""
    context, page, table, errors = _open(browser, dist_url)

    def another_tab_first(route, *_):
        table["/api/packages"]["packages"].insert(0, _package(
            13, "queued", job="jx", attempt=3, retry_of=12, title="Role X"))
        route.fulfill(status=409, content_type="application/json", body=json.dumps(
            {"detail": "a newer package for this job already exists"}))
    page.route("**/api/packages/12/retry", another_tab_first)
    page.get_by_role("button", name="Try again Role X").focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Attempt 3")
    page.wait_for_function(
        "() => document.activeElement.textContent === 'Packages'", timeout=2_000)
    _assert_stop(_focused(page), "Packages")
    page.locator("li", has_text="Attempt 2").get_by_text(
        "a newer package for this job already exists").wait_for()
    assert errors == []
    context.close()
