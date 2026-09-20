"""Every path a keyboard user takes, without a pointer.

Each stop must show the focus ring and sit unobscured inside the viewport,
including where an action removes the control that had focus — a folded
editor, a bar that goes away, buttons replaced by a chip — and focus has
to be put somewhere sensible rather than dropped to the top of the page.
"""

import json

from backend.tests.test_ui_theme import _is_dark
from backend.tests.ui_harness import profile_payload, route_fixtures

FOCUS_JS = """() => {
  const el = document.activeElement
  const r = el.getBoundingClientRect()
  const cs = getComputedStyle(el)
  const label = el.labels && el.labels[0] ? el.labels[0].textContent.trim() : ''
  const hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2)
  return {
    tag: el.tagName,
    name: el.getAttribute('aria-label') || label || (el.textContent || '').trim().slice(0, 40)
      || el.placeholder || '',
    outline: cs.outlineStyle + ' ' + cs.outlineWidth,
    inViewport: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
    unobscured: hit !== null && (hit === el || el.contains(hit)),
  }
}"""


def _focused(page):
    return page.evaluate(FOCUS_JS)


def _tab_to(page, name: str, *, limit=80, key="Tab"):
    for _ in range(limit):
        page.keyboard.press(key)
        f = _focused(page)
        if f["name"].startswith(name):
            return f
    raise AssertionError(f"never reached a control named {name!r}; last: {_focused(page)}")


def _assert_stop(f, name: str | None = None):
    """A keyboard stop: ring on, in view, not under anything."""
    if name is not None:
        assert f["name"].startswith(name), f
    assert f["outline"] == "solid 2px", f
    assert f["inViewport"], f
    assert f["unobscured"], f


def _open(browser, dist_url, fixtures=None):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    table = route_fixtures(page)
    table.update(fixtures or {})
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    return context, page, table


def _go_to_tab(page, tab: str, ready: str):
    _assert_stop(_tab_to(page, tab), tab)
    page.keyboard.press("Enter")
    page.wait_for_selector(ready)


def test_search_and_the_gate_by_keyboard_only(browser, dist_url):
    context, page, table = _open(browser, dist_url)
    bodies: list = []

    def generate(route, *_):
        bodies.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"package_ids": [9], "created": 1}))
    page.route("**/api/packages/generate", generate)
    _go_to_tab(page, "Search", "text=Backend Engineer a")

    # Unfold the editor: focus lands in the first field.
    _assert_stop(_tab_to(page, "Edit search"))
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => document.activeElement.placeholder === 'software engineer'")
    _assert_stop(_focused(page), "Keywords")
    page.keyboard.type(" python")
    # Fold it again: focus returns to the button that unfolds it.
    _assert_stop(_tab_to(page, "Cancel"))
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Edit search")
    page.wait_for_function("() => document.activeElement.textContent === 'Edit search'")
    _assert_stop(_focused(page), "Edit search")

    # A filter, toggled with Space, twice.
    _assert_stop(_tab_to(page, "Easy Apply only"))
    page.keyboard.press("Space")
    page.wait_for_selector("text=Jobs (1 of 4)")
    page.keyboard.press("Space")
    page.wait_for_selector("text=Jobs (4)")

    # Select a row, open its details, ask for the explanation.
    _assert_stop(_tab_to(page, "Select Backend Engineer a"))
    page.keyboard.press("Space")
    page.wait_for_selector("text=1 selected")
    _assert_stop(_tab_to(page, "Details"))
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Build APIs.")
    _assert_stop(_tab_to(page, "Explain fit"))
    page.keyboard.press("Enter")
    page.wait_for_selector("text=A close fit on the backend work.")

    # The gate itself, then where focus goes once the bar is gone.
    _assert_stop(_tab_to(page, "Generate packages"))
    page.keyboard.press("Enter")
    page.wait_for_function("() => !document.body.innerText.includes(' selected')")
    page.wait_for_function("() => document.activeElement.textContent === 'Packages'")
    _assert_stop(_focused(page), "Packages")
    assert bodies == [{"job_ids": ["a"], "run_id": 7}]
    context.close()


def test_clear_selection_by_keyboard_only(browser, dist_url):
    context, page, table = _open(browser, dist_url)
    _go_to_tab(page, "Search", "text=Backend Engineer a")
    _assert_stop(_tab_to(page, "Select Backend Engineer b"))
    page.keyboard.press("Space")
    page.wait_for_selector("text=1 selected")
    _assert_stop(_tab_to(page, "Clear selection"))
    page.keyboard.press("Enter")
    page.wait_for_function("() => !document.body.innerText.includes(' selected')")
    page.wait_for_function(
        "() => document.activeElement.textContent.startsWith('Jobs (')")
    _assert_stop(_focused(page), "Jobs (")
    context.close()


def test_upload_save_and_confirm_by_keyboard_only(browser, dist_url):
    context, page, table = _open(browser, dist_url,
                                 {"/api/profile": {"version": None}})
    _go_to_tab(page, "Profile", "text=Upload your resume")

    # The button opens the browser's chooser; the file arrives through it.
    uploaded = profile_payload(6, live=4, name="Uploaded Person")
    table["/api/profile"] = uploaded  # what every later refetch must see
    table[("POST", "/api/profile/upload")] = uploaded
    _assert_stop(_tab_to(page, "Choose file"))
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Enter")
    chooser.value.set_files({"name": "resume.pdf", "mimeType": "application/pdf",
                             "buffer": b"%PDF-1.4\n% synthetic\n"})
    page.wait_for_selector("text=Draft v6")
    page.wait_for_function("() => document.activeElement.tagName === 'H2'")
    _assert_stop(_focused(page), "Your profile")
    assert page.get_by_label("Name", exact=True).input_value() == "Uploaded Person"

    # Edit, save: the button that had focus is disabled by the save, so
    # focus moves to the heading instead of vanishing.
    saved = profile_payload(6, live=4, name="Uploaded Person B")
    table[("PUT", "/api/profile")] = saved
    table["/api/profile"] = saved
    _assert_stop(_tab_to(page, "Name"))
    page.keyboard.press("End")
    page.keyboard.type(" B")
    page.wait_for_selector("text=Unsaved edits")
    _assert_stop(_tab_to(page, "Save & re-render"))
    page.keyboard.press("Enter")
    page.wait_for_function("() => !document.body.innerText.includes('Unsaved edits')")
    page.wait_for_function("() => document.activeElement.tagName === 'H2'")
    _assert_stop(_focused(page), "Your profile")

    # Confirm: the buttons become a chip.
    confirmed = profile_payload(6, confirmed=True, live=6, name="Uploaded Person B")
    table[("POST", "/api/profile/confirm")] = confirmed
    table["/api/profile"] = confirmed
    _assert_stop(_tab_to(page, "Looks right"))
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Confirmed")
    page.wait_for_function("() => document.activeElement.tagName === 'H2'")
    _assert_stop(_focused(page), "Your profile")
    context.close()


def test_discard_by_keyboard_only(browser, dist_url):
    context, page, table = _open(browser, dist_url)
    _go_to_tab(page, "Profile", "text=Draft v5")
    live = profile_payload(4, confirmed=True, live=4)
    table[("POST", "/api/profile/discard")] = live
    table["/api/profile"] = live
    _assert_stop(_tab_to(page, "Discard draft"))
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Live v4")
    page.wait_for_function("() => document.activeElement.tagName === 'H2'")
    _assert_stop(_focused(page), "Your profile")
    context.close()


def test_appearance_by_keyboard_only(browser, dist_url):
    context, page, table = _open(browser, dist_url)
    _go_to_tab(page, "Settings", "text=Appearance")
    _assert_stop(_tab_to(page, "System"))  # the checked radio takes the stop
    page.keyboard.press("ArrowRight")
    _assert_stop(_focused(page), "Light")
    assert not _is_dark(page)
    page.keyboard.press("ArrowRight")
    _assert_stop(_focused(page), "Dark")
    assert _is_dark(page)
    assert page.get_by_label("Dark", exact=True).is_checked()
    context.close()


def test_the_preview_fallback_fetches_the_same_pdf_by_keyboard(browser, dist_url):
    """The preview pane is the browser's PDF viewer in an iframe. With that
    request blocked, the visible link still opens the same file — the same
    profile version, inline — in its own tab, through its own request."""
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    route_fixtures(page)
    fetched: list[str] = []

    def preview(route, *_):
        # The pane's request says so in its fetch metadata; the tab the
        # link opens is a top-level document.
        if route.request.headers.get("sec-fetch-dest") == "iframe":
            route.abort()  # the pane's own request: gone
            return
        fetched.append(route.request.url)
        route.fulfill(status=200, content_type="application/pdf",
                      body=b"%PDF-1.4\n% synthetic\n")
    # On the context, so the tab the link opens is covered too.
    context.route("**/api/profile/preview.pdf*", preview)

    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    _go_to_tab(page, "Profile", "text=Draft v5")
    assert fetched == [], "the pane's request must not have got through"

    _assert_stop(_tab_to(page, "Open the preview PDF"))
    with context.expect_page():
        page.keyboard.press("Enter")
    # Headless Chromium has no PDF viewer, so the new tab downloads rather
    # than navigates and never shows a URL; the request it made is the
    # evidence, and it must be the pane's exact file.
    for _ in range(50):
        if fetched:
            break
        page.wait_for_timeout(100)
    assert [u.split("/api")[-1] for u in fetched] == [
        "/profile/preview.pdf?version=5&inline=1"], fetched
    context.close()
