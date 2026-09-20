"""Shared fixtures. The browser-level ones live in ui_harness.py; they are
registered here once so every UI test module sees the same session-scoped
Chromium and static server instead of each launching its own.

With UI_TESTS_REQUIRED=1 the browser tests are a gate: a skipped UI test,
or a run in which none executed, fails the session. Skips elsewhere in
the suite (the engine-dependent ones) are unaffected."""

import pytest

from backend.tests.ui_harness import UI_REQUIRED, browser, dist_url, events  # noqa: F401

_UI_OUTCOMES: dict[str, int] = {"passed": 0, "failed": 0, "skipped": 0}


def pytest_runtest_logreport(report):
    if "/test_ui_" not in report.nodeid and not report.nodeid.startswith("backend/tests/test_ui_"):
        return
    if report.when == "call" or (report.when == "setup" and report.outcome == "skipped"):
        _UI_OUTCOMES[report.outcome] = _UI_OUTCOMES.get(report.outcome, 0) + 1


def pytest_sessionfinish(session, exitstatus):
    if not UI_REQUIRED:
        return
    ran = _UI_OUTCOMES["passed"] + _UI_OUTCOMES["failed"]
    problem = None
    if ran == 0:
        problem = "UI_TESTS_REQUIRED=1 but no UI test executed"
    elif _UI_OUTCOMES["skipped"]:
        problem = f"UI_TESTS_REQUIRED=1 but {_UI_OUTCOMES['skipped']} UI test(s) skipped"
    if problem:
        session.config.pluginmanager.get_plugin("terminalreporter").write_line(
            f"\nFAIL: {problem}", red=True)
        session.exitstatus = 1
