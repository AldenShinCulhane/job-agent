"""Claude Agent SDK engine — subscription auth only, no API keys.

Design (PLAN.md §4.1):
- Model probe with typed failure routing: fable -> opus5 only when the plan
  lacks fable; auth problems surface as user_intervention_required; usage
  limits become a durable waiting state; transient errors retry (bounded,
  with jitter); invalid configuration fails loudly.
- Per-task effort tiers — never blanket xhigh. "max" stays disabled in code
  until the M4 evaluation proves it helps.
- Tool and prompt isolation: every call is a single prompt (no open-ended
  agent loop), JSON-schema structured output, with the entire Claude Code
  toolset, all MCP servers, and the user's global Claude state removed
  (tools=[], strict_mcp_config, dedicated CLAUDE_CONFIG_DIR — see
  claude_env.py). A real tool call in a response is treated as a bug.
"""

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ResultMessage,
    ToolUseBlock,
    query,
)

from backend.core import claude_cli, db
from backend.core.claude_env import harden_process_env

MODEL_PREFERRED = "claude-fable-5"
MODEL_FALLBACK = "claude-opus-5"

# PLAN.md §4.1 effort tiers.
TASK_EFFORT: dict[str, str] = {
    "extract": "medium",
    "normalize": "medium",
    "score_explain": "medium",
    "entail": "medium",  # batched fact→claim support judgments (validation)
    "tailor": "high",
    "repair": "xhigh",
    "layout_final": "max",
}

# Flipped only if the M4 evaluation proves max effort helps final layout
# repair (PLAN.md §4.1); until then "max" is capped to "xhigh" in code.
ALLOW_MAX_EFFORT = False

# Bounded concurrency for LLM calls (browser actions are serialized
# elsewhere; generation is not).
MAX_CONCURRENT_CALLS = 3
_semaphore: asyncio.Semaphore | None = None

_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 2.0

harden_process_env()


class EngineError(Exception):
    """Base for typed engine failures."""


class AuthRequired(EngineError):
    """No usable claude.ai login — user_intervention_required."""


class UsageExhausted(EngineError):
    """Subscription allowance hit its limit; wait and retry."""


class ModelNotEntitled(EngineError):
    """The probed model is not available on the user's plan."""


class TransientFailure(EngineError):
    """Network / server hiccup; retried with backoff before propagating."""


class ConfigError(EngineError):
    """Invalid engine configuration — fail loudly, do not retry."""


def _classify(error_text: str) -> type[EngineError]:
    text = error_text.lower()
    if any(s in text for s in ("api key", "login", "authenticat", "oauth", "unauthorized", "credential")):
        return AuthRequired
    if any(s in text for s in ("usage limit", "rate limit", "limit reached", "429", "quota", "out of extra usage")):
        return UsageExhausted
    if any(s in text for s in ("not_found", "model not found", "does not exist", "invalid model", "no access to", "not available on")):
        return ModelNotEntitled
    if any(s in text for s in ("unknown option", "unrecognized option", "invalid option", "unsupported option")):
        return ConfigError
    return TransientFailure


def effective_effort(task: str) -> str:
    """The effort tier a task actually runs at (public: recorded in
    package provenance), with the max→xhigh cap applied."""
    effort = TASK_EFFORT[task]
    if effort == "max" and not ALLOW_MAX_EFFORT:
        return "xhigh"
    return effort


def _options(model: str, effort: str, system_prompt: str, schema: dict | None) -> ClaudeAgentOptions:
    kwargs: dict[str, Any] = dict(
        model=model,
        effort=effort,
        system_prompt=system_prompt,
        # Structured output is delivered via a synthetic StructuredOutput
        # tool call, which consumes a turn — schema calls need one extra.
        max_turns=2 if schema is not None else 1,
        # Lockdown: tools=[] removes every built-in tool definition from the
        # model's context while leaving the SDK's synthetic StructuredOutput
        # delivery channel intact. (disallowed_tools=["*"] would deny
        # StructuredOutput too and break output_format; allowed_tools=[]
        # alone restricts nothing.) dontAsk auto-denies anything that
        # somehow falls through, and no MCP server is ever attached.
        tools=[],
        permission_mode="dontAsk",
        mcp_servers={},
        strict_mcp_config=True,
        # Isolation: never load the user's Claude Code settings or CLAUDE.md
        # into this app's prompts (global state is walled off by the
        # dedicated CLAUDE_CONFIG_DIR in claude_env.py).
        setting_sources=[],
    )
    if schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": schema}
    return ClaudeAgentOptions(**kwargs)


async def _run_query(prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
    """One locked-down query. Raises typed EngineError on failure."""
    result: ResultMessage | None = None
    violations: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    # "StructuredOutput" is the SDK's own delivery channel
                    # for output_format — a synthetic call with no side
                    # effects, not a real tool.
                    if isinstance(block, ToolUseBlock) and block.name != "StructuredOutput":
                        violations.append(block.name)
            if isinstance(message, ResultMessage):
                result = message
    except (CLINotFoundError, CLIJSONDecodeError) as exc:
        # Missing/broken bundled CLI or protocol garbage: retrying cannot
        # help, and silent retries would mask an install or SDK bug.
        raise ConfigError(str(exc)) from exc
    except CLIConnectionError as exc:
        raise TransientFailure(str(exc)) from exc
    except Exception as exc:  # ProcessError and friends -> classify by text
        # A logged-out CLI yields an error ResultMessage (subtype "success",
        # is_error=True, the real signal in .result — e.g. "Not logged in ·
        # Please run /login") and then the SDK raises a generic exception
        # whose text is useless. Classify from the captured result text.
        detail = str(result.result) if result is not None and result.is_error else str(exc)
        raise _classify(detail)(detail) from exc

    # Raised outside the loop: raising mid-iteration aborts the SDK's
    # async generator uncleanly.
    if violations:
        raise ConfigError(f"tool lockdown breached: model called {violations!r}")
    if result is None:
        raise TransientFailure("query ended without a result message")
    if result.is_error:
        detail = str(result.result or result.subtype)
        raise _classify(detail)(detail)
    if result.subtype == "success":
        return result
    if result.subtype == "error_max_structured_output_retries":
        raise TransientFailure("structured output failed schema validation repeatedly")
    raise _classify(result.subtype + " " + str(getattr(result, "result", "")))(
        f"query failed: {result.subtype}"
    )


async def _run_with_retry(prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
    """Bounded retry with jittered exponential backoff, transient errors only."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await _run_query(prompt, options)
        except TransientFailure:
            if attempt == _RETRY_ATTEMPTS - 1:
                raise
            delay = _RETRY_BASE_SECONDS * (2**attempt) * (0.5 + random.random())
            await asyncio.sleep(delay)
    raise TransientFailure("retry loop fell through")  # unreachable


# --- Durable engine state (survives restarts) -------------------------------

_BACKOFF_START_MIN = 5
_BACKOFF_CAP_MIN = 60


def _enter_usage_exhausted() -> None:
    minutes = int(db.get_state("usage_backoff_minutes", str(_BACKOFF_START_MIN)))
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.set_state("engine_status", "waiting_usage_exhausted")
    db.set_state("retry_at", retry_at.isoformat())
    db.set_state("usage_backoff_minutes", str(min(minutes * 2, _BACKOFF_CAP_MIN)))


def _clear_wait_state() -> None:
    """Success recovery: a stale last_error must not survive an ok status."""
    db.set_state("engine_status", "ok")
    db.delete_state("retry_at")
    db.delete_state("usage_backoff_minutes")
    db.delete_state("last_error")


def seconds_until_retry() -> float:
    """0 when the engine may run now; positive while waiting out a limit."""
    retry_at = db.get_state("retry_at")
    if retry_at is None:
        return 0.0
    remaining = (datetime.fromisoformat(retry_at) - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, remaining)


def _require_not_waiting() -> None:
    """The wait state is enforced here, not just recorded."""
    wait = seconds_until_retry()
    if wait > 0:
        raise UsageExhausted(f"usage limit cooldown: retry in {int(wait)}s")


def engine_status() -> dict:
    return {
        "status": db.get_state("engine_status", "unprobed"),
        "model": db.get_state("pinned_model"),
        "retry_in_seconds": seconds_until_retry() or None,
        "last_error": db.get_state("last_error"),
    }


# --- Public API -------------------------------------------------------------

async def probe() -> str:
    """Resolve and pin the model. Returns the pinned model id.

    Typed routing per PLAN.md: only ModelNotEntitled falls back to opus-5;
    every other failure class propagates for the caller/UI to handle.
    """
    pinned = db.get_state("pinned_model")
    if pinned:
        return pinned

    _require_not_waiting()

    # loggedIn alone is not enough — reject API-key / external-provider auth
    # before spending a request.
    problem = await asyncio.to_thread(claude_cli.subscription_problem)
    if problem is not None:
        db.set_state("engine_status", "auth_required")
        db.set_state("last_error", problem)
        raise AuthRequired(problem)

    for model in (MODEL_PREFERRED, MODEL_FALLBACK):
        options = _options(model, "low", "Reply with the single word: ok", schema=None)
        try:
            await _run_with_retry("Say ok.", options)
        except ModelNotEntitled as exc:
            if model == MODEL_FALLBACK:
                db.set_state("last_error", str(exc))
                raise
            continue
        except AuthRequired as exc:
            db.set_state("engine_status", "auth_required")
            db.set_state("last_error", str(exc))
            raise
        except UsageExhausted as exc:
            _enter_usage_exhausted()
            db.set_state("last_error", str(exc))
            raise
        except ConfigError as exc:
            db.set_state("engine_status", "config_error")
            db.set_state("last_error", str(exc))
            raise
        db.set_state("pinned_model", model)
        _clear_wait_state()
        return model

    raise ConfigError("probe fell through without pinning a model")  # unreachable


def reset_probe() -> None:
    """Settings-page hook: forget the pinned model and re-probe next call."""
    db.delete_state("pinned_model")
    db.delete_state("last_error")
    db.set_state("engine_status", "unprobed")


async def structured(
    prompt: str,
    schema: dict,
    task: str,
    system_prompt: str = "You are the reasoning engine inside a job-application assistant. Answer only in the requested JSON structure.",
) -> dict:
    """Single-prompt, zero-tool, JSON-schema-validated call.

    `task` selects the effort tier (see TASK_EFFORT). On usage limits the
    durable waiting state is recorded before UsageExhausted propagates —
    and enforced: while the cooldown runs, calls raise without spending
    a request.
    """
    global _semaphore
    if task not in TASK_EFFORT:
        raise ConfigError(f"unknown task tier {task!r}")
    _require_not_waiting()
    model = await probe()
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

    options = _options(model, effective_effort(task), system_prompt, schema)
    async with _semaphore:
        try:
            result = await _run_with_retry(prompt, options)
        except UsageExhausted:
            _enter_usage_exhausted()
            raise
        except AuthRequired as exc:
            db.set_state("engine_status", "auth_required")
            db.set_state("last_error", str(exc))
            raise
        except ConfigError as exc:
            # Persisted for /api/status even when the model was already
            # pinned — a lockdown breach or broken CLI must not leave the
            # engine reporting ok.
            db.set_state("engine_status", "config_error")
            db.set_state("last_error", str(exc))
            raise

    _clear_wait_state()
    output = getattr(result, "structured_output", None)
    if output is None:
        raise TransientFailure("success result carried no structured output")
    return output
