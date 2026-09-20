"""The UI fixture tables are per page. A test changes a page's answers in
place to drive a transition, and nothing it changes may reach the module
tables, another page, or the dict it passed in — or a later test would
start from the wrong state and only pass in one order."""

import copy

from backend.tests.ui_harness import API_FIXTURES, _package, route_fixtures


class _Page:
    def route(self, pattern, handler):
        pass


def test_a_page_changing_its_answers_changes_nothing_shared():
    before = copy.deepcopy(API_FIXTURES)
    one = route_fixtures(_Page())
    two = route_fixtures(_Page())
    one["/api/packages"]["packages"][1]["status"] = "done"
    assert two["/api/packages"]["packages"][1]["status"] == "generating"
    assert API_FIXTURES == before


def test_supplied_overrides_are_copied_too():
    given = {**API_FIXTURES,
             "/api/packages": {"page_limit": 1, "packages": [_package(1, "generating")]}}
    table = route_fixtures(_Page(), given)
    table["/api/packages"]["packages"][0]["status"] = "done"
    assert given["/api/packages"]["packages"][0]["status"] == "generating"
