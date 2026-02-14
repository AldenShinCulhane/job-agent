"""
LLM client — sends requests to Groq's API via its OpenAI-compatible endpoint.

Requires:
  - A free Groq API key: https://console.groq.com
  - Set GROQ_API_KEY in .env

Groq free tier limits (llama-3.3-70b-versatile):
  - 30 requests/min, 1,000 requests/day
  - 12,000 tokens/min, 100,000 tokens/day
  The TPM (tokens/min) limit is the usual bottleneck.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
MAX_RETRIES = 6


class LLMConfigError(Exception):
    """Raised when the LLM API is not reachable or misconfigured."""
    pass


def check_llm() -> bool:
    """Check if the Groq API key is configured. Returns True if set."""
    return bool(GROQ_API_KEY)


def chat_completion(
    system: str,
    user_message: str,
    max_tokens: int = 4096,
    task: str = "analyze",
) -> str:
    """
    Send a chat completion request to Groq.

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

    if not GROQ_API_KEY:
        raise LLMConfigError(
            "GROQ_API_KEY not set. Get a free key at https://console.groq.com\n"
            "  Then add it to your .env file: GROQ_API_KEY=gsk_..."
        )

    client = OpenAI(base_url=GROQ_BASE_URL, api_key=GROQ_API_KEY)

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
            return response.choices[0].message.content.strip()
        except APIConnectionError:
            if attempt == MAX_RETRIES - 1:
                raise LLMConfigError(
                    "Cannot connect to Groq API. Check your internet connection\n"
                    "  and verify your GROQ_API_KEY is valid."
                )
            wait = 2 ** (attempt + 1)
            print(f"    Connection failed, retrying in {wait}s...")
            time.sleep(wait)
        except RateLimitError as e:
            if attempt == MAX_RETRIES - 1:
                raise LLMConfigError("Groq API: max retries exceeded (rate limited).")
            # Try to parse retry-after from response headers
            wait = _get_retry_after(e)
            if wait is None:
                # Fallback: longer backoff since the bottleneck is TPM (token window)
                wait = [15, 30, 60, 60, 60, 60][min(attempt, 5)]
            print(f"    Rate limited, waiting {wait}s... (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
        except APIError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            print(f"    API error: {e}, retrying...")
            time.sleep(2)

    raise LLMConfigError("Groq API: max retries exceeded.")


def _get_retry_after(error) -> float | None:
    """Extract retry-after seconds from a RateLimitError, if available."""
    try:
        # OpenAI SDK stores response headers on the error
        headers = error.response.headers
        retry_after = headers.get("retry-after")
        if retry_after:
            return float(retry_after) + 1  # +1s buffer
    except (AttributeError, ValueError, TypeError):
        pass
    return None
