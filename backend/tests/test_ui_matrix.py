"""Every named state, checked three ways: no unexpected console or page
errors, zero axe-core violations under its default rules, and nothing
wider than the page at every breakpoint edge.

"Zero violations" is exactly that — the tested states, axe-core's enabled
rules at the pinned version, iframe content excluded. Items axe marks
incomplete are not counted either way; they are listed for review in the
session's terminal summary, which the CI script keeps in its log.
"""

import pytest

from backend.tests.test_ui_layout import OVERFLOW_JS
from backend.tests.ui_harness import AXE_INCOMPLETE, AXE_PATH, axe_check
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
        AXE_INCOMPLETE.append((f"{state.name}/{scheme}", result["incomplete"]))
    context.close()


# A document whose only violation is inside a frame: an image without alt.
FRAMED = """<!doctype html><html lang="en"><head><title>Outer</title></head>
<body><main><h1>Outer</h1><iframe title="Inner" srcdoc="&lt;!doctype html&gt;
&lt;html lang='en'&gt;&lt;head&gt;&lt;title&gt;Inner&lt;/title&gt;&lt;/head&gt;
&lt;body&gt;&lt;main&gt;&lt;h1&gt;Inner&lt;/h1&gt;&lt;img src='x.png'&gt;
&lt;/main&gt;&lt;/body&gt;&lt;/html&gt;"></iframe></main></body></html>"""


def test_the_axe_check_excludes_frame_content_and_nothing_else(browser, dist_url):
    """The exclusion the check claims is real: with axe able to see into
    the frame, the check reports nothing from it, and an unrestricted run
    of the same page reports the frame's violation."""
    context = browser.new_context()
    page = context.new_page()
    # Served, not set_content: axe only talks to frames of its own origin,
    # and about:blank has none. Served in place of the app, so nothing of
    # the app — its event stream above all — is started by this test.
    page.route("**/*", lambda route, *_: route.fulfill(
        status=200, content_type="text/html", body=FRAMED))
    page.goto(dist_url)
    inner = next(f for f in page.frames if f != page.main_frame)
    inner.add_script_tag(path=str(AXE_PATH))
    assert axe_check(page)["violations"] == []
    everything = page.evaluate("() => axe.run().then(r => r.violations.map(v => v.id))")
    assert "image-alt" in everything, everything
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
