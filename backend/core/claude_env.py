"""Environment hardening for every Claude CLI / Agent SDK subprocess.

Subscription-only means more than "no ANTHROPIC_API_KEY": auth tokens,
base-URL overrides, and Bedrock/Vertex/Foundry routing flags would all
silently switch billing away from the user's claude.ai login. Every code
path that spawns the CLI (SDK queries, auth login/status) must call
harden_process_env() first so the subprocess inherits none of them.

The app also runs the CLI against its own config dir (`.data/claude/`)
instead of the user's global `~/.claude`, so the user's Claude Code
settings, memory, and MCP servers never leak into this app's calls — and
the app's login lives inside the already-0700 `.data/`.
"""

import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
CLAUDE_CONFIG_DIR = DATA_DIR / "claude"

# Alternate credential / provider routing. Names from the Claude Code
# environment-variable reference (code.claude.com/docs/en/env-vars);
# anything here gets dropped from our process environment. The OAuth token
# variables are subscription credentials, but an inherited
# CLAUDE_CODE_OAUTH_TOKEN would take precedence over the config-stored
# login and defeat the isolated .data/claude/ identity.
FORBIDDEN_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
)


def harden_process_env() -> None:
    for var in FORBIDDEN_ENV:
        os.environ.pop(var, None)

    CLAUDE_CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.chmod(CLAUDE_CONFIG_DIR, 0o700)
    os.environ["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)

    # setting_sources=[] does not cover global config or auto-memory;
    # the dedicated config dir plus these flags does. Telemetry/update
    # traffic from the bundled CLI is switched off as well.
    os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
