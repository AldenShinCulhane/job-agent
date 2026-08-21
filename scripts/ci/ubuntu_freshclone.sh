#!/bin/bash
# Clean-Ubuntu fresh-clone test (PLAN.md M5): clone -> setup.sh (which
# test-compiles the resume template) -> boot with the documented entrypoint
# -> API health -> real Playwright Chromium launch -> the backend test suite,
# run in that container. Runs inside ubuntu:24.04 with the host repo mounted
# read-only at /src.
#
#   docker run --rm -v "$PWD":/src:ro -v "$PWD/scripts/ci":/scratch:ro \
#     ubuntu:24.04 bash /scratch/ubuntu_freshclone.sh
#
# Scope: the container is clean, the clone is fresh, and every dependency
# below is fetched inside it. It is a LOCAL clone of the host checkout on
# whatever architecture the container runs — not a remote clone, and not
# evidence for any other architecture.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
PORT=8700

echo "=== [1/8] base packages"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg >/dev/null

echo "=== [2/8] Node 22 (NodeSource)"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
apt-get install -y -qq nodejs >/dev/null
node --version

echo "=== [3/8] uv + tectonic"
curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
export PATH="$HOME/.local/bin:$PATH"
uv --version
ARCH_TAG="$(uname -m)-unknown-linux-musl"
TECT_URL=$(curl -fsSL https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest \
  | grep -o "https://[^\"]*${ARCH_TAG}.tar.gz" | head -1 || true)
if [ -n "$TECT_URL" ]; then
  curl -fsSL "$TECT_URL" | tar -xz -C /usr/local/bin tectonic
  tectonic --version
else
  echo "FAIL: no tectonic release asset for $ARCH_TAG — this run cannot prove PDF generation"
  exit 1
fi

echo "=== [4/8] fresh clone + setup.sh"
git clone -q /src /work/job-agent
cd /work/job-agent
echo "provenance: cloned-from=$(git remote get-url origin)" \
     "ref=$(git rev-parse --abbrev-ref HEAD)" \
     "head=$(git rev-parse HEAD)" \
     "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
     "os=$(. /etc/os-release && echo "$PRETTY_NAME") arch=$(uname -m)"
./scripts/setup.sh

echo "=== [5/8] boot with the documented entrypoint"
nohup uv run job-agent >/tmp/server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 && break
  sleep 1
done

echo "=== [6/8] health"
# Each check must fail loudly: an empty answer printed as if it were data
# would let a dead server look like a pass.
STATUS=$(curl -sf "http://127.0.0.1:$PORT/api/status") || {
  echo "FAIL: /api/status never answered"; echo "--- server log:"
  tail -20 /tmp/server.log; exit 1; }
echo "status:   $STATUS"
SETTINGS=$(curl -sf "http://127.0.0.1:$PORT/api/settings") || {
  echo "FAIL: /api/settings never answered"; exit 1; }
echo "settings: $SETTINGS"
case "$SETTINGS" in
  *'"latex_engines":["tectonic"'*) ;;
  *) echo "FAIL: tectonic is not the reported engine — $SETTINGS"; exit 1 ;;
esac
HTML=$(curl -sf "http://127.0.0.1:$PORT/" | head -c 60) || {
  echo "FAIL: built UI not served"; exit 1; }
echo "$HTML ...HTML ok"
ls -ld .data | awk '{print ".data perms:", $1}'

echo "=== [7/8] real Chromium launch"
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

echo "=== [8/8] backend test suite in this container"
uv run pytest backend/tests -q 2>&1 | tail -3 || {
  echo "FAIL: backend test suite did not pass in this container"; exit 1; }

echo "=== UBUNTU FRESH-CLONE TEST PASS"
