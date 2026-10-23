"""The application tracker (PLAN.md P8b) in the built app over fixtures:
Mark applied on a finished package, a status and day saved from its row,
Clear status, the role's status on the job list, and Hide applied. And
what the page does around them: nothing offered before the tracker is
read, a form that follows the status until the user changes it, edits
that hold the page, and focus that follows the package's tracker."""

import copy
import json

from backend.tests.test_ui_engine import WAITING
from backend.tests.ui_harness import (
    API_FIXTURES, Response, _package, engines_view, route_fixtures,
)
from backend.tests.ui_states import GROUPS, TRACKER

NONE_YET = dict(TRACKER, **{"/api/applications": {"applications": []}})


def _open(browser, dist_url, fixtures, *, hold=False, fail=None, lost=False):
    """The Search tab, with the tracker's POST answered the way the server
    would: the event appended to what the tracker lists now, the latest
    per posting listed unless it is a clear. `hold` keeps each answer
    until released; `fail` answers (status, detail) instead; `lost` takes
    the event and loses the answer on the way back."""
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    table = route_fixtures(page)
    table.update(copy.deepcopy(fixtures))
    bodies: list[dict] = []
    held: list = []

    def answer(route):
        body = json.loads(route.request.post_data)
        if fail is not None:
            route.fulfill(status=fail[0], content_type="application/json",
                          body=json.dumps({"detail": fail[1]}))
            return
        listed = table["/api/applications"]["applications"]
        event = dict(body, event_id=max((a["event_id"] for a in listed), default=0) + 1)
        latest = {a["job_id"]: a for a in listed} | {body["job_id"]: event}
        listed = [{k: e[k] for k in ("event_id", "job_id", "package_id", "status",
                                     "occurred_on")}
                  for e in latest.values() if e["status"] != "cleared"]
        table["/api/applications"] = {"applications": listed}
        if lost:
            route.abort()
            return
        mine = next((a for a in listed if a["job_id"] == body["job_id"]), None)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"created": True, "application": mine}))

    def handler(route, *_):
        if route.request.method != "POST":
            route.fallback()
            return
        bodies.append(json.loads(route.request.post_data))
        if hold:
            held.append(route)
        else:
            answer(route)

    page.route("**/api/applications", handler)
    page.goto(dist_url + "/?tab=search")
    page.wait_for_selector("text=Platform Engineer")
    return context, page, errors, table, bodies, lambda: answer(held.pop(0))


def _reread(page):
    """The page reads everything again, as it does when its tab comes
    back into view."""
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }""")


def _elsewhere(table, job_id, status, occurred_on=None):
    """Another tab records an event on `job_id`; None for a clear."""
    listed = [a for a in table["/api/applications"]["applications"] if a["job_id"] != job_id]
    if status is not None:
        listed.append({"event_id": max((a["event_id"] for a in
                                        table["/api/applications"]["applications"]),
                                       default=0) + 1,
                       "job_id": job_id, "package_id": None, "status": status,
                       "occurred_on": occurred_on})
    table["/api/applications"] = {"applications": listed}


def _today(page):
    """Today and its label, on the page's own calendar."""
    return page.evaluate("""() => {
        const now = new Date(), pad = (n) => String(n).padStart(2, '0')
        return [`${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
                now.toLocaleDateString([], { month: 'short', day: 'numeric' })]
    }""")


def _focused(page):
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return page.evaluate("() => document.activeElement.getAttribute('aria-label')")


def _package_row(page, pid):
    return page.locator(f"li:has(a[href$='/packages/{pid}/file/job.md'])")


def _job_row(page, title):
    jobs = page.locator("section", has=page.get_by_role("heading", name="Jobs ("))
    return jobs.locator("ul.divide-y > li").filter(
        has=page.get_by_role("link", name=title, exact=True))


def _update(page, title):
    page.locator(f"summary[aria-label='Update status for {title}']").click()


def _toggle(summary):
    """Opens or closes the summary's fold from the keyboard, and waits for
    the browser's toggle event, which is what the page keeps. It comes up
    to a frame later, and two toggles within one frame are one event."""
    summary.evaluate("""el => { window.toggled = new Promise((r) =>
        el.parentElement.addEventListener('toggle', r, { once: true })) }""")
    summary.press("Enter")
    summary.page.evaluate("() => window.toggled")


def test_mark_applied_records_today_from_that_package(browser, dist_url):
    context, page, errors, _, bodies, _ = _open(browser, dist_url, NONE_YET)
    day, label = _today(page)
    page.get_by_role("button", name="Mark applied for Backend Engineer d").click()
    row = _package_row(page, 9)
    row.get_by_text(f"Applied {label}").wait_for()
    assert bodies == [{"job_id": "d", "status": "applied", "occurred_on": day,
                       "package_id": 9}]
    # Mark applied went with the answer: the focus is on what replaced it.
    assert _focused(page) == "Update status for Backend Engineer d"
    assert _job_row(page, "Backend Engineer d").get_by_text(f"Applied {label}").is_visible()
    assert errors == []
    context.close()


def test_a_status_saved_with_another_day_keeps_the_focus(browser, dist_url):
    context, page, errors, _, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    page.get_by_label("Status for Backend Engineer a", exact=True).select_option("offer")
    page.get_by_label("Date for Backend Engineer a", exact=True).fill("2026-09-30")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _package_row(page, 8).get_by_text("Offer Sep 30").wait_for()
    assert bodies == [{"job_id": "a", "status": "offer", "occurred_on": "2026-09-30",
                       "package_id": 8}]
    assert _focused(page) == "Save status for Backend Engineer a"
    assert _job_row(page, "Backend Engineer a").get_by_text("Offer Sep 30").is_visible()
    assert errors == []
    context.close()


def test_clear_status_brings_mark_applied_back(browser, dist_url):
    context, page, errors, _, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    page.get_by_label("Status for Backend Engineer a", exact=True).select_option("cleared")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    page.get_by_role("button", name="Mark applied for Backend Engineer a").wait_for()
    assert [b["status"] for b in bodies] == ["cleared"]
    assert _focused(page) == "Mark applied for Backend Engineer a"
    assert _job_row(page, "Backend Engineer a").get_by_text("Interviewing").count() == 0
    assert errors == []
    context.close()


def test_a_role_shows_the_latest_status_of_any_of_its_listings(browser, dist_url):
    """The repeated role was applied to through its earlier listing: the
    row says so, and whose it is. A later status on another listing is
    the one it shows next."""
    context, page, errors, table, _, _ = _open(browser, dist_url, TRACKER)
    role = _job_row(page, "Platform Engineer")
    assert role.get_by_text("Applied Sep 20").is_visible()
    assert role.get_by_text("for the listing first seen Aug 1").count() == 2  # package, status
    table["/api/applications"]["applications"].append(
        {"event_id": 3, "job_id": "b", "package_id": None, "status": "withdrawn",
         "occurred_on": "2026-10-04"})
    role.get_by_text("Withdrawn Oct 4").wait_for()
    assert role.get_by_text("for the listing first seen Sep 14").is_visible()
    assert role.get_by_text("Applied Sep 20").count() == 0
    assert errors == []
    context.close()


def test_hide_applied_hides_a_role_applied_to_through_any_listing(browser, dist_url):
    context, page, errors, _, bodies, _ = _open(browser, dist_url, TRACKER)
    page.get_by_role("checkbox", name="Select Platform Engineer").check()
    page.get_by_label("Hide applied").check()
    heading = page.get_by_role("heading", name="Jobs (")
    assert heading.inner_text() == "Jobs (1 of 3)"
    assert _job_row(page, "Platform Engineer").count() == 0
    assert _job_row(page, "Backend Engineer a").count() == 0
    # A hidden role is not generated for: nothing selected is on screen.
    assert page.get_by_role("button", name="Generate packages").count() == 0
    page.get_by_label("Hide applied").uncheck()
    assert heading.inner_text() == "Jobs (3)"
    assert page.get_by_role("button", name="Generate packages").inner_text() == \
        "Generate packages (1)"
    assert errors == []
    context.close()


def test_a_failed_save_is_said_beside_its_row_and_keeps_the_focus(browser, dist_url):
    context, page, errors, _, bodies, _ = _open(
        browser, dist_url, NONE_YET, fail=(422, "Package 9 is not one of this posting's."))
    page.get_by_role("button", name="Mark applied for Backend Engineer d").click()
    row = _package_row(page, 9)
    row.get_by_text("Package 9 is not one of this posting's.").wait_for()
    assert _focused(page) == "Mark applied for Backend Engineer d"
    assert len(bodies) == 1
    assert errors == []
    context.close()


def test_a_click_sent_twice_is_one_event(browser, dist_url):
    """Two clicks inside one task: the second lands before the button can
    render busy, and must still not send a second event."""
    context, page, errors, _, bodies, release = _open(browser, dist_url, NONE_YET, hold=True)
    page.get_by_role("button", name="Mark applied for Backend Engineer d").evaluate(
        "b => { b.click(); b.click() }")
    page.wait_for_selector("button[aria-label='Mark applied for Backend Engineer d']"
                           "[aria-disabled='true']")
    assert len(bodies) == 1
    release()
    page.get_by_text("Update status").first.wait_for()
    page.wait_for_timeout(300)
    assert len(bodies) == 1
    assert errors == []
    context.close()


def test_without_a_finished_package_there_is_nothing_to_mark(browser, dist_url):
    """Mark applied is on finished packages only."""
    fixtures = dict(NONE_YET, **{"/api/packages": API_FIXTURES["/api/packages"],
                                 "/api/jobs": GROUPS["/api/jobs"]})
    context, page, errors, _, _, _ = _open(browser, dist_url, fixtures)
    marks = page.locator("button[aria-label^='Mark applied for']")
    assert marks.count() == 1
    assert marks.get_attribute("aria-label") == "Mark applied for Role 1"  # the done one
    assert errors == []
    context.close()


def _form(page, pid, title):
    """A package row's status form: its status and its day."""
    row = _package_row(page, pid)
    return (row.get_by_label(f"Status for {title}", exact=True),
            row.get_by_label(f"Date for {title}", exact=True))


def _values(form):
    return tuple(field.input_value() for field in form)


def _until(page, done):
    """Waits for something only Python can see, such as a request sent."""
    for _ in range(100):
        if done():
            return
        page.wait_for_timeout(50)
    raise AssertionError("never happened")


def _is_focused(locator) -> bool:
    locator.page.evaluate(
        "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return locator.evaluate("el => el === document.activeElement")


# Finished packages pushed out of the five most recent by four newer ones.
NEWER = [_package(pid, "done", job=f"n{pid}") for pid in (14, 13, 12, 11)]


def test_nothing_is_offered_before_the_tracker_is_read(browser, dist_url):
    """Until the tracker has been read, nothing is known about any
    posting: no Mark applied that could overwrite a status the page
    hasn't seen, no Update status, and nothing for Hide applied to judge.
    The read is owed, and once it lands the tracker is offered."""
    failing = dict(TRACKER, **{"/api/applications": Response(503, {"detail": "db locked"})})
    context, page, errors, table, bodies, _ = _open(browser, dist_url, failing)
    page.wait_for_selector("text=db locked")
    rows = page.get_by_role("region", name="Package list")
    assert rows.get_by_text("Mark applied").count() == 0
    assert rows.get_by_text("Update status").count() == 0
    assert page.get_by_label("Hide applied").is_disabled()
    assert _job_row(page, "Platform Engineer").get_by_text("Applied").count() == 0

    table["/api/applications"] = copy.deepcopy(TRACKER["/api/applications"])
    page.locator("summary[aria-label='Update status for Backend Engineer a']").wait_for(
        timeout=15_000)
    assert page.get_by_role("button", name="Mark applied for Backend Engineer d").is_visible()
    assert page.get_by_label("Hide applied").is_enabled()
    assert bodies == []
    assert errors == []
    context.close()


def test_an_untouched_form_follows_a_status_read_since(browser, dist_url):
    """The form opened on Interviewing, and another tab then recorded an
    offer. The form says so: Save sends the offer, and cannot put
    Interviewing back over it."""
    context, page, errors, table, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    form = _form(page, 8, "Backend Engineer a")
    assert _values(form) == ("interviewing", "2026-10-02")
    _elsewhere(table, "a", "offer", "2026-10-01")
    _reread(page)
    _package_row(page, 8).get_by_text("Offer Oct 1").wait_for()
    assert _values(form) == ("offer", "2026-10-01")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    assert bodies == [{"job_id": "a", "status": "offer", "occurred_on": "2026-10-01",
                       "package_id": 8}]
    assert errors == []
    context.close()


def test_a_status_the_user_changes_is_dated_today_unless_they_say(browser, dist_url):
    context, page, errors, _, _, _ = _open(browser, dist_url, TRACKER)
    day, _ = _today(page)
    _update(page, "Backend Engineer a")
    status, date = form = _form(page, 8, "Backend Engineer a")
    status.select_option("offer")
    assert _values(form) == ("offer", day)
    status.select_option("interviewing")  # back to the status it has, and its day
    assert _values(form) == ("interviewing", "2026-10-02")
    date.fill("2026-09-28")
    status.select_option("rejected")  # a day the user gave stays theirs
    assert _values(form) == ("rejected", "2026-09-28")
    assert errors == []
    context.close()


def test_a_form_changed_back_follows_the_status_again(browser, dist_url):
    """Changed and changed back is no edit: the form follows the status
    read since, and holds nothing."""
    context, page, errors, table, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    status, date = form = _form(page, 8, "Backend Engineer a")
    status.select_option("offer")
    status.select_option("interviewing")
    date.fill("2026-09-28")
    date.fill("2026-10-02")
    _elsewhere(table, "a", "rejected", "2026-10-04")
    _reread(page)
    _package_row(page, 8).get_by_text("Rejected Oct 4").wait_for()
    assert _values(form) == ("rejected", "2026-10-04")
    table["/api/engines"] = engines_view(claude={**WAITING, "episode": "ep-clean"})
    _reread(page)
    page.get_by_role("heading", name="AI engine").wait_for()  # nothing held the page
    assert bodies == []
    assert errors == []
    context.close()


def test_an_edited_form_keeps_its_edits_through_a_read(browser, dist_url):
    """Two finished packages of one posting, each with its form. The one
    the user changed keeps both fields through a status read since; the
    other follows it. Each saves what it shows."""
    two = dict(TRACKER, **{"/api/packages": {"page_limit": 1, "packages": [
        _package(10, "done", job="a", title="Backend Engineer a", attempt=2, retry_of=8),
        *TRACKER["/api/packages"]["packages"]]}})
    context, page, errors, table, bodies, _ = _open(browser, dist_url, two)
    for pid in (8, 10):
        _package_row(page, pid).locator("summary[aria-label^='Update status']").click()
    mine, other = _form(page, 8, "Backend Engineer a"), _form(page, 10, "Backend Engineer a")
    mine[0].select_option("withdrawn")
    mine[1].fill("2026-10-03")
    _elsewhere(table, "a", "offer", "2026-10-01")
    _reread(page)
    _package_row(page, 10).get_by_text("Offer Oct 1").wait_for()
    assert _values(other) == ("offer", "2026-10-01")
    assert _values(mine) == ("withdrawn", "2026-10-03")

    _package_row(page, 10).get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    page.wait_for_timeout(300)  # its answer and the read after it
    assert _values(mine) == ("withdrawn", "2026-10-03")  # its own read, not its reset
    _package_row(page, 8).get_by_role("button", name="Save status for Backend Engineer a").click()
    _package_row(page, 10).get_by_text("Withdrawn Oct 3").wait_for()
    assert bodies == [
        {"job_id": "a", "status": "offer", "occurred_on": "2026-10-01", "package_id": 10},
        {"job_id": "a", "status": "withdrawn", "occurred_on": "2026-10-03", "package_id": 8}]
    assert _values(mine) == _values(other) == ("withdrawn", "2026-10-03")
    assert errors == []
    context.close()


def test_a_saved_status_stays_on_the_form_until_it_is_read_back(browser, dist_url):
    """The save is answered before the tracker is read again: in between,
    the form shows what was saved, not the status from before."""
    context, page, errors, table, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    form = _form(page, 8, "Backend Engineer a")
    form[0].select_option("offer")
    form[1].fill("2026-10-05")
    reads: list = []

    def hold_reads(route, *_):
        if route.request.method == "GET":
            reads.append(route)
        else:
            route.fallback()
    page.route("**/api/applications", hold_reads)
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: reads)  # saved, and the read after it is out
    assert len(bodies) == 1
    page.wait_for_timeout(200)
    assert _values(form) == ("offer", "2026-10-05")
    assert _package_row(page, 8).get_by_text("Interviewing Oct 2").is_visible()
    page.unroute("**/api/applications", hold_reads)
    for route in reads:
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(table["/api/applications"]))
    _package_row(page, 8).get_by_text("Offer Oct 5").wait_for()
    assert _values(form) == ("offer", "2026-10-05")
    # Read back, the form is the posting's again: it follows what comes.
    _elsewhere(table, "a", "rejected", "2026-10-09")
    _reread(page)
    _package_row(page, 8).get_by_text("Rejected Oct 9").wait_for()
    assert _values(form) == ("rejected", "2026-10-09")
    assert errors == []
    context.close()


def test_an_edit_made_while_the_save_is_out_stays_the_users(browser, dist_url):
    context, page, errors, _, bodies, release = _open(browser, dist_url, TRACKER, hold=True)
    day, label = _today(page)
    _update(page, "Backend Engineer a")
    status, date = form = _form(page, 8, "Backend Engineer a")
    status.select_option("offer")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    date.fill("2026-10-08")
    release()
    _package_row(page, 8).get_by_text(f"Offer {label}").wait_for()
    page.wait_for_timeout(300)
    assert bodies == [{"job_id": "a", "status": "offer", "occurred_on": day, "package_id": 8}]
    assert _values(form) == ("offer", "2026-10-08")
    assert errors == []
    context.close()


def test_an_edit_outlives_its_status_cleared_elsewhere(browser, dist_url):
    """Another tab clears the status while the user is changing it: the
    form, and what they put in it, stay until they save or leave it."""
    context, page, errors, table, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    form = _form(page, 8, "Backend Engineer a")
    form[0].select_option("offer")
    form[1].fill("2026-10-06")
    _elsewhere(table, "a", None)
    _reread(page)
    page.wait_for_function("() => !document.body.innerText.includes('Interviewing Oct 2')")
    assert _values(form) == ("offer", "2026-10-06")
    assert page.get_by_role("button", name="Mark applied for Backend Engineer a").count() == 0
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _package_row(page, 8).get_by_text("Offer Oct 6").wait_for()
    assert bodies == [{"job_id": "a", "status": "offer", "occurred_on": "2026-10-06",
                       "package_id": 8}]
    assert errors == []
    context.close()


def test_an_unsaved_status_keeps_the_page_when_a_usage_limit_starts(browser, dist_url):
    """A usage limit opens the engine picker by itself, but never over
    edits the user would lose: a status and a day chosen, not saved. The
    banner is the way there instead."""
    context, page, errors, table, bodies, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    form = _form(page, 8, "Backend Engineer a")
    form[0].select_option("offer")
    form[1].fill("2026-09-30")
    table["/api/engines"] = engines_view(claude={**WAITING, "episode": "ep-tracker"})
    _reread(page)
    page.wait_for_selector("text=Claude usage limit reached")
    page.wait_for_timeout(300)
    assert page.get_by_role("heading", name="AI engine").count() == 0
    assert _values(form) == ("offer", "2026-09-30")
    assert bodies == []
    assert errors == []
    context.close()


def test_focus_moved_away_while_marking_stays_where_the_user_put_it(browser, dist_url):
    context, page, errors, _, bodies, release = _open(browser, dist_url, NONE_YET, hold=True)
    page.get_by_role("button", name="Mark applied for Backend Engineer d").click()
    page.wait_for_selector("button[aria-label='Mark applied for Backend Engineer d']"
                           "[aria-disabled='true']")
    hide = page.get_by_label("Hide applied")
    hide.focus()
    release()
    page.locator("summary[aria-label='Update status for Backend Engineer d']").wait_for()
    assert _is_focused(hide)
    assert errors == []
    context.close()


def test_a_status_that_arrives_by_a_read_takes_the_focus_along(browser, dist_url):
    """The event was taken but its answer lost: the read after the
    failure is what replaces Mark applied, and the focus goes to what
    replaced it."""
    context, page, errors, _, bodies, _ = _open(browser, dist_url, NONE_YET, lost=True)
    page.get_by_role("button", name="Mark applied for Backend Engineer d").click()
    page.locator("summary[aria-label='Update status for Backend Engineer d']").wait_for()
    assert _focused(page) == "Update status for Backend Engineer d"
    assert len(bodies) == 1
    assert errors == []
    context.close()


def test_a_status_cleared_elsewhere_hands_the_focus_to_mark_applied(browser, dist_url):
    context, page, errors, table, _, _ = _open(browser, dist_url, TRACKER)
    page.locator("summary[aria-label='Update status for Backend Engineer a']").focus()
    _elsewhere(table, "a", None)
    _reread(page)
    page.get_by_role("button", name="Mark applied for Backend Engineer a").wait_for()
    assert _focused(page) == "Mark applied for Backend Engineer a"
    assert errors == []
    context.close()


def test_a_form_pushed_into_earlier_packages_keeps_its_edit_and_the_focus(browser, dist_url):
    context, page, errors, table, _, _ = _open(browser, dist_url, TRACKER)
    _update(page, "Backend Engineer a")
    status, date = form = _form(page, 8, "Backend Engineer a")
    date.fill("2026-10-07")
    status.select_option("offer")
    status.focus()
    table["/api/packages"]["packages"][:0] = copy.deepcopy(NEWER)
    _reread(page)
    page.get_by_text("Earlier packages (2)").wait_for()
    assert _focused(page) == "Status for Backend Engineer a"
    assert _values(form) == ("offer", "2026-10-07")
    assert errors == []
    context.close()



def test_an_open_form_moved_between_the_package_lists_stays_open(browser, dist_url):
    """Newer packages push the row into Earlier packages, where it is
    built again, and taking them away brings it back. Each way the form
    stays open and the focus stays on its summary."""
    context, page, errors, table, _, _ = _open(browser, dist_url, TRACKER)
    _toggle(page.locator("summary[aria-label='Update status for Backend Engineer a']"))
    status, _ = _form(page, 8, "Backend Engineer a")
    status.wait_for()
    packages = table["/api/packages"]["packages"]
    packages[:0] = copy.deepcopy(NEWER)
    _reread(page)
    page.get_by_text("Earlier packages (2)").wait_for()
    assert _focused(page) == "Update status for Backend Engineer a"
    assert status.is_visible()
    del packages[:len(NEWER)]
    _reread(page)
    page.get_by_text("Earlier packages").wait_for(state="detached")
    assert _focused(page) == "Update status for Backend Engineer a"
    assert status.is_visible()
    assert errors == []
    context.close()


def test_a_form_closed_again_moves_closed(browser, dist_url):
    context, page, errors, table, _, _ = _open(browser, dist_url, TRACKER)
    summary = page.locator("summary[aria-label='Update status for Backend Engineer a']")
    status, _ = _form(page, 8, "Backend Engineer a")
    _toggle(summary)
    status.wait_for()
    _toggle(summary)
    status.wait_for(state="hidden")
    table["/api/packages"]["packages"][:0] = copy.deepcopy(NEWER)
    _reread(page)
    page.get_by_text("Earlier packages (2)").wait_for()
    assert _focused(page) == "Update status for Backend Engineer a"
    assert summary.is_visible()
    assert not status.is_visible()
    assert errors == []
    context.close()


def test_a_status_cleared_and_marked_again_starts_with_its_form_closed(browser, dist_url):
    """Clear status takes the form away and Mark applied takes its place.
    Marked again, the posting has a new form, and it starts closed: the
    old form being open went with the old form."""
    context, page, errors, _, bodies, _ = _open(browser, dist_url, TRACKER)
    _toggle(page.locator("summary[aria-label='Update status for Backend Engineer a']"))
    status, _ = _form(page, 8, "Backend Engineer a")
    status.select_option("cleared")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    page.get_by_role("button", name="Mark applied for Backend Engineer a").click()
    page.locator("summary[aria-label='Update status for Backend Engineer a']").wait_for()
    assert [b["status"] for b in bodies] == ["cleared", "applied"]
    assert not status.is_visible()
    assert errors == []
    context.close()


def test_a_form_cleared_elsewhere_stays_open_only_with_an_edit(browser, dist_url):
    """Another tab clears two postings with their forms open. The form
    the user is changing stays, open. The other goes, and when that tab
    marks its posting again, it comes back closed."""
    context, page, errors, table, _, _ = _open(browser, dist_url, TRACKER)
    _toggle(page.locator("summary[aria-label='Update status for Backend Engineer a']"))
    _toggle(page.locator("summary[aria-label='Update status for Platform Engineer']"))
    edited, _ = _form(page, 8, "Backend Engineer a")
    untouched, _ = _form(page, 5, "Platform Engineer")
    edited.select_option("offer")
    untouched.wait_for()
    _elsewhere(table, "a", None)
    _elsewhere(table, "old", None)
    _reread(page)
    page.get_by_role("button", name="Mark applied for Platform Engineer").wait_for()
    assert edited.is_visible()
    _elsewhere(table, "old", "applied", "2026-10-08")
    _reread(page)
    page.locator("summary[aria-label='Update status for Platform Engineer']").wait_for()
    assert not untouched.is_visible()
    assert edited.is_visible()
    assert errors == []
    context.close()

def _usage_limit(page, table, episode):
    """A usage limit starts. Unless something unsaved holds the page, it
    opens the engine picker by itself."""
    table["/api/engines"] = engines_view(claude={**WAITING, "episode": episode})
    _reread(page)


def test_a_status_changed_back_while_the_save_is_out_stays_the_users(browser, dist_url):
    """Offer was saved and, while its answer was out, the user put the
    form back to Interviewing. That is the newer edit: the save changes
    the status under it, and the form keeps what the user chose."""
    context, page, errors, _, bodies, release = _open(browser, dist_url, TRACKER, hold=True)
    day, label = _today(page)
    _update(page, "Backend Engineer a")
    status, _ = form = _form(page, 8, "Backend Engineer a")
    status.select_option("offer")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    status.select_option("interviewing")
    assert _values(form) == ("interviewing", "2026-10-02")
    release()
    _package_row(page, 8).get_by_text(f"Offer {label}").wait_for()
    page.wait_for_timeout(300)
    assert _values(form) == ("interviewing", "2026-10-02")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: len(bodies) == 2)
    release()
    _package_row(page, 8).get_by_text("Interviewing Oct 2").wait_for()
    assert bodies == [
        {"job_id": "a", "status": "offer", "occurred_on": day, "package_id": 8},
        {"job_id": "a", "status": "interviewing", "occurred_on": "2026-10-02", "package_id": 8}]
    assert errors == []
    context.close()


def test_a_day_changed_back_while_the_save_is_out_stays_the_users(browser, dist_url):
    """The same for a day. A read that lands while the save is out is not
    the save's: the change back made after it is still the newer edit."""
    context, page, errors, _, bodies, release = _open(browser, dist_url, TRACKER, hold=True)
    _update(page, "Backend Engineer a")
    _, date = form = _form(page, 8, "Backend Engineer a")
    date.fill("2026-10-08")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    _reread(page)
    page.wait_for_timeout(300)
    date.fill("2026-10-02")
    release()
    _package_row(page, 8).get_by_text("Interviewing Oct 8").wait_for()
    page.wait_for_timeout(300)
    assert _values(form) == ("interviewing", "2026-10-02")
    assert bodies == [{"job_id": "a", "status": "interviewing", "occurred_on": "2026-10-08",
                       "package_id": 8}]
    assert errors == []
    context.close()


def test_a_change_back_on_another_form_of_the_posting_stays_the_users(browser, dist_url):
    """Two finished packages of one posting. One saves an offer; while it
    is out, the user changes the other's form and puts it back to the
    status both opened on. The save changes that status under it too, so
    the change back is an edit, and stays."""
    two = dict(TRACKER, **{"/api/packages": {"page_limit": 1, "packages": [
        _package(10, "done", job="a", title="Backend Engineer a", attempt=2, retry_of=8),
        *TRACKER["/api/packages"]["packages"]]}})
    context, page, errors, _, bodies, release = _open(browser, dist_url, two, hold=True)
    day, label = _today(page)
    for pid in (8, 10):
        _package_row(page, pid).locator("summary[aria-label^='Update status']").click()
    saving, other = _form(page, 8, "Backend Engineer a"), _form(page, 10, "Backend Engineer a")
    saving[0].select_option("offer")
    _package_row(page, 8).get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    other[0].select_option("rejected")
    other[0].select_option("interviewing")
    release()
    _package_row(page, 10).get_by_text(f"Offer {label}").wait_for()
    page.wait_for_timeout(300)
    assert _values(saving) == ("offer", day)
    assert _values(other) == ("interviewing", "2026-10-02")
    assert errors == []
    context.close()


def test_an_edit_that_matches_the_save_is_no_edit_once_read_back(browser, dist_url):
    """While the save was out the user changed the day and put it back to
    the one being saved. Read back, the form shows just what the tracker
    says: it holds nothing, and follows what comes next."""
    context, page, errors, table, bodies, release = _open(browser, dist_url, TRACKER, hold=True)
    day, label = _today(page)
    _update(page, "Backend Engineer a")
    status, date = form = _form(page, 8, "Backend Engineer a")
    status.select_option("offer")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    date.fill("2026-10-08")
    date.fill(day)
    release()
    _package_row(page, 8).get_by_text(f"Offer {label}").wait_for()
    _elsewhere(table, "a", "rejected", "2026-10-09")
    _reread(page)
    _package_row(page, 8).get_by_text("Rejected Oct 9").wait_for()
    assert _values(form) == ("rejected", "2026-10-09")
    _usage_limit(page, table, "ep-settled")
    page.get_by_role("heading", name="AI engine").wait_for()  # nothing held the page
    assert len(bodies) == 1
    assert errors == []
    context.close()


def test_a_failed_save_settles_once_the_tracker_is_read(browser, dist_url):
    """The save failed, and the user had put the form back meanwhile. The
    read after the failure says nothing changed: the form is the posting's
    again, and holds nothing."""
    context, page, errors, table, bodies, release = _open(
        browser, dist_url, TRACKER, hold=True, fail=(503, "db locked"))
    _update(page, "Backend Engineer a")
    status, _ = form = _form(page, 8, "Backend Engineer a")
    status.select_option("offer")
    page.get_by_role("button", name="Save status for Backend Engineer a").click()
    _until(page, lambda: bodies)
    status.select_option("interviewing")
    release()
    _package_row(page, 8).get_by_text("db locked").wait_for()
    page.wait_for_timeout(300)
    assert _values(form) == ("interviewing", "2026-10-02")
    _usage_limit(page, table, "ep-failed")
    page.get_by_role("heading", name="AI engine").wait_for()  # nothing held the page
    assert len(bodies) == 1
    assert errors == []
    context.close()
