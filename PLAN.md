# Job Agent v2 — LinkedIn-Native Tailoring Studio

**Status:** Approved for M0 (working spec, rev 4 — final planning revision)
**Branch:** `v2-linkedin-webapp`
**Goal:** Fundamentally rework job-agent from a hiring.cafe CLI pipeline into a generalized, cloneable web app: anyone uploads their resume, searches LinkedIn jobs, sees them scored in a nice UI, selects the ones they want, and gets tailored LaTeX-typeset resume + cover letter PDFs.

---

## 1. Product Vision

A person clones this repo, runs one setup command, and gets a local web app where they:

1. **Upload their most recent general resume** (PDF/DOCX). We extract structured content and re-render it into our proven LaTeX template. They confirm the preview once (with unlimited inline corrections inside that single gate).
2. **Enter location + keywords** (and optional filters) for the jobs they want.
3. **Sign into LinkedIn once** in a real browser window we open. Session persists locally.
4. **Browse a scored job list** pulled from LinkedIn — cards with title, company, location, posted date, match score, and a link to the posting.
5. **Select jobs** they want packages for.
6. **Receive tailored PDFs** — resume + cover letter per job, generated automatically with strict page-limit and spacing enforcement, saved to `output/` and downloadable from the UI.

The app stops at PDFs. The user clicks Apply on LinkedIn themselves. (Easy Apply automation is explicitly out of scope for v2.)

### Design principles

- **Exactly three human gates:** (1) resume review/confirmation, (2) LinkedIn authentication when needed, (3) job selection. Everything else — parsing, normalization, scoring, tailoring, trimming, QA, retries — is autonomous. Error states are **not approval gates**, and they split into two classes:
  - `waiting` — auto-resolving: exhausted Claude allowance, transient failures. The app pauses, banners, and retries with backoff. No user action.
  - `user_intervention_required` — cannot resolve by waiting: unauthenticated Claude, expired LinkedIn session / captcha stop, invalid upload. The app banners with the exact action needed and resumes automatically once it's done. Still no decision prompt — the user performs an action, never approves one.

  No score confirmation, no trim approval, no per-document review, no model-choice prompt.
- **Generalized.** No assumptions about who the user is. Profile comes entirely from the uploaded resume + a small settings form. Clear README, one-command setup, sensible defaults.
- **Subscription-only LLM.** No API keys, no per-token billing. The engine is the user's **Claude subscription** via the Claude Agent SDK. Usage is subject to the user's plan limits — the app handles allowance exhaustion as a durable wait/retry state, never as a prompt or an API-key fallback.

---

## 2. Locked Decisions

| Decision | Choice | Rationale |
|---|---|---|
| UI stack | **FastAPI backend + React (Vite) frontend** | Nice-looking, full control over job cards/selection/progress. One command starts both. |
| Job source | **LinkedIn, via Playwright + user's own login**, behind a `JobSource` interface | LinkedIn is where the user applies. **Risk is stated honestly:** LinkedIn's User Agreement prohibits scraping and automated access. Human pacing and the user's own session reduce *load and detection surface*, not ToS exposure. Account restriction is an **accepted product limitation** — documented in README, stop-cleanly-on-captcha, never any bypass attempt. The thin `JobSource` interface keeps the provider swappable if LinkedIn becomes untenable. |
| Resume ingestion | **Re-render into our LaTeX template** | Upload PDF/DOCX → structured extraction → rendered in the `resume_subheading2`-style template. Consistent spacing control makes page-limit enforcement reliable. Original visual design is not preserved (documented clearly). OCR fallback for scanned PDFs. |
| LLM engine | **Claude Agent SDK (Python), subscription auth** — no API keys | The SDK bundles the Claude Code CLI, so a separate `claude` install is not required in the default path; setup only has to verify *authentication* (and offer login instructions if absent). All previous multi-provider/API-key code is removed. |
| Model | **`claude-fable-5` if available on the user's plan, else `claude-opus-5`** | Probe at startup with a tiny request; cache the result; re-probe from settings. |
| Effort | **Tiered per task** (see §4.1) — medium for extraction/scoring, high for tailoring, xhigh for difficult repair, max only for final failed-layout repair | Blanket xhigh would burn subscription allowance and make 50-job runs slow for no quality gain on mechanical steps. |
| Application submission | **Stop at PDFs** | Lower account risk; wrong auto-submission is unrecoverable. |
| Legacy pipeline | **Dropped** (hiring.cafe scraper, CLI menu, ReAct agent, multi-provider `llm_client`) | Single product surface. v1's deterministic scorer and LaTeX build code are **ported** (see §5); v1's page-shrink logic is **rewritten**, not ported. |
| LaTeX toolchain | **Tectonic preferred, `pdflatex` fallback** | Tectonic is a single binary that auto-fetches packages — easiest install story. Engine actually used is **recorded per artifact** (Tectonic and pdfLaTeX can paginate differently). |
| Platforms | **macOS + Linux natively; Windows via WSL** (documented) | `setup.sh` targets POSIX; README states WSL path for Windows. |
| Data store | **SQLite** (single file in `.data/`) | Jobs, normalized requirements, scores, generation state machine, artifact provenance. Zero-config, survives restarts, trivially inspectable. |

---

## 3. Architecture

```
job-agent/
├── backend/                    # FastAPI app (Python, uv-managed)
│   ├── main.py                 # app factory, serves built frontend statically
│   ├── api/                    # routes: profile, search, jobs, generate, artifacts, status
│   ├── core/
│   │   ├── llm.py              # Agent SDK wrapper: auth check, model probe (fable→opus5),
│   │   │                       #   per-task effort tiers, structured-output helper,
│   │   │                       #   usage_exhausted → waiting → retry state
│   │   ├── db.py               # SQLite: Profile(+versions), SearchConfig, Job, JobRequirements,
│   │   │                       #   Score, Package(+state machine, provenance)
│   │   └── events.py           # SSE progress stream (one-way; no WebSocket)
│   ├── sources/
│   │   ├── base.py             # JobSource interface (search, fetch_detail, health)
│   │   └── linkedin/
│   │       ├── supervisor.py   # single persistent-context owner; serializes ALL LinkedIn
│   │       │                   #   actions; one browser profile dir; no concurrent locks
│   │       ├── search.py       # search-results scraping (keywords, location, filters)
│   │       ├── job_detail.py   # full job-description scrape per job
│   │       ├── selectors.py    # all selectors, central, with fallback chains
│   │       └── pacing.py       # human-ish delays, jitter, backoff, daily caps, captcha stop
│   ├── resume/
│   │   ├── ingest.py           # PDF/DOCX → text (OCR fallback) → structured profile JSON
│   │   ├── facts.py            # stable source IDs for every fact/bullet; truth ledger
│   │   ├── template.py         # structured profile → LaTeX (our template)
│   │   └── schema.py           # profile JSON schema (jobs, bullets w/ fact IDs, skills, education, links)
│   ├── normalize/
│   │   └── requirements.py     # raw posting → normalized requirements (LLM, cached by JD hash)
│   ├── scoring/
│   │   └── score.py            # deterministic scorer (ported v1 logic) = primary ranking;
│   │                           #   LLM explains / reranks close matches only
│   ├── tailor/
│   │   ├── resume.py           # per-job resume tailoring (fact-ID-referenced claims)
│   │   ├── cover.py            # per-job cover letter (LaTeX template)
│   │   ├── validate.py         # truth validator: rejects claims w/o source-ID support
│   │   ├── compile.py          # tectonic/pdflatex build, page count, bounding boxes, artifacts
│   │   └── page_limit.py       # geometry-aware N-page enforcement (see §4.5)
│   └── tests/fixtures/         # sanitized minimal HTML fragments for selector tests (no real data)
├── frontend/                   # React + Vite + Tailwind
│   └── src/
│       ├── pages/ Onboarding | Search | Jobs | Packages
│       └── components/ JobCard, ScoreBadge, ProgressStream, PdfPreview, ...
├── output/                     # output/<company>_<jobid>/{resume.pdf, cover_letter.pdf, job.md}
├── .data/                      # SQLite DB, browser profile dir, probe cache (gitignored)
├── scripts/setup.sh            # detects/installs: uv, Node/npm, Playwright browser, LaTeX engine,
│                               #   Tesseract + OCRmyPDF (+ Ghostscript if required by OCRmyPDF);
│                               #   states internet requirement; macOS/Linux (Windows → WSL)
├── PLAN.md                     # this file
└── README.md                   # rewritten for v2 (incl. honest LinkedIn ToS/risk section)
```

**Run story:** `./scripts/setup.sh` once, then `uv run job-agent` → FastAPI on `localhost:8700`, serves the built React app, opens the browser. Dev mode runs Vite separately with proxy.

---

## 4. Core Flows (detail)

### 4.1 LLM engine (`backend/core/llm.py`)

- **Auth:** Claude Agent SDK rides the user's Claude subscription. The SDK bundles the Claude Code CLI — no separate install required in the default path. Startup check verifies authentication only; if absent, the UI shows a setup card with login instructions. All LLM calls fail soft to that banner.
- **Model resolution — typed failure routing:** probe `claude-fable-5` with a 1-line request on first run and route by failure class, not blanket-fallback:
  - model unavailable / not entitled on plan → probe/pin `claude-opus-5`
  - authentication missing → `user_intervention_required` (auth card), no fallback
  - usage limit / 429 → `waiting(usage_exhausted)`, retry same model later
  - network/transient → retry same model with backoff
  - invalid SDK configuration → fail the startup check loudly with the config error

  Cache the pinned model in `.data/`; re-probe from settings.
- **Effort tiers (autonomous policy, no user decisions):**

  | Task | Effort |
  |---|---|
  | Resume/job extraction, requirement normalization | medium |
  | Batch scoring explanations / reranks | medium |
  | Resume + cover tailoring | high |
  | Difficult repair (failed trim round, QA fix) | xhigh |
  | Final failed-layout repair | max — only if evaluation during M4 proves it helps; otherwise cap at xhigh |

- **Tool lockdown (explicit implementation criteria):** the SDK exposes the full Claude Code toolset by default, and `allowed_tools=[]` alone may not remove tools. The wrapper must: explicitly **disable Read/Write/Edit/Bash/Web tools** (disallowed-tools / permission configuration per current SDK docs), run **single-turn**, **JSON structured output only**. **The LLM never emits raw LaTeX.** All outputs are schema-validated JSON (tailored bullets, cover paragraphs, layout preferences); application code escapes content and renders every LaTeX document from templates. Our code does all file I/O. Verify the exact option surface against the Agent SDK README at M0; treat "model produced a tool call" as a bug.
- **Allowance handling:** subscription usage is subject to plan limits. On limit/quota errors the engine enters a **durable `usage_exhausted → waiting → retry` state** persisted in SQLite: queue pauses, UI banner shows wait state, automatic retry with backoff resumes work. No API-key fallback, no user decision.
- **Concurrency:** only LinkedIn *browser* actions are serialized (the supervisor queue). Generation has no LinkedIn constraint — LLM calls run with bounded concurrency (small worker pool) plus retry/backoff on transient failures.

### 4.2 Resume ingestion (`backend/resume/`)

1. Upload PDF or DOCX. Extract raw text (`pypdf`/`pdftotext`, `python-docx`). **If extraction is empty or weak (near-zero text, garbage ratio), automatic OCR fallback** (e.g. `ocrmypdf`/Tesseract) before giving up — scanned PDFs are handled, not hand-waved.
2. LLM structured extraction (medium effort) against `schema.py`: contact info, headline, experiences (company, title, dates, location, bullets), projects, education, skills, links.
3. **Truth ledger:** every extracted fact and bullet gets a **stable source ID** (`facts.py`). This is the ground truth all tailoring must cite.
4. Render into our LaTeX template → compile → PDF preview in UI.
5. **One confirmation gate, unlimited corrections inside it:** the user can edit any field inline as many times as they like (each edit re-renders the preview); the gate closes when they confirm. Corrections update the truth ledger.
6. Confirmed profile = **versioned base profile**. All tailoring starts from a specific profile version.

### 4.3 LinkedIn (`backend/sources/linkedin/`)

- **Browser ownership:** a single **persistent-context supervisor** owns one Playwright browser profile directory in `.data/` and **serializes every LinkedIn action** (login, search, detail fetches) through one queue — no concurrent profile locks, no separate storage_state files. **All LinkedIn automation runs headed** in that context (login and scraping alike — consistent fingerprint; the window may be minimized). The supervisor is the only code path that touches the browser.
- **Login:** supervisor opens the window; user signs in (2FA/checkpoint handled naturally by the user). Logged-in state detected; session reused across restarts; health check prompts re-login only when expired (gate 2, only when needed).
- **Search:** build search URL from keywords + location + filters (date posted, experience level, workplace type). Paginate, human-paced (randomized waits, scroll simulation, page cap per run). Collect job id, title, company, location, posted date, workplace type, easy-apply flag, URL. Dedupe on job id.
- **Detail fetch:** full description per job, paced, resumable, cached in SQLite by job id with `fetched_at` + content hash. **Automatic TTL** (default a few days): a stale job viewed/selected again gets silently re-fetched; edited postings are detected by hash change. Generated packages **pin the exact description snapshot** they were built from (see provenance). No prompt anywhere.
- **Risk posture (honest):** automated access violates LinkedIn's User Agreement regardless of pacing; pacing and daily caps reduce load and detection surface only. On captcha/checkpoint/restriction detection: **stop cleanly, persist state, show UI banner. Never attempt any bypass.** Account restriction is an accepted limitation of the product, stated plainly in the README.
- **Selector fragility:** all selectors in `selectors.py` with fallback chains; parsing is tested against **sanitized, minimal HTML fragments** in `backend/tests/fixtures/` — structure preserved, real company/member data stripped/faked before commit (live account used only for occasional smoke tests); scrape failures degrade per-job (skip + log), never crash the run.
- **Provider abstraction:** `sources/base.py` defines the small `JobSource` interface (search, fetch_detail, health). LinkedIn is the only v2 implementation, but nothing outside `sources/` imports LinkedIn directly.

### 4.4 Normalization + scoring (`backend/normalize/`, `backend/scoring/`)

Pipeline per job (fully automatic):

```
raw posting ──LLM (medium)──▶ normalized requirements ──deterministic──▶ score ──LLM (close matches only)──▶ explanation/rerank
                              (cached by JD hash)
```

1. **Normalization (`requirements.py`):** structured extraction from the raw description — required skills, preferred skills, minimum years of experience, education requirement, seniority, workplace type, compensation if stated. **Cached by job-description hash** — a JD never pays for extraction twice. **A hash change invalidates the cascade:** normalized requirements, score, explanation, and any unfinished package for that job are invalidated before generation proceeds (completed packages keep their pinned snapshot).
2. **Deterministic scorer = primary ranking (`score.py`):** ported and extended from v1 `tools/score_jobs.py` (its skill/experience/education matching logic is the baseline). Operates on normalized requirements vs. the structured profile: skill overlap, YoE vs. minimum (the **experience filter** — user's YoE + tolerance; jobs demanding more get flagged/filtered), education match, keyword include/exclude, posted-within window. Transparent, reproducible, free.
3. **LLM layer (medium effort):** explains scores (strengths/gaps/one-line pitch on card expand) and may **rerank close matches** (scores within a configurable band). It never invents the scale — the deterministic score is the backbone. **Stored separately:** `deterministic_score` and optional `llm_rerank` are distinct columns; the baseline is never overwritten.

Scoring runs automatically after scraping — no gate. Job list sortable by score/date, filterable.

### 4.5 Tailoring + generation (`backend/tailor/`)

Triggered by UI multi-select → "Generate packages". Per job, fully autonomous. Pipeline order guarantees nothing unvalidated ever ships:

```
generate structured resume + cover (JSON) → validate both → fit both
  → revalidate after every content rewrite → final compile + assertions → atomic publish
```

1. **Generation (high effort):** starts from the versioned base profile. One structured-output pass produces **JSON only — never raw LaTeX**:
   - tailored resume bullets: `{text, source_ids, relevance}` + headline + skills ordering
   - cover letter paragraphs: `{text, source_ids}` — same truth discipline as resume bullets
   - layout preferences as structured fields

   Prompt encodes the proven manual rules — use whitespace well (no lines with minimal words, fill lines, pleasing but effective use of space), keep general spacing decisions, tailor with judgment to be maximally attractive to hiring managers, ATS, and AI filters for *this* JD. Application code escapes all content and renders both LaTeX documents from templates — schema validation, safe compilation, reliable source tracking.
2. **Truth validation (`validate.py`) — both documents:** automated validator rejects new employers, dates, degrees, unsupported skills, or unsupported metrics. Source IDs give *traceability*; the validator additionally checks that each cited fact **actually supports the claim**. Implementation: deterministic exact checks (IDs exist, entities/dates/numbers match the ledger) combined with **one batched structured entailment pass covering both documents** — a single LLM call judging fact→claim support for every cited pair, not separate redundant QA prompts. Rejection → repair round (xhigh) → revalidate → still failing → package failed. **Source IDs are internal metadata only — never rendered into the PDFs.**
3. **Fit (`page_limit.py`) — both documents.** Resume ≤ N (user-set at profile setup, default 1); cover == 1. Geometry-aware algorithm:
   - Compile with the **actual selected engine** (the one recorded in provenance).
   - Measure page count **and content bounding boxes** from the PDF (coordinates, not line counts).
   - Over limit → **bounded layout parameters first**: margins, section spacing, item spacing — each with a defined **readability floor** it never crosses.
   - Still over → **relevance-aware content trimming**: drop/condense the *lowest-relevance* bullets for this JD (from generation-pass relevance), not the shortest ones.
   - **Recompile and re-measure after every change. Every content rewrite (trim/condense/repair, xhigh or max) goes back through truth validation before continuing** — page repairs can introduce unsupported claims, so the final output must pass validation *after* the last repair.
   - Escalation: repair rounds at xhigh; final failed-layout repair at max only if M4 evaluation proves benefit.
   - **Sparse-page balance:** last-page fullness measured from PDF coordinates; if sparse, one balance pass (also revalidated).
   - Note: v1's `generate_documents.py` shrink logic (drop shortest bullets, string-replace spacing values) is explicitly **not** ported — only its template/build/compile scaffolding is reused.
4. **Final compile + hard assertions:** resume ≤ N pages AND cover letter == 1 page AND truth validation passed after the last rewrite. Any assertion failing → package **failed** — never delivered oversized or unvalidated.
5. **Atomic publish:** artifacts written to a temp dir, then moved into `output/<company>_<jobid>/` (`resume.pdf`, `cover_letter.pdf`, `job.md` with pinned posting snapshot + link + score reasons) in one step; the package is marked `done` only after publish. Packages page lists them with preview + download + posting link. Failures are isolated — one failed package never blocks the batch.

### 4.6 Durability (`backend/core/db.py`)

- **Generation state machine, persisted per package:** `queued → generating → validating → fitting (with revalidation) → asserting → publishing → done | failed | waiting(usage_exhausted)`. Every transition written to SQLite.
- **App restart resumes queued/in-flight work and never regenerates completed packages.**
- **Artifact provenance recorded per package:** profile version, pinned job-description snapshot hash, prompt/schema version, model + effort used, LaTeX engine, retry count, final status. Reproducibility and debugging depend on this row.
- **Progress via SSE** (one-way stream; no WebSocket — nothing flows client→server mid-run).
- **Local privacy:** the server binds explicitly to `127.0.0.1` (never `0.0.0.0`); `.data/` (browser profile, SQLite DB) created with restrictive permissions (`0700`); README states plainly that resume content and job descriptions are sent to Claude (Anthropic) for processing.

---

## 5. What We Keep vs. Drop from v1

| v1 asset | Fate |
|---|---|
| `scrape_jobs.py` (hiring.cafe Playwright) | **Drop.** Pacing ideas inform `sources/linkedin/pacing.py`. |
| `parse_jobs.py` filters | **Port** into deterministic scorer inputs. |
| `score_jobs.py` | **Port as the primary ranking baseline** (skill/experience/education logic). LLM explains/reranks only. |
| `generate_documents.py` | **Split:** LaTeX template/build/compile scaffolding **ported**; page-shrink logic (shortest-bullet removal, spacing string-replace) **rewritten** as geometry-aware `page_limit.py`. |
| `llm_client.py` (multi-provider failover) | **Drop entirely** (Agent SDK subscription engine). |
| `agent.py` (ReAct), `run_pipeline.py`, `setup.py` CLI menus | **Drop.** |
| `generate_report.py` | **Drop** (UI replaces the report). |
| Cover letter LaTeX template | **Port.** |
| `config/*.yaml.example` | **Replace** with UI-driven settings in SQLite. |
| `CLAUDE.md` | **Rewrite** for v2 layout. |

Deletion happens on this branch; master keeps v1 intact until v2 merges.

---

## 6. Milestones

Each milestone independently runnable/testable; no milestone requires user decisions beyond the three gates.

- **M0 — Skeleton & engine.** Criteria: repo restructure; FastAPI + Vite scaffold served together (bound to `127.0.0.1`); `llm.py` with typed probe routing (fable→opus5 on not-entitled only; auth/quota/transient/config each routed distinctly), per-task effort tiers, verified tool lockdown (zero tool calls in test runs), JSON-only structured-output helper, durable usage_exhausted state; **fresh-clone subscription login proven end-to-end using the SDK's bundled CLI** — the exact login command/path exercised on a clean machine, not just instructions rendered; `setup.sh` detecting uv/Node/Playwright/LaTeX/Tesseract/OCRmyPDF (+ Ghostscript if required) with clear failure messages; README v2 draft including honest LinkedIn risk section and the resume-content-goes-to-Claude disclosure.
- **M1 — Resume ingestion.** Prerequisites: Tesseract + OCRmyPDF (+ Ghostscript if OCRmyPDF requires it) detected by `setup.sh`. Criteria: real resume PDF round-trips to a compiled 1-page PDF in our template with correct content and a populated truth ledger; scanned-PDF path exercises OCR fallback; inline corrections update ledger + preview.
- **M2 — LinkedIn session + search.** Criteria: login flow with persistent supervisor-owned session; a search **collects all available results up to the configured cap, deduplicates, caches, and resumes across restarts** without re-login; selector parsing passes against sanitized HTML fixtures (live account only for smoke); detail-cache TTL + hash-change re-fetch exercised.
- **M3 — Normalization, scoring + job UI.** Criteria: normalization cached by JD hash (hash change invalidates the dependent cascade); deterministic scores reproducible run-to-run; **scoring quality asserted against a small labeled fixture set** — exact skill match, skill aliases, false substring matches (e.g. "Java" vs "JavaScript"), YoE boundary cases, missing requirements, education matching — with expected ordering and filter behavior; `deterministic_score` stored separately from optional `llm_rerank`, baseline never overwritten; LLM explanations on demand; scored list renders < 2 s from cache; sort/filter/select work.
- **M4 — Tailoring pipeline.** Criteria: 5 selected jobs → 5 complete packages unattended; **restart-mid-batch test** — kill the app mid-generation, restart, batch completes with no duplicated work; 20-job stress batch asserts **truth validation passes, resume ≤ N, cover == 1, and failure isolation** (an induced failure affects only its package). Max-effort escalation evaluated here (kept only if it measurably helps).
- **M5 — Polish & generalization.** Criteria: onboarding UX; recovery banners (login expired, captcha stop, usage exhausted, claude not authenticated, invalid upload); settings page; README + screenshots; fresh-clone test on a clean machine (macOS and Linux; WSL documented).

---

## 7. Risks & Limitations

| Risk | Posture |
|---|---|
| LinkedIn ToS / account restriction | **Accepted limitation, not a mitigated problem.** Automated access violates the User Agreement; pacing/caps reduce load and detection surface only. Stop cleanly on captcha/restriction, never bypass, document plainly. `JobSource` interface keeps the provider swappable. |
| LinkedIn DOM changes | Central selectors with fallbacks; fixture-based parser tests; per-job graceful skip. |
| Fable not on user's plan | Automatic opus-5 fallback via probe; visible in settings. |
| Claude not authenticated / allowance exhausted | Setup card / durable waiting state with auto-retry. Never blocks on a user decision. |
| LaTeX install friction | Tectonic single-binary default; pdflatex autodetect; engine recorded per artifact (pagination differs between engines); compile errors surfaced with log tail. |
| Page-limit edge cases | Geometry-aware loop with readability floors + relevance-aware trimming + hard assertions; a package that can't meet N pages fails rather than ships oversized. |
| Fabrication in tailored output | Truth ledger + source-ID traceability + automated semantic-support validation on both documents, re-run after every content rewrite. IDs prove traceability; the validator's support check does the semantic work — stronger than a QA prompt, but validated, not assumed. |
| Scanned/exotic resumes | OCR fallback + unlimited inline corrections inside the confirmation gate; DOCX as the cleaner path. |
| Windows users | WSL path documented; native Windows out of scope for v2. |

---

## 8. Open Items (resolve during implementation, no user gate)

- Exact Agent SDK option names for model, effort, disallowed tools, and single-turn structured output — verify against the current SDK README at M0 start (do not guess; confirm `allowed_tools=[]` behavior specifically).
- Readability floor values for margins/spacing (pick from the current template's proven values; tune in M4).
- Sparse-page fullness threshold from bounding-box coverage (tune in M4).
- Whether to bundle a tiny sample resume for demo mode (nice for README GIF).
