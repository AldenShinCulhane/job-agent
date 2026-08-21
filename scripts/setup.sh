#!/usr/bin/env bash
# Job Agent setup — macOS and Linux. Windows users: run inside WSL.
#
# Needs internet access: downloads Python packages, npm packages, the uv
# installer if missing, and Playwright's Chromium build.
set -u

BOLD=$(tput bold 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
ok()   { printf "  [ok]   %s\n" "$1"; }
warn() { printf "  [warn] %s\n" "$1"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf "  [FAIL] %s\n" "$1"; FAILURES=$((FAILURES + 1)); }
WARNINGS=0; FAILURES=0

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
export PATH="$HOME/.local/bin:$PATH"

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "Unsupported platform: $(uname -s). On Windows, run this inside WSL (see README)."; exit 1 ;;
esac

echo "${BOLD}Job Agent setup${RESET}"
echo
echo "1. Core toolchain"

# --- uv (installed automatically — it manages the Python side) -------------
UV_JUST_INSTALLED=0
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version | awk '{print $2}')"
else
  echo "  installing uv (https://astral.sh/uv) ..."
  if curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 && command -v uv >/dev/null 2>&1; then
    ok "uv installed to ~/.local/bin"
    UV_JUST_INSTALLED=1
  else
    fail "uv install failed — install manually: curl -LsSf https://astral.sh/uv/install.sh | sh"
  fi
fi

# --- Node / npm (frontend build) -------------------------------------------
# Vite requires ^20.19 or >=22.12 — a bare "Node 20" is not enough.
if command -v npm >/dev/null 2>&1; then
  NODE_V=$(node --version | sed 's/^v//')
  NODE_MAJOR=${NODE_V%%.*}
  NODE_MINOR=$(printf '%s' "$NODE_V" | cut -d. -f2)
  if { [ "$NODE_MAJOR" -eq 20 ] && [ "$NODE_MINOR" -ge 19 ]; } \
     || { [ "$NODE_MAJOR" -eq 22 ] && [ "$NODE_MINOR" -ge 12 ]; } \
     || [ "$NODE_MAJOR" -ge 23 ]; then
    ok "node v$NODE_V / npm $(npm --version)"
  else
    fail "node v$NODE_V is too old for the frontend build — need ^20.19 or >=22.12: macOS: brew install node | Ubuntu/Debian: https://nodejs.org or NodeSource | nvm install 22"
  fi
else
  fail "Node.js not found — install Node 20.19+ or 22.12+: macOS: brew install node | Ubuntu/Debian: https://nodejs.org or NodeSource | nvm install 22"
fi

# --- LaTeX engine (PDF generation) -----------------------------------------
if command -v tectonic >/dev/null 2>&1; then
  ok "tectonic $(tectonic --version 2>/dev/null | head -1 | awk '{print $2}')"
elif command -v pdflatex >/dev/null 2>&1; then
  ok "pdflatex found (tectonic preferred but not required)"
else
  warn "no LaTeX engine — needed for PDF generation. Install tectonic: macOS: brew install tectonic | Linux: https://tectonic-typesetting.github.io/install.html (or a TeX Live pdflatex)"
fi

# --- OCR stack (scanned-resume fallback) -----------------------------------
if command -v tesseract >/dev/null 2>&1; then
  ok "tesseract $(tesseract --version 2>&1 | head -1 | awk '{print $2}')"
else
  warn "tesseract not found — needed only for scanned (image-based) resume PDFs. macOS: brew install tesseract | Ubuntu/Debian: sudo apt install tesseract-ocr"
fi
if command -v ocrmypdf >/dev/null 2>&1; then
  ok "ocrmypdf $(ocrmypdf --version 2>/dev/null)"
else
  warn "ocrmypdf not found — needed only for scanned resume PDFs. macOS: brew install ocrmypdf | Ubuntu/Debian: sudo apt install ocrmypdf"
fi
if command -v gs >/dev/null 2>&1; then
  ok "ghostscript $(gs --version 2>/dev/null)"
else
  warn "ghostscript not found — required by ocrmypdf. macOS: brew install ghostscript | Ubuntu/Debian: sudo apt install ghostscript"
fi

if [ "$FAILURES" -gt 0 ]; then
  echo
  echo "${BOLD}Setup blocked${RESET} — fix the [FAIL] items above and re-run."
  exit 1
fi

echo
echo "2. Project dependencies"

if uv sync >/dev/null 2>&1; then
  ok "Python dependencies (uv sync)"
else
  fail "uv sync failed — re-run 'uv sync' manually to see the error"
fi

if (cd frontend && npm ci --no-fund --no-audit >/dev/null 2>&1); then
  ok "frontend dependencies (npm ci)"
else
  fail "npm ci failed — re-run 'cd frontend && npm ci' to see the error"
fi

if (cd frontend && npm run build >/dev/null 2>&1); then
  ok "frontend built (frontend/dist)"
else
  fail "frontend build failed — re-run 'cd frontend && npm run build' to see the error"
fi

# Playwright's Chromium (LinkedIn browser). The playwright package arrives
# with a later milestone; skip quietly until it is a dependency.
if uv run python -c "import playwright" >/dev/null 2>&1; then
  if uv run playwright install chromium >/dev/null 2>&1; then
    ok "Playwright Chromium"
  else
    fail "Playwright browser install failed — re-run 'uv run playwright install chromium'"
  fi
  # Linux needs Chromium's system libraries too (the download alone
  # won't launch without them). install-deps uses apt and may need root.
  if [ "$(uname -s)" = "Linux" ]; then
    if uv run playwright install-deps chromium >/dev/null 2>&1; then
      ok "Chromium system dependencies"
    else
      warn "Chromium system libraries missing — run: sudo env \"PATH=\$PATH\" \$(command -v uv) run playwright install-deps chromium"
    fi
  fi
fi

echo
echo "3. Claude sign-in (uses your Claude subscription — no API key)"

# loggedIn alone is not enough — API-key or external-provider auth must not
# pass as subscription auth.
PROBLEM=$(uv run python -c "from backend.core.claude_cli import subscription_problem; print(subscription_problem() or '')" 2>/dev/null || echo "auth check failed — run 'uv sync' and retry")
if [ -z "$PROBLEM" ]; then
  ok "signed in with a claude.ai subscription"
else
  warn "$PROBLEM"
fi

echo
if [ "$FAILURES" -gt 0 ]; then
  echo "${BOLD}Setup finished with failures${RESET} — fix the [FAIL] items and re-run."
  exit 1
fi
if [ "$WARNINGS" -gt 0 ]; then
  echo "${BOLD}Setup finished with $WARNINGS warning(s)${RESET} — optional features affected (details above)."
else
  echo "${BOLD}Setup complete.${RESET}"
fi
if [ "$UV_JUST_INSTALLED" -eq 1 ]; then
  echo "uv was installed into ~/.local/bin, which THIS shell does not know about yet."
  echo "Run this first (or just open a new terminal):"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
echo "Start the app with: uv run job-agent   (then open http://127.0.0.1:8700)"
