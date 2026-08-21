#!/bin/bash
# Clean-environment fresh-clone run on macOS: everything the app needs is
# installed from upstream by the documented flow, inside a throwaway
# sandbox, with none of the development host's tooling visible.
#
# What is scrubbed:
#   * HOME            -> empty sandbox dir (no ~/.local/bin, no dotfiles)
#   * PATH            -> /usr/bin:/bin:/usr/sbin:/sbin only (no Homebrew,
#                        no pre-installed uv / node / tectonic / tesseract)
#   * uv cache        -> sandbox (Python toolchain downloaded fresh)
#   * npm cache       -> sandbox (frontend deps downloaded fresh)
#   * Playwright      -> sandbox (Chromium downloaded fresh)
#   * Node.js         -> fetched from nodejs.org into the sandbox, which is
#                        what setup.sh tells a user without Node to do
#
# What is NOT scrubbed, and therefore NOT proven: the macOS install itself.
# The kernel, system frameworks and Xcode command line tools (git, curl,
# make) are the host's. This is a clean ENVIRONMENT on an existing macOS
# install — it is not a pristine machine or an ephemeral CI runner.
#
# LaTeX and the OCR stack are deliberately absent here: setup.sh should
# warn about them and continue, since they are optional for boot.
#
# Usage: scripts/ci/macos_cleanenv_freshclone.sh [parent-dir]
# Port 8700 must be free — stop any running instance first.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd -P)"
PARENT="${1:-${TMPDIR:-/tmp}}"
[ -d "$PARENT" ] || { echo "parent dir does not exist: $PARENT"; exit 1; }
PARENT="$(cd "$PARENT" && pwd -P)"
SANDBOX="$(mktemp -d "$PARENT/job-agent-cleanenv.XXXXXX")"

echo "=== [0/6] sandbox"
echo "sandbox: $SANDBOX (left in place — nothing is ever deleted)"
mkdir -p "$SANDBOX/home" "$SANDBOX/work" "$SANDBOX/tools"

export HOME="$SANDBOX/home"
export TMPDIR="$SANDBOX/tmp"; mkdir -p "$TMPDIR"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export TERM=dumb
export UV_CACHE_DIR="$SANDBOX/home/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$SANDBOX/home/.local/share/uv/python"
export npm_config_cache="$SANDBOX/home/.npm"
export PLAYWRIGHT_BROWSERS_PATH="$SANDBOX/home/ms-playwright"
unset HOMEBREW_PREFIX HOMEBREW_CELLAR HOMEBREW_REPOSITORY 2>/dev/null || true

echo "PATH=$PATH"
echo "HOME=$HOME"
for tool in uv node npm tectonic pdflatex tesseract ocrmypdf; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "  UNEXPECTED: $tool visible at $(command -v "$tool")"
  else
    echo "  absent: $tool"
  fi
done

echo "=== [0b/6] Node.js from nodejs.org (setup.sh's instruction for a host without Node)"
ARCH=$(uname -m); case "$ARCH" in arm64) NODE_ARCH=arm64 ;; x86_64) NODE_ARCH=x64 ;;
  *) echo "unsupported arch $ARCH"; exit 1 ;; esac
NODE_TARBALL=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/ \
  | grep -o "node-v22\.[0-9.]*-darwin-${NODE_ARCH}\.tar\.gz" | head -1)
[ -n "$NODE_TARBALL" ] || { echo "could not resolve a Node 22 tarball"; exit 1; }
curl -fsSL "https://nodejs.org/dist/latest-v22.x/$NODE_TARBALL" \
  | tar -xz -C "$SANDBOX/tools"
export PATH="$SANDBOX/tools/${NODE_TARBALL%.tar.gz}/bin:$PATH"
echo "node $(node --version) / npm $(npm --version) (from $NODE_TARBALL)"

echo "=== [1/6] handing off to the fresh-clone test in this environment"
exec "$SRC/scripts/ci/macos_freshclone.sh" "$SANDBOX/work"
