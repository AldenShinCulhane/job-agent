"""Shared fixtures. The browser-level ones live in ui_harness.py; they are
registered here once so every UI test module sees the same session-scoped
Chromium and static server instead of each launching its own."""

from backend.tests.ui_harness import browser, dist_url  # noqa: F401
