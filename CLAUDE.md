# Agent Instructions

Local web app: upload resume → search LinkedIn → scored job list → user
selects → tailored resume + cover letter PDFs. **`PLAN.md` is the
authoritative spec (approved rev 4) — read it before changing behavior, and
don't re-litigate its locked decisions.**

## Layout

```
backend/    FastAPI app. core/ (llm, db, events) · api/ · sources/linkedin/
            · resume/ · normalize/ · scoring/ · tailor/ · tests/
frontend/   React + Vite + Tailwind (TypeScript)
scripts/    setup.sh (dependency detection/install)
output/     generated packages (gitignored)
.data/      SQLite DB, browser profile, probe cache (gitignored, 0700)
```

## Hard rules

- **LLM = Claude Agent SDK on subscription auth. Never add API keys or
  alternate providers.** All calls go through `backend/core/llm.py`:
  single-turn, `tools=[]` lockdown, JSON-schema output only. The LLM never
  emits raw LaTeX; app code renders all LaTeX from templates.
- **Exactly three human gates** (resume confirm, LinkedIn auth, job
  selection). Never add approval prompts; failures are `waiting` or
  `user_intervention_required` states.
- Server binds `127.0.0.1` only. Never bypass LinkedIn captchas or
  checkpoints — stop cleanly.
- Test fixtures must be sanitized — no real company/member data.
- Run `cd frontend && npx tsc -b --noEmit` before considering frontend
  changes done.

## Commands

```bash
uv run job-agent                          # run the app (:8700)
uv run python -m backend.tests.smoke_llm  # live engine smoke test (uses subscription)
uv run python -m backend.core.claude_cli auth status   # bundled-CLI auth check
cd frontend && npm run build              # rebuild UI served by FastAPI
```
