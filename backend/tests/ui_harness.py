"""Browser-level checks of the BUILT frontend.

The page under test is `frontend/dist`, served by a throwaway static server;
every `/api/**` request is answered from the synthetic fixtures below, so a
run touches no database, no browser profile and no real profile data, and
the same states render every time — including the ones a live app may
never show at once (every status tone, an engine failure and a LinkedIn
stop together, a failed package next to a running one).

Skips, rather than fails, when the build or Chromium is missing: these are
gates on the UI, not on the environment.
"""

from __future__ import annotations

import os
import queue
from dataclasses import dataclass

import functools
import http.server
import json
import struct
import threading
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "frontend" / "dist"

# ---------------------------------------------------------------- fixtures


def _job(job_id: str, score: float | None, rerank: float | None = None,
         easy: bool = False, title: str | None = None,
         description: str | None = None) -> dict:
    return {
        "job_id": job_id,
        "title": title or f"Backend Engineer {job_id}",
        "company": "Example Co",
        "location": "Toronto, ON",
        "posted_text": "2 days ago",
        "workplace": "hybrid",
        "easy_apply": easy,
        "url": f"https://example.invalid/jobs/{job_id}",
        "first_seen_at": "2026-09-15T12:00:00Z",
        "last_seen_at": "2026-09-15T12:00:00Z",
        "description": description,
        "description_hash": None,
        "detail_fetched_at": None,
        "deterministic_score": score,
        "llm_rerank": rerank,
        "llm_rank": None,
        "score_breakdown": None if score is None else {
            "skills": 1, "experience": 1, "education": 1,
            "matched_skills": ["python"], "missing_skills": ["go"],
            "match_reasons": ["python"], "gap_reasons": ["no go"],
            "exceeds_experience": False, "user_years": 1,
        },
    }


def _package(pid: int, status: str, error: str | None = None) -> dict:
    return {
        "id": pid, "job_id": f"j{pid}", "profile_version": 4, "jd_hash": "h",
        "page_limit": 1, "status": status, "error": error, "retry_count": 0,
        "output_dir": None if status != "done" else "/out",
        "created_at": "2026-09-15T12:00:00Z", "updated_at": "2026-09-15T12:00:00Z",
        "title": f"Role {pid}", "company": "Example Co",
        "url": f"https://example.invalid/jobs/j{pid}", "provenance": {},
    }


ENGINE = {"status": "auth_required", "model": None, "retry_in_seconds": None,
          "last_error": "not logged in"}

# Set to "1" where the browser tests are a gate rather than a courtesy:
# then a missing dist, Chromium or axe bundle FAILS instead of skipping,
# and conftest fails the session if any UI test was skipped or none ran.
UI_REQUIRED = os.environ.get("UI_TESTS_REQUIRED") == "1"


def unavailable(reason: str):
    (pytest.fail if UI_REQUIRED else pytest.skip)(reason)


AXE_PATH = DIST.parent / "node_modules" / "axe-core" / "axe.min.js"


@dataclass(frozen=True)
class Response:
    """A fixture answer with a status code, for the failing cases."""
    status: int
    body: dict


def profile_payload(version=5, *, confirmed=False, live=4, name="Sample Person"):
    return {
        "version": version, "confirmed": confirmed, "live_version": live,
        "profile": {
            "name": name, "headline": "Engineer",
            "contact": {"phone": None, "email": "sample@example.invalid",
                        "linkedin": None, "github": None, "website": None,
                        "location": "Toronto, ON"},
            "experiences": [{"title": "Engineer", "company": "Example Co",
                             "location": None, "start": "2025", "end": "2026",
                             "bullets": ["Did things"]}],
            "projects": [], "education": [], "skills": [
                {"category": "Languages", "items": ["Python"]}],
        },
    }

# Chosen so that every tone in styles.ts renders on the first paint of some
# tab: danger (engine banner, failed package), warn (LinkedIn banner, run
# message, waiting package, missing LaTeX), info (generating package), ok
# (done package, score 80), neutral (queued package, score 30, and a
# package whose status the UI does not know).
API_FIXTURES: dict[str, dict] = {
    "/api/status": {"app": "job-agent", "engine": ENGINE},
    "/api/linkedin/status": {"state": "logged_out", "detail": "session expired"},
    "/api/search/latest": {"id": 7, "status": "stopped_cap", "collected": 4,
                           "message": "Daily cap reached",
                           "config": {"keywords": "backend"}},
    "/api/jobs": {"run_id": 7, "scoring_pending": 0, "scoring_stall": None,
                  "jobs": [_job("a", 80, easy=True,
                                description="Build APIs.\nRequirements: Python."),
                           _job("b", 60),
                           _job("c", 30), _job("d", None)]},
    "/api/packages": {"page_limit": 1, "packages": [
        _package(1, "done"), _package(2, "generating"),
        _package(3, "failed", "compile failed"), _package(4, "queued"),
        _package(5, "waiting_usage"),
        # A status the UI has no label for: it must still render, in the
        # neutral tone, under its raw name.
        _package(6, "archived"),
    ]},
    "/api/profile": profile_payload(),
    "/api/settings": {"resume_page_limit": 1, "engine": ENGINE,
                      "latex_engines": []},
}


# What a mutation answers, by method and path. Every gate and button on
# the pages has an answer here, so a keyboard walkthrough can press them
# all without a real backend.
API_POSTS: dict[tuple[str, str], dict] = {
    ("POST", "/api/profile/upload"): profile_payload(6, live=4, name="Uploaded Person"),
    ("PUT", "/api/profile"): profile_payload(),
    ("POST", "/api/profile/confirm"): profile_payload(confirmed=True, live=5),
    ("POST", "/api/profile/discard"): profile_payload(4, confirmed=True, live=4),
    ("POST", "/api/linkedin/connect"): {"state": "connecting", "detail": None},
    ("POST", "/api/search/start"): {"run_id": 8, "status": "running"},
    ("POST", "/api/search/resume"): {"id": 7, "status": "running"},
    ("POST", "/api/search/rerank"): {"reranked": 2},
    ("POST", "/api/search/retry-scoring"): {"resumed": True},
    ("POST", "/api/packages/generate"): {"package_ids": [9], "created": 1},
    ("POST", "/api/engine/reprobe"): {"ok": False, "verified": True,
                                      "error": "auth_required",
                                      "detail": "not logged in"},
    ("PUT", "/api/settings"): {"resume_page_limit": 2},
}
EXPLANATION = {"strengths": ["Python every day"], "gaps": ["No Go"],
               "pitch": "A close fit on the backend work."}


def route_fixtures(page, fixtures: dict | None = None, *,
                   live_events: bool = False) -> dict:
    """Answer every /api request from a fixture table, and return that
    table: it is consulted per request, so a test can change an answer
    after the page is up and see the change land through an event or a
    poll.

    The event stream gets a complete, empty stream unless `live_events`,
    which lets it through to the static server's real stream: EventSource
    reconnects quietly, whereas an aborted or non-200 response is logged
    by Chromium as a console error and would trip the no-console-errors
    assertion. The preview iframe gets a text stub for the same reason.
    """
    table = dict(API_FIXTURES) if fixtures is None else fixtures

    def fulfill(route, answer):
        if isinstance(answer, Response):
            route.fulfill(status=answer.status, content_type="application/json",
                          body=json.dumps(answer.body))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(answer))

    def api(route, *_):
        path = route.request.url.split("?", 1)[0]
        path = path[path.index("/api"):]
        method = route.request.method
        if path == "/api/events":
            if live_events:
                route.continue_()
            else:
                route.fulfill(status=200, content_type="text/event-stream",
                              body=": connected\n\n")
            return
        if method == "GET":
            answer = table.get(path)
        elif (method, path) in table:
            answer = table[(method, path)]
        elif (method, path) in API_POSTS:
            answer = API_POSTS[(method, path)]
        elif method == "POST" and path.endswith("/explain"):
            answer = EXPLANATION
        elif method == "POST" and path.endswith("/detail"):
            job_id = path.split("/")[3]
            jobs = table.get("/api/jobs", {}).get("jobs", [])
            job = next((j for j in jobs if j["job_id"] == job_id), None)
            answer = None if job is None else dict(
                job, description="Fetched description.", refetched=True,
                changed=False)
        else:
            answer = None
        if answer is None:
            route.fulfill(status=404, content_type="application/json",
                          body=json.dumps({"detail": f"no fixture for {method} {path}"}))
            return
        fulfill(route, answer)

    page.route("**/api/**", api)
    page.route("**/api/profile/preview.pdf*", lambda route, *_: route.fulfill(
        status=200, content_type="text/plain", body="preview stub"))
    return table


# ------------------------------------------------------------- live events


class EventHub:
    """The static server's /api/events, driven from the test: push a named
    event to every open stream, or drop them all and decide whether the
    reconnects are accepted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self.accepting = True

    def _add(self, q):
        with self._lock:
            self._clients.append(q)

    def _remove(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    @property
    def connections(self) -> int:
        with self._lock:
            return len(self._clients)

    def push(self, topic: str, data: dict | None = None) -> None:
        message = f"event: {topic}\ndata: {json.dumps(data or {})}\n\n"
        with self._lock:
            for q in self._clients:
                q.put(message)

    def drop(self) -> None:
        with self._lock:
            for q in self._clients:
                q.put(None)

    def wait_for_connections(self, n: int, timeout: float = 5.0, *,
                             page=None) -> None:
        """Wait until exactly `n` streams are open. With `page`, the wait
        pumps Playwright: the page's route handler for a reconnect only
        runs while some Playwright call is in progress, so a plain sleep
        here would be waiting for a request it is itself blocking."""
        import time
        deadline = time.monotonic() + timeout
        while self.connections != n:
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"expected {n} stream connection(s), have {self.connections}")
            if page is not None:
                page.wait_for_timeout(20)
            else:
                time.sleep(0.02)


# ---------------------------------------------------------------- servers


class _DistHandler(http.server.SimpleHTTPRequestHandler):
    hub: EventHub  # set by the server fixture

    def log_message(self, *_):  # keep pytest output clean
        pass

    def do_GET(self):
        if not self.path.startswith("/api/events"):
            return super().do_GET()
        hub = type(self).hub
        if not hub.accepting:
            self.send_response(503)
            self.end_headers()
            return
        q: queue.Queue = queue.Queue()
        hub._add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            # A short retry so a dropped stream is back within the test's
            # patience; the app's own reconnect logic is what is under test.
            self.wfile.write(b"retry: 200\n: connected\n\n")
            self.wfile.flush()
            while True:
                message = q.get()
                if message is None:
                    break
                self.wfile.write(message.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub._remove(q)


_HUB = EventHub()


@pytest.fixture(scope="session")
def dist_url():
    if not (DIST / "index.html").exists():
        unavailable("frontend/dist is missing: `cd frontend && npm run build`")
    _DistHandler.hub = _HUB
    handler = functools.partial(_DistHandler, directory=str(DIST))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Error as exc:  # no browser downloaded on this machine
            unavailable(f"Chromium unavailable: {str(exc).splitlines()[0]}")
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def events(dist_url):
    """The live stream for one test. Every stream is dropped afterwards
    so the next test starts with none open."""
    _HUB.accepting = True
    yield _HUB
    _HUB.accepting = False
    _HUB.drop()
    _HUB.wait_for_connections(0)
    _HUB.accepting = True


# --------------------------------------------------------------------- axe

AXE_RUN = """() => axe.run(document, { exclude: [['iframe']] }).then(r => ({
  violations: r.violations.map(v => ({
    id: v.id, impact: v.impact,
    nodes: v.nodes.slice(0, 5).map(n => n.target.join(' ')),
  })),
  incomplete: r.incomplete.map(i => ({ id: i.id, nodes: i.nodes.length })),
}))"""


def axe_check(page) -> dict:
    """axe-core's default rules over the page, browser-rendered PDF content
    excluded (the preview iframe is the browser's viewer, not ours).
    Returns violations and the incomplete (needs-review) list."""
    if not AXE_PATH.exists():
        unavailable(f"axe-core bundle missing at {AXE_PATH}: `cd frontend && npm install`")
    page.add_script_tag(path=str(AXE_PATH))
    return page.evaluate(AXE_RUN)


# ---------------------------------------------------------------- measuring

# Runs in the page. For each element matched, the rendered text colour and
# the first painted background behind it, and the WCAG ratio between them.
# Elements rendered at reduced opacity (disabled controls) are skipped —
# inactive components are exempt, and the colour alone would mismeasure them.
MEASURE_JS = """
(selector) => {
  const parse = (c) => {
    const m = c && c.match(/rgba?\\(([^)]+)\\)/)
    if (!m) return null
    const [r, g, b, a = 1] = m[1].split(',').map(parseFloat)
    return a === 0 ? null : [r, g, b]
  }
  const lum = ([r, g, b]) => {
    const ch = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
  }
  const ratio = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05) }
  const bgBehind = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor)
      if (c) return c
    }
    return parse(getComputedStyle(document.documentElement).backgroundColor)
  }
  const effectiveOpacity = (el) => {
    let o = 1
    for (let e = el; e; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity)
    return o
  }
  const out = []
  for (const el of document.querySelectorAll(selector)) {
    if (!el.checkVisibility()) continue
    if (effectiveOpacity(el) < 1) continue
    const fg = parse(getComputedStyle(el).color)
    const bg = bgBehind(el)
    out.push({
      text: (el.textContent || '').trim().slice(0, 40),
      cls: el.className,
      fg, bg, ratio: fg && bg ? ratio(fg, bg) : null,
    })
  }
  return out
}
"""

PLACEHOLDER_JS = """
() => {
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); const [r,g,b,a=1] = m[1].split(',').map(parseFloat); return a === 0 ? null : [r,g,b] }
  const lum = ([r, g, b]) => { const ch = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }; return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b) }
  const ratio = (a, b) => { const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05) }
  const out = []
  for (const el of document.querySelectorAll('input[placeholder], textarea[placeholder]')) {
    if (!el.checkVisibility() || !el.placeholder) continue
    const fg = parse(getComputedStyle(el, '::placeholder').color)
    let bg = null
    for (let e = el; e && !bg; e = e.parentElement) bg = parse(getComputedStyle(e).backgroundColor)
    out.push({ placeholder: el.placeholder, ratio: fg && bg ? ratio(fg, bg) : null })
  }
  return out
}
"""


def resolved_token(page, name: str) -> str:
    """The rgb() a token resolves to right now, via a probe element."""
    return page.evaluate(
        """(name) => {
          const el = document.createElement('div')
          el.style.color = `var(--${name})`
          document.body.appendChild(el)
          const c = getComputedStyle(el).color
          el.remove()
          return c
        }""",
        name,
    )


def luminance_of_css_rgb(css: str) -> float:
    inner = css[css.index("(") + 1:css.index(")")]
    r, g, b = [float(v) for v in inner.replace("/", ",").split(",")[:3]]

    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def png_pixel(png: bytes, x: int = 0, y: int = 0) -> tuple[int, int, int]:
    """One pixel of a PNG, standard library only.

    Decodes the IDAT stream and unfilters up to the requested row — enough
    for sampling a corner of a screenshot, which is all the flash-of-wrong-
    theme check needs.
    """
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    idat = b""
    width = height = channels = 0
    while pos < len(png):
        length, kind = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", data[:10])
            assert depth == 8, "8-bit PNG expected"
            channels = {2: 3, 6: 4}[ctype]
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels
    prev = bytearray(stride)
    for row in range(y + 1):
        start = row * (stride + 1)
        ftype = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 0xFF
        prev = line
    off = x * channels
    return tuple(prev[off:off + 3])
