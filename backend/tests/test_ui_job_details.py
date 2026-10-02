"""What the Search page says about a job and its packages, in the built
app over fixtures: the posting in its paragraphs and lines, and as text
only; the requirement checklist; each job's newest package on its row;
and a failed package that is waiting on the user first, quoting the
claim that stopped it.
"""

import re
from pathlib import Path

from backend.tests.ui_harness import API_FIXTURES, _job, _package, route_fixtures
from backend.tests.ui_states import CLAIM, DETAILS, REASON

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _open(browser, dist_url, fixtures, *, ready="text=Backend Engineer a"):
    context = browser.new_context(color_scheme="light",
                                  viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: errors.append(f"dialog: {d.message}"))
    table = route_fixtures(page)
    table.update(fixtures)
    page.goto(dist_url + "/?tab=search")
    page.wait_for_selector(ready)
    return context, page, errors


def _details(page, title="Backend Engineer a"):
    row = page.locator("li", has=page.get_by_role("link", name=title, exact=True))
    row.get_by_role("button", name="Details").click()
    return row


def test_a_posting_reads_in_its_paragraphs_and_lines(browser, dist_url):
    context, page, errors = _open(browser, dist_url, DETAILS)
    row = _details(page)
    posting = row.locator("p.whitespace-pre-line")
    posting.first.wait_for()
    assert posting.all_inner_texts() == [
        "Example Widgets Inc. is hiring a backend engineer.",
        "Build APIs with Python.\nOwn services end to end.",
        "We ship every week.",
    ]
    assert errors == []
    context.close()


HOSTILE = ('<img src="x" onerror="window.__ran = 1"> <script>window.__ran = 2</script>'
           '\n\n<b>Bold?</b> <a href="javascript:window.__ran=3">link</a>')


def test_a_posting_is_shown_as_text_never_as_markup(browser, dist_url):
    """A posting is someone else's words. Markup in it — which LinkedIn's
    own page shows as text — stays text here: no element is made from
    it, and nothing in it runs."""
    jobs = dict(API_FIXTURES["/api/jobs"], jobs=[_job("a", 80, description=HOSTILE)])
    context, page, errors = _open(browser, dist_url, {"/api/jobs": jobs})
    row = _details(page)
    row.get_by_text("Bold?", exact=False).wait_for()
    texts = row.locator("p.whitespace-pre-line").all_inner_texts()
    assert texts == [('<img src="x" onerror="window.__ran = 1"> '
                      '<script>window.__ran = 2</script>'),
                     '<b>Bold?</b> <a href="javascript:window.__ran=3">link</a>']
    made = row.evaluate(
        "li => li.querySelectorAll('.whitespace-pre-line img, .whitespace-pre-line script,"
        " .whitespace-pre-line b, .whitespace-pre-line a').length")
    assert made == 0
    assert page.evaluate("() => window.__ran") is None
    assert errors == []
    context.close()


def test_no_source_file_writes_markup_from_a_string():
    """Every page renders text through React, which escapes it. No file
    sets HTML from a string — the one way a posting could become markup."""
    offenders = [
        str(path.relative_to(FRONTEND_SRC))
        for path in FRONTEND_SRC.rglob("*.ts*")
        if re.search(r"dangerouslySetInnerHTML|\.innerHTML\s*=|outerHTML\s*=|"
                     r"insertAdjacentHTML|document\.write", path.read_text())
    ]
    assert offenders == []


def _checklist(row):
    return row.locator("h3", has_text="Requirements").locator("xpath=..")


def test_the_checklist_says_what_the_profile_has(browser, dist_url):
    context, page, errors = _open(browser, dist_url, DETAILS)
    checklist = _checklist(_details(page))
    checklist.wait_for()
    groups = checklist.locator("h4").all_inner_texts()
    assert groups == ["Required", "Nice to have", "Experience and education"]
    # What a screen reader hears: the verdict in words, not a mark.
    items = [" ".join(t.split()) for t in checklist.locator("li").evaluate_all(
        "lis => lis.map(li => [...li.childNodes].filter(n => !(n.getAttribute &&"
        " n.getAttribute('aria-hidden'))).map(n => n.textContent).join(' '))")]
    assert items == ["Has: Python", "Missing: Go", "Missing: Kubernetes", "Has: Docker",
                     "Unknown: 3+ years of experience", "Has: Bachelor's degree"]
    # A missing requirement stands out; a missing nice-to-have does not.
    tones = checklist.locator("li").evaluate_all("lis => lis.map(li => li.className)")
    assert tones == ["text-ok-fg", "text-warn-fg", "text-ink-2", "text-ok-fg",
                     "text-ink-2", "text-ok-fg"]
    assert errors == []
    context.close()


def test_a_job_without_requirements_shows_no_checklist(browser, dist_url):
    context, page, errors = _open(browser, dist_url, DETAILS)
    row = _details(page, "Backend Engineer b")
    row.get_by_text("Build APIs.").wait_for()
    assert row.locator("h3", has_text="Requirements").count() == 0
    assert errors == []
    context.close()


def _row_badges(page, title):
    row = page.locator("li", has=page.get_by_role("link", name=title, exact=True))
    return row.locator("span.rounded-full").all_inner_texts()


def test_each_job_row_shows_its_newest_package(browser, dist_url):
    packages = {"page_limit": 1, "packages": [
        _package(9, "done", job="a", title="Backend Engineer a"),
        _package(8, "failed", "compile failed", job="a", title="Backend Engineer a"),
        _package(7, "waiting_usage", job="b", title="Backend Engineer b"),
        _package(6, "validating", job="c", title="Backend Engineer c"),
    ]}
    context, page, errors = _open(browser, dist_url, {"/api/packages": packages})
    assert "Package ready" in _row_badges(page, "Backend Engineer a")
    assert "Package failed" not in _row_badges(page, "Backend Engineer a")
    assert "Package waiting" in _row_badges(page, "Backend Engineer b")
    assert "Package in progress" in _row_badges(page, "Backend Engineer c")
    assert not [b for b in _row_badges(page, "Backend Engineer d") if "Package" in b]
    assert errors == []
    context.close()


def _rail_titles(page):
    rail = page.get_by_role("region", name="Package list")
    return [t.split(" — ")[0] for t in rail.locator("li > p.font-medium").all_inner_texts()]


def test_a_failure_waiting_on_the_user_comes_first(browser, dist_url):
    """Newest first, except a failed package that is its job's newest —
    the one Try again is on. A failure a later attempt answered keeps its
    place in the history."""
    packages = {"page_limit": 1, "packages": [
        _package(9, "done", job="x", title="Role X"),
        _package(8, "generating", job="z", title="Role Z"),
        _package(7, "failed", "compile failed", job="y", title="Role Y"),
        _package(6, "failed", "compile failed", job="x", title="Role X old"),
    ]}
    context, page, errors = _open(browser, dist_url, {"/api/packages": packages},
                                  ready="text=Role Y")
    assert _rail_titles(page) == ["Role Y", "Role X", "Role Z", "Role X old"]
    assert errors == []
    context.close()


def test_a_truth_check_failure_quotes_the_claim(browser, dist_url):
    context, page, errors = _open(browser, dist_url, DETAILS,
                                  ready="text=Stopped at the truth check")
    rail = page.get_by_role("region", name="Package list")
    entry = rail.locator("li", has_text="Stopped at the truth check")
    assert _rail_titles(page)[0] == "Backend Engineer b"
    # A claim and the section order: not two claims, two issues.
    assert "Stopped at the truth check: 2 validation issues" in entry.inner_text()
    assert "claims your profile" not in entry.inner_text()
    assert entry.locator("figcaption").all_inner_texts() == ["Cover letter", "Resume"]
    quote = entry.locator("blockquote")
    assert quote.count() == 1  # the section-order failure is not a sentence
    assert quote.inner_text() == f"“{CLAIM}”"
    # The raw validator message is not what the row shows.
    assert "truth validation failed" not in entry.inner_text()
    # Folded to three lines until the reason is opened, then in full.
    line = float(quote.evaluate("q => parseFloat(getComputedStyle(q).lineHeight)"))
    folded = quote.bounding_box()["height"]
    assert folded <= 3 * line + 1, (folded, line)
    assert not entry.get_by_text(REASON).is_visible()
    entry.get_by_text("Why", exact=True).first.click()
    entry.get_by_text(REASON).wait_for()
    assert quote.bounding_box()["height"] > folded
    assert errors == []
    context.close()


def test_other_failures_still_say_what_happened(browser, dist_url):
    packages = {"page_limit": 1, "packages": [
        _package(3, "failed", "CompileError: pdflatex exited 1", title="Role C")]}
    context, page, errors = _open(browser, dist_url, {"/api/packages": packages},
                                  ready="text=Role C")
    rail = page.get_by_role("region", name="Package list")
    assert rail.get_by_text("CompileError: pdflatex exited 1").is_visible()
    assert rail.get_by_text("Stopped at the truth check").count() == 0
    assert errors == []
    context.close()
