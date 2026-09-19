"""The page structure, as the built app lays it out.

Every viewport is measured, not eyeballed: nothing wider than the page,
every control inside its card, the search fields folded behind the run
they would replace, packages beside the list from xl and before it below,
history bounded, and the row pitch that the density trial settled on.
"""

import json

import pytest

from backend.tests.ui_harness import API_FIXTURES, _job, _package, route_fixtures
from backend.tests.test_ui_tokens import TAB_READY

LONG = ("Senior Backend Engineer, Payments Platform (Remote, Canada) "
        "— 12-month contract, hybrid after probation")
THIRTY_JOBS = [_job(f"j{i}", [92, 81, 74, 66, 58, 49, 33, None][i % 8],
                    easy=(i % 3 == 0), title=LONG if i % 5 == 0 else None,
                    description="Build APIs.")
               for i in range(30)]


def _open(browser, dist_url, *, width=1280, height=900, jobs=None,
          packages=None, latest=None, linkedin=None):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" else None)
    route_fixtures(page)
    overrides = {
        "/api/jobs": None if jobs is None else dict(
            API_FIXTURES["/api/jobs"], jobs=jobs),
        "/api/packages": None if packages is None else {
            "page_limit": 1, "packages": packages},
        "/api/search/latest": latest,
        "/api/linkedin/status": linkedin,
    }
    for path, body in overrides.items():
        if body is not None:
            page.route(f"**{path}", _fulfill(body))
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    return context, page, errors


def _fulfill(body: dict):
    # Route handlers get (route, request); the body must not be the second.
    def handler(route, *_):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))
    return handler


def _search_tab(page, ready="text=Backend Engineer a"):
    page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_selector(ready)


def _heading(page, text):
    return page.get_by_role("heading", name=text).bounding_box()


OVERFLOW_JS = """() => {
  const w = document.documentElement.clientWidth
  const out = []
  for (const el of document.querySelectorAll('label, button, input, select, a, summary')) {
    const r = el.getBoundingClientRect()
    if (r.width === 0) continue
    const card = el.closest('section')
    const box = card ? card.getBoundingClientRect() : {left: 0, right: w}
    if (r.left < box.left - 1 || r.right > box.right + 1 || r.right > w + 1)
      out.push([el.tagName, (el.textContent || el.placeholder || '').trim().slice(0, 30),
                Math.round(r.left), Math.round(r.right), Math.round(box.right)])
  }
  return [document.documentElement.scrollWidth, w, out]
}"""


@pytest.mark.parametrize("width,height", [
    (375, 900), (767, 900), (768, 900), (1279, 900), (1280, 900), (1280, 600),
])
def test_nothing_is_wider_than_the_page(browser, dist_url, width, height):
    context, page, errors = _open(browser, dist_url, width=width, height=height)
    for tab, ready in TAB_READY.items():
        page.get_by_role("button", name=tab, exact=True).click()
        for selector in ready:
            page.wait_for_selector(selector)
        if tab == "Search":
            page.get_by_role("button", name="Edit search").click()
            page.wait_for_selector("[placeholder]")
            page.get_by_role("button", name="Details").first.click()
            page.wait_for_selector("text=Build APIs.")
        scroll_w, client_w, escaped = page.evaluate(OVERFLOW_JS)
        assert scroll_w == client_w, (tab, width, scroll_w, client_w)
        assert escaped == [], (tab, width, escaped)
    assert not errors, errors
    context.close()


def test_the_fields_fold_behind_the_last_run(browser, dist_url):
    context, page, errors = _open(browser, dist_url)
    _search_tab(page)
    summary = page.get_by_role("list", name="Last search")
    assert summary.get_by_role("listitem").all_inner_texts() == ["backend"]
    assert page.locator("[placeholder]").count() == 0
    assert page.get_by_text("Daily action cap reached").is_visible()
    assert page.get_by_text("Daily cap reached").is_visible()

    page.get_by_role("button", name="Edit search").click()
    assert page.get_by_placeholder("software engineer").input_value() == "backend"
    assert page.get_by_role("button", name="Edit search").count() == 0
    page.get_by_role("button", name="Cancel").click()
    assert page.locator("[placeholder]").count() == 0
    assert summary.get_by_role("listitem").all_inner_texts() == ["backend"]
    assert not errors, errors
    context.close()


def test_without_a_run_the_fields_simply_show(browser, dist_url):
    context, page, errors = _open(browser, dist_url, latest={"id": None},
                                  jobs=[])
    _search_tab(page, ready="text=No jobs collected yet.")
    assert page.get_by_placeholder("software engineer").is_visible()
    assert page.get_by_role("button", name="Edit search").count() == 0
    assert page.get_by_role("button", name="Cancel").count() == 0
    assert not errors, errors
    context.close()


def test_a_refresh_does_not_fold_an_open_editor(browser, dist_url):
    """A running run polls; the poll's answer must not touch the fields."""
    running = dict(API_FIXTURES["/api/search/latest"], status="running",
                   message=None)
    context, page, errors = _open(browser, dist_url, latest=running)
    _search_tab(page)
    page.get_by_role("button", name="Edit search").click()
    field = page.get_by_placeholder("software engineer")
    field.fill("backend python")
    with page.expect_response(lambda r: r.url.endswith("/api/search/latest"),
                              timeout=20_000):
        pass
    assert field.input_value() == "backend python"
    assert page.get_by_role("button", name="Cancel").is_visible()
    assert not errors, errors
    context.close()


def test_the_disclosure_folds_once_connected(browser, dist_url):
    context, page, errors = _open(browser, dist_url)
    _search_tab(page)
    assert page.get_by_text("A real browser window opens").is_visible()
    assert page.get_by_text("Browser access and account risk").count() == 0
    context.close()

    context, page, errors = _open(browser, dist_url, linkedin={
        "state": "ready", "detail": "reconnected after a checkpoint"})
    _search_tab(page)
    assert page.get_by_text("reconnected after a checkpoint").is_visible()
    assert not page.get_by_text("A real browser window opens").is_visible()
    page.get_by_text("Browser access and account risk").click()
    assert page.get_by_text("A real browser window opens").is_visible()
    assert not errors, errors
    context.close()


def test_packages_sit_beside_the_list_from_xl(browser, dist_url):
    context, page, errors = _open(browser, dist_url, jobs=THIRTY_JOBS)
    _search_tab(page, ready="text=Backend Engineer j29")
    packages, jobs = _heading(page, "Packages"), _heading(page, "Jobs (30)")
    assert packages["y"] + packages["height"] < 900, packages  # no scrolling
    assert packages["x"] > jobs["x"] + 600, (packages, jobs)
    assert page.get_by_text("Role 5").is_visible()
    context.close()


def test_packages_come_before_the_list_below_xl(browser, dist_url):
    context, page, errors = _open(browser, dist_url, width=1279,
                                  jobs=THIRTY_JOBS)
    _search_tab(page, ready="text=Backend Engineer j29")
    packages, jobs = _heading(page, "Packages"), _heading(page, "Jobs (30)")
    assert packages["y"] < jobs["y"], (packages, jobs)
    assert abs(packages["x"] - jobs["x"]) < 2, (packages, jobs)
    context.close()


THIRTY_PACKAGES = [_package(i, "generating" if i == 2 else
                            "queued" if i == 9 else "done")
                   for i in range(1, 31)]


def test_package_history_is_folded_whatever_the_list_length(browser, dist_url):
    context, page, errors = _open(browser, dist_url, jobs=[],
                                  packages=THIRTY_PACKAGES)
    _search_tab(page, ready="text=No jobs collected yet.")
    page.wait_for_selector("text=Role 1")  # the packages read is its own fetch
    region = page.get_by_role("region", name="Package list")
    rows = region.get_by_role("listitem")
    # The newest five, plus the older one still in flight.
    assert [r for r in rows.all_inner_texts() if r.startswith("Role")] and \
        rows.count() == 6, rows.all_inner_texts()
    assert page.get_by_text("Role 9").is_visible()
    assert not page.get_by_text("Role 30").is_visible()
    page.get_by_text("Earlier packages (24)").click()
    assert page.get_by_text("Role 30").is_visible()
    assert rows.count() == 30
    context.close()


def test_the_rail_scrolls_on_its_own_and_from_the_keyboard(browser, dist_url):
    context, page, errors = _open(browser, dist_url, height=700, jobs=[],
                                  packages=THIRTY_PACKAGES)
    _search_tab(page, ready="text=No jobs collected yet.")
    region = page.get_by_role("region", name="Package list")
    page.get_by_text("Earlier packages (24)").click()
    assert region.evaluate("el => el.scrollHeight > el.clientHeight")
    assert region.get_attribute("tabindex") == "0"
    region.focus()
    page_y = page.evaluate("window.scrollY")
    page.keyboard.press("ArrowDown")
    page.wait_for_function(
        "() => document.activeElement.scrollTop > 0")
    # The page itself did not move: the rail took the key.
    assert page.evaluate("window.scrollY") == page_y
    context.close()


PITCH_JS = """() => {
  const section = [...document.querySelectorAll('section')]
    .find(s => s.textContent.startsWith('Jobs ('))
  const ul = section.querySelector('ul.divide-y')
  ul.scrollIntoView()
  const rows = [...ul.querySelectorAll(':scope > li')]
  const rects = rows.map(li => li.getBoundingClientRect())
  const H = innerHeight
  const titles = rows.map(li => li.querySelector('a'))
  return {
    pitch: rects[1].top - rects[0].top,
    fullyVisible: rects.filter(r => r.top >= 0 && r.bottom <= H).length,
    titleEllipsis: titles.filter(a => getComputedStyle(a).textOverflow === 'ellipsis').length,
    wrappedTitles: titles.filter(a => a.getBoundingClientRect().height > 24).length,
    metaTruncated: rows.filter(li => { const p = li.querySelector('p.truncate'); return p.scrollWidth > p.clientWidth }).length,
  }
}"""


def test_row_density_at_the_narrowed_column_width(browser, dist_url):
    """The density trial's result, kept: a tightened two-line row.

    Measured the same way on this fixture (row rectangles, list scrolled
    to the top, 1280×900): 5f9691f showed 13 whole rows at 69 px each, the
    long titles fitting on one line because the list had the full page
    width. Here the rail narrows the list, a long title wraps (81 px) and
    a short one is 57 px: 14 rows with six long titles in thirty, 15 with
    none. Titles are never cut; the meta line may be, and says so."""
    context, page, errors = _open(browser, dist_url, jobs=THIRTY_JOBS)
    _search_tab(page, ready="text=Backend Engineer j29")
    m = page.evaluate(PITCH_JS)
    assert m["fullyVisible"] >= 14, m
    # Titles wrap rather than truncate: a long title costs a line, not
    # its distinguishing tail. Only the meta line may be cut, and the
    # fixture's is short enough that it is not.
    assert m["titleEllipsis"] == 0, m
    assert m["wrappedTitles"] == 6, m  # every fifth of thirty is long
    assert m["metaTruncated"] == 0, m
    assert not errors, errors
    context.close()
