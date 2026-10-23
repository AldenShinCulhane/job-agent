"""Keyword coverage on a finished package's row (PLAN.md P8c), in the
built app over fixtures: how many of the posting's skills the documents
mention, where each is mentioned, a short name the text isn't searched
for, and no coverage said as unavailable, never as none."""

import copy

from backend.tests.ui_harness import _package, route_fixtures
from backend.tests.ui_states import COVERAGE, MENTIONS, MENTIONS_SUMMARY

# Enough newer packages to push every earlier row into Earlier packages.
NEWER = [_package(pid, "done", job=f"n{pid}") for pid in (14, 13, 12, 11)]


def _open(browser, dist_url, fixtures=COVERAGE):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table.update(copy.deepcopy(fixtures))
    page.goto(dist_url + "/?tab=search")
    page.wait_for_selector("text=Backend Engineer a")
    return context, page, errors, table


def _entry(page, title):
    rail = page.get_by_role("region", name="Package list")
    return rail.locator("li", has=page.locator("p.font-medium", has_text=title))


def _mentions(entry):
    return [" ".join(t.split()) for t in entry.locator("details li").all_text_contents()]


def _reread(page):
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }""")


def _focused(page):
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return page.evaluate("() => document.activeElement.getAttribute('aria-label')")


def _toggle(summary):
    """Opens or closes the summary's fold from the keyboard, and waits for
    the browser's toggle event, which is what the page keeps. It comes up
    to a frame later, and two toggles within one frame are one event."""
    summary.evaluate("""el => { window.toggled = new Promise((r) =>
        el.parentElement.addEventListener('toggle', r, { once: true })) }""")
    summary.press("Enter")
    summary.page.evaluate("() => window.toggled")


def test_a_finished_package_says_where_its_documents_mention_each_skill(browser, dist_url):
    context, page, errors, _ = _open(browser, dist_url)
    entry = _entry(page, "Backend Engineer a")
    summary = entry.locator("summary", has_text=MENTIONS_SUMMARY)
    assert summary.get_attribute("aria-label") == f"{MENTIONS_SUMMARY} for Backend Engineer a"
    caveat = entry.get_by_text("not proof of experience")
    assert not caveat.is_visible()
    # A disclosure, opened from the keyboard like any other.
    summary.focus()
    page.keyboard.press("Enter")
    caveat.wait_for()
    assert _mentions(entry) == [
        "✓ Mentioned: Python (skills, resume text, cover letter)",
        "✓ Mentioned: Kafka (resume text)",
        "✓ Mentioned: Go (skills; text not searched (short name))",
        "✗ Not mentioned: Terraform (not mentioned)",
        "✓ Mentioned: Docker (cover letter)",
        "? Unknown: R (not in skills; text not searched (short name))",
        "✗ Not mentioned: Rust (not mentioned)",
    ]
    assert entry.locator("details p.font-medium").all_inner_texts() == [
        "Required", "Nice to have"]
    # A required skill nothing mentions is the one to look at; a
    # preferred one, or a short name nobody searched for, is not a warning.
    tones = [c.split()[0] for c in
             entry.locator("details li").evaluate_all("els => els.map(e => e.className)")]
    assert tones == ["text-ok-fg", "text-ok-fg", "text-ok-fg", "text-warn-fg",
                     "text-ok-fg", "text-ink-2", "text-ink-2"]
    assert errors == []
    context.close()


def test_no_coverage_is_unavailable_never_none(browser, dist_url):
    """A package with no coverage says it's unavailable, not that nothing
    is mentioned; a posting that names no skills says that. Only a
    finished package says either."""
    context, page, errors, _ = _open(browser, dist_url)
    unknown = _entry(page, "Backend Engineer c")
    assert unknown.get_by_text("Keyword mentions unavailable").is_visible()
    assert unknown.locator("details").count() == 0
    assert "Mentions" not in unknown.inner_text()
    none = _entry(page, "Backend Engineer b")
    assert none.get_by_text("The posting names no skills").is_visible()
    assert none.locator("details").count() == 0
    failed = _entry(page, "Backend Engineer d")
    assert failed.get_by_text("compile failed").is_visible()
    for text in ("Keyword mentions unavailable", "The posting names no skills", "Mentions"):
        assert failed.get_by_text(text).count() == 0
    assert errors == []
    context.close()


def test_an_open_list_follows_the_packages_read_again(browser, dist_url):
    """The list stays open while the packages are read again, and says
    what the read says: a package that finishes gets its coverage."""
    context, page, errors, table = _open(browser, dist_url)
    entry = _entry(page, "Backend Engineer a")
    entry.get_by_text(MENTIONS_SUMMARY).click()
    entry.get_by_text("not proof of experience").wait_for()
    mentions = copy.deepcopy(MENTIONS)
    mentions[3]["letter"] = True  # Terraform, now in the letter
    packages = table["/api/packages"]["packages"]
    packages[0]["coverage"] = mentions
    packages[3].update(status="done", error=None, coverage=MENTIONS[:2])
    _reread(page)
    entry.get_by_text("Mentions 4 of 4 required and 1 of 3 preferred skills").wait_for()
    assert entry.get_by_text("not proof of experience").is_visible()
    assert "✓ Mentioned: Terraform (cover letter)" in _mentions(entry)
    finished = _entry(page, "Backend Engineer d")
    finished.get_by_text("Mentions 2 of 2 required skills").wait_for()
    assert errors == []
    context.close()


def test_an_open_list_moved_between_the_package_lists_keeps_open_and_focused(browser, dist_url):
    """Newer packages push the row into Earlier packages, where it is
    built again, and taking them away brings it back. Each way the list
    stays open and the focus stays on its summary, with the fold it moved
    into opened."""
    context, page, errors, table = _open(browser, dist_url)
    label = f"{MENTIONS_SUMMARY} for Backend Engineer a"
    _toggle(page.locator(f"summary[aria-label='{label}']"))
    caveat = page.get_by_text("not proof of experience")
    caveat.wait_for()
    packages = table["/api/packages"]["packages"]
    packages[:0] = copy.deepcopy(NEWER)
    _reread(page)
    page.get_by_text("Earlier packages (3)").wait_for()
    assert _focused(page) == label
    assert caveat.is_visible()
    del packages[:len(NEWER)]
    _reread(page)
    page.get_by_text("Earlier packages").wait_for(state="detached")
    assert _focused(page) == label
    assert caveat.is_visible()
    assert errors == []
    context.close()


def test_a_list_moved_after_the_focus_left_it_takes_nothing(browser, dist_url):
    """Once the user has moved the focus on, the row moving into Earlier
    packages leaves it where they put it, and leaves the fold closed.
    The list is still open inside."""
    context, page, errors, table = _open(browser, dist_url)
    _toggle(page.locator(f"summary[aria-label='{MENTIONS_SUMMARY} for Backend Engineer a']"))
    caveat = page.get_by_text("not proof of experience")
    caveat.wait_for()
    rail = page.get_by_role("region", name="Package list")
    rail.focus()
    table["/api/packages"]["packages"][:0] = copy.deepcopy(NEWER)
    _reread(page)
    earlier = page.get_by_text("Earlier packages (3)")
    earlier.wait_for()
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert rail.evaluate("el => el === document.activeElement")
    assert not caveat.is_visible()
    earlier.click()
    assert caveat.is_visible()
    assert errors == []
    context.close()


def test_a_closed_list_moved_into_earlier_packages_stays_closed_and_focused(browser, dist_url):
    """Opened and closed again, the list moves closed. The fold it moved
    into opens to show the summary that has the focus, and only that."""
    context, page, errors, table = _open(browser, dist_url)
    label = f"{MENTIONS_SUMMARY} for Backend Engineer a"
    summary = page.locator(f"summary[aria-label='{label}']")
    caveat = page.get_by_text("not proof of experience")
    _toggle(summary)
    caveat.wait_for()
    _toggle(summary)
    caveat.wait_for(state="hidden")
    table["/api/packages"]["packages"][:0] = copy.deepcopy(NEWER)
    _reread(page)
    page.get_by_text("Earlier packages (3)").wait_for()
    assert _focused(page) == label
    assert summary.is_visible()
    assert not caveat.is_visible()
    assert errors == []
    context.close()


def test_a_summary_that_goes_hands_the_focus_to_the_packages_heading(browser, dist_url):
    """A read that takes the package's coverage away takes its summary,
    and the focus goes to the Packages heading, not the top of the page."""
    context, page, errors, table = _open(browser, dist_url)
    entry = _entry(page, "Backend Engineer a")
    entry.locator(f"summary[aria-label='{MENTIONS_SUMMARY} for Backend Engineer a']").focus()
    table["/api/packages"]["packages"][0]["coverage"] = None
    _reread(page)
    entry.get_by_text("Keyword mentions unavailable").wait_for()
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert page.get_by_role("heading", name="Packages", exact=True).evaluate(
        "el => el === document.activeElement")
    assert errors == []
    context.close()
