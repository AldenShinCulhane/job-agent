"""Shared fixtures. The browser-level ones live in ui_harness.py; they are
registered here once so every UI test module sees the same session-scoped
Chromium and static server instead of each launching its own.

With UI_TESTS_REQUIRED=1 the browser tests are a gate: a skipped UI test,
or a run in which none executed, fails the session. Skips elsewhere in
the suite (the engine-dependent ones) are unaffected."""

import json
import os

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
    """What axe could not decide across the state matrix, once any browser
    test has run: grouped by rule and reason, with the states and sample
    selectors, and the whole list written to $AXE_REPORT as JSON when
    that is set. Not a gate: these are neither violations nor passes,
    they are what a reviewer looks at."""
    if not (_UI_OUTCOMES["passed"] + _UI_OUTCOMES["failed"]):
        return
    groups: dict[tuple[str, str], dict] = {}
    for where, items in AXE_INCOMPLETE:
        for item in items:
            for node in item["nodes"]:
                g = groups.setdefault((item["id"], node["reason"]),
                                      {"nodes": 0, "states": set(), "samples": []})
                g["nodes"] += 1
                g["states"].add(where)
                if len(g["samples"]) < 3:
                    g["samples"].append(f"{where} {node['target']}")
    terminalreporter.section("axe needs-review")
    if not groups:
        terminalreporter.line("none: axe decided every check it ran")
    for (rule, reason), g in sorted(groups.items()):
        terminalreporter.line(
            f"{rule}: {g['nodes']} node(s) in {len(g['states'])} state(s): {reason}")
        for sample in g["samples"]:
            terminalreporter.line(f"    e.g. {sample}")
    report = os.environ.get("AXE_REPORT")
    if report:
        with open(report, "w") as f:
            json.dump([{"state": where, "rule": item["id"], **node}
                       for where, items in AXE_INCOMPLETE
                       for item in items for node in item["nodes"]], f, indent=1)
        terminalreporter.line(f"full list: {report}")


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
