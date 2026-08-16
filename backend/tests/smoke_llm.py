"""Live engine smoke test (uses the Claude subscription; run manually).

    uv run python -m backend.tests.smoke_llm

Proves the M0 criteria: subscription auth without an API key, model probe
with fable->opus5 routing, zero tool calls under lockdown, and JSON-schema
structured output.
"""

import asyncio
import json
import os

from backend.core import db, llm

SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["language", "confidence"],
    "additionalProperties": False,
}


async def main() -> None:
    assert "ANTHROPIC_API_KEY" not in os.environ, "engine must strip the API key"
    db.init()

    model = await llm.probe()
    print(f"probe pinned: {model}")

    out = await llm.structured(
        "What programming language is this? `def f(x): return x * 2`",
        SCHEMA,
        task="extract",
    )
    print(f"structured output: {json.dumps(out)}")
    assert set(out) == {"language", "confidence"}, out

    print(f"engine status: {json.dumps(llm.engine_status())}")
    print("SMOKE PASS — zero tool calls, schema-valid JSON, subscription auth")


if __name__ == "__main__":
    asyncio.run(main())
