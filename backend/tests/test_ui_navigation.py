"""Where the app puts things and how it moves between them, in the built
app over fixtures: one column from the header to the page, and the tab in
the address, so that a link opens it and Back and Forward move between
tabs.
"""

import pytest

from backend.tests.test_ui_engine import WAITING
from backend.tests.test_ui_keyboard import _assert_stop, _tab_to
from backend.tests.ui_harness import engines_view, route_fixtures


def _open(browser, dist_url, *, path="", width=1280, height=900, fixtures=None,
          ready="text=Your profile"):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table.update(fixtures or {})
    page.goto(dist_url + path)
    page.wait_for_selector(ready)
    return context, page, errors


# The left edge of the text in each band: the header's title, a notice,
# the first tab, and the first card on the page.
EDGES_JS = """() => {
  const left = (el) => el ? Math.round(el.getBoundingClientRect().left) : null
  return {
    header: left(document.querySelector('header h1')),
    notice: left(document.querySelector('[aria-label="Recovery notices"] span')),
    tabs: left(document.querySelector('nav[aria-label="Pages"] a')),
    page: left(document.querySelector('main section')),
  }
}"""


@pytest.mark.parametrize("width", [375, 768, 1280, 1440, 1920])
def test_one_left_edge_from_the_header_to_the_page(browser, dist_url, width):
    """The header, the notices, the tabs and the page start at the same
    x at every width — at 1440 they were 32, 32, 80 and 112 px."""
    context, page, errors = _open(browser, dist_url, width=width)
    edges = page.evaluate(EDGES_JS)
    assert None not in edges.values(), edges
    assert len(set(edges.values())) == 1, (width, edges)
    assert errors == []
    context.close()


def _current(page) -> list[str]:
    return page.eval_on_selector_all(
        'nav[aria-label="Pages"] a[aria-current="page"]',
        "links => links.map(a => a.textContent)")


def _query(page) -> str:
    return page.evaluate("() => window.location.search")


def test_the_address_names_the_tab(browser, dist_url):
    context, page, errors = _open(browser, dist_url, path="/?tab=search",
                                  ready="text=Backend Engineer a")
    assert _current(page) == ["Search"]
    # Each tab is a link to its own address.
    hrefs = page.eval_on_selector_all('nav[aria-label="Pages"] a', "as => as.map(a => a.href)")
    assert [h.split("?", 1)[1] for h in hrefs] == ["tab=profile", "tab=search", "tab=settings"]

    page.get_by_role("link", name="Settings", exact=True).click()
    page.wait_for_selector("text=Appearance")
    assert _query(page) == "?tab=settings"
    assert _current(page) == ["Settings"]

    page.go_back()
    page.wait_for_selector("text=Backend Engineer a")
    assert _query(page) == "?tab=search"
    assert _current(page) == ["Search"]

    page.go_forward()
    page.wait_for_selector("text=Appearance")
    assert _current(page) == ["Settings"]
    assert errors == []
    context.close()


@pytest.mark.parametrize("path", ["", "/?tab=admin", "/?tab=constructor"])
def test_no_tab_or_an_unknown_one_opens_the_profile(browser, dist_url, path):
    context, page, errors = _open(browser, dist_url, path=path)
    assert _current(page) == ["Profile"]
    assert errors == []
    context.close()


def test_a_click_that_asks_for_a_new_tab_is_the_browsers(browser, dist_url):
    """Ctrl- or Cmd-click opens the tab's address in a new browser tab and
    leaves this page where it was."""
    context, page, errors = _open(browser, dist_url)
    # The new tab gets the fixtures too: left to the static server, its
    # event stream would join the live hub the stream tests count.
    route_fixtures(context)
    with context.expect_page() as opened:
        page.get_by_role("link", name="Settings", exact=True).click(
            modifiers=["ControlOrMeta"])
    other = opened.value
    other.wait_for_load_state()
    assert "tab=settings" in other.url
    assert _current(page) == ["Profile"]
    assert _query(page) == ""
    assert errors == []
    context.close()


def test_the_keyboard_follows_a_tab_link(browser, dist_url):
    context, page, errors = _open(browser, dist_url)
    _assert_stop(_tab_to(page, "Search"), "Search")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Backend Engineer a")
    assert _query(page) == "?tab=search"
    assert _current(page) == ["Search"]
    assert errors == []
    context.close()


def test_a_usage_limit_that_opens_the_picker_moves_the_address(browser, dist_url):
    """The picker opening by itself is a tab change like any other: the
    address follows, and Back returns to the page the user was on."""
    context, page, errors = _open(
        browser, dist_url, path="/?tab=search",
        fixtures={"/api/engines": engines_view(claude={**WAITING, "episode": "nav-1"})},
        ready="text=switch models to keep working now")
    assert _query(page) == "?tab=settings"
    assert _current(page) == ["Settings"]
    page.go_back()
    page.wait_for_selector("text=Backend Engineer a")
    assert _current(page) == ["Search"]
    assert errors == []
    context.close()
