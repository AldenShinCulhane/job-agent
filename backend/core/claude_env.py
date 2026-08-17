"""Environment hardening for every Claude CLI / Agent SDK subprocess.

Subscription-only means more than "no ANTHROPIC_API_KEY": auth tokens,
base-URL overrides, provider selectors (Bedrock/Vertex/Foundry/Mantle),
profile/federation credentials, and OAuth token variables all outrank or
bypass the config-stored /login. A name-by-name denylist drifts behind
CLI releases, so the scrub is policy-based: entire namespaces are
stripped, then only app-controlled variables are set. Every code path
that spawns the CLI (SDK queries, auth login/status) must call
harden_process_env() first.

The app also runs the CLI against its own config dir (`.data/claude/`)
instead of the user's global `~/.claude`, so the user's Claude Code
settings, memory, and MCP servers never leak into this app's calls — and
the app's login lives inside the already-0700 `.data/`.
"""

import os
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
CLAUDE_CONFIG_DIR = DATA_DIR / "claude"

FORBIDDEN_PREFIXES = (
    # Keys, tokens, endpoints, profile/federation/org overrides — every
    # documented ANTHROPIC_* variable either overrides auth/routing or is
    # config this app sets explicitly through the SDK instead.
    "ANTHROPIC_",
    # OAuth material that outranks the config-stored login identity.
    "CLAUDE_CODE_OAUTH_",
    # External provider selectors (Bedrock/Vertex/Foundry/Mantle/AWS).
    "CLAUDE_CODE_USE_",
    # Provider auth-skip flags.
    "CLAUDE_CODE_SKIP_",
)
FORBIDDEN_EXACT = ("AWS_BEARER_TOKEN_BEDROCK",)


def is_forbidden(var: str) -> bool:
    return var in FORBIDDEN_EXACT or var.startswith(FORBIDDEN_PREFIXES)


def harden_process_env() -> None:
    for var in list(os.environ):
        if is_forbidden(var):
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
