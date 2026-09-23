"""Engine state and fake engine calls, shaped the way production makes them.

Real engine answers carry the context they were produced under, fixed
before the call was sent (llm.Structured). Fakes wrapped here do the same:
the context is taken when the fake is entered, so a test that changes the
selection mid-call sees exactly what production would record.
"""

import functools
import json

from backend.core import db, llm


def set_engine(provider: str = "claude", **fields) -> None:
    """Write provider-level engine state (status, last_error,
    next_retry_at, ...), bumping the provider's generation as a real
    write does."""
    for name, value in fields.items():
        key = f"engine.{provider}.{name}"
        if value is None:
            db.delete_state(key)
        else:
            db.set_state(key, str(value))
    gen = int(db.get_state(f"engine.{provider}.gen", "0") or 0)
    db.set_state(f"engine.{provider}.gen", str(gen + 1))


def engine_value(name: str, provider: str = "claude") -> str | None:
    return db.get_state(f"engine.{provider}.{name}")


def pin(model: str) -> None:
    """The default Claude selection, already probed onto `model`."""
    db.set_state("engine.claude.auto_model", model)
    raw = db.get_state("engine.claude.models")
    models = json.loads(raw) if raw else {}
    models[model] = {"status": "ok", "last_error": None, "verified_at": None}
    db.set_state("engine.claude.models", json.dumps(models))


def answering(task: str, fn):
    """Wrap a fake that stands in for a task-specific engine call (e.g.
    generate_tailored, validate.repair)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        context = llm.dispatch_context(task)
        result = await fn(*args, **kwargs)
        return llm.Structured(result, context)
    return wrapper


def structured(fn):
    """Wrap a fake llm.structured(prompt, schema, task, system_prompt)."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        task = kwargs["task"] if "task" in kwargs else args[2]
        context = llm.dispatch_context(task)
        result = await fn(*args, **kwargs)
        return llm.Structured(result, context)
    return wrapper
