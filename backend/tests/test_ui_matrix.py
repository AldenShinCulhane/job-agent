"""Every named state, checked three ways: no unexpected console or page
errors, zero axe-core violations under its default rules, and nothing
wider than the page at every breakpoint edge.

"Zero violations" is exactly that — the tested states, axe-core's enabled
rules at the pinned version, browser-rendered PDF content excluded. Items
axe marks incomplete are printed for review, not counted either way.
"""

import pytest

from backend.tests.test_ui_layout import OVERFLOW_JS
from backend.tests.ui_harness import axe_check
from backend.tests.ui_states import STATES, STATE_IDS, open_state

WIDTHS = [375, 639, 640, 767, 768, 1279, 1280]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("state", STATES, ids=STATE_IDS)
def test_state_is_clean(browser, dist_url, state, scheme):
    context, page, errors = open_state(browser, dist_url, state, scheme=scheme)
    result = axe_check(page)
    assert result["violations"] == [], (state.name, scheme, result["violations"])
    assert errors == [], (state.name, scheme, errors)
    if result["incomplete"]:
        print(f"axe needs-review ({state.name}, {scheme}): {result['incomplete']}")
    context.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_nothing_wider_than_the_page_in_any_state(browser, dist_url, width):
    for state in STATES:
        context, page, errors = open_state(browser, dist_url, state, width=width)
        scroll_w, client_w, escaped = page.evaluate(OVERFLOW_JS)
        assert scroll_w == client_w, (state.name, width, scroll_w, client_w)
        assert escaped == [], (state.name, width, escaped)
        assert errors == [], (state.name, width, errors)
        context.close()
