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

- **LLM = the user's own subscription, never an API key: Claude (Claude
  Agent SDK, claude.ai login) or Codex (Codex CLI, ChatGPT login).** The
  user picks one model for every task in Settings; the app never switches
  engines by itself. Never add API keys, per-token billing, or any other
  provider. All calls go through `backend/core/llm.py`: single-turn,
  zero-tool lockdown, JSON-schema output only (PLAN.md §4.1, §4.1a). The
  LLM never emits raw LaTeX; app code renders all LaTeX from templates.
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
uv run python -m backend.core.codex_cli status         # app's own Codex sign-in (ChatGPT only)
CODEX_TESTS_REQUIRED=1 uv run pytest backend/tests/test_codex_engine.py  # Codex lockdown, local stand-ins only
cd frontend && npm run build              # rebuild UI served by FastAPI
```
