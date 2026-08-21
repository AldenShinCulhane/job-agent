"""Browser check (PLAN.md M5, 'invalid upload' recovery): a failed resume
upload must be retriable with the SAME file from the same input element.

By default this starts its own disposable server from THIS checkout, so
the bundle under test is provably the code at the printed SHA:

  * the worktree must be clean (otherwise HEAD does not describe the
    source that gets built) — override with --allow-dirty, which downgrades
    the log to operator-attested;
  * the frontend is rebuilt from that source immediately before serving;
  * `uv run job-agent` from this checkout serves that build, and is shut
    down when the check finishes.

Passing --base-url instead points the check at a server this script did
not start. That cannot bind the served bundle to any SHA, so the run is
labelled operator-attested.

Every /api/* response is mocked with sanitized sample data — no real
profile or LinkedIn data is fetched or rendered. The first upload attempt
is answered 503 (usage limit), the second 200. Two things are asserted,
and only together do they prove the retry path:

1. After the failed attempt the file input's value is EMPTY. This is the
   DOM precondition browsers require: an input whose value still holds the
   previous path fires no `change` event when the user picks that same
   file again, so the retry would silently do nothing.
2. A second selection of the same file reaches the API and the app
   advances to the profile view.

Scope note: Playwright's set_input_files always dispatches `change`, even
when the same file is re-selected, so assertion 2 alone cannot distinguish
a fixed input from a broken one — assertion 1 is what covers the same-file
case. (Against the pre-fix bundle assertion 1 fails with
"file input still holds 'C:\\fakepath\\resume.pdf'".)

Usage:
  uv run python scripts/ci/upload_retry_browser_check.py [--allow-dirty]
  uv run python scripts/ci/upload_retry_browser_check.py --base-url URL
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parents[2]
PORT = 8700
SELF_SERVED_URL = f"http://127.0.0.1:{PORT}"

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


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=REPO, check=True).stdout.strip()


def wait_for_health(url: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


async def run_check(base_url: str) -> None:
    uploads: list[str] = []
    resume_pdf = Path(tempfile.mkdtemp()) / "resume.pdf"
    resume_pdf.write_bytes(b"%PDF-1.4\n% sanitized placeholder\n%%EOF\n")

    async def route(r):
        path = r.request.url.split(base_url)[-1].split("?")[0]
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
        await page.goto(base_url + "/")
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


def main() -> int:
    args = sys.argv[1:]
    external = None
    if "--base-url" in args:
        external = args[args.index("--base-url") + 1].rstrip("/")
    allow_dirty = "--allow-dirty" in args

    if external:
        print(f"[warn] testing {external}, a server this check did not start — "
              "the served bundle is NOT bound to any SHA (operator-attested)")
        asyncio.run(run_check(external))
        binding = "operator-attested (external base-url)"
        served = external
    else:
        dirty = git("status", "--porcelain")
        if dirty and not allow_dirty:
            print("[FAIL] worktree is dirty, so HEAD would not describe the "
                  "bundle under test:\n" + dirty)
            return 1
        print("=== [1/3] rebuilding the frontend from this checkout")
        subprocess.run(["npm", "run", "build"], cwd=REPO / "frontend",
                       check=True, stdout=subprocess.DEVNULL)
        print("=== [2/3] starting a disposable server from this checkout")
        if wait_for_health(SELF_SERVED_URL, timeout=2):
            print(f"[FAIL] something is already serving {SELF_SERVED_URL} — "
                  "stop it so this check can serve its own build")
            return 1
        server = subprocess.Popen(["uv", "run", "job-agent"], cwd=REPO,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        try:
            if not wait_for_health(SELF_SERVED_URL):
                print("[FAIL] server did not become healthy")
                return 1
            print("=== [3/3] browser check")
            asyncio.run(run_check(SELF_SERVED_URL))
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
        binding = ("SHA-bound: clean worktree, frontend rebuilt from this "
                   "checkout, served by this checkout's own entrypoint")
        if allow_dirty and dirty:
            binding = "operator-attested (--allow-dirty with local edits)"
        served = SELF_SERVED_URL

    print("provenance:",
          f"head={git('rev-parse', 'HEAD')}",
          f"ref={git('rev-parse', '--abbrev-ref', 'HEAD')}",
          f"date={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
          f"served={served}",
          f"os={sys.platform}-{subprocess.run(['uname', '-m'], capture_output=True, text=True).stdout.strip()}")
    print("binding:", binding)
    print("=== UPLOAD RETRY BROWSER CHECK PASS")
    return 0


raise SystemExit(main())
