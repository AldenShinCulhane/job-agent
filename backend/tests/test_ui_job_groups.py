"""Postings that repeat one role, in the built app over fixtures: one row
per role, a row that stands for a posting in these results, the other
listings a click away, and a selection that is never hidden or swapped
by the grouping."""

import copy
import json

import pytest

from backend.tests.ui_harness import (
    API_FIXTURES, _job, _member, _package, route_fixtures,
)
from backend.tests.ui_states import EARLIER, GROUPS, ROLE, SEEN_B, SEEN_C


def _open(browser, dist_url, fixtures, *, ready="text=Platform Engineer"):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table.update(copy.deepcopy(fixtures))
    bodies: list = []

    def generate(route):
        bodies.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"package_ids": [9], "created": 1}))

    page.route("**/api/packages/generate", generate)
    page.goto(dist_url + "/?tab=search")
    page.wait_for_selector(ready)
    return context, page, errors, table, bodies


def _jobs_list(page):
    return page.locator("section", has=page.get_by_role("heading", name="Jobs ("))


def _rows(page):
    return _jobs_list(page).locator("ul.divide-y > li")


def _role(page, title="Platform Engineer"):
    return _rows(page).filter(has=page.get_by_role("link", name=title, exact=True))


def _generate(page):
    return page.get_by_role("button", name="Generate packages")


def _alone(*jobs):
    """GROUPS' postings before the grouping: B and C still two rows."""
    return dict(GROUPS, **{"/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=list(jobs) or [
        _job("a", 80),
        _job("b", 70, title="Platform Engineer", first_seen_at=SEEN_B,
             description="Own the platform."),
        _job("c", 70, title="Platform Engineer B", first_seen_at=SEEN_C)])})


def _focus_after_update(page):
    """Where the focus is once the update has painted: the control's name
    and the posting whose line it is on — the row's, or a listing's —
    read from that line's link; or None on the body."""
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return page.evaluate("""() => {
        const el = document.activeElement
        if (el === null || el === document.body) return null
        const link = el.closest('li')?.querySelector('a')
        return [el.getAttribute('aria-label') || el.textContent.trim(),
                link ? link.href.split('/').pop() : null]
    }""")


def _submitted(page, bodies):
    _generate(page).click()
    page.wait_for_function("() => !document.body.innerText.includes('Generate packages')")
    return bodies


def test_listings_of_one_role_are_one_row(browser, dist_url):
    context, page, errors, _, _ = _open(browser, dist_url, GROUPS)
    titles = [t.split("\n")[0] for t in _rows(page).locator("a.font-medium").all_inner_texts()]
    assert titles == ["Backend Engineer a", "Platform Engineer", "Backend Engineer d"]
    # Roles are what the list counts; the listings are said beside it.
    assert page.get_by_role("heading", name="Jobs (").inner_text() == "Jobs (3)"
    assert _jobs_list(page).get_by_text("4 listings", exact=True).is_visible()
    # The row stands for the newest posting in these results.
    role = _role(page)
    assert role.get_by_role("link", name="Platform Engineer").get_attribute(
        "href") == "https://example.invalid/jobs/c"
    assert role.get_by_text("Also listed 2 more times").is_visible()
    assert errors == []
    context.close()


def test_the_package_an_earlier_listing_got_is_named_on_the_row(browser, dist_url):
    """An old packaged listing and a new unbuilt one of the same role: the
    row is the new one, and its badge says whose package it is."""
    role = dict(ROLE, members=[_member("old", EARLIER, False), _member("b", SEEN_B, True)])
    fixtures = dict(GROUPS, **{"/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=[
        _job("a", 80),
        _job("b", 70, title="Platform Engineer", first_seen_at=SEEN_B, group=role)])})
    context, page, errors, _, bodies = _open(browser, dist_url, fixtures)
    row = _role(page)
    # The row's own badge, beside its actions — not the listing's, which
    # is folded away.
    assert row.get_by_text("Package ready", exact=True).filter(visible=True).count() == 1
    assert row.get_by_text("for the listing first seen Aug 1").is_visible()

    row.get_by_role("checkbox", name="Select Platform Engineer").check()
    assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()


def test_each_listing_says_where_it_came_from(browser, dist_url):
    context, page, errors, _, _ = _open(browser, dist_url, GROUPS)
    row = _role(page)
    row.get_by_text("Also listed 2 more times").click()
    lines = row.locator("details li")
    assert [" ".join(t.split()) for t in lines.all_inner_texts()] == [
        "Listing first seen Aug 1 from an earlier search Package ready",
        "Listing first seen Sep 14 in this search",
    ]
    links = lines.get_by_role("link").evaluate_all("as => as.map(a => a.href)")
    assert links == ["https://example.invalid/jobs/old", "https://example.invalid/jobs/b"]
    # Only a listing in these results can be selected.
    assert lines.nth(0).get_by_role("checkbox").count() == 0
    assert lines.nth(1).get_by_role("checkbox").count() == 1
    assert errors == []
    context.close()


def test_a_listing_ticked_in_the_fold_is_said_and_submitted(browser, dist_url):
    context, page, errors, _, bodies = _open(browser, dist_url, GROUPS)
    row = _role(page)
    row.get_by_text("Also listed 2 more times").click()
    box = page.get_by_role("checkbox", name="Select the listing first seen Sep 14")
    box.check()
    # The row did not swap under the click: focus is still on that box.
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == \
        "Select the listing first seen Sep 14"
    assert row.get_by_role("link", name="Platform Engineer").get_attribute(
        "href") == "https://example.invalid/jobs/c"
    assert row.get_by_text("one of them selected").is_visible()
    assert _generate(page).inner_text() == "Generate packages (1)"
    assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()


def test_a_regroup_keeps_the_selection_and_says_so(browser, dist_url):
    """Two rows become one role while one of them is selected: what is
    selected does not change, and the row that folds it says so."""
    alone = [_job("a", 80),
             _job("b", 70, title="Platform Engineer", first_seen_at=SEEN_B),
             _job("c", 70, title="Platform Engineer B", first_seen_at=SEEN_C)]
    fixtures = dict(GROUPS, **{"/api/jobs": dict(API_FIXTURES["/api/jobs"], jobs=alone)})
    context, page, errors, table, bodies = _open(browser, dist_url, fixtures)
    page.get_by_role("checkbox", name="Select Platform Engineer", exact=True).check()

    table["/api/jobs"] = copy.deepcopy(GROUPS["/api/jobs"])
    page.wait_for_selector("text=Also listed 2 more times")
    row = _role(page)
    assert row.get_by_role("link", name="Platform Engineer").get_attribute(
        "href") == "https://example.invalid/jobs/c"
    assert not row.get_by_role("checkbox", name="Select Platform Engineer").is_checked()
    assert row.get_by_text("one of them selected").is_visible()
    assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()


def test_filters_judge_each_listing(browser, dist_url):
    """Easy Apply only: of the role's listings in these results, only B
    offers it — the row is B, and C is listed but can't be selected."""
    context, page, errors, _, bodies = _open(browser, dist_url, GROUPS)
    page.get_by_label("Easy Apply only").check()
    assert page.get_by_role("heading", name="Jobs (").inner_text() == "Jobs (1 of 3)"
    row = _role(page)
    assert row.get_by_role("link", name="Platform Engineer").get_attribute(
        "href") == "https://example.invalid/jobs/b"
    row.get_by_text("Also listed 2 more times").click()
    assert row.locator("details li").get_by_role("checkbox").count() == 0
    row.get_by_role("checkbox", name="Select Platform Engineer").check()
    assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()


def test_a_listing_with_its_own_package_is_the_row(browser, dist_url):
    fixtures = dict(GROUPS, **{"/api/packages": {"page_limit": 1, "packages": [
        _package(6, "generating", job="b", title="Platform Engineer"),
        _package(5, "done", job="old", title="Platform Engineer")]}})
    context, page, errors, _, _ = _open(browser, dist_url, fixtures)
    row = _role(page)
    assert row.get_by_role("link", name="Platform Engineer").get_attribute(
        "href") == "https://example.invalid/jobs/b"
    # Its own package, so no other listing is named.
    assert row.get_by_text("Package in progress", exact=True).is_visible()
    assert row.get_by_text("for the listing first seen").count() == 0
    assert errors == []
    context.close()


# Focus follows the posting, not the row. A row stands for one posting of
# its role and is rebuilt when that changes; whatever the focus was on
# goes with it, and a keyboard user would be back at the top of the page.


@pytest.mark.parametrize("control", ["checkbox", "Details"])
def test_a_posting_folded_into_another_row_keeps_the_focus(browser, dist_url, control):
    """B is its own row and has the focus; a refresh folds B under C. The
    focus follows B into C's listings, shown to show it: to B's checkbox
    there — the same control, or, the Details button being a row's only,
    the one B still has."""
    context, page, errors, table, bodies = _open(browser, dist_url, _alone())
    if control == "checkbox":
        page.get_by_role("checkbox", name="Select Platform Engineer", exact=True).check()
    else:
        _role(page).get_by_role("button", name="Details").click()
        page.wait_for_selector("text=Own the platform.")
    table["/api/jobs"] = copy.deepcopy(GROUPS["/api/jobs"])
    page.wait_for_selector("text=Also listed 2 more times")
    assert _focus_after_update(page) == ["Select the listing first seen Sep 14", "b"]
    assert _role(page).locator("details").evaluate("d => d.open")
    if control == "checkbox":
        assert page.get_by_role("checkbox", name="Select the listing first seen Sep 14").is_checked()
        assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()


def test_a_listing_that_becomes_its_row_keeps_the_focus(browser, dist_url):
    """B's checkbox in C's listings has the focus, and B gets a package of
    its own, so B becomes the row. The focus goes to the row's checkbox,
    which is B's, and the listings stay shown."""
    context, page, errors, table, bodies = _open(browser, dist_url, GROUPS)
    _role(page).get_by_text("Also listed 2 more times").click()
    page.get_by_role("checkbox", name="Select the listing first seen Sep 14").check()
    table["/api/packages"] = {"page_limit": 1, "packages": [
        _package(6, "generating", job="b", title="Platform Engineer"),
        _package(5, "done", job="old", title="Platform Engineer")]}
    page.wait_for_selector('li a.font-medium[href="https://example.invalid/jobs/b"]')
    assert _focus_after_update(page) == ["Select Platform Engineer", "b"]
    assert page.get_by_role("checkbox", name="Select Platform Engineer").is_checked()
    assert _role(page).get_by_role("link", name="Listing first seen Sep 15").is_visible()
    assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()


@pytest.mark.parametrize("shown", [True, False])
def test_the_listings_toggle_keeps_the_focus_when_its_row_is_rebuilt(
    browser, dist_url, shown
):
    """The listings toggle of C's row has the focus, and B gets a package,
    so the row is rebuilt as B's: the focus goes to its toggle, and the
    listings are shown, or not, as the user left them."""
    context, page, errors, table, _ = _open(browser, dist_url, GROUPS)
    toggle = _role(page).get_by_text("Also listed 2 more times")
    toggle.click()
    if not shown:
        toggle.click()
    table["/api/packages"] = {"page_limit": 1, "packages": [
        _package(6, "generating", job="b", title="Platform Engineer")]}
    page.wait_for_selector('li a.font-medium[href="https://example.invalid/jobs/b"]')
    assert _focus_after_update(page) == ["Also listed 2 more times", "b"]
    assert _role(page).locator("details").evaluate("d => d.open") is shown
    assert errors == []
    context.close()


@pytest.mark.parametrize("away", ["to another row", "out of the list", "to nothing"])
def test_focus_the_user_moved_away_stays_where_they_put_it(browser, dist_url, away):
    """B's checkbox had the focus, but the user moved it before the refresh
    that folds B: it stays where they put it."""
    context, page, errors, table, _ = _open(browser, dist_url, _alone())
    page.get_by_role("checkbox", name="Select Platform Engineer", exact=True).check()
    if away == "to another row":
        page.keyboard.press("Shift+Tab")
        moved_to = ["Details", "a"]
    elif away == "out of the list":
        # Straight there, as a screen reader moves it — by keyboard, the
        # focus would cross row A's controls on the way out.
        page.get_by_role("button", name="Rerank close matches").focus()
        moved_to = ["Rerank close matches", None]
    else:
        # The page margin: nothing there takes focus, so it goes to the body.
        page.mouse.click(2, 450)
        moved_to = None
    assert _focus_after_update(page) == moved_to
    table["/api/jobs"] = copy.deepcopy(GROUPS["/api/jobs"])
    page.wait_for_selector("text=Also listed 2 more times")
    assert _focus_after_update(page) == moved_to
    assert errors == []
    context.close()


def test_a_posting_gone_from_the_list_hands_the_focus_to_the_jobs_heading(
    browser, dist_url
):
    context, page, errors, table, _ = _open(browser, dist_url, _alone())
    page.get_by_role("checkbox", name="Select Platform Engineer", exact=True).check()
    table["/api/jobs"]["jobs"] = [j for j in table["/api/jobs"]["jobs"] if j["job_id"] != "b"]
    page.wait_for_selector('li a[href="https://example.invalid/jobs/b"]', state="detached")
    assert _focus_after_update(page) == ["Jobs (2)", None]
    assert errors == []
    context.close()


def test_a_row_the_list_moves_and_then_folds_keeps_the_focus(browser, dist_url):
    """A re-sort re-inserts B's row: the browser blurs B's checkbox and
    React gives it the focus back. That blur was not the user leaving, so
    when a refresh then folds B under C, the focus still follows B."""
    context, page, errors, table, bodies = _open(browser, dist_url, _alone())
    page.get_by_role("checkbox", name="Select Platform Engineer", exact=True).check()
    table["/api/jobs"]["jobs"][1]["deterministic_score"] = 50
    page.wait_for_function(
        "() => document.querySelector('ul.divide-y > li:last-child a.font-medium')"
        "?.href === 'https://example.invalid/jobs/b'")
    assert _focus_after_update(page) == ["Select Platform Engineer", "b"]

    table["/api/jobs"] = copy.deepcopy(GROUPS["/api/jobs"])
    page.wait_for_selector("text=Also listed 2 more times")
    assert _focus_after_update(page) == ["Select the listing first seen Sep 14", "b"]
    assert _submitted(page, bodies) == [{"job_ids": ["b"], "run_id": 7}]
    assert errors == []
    context.close()
