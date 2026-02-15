"""
LLM client — sends requests via OpenAI-compatible APIs.

Default provider: SambaNova (Llama 3.3 70B, unlimited free tier).
Use set_provider() or --provider flag to switch to an alternative.

Supported providers:
  - sambanova: SambaNova (default) — unlimited free tokens, 20 RPM
  - cerebras:  Cerebras — 1M tokens/day free, 30 RPM
  - gemini:    Google Gemini 2.5 Flash — 10 RPM, 250 RPD
  - groq:      Groq — Llama 3.3 70B, 12K TPM

Override the model with LLM_MODEL env var.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

MAX_RETRIES = 6

# Supported providers
_PROVIDERS = {
    "sambanova": {
        "key_env": "SAMBANOVA_API_KEY",
        "base_url": "https://api.sambanova.ai/v1",
        "model": "Meta-Llama-3.3-70B-Instruct",
        "delay": 3,
        "backoff": [5, 10, 20, 30, 60, 60],
        "display_name": "SambaNova",
    },
    "cerebras": {
        "key_env": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
        "delay": 3,
        "backoff": [5, 10, 20, 30, 60, 60],
        "display_name": "Cerebras",
    },
    "gemini": {
        "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
        "delay": 7,
        "backoff": [7, 15, 30, 30, 60, 60],
        "display_name": "Gemini",
    },
    "groq": {
        "key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "delay": 7,
        "backoff": [15, 30, 60, 60, 60, 60],
        "display_name": "Groq",
    },
}

_DEFAULT_PROVIDER = "sambanova"

# Active provider state
_active_provider = _DEFAULT_PROVIDER
_active_config = _PROVIDERS[_DEFAULT_PROVIDER]
_api_key = os.getenv(_active_config["key_env"], "")
MODEL = os.getenv("LLM_MODEL", _active_config["model"])


def set_provider(name: str):
    """Set the active LLM provider. Called by run_pipeline.py when --provider is used."""
    global _active_provider, _active_config, _api_key, MODEL

    name = name.lower()
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown provider '{name}'. Choose from: {', '.join(_PROVIDERS.keys())}")

    _active_provider = name
    _active_config = _PROVIDERS[name]
    _api_key = os.getenv(_active_config["key_env"], "")
    MODEL = os.getenv("LLM_MODEL", _active_config["model"])


class LLMConfigError(Exception):
    """Raised when the LLM API is not reachable or misconfigured."""
    pass


def check_llm() -> bool:
    """Check if the active provider's API key is configured. Returns True if set."""
    return bool(_api_key)


def get_provider_name() -> str:
    """Return the active provider's display name."""
    return _active_config["display_name"]


def get_expected_key_name() -> str:
    """Return the env var name the active provider expects (for error messages)."""
    return _active_config["key_env"]


def get_call_delay() -> float:
    """Return recommended seconds to wait between API calls for the active provider."""
    return _active_config["delay"]


def chat_completion(
    system: str,
    user_message: str,
    max_tokens: int = 4096,
    task: str = "analyze",
) -> str:
    """
    Send a chat completion request to the configured LLM provider.

    Args:
        system: System prompt
        user_message: User message
        max_tokens: Maximum tokens in the response
        task: "analyze" or "generate" (currently uses same model)

    Returns:
        The model's text response.

    Raises:
        LLMConfigError: If API key is missing or API is unreachable.
    """
    from openai import OpenAI, RateLimitError, APIError, APIConnectionError

    if not _api_key:
        raise LLMConfigError(
            f"No API key found for {get_provider_name()}.\n"
            f"  Set {get_expected_key_name()}=... in your .env file.\n"
            f"  Get a free key at https://cloud.sambanova.ai/apis (SambaNova, recommended)\n"
            f"  Or use --provider to select a different provider."
        )

    client = OpenAI(base_url=_active_config["base_url"], api_key=_api_key)

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            content = response.choices[0].message.content
            if content is None:
                if attempt == MAX_RETRIES - 1:
                    raise LLMConfigError("LLM returned empty response.")
                print(f"    Empty response, retrying...")
                time.sleep(2)
                continue
            return content.strip()
        except APIConnectionError:
            if attempt == MAX_RETRIES - 1:
                raise LLMConfigError(
                    f"Cannot connect to {get_provider_name()} API. Check your internet connection\n"
                    f"  and verify your API key is valid."
                )
            wait = 2 ** (attempt + 1)
            print(f"    Connection failed, retrying in {wait}s...")
            time.sleep(wait)
        except RateLimitError as e:
            if attempt == MAX_RETRIES - 1:
                raise LLMConfigError(f"{get_provider_name()} API: max retries exceeded (rate limited).")
            wait = _get_retry_after(e)
            if wait is None:
                wait = _active_config["backoff"][min(attempt, 5)]
            print(f"    Rate limited, waiting {wait}s... (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
        except APIError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            print(f"    API error: {e}, retrying...")
            time.sleep(2)

    raise LLMConfigError(f"{get_provider_name()} API: max retries exceeded.")


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
