#!/bin/bash
# Fresh-clone test on macOS (PLAN.md M5): clone -> setup.sh -> boot with the
# documented entrypoint -> API health -> real Playwright Chromium launch.
#
# Scope, stated up front: this is a fresh CHECKOUT on the host, so it uses
# whatever toolchain the host already has (uv, Node, LaTeX, Playwright's
# browser cache). It proves a clean clone installs its own dependencies,
# builds, boots and runs on macOS — it does NOT prove a first-install of
# system dependencies on a bare machine. The Ubuntu container run
# (scripts/ci/ubuntu_freshclone.sh) is what covers the from-nothing path.
#
# Usage: scripts/ci/macos_freshclone.sh [workdir]
# Port 8700 must be free — stop any running instance first.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/job-agent-freshclone.XXXXXX)}"
CLONE="$WORK/job-agent"
PORT=8700

echo "=== [1/5] preflight"
[ "$(uname -s)" = "Darwin" ] || { echo "not macOS"; exit 1; }
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "port $PORT is in use — stop the running app first"; exit 1
fi
sw_vers | tr '\n' ' '; echo "arch=$(uname -m)"

echo "=== [2/5] fresh clone + setup.sh"
rm -rf "$CLONE"
git clone -q "$SRC" "$CLONE"
cd "$CLONE"
echo "provenance: cloned-from=$(git remote get-url origin)" \
     "ref=$(git rev-parse --abbrev-ref HEAD)" \
     "head=$(git rev-parse HEAD)" \
     "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
     "os=$(sw_vers -productName)-$(sw_vers -productVersion) arch=$(uname -m)"
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
echo "status:   $(curl -sf http://127.0.0.1:$PORT/api/status)"
echo "settings: $(curl -sf http://127.0.0.1:$PORT/api/settings)"
curl -sf "http://127.0.0.1:$PORT/" | head -c 60 && echo " ...HTML ok"
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
