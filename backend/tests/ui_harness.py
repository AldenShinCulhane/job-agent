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
         easy: bool = False) -> dict:
    return {
        "job_id": job_id,
        "title": f"Backend Engineer {job_id}",
        "company": "Example Co",
        "location": "Toronto, ON",
        "posted_text": "2 days ago",
        "workplace": "hybrid",
        "easy_apply": easy,
        "url": f"https://example.invalid/jobs/{job_id}",
        "first_seen_at": "2026-09-15T12:00:00Z",
        "last_seen_at": "2026-09-15T12:00:00Z",
        "description": None,
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
                  "jobs": [_job("a", 80, easy=True), _job("b", 60),
                           _job("c", 30), _job("d", None)]},
    "/api/packages": {"page_limit": 1, "packages": [
        _package(1, "done"), _package(2, "generating"),
        _package(3, "failed", "compile failed"), _package(4, "queued"),
        _package(5, "waiting_usage"),
        # A status the UI has no label for: it must still render, in the
        # neutral tone, under its raw name.
        _package(6, "archived"),
    ]},
    "/api/profile": {
        "version": 5, "confirmed": False, "live_version": 4,
        "profile": {
            "name": "Sample Person", "headline": "Engineer",
            "contact": {"phone": None, "email": "sample@example.invalid",
                        "linkedin": None, "github": None, "website": None,
                        "location": "Toronto, ON"},
            "experiences": [{"title": "Engineer", "company": "Example Co",
                             "location": None, "start": "2025", "end": "2026",
                             "bullets": ["Did things"]}],
            "projects": [], "education": [], "skills": [
                {"category": "Languages", "items": ["Python"]}],
        },
    },
    "/api/settings": {"resume_page_limit": 1, "engine": ENGINE,
                      "latex_engines": []},
}


def route_fixtures(page) -> None:
    """Answer every /api request from API_FIXTURES.

    The event stream gets a complete, empty stream: EventSource reconnects
    quietly, whereas an aborted or non-200 response is logged by Chromium as
    a console error and would trip the no-console-errors assertion. The
    preview iframe gets a text stub for the same reason.
    """

    def api(route):
        path = route.request.url.split("?", 1)[0]
        path = path[path.index("/api"):]
        if path == "/api/events":
            route.fulfill(status=200, content_type="text/event-stream",
                          body=": connected\n\n")
            return
        body = API_FIXTURES.get(path)
        if body is None or route.request.method != "GET":
            route.fulfill(status=404, content_type="application/json",
                          body=json.dumps({"detail": f"no fixture for {path}"}))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/**", api)
    page.route("**/api/profile/preview.pdf*", lambda route: route.fulfill(
        status=200, content_type="text/plain", body="preview stub"))


# ---------------------------------------------------------------- servers


class _DistHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):  # keep pytest output clean
        pass


@pytest.fixture(scope="session")
def dist_url():
    if not (DIST / "index.html").exists():
        pytest.skip("frontend/dist is missing: `cd frontend && npm run build`")
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
            pytest.skip(f"Chromium unavailable: {str(exc).splitlines()[0]}")
        try:
            yield instance
        finally:
            instance.close()


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
