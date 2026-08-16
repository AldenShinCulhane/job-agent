"""Run the Claude Code CLI bundled inside claude-agent-sdk.

The SDK ships the CLI, so users never install `claude` separately:

    uv run python -m backend.core.claude_cli auth login
    uv run python -m backend.core.claude_cli auth status
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import claude_agent_sdk


def bundled_cli() -> Path:
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not path.is_file():
        raise SystemExit(
            "Bundled Claude Code CLI not found — reinstall with: uv sync --reinstall-package claude-agent-sdk"
        )
    return path


def auth_status() -> dict:
    """Parsed `claude auth status` (contains loggedIn / authMethod / email)."""
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


if __name__ == "__main__":
    cli = bundled_cli()
    os.execv(str(cli), [str(cli), *sys.argv[1:]])
