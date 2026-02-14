"""
LLM client — sends requests via OpenAI-compatible APIs.

Provider auto-detection (checks in order):
  1. GEMINI_API_KEY -> Google Gemini 2.5 Flash (recommended, generous free tier)
  2. GROQ_API_KEY  -> Groq (Llama 3.3 70B, stricter limits)

Gemini free tier: 10 RPM, 250 RPD, 250K TPM (no daily token cap)
Groq free tier:   30 RPM, 1K RPD, 12K TPM, 100K TPD

Override the model with LLM_MODEL env var.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

MAX_RETRIES = 6

# Provider auto-detection
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

if GEMINI_API_KEY:
    _PROVIDER = "gemini"
    _API_KEY = GEMINI_API_KEY
    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
    _DEFAULT_MODEL = "gemini-2.5-flash"
elif GROQ_API_KEY:
    _PROVIDER = "groq"
    _API_KEY = GROQ_API_KEY
    _BASE_URL = "https://api.groq.com/openai/v1"
    _DEFAULT_MODEL = "llama-3.3-70b-versatile"
else:
    _PROVIDER = None
    _API_KEY = ""
    _BASE_URL = ""
    _DEFAULT_MODEL = ""

MODEL = os.getenv("LLM_MODEL", _DEFAULT_MODEL)


class LLMConfigError(Exception):
    """Raised when the LLM API is not reachable or misconfigured."""
    pass


def check_llm() -> bool:
    """Check if an LLM API key is configured. Returns True if set."""
    return bool(GEMINI_API_KEY or GROQ_API_KEY)


def get_provider_name() -> str:
    """Return the active provider name for display."""
    if _PROVIDER == "gemini":
        return "Gemini"
    elif _PROVIDER == "groq":
        return "Groq"
    return "None"


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

    if not _API_KEY:
        raise LLMConfigError(
            "No LLM API key found. Set one of these in your .env file:\n"
            "  GEMINI_API_KEY=AIza...  (recommended — https://aistudio.google.com/apikeys)\n"
            "  GROQ_API_KEY=gsk_...    (alternative — https://console.groq.com)"
        )

    client = OpenAI(base_url=_BASE_URL, api_key=_API_KEY)

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
                if _PROVIDER == "gemini":
                    # Gemini: 10 RPM limit, so wait ~7s per retry
                    wait = [7, 15, 30, 30, 60, 60][min(attempt, 5)]
                else:
                    # Groq: TPM is the bottleneck, needs longer waits
                    wait = [15, 30, 60, 60, 60, 60][min(attempt, 5)]
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
