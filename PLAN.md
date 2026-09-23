# Job Agent v2 — LinkedIn-Native Tailoring Studio

**Status:** Approved for M0 (working spec, rev 4 — final planning revision;
rev 5 amends one M5 acceptance criterion only, owner-approved 2026-08-21 —
see M5 in §6; rev 6 adds a second, user-chosen subscription engine, Codex
on the user's ChatGPT plan, owner-approved 2026-09-22 — see §4.1a)
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
- **Subscription-only LLM.** No API keys, no per-token billing. The engine is the user's **Claude subscription** via the Claude Agent SDK, or (rev 6) their **ChatGPT subscription via the Codex CLI** when they pick a Codex model in Settings. Usage is subject to the user's plan limits — the app handles allowance exhaustion as a durable wait/retry state, never as a prompt or an API-key fallback. When a limit is hit, the app shows the engine picker with that limit, and its reset time when the provider reports one, so the user can switch models to keep going; if they don't, work resumes on its own at the reset. The app never switches engines by itself.

---

## 2. Locked Decisions

| Decision | Choice | Rationale |
|---|---|---|
| UI stack | **FastAPI backend + React (Vite) frontend** | Nice-looking, full control over job cards/selection/progress. One command starts both. |
| Job source | **LinkedIn, via Playwright + user's own login**, behind a `JobSource` interface | LinkedIn is where the user applies. **Risk is stated honestly:** LinkedIn's User Agreement prohibits scraping and automated access. Human pacing and the user's own session reduce *load and detection surface*, not ToS exposure. Account restriction is an **accepted product limitation** — documented in README, stop-cleanly-on-captcha, never any bypass attempt. The thin `JobSource` interface keeps the provider swappable if LinkedIn becomes untenable. |
| Resume ingestion | **Re-render into our LaTeX template** | Upload PDF/DOCX → structured extraction → rendered in the `resume_subheading2`-style template. Consistent spacing control makes page-limit enforcement reliable. Original visual design is not preserved (documented clearly). OCR fallback for scanned PDFs. |
| LLM engine | **Claude Agent SDK (Python), subscription auth** — no API keys. **Rev 6:** or the **Codex CLI on the user's ChatGPT subscription**, when the user chooses a Codex model (§4.1a) | The SDK bundles the Claude Code CLI, so a separate `claude` install is not required in the default path; setup only has to verify *authentication* (and offer login instructions if absent). All previous multi-provider/API-key code is removed. Codex is optional, not bundled, and detected on `PATH`. |
| Model | **Default: `claude-fable-5` if available on the user's plan, else `claude-opus-5`.** **Rev 6:** the user may instead choose any model either engine lists; one model runs every task | Probe at startup with a tiny request; cache the result; re-probe from settings. A chosen model is probed the same way before it is used. |
| Effort | **Tiered per task** (see §4.1) — medium for extraction/scoring, high for tailoring, xhigh for difficult repair, max only for final failed-layout repair | Blanket xhigh would burn subscription allowance and make 50-job runs slow for no quality gain on mechanical steps. |
| Application submission | **Stop at PDFs** | Lower account risk; wrong auto-submission is unrecoverable. |
| Legacy pipeline | **Dropped** (hiring.cafe scraper, CLI menu, ReAct agent, multi-provider `llm_client`; its automatic provider failover stays dropped under rev 6) | Single product surface. v1's deterministic scorer and LaTeX build code are **ported** (see §5); v1's page-shrink logic is **rewritten**, not ported. |
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

### 4.1a Codex engine (rev 6, owner-approved 2026-09-22)

Optional second engine on the user's ChatGPT subscription, through the
Codex CLI's non-interactive `codex exec`. Everything in §4.1 about
single-turn calls, JSON-only output, effort tiers, typed failure routing,
the durable usage wait and provenance applies to both engines. What
differs, or is tightened for both, follows:

- **Auth and isolation.** A dedicated `CODEX_HOME` inside `.data/`
  (0700) with its own ChatGPT login, never the user's global Codex home:
  the global one injects the user's AGENTS.md and memories into every
  prompt (verified 2026-09-22). `forced_login_method="chatgpt"`, and the
  spawn environment is scrubbed of `OPENAI_*` and `CODEX_*` except the
  app's own `CODEX_HOME`. API-key login is rejected like §4.1's
  non-claude.ai auth.
- **Tool lockdown.** Codex is an agent with shell, patch, web and image
  tools, and job postings are untrusted text, so this is the security
  boundary. **The guarantee is: no executable or side-effect-capable
  tool is ever offered to the model.** The one exception is
  `request_user_input`, which has no file, process or network access and
  which exec mode rejects. Every tool-bearing feature is disabled, web
  search is disabled, and the model runs from a custom catalog entry that
  drops the patch tool, the skills list and Codex's own system prompt in
  favour of the app's. The prompt carries no environment context.
  - *Prevention is the verified tool surface; detection is only a
    backstop.* A tool item seen in the event stream may already have run,
    so the run-time check never stands in for the pre-use verification.
  - *Pre-use verification.* One launch builder produces every Codex
    invocation, production and verification alike. Before a Codex
    configuration is used, the app runs that builder against a
    localhost-only capture endpoint and asserts the exact request it
    would send: the app's instructions, the user message only, and
    exactly the one allowed tool. A pass is bound to a fingerprint of the
    resolved executable (path and content hash), the effective launch
    arguments, the catalog entry and model metadata, and the environment
    allowlist. Any change to any of them requires a new pass. Anything
    unexpected fails closed as `config_error`, and Claude stays usable.
  - *Defence in depth at run time.* Read-only sandbox without network, an
    empty isolated working directory per call, ephemeral sessions, stdin
    closed, and a bounded wall-clock timeout that kills the process tree.
  - *Event classification.* Thread and turn lifecycle events, reasoning
    items and the answer item are normal. Turn failures and error events
    are routed by type (auth, quota, not-entitled, transient, config) as
    in §4.1, never as a breach. Any command, file-change, MCP, web-search
    or other tool item is a lockdown breach: the process is killed, the
    call fails as `config_error`, and nothing from it is used.
- **Structured output.** `--output-schema` with the task's schema,
  validated again app-side. Schemas must satisfy OpenAI's strict rules.
- **Effort.** The §4.1 tiers map onto the chosen model's supported
  reasoning levels, clamped; `max` stays disabled for both engines and
  Codex's delegating `ultra` level is never used.
- **One model, user-chosen.** Settings lists each engine's models (the
  Claude SDK's model list and `codex debug models`, visible entries only)
  plus a checked custom ID. The choice applies to every task. The
  fable→opus fallback of §4.1 applies only to the default, unchosen
  selection: a model the user chose explicitly never falls back silently,
  it reports not-entitled on that selection.
- **Execution identity is frozen before dispatch.** Every call captures
  an immutable context before it is sent: provider, model, effective
  effort, and the lockdown fingerprint for Codex. The result comes back
  with that context, and ledger entries, provenance, health updates and
  retry handling are written from it, never from the current global
  selection. A completion from a provider the user has since moved away
  from cannot change the new provider's badge, clear its wait, or be
  attributed to it.
- **Changing the selection mid-work.** Completed checkpoints are kept.
  Requests not yet sent use the new selection; requests already
  dispatched keep their original context and provenance. Switching never
  resets a package's repair allowance and never rebuilds cached results.
- **Auth freshness, both engines.** A backend monitor checks each
  engine's local credentials on a bounded, coalesced schedule that does
  not depend on any browser tab polling, and publishes an `engine`
  invalidation on change. A cheap pre-dispatch check runs before each
  call. `/api/status` only reads cached monitor state and stays
  responsive. Two facts are kept apart: credentials present locally, and
  service access last verified by a successful call. Healthy local
  credentials never clear a quota or configuration failure.
- **Usage limits: reset time versus retry estimate.** `next_retry_at` is
  the app's own backoff estimate. `limit_resets_at` is stored only when
  the provider reports it, and the UI says "unknown" otherwise. Limits
  are tracked per provider as account-wide; switching models within the
  same provider does not clear them. Each limit is one quota episode: the
  engine picker opens once per episode, never on countdown ticks or on
  concurrent failures from the same episode. It does not open over an
  editor that is dirty or busy, upload and save included; a banner links
  to it instead. Auto-resume still happens without the user visiting the
  picker.

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
2. **Truth validation (`validate.py`) — both documents:** automated validator rejects new employers, dates, degrees, unsupported skills, or unsupported metrics. Source IDs give *traceability*; the validator additionally checks that each cited fact **actually supports the claim**. Implementation: deterministic exact checks (IDs exist, entities/dates/numbers match the ledger) combined with **one batched structured entailment pass covering both documents** — a single LLM call judging fact→claim support for every cited pair, not separate redundant QA prompts. Rejection → up to two bounded repair rounds (xhigh, round count persisted in the package's call ledger) → revalidate after each → still failing → package failed. **Source IDs are internal metadata only — never rendered into the PDFs.**
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

### 4.5a Package attempts (rev 6)

- Package identity gains `attempt`. Existing rows become attempt 1 in
  one atomic migration, with IDs and download paths unchanged.
- Normal Generate stays idempotent: re-selecting a job returns its latest
  attempt for the same inputs.
- **Try again** on a failed package and **Regenerate** on a done one are
  explicit user actions, never automatic. Each creates a fresh row with
  its own stage state, validation cache and two-round repair ledger, on
  the current model selection. Old rows and their artifacts are
  immutable.
- Retry submissions are idempotent server-side: the client sends a
  request key that is stored unique, so repeated or concurrent
  submissions of one click create exactly one attempt. `MAX(attempt)+1`
  alone is not enough.
- If the profile, posting or templates have changed since the original,
  the new row is a package for the current inputs, labelled as such, and
  never presented as another attempt of the old identity.

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
- **M5 — Polish & generalization.** Criteria: onboarding UX; recovery banners (login expired, captcha stop, usage exhausted, claude not authenticated, invalid upload); settings page; README + screenshots; fresh-clone test with no hidden host state on both platforms — a clean *environment* on macOS (empty HOME, system-only PATH, no Homebrew) and a clean *container* on Linux (`ubuntu:24.04`). Both runs fetch uv, its managed Python, Node, the npm packages, Playwright's Chromium and Tectonic from upstream, then build, boot via the documented entrypoint, answer `/api/*`, launch Chromium, and run the backend test suite in that environment. **Rev 5 (owner-approved, 2026-08-21):** this criterion originally read "fresh-clone test on a clean machine (macOS and Linux)". Both runs are retained in `docs/ci-logs/`. Two limits are deliberate and must not be overstated: (a) pristine-hardware and ephemeral-runner testing are out of scope for a local personal tool — the macOS runs share the host's kernel, system frameworks and Xcode command line tools, and no pristine-image claim is made anywhere; (b) the OCR stack (Tesseract, OCRmyPDF, Ghostscript) is **not** installed in the macOS clean-environment run — it ships no official standalone macOS binary and is only needed for scanned image-only resumes — so that run is evidence about setup, boot and PDF generation, not about the OCR fallback path. WSL documented.

### v4 milestones (rev 6)

Each slice is reviewed on its own before the next starts.

- **P6a — Engine isolation, selection and health.** Criteria: provider
  interface with Claude and Codex adapters; the §4.1a launch builder,
  pre-use verification and fingerprint; adversarial mocked-response
  tests in which the model attempts shell, patch and
  `request_user_input` calls, each proving the call is rejected, nothing
  runs, no file changes, and the run ends without an interactive or
  unbounded loop; event classification tests covering lifecycle,
  reasoning, answer, typed errors and breaches; frozen execution context
  with the acceptance case "a request to model A is pending, the user
  selects B, then A succeeds or fails: its provenance stays A, and it
  changes neither B's badge nor B's wait"; explicitly chosen unavailable
  models do not fall back; the auth monitor works with every tab idle and
  the stream connected; `next_retry_at` and `limit_resets_at` are stored
  and shown separately; the picker opens once per quota episode and
  never over a dirty or busy editor; settings UI with both model lists
  and the custom ID; live smoke on each engine.
- **P6b — Package attempts.** Criteria: the §4.5a migration with a
  failure-injection rollback test, preserved IDs and downloads, duplicate
  and concurrent retry POSTs creating one attempt, a restart immediately
  after attempt creation, superseded inputs labelled as a new package,
  and Generate still idempotent.
- **P7 — UX batch one.** Criteria as proposed, plus: a fixture assertion
  that formatting-only description changes keep the existing canonical
  description hash, and untrusted descriptions rendered as text or
  sanitized markup, never raw HTML.
- **P8 — separate design pass before any code.** Duplicate grouping must
  keep original posting IDs, package references and application history.
  P6b's migration does not constrain P8's schema.

---

## 7. Risks & Limitations

| Risk | Posture |
|---|---|
| LinkedIn ToS / account restriction | **Accepted limitation, not a mitigated problem.** Automated access violates the User Agreement; pacing/caps reduce load and detection surface only. Stop cleanly on captcha/restriction, never bypass, document plainly. `JobSource` interface keeps the provider swappable. |
| LinkedIn DOM changes | Central selectors with fallbacks; fixture-based parser tests; per-job graceful skip. |
| Fable not on user's plan | Automatic opus-5 fallback via probe; visible in settings. |
| Codex CLI update changes its tools or flags (rev 6) | The pre-use request capture in §4.1a fails closed on any new tool, instruction or message, and the Codex engine reports `config_error` until the lockdown is re-verified. Claude stays available. |
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
