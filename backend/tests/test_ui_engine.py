"""The engine picker and what the app does when a usage limit starts
(PLAN.md §4.1a, P6a), driven through the built app over fixtures.

A limit opens the picker once per limit (quota episode), because the way
to keep working now is another provider's model — but never over work
the user would lose: unsaved edits, or an action of theirs still in
flight. Then the banner carries the same way out.
"""

import json

from backend.tests.test_ui_keyboard import _assert_stop, _focused, _tab_to
from backend.tests.ui_harness import SIGNED_IN, engines_view, route_fixtures
from backend.tests.ui_states import LOCKDOWN_FAILURE, _busy, _dirty

WAITING = {"status": "waiting_usage_exhausted", "model": "claude-sample",
           "retry_in_seconds": 1500, "last_error": "usage limit reached",
           "next_retry_at": "2026-09-15T12:25:00Z", "limit_resets_at": None,
           "credentials": SIGNED_IN}
READY = {"status": "ok", "model": "claude-sample", "last_error": None,
         "credentials": SIGNED_IN}


def _open(browser, dist_url, claude, *, episode=None, tab_ready="text=Your profile",
          fixtures=None):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table.update(fixtures or {})
    table["/api/engines"] = engines_view(claude={**claude, "episode": episode})
    page.goto(dist_url)
    page.wait_for_selector(tab_ready)
    return context, page, table, errors


def _limit_starts(page, table, episode):
    """The engine reaches its limit; the page learns of it the way it
    learns of any change — a refetch — here the one a returning tab makes."""
    table["/api/engines"] = engines_view(claude={**WAITING, "episode": episode})
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }""")
    page.wait_for_selector("text=Claude usage limit reached")


def _on_settings(page) -> bool:
    return page.get_by_role("heading", name="AI engine").count() == 1


def test_a_usage_limit_opens_the_picker_once_per_limit(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, WAITING, episode="ep-1",
                                         tab_ready="text=switch models to keep working now")
    # Opened by itself, with focus on the section so a keyboard or screen
    # reader user lands where the page changed.
    page.wait_for_function("() => document.activeElement?.id === 'engine-heading'")
    assert "Another Claude model won't help" in page.inner_text("main")

    # Leaving it is the user's call, and the same limit does not reopen it:
    # not on a refetch, not after a reload of the tab.
    page.get_by_role("button", name="Profile", exact=True).click()
    page.wait_for_selector("text=Your profile")
    _limit_starts(page, table, "ep-1")
    assert not _on_settings(page)
    page.reload()
    page.wait_for_selector("text=Your profile")
    page.wait_for_selector("text=Claude usage limit reached")
    assert not _on_settings(page)

    # A new limit is news again.
    _limit_starts(page, table, "ep-2")
    page.wait_for_selector("text=switch models to keep working now")
    assert errors == []
    context.close()


def test_the_picker_never_opens_over_unsaved_edits(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    _dirty(page)
    _limit_starts(page, table, "ep-dirty")
    page.wait_for_timeout(300)
    assert not _on_settings(page)
    assert page.get_by_label("Name", exact=True).input_value() == "Sample Person B"
    # The banner is the way there, when the user is ready.
    page.get_by_role("button", name="Switch model").click()
    page.wait_for_selector("text=switch models to keep working now")
    assert errors == []
    context.close()


def test_the_picker_never_opens_over_an_action_in_flight(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    _busy(page)  # a save that has not answered yet
    _limit_starts(page, table, "ep-busy")
    page.wait_for_timeout(300)
    assert not _on_settings(page)
    page.wait_for_selector("text=Re-rendering preview…")
    context.close()


def test_the_picker_never_opens_over_an_upload_in_flight(browser, dist_url):
    """Nothing typed, nothing unsaved: only an answer still on its way,
    which a tab switch would throw away."""
    context, page, table, errors = _open(browser, dist_url, READY,
                                         tab_ready="text=Upload your resume",
                                         fixtures={"/api/profile": {"version": None}})
    page.route("**/api/profile/upload", lambda route, *_: None)  # never answers
    page.set_input_files("input[type=file]", files=[{
        "name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n"}])
    page.wait_for_selector("text=Extracting your resume with Claude")
    _limit_starts(page, table, "ep-upload")
    page.wait_for_timeout(300)
    assert not _on_settings(page)
    page.wait_for_selector("text=Extracting your resume with Claude")
    context.close()


def test_the_picker_never_folds_a_search_being_edited(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    page.get_by_role("button", name="Search", exact=True).click()
    page.get_by_role("button", name="Edit search").click()
    field = page.get_by_placeholder("software engineer")
    field.fill("platform engineer")
    _limit_starts(page, table, "ep-search")
    page.wait_for_timeout(300)
    assert not _on_settings(page)
    assert field.input_value() == "platform engineer"
    context.close()


def test_choosing_a_codex_model_sets_it_for_every_task(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    bodies: list = []

    def select(route, *_):
        bodies.append(json.loads(route.request.post_data))
        # The server now has Codex selected and proven.
        table["/api/engines"] = engines_view(
            claude=READY, codex={"status": "ok", "model": "gpt-sample",
                                 "verified_at": "2026-09-15T12:00:00Z"},
            selection={"provider": "codex", "model": "gpt-sample"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "selection": {"provider": "codex", "model": "gpt-sample"},
            "ok": True, "verified": True, "model": "gpt-sample"}))
    page.route("**/api/engine/selection", select)

    page.get_by_role("button", name="Settings", exact=True).click()
    use = page.get_by_role("button", name="Use this model")
    assert use.is_disabled()  # nothing picked yet
    page.get_by_label("Model for every task").select_option(label="GPT Sample")
    use.click()
    page.wait_for_selector("text=Every task now uses GPT Sample — checked, and it answers.")
    assert bodies == [{"provider": "codex", "model": "gpt-sample"}]
    # The header and the picker both follow the one engine state.
    page.wait_for_selector("header >> text=Codex ready · GPT Sample")
    page.wait_for_selector("text=Every task uses GPT Sample on Codex.")
    assert use.is_disabled()
    assert errors == []
    context.close()


def test_a_model_that_cannot_run_stays_chosen_and_says_why(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)

    def select(route, *_):
        table["/api/engines"] = engines_view(
            claude={**READY, "status": "not_entitled", "model": "claude-small",
                    "models": {"claude-small": {"status": "not_entitled",
                                                "last_error": "model not_found",
                                                "verified_at": None}}},
            selection={"provider": "claude", "model": "claude-small"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "selection": {"provider": "claude", "model": "claude-small"},
            "ok": False, "verified": True, "error": "ModelNotEntitled",
            "detail": "model not_found"}))
    page.route("**/api/engine/selection", select)

    page.get_by_role("button", name="Settings", exact=True).click()
    page.get_by_label("Model for every task").select_option(label="Claude Small")
    page.get_by_role("button", name="Use this model").click()
    page.wait_for_selector("text=Claude Small is selected but can't run yet: model not_found")
    page.wait_for_selector("text=Claude Small isn't available on your Claude plan")
    page.wait_for_selector("header >> text=Model not on your plan")
    # The list says so too, and nothing switched by itself.
    assert "Claude Small — not on your plan" in page.inner_text("#engine-model")
    context.close()


LOCKED_OUT = {"gpt-mini": {"status": "unavailable", "last_error": LOCKDOWN_FAILURE,
                           "verified_at": None}}


def test_a_model_that_fails_the_lockdown_is_listed_but_never_offered(browser, dist_url):
    """The model list was checked against the lockdown: one that fails it
    shows as unavailable and cannot be picked, and the page says why."""
    context, page, table, errors = _open(browser, dist_url, READY)
    table["/api/engines"] = engines_view(claude={**READY, "episode": None},
                                         codex={"models": LOCKED_OUT})
    page.reload()
    page.get_by_role("button", name="Settings", exact=True).click()
    page.wait_for_selector("text=GPT Mini can't run under the app's safety lockdown")
    locked = page.locator("#engine-model option", has_text="GPT Mini")
    assert locked.inner_text() == "GPT Mini — unavailable"
    assert locked.is_disabled()
    assert page.locator("#engine-model option", has_text="GPT Sample").is_enabled()
    page.wait_for_selector("text=GPT Mini can't run under the app's safety lockdown on "
                           "this version of Codex.")
    assert errors == []
    context.close()


def test_the_move_off_an_unavailable_model_is_one_choice(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    table["/api/engines"] = engines_view(
        claude={**READY, "episode": None},
        codex={"status": "unavailable", "model": "gpt-mini",
               "last_error": LOCKDOWN_FAILURE, "models": LOCKED_OUT},
        selection={"provider": "codex", "model": "gpt-mini"})
    bodies: list = []

    def select(route, *_):
        bodies.append(json.loads(route.request.post_data))
        table["/api/engines"] = engines_view(
            claude={**READY, "episode": None},
            codex={"status": "ok", "model": "gpt-sample", "models": LOCKED_OUT,
                   "verified_at": "2026-09-15T12:00:00Z"},
            selection={"provider": "codex", "model": "gpt-sample"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "selection": {"provider": "codex", "model": "gpt-sample"},
            "ok": True, "verified": True, "model": "gpt-sample"}))
    page.route("**/api/engine/selection", select)
    page.reload()
    page.wait_for_selector("header >> text=Model unavailable")
    page.get_by_role("button", name="Choose model").click()
    page.wait_for_function("() => document.activeElement?.id === 'engine-heading'")
    page.wait_for_selector("text=GPT Mini can't run under the app's safety lockdown on "
                           "this version of Codex.")
    assert LOCKDOWN_FAILURE in page.inner_text("main")
    # The current choice stays shown for what it is; the way off is one pick.
    assert page.locator("#engine-model").input_value() == "codex:gpt-mini"
    page.get_by_label("Model for every task").select_option(label="GPT Sample")
    page.get_by_role("button", name="Use this model").click()
    page.wait_for_selector("text=Every task now uses GPT Sample — checked, and it answers.")
    assert bodies == [{"provider": "codex", "model": "gpt-sample"}]
    page.wait_for_selector("header >> text=Codex ready · GPT Sample")
    assert page.get_by_role("button", name="Re-check engine").is_enabled()
    assert errors == []
    context.close()


def test_a_custom_model_id_is_checked_before_it_is_sent(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    bodies: list = []
    page.route("**/api/engine/selection", lambda route, *_: (
        bodies.append(json.loads(route.request.post_data)),
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "selection": {"provider": "codex", "model": "gpt-9-preview"},
            "ok": True, "verified": True, "model": "gpt-9-preview"}))))
    page.get_by_role("button", name="Settings", exact=True).click()
    page.get_by_label("Model for every task").select_option("custom")
    page.get_by_label("Engine", exact=True).select_option("codex")
    page.get_by_label("Model ID").fill("--config evil")
    page.wait_for_selector("text=Letters, digits")
    assert page.get_by_role("button", name="Use this model").is_disabled()
    page.get_by_label("Model ID").fill("gpt-9-preview")
    page.get_by_role("button", name="Use this model").click()
    page.wait_for_selector("text=Every task now uses gpt-9-preview")
    assert bodies == [{"provider": "codex", "model": "gpt-9-preview"}]
    context.close()


def test_the_signed_out_engine_shows_how_to_sign_in(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, {}, tab_ready="text=Your profile")
    page.get_by_role("button", name="How to fix").click()
    page.wait_for_selector("text=Sign in from a terminal in the project folder:")
    assert "uv run python -m backend.core.claude_cli auth login" in page.inner_text("main")
    page.wait_for_function("() => document.activeElement?.id === 'engine-heading'")
    context.close()


def test_the_picker_by_keyboard_only(browser, dist_url):
    context, page, table, errors = _open(browser, dist_url, READY)
    bodies: list = []
    page.route("**/api/engine/selection", lambda route, *_: (
        bodies.append(json.loads(route.request.post_data)),
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "selection": {"provider": "codex", "model": "gpt-sample"},
            "ok": True, "verified": True, "model": "gpt-sample"}))))
    _assert_stop(_tab_to(page, "Settings"), "Settings")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=AI engine")
    _assert_stop(_tab_to(page, "Model for every task"))
    page.keyboard.type("GPT S")  # type-ahead picks the option, as a user would
    page.wait_for_function("() => document.getElementById('engine-model').value"
                           " === 'codex:gpt-sample'")
    _assert_stop(_tab_to(page, "Use this model"), "Use this model")
    page.keyboard.press("Enter")
    page.wait_for_selector("text=Every task now uses GPT Sample")
    assert bodies == [{"provider": "codex", "model": "gpt-sample"}]
    _assert_stop(_tab_to(page, "Re-check engine"), "Re-check engine")
    _assert_stop(_tab_to(page, "Refresh model lists"), "Refresh model lists")
    context.close()
