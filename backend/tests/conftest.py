"""Shared fixtures. The browser-level ones live in ui_harness.py; they are
registered here once so every UI test module sees the same session-scoped
Chromium and static server instead of each launching its own.

With UI_TESTS_REQUIRED=1 the browser tests are a gate: a skipped UI test,
or a run in which none executed, fails the session. Skips elsewhere in
the suite (the engine-dependent ones) are unaffected."""

import pytest

from backend.tests.ui_harness import (  # noqa: F401
    AXE_INCOMPLETE, UI_REQUIRED, browser, dist_url, events,
)

_UI_OUTCOMES: dict[str, int] = {"passed": 0, "failed": 0, "skipped": 0}


def pytest_runtest_logreport(report):
    if "/test_ui_" not in report.nodeid and not report.nodeid.startswith("backend/tests/test_ui_"):
        return
    if report.when == "call" or (report.when == "setup" and report.outcome == "skipped"):
        _UI_OUTCOMES[report.outcome] = _UI_OUTCOMES.get(report.outcome, 0) + 1


def pytest_terminal_summary(terminalreporter):
    """What axe could not decide across the state matrix, by rule, once any
    browser test has run. Not a gate: these are the items a reviewer looks
    at, and this is where they can be looked at."""
    if not (_UI_OUTCOMES["passed"] + _UI_OUTCOMES["failed"]):
        return
    by_rule: dict[str, dict] = {}
    for where, items in AXE_INCOMPLETE:
        for item in items:
            entry = by_rule.setdefault(item["id"], {"nodes": 0, "states": []})
            entry["nodes"] += item["nodes"]
            entry["states"].append(where)
    terminalreporter.section("axe needs-review")
    if not by_rule:
        terminalreporter.line("none: axe decided every check it ran")
    for rule, entry in sorted(by_rule.items()):
        terminalreporter.line(
            f"{rule}: {entry['nodes']} node(s) across {len(entry['states'])} state(s), "
            f"first {entry['states'][0]}")


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
