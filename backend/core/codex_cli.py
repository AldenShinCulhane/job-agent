"""The Codex CLI, run for this app only (PLAN.md §4.1a).

Codex is optional and not bundled: it is found on PATH. The app keeps its
own Codex home inside `.data/codex/` (0700) with its own ChatGPT login,
never the user's global `~/.codex`, which injects the user's AGENTS.md and
memories into every prompt. Sign in once from the project folder:

    uv run python -m backend.core.codex_cli login
    uv run python -m backend.core.codex_cli status

Every spawn gets an allowlisted environment: nothing else from the
server's environment (API keys, base-URL overrides, proxies of other
tools) reaches the CLI, and ChatGPT sign-in is enforced by the CLI itself.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from backend.core.claude_env import DATA_DIR

CODEX_HOME = DATA_DIR / "codex"

# The only variables a Codex process ever sees, besides CODEX_HOME.
ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")

# Enforced by the CLI: an API-key login is refused outright.
FORCE_CHATGPT = 'forced_login_method="chatgpt"'

LOGIN_COMMAND = "uv run python -m backend.core.codex_cli login"
INSTALL_HINT = ("Codex CLI not found on PATH. Install it "
                "(https://developers.openai.com/codex/cli), then sign in: "
                + LOGIN_COMMAND)

_TIMEOUT_S = 30


def binary() -> Path | None:
    """The resolved Codex executable, or None when it is not installed."""
    found = shutil.which("codex")
    return Path(os.path.realpath(found)) if found else None


def home() -> Path:
    CODEX_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.chmod(CODEX_HOME, 0o700)
    return CODEX_HOME


def spawn_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env["CODEX_HOME"] = str(home())
    return env


def _run(args: list[str], timeout: float = _TIMEOUT_S) -> subprocess.CompletedProcess:
    exe = binary()
    if exe is None:
        raise FileNotFoundError(INSTALL_HINT)
    return subprocess.run([str(exe), *args], capture_output=True, text=True,
                          timeout=timeout, env=spawn_env(), stdin=subprocess.DEVNULL)


def login_problem() -> tuple[str, str] | None:
    """None when signed in to the app's Codex home with ChatGPT; else
    (kind, text): "setup" when Codex is missing or broken, "auth" when
    the user has to sign in."""
    if binary() is None:
        return ("setup", INSTALL_HINT)
    try:
        out = _run(["-c", FORCE_CHATGPT, "login", "status"])
    except subprocess.TimeoutExpired:
        return ("setup", "codex login status timed out")
    text = (out.stdout + out.stderr).strip()
    if "Logged in using ChatGPT" in text:
        return None
    if "API key" in text or "api key" in text:
        return ("auth", "signed in with an API key, but this app only supports "
                        "ChatGPT sign-in — run: " + LOGIN_COMMAND)
    return ("auth", "not signed in to Codex for this app — run: " + LOGIN_COMMAND)


def raw_catalog() -> list[dict]:
    """Codex's full model catalog (`codex debug models`), hidden entries
    included: a custom model id may name one."""
    out = _run(["debug", "models"], timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:500] or "codex debug models failed")
    data = json.loads(out.stdout)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("codex debug models returned no model list")
    return [m for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)]


def catalog_entry(model: str) -> dict | None:
    for entry in raw_catalog():
        if entry["slug"] == model:
            return entry
    return None


def models() -> list[dict]:
    """The listed (visible) Codex models for the engine picker, as {id,
    label, description, efforts}. Codex's delegating "ultra" level is never
    offered: it hands work to sub-agents."""
    catalog = []
    for entry in sorted(raw_catalog(), key=lambda m: m.get("priority", 999)):
        if entry.get("visibility") != "list":
            continue
        levels = entry.get("supported_reasoning_levels") or []
        efforts = [lvl.get("effort") for lvl in levels
                   if isinstance(lvl, dict) and lvl.get("effort") not in (None, "ultra")]
        catalog.append({
            "id": entry["slug"],
            "label": str(entry.get("display_name") or entry["slug"]),
            "description": str(entry.get("description") or ""),
            "efforts": efforts,
        })
    return catalog


if __name__ == "__main__":
    exe = binary()
    if exe is None:
        raise SystemExit(INSTALL_HINT)
    command = sys.argv[1:] or ["status"]
    if command[0] == "status":
        problem = login_problem()
        print("signed in with ChatGPT" if problem is None else problem[1])
        raise SystemExit(0 if problem is None else 1)
    if command[0] == "login":
        os.execve(str(exe), [str(exe), "-c", FORCE_CHATGPT, "login", *command[1:]],
                  spawn_env())
    raise SystemExit("usage: python -m backend.core.codex_cli [status|login]")
