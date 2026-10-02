"""The design tokens as actually rendered, in both colour schemes.

scripts/contrast.mjs checks the token TABLE. This checks what the browser
paints from it: real chips, notes, placeholders and buttons, against the
background actually behind each of them, plus the keyboard focus ring —
because a token pair can pass on paper while an element ends up over a
different surface than the table assumed.
"""

import json
import os
from pathlib import Path

import pytest

from backend.tests.ui_harness import (
    MEASURE_JS, PLACEHOLDER_JS, engines_view, luminance_of_css_rgb,
    resolved_token, route_fixtures,
)

AA_TEXT = 4.5
AA_NON_TEXT = 3.0

# What each tab shows once ALL of its own fetches have landed — fixture
# content, not the tab's static heading, which paints before the data.
TAB_READY = {
    "Profile": ["text=Your profile"],
    "Search": ["text=Backend Engineer a", "text=Ready", "text=Daily cap reached"],
    "Settings": ["text=Documents", "text=No LaTeX engine found"],
}

TEXT_SELECTOR = ", ".join([
    '[class*="-fg"]',      # every chip and note: text-ok-fg, text-warn-fg, …
    ".text-ink", ".text-ink-2", ".text-ink-3",
    ".text-primary-ink",   # filled-button text over the fill
])


def _shot(page, name: str) -> None:
    target = os.environ.get("UI_SHOTS_DIR")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(target) / f"{name}.png"), full_page=True)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_rendered_text_meets_aa_on_every_surface(browser, dist_url, scheme):
    context = browser.new_context(color_scheme=scheme,
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    route_fixtures(page)

    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")  # status landed
    # `color-scheme` computes to the declared list; what light-dark() picked
    # shows in the resolved tokens, so that is what says which scheme is on.
    page_lum = luminance_of_css_rgb(resolved_token(page, "bg"))
    assert (page_lum < 0.2) == (scheme == "dark"), (scheme, page_lum)

    tones_seen: set[str] = set()
    failures: list[dict] = []
    placeholders_checked = 0
    for tab, ready in TAB_READY.items():
        page.get_by_role("link", name=tab, exact=True).click()
        for selector in ready:
            page.wait_for_selector(selector)
        # The search fields fold behind the last run's summary; open them
        # so their placeholders and borders are measured too.
        edit = page.get_by_role("button", name="Edit search")
        if edit.count():
            edit.click()
            page.wait_for_selector("[placeholder]")
        for m in page.evaluate(MEASURE_JS, TEXT_SELECTOR):
            if m["ratio"] is None:
                failures.append({"why": "unmeasurable", **m})
            elif m["ratio"] < AA_TEXT:
                failures.append({"why": f"{m['ratio']:.2f} < {AA_TEXT}", **m})
        for cls in page.evaluate(
                "() => [...document.querySelectorAll('[class*=\"-bg\"], .bg-surface-2')]"
                ".flatMap(e => e.className.split(' '))"):
            if cls.endswith("-bg") or cls == "bg-surface-2":
                tones_seen.add(cls)
        for ph in page.evaluate(PLACEHOLDER_JS):
            placeholders_checked += 1
            if ph["ratio"] is None or ph["ratio"] < AA_TEXT:
                failures.append({"why": "placeholder", **ph})
        _shot(page, f"{scheme}-{tab.lower()}")

    assert not failures, failures
    # Every tone actually painted somewhere, not just defined.
    assert {"bg-ok-bg", "bg-info-bg", "bg-warn-bg", "bg-danger-bg",
            "bg-surface-2"} <= tones_seen, tones_seen
    assert placeholders_checked > 0, "no placeholder was rendered to measure"
    assert not console_errors, console_errors
    context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_keyboard_focus_ring_is_the_accent_and_visible(browser, dist_url, scheme):
    context = browser.new_context(color_scheme=scheme)
    page = context.new_page()
    route_fixtures(page)
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")

    accent = resolved_token(page, "accent")
    surface_lum = luminance_of_css_rgb(resolved_token(page, "surface"))
    accent_lum = luminance_of_css_rgb(accent)
    hi, lo = max(accent_lum, surface_lum), min(accent_lum, surface_lum)
    assert (hi + 0.05) / (lo + 0.05) >= AA_NON_TEXT

    page.keyboard.press("Tab")
    focused = page.evaluate("""() => {
      const el = document.activeElement
      const s = getComputedStyle(el)
      return { tag: el.tagName, text: (el.textContent || '').trim().slice(0, 30),
               style: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor,
               offset: s.outlineOffset }
    }""")
    assert focused["tag"] != "BODY", "Tab did not reach a control"
    assert focused["style"] == "solid" and focused["width"] == "2px", focused
    assert focused["color"] == accent, (focused, accent)
    assert focused["offset"] == "2px", focused
    context.close()


def test_disabled_controls_render_dimmed(browser, dist_url):
    context = browser.new_context(color_scheme="light")
    page = context.new_page()
    route_fixtures(page)
    page.goto(dist_url)
    page.wait_for_selector("text=Claude sign-in required")
    page.get_by_role("link", name="Settings", exact=True).click()
    for selector in TAB_READY["Settings"]:
        page.wait_for_selector(selector)
    save = page.get_by_role("button", name="Save", exact=True)
    assert save.is_disabled()  # limit unchanged from the fixture value
    assert page.evaluate("(el) => getComputedStyle(el).opacity",
                         save.element_handle()) == "0.4"
    context.close()


def test_unknown_statuses_get_the_neutral_chip(browser, dist_url):
    """Every status map falls back for a status it has no entry for: the
    raw name, in the neutral tone, still readable. Engine, LinkedIn and
    package each have their own map; the fixtures give each one a status
    the UI has never heard of."""
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))
    route_fixtures(page)
    page.route("**/api/engines", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(engines_view(claude={"status": "rebooting",
                                             "last_error": None}))))
    page.route("**/api/linkedin/status", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"state": "rate_limited", "detail": None})))

    page.goto(dist_url)
    page.wait_for_selector("text=rebooting")  # header chip, engine map
    page.get_by_role("link", name="Search", exact=True).click()
    page.wait_for_selector("text=rate_limited")  # LinkedIn map
    # The unknown package is older than the recent few, so it is folded.
    page.get_by_text("Earlier packages (1)").click()
    page.wait_for_selector("text=archived")  # package map

    chips = {m["text"]: m for m in page.evaluate(MEASURE_JS, ".text-ink-2")
             if m["text"] in {"rebooting", "rate_limited", "archived"}}
    assert set(chips) == {"rebooting", "rate_limited", "archived"}, chips
    for m in chips.values():
        assert "bg-surface-2" in m["cls"].split(), m
        assert m["ratio"] is not None and m["ratio"] >= AA_TEXT, m
    assert not console_errors, console_errors
    context.close()
