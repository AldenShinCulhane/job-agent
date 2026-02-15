"""
LLM client with automatic provider failover.

Supports 4 free OpenAI-compatible providers. The user adds API keys for
one or more to .env. On each call, the client picks the best available
provider and automatically fails over to the next one if rate limited.

Providers (priority order):
  1. SambaNova  — unlimited free tokens, 20 RPM
  2. Cerebras   — 1M tokens/day free, 30 RPM
  3. Groq       — Llama 3.3 70B, 12K TPM
  4. Gemini     — Gemini 2.5 Flash, 10 RPM, 250 RPD
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "sambanova": {
        "key_env": "SAMBANOVA_API_KEY",
        "base_url": "https://api.sambanova.ai/v1",
        "model": "Meta-Llama-3.3-70B-Instruct",
        "delay": 3,
        "display_name": "SambaNova",
    },
    "cerebras": {
        "key_env": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
        "delay": 3,
        "display_name": "Cerebras",
    },
    "groq": {
        "key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "delay": 7,
        "display_name": "Groq",
    },
    "gemini": {
        "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
        "delay": 7,
        "display_name": "Gemini",
    },
}

# Failover priority — tried in this order
_PRIORITY_ORDER = ["sambanova", "cerebras", "groq", "gemini"]

# Cooldown seconds when rate limited (if no retry-after header)
_DEFAULT_COOLDOWN = {
    "sambanova": 30,
    "cerebras": 30,
    "groq": 60,
    "gemini": 60,
}

# How many times to retry the same provider before failing over
_RETRIES_BEFORE_FAILOVER = 2

# Max total wait time (seconds) across all providers before giving up
_MAX_WAIT_TOTAL = 300

# Tracks when each provider can be retried (provider -> available_at timestamp)
_provider_cooldowns: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMConfigError(Exception):
    """Raised when no LLM provider is reachable or configured."""
    pass


# ---------------------------------------------------------------------------
# Provider selection helpers
# ---------------------------------------------------------------------------

def _get_available_providers() -> list[str]:
    """Return providers that have API keys set in .env, in priority order."""
    available = []
    for name in _PRIORITY_ORDER:
        key = os.getenv(_PROVIDERS[name]["key_env"], "").strip()
        if key:
            available.append(name)
    return available


def _is_provider_available(name: str) -> bool:
    """Check if a provider is past its cooldown period."""
    return time.time() >= _provider_cooldowns.get(name, 0)


def _set_provider_cooldown(name: str, seconds: float):
    """Mark a provider as rate-limited for the given duration."""
    _provider_cooldowns[name] = time.time() + seconds
    display = _PROVIDERS[name]["display_name"]
    print(f"    [{display}] Rate limited — cooling down for {seconds:.0f}s")


def _next_available_provider(exclude: str | None = None) -> tuple[str, float] | None:
    """Pick the best provider to try next.

    Returns (provider_name, wait_seconds) or None if no providers have keys.
    wait_seconds is 0 if the provider is immediately available.
    """
    available = _get_available_providers()
    if not available:
        return None

    # Prefer providers other than the one that just failed
    if exclude and exclude in available:
        others = [p for p in available if p != exclude]
        ordered = others + [exclude]
    else:
        ordered = available

    # First pass: find one not in cooldown
    for name in ordered:
        if _is_provider_available(name):
            return (name, 0.0)

    # Second pass: find the one with the shortest remaining cooldown
    soonest = min(ordered, key=lambda n: _provider_cooldowns.get(n, 0))
    wait = max(0.0, _provider_cooldowns[soonest] - time.time())
    return (soonest, wait)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def check_llm() -> bool:
    """Check if at least one provider has an API key configured."""
    return len(_get_available_providers()) > 0


def get_provider_name() -> str:
    """Return display names of available providers in priority order."""
    available = _get_available_providers()
    if not available:
        return "None configured"
    names = [_PROVIDERS[p]["display_name"] for p in available]
    return " > ".join(names)


def get_expected_key_name() -> str:
    """Return all provider env var names (for error messages)."""
    return ", ".join(_PROVIDERS[p]["key_env"] for p in _PRIORITY_ORDER)


def get_call_delay() -> float:
    """Return the delay for the highest-priority available provider."""
    available = _get_available_providers()
    if available:
        return _PROVIDERS[available[0]]["delay"]
    return 5


def provider_status() -> str:
    """Return a formatted status of all providers (for pre-flight display)."""
    lines = ["  LLM providers (failover order):"]
    for name in _PRIORITY_ORDER:
        config = _PROVIDERS[name]
        has_key = bool(os.getenv(config["key_env"], "").strip())
        if has_key:
            status = "ready"
        else:
            status = "no key"
        lines.append(f"    {config['display_name']:12s} [{status}]")
    available = _get_available_providers()
    if not available:
        lines.append("    WARNING: No API keys configured!")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core failover engine
# ---------------------------------------------------------------------------

def _call_with_failover(make_request, task_label: str = "") -> str:
    """Execute an LLM request with automatic provider failover.

    Args:
        make_request: Callable(client, model) -> response content string.
                      Should raise RateLimitError/APIError/APIConnectionError on failure.
        task_label: Description for logging (e.g., "resume for TestCorp").

    Returns:
        The model's text response.

    Raises:
        LLMConfigError if all providers are exhausted or no keys are configured.
    """
    from openai import OpenAI, RateLimitError, APIError, APIConnectionError

    available = _get_available_providers()
    if not available:
        raise LLMConfigError(
            "No LLM API keys configured. Add at least one to your .env file:\n"
            + "\n".join(f"  {_PROVIDERS[p]['key_env']}" for p in _PRIORITY_ORDER)
            + "\n\nGet free keys at:"
            + "\n  SambaNova (recommended): https://cloud.sambanova.ai/apis"
            + "\n  Cerebras: https://cloud.cerebras.ai/"
            + "\n  Groq: https://console.groq.com/"
            + "\n  Gemini: https://aistudio.google.com/apikey"
        )

    total_wait_start = time.time()
    last_failed_provider = None

    while True:
        # Check total wait budget
        if time.time() - total_wait_start > _MAX_WAIT_TOTAL:
            raise LLMConfigError(
                f"All LLM providers exhausted after {_MAX_WAIT_TOTAL}s of waiting.\n"
                "  All configured providers are rate-limited. Try again later\n"
                "  or add more API keys to .env for better failover."
            )

        # Pick next provider
        result = _next_available_provider(exclude=last_failed_provider)
        if result is None:
            raise LLMConfigError("No LLM API keys configured.")

        provider_name, wait_seconds = result

        if wait_seconds > 0:
            display = _PROVIDERS[provider_name]["display_name"]
            print(f"    All providers cooling down. Waiting {wait_seconds:.0f}s for {display}...")
            time.sleep(wait_seconds + 2)

        config = _PROVIDERS[provider_name]
        api_key = os.getenv(config["key_env"], "")
        model = os.getenv("LLM_MODEL", config["model"])
        client = OpenAI(base_url=config["base_url"], api_key=api_key)
        display = config["display_name"]

        if task_label:
            print(f"    [{display}] {task_label}")

        # Try this provider with limited retries before failing over
        for attempt in range(_RETRIES_BEFORE_FAILOVER):
            try:
                content = make_request(client, model)
                if content is None:
                    if attempt < _RETRIES_BEFORE_FAILOVER - 1:
                        print(f"    [{display}] Empty response, retrying...")
                        time.sleep(2)
                        continue
                    break  # Fall through to failover
                return content.strip()

            except RateLimitError as e:
                retry_after = _get_retry_after(e)
                cooldown = retry_after or _DEFAULT_COOLDOWN[provider_name]
                _set_provider_cooldown(provider_name, cooldown)
                last_failed_provider = provider_name
                break  # Try next provider

            except APIConnectionError:
                if attempt < _RETRIES_BEFORE_FAILOVER - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"    [{display}] Connection failed, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    _set_provider_cooldown(provider_name, 30)
                    last_failed_provider = provider_name
                    break

            except APIError as e:
                if attempt < _RETRIES_BEFORE_FAILOVER - 1:
                    print(f"    [{display}] API error: {e}, retrying...")
                    time.sleep(2)
                else:
                    _set_provider_cooldown(provider_name, 15)
                    last_failed_provider = provider_name
                    break


# ---------------------------------------------------------------------------
# Public API — thin wrappers around _call_with_failover
# ---------------------------------------------------------------------------

def chat_completion(
    system: str,
    user_message: str,
    max_tokens: int = 4096,
    task: str = "",
) -> str:
    """Send a single-turn chat completion with automatic provider failover.

    Args:
        system: System prompt
        user_message: User message
        max_tokens: Maximum tokens in the response
        task: Description for logging

    Returns:
        The model's text response.
    """

    def make_request(client, model):
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        )
        return response.choices[0].message.content

    return _call_with_failover(make_request, task_label=task)


def chat_completion_multi(
    messages: list[dict],
    max_tokens: int = 4096,
) -> str:
    """Send a multi-turn chat completion with automatic provider failover.

    Args:
        messages: List of {"role": "system"|"user"|"assistant", "content": "..."}
        max_tokens: Maximum tokens in the response

    Returns:
        The model's text response.
    """

    def make_request(client, model):
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return response.choices[0].message.content

    return _call_with_failover(make_request, task_label="")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_retry_after(error) -> float | None:
    """Extract retry-after seconds from a RateLimitError, if available."""
    try:
        headers = error.response.headers
        retry_after = headers.get("retry-after")
        if retry_after:
            return float(retry_after) + 1  # +1s buffer
    except (AttributeError, ValueError, TypeError):
        pass
    return None
