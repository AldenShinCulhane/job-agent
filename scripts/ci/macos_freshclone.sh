#!/bin/bash
# Fresh-clone test on macOS (PLAN.md M5): clone -> setup.sh -> boot with the
# documented entrypoint -> API health -> real Playwright Chromium launch.
#
# Scope, stated up front: run directly, this clones onto the host and uses
# whatever toolchain the host already has (uv, Node, LaTeX, Playwright's
# browser cache). That is fresh-CHECKOUT evidence, not clean-machine
# evidence. For a from-nothing run — empty HOME, system-only PATH, no
# Homebrew, no shared caches, Node fetched from nodejs.org — use
# scripts/ci/macos_cleanenv_freshclone.sh, which sets that environment up
# and then calls this script.
#
# Usage: scripts/ci/macos_freshclone.sh [parent-dir]
#   parent-dir  existing directory to create the throwaway workdir under
#               (default: $TMPDIR). A uniquely named directory is created
#               with mktemp -d; nothing pre-existing is ever removed.
# Port 8700 must be free — stop any running instance first.
set -euo pipefail
export TERM=dumb  # keep terminal escapes out of retained logs
# setup.sh may install uv into ~/.local/bin; do what it tells the user to do
# so the boot step below can find a freshly installed uv.
export PATH="$HOME/.local/bin:$PATH"

SRC="$(cd "$(dirname "$0")/../.." && pwd -P)"
PARENT="${1:-${TMPDIR:-/tmp}}"
PORT=8700

echo "=== [1/5] preflight"
[ "$(uname -s)" = "Darwin" ] || { echo "not macOS"; exit 1; }
[ -d "$PARENT" ] || { echo "parent dir does not exist: $PARENT"; exit 1; }
PARENT="$(cd "$PARENT" && pwd -P)"

# The workdir is created fresh by mktemp, so it can never be an existing
# path the caller cares about. Refuse anyway if it would sit on top of the
# source checkout — this script must never be able to delete or overwrite
# the repository it is testing.
case "$SRC/" in
  "$PARENT"/*) echo "refusing: $PARENT contains the source checkout $SRC"; exit 1 ;;
esac
case "$PARENT/" in
  "$SRC"/*) echo "refusing: $PARENT is inside the source checkout $SRC"; exit 1 ;;
esac
[ "$PARENT" != "$SRC" ] || { echo "refusing: parent dir is the source checkout"; exit 1; }

WORK="$(mktemp -d "$PARENT/job-agent-freshclone.XXXXXX")"
CLONE="$WORK/job-agent"   # inside a directory mktemp just created: empty
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "port $PORT is in use — stop the running app first"; exit 1
fi
sw_vers | tr '\n' ' '; echo "arch=$(uname -m)"
echo "workdir: $WORK (left in place — this script never deletes anything)"

echo "=== [2/5] fresh clone + setup.sh"
git clone -q "$SRC" "$CLONE"
cd "$CLONE"
echo "provenance: cloned-from=$(git remote get-url origin)" \
     "ref=$(git rev-parse --abbrev-ref HEAD)" \
     "head=$(git rev-parse HEAD)" \
     "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
     "os=$(sw_vers -productName)-$(sw_vers -productVersion) arch=$(uname -m)" \
     "home=$HOME"
./scripts/setup.sh

echo "=== [3/5] boot with the documented entrypoint"
nohup uv run job-agent >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 && break
  sleep 1
done

echo "=== [4/5] health"
# Each check must fail loudly: an empty answer printed as if it were data
# would let a dead server look like a pass.
STATUS=$(curl -sf "http://127.0.0.1:$PORT/api/status") || {
  echo "FAIL: /api/status never answered"; echo "--- server log:"
  tail -20 "$WORK/server.log"; exit 1; }
echo "status:   $STATUS"
SETTINGS=$(curl -sf "http://127.0.0.1:$PORT/api/settings") || {
  echo "FAIL: /api/settings never answered"; exit 1; }
echo "settings: $SETTINGS"
HTML=$(curl -sf "http://127.0.0.1:$PORT/" | head -c 60) || {
  echo "FAIL: built UI not served"; exit 1; }
echo "$HTML ...HTML ok"
ls -ld .data | awk '{print ".data perms:", $1}'

echo "=== [5/5] real Chromium launch"
uv run python - <<'EOF'
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    page.goto("data:text/html,<title>chromium-ok</title>")
    assert page.title() == "chromium-ok"
    print("chromium launched, version:", browser.version)
    browser.close()
EOF

echo "=== MACOS FRESH-CLONE TEST PASS"
