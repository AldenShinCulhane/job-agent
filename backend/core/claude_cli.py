"""Run the Claude Code CLI bundled inside claude-agent-sdk.

The SDK ships the CLI, so users never install `claude` separately. The
app keeps its own login in `.data/claude/` (see claude_env.py):

    uv run python -m backend.core.claude_cli auth login
    uv run python -m backend.core.claude_cli auth status
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import claude_agent_sdk

from backend.core.claude_env import harden_process_env


def bundled_cli() -> Path:
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not path.is_file():
        raise SystemExit(
            "Bundled Claude Code CLI not found — reinstall with: uv sync --reinstall-package claude-agent-sdk"
        )
    return path


def auth_status() -> dict:
    """Parsed `claude auth status` (contains loggedIn / authMethod / email)."""
    harden_process_env()
    out = subprocess.run(
        [str(bundled_cli()), "auth", "status"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": (out.stdout + out.stderr).strip()[:500]}


def subscription_problem() -> str | None:
    """None when signed in with a claude.ai subscription; else what's wrong.

    Any other auth method (API key, external provider) is rejected — this
    app is subscription-only, and `loggedIn: true` alone doesn't prove that.
    """
    status = auth_status()
    if not status.get("loggedIn"):
        return "not signed in — run: uv run python -m backend.core.claude_cli auth login"
    method = status.get("authMethod")
    if method != "claude.ai":
        return (
            f"signed in via {method!r}, but this app only supports claude.ai "
            "subscription login — run: uv run python -m backend.core.claude_cli auth login"
        )
    return None


if __name__ == "__main__":
    harden_process_env()
    cli = bundled_cli()
    os.execv(str(cli), [str(cli), *sys.argv[1:]])
