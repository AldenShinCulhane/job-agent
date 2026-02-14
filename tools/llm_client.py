"""
LLM client — sends requests to a local Ollama instance via its
OpenAI-compatible API.

Requires:
  - Ollama installed and running: https://ollama.com
  - A model pulled: ollama pull llama3.3
"""

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

OLLAMA_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
ANALYZE_MODEL = os.getenv("LLM_MODEL", "llama3.3")
GENERATE_MODEL = os.getenv("LLM_MODEL", "llama3.3")
MAX_RETRIES = 3


class LLMConfigError(Exception):
    """Raised when Ollama is not running or misconfigured."""
    pass


def check_ollama() -> bool:
    """Check if Ollama is reachable. Returns True if running."""
    try:
        resp = requests.get(OLLAMA_HOST, timeout=3)
        return resp.status_code == 200
    except requests.ConnectionError:
        return False
    except Exception:
        return False


def chat_completion(
    system: str,
    user_message: str,
    max_tokens: int = 4096,
    task: str = "analyze",
) -> str:
    """
    Send a chat completion request to Ollama.

    Args:
        system: System prompt
        user_message: User message
        max_tokens: Maximum tokens in the response
        task: "analyze" or "generate" — selects model

    Returns:
        The model's text response.

    Raises:
        LLMConfigError: If Ollama is not running.
    """
    from openai import OpenAI, RateLimitError, APIError, APIConnectionError

    model = ANALYZE_MODEL if task == "analyze" else GENERATE_MODEL
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            return response.choices[0].message.content.strip()
        except APIConnectionError:
            raise LLMConfigError(
                "Cannot connect to Ollama. Make sure it's running:\n"
                "  1. Install: https://ollama.com\n"
                "  2. Start Ollama\n"
                "  3. Pull a model: ollama pull llama3.3"
            )
        except RateLimitError:
            wait = 2 ** (attempt + 1)
            print(f"    Rate limited, waiting {wait}s...")
            time.sleep(wait)
        except APIError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            print(f"    API error: {e}, retrying...")
            time.sleep(2)

    raise LLMConfigError("Ollama: max retries exceeded.")
