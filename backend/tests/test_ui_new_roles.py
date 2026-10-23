"""New and reposted on the job list (PLAN.md P8d), in the built app over
fixtures: a chip on the row, how many of the search's roles are new,
and a filter that keeps only those. All of it is the list's own answer:
the page works none of it out from earlier searches itself."""

import copy
import json

from backend.tests.ui_harness import route_fixtures
from backend.tests.ui_states import NEW_ROLES


def _open(browser, dist_url, fixtures=NEW_ROLES):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table.update(copy.deepcopy(fixtures))
    page.goto(dist_url + "/?tab=search")
    page.wait_for_selector("text=Backend Engineer a")
    return context, page, errors, table


def _row(page, title):
    return page.locator("ul li", has=page.get_by_role("link", name=title, exact=True))


def _chips(page, title):
    row = _row(page, title).first
    return [name for name in ("New", "Reposted")
            if row.get_by_text(name, exact=True).count() > 0]


def _rows(page):
    return page.locator("ul li a[data-control='link']").evaluate_all(
        "els => els.filter(e => !e.closest('details')).map(e => e.textContent)")


def _reread(page):
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }""")


def test_each_row_says_whether_its_role_is_new_or_reposted(browser, dist_url):
    context, page, errors, _ = _open(browser, dist_url)
    assert _chips(page, "Backend Engineer a") == ["New"]
    # The role listed twice in this search is one row, a repost of the
    # listing an earlier search found.
    assert _chips(page, "Platform Engineer") == ["Reposted"]
    assert _chips(page, "Backend Engineer d") == []
    assert _chips(page, "Backend Engineer e") == ["New"]
    # Counted as roles, beside the roles and the listings.
    assert page.get_by_role("heading", name="Jobs (4)").is_visible()
    assert page.get_by_text("5 listings").is_visible()
    assert page.get_by_text("2 new roles").is_visible()
    assert errors == []
    context.close()


def test_new_only_keeps_the_new_roles(browser, dist_url):
    context, page, errors, _ = _open(browser, dist_url)
    new_only = page.get_by_label("New only")
    new_only.check()
    assert _rows(page) == ["Backend Engineer a", "Backend Engineer e"]
    assert page.get_by_role("heading", name="Jobs (2 of 4)").is_visible()
    new_only.uncheck()
    assert _rows(page) == ["Backend Engineer a", "Platform Engineer", "Backend Engineer d",
                           "Backend Engineer e"]
    assert errors == []
    context.close()


def test_a_selection_new_only_hides_is_not_submitted(browser, dist_url):
    context, page, errors, _ = _open(browser, dist_url)
    bodies: list = []

    def generate(route):
        bodies.append(json.loads(route.request.post_data))
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"package_ids": [9], "created": 1}))
    page.route("**/api/packages/generate", generate)
    page.get_by_label("Select Backend Engineer a").check()
    page.get_by_label("Select Backend Engineer d").check()
    page.get_by_label("New only").check()
    button = page.get_by_role("button", name="Generate packages (1)")
    button.click()
    page.wait_for_function("() => !document.body.innerText.includes('Generate packages')")
    assert bodies == [{"job_ids": ["a"], "run_id": 7}]
    assert errors == []
    context.close()


def test_the_count_says_one_role_and_nothing_when_none(browser, dist_url):
    one = copy.deepcopy(NEW_ROLES)
    one["/api/jobs"]["jobs"][4]["novelty"] = None
    context, page, errors, table = _open(browser, dist_url, one)
    assert page.get_by_text("1 new role", exact=True).is_visible()
    # A read that finds nothing new says nothing, not "0 new roles".
    table["/api/jobs"]["jobs"][0]["novelty"] = None
    _reread(page)
    page.get_by_text("1 new role", exact=True).wait_for(state="detached")
    assert page.get_by_text("new role").count() == 0
    assert _chips(page, "Backend Engineer a") == []
    assert errors == []
    context.close()


def test_the_rows_follow_the_list_read_again(browser, dist_url):
    """A saved description can show a posting repeats a role an earlier
    search listed: the list read again says so, and the row follows."""
    context, page, errors, table = _open(browser, dist_url)
    table["/api/jobs"]["jobs"][4]["novelty"] = "reposted"
    _reread(page)
    _row(page, "Backend Engineer e").first.get_by_text("Reposted", exact=True).wait_for()
    assert _chips(page, "Backend Engineer e") == ["Reposted"]
    assert page.get_by_text("1 new role", exact=True).is_visible()
    assert errors == []
    context.close()
