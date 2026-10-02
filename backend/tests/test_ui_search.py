"""The Search page's job-selection gate, as the built app behaves.

Generating packages is the third human gate: what the user has ticked AND
can see is what gets submitted, nothing else. These cases pin that down
against the built bundle over synthetic fixtures.
"""

import json

from backend.tests.ui_harness import API_FIXTURES, _job, route_fixtures

THIRTY_JOBS = [_job(f"j{i}", 80 - i) for i in range(30)]


def _open(browser, dist_url, *, width=1280, height=900):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    route_fixtures(page)
    return context, page


def _goto_search(page, dist_url, ready="text=Backend Engineer a"):
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    page.get_by_role("link", name="Search", exact=True).click()
    page.wait_for_selector(ready)


def _row(page, title: str):
    return page.get_by_role("listitem").filter(has_text=title)


def _checkbox(page, title: str):
    return page.get_by_role("checkbox", name=f"Select {title}", exact=True)


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


def _open_long(browser, dist_url, *, width=1280, height=900):
    context, page = _open(browser, dist_url, width=width, height=height)
    page.route("**/api/jobs", lambda route, *_: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(dict(API_FIXTURES["/api/jobs"], jobs=THIRTY_JOBS))))
    _goto_search(page, dist_url, ready="text=Backend Engineer j29")
    return context, page


def _bar(page):
    return page.get_by_text("selected", exact=False).locator("..")


def _box(locator):
    return locator.bounding_box()


def test_the_gate_stays_in_view_while_the_list_runs_past_it(browser, dist_url):
    context, page = _open_long(browser, dist_url)
    _checkbox(page, "Backend Engineer j0").check()
    _checkbox(page, "Backend Engineer j3").check()
    assert page.get_by_text("2 selected").is_visible()

    bar = _bar(page)
    for y in (0, 600, 1200):
        page.evaluate(f"window.scrollTo(0, {y})")
        b = _box(bar)
        assert 0 <= b["y"] and b["y"] + b["height"] <= 900, (y, b)
        assert _generate(page).is_visible()

    # At the end of the list the bar sits in its own place, under the
    # last row rather than over it.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    last = _box(_row(page, "Backend Engineer j29"))
    b = _box(bar)
    assert last["y"] + last["height"] <= b["y"] + 1, (last, b)
    assert b["y"] + b["height"] <= 900, b
    context.close()


def test_focus_never_lands_under_the_gate(browser, dist_url):
    context, page = _open_long(browser, dist_url)
    _checkbox(page, "Backend Engineer j0").check()
    bar_top = _box(_bar(page))["y"]

    # A row well below the fold, focused from the keyboard's point of
    # view: the browser scrolls it into view, and the bar must not be
    # what it scrolls it under.
    _checkbox(page, "Backend Engineer j24").focus()
    for _ in range(8):
        focused = page.evaluate(
            "() => { const r = document.activeElement.getBoundingClientRect();"
            " return [document.activeElement.textContent || document.activeElement.ariaLabel, r.top, r.bottom] }")
        bar_top = _box(_bar(page))["y"]
        assert 0 <= focused[1] and focused[2] <= bar_top + 0.5, (focused, bar_top)
        page.keyboard.press("Tab")
    context.close()


def test_clear_selection_empties_the_gate(browser, dist_url):
    context, page = _open_long(browser, dist_url)
    _checkbox(page, "Backend Engineer j1").check()
    _checkbox(page, "Backend Engineer j2").check()
    page.get_by_role("button", name="Clear selection").click()
    assert _generate(page).count() == 0
    assert page.get_by_text("selected", exact=False).count() == 0
    assert not _checkbox(page, "Backend Engineer j1").is_checked()
    assert not _checkbox(page, "Backend Engineer j2").is_checked()
    assert page.evaluate("document.documentElement.style.scrollPaddingBottom") == ""
    context.close()


def test_the_gate_fits_a_phone(browser, dist_url):
    context, page = _open_long(browser, dist_url, width=375, height=667)
    _checkbox(page, "Backend Engineer j0").check()
    b = _box(_bar(page))
    assert b["x"] >= 0 and b["x"] + b["width"] <= 375, b
    assert b["y"] + b["height"] <= 667, b
    assert page.evaluate("document.documentElement.scrollWidth") == 375
    context.close()


def test_a_failed_generate_explains_itself_beside_the_gate(browser, dist_url):
    context, page = _open_long(browser, dist_url)
    bodies: list = []
    _route_generate(page, bodies, status=503, detail="engine unavailable")
    _checkbox(page, "Backend Engineer j25").check()
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    _generate(page).click()
    alert = page.get_by_role("alert")
    alert.wait_for()
    assert alert.inner_text() == "Error: engine unavailable"
    box = alert.bounding_box()
    assert 0 <= box["y"] and box["y"] + box["height"] <= 900, box
    assert _checkbox(page, "Backend Engineer j25").is_checked()
    assert not _generate(page).is_disabled()
    # The search card, a screen above, did not get it instead.
    assert page.get_by_text("engine unavailable").count() == 1
    context.close()
