"""The theme preference, as the built app actually behaves.

Every case here is one the reviewer named: both system schemes, both
overrides against the opposite system, returning to System, a live system
change, persistence across reload, other tabs following, storage being
unavailable, a first paint that never shows the wrong scheme, and the
control staying reachable when the settings read fails.
"""

import json

import pytest

from backend.tests.ui_harness import (
    luminance_of_css_rgb, png_pixel, resolved_token, route_fixtures,
)


def _open(browser, dist_url, scheme, *, stored=None, block_storage=False):
    """A page on the app with `scheme` emulated as the system preference.

    `stored` is seeded through an init script, which runs on EVERY
    navigation — so tests that reload must set storage through the UI or
    `page.evaluate` instead, or the seed would silently win the reload.
    """
    context = browser.new_context(color_scheme=scheme,
                                  viewport={"width": 1100, "height": 800})
    if block_storage:
        context.add_init_script(
            "Object.defineProperty(window, 'localStorage', "
            "{ get() { throw new Error('storage blocked') } })")
    if stored is not None:
        context.add_init_script(
            f"localStorage.setItem('theme', {json.dumps(stored)})")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" else None)
    route_fixtures(page)
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    return context, page, errors


def _is_dark(page) -> bool:
    return luminance_of_css_rgb(resolved_token(page, "bg")) < 0.2


def _override(page):
    return page.evaluate("document.documentElement.dataset.theme ?? null")


def _stored(page):
    return page.evaluate(
        "(() => { try { return localStorage.getItem('theme') } "
        "catch { return 'blocked' } })()")


def _open_appearance(page) -> None:
    page.get_by_role("link", name="Settings", exact=True).click()
    page.wait_for_selector("text=Appearance")


def _choose(page, label: str) -> None:
    page.get_by_label(label, exact=True).check()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_no_preference_follows_the_system(browser, dist_url, scheme):
    context, page, errors = _open(browser, dist_url, scheme)
    assert _is_dark(page) == (scheme == "dark")
    assert _override(page) is None
    _open_appearance(page)
    assert page.get_by_label("System", exact=True).is_checked()
    assert not errors, errors
    context.close()


@pytest.mark.parametrize("stored,scheme", [("dark", "light"), ("light", "dark")])
def test_stored_override_beats_the_system(browser, dist_url, stored, scheme):
    context, page, errors = _open(browser, dist_url, scheme, stored=stored)
    assert _is_dark(page) == (stored == "dark")
    assert _override(page) == stored
    _open_appearance(page)
    assert page.get_by_label(stored.capitalize(), exact=True).is_checked()
    assert not errors, errors
    context.close()


def test_garbage_in_storage_means_system(browser, dist_url):
    context, page, errors = _open(browser, dist_url, "light", stored="purple")
    assert not _is_dark(page)
    assert _override(page) is None
    _open_appearance(page)
    assert page.get_by_label("System", exact=True).is_checked()
    assert not errors, errors
    context.close()


def test_choice_applies_at_once_survives_reload_and_system_clears_it(
        browser, dist_url):
    context, page, errors = _open(browser, dist_url, "light")
    _open_appearance(page)

    _choose(page, "Dark")
    assert _is_dark(page)
    assert _override(page) == "dark"
    assert _stored(page) == "dark"

    page.reload()
    page.wait_for_selector("text=Claude sign-in required")
    assert _is_dark(page)
    _open_appearance(page)
    assert page.get_by_label("Dark", exact=True).is_checked()

    _choose(page, "System")
    assert not _is_dark(page)  # the emulated system is light
    assert _override(page) is None
    assert _stored(page) is None
    assert page.evaluate("document.documentElement.style.colorScheme") == ""

    page.reload()
    page.wait_for_selector("text=Claude sign-in required")
    assert not _is_dark(page)
    assert _override(page) is None
    assert not errors, errors
    context.close()


def test_live_system_change_is_followed_without_a_reload(browser, dist_url):
    context, page, errors = _open(browser, dist_url, "light")
    assert not _is_dark(page)
    page.emulate_media(color_scheme="dark")
    assert _is_dark(page)
    page.emulate_media(color_scheme="light")
    assert not _is_dark(page)

    # An override is exactly what stops that following.
    _open_appearance(page)
    _choose(page, "Light")
    page.emulate_media(color_scheme="dark")
    assert not _is_dark(page)
    assert not errors, errors
    context.close()


def test_other_tabs_follow_a_choice_and_a_clear(browser, dist_url):
    context, first, errors = _open(browser, dist_url, "light")
    second = context.new_page()
    route_fixtures(second)
    second.goto(dist_url)
    second.wait_for_selector("text=Claude sign-in required")
    _open_appearance(first)
    _open_appearance(second)

    _choose(first, "Dark")
    second.wait_for_function(
        "document.documentElement.dataset.theme === 'dark'")
    assert _is_dark(second)
    assert second.get_by_label("Dark", exact=True).is_checked()

    # Site data cleared elsewhere: key === null on the storage event.
    first.evaluate("localStorage.clear()")
    second.wait_for_function(
        "document.documentElement.dataset.theme === undefined")
    assert not _is_dark(second)
    assert second.get_by_label("System", exact=True).is_checked()
    assert not errors, errors
    context.close()


def test_a_tab_that_never_opened_settings_still_follows(browser, dist_url):
    """The listener belongs to the document, not to the Settings page: a
    tab left on Profile, then Search, follows another tab's choice and
    its return to System without ever showing the control."""
    context, sender, errors = _open(browser, dist_url, "light")
    receiver = context.new_page()
    route_fixtures(receiver)
    receiver.goto(dist_url)
    receiver.wait_for_selector("text=Claude sign-in required")
    receiver.wait_for_selector("text=Your profile")  # on Profile, and stays
    _open_appearance(sender)

    _choose(sender, "Dark")
    receiver.wait_for_function(
        "document.documentElement.dataset.theme === 'dark'")
    assert _is_dark(receiver)

    receiver.get_by_role("link", name="Search", exact=True).click()
    receiver.wait_for_selector("text=Daily cap reached")
    _choose(sender, "System")
    receiver.wait_for_function(
        "document.documentElement.dataset.theme === undefined")
    assert not _is_dark(receiver)

    # The control, opened only now, agrees with what the document shows.
    _open_appearance(receiver)
    assert receiver.get_by_label("System", exact=True).is_checked()
    assert not errors, errors
    context.close()


def test_blocked_storage_keeps_the_tab_usable(browser, dist_url):
    context, page, errors = _open(browser, dist_url, "light",
                                  block_storage=True)
    assert not _is_dark(page)
    _open_appearance(page)
    assert page.get_by_label("System", exact=True).is_checked()

    _choose(page, "Dark")  # switches this document; cannot be stored
    assert _is_dark(page)
    assert _override(page) == "dark"
    assert _stored(page) == "blocked"

    # The choice outlives the Settings page: it is held in memory by the
    # document, not re-read from the storage that refused it.
    page.get_by_role("link", name="Profile", exact=True).click()
    page.wait_for_selector("text=Your profile")
    assert _is_dark(page)
    _open_appearance(page)
    assert page.get_by_label("Dark", exact=True).is_checked()
    assert _is_dark(page)
    assert not errors, errors
    context.close()


@pytest.mark.parametrize("stored,scheme", [("dark", "light"), ("light", "dark")])
def test_first_paint_is_the_stored_scheme_before_any_asset_arrives(
        browser, dist_url, stored, scheme):
    """The production build with the app bundle held back: the bootstrap
    and the stylesheet alone must already paint the stored scheme, or a
    user with the override opposite their system sees a flash of the
    wrong one on every load until React runs.

    The stylesheet is not held: a `<link rel="stylesheet">` in `<head>` is
    render-blocking, so nothing paints until it arrives — there is no
    frame to flash in. The app bundle is what a slow load delays."""
    context = browser.new_context(color_scheme=scheme,
                                  viewport={"width": 400, "height": 300})
    context.add_init_script(
        f"localStorage.setItem('theme', {json.dumps(stored)})")
    page = context.new_page()
    held = []
    page.route("**/*.js", lambda route: held.append(route))
    route_fixtures(page)

    page.goto(dist_url, wait_until="commit")
    page.wait_for_function(
        "document.documentElement.dataset.theme !== undefined")
    page.wait_for_function("document.styleSheets.length > 0")
    page.wait_for_timeout(250)
    assert held, "the bundle was not held back, so this proved nothing"
    assert page.evaluate("document.getElementById('root').childElementCount") == 0
    assert page.evaluate(
        "getComputedStyle(document.documentElement).colorScheme") == stored
    r, g, b = png_pixel(page.screenshot(), 8, 8)
    painted_dark = (r + g + b) / 3 < 100
    assert painted_dark == (stored == "dark"), (stored, scheme, (r, g, b))

    for route in held:
        route.continue_()
    page.wait_for_selector("text=Claude sign-in required")
    assert _is_dark(page) == (stored == "dark")
    context.close()


def test_appearance_stays_reachable_when_the_settings_read_fails(
        browser, dist_url):
    context = browser.new_context(color_scheme="light")
    page = context.new_page()
    route_fixtures(page)
    page.route("**/api/settings", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body=json.dumps({"detail": "settings store unavailable"})))
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    _open_appearance(page)
    page.wait_for_selector("text=settings store unavailable")
    _choose(page, "Dark")
    assert _is_dark(page)
    # The engine section does not depend on that read either.
    assert page.get_by_role("button", name="Re-check engine").is_visible()
    context.close()
