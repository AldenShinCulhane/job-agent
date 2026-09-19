"""The Search page's job-selection gate, as the built app behaves.

Generating packages is the third human gate: what the user has ticked AND
can see is what gets submitted, nothing else. These cases pin that down
against the built bundle over synthetic fixtures.
"""

import json

from backend.tests.ui_harness import API_FIXTURES, route_fixtures


def _open(browser, dist_url, *, width=1280, height=900):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    route_fixtures(page)
    return context, page


def _goto_search(page, dist_url, ready="text=Backend Engineer a"):
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_selector(ready)


def _row(page, title: str):
    return page.get_by_role("listitem").filter(has_text=title)


def _checkbox(page, title: str):
    return _row(page, title).get_by_role("checkbox")


def _generate(page):
    return page.get_by_role("button", name="Generate packages")


def _route_generate(page, bodies: list, *, status=200, detail=None):
    """The POST the gate submits. Registered after the fixtures, so it wins."""
    def handler(route):
        bodies.append(json.loads(route.request.post_data))
        if status == 200:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"package_ids": [9], "created": 1}))
        else:
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps({"detail": detail}))
    page.route("**/api/packages/generate", handler)


def test_a_filtered_out_row_is_neither_counted_nor_submitted(browser, dist_url):
    context, page = _open(browser, dist_url)
    bodies: list = []
    _route_generate(page, bodies)
    _goto_search(page, dist_url)

    _checkbox(page, "Backend Engineer a").check()  # Easy Apply
    _checkbox(page, "Backend Engineer b").check()  # not
    assert _generate(page).inner_text() == "Generate packages (2)"

    page.get_by_label("Easy Apply only").check()
    assert _row(page, "Backend Engineer b").count() == 0
    assert _generate(page).inner_text() == "Generate packages (1)"

    _generate(page).click()
    page.wait_for_function(
        "() => !document.body.innerText.includes('Generate packages')")
    assert bodies == [{"job_ids": ["a"], "run_id": 7}]

    # A submission is the end of the whole selection, hidden rows included.
    page.get_by_label("Easy Apply only").uncheck()
    assert not _checkbox(page, "Backend Engineer b").is_checked()
    context.close()


def test_rows_from_a_superseded_run_cannot_be_selected(browser, dist_url):
    """The run row says a newer run exists while the list still shows the
    old one: those rows belong to a dead result set."""
    context, page = _open(browser, dist_url)
    newer = dict(API_FIXTURES["/api/search/latest"], id=8, status="running",
                 message=None)
    page.route("**/api/search/latest", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(newer)))
    _goto_search(page, dist_url)

    for title in ("Backend Engineer a", "Backend Engineer b"):
        box = _checkbox(page, title)
        assert box.is_disabled()
        assert "loading" in (box.get_attribute("title") or "")
    assert _generate(page).count() == 0
    context.close()


def test_a_failed_generation_keeps_the_selection_and_shows_why(browser, dist_url):
    context, page = _open(browser, dist_url)
    bodies: list = []
    _route_generate(page, bodies, status=503, detail="engine unavailable")
    _goto_search(page, dist_url)

    _checkbox(page, "Backend Engineer a").check()
    _generate(page).click()
    page.wait_for_selector("text=engine unavailable")
    assert bodies == [{"job_ids": ["a"], "run_id": 7}]
    assert _checkbox(page, "Backend Engineer a").is_checked()
    assert _generate(page).inner_text() == "Generate packages (1)"
    assert not _generate(page).is_disabled()
    context.close()
