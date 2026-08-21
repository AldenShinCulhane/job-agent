#!/bin/bash
# Clean-Ubuntu fresh-clone test (PLAN.md M5): clone -> setup.sh -> boot ->
# API health -> real Playwright Chromium launch. Runs inside ubuntu:24.04
# with the host repo mounted read-only at /src.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "=== [1/6] base packages"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg >/dev/null

echo "=== [2/6] Node 22 (NodeSource)"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
apt-get install -y -qq nodejs >/dev/null
node --version

echo "=== [3/6] uv + tectonic"
curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
export PATH="$HOME/.local/bin:$PATH"
uv --version
TECT_URL=$(curl -fsSL https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest \
  | grep -o 'https://[^"]*aarch64-unknown-linux-musl.tar.gz' | head -1 || true)
if [ -n "$TECT_URL" ]; then
  curl -fsSL "$TECT_URL" | tar -xz -C /usr/local/bin tectonic
  tectonic --version
else
  echo "tectonic asset not found for this arch — continuing; setup.sh will warn about LaTeX"
fi

echo "=== [4/6] fresh clone + setup.sh"
git clone -q /src /work/job-agent
cd /work/job-agent
./scripts/setup.sh

echo "=== [5/6] boot + health"
nohup uv run job-agent >/tmp/server.log 2>&1 &
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8700/api/status >/dev/null 2>&1 && break
  sleep 1
done
echo "status:   $(curl -sf http://127.0.0.1:8700/api/status)"
echo "settings: $(curl -sf http://127.0.0.1:8700/api/settings)"
curl -sf http://127.0.0.1:8700/ | head -c 60 && echo " ...HTML ok"
ls -ld .data | awk '{print ".data perms:", $1}'

echo "=== [6/6] real Chromium launch"
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

echo "=== UBUNTU FRESH-CLONE TEST PASS"
