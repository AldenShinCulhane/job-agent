"""Browser check (PLAN.md M5, 'invalid upload' recovery): a failed resume
upload must be retriable with the SAME file from the same input element.

Runs the real built UI in Chromium against a running app, with every
/api/* response mocked (sanitized sample data only — no real profile or
LinkedIn data is fetched or rendered). The first upload attempt is
answered 503 (usage limit), the second 200.

Two things are asserted, and only together do they prove the retry path:

1. After the failed attempt the file input's value is EMPTY. This is the
   DOM precondition browsers require: an input whose value still holds
   the previous path fires no `change` event when the user picks that
   same file again, so the retry would silently do nothing.
2. A second selection of the same file reaches the API and the app
   advances to the profile view.

Scope note: Playwright's set_input_files always dispatches `change`, even
when the same file is re-selected, so assertion 2 alone cannot
distinguish a fixed input from a broken one — assertion 1 is what covers
the same-file case.

Usage: uv run python scripts/ci/upload_retry_browser_check.py [base_url]
"""

import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8700").rstrip("/")

SAMPLE_PROFILE = {
    "name": "Jordan Sample",
    "headline": "Software Engineer",
    "contact": {"phone": "555-010-2030", "email": "jordan@example.com",
                "linkedin": "linkedin.com/in/jordansample", "github": None,
                "website": None, "location": "Toronto, ON"},
    "experiences": [{"title": "Software Engineer", "company": "Acme Robotics",
                     "location": "Toronto, ON", "start": "May 2024",
                     "end": "Present",
                     "bullets": ["Built event-driven ingestion for 4,000+ users"]}],
    "projects": [],
    "education": [{"institution": "University of Toronto",
                   "location": "Toronto, ON", "degree": "BSc Computer Science",
                   "start": "2020", "end": "2024", "details": []}],
    "skills": [{"category": "Languages", "items": ["Python", "TypeScript"]}],
}
ENGINE_WAITING = {"status": "waiting_usage_exhausted", "model": "claude-fable-5",
                  "retry_in_seconds": 300, "last_error": "usage limit reached"}
USAGE_503 = {"detail": "UsageExhausted: usage limit cooldown: retry in 300s"}


async def main() -> int:
    uploads: list[str] = []
    resume_pdf = Path(tempfile.mkdtemp()) / "resume.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4\n% sanitized placeholder\n%%EOF\n")

    async def route(r):
        path = r.request.url.split(BASE_URL)[-1].split("?")[0]
        if path == "/api/profile/upload":
            uploads.append(r.request.method)
            if len(uploads) == 1:  # usage limit on the first attempt
                await r.fulfill(status=503, content_type="application/json",
                                body=json.dumps(USAGE_503))
            else:
                await r.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"version": 1, "confirmed": False,
                                                 "profile": SAMPLE_PROFILE,
                                                 "ledger": {}}))
            return
        if path == "/api/profile":
            await r.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"version": None}))
            return
        if path == "/api/status":
            await r.fulfill(status=200, content_type="application/json",
                            body=json.dumps({"app": "job-agent",
                                             "engine": ENGINE_WAITING}))
            return
        if path.startswith("/api/"):
            await r.fulfill(status=200, content_type="application/json", body="{}")
            return
        await r.continue_()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.route("**/*", route)
        await page.goto(BASE_URL + "/")
        await page.wait_for_selector("text=Upload your resume")

        # Attempt 1 — the usage limit rejects it.
        await page.set_input_files("input[type=file]", str(resume_pdf))
        await page.wait_for_selector("text=usage limit cooldown")
        assert len(uploads) == 1, uploads
        value = await page.eval_on_selector("input[type=file]", "el => el.value")
        assert value == "", f"file input still holds {value!r} after a failure"
        print("[ok] failure shown inline; file input cleared for a same-file retry")

        # Attempt 2 — the SAME file through the SAME input element.
        await page.set_input_files("input[type=file]", str(resume_pdf))
        await page.wait_for_selector("text=Your profile")
        assert len(uploads) == 2, uploads
        print("[ok] same-file retry reached the API and the app advanced")

        await browser.close()

    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, cwd=Path(__file__).resolve().parents[2])
    print("provenance:",
          f"head={head.stdout.strip()[:40]}",
          f"date={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
          f"base_url={BASE_URL}",
          f"os={sys.platform}")
    print("=== UPLOAD RETRY BROWSER CHECK PASS")
    return 0


raise SystemExit(asyncio.run(main()))
