"""Browser check (PLAN.md M5, 'invalid upload' recovery): a failed resume
upload must be retriable with the SAME file from the same input element.

Isolation, which matters more than convenience here: this check never runs
the app's backend. It captures HEAD first, clones THIS repository into a
throwaway directory, pins that clone to the captured SHA, builds the
frontend there, and serves the built files with a static file server on an
ephemeral port bound to 127.0.0.1. Nothing touches the working checkout's
.data, no startup recovery runs, no search or package worker can resume,
and no subscription call is possible. Every /api/* response the page sees
is a Playwright mock with sanitized sample data.

That also makes the SHA claim airtight: the SHA is read before any build,
the bundle is built from a checkout pinned to exactly that SHA, and the
same captured SHA is what the log prints. Edits to the working checkout
during the run cannot change what was tested (they are simply not under
test — the check says so when the worktree is dirty).

The first upload attempt is answered 503 (usage limit), the second 200.
Two things are asserted, and only together do they prove the retry path:

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
  uv run python scripts/ci/upload_retry_browser_check.py
  uv run python scripts/ci/upload_retry_browser_check.py --base-url URL
      (tests a server this script did not build or start: the served
       bundle is bound to nothing, so the run is operator-attested)
"""

import asyncio
import contextlib
import json
import socket
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


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=cwd, check=True).stdout.strip()


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
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
    if "--base-url" in args:
        external = args[args.index("--base-url") + 1].rstrip("/")
        print(f"[warn] testing {external}: this check neither built nor started "
              "it, so the served bundle is bound to no SHA (operator-attested)")
        asyncio.run(run_check(external))
        print("provenance:", f"served={external}",
              f"date={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
        print("binding: operator-attested (external base-url)")
        print("=== UPLOAD RETRY BROWSER CHECK PASS")
        return 0

    # 1. Pin the SHA before anything is built, so nothing that happens
    #    later can change which commit this log is about.
    sha = git("rev-parse", "HEAD")
    ref = git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = git("status", "--porcelain")
    if dirty:
        print("[note] the working checkout has uncommitted changes; they are "
              f"NOT under test — this run tests committed {sha[:12]}:\n{dirty}")

    work = Path(tempfile.mkdtemp(prefix="job-agent-uploadcheck."))
    clone = work / "checkout"
    print(f"=== [1/4] isolated checkout pinned to {sha[:12]} ({work})")
    subprocess.run(["git", "clone", "--quiet", "--shared", str(REPO), str(clone)],
                   check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "--quiet", "--detach", sha],
                   check=True)
    got = git("rev-parse", "HEAD", cwd=clone)
    if got != sha:
        print(f"[FAIL] isolated checkout is at {got}, expected {sha}")
        return 1

    print("=== [2/4] building the frontend inside the isolated checkout")
    subprocess.run(["npm", "ci", "--no-fund", "--no-audit"], cwd=clone / "frontend",
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["npm", "run", "build"], cwd=clone / "frontend",
                   check=True, stdout=subprocess.DEVNULL)

    # 3. Serve the built files only. The app's backend is never started:
    #    it would open the working checkout's .data and run startup
    #    recovery, which can resume packages, scoring and LinkedIn work.
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    dist = clone / "frontend" / "dist"
    print(f"=== [3/4] serving {dist.relative_to(clone)} statically on {base_url} "
          "(no backend, no .data, no recovery)")
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
         "--directory", str(dist)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_for(base_url + "/"):
            print("[FAIL] static server did not come up")
            return 1
        print("=== [4/4] browser check")
        asyncio.run(run_check(base_url))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    arch = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
    print("provenance:",
          f"head={sha}",
          f"ref={ref}",
          f"date={datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
          f"served={base_url}",
          f"os={sys.platform}-{arch}")
    print("binding: SHA-bound — bundle built from an isolated checkout pinned "
          f"to {sha[:12]} (captured before the build) and served statically; "
          "the app backend was never started")
    print(f"workdir left in place: {work}")
    print("=== UPLOAD RETRY BROWSER CHECK PASS")
    return 0


raise SystemExit(main())
