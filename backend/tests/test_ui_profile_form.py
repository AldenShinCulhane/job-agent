"""The profile editor as the built app lays it out, over fixtures: every
bullet shown in full, the preview kept in view beside the form, and
links from anywhere in the form to each of its sections.
"""

import copy

import pytest

from backend.tests.test_ui_keyboard import _assert_stop, _focused, _tab_to
from backend.tests.ui_harness import profile_payload, route_fixtures

LONG_BULLET = ("Built and ran the sample ingestion service end to end: "
               "schema design, a paced crawler, retries with backoff, "
               "structured logging, dashboards and alerts, a weekly on-call "
               "rotation, and the runbook the next team used to take it over. ") * 3


def _profile(bullets: int = 6):
    payload = profile_payload()
    experience = payload["profile"]["experiences"][0]
    experience["bullets"] = [LONG_BULLET] + ["Did things"] * (bullets - 1)
    payload["profile"]["experiences"] = [copy.deepcopy(experience) for _ in range(3)]
    return payload


def _open(browser, dist_url, *, width=1440, height=900):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table["/api/profile"] = _profile()
    page.goto(dist_url)
    page.wait_for_selector("text=Your profile")
    return context, page, errors


def _fit(textarea) -> dict:
    return textarea.evaluate(
        "t => ({scroll: t.scrollHeight, client: t.clientHeight, height: t.offsetHeight,"
        " line: parseFloat(getComputedStyle(t).lineHeight)})")


def test_a_long_bullet_shows_every_line(browser, dist_url):
    # One column at this width, so a narrower window is a narrower box.
    context, page, errors = _open(browser, dist_url, width=900)
    bullet = page.get_by_label("bullet 1", exact=True).first
    fit = _fit(bullet)
    assert fit["scroll"] <= fit["client"], fit          # nothing hidden
    assert fit["client"] > 4 * fit["line"], fit         # more than the old fixed box
    short = _fit(page.get_by_label("bullet 2", exact=True).first)
    assert short["client"] < fit["client"]              # a short one stays short

    # Typing more grows it.
    bullet.click()
    page.keyboard.press("End")
    page.keyboard.type(" And one more line of detail." * 6)
    grown = _fit(bullet)
    assert grown["client"] > fit["client"] and grown["scroll"] <= grown["client"], grown

    # A narrower window wraps it to more lines; it grows to hold them.
    page.set_viewport_size({"width": 500, "height": 900})
    page.wait_for_function(
        f"t => t.scrollHeight <= t.clientHeight && t.offsetHeight > {grown['height']}",
        arg=bullet.element_handle())
    assert errors == []
    context.close()


def _preview_box(page):
    return page.get_by_title("Resume preview").bounding_box()


@pytest.mark.parametrize("height", [900, 420])
def test_the_preview_stays_in_view_while_the_form_scrolls(browser, dist_url, height):
    """One screen tall, whatever the screen: a short window gets a
    shorter pane rather than a pane that runs off the bottom."""
    context, page, errors = _open(browser, dist_url, width=1440, height=height)
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    box = _preview_box(page)
    assert page.evaluate("() => window.scrollY") > 300
    assert box["y"] >= 0 and box["y"] + box["height"] <= height, box
    # The link to the same file in a new tab is in view too.
    link = page.get_by_role("link", name="Open the preview PDF in a new tab")
    link_box = link.bounding_box()
    assert link_box["y"] + link_box["height"] <= height, link_box
    assert errors == []
    context.close()


def test_below_xl_the_preview_follows_the_form(browser, dist_url):
    """One column: the preview comes after the form and scrolls with it."""
    context, page, errors = _open(browser, dist_url, width=1024, height=900)
    form = page.get_by_role("heading", name="Your profile").bounding_box()
    assert _preview_box(page)["y"] > form["y"]
    page.evaluate("() => window.scrollTo(0, 400)")
    assert _preview_box(page)["y"] > 0  # not pinned to the top
    assert errors == []
    context.close()


SECTIONS = ["Experience", "Projects", "Education", "Skills"]


@pytest.mark.parametrize("section", SECTIONS)
def test_a_section_link_takes_focus_to_its_section(browser, dist_url, section):
    context, page, errors = _open(browser, dist_url, width=1440, height=700)
    nav = page.get_by_role("navigation", name="Profile sections")
    nav.get_by_role("link", name=section, exact=True).click()
    page.wait_for_function("(name) => document.activeElement?.textContent === name",
                           arg=section)
    heading = page.get_by_role("heading", name=section, exact=True).bounding_box()
    bar = nav.bounding_box()
    # Below the sticky links, not under them, and on screen.
    assert heading["y"] >= bar["y"] + bar["height"] - 1, (heading, bar)
    assert heading["y"] + heading["height"] <= 700
    # The anchor is the section's; the tab stays the profile.
    assert page.get_by_role("heading", name="Your profile").count() == 1
    assert page.eval_on_selector_all(
        'nav[aria-label="Pages"] a[aria-current="page"]',
        "links => links.map(a => a.textContent)") == ["Profile"]
    assert errors == []
    context.close()


def test_the_section_links_stay_in_reach(browser, dist_url):
    context, page, errors = _open(browser, dist_url, width=1440, height=700)
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    bar = page.get_by_role("navigation", name="Profile sections").bounding_box()
    assert 0 <= bar["y"] <= 1, bar
    assert errors == []
    context.close()


def test_the_keyboard_follows_a_section_link(browser, dist_url):
    context, page, errors = _open(browser, dist_url, width=1440, height=700)
    _assert_stop(_tab_to(page, "Skills"), "Skills")
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.activeElement?.id === 'profile-skills'")
    # The next Tab goes on from the section, not from the top of the page.
    page.keyboard.press("Tab")
    assert _focused(page)["name"].startswith("+ skill group"), _focused(page)
    assert errors == []
    context.close()
