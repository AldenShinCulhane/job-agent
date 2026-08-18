# Job Agent

A local web app that turns your resume + a LinkedIn job search into tailored,
LaTeX-typeset application packages — a resume and cover letter PDF per job,
with strict page-limit enforcement and no fabricated claims.

> **Status: v2 in development.** The engine, setup, and app skeleton work
> (M0). Resume ingestion, LinkedIn search, scoring UI, and package
> generation are being built — see `PLAN.md` for the full spec and
> milestones.

## What it does

1. **Upload your resume** (PDF/DOCX). It's re-rendered into a clean,
   ATS-friendly LaTeX template that you confirm once — with unlimited inline
   corrections. (Your original layout is not preserved; the template is the
   point.) Scanned PDFs are handled by OCR fallback.
2. **Search LinkedIn** by keywords + location. You sign in once in a real
   browser window; the session persists locally.
3. **Browse scored jobs** in the UI — match score, experience filter,
   strengths/gaps — before anything is generated.
4. **Select the jobs you want.** For each one, the app autonomously writes a
   tailored resume and cover letter, validates every claim against facts
   extracted from *your* resume, enforces your page limit (default 1 page —
   never one line over), and drops finished PDFs in `output/`.

The app stops at PDFs. You click Apply yourself.

## Quick start

macOS or Linux (Windows: use [WSL](https://learn.microsoft.com/windows/wsl/install)
and follow the Linux steps inside it).

```bash
git clone <this-repo> && cd job-agent
./scripts/setup.sh                                    # checks/installs everything, tells you what's missing
uv run python -m backend.core.claude_cli auth login   # sign in with your Claude subscription (first time only)
uv run job-agent                                      # → http://127.0.0.1:8700
```

`setup.sh` needs internet access and will report exactly what's missing and
how to install it. The pieces:

| Dependency | Used for | Required? |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python environment | yes (auto-installed) |
| Node.js 20+ / npm | frontend build | yes |
| [Tectonic](https://tectonic-typesetting.github.io) or pdflatex | PDF typesetting | for PDF generation |
| Tesseract + OCRmyPDF (+ Ghostscript) | scanned-resume OCR fallback | only for image-based PDFs |
| Playwright Chromium | LinkedIn browser | installed by setup when needed |

## Claude subscription — no API keys

All AI work runs through the **Claude Agent SDK using your Claude
subscription** (Pro/Max). There are no API keys and no per-token billing —
you sign in once with the same account you use for Claude.

- The app prefers `claude-fable-5` and automatically falls back to
  `claude-opus-5` if your plan doesn't include Fable.
- Subscription usage limits apply. If you hit your plan's limit mid-batch,
  the app pauses, shows a banner, and resumes automatically — no work is
  lost and nothing is regenerated.
- The model is locked down for this app: no file, shell, or web tools, and
  no open-ended agent loop — every call is a single prompt that must answer
  in JSON.
- The app keeps its own Claude sign-in in `.data/claude/`, separate from
  any Claude Code install you may have — so the login command above is
  needed once even if you already use Claude Code.

> **Note for redistributors:** Anthropic requires prior approval for
> third-party products that offer claude.ai sign-in (see the
> [Agent SDK terms](https://code.claude.com/docs/en/agent-sdk/overview)).
> Using this repo yourself on your own account is personal tooling; if you
> ship a product based on it, get that approval first.

## Privacy

- **Your resume content and job descriptions are sent to Claude (Anthropic)
  for processing.** See [Anthropic's privacy policy](https://www.anthropic.com/legal/privacy).
  Beyond that, the app's network traffic is LinkedIn itself (through your
  own signed-in browser session) and dependency downloads during setup;
  the bundled Claude CLI runs with telemetry and auto-update traffic
  disabled.
- The server binds to `127.0.0.1` only — nothing is exposed to your network.
- All local state (your profile, jobs database, LinkedIn browser session)
  lives in `.data/`, created with owner-only permissions. Generated PDFs go
  to `output/`. Both are gitignored.

## LinkedIn: read this before using

**Automated access to LinkedIn violates LinkedIn's User Agreement.** This
app signs in as you and browses job search results the way a fast human
would — paced, capped, in a real browser — but pacing reduces load and
detection surface, **not** your terms-of-service exposure. Consequences,
including account restriction, are yours to accept. To keep risk as low as
practical, the app:

- uses your own logged-in session in one persistent real browser — no
  parallel scraping, no headless fleet, no API abuse;
- paces every action with human-like delays and daily caps;
- **stops cleanly the moment LinkedIn shows a captcha or checkpoint** and
  asks you to resolve it yourself in the browser window — it never attempts
  a bypass.

If that risk isn't acceptable to you, don't use the LinkedIn integration.
The job-source layer is a small interface, so other sources can be added.

**Sign in with your LinkedIn email and password.** Google refuses its own
sign-in inside automated browsers ("This browser or app may not be
secure"), so "Continue with Google" will not work in the window the app
opens — and the app will never try to disguise the browser to get around
that. If your LinkedIn account only has Google sign-in, set a password
first (LinkedIn Settings → Sign in & security).

## How tailoring stays honest

Every fact in your confirmed resume gets a stable source ID. Tailored
bullets and cover-letter paragraphs must cite the source facts they rely
on, and a validator checks that each cited fact actually supports the
claim — after generation *and* after every page-fitting rewrite. New
employers, degrees, dates, skills, or metrics that aren't in your resume
are rejected and repaired. A package that can't pass validation or meet
your page limit **fails** rather than shipping something oversized or
untrue.

## Development

```bash
uv run job-agent                        # backend + built frontend on :8700
cd frontend && npm run dev              # optional: Vite dev server w/ HMR, proxies /api
uv run python -m backend.tests.smoke_llm  # live engine smoke test (uses your subscription)
```

`PLAN.md` is the working spec. Architecture in short: FastAPI backend
(SQLite state, SSE progress), React + Vite + Tailwind frontend, Playwright
supervisor for LinkedIn, Tectonic/pdflatex for PDFs, Claude Agent SDK for
all reasoning.
