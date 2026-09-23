"""LLM engine — the user's own subscription, never an API key.

Two engines (PLAN.md §4.1, §4.1a): Claude through the Claude Agent SDK on
a claude.ai login, or Codex through the Codex CLI on a ChatGPT login. The
user picks ONE model for every task in Settings; the app never switches
engines by itself. Every call is a single prompt with JSON-schema output
and no tools; the LLM never emits raw LaTeX.

State (all durable, in SQLite):
- Per provider: sign-in, usage limit, CLI/configuration problems. A limit
  is account-wide, so switching models within a provider never clears it.
- Per model: entitlement (ok / not_entitled), so a stale answer about one
  model can never change the badge of another.
- Every provider-level write bumps the provider's generation. A call
  records the generation it was dispatched under, and its outcome may only
  change provider state while that generation is still current: a request
  sent before a limit began cannot clear the limit, and a failure sent
  before the user fixed their login cannot bring the error back.

Execution identity is frozen before dispatch (`CallContext`) and returned
with the answer (`Structured.context`). Ledgers and provenance are written
from it, never from the current selection, which can change mid-call.

Usage limits: `next_retry_at` is the app's own backoff schedule;
`limit_resets_at` is kept only when the provider reports it. Each limit is
one quota episode (`episode`), which the UI uses to open the engine picker
once, not on every countdown tick or concurrent failure.

Claude keeps the PLAN.md probe: fable -> opus5 only for the default,
unchosen selection. An explicitly chosen model never falls back.
Tool and prompt isolation for Claude: tools=[], strict MCP, dedicated
CLAUDE_CONFIG_DIR (claude_env.py); a real tool call is a lockdown breach.
Codex isolation lives in codex_engine.py.
"""

import asyncio
import json
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from backend.core.events import broker

PROVIDERS = ("claude", "codex")

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

# Every effort level either engine names, weakest first. Codex's "ultra"
# (automatic task delegation) is deliberately absent: it is never used.
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

# Bounded concurrency for LLM calls (browser actions are serialized
# elsewhere; generation is not).
MAX_CONCURRENT_CALLS = 3
_semaphore: asyncio.Semaphore | None = None

_RETRY_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 2.0

_BACKOFF_START_MIN = 5
_BACKOFF_CAP_MIN = 60

# A pre-dispatch credentials check older than this is refreshed first.
CREDENTIALS_MAX_AGE_S = 60.0

DEFAULT_SYSTEM_PROMPT = (
    "You are the reasoning engine inside a job-application assistant. "
    "Answer only in the requested JSON structure."
)

# Model IDs travel into CLI arguments and JSON files: a conservative shape.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")

harden_process_env()


class EngineError(Exception):
    """Base for typed engine failures."""


class AuthRequired(EngineError):
    """No usable subscription login — user_intervention_required."""


class UsageExhausted(EngineError):
    """Subscription allowance hit its limit; wait and retry.

    `resets_at` is set only when the provider said when the limit resets.
    """

    def __init__(self, message: str = "", resets_at: datetime | None = None):
        super().__init__(message)
        self.resets_at = resets_at


class ModelNotEntitled(EngineError):
    """The model is not available on the user's plan."""


class TransientFailure(EngineError):
    """Network / server hiccup; retried with backoff before propagating."""


class ConfigError(EngineError):
    """Invalid engine configuration — fail loudly, do not retry."""


class ToolCallBlocked(Exception):
    """The model tried to call a tool; the engine refused it and the app
    stopped the call. Deliberately NOT an EngineError: the engine is
    healthy, this one prompt misbehaved (a hostile job posting can cause
    it), so callers fail just that item and carry on."""


@dataclass(frozen=True)
class CallContext:
    """What a call ran on, fixed before it was sent."""

    provider: str
    model: str | None
    effort: str | None
    gen: int = 0
    fingerprint: str | None = None

    def record(self) -> dict:
        entry: dict[str, Any] = {"provider": self.provider, "model": self.model,
                                 "effort": self.effort}
        if self.fingerprint:
            entry["lockdown"] = self.fingerprint[:16]
        return entry


class Structured(dict):
    """A structured answer that carries the context it was produced under.

    A dict, so every caller that reads the answer keeps working; the
    package pipeline reads `.context` for its ledger. `resolved_model` is
    the concrete model the provider reported, when it did (an alias such
    as "sonnet" resolves to a dated model).
    """

    def __init__(self, data: dict, context: CallContext,
                 resolved_model: str | None = None):
        super().__init__(data)
        self.context = context
        self.resolved_model = resolved_model


# --- durable state ------------------------------------------------------------

_SELECTION_KEY = "engine.selection"


def _key(provider: str, name: str) -> str:
    return f"engine.{provider}.{name}"


def _get(provider: str, name: str, default: str | None = None) -> str | None:
    return db.get_state(_key(provider, name), default)


def _put(provider: str, name: str, value: Any) -> None:
    if value is None:
        db.delete_state(_key(provider, name))
    else:
        db.set_state(_key(provider, name), str(value))


def _get_json(provider: str, name: str, default: Any) -> Any:
    raw = _get(provider, name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _put_json(provider: str, name: str, value: Any) -> None:
    _put(provider, name, None if value is None else json.dumps(value, sort_keys=True))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gen(provider: str) -> int:
    try:
        return int(_get(provider, "gen", "0") or 0)
    except ValueError:
        return 0


def _bump(provider: str) -> None:
    _put(provider, "gen", _gen(provider) + 1)


def _models(provider: str) -> dict[str, dict]:
    value = _get_json(provider, "models", {})
    return value if isinstance(value, dict) else {}


def _model_state(provider: str, model: str | None) -> dict:
    if not model:
        return {}
    return _models(provider).get(model, {})


def _set_model_state(provider: str, model: str, state: dict | None) -> None:
    models = _models(provider)
    if state is None:
        models.pop(model, None)
    else:
        models[model] = state
    _put_json(provider, "models", models or None)


def _signature() -> str:
    """Everything the badge, banners and engine picker render, in one
    comparable value — minus per-call timestamps, which move on every
    successful call and would put one identical event per scored job on
    the wire."""
    view: dict[str, Any] = {"selection": selection()}
    for provider in PROVIDERS:
        credentials = _get_json(provider, "credentials", None) or {}
        view[provider] = {
            "status": _get(provider, "status"),
            "last_error": _get(provider, "last_error"),
            "next_retry_at": _get(provider, "next_retry_at"),
            "limit_resets_at": _get(provider, "limit_resets_at"),
            "episode": _get(provider, "episode"),
            "auto_model": _get(provider, "auto_model"),
            "credentials": [credentials.get("present"), credentials.get("problem")],
            "models": {m: [s.get("status"), s.get("last_error")]
                       for m, s in _models(provider).items()},
            "catalog": _get(provider, "catalog"),
            "catalog_error": _get(provider, "catalog_error"),
        }
    return json.dumps(view, sort_keys=True)


def _announce(previous: str) -> None:
    """Invalidate the UI surfaces that read engine state, only on a real
    change. The payload is a hint, not truth: the client refetches."""
    if _signature() != previous:
        broker.publish("engine", {"status": engine_status()["status"]})


# --- selection ----------------------------------------------------------------

def valid_model_id(model: str) -> bool:
    return bool(_MODEL_ID.match(model))


def selection() -> dict:
    """The user's engine choice. Claude with no model is the default: the
    fable -> opus probe picks it."""
    raw = db.get_state(_SELECTION_KEY)
    try:
        chosen = json.loads(raw) if raw else {}
    except ValueError:
        chosen = {}
    provider = chosen.get("provider")
    model = chosen.get("model")
    if provider not in PROVIDERS:
        return {"provider": "claude", "model": None}
    if not isinstance(model, str) or not valid_model_id(model):
        model = None
    if provider == "codex" and model is None:
        # Codex has no default model of its own here; a damaged record
        # falls back to the app default rather than an unusable choice.
        return {"provider": "claude", "model": None}
    return {"provider": provider, "model": model}


def select(provider: str, model: str | None) -> dict:
    """Record the user's choice. Validation only; the next call (or the
    caller's re-check) probes it."""
    if provider not in PROVIDERS:
        raise ConfigError(f"unknown engine {provider!r}")
    if model is not None and not valid_model_id(model):
        raise ConfigError(f"not a valid model id: {model!r}")
    if provider == "codex" and model is None:
        raise ConfigError("choose a Codex model")
    previous = _signature()
    db.set_state(_SELECTION_KEY, json.dumps({"provider": provider, "model": model}))
    _announce(previous)
    return selection()


def _resolved_model(chosen: dict) -> str | None:
    if chosen["model"]:
        return chosen["model"]
    return _get("claude", "auto_model")


# --- status view --------------------------------------------------------------

def _iso_or_none(value: str | None) -> str | None:
    return value or None


def seconds_until_retry(provider: str | None = None) -> float:
    """0 when the provider may run now; positive while waiting out a limit.
    Defaults to the selected provider."""
    provider = provider or selection()["provider"]
    retry_at = _get(provider, "next_retry_at")
    if retry_at is None:
        return 0.0
    try:
        remaining = (datetime.fromisoformat(retry_at) - _now()).total_seconds()
    except ValueError:
        return 0.0
    return max(0.0, remaining)


def provider_status(provider: str, model: str | None) -> dict:
    """One provider's view for one model. A provider-level problem
    (sign-in, limit, broken CLI) outranks the model's own state."""
    status = _get(provider, "status", "ok")
    state = _model_state(provider, model)
    combined = status if status != "ok" else (state.get("status") or "unprobed")
    credentials = _get_json(provider, "credentials", None)
    return {
        "provider": provider,
        "status": combined,
        "model": model,
        "retry_in_seconds": seconds_until_retry(provider) or None,
        "next_retry_at": _iso_or_none(_get(provider, "next_retry_at")),
        "limit_resets_at": _iso_or_none(_get(provider, "limit_resets_at")),
        "episode": _get(provider, "episode"),
        "last_error": _get(provider, "last_error") or state.get("last_error"),
        "credentials": credentials,
        "verified_at": state.get("verified_at"),
    }


def engine_status() -> dict:
    """The selected engine, in the shape the header badge and banners
    read (status / model / retry_in_seconds / last_error), plus what the
    engine picker needs."""
    chosen = selection()
    view = provider_status(chosen["provider"], _resolved_model(chosen))
    view["selected_model"] = chosen["model"]
    return view


def engines_view() -> dict:
    """Both providers for the engine picker. Model lists are read from the
    cached catalogs; `refresh_catalog` fills them."""
    chosen = selection()
    providers = {}
    for provider in PROVIDERS:
        model = (_resolved_model(chosen) if provider == chosen["provider"]
                 else None)
        view = provider_status(provider, model)
        view["catalog"] = _get_json(provider, "catalog", None)
        view["catalog_error"] = _get(provider, "catalog_error")
        view["models"] = {m: {"status": s.get("status"),
                              "last_error": s.get("last_error"),
                              "verified_at": s.get("verified_at")}
                          for m, s in _models(provider).items()}
        providers[provider] = view
    providers["claude"]["auto_model"] = _get("claude", "auto_model")
    return {"selection": chosen, "providers": providers}


# --- effort -------------------------------------------------------------------

def effective_effort(task: str) -> str:
    """The effort tier a task runs at, with the max→xhigh cap applied."""
    effort = TASK_EFFORT[task]
    if effort == "max" and not ALLOW_MAX_EFFORT:
        return "xhigh"
    return effort


def model_efforts(provider: str, model: str | None) -> list[str] | None:
    """The effort levels a catalogued model supports; None when the model
    is not in the cached catalog (the tier is then passed as-is)."""
    if not model:
        return None
    catalog = _get_json(provider, "catalog", None) or []
    for entry in catalog:
        if isinstance(entry, dict) and entry.get("id") == model:
            efforts = entry.get("efforts")
            return list(efforts) if isinstance(efforts, list) else None
    return None


def effort_for(task: str, provider: str, model: str | None) -> str | None:
    """The tier, clamped to what the model supports: the strongest level
    not above the tier, else the weakest it has. None when the model takes
    no effort setting at all."""
    tier = effective_effort(task)
    levels = model_efforts(provider, model)
    if levels is None:
        return tier
    allowed = [lvl for lvl in EFFORT_ORDER if lvl in levels
               and (ALLOW_MAX_EFFORT or lvl != "max")]
    if not allowed:
        return None
    ceiling = EFFORT_ORDER.index(tier)
    below = [lvl for lvl in allowed if EFFORT_ORDER.index(lvl) <= ceiling]
    return below[-1] if below else allowed[0]


def dispatch_context(task: str, *, chosen: dict | None = None,
                     model: str | None = None,
                     fingerprint: str | None = None) -> CallContext:
    """The context a call for `task` would be sent with right now."""
    chosen = chosen or selection()
    provider = chosen["provider"]
    model = model or _resolved_model(chosen)
    return CallContext(provider, model, effort_for(task, provider, model),
                       gen=_gen(provider), fingerprint=fingerprint)


# --- outcome recording (context-bound) ----------------------------------------

def _may_write(ctx: CallContext) -> bool:
    return ctx.gen == _gen(ctx.provider)


def _record_problem(ctx: CallContext | None, provider: str, status: str,
                    error: str) -> None:
    """auth_required / config_error at provider level. A call dispatched
    before the provider's latest state change cannot overwrite it."""
    previous = _signature()
    if ctx is None or _may_write(ctx):
        _put(provider, "status", status)
        _put(provider, "last_error", error)
        _put(provider, "problem_source", "service")
        _bump(provider)
    _announce(previous)


def _record_usage(ctx: CallContext, exc: UsageExhausted) -> None:
    """Start a quota episode, or extend the current one when a call sent
    during it (after its cooldown) hit the limit again. A call sent before
    the provider's latest change adds nothing: concurrent calls sent before
    the episode began (one limit, one episode, one backoff step), and a
    late answer after a newer success ended the episode, which must not
    reopen it. A limit that is really back is hit again by the next call."""
    provider = ctx.provider
    previous = _signature()
    if _may_write(ctx):
        episode = _get(provider, "episode") or uuid.uuid4().hex
        try:
            minutes = int(_get(provider, "backoff_minutes",
                               str(_BACKOFF_START_MIN)) or _BACKOFF_START_MIN)
        except ValueError:
            minutes = _BACKOFF_START_MIN
        retry_at = _now() + timedelta(minutes=minutes)
        if exc.resets_at is not None and exc.resets_at > retry_at:
            # Retrying before the provider's own reset only spends requests.
            retry_at = exc.resets_at
        _put(provider, "status", "waiting_usage_exhausted")
        _put(provider, "episode", episode)
        _put(provider, "next_retry_at", retry_at.isoformat())
        _put(provider, "backoff_minutes", min(minutes * 2, _BACKOFF_CAP_MIN))
        _put(provider, "limit_resets_at",
             exc.resets_at.isoformat() if exc.resets_at else None)
        _put(provider, "last_error", str(exc) or None)
        _put(provider, "problem_source", None)
        _bump(provider)
    _announce(previous)


def _record_not_entitled(ctx: CallContext, error: str) -> None:
    previous = _signature()
    if ctx.model:
        _set_model_state(ctx.provider, ctx.model, {
            "status": "not_entitled", "last_error": error, "verified_at": None})
    _announce(previous)


def _record_success(ctx: CallContext) -> None:
    """The model answered: it is entitled, and — if nothing changed at the
    provider since this call was sent — whatever problem the provider had
    is over."""
    provider = ctx.provider
    previous = _signature()
    if ctx.model:
        _set_model_state(provider, ctx.model, {
            "status": "ok", "last_error": None, "verified_at": _now().isoformat()})
    if _may_write(ctx):
        dirty = any(_get(provider, name) is not None for name in (
            "last_error", "next_retry_at", "backoff_minutes",
            "limit_resets_at", "episode", "problem_source"))
        if _get(provider, "status", "ok") != "ok" or dirty:
            for name in ("last_error", "next_retry_at", "backoff_minutes",
                         "limit_resets_at", "episode", "problem_source"):
                _put(provider, name, None)
            _put(provider, "status", "ok")
            _bump(provider)
    _announce(previous)


def _clear_problem(provider: str) -> None:
    """End a sign-in or setup problem. A usage limit is account-wide and
    outlives it: while its episode is open the provider goes back to
    waiting, never straight to ok."""
    waiting = _get(provider, "episode") is not None
    _put(provider, "status", "waiting_usage_exhausted" if waiting else "ok")
    _put(provider, "last_error", None)
    _put(provider, "problem_source", None)
    _bump(provider)


def _require_not_waiting(provider: str) -> None:
    """The wait state is enforced here, not just recorded."""
    wait = seconds_until_retry(provider)
    if wait > 0:
        raise UsageExhausted(f"usage limit cooldown: retry in {int(wait)}s")


# --- credentials (local, cheap, coalesced) ------------------------------------

def credential_problem(provider: str) -> tuple[str, str] | None:
    """(kind, text) when the provider cannot be used as signed in, where
    kind is "auth" (sign in) or "setup" (install/configure). Local checks
    only: no request is spent."""
    if provider == "claude":
        problem = claude_cli.subscription_problem()
        return None if problem is None else ("auth", problem)
    from backend.core import codex_cli
    return codex_cli.login_problem()


_credential_checks: dict[str, asyncio.Task] = {}


async def refresh_credentials(provider: str, *, max_age: float = 0.0) -> str | None:
    """Check local credentials, at most once per `max_age` seconds and at
    most once at a time per provider. Records the result; a missing login
    becomes auth_required. Present credentials clear only a problem a
    local check found: only a successful call proves the service works, so
    a rejection it reported, a limit or a lockdown breach stays."""
    cached = _get_json(provider, "credentials", None)
    if cached and max_age > 0:
        try:
            age = (_now() - datetime.fromisoformat(cached["checked_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = max_age
        if age < max_age:
            return cached.get("problem")
    loop = asyncio.get_running_loop()
    task = _credential_checks.get(provider)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_check_credentials(provider))
        _credential_checks[provider] = task
    return await asyncio.shield(task)


async def _check_credentials(provider: str) -> str | None:
    try:
        result = await asyncio.to_thread(credential_problem, provider)
    except asyncio.CancelledError:
        raise
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — a broken CLI is a finding
        result = ("setup", f"credentials check failed: {type(exc).__name__}: {exc}")
    previous = _signature()
    problem = None if result is None else result[1]
    _put_json(provider, "credentials", {
        "present": result is None, "problem": problem,
        "kind": None if result is None else result[0],
        "checked_at": _now().isoformat()})
    # Whose finding the provider's sign-in or setup problem is. Only one a
    # local check made can be ended by one; anything else (the service's
    # rejection, a lockdown breach, a state from before sources were
    # recorded) is left for a call that succeeds.
    standing = _get(provider, "status", "ok") in ("auth_required", "config_error")
    local = _get(provider, "problem_source") == "local"
    if result is None and standing and local:
        _clear_problem(provider)
    if result is not None and (local or not standing):
        # A local finding never writes over the service's own: it is kept
        # in `credentials`, and stops calls before they are sent.
        status = "auth_required" if result[0] == "auth" else "config_error"
        if _get(provider, "status", "ok") != status or _get(provider, "last_error") != problem:
            _put(provider, "status", status)
            _put(provider, "last_error", problem)
            _put(provider, "problem_source", "local")
            _bump(provider)
    _announce(previous)
    return problem


def credentials_present(provider: str) -> bool | None:
    """What the last local check found; None before the first check."""
    cached = _get_json(provider, "credentials", None)
    return None if not cached else bool(cached.get("present"))


async def _require_credentials(provider: str, *, max_age: float) -> None:
    problem = await refresh_credentials(provider, max_age=max_age)
    if problem is not None:
        # A missing or broken CLI is a setup problem, not a sign-in one.
        # Read from the check itself: the provider's status may be the
        # service's finding, which a local one never overwrites.
        cached = _get_json(provider, "credentials", None) or {}
        kind = cached.get("kind") or (
            "setup" if _get(provider, "status") == "config_error" else "auth")
        if kind == "setup":
            raise ConfigError(problem)
        raise AuthRequired(problem)


# --- Claude adapter -----------------------------------------------------------

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


_EPOCH_RESET = re.compile(r"\|\s*(\d{10})\b")
_CLOCK_RESET = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b(?:\s*\(([A-Za-z_]+/[A-Za-z_]+)\))?",
    re.IGNORECASE)


def parse_claude_reset(text: str, now: datetime | None = None) -> datetime | None:
    """When a Claude usage-limit message names its reset, that time. Two
    shapes are recognised: "…|<unix seconds>" and "resets 3pm
    (Area/City)"; anything else is unknown, never guessed."""
    now = now or _now()
    match = _EPOCH_RESET.search(text)
    if match:
        moment = datetime.fromtimestamp(int(match.group(1)), timezone.utc)
        return moment if moment > now else None
    match = _CLOCK_RESET.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
    minute = int(match.group(2) or 0)
    try:
        zone = ZoneInfo(match.group(4)) if match.group(4) else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    local_now = now.astimezone(zone) if zone else now.astimezone()
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _engine_error(cls: type[EngineError], detail: str) -> EngineError:
    if cls is UsageExhausted:
        return UsageExhausted(detail, resets_at=parse_claude_reset(detail))
    return cls(detail)


def _options(model: str, effort: str | None, system_prompt: str,
             schema: dict | None) -> ClaudeAgentOptions:
    kwargs: dict[str, Any] = dict(
        model=model,
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
    if effort is not None:
        # A model without effort control (e.g. Haiku) gets none.
        kwargs["effort"] = effort
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
        raise _engine_error(_classify(detail), detail) from exc

    # Raised outside the loop: raising mid-iteration aborts the SDK's
    # async generator uncleanly.
    if violations:
        raise ConfigError(f"tool lockdown breached: model called {violations!r}")
    if result is None:
        raise TransientFailure("query ended without a result message")
    if result.is_error:
        detail = str(result.result or result.subtype)
        raise _engine_error(_classify(detail), detail)
    if result.subtype == "success":
        return result
    if result.subtype == "error_max_structured_output_retries":
        raise TransientFailure("structured output failed schema validation repeatedly")
    detail = result.subtype + " " + str(getattr(result, "result", ""))
    raise _engine_error(_classify(detail), f"query failed: {result.subtype}")


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


async def _claude_call(ctx: CallContext, prompt: str, schema: dict | None,
                       system_prompt: str) -> tuple[Any, str | None]:
    result = await _run_with_retry(prompt, _options(ctx.model, ctx.effort,
                                                    system_prompt, schema))
    usage = getattr(result, "model_usage", None) or {}
    resolved = next(iter(usage), None) if isinstance(usage, dict) else None
    if schema is None:
        return result.result, resolved
    output = getattr(result, "structured_output", None)
    if output is None:
        raise TransientFailure("success result carried no structured output")
    return output, resolved


# --- probing -------------------------------------------------------------------

_PROBE_SYSTEM = "Reply with the single word: ok"


def _route_failure(ctx: CallContext, exc: EngineError) -> None:
    """Record a typed failure against the provider/model it belongs to."""
    if isinstance(exc, UsageExhausted):
        _record_usage(ctx, exc)
    elif isinstance(exc, AuthRequired):
        _record_problem(ctx, ctx.provider, "auth_required", str(exc))
    elif isinstance(exc, ConfigError):
        _record_problem(ctx, ctx.provider, "config_error", str(exc))
    elif isinstance(exc, ModelNotEntitled):
        _record_not_entitled(ctx, str(exc))


async def _probe_call(ctx: CallContext) -> None:
    if ctx.provider == "claude":
        await _claude_call(ctx, "Say ok.", None, _PROBE_SYSTEM)
    else:
        from backend.core import codex_engine
        await codex_engine.probe(ctx)


async def _probe_claude_default() -> str:
    """PLAN.md typed routing: only ModelNotEntitled falls back to opus-5;
    every other failure class propagates for the caller/UI to handle."""
    pinned = _get("claude", "auto_model")
    if pinned:
        return pinned
    _require_not_waiting("claude")
    await _require_credentials("claude", max_age=0.0)
    for model in (MODEL_PREFERRED, MODEL_FALLBACK):
        ctx = CallContext("claude", model, "low", gen=_gen("claude"))
        try:
            await _probe_call(ctx)
        except ModelNotEntitled as exc:
            _record_not_entitled(ctx, str(exc))
            if model == MODEL_FALLBACK:
                previous = _signature()
                _put("claude", "last_error", str(exc))
                _announce(previous)
                raise
            continue
        except EngineError as exc:
            _route_failure(ctx, exc)
            raise
        previous = _signature()
        _put("claude", "auto_model", model)
        _announce(previous)
        _record_success(ctx)
        return model
    raise ConfigError("probe fell through without pinning a model")  # unreachable


async def _probe_chosen(provider: str, model: str) -> str:
    """An explicitly chosen model: one probe, no fallback."""
    _require_not_waiting(provider)
    await _require_credentials(provider, max_age=0.0)
    # A probe proves the model answers; the weakest effort spends least.
    ctx = CallContext(provider, model, _weakest(provider, model), gen=_gen(provider))
    try:
        await _probe_call(ctx)
    except EngineError as exc:
        _route_failure(ctx, exc)
        raise
    _record_success(ctx)
    return model


def _weakest(provider: str, model: str | None) -> str | None:
    levels = model_efforts(provider, model)
    if levels is None:
        return "low"
    allowed = [lvl for lvl in EFFORT_ORDER if lvl in levels]
    return allowed[0] if allowed else None


async def _resolve(chosen: dict) -> str:
    provider, model = chosen["provider"], chosen["model"]
    if provider == "claude" and model is None:
        return await _probe_claude_default()
    assert model is not None
    state = _model_state(provider, model)
    if state.get("status") == "ok":
        return model
    if state.get("status") == "not_entitled":
        raise ModelNotEntitled(state.get("last_error")
                               or f"{model} is not available on this plan")
    return await _probe_chosen(provider, model)


async def probe() -> str:
    """Resolve and verify the selected model. Returns the model id."""
    return await _resolve(selection())


def reset_probe() -> None:
    """Settings-page hook: forget what is known about the selected model and
    re-check it on the next call. A usage limit is account-wide and stays:
    re-checking cannot erase it."""
    chosen = selection()
    provider = chosen["provider"]
    previous = _signature()
    if chosen["model"] is None:
        _put("claude", "auto_model", None)
        for model in (MODEL_PREFERRED, MODEL_FALLBACK):
            _set_model_state("claude", model, None)
    else:
        _set_model_state(provider, chosen["model"], None)
    if _get(provider, "status", "ok") in ("auth_required", "config_error"):
        _clear_problem(provider)
    elif _get(provider, "status", "ok") == "ok":
        _put(provider, "last_error", None)
    _announce(previous)


# --- public call ----------------------------------------------------------------

async def structured(
    prompt: str,
    schema: dict,
    task: str,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
) -> Structured:
    """Single-prompt, zero-tool, JSON-schema-validated call on the selected
    engine. The returned answer carries the context it ran under.

    `task` selects the effort tier (see TASK_EFFORT). While a usage-limit
    cooldown runs, calls raise without spending a request.
    """
    global _semaphore
    if task not in TASK_EFFORT:
        raise ConfigError(f"unknown task tier {task!r}")
    chosen = selection()
    provider = chosen["provider"]
    _require_not_waiting(provider)
    await _require_credentials(provider, max_age=CREDENTIALS_MAX_AGE_S)
    model = await _resolve(chosen)
    fingerprint = None
    if provider == "codex":
        from backend.core import codex_engine
        try:
            fingerprint = await codex_engine.ensure_verified(model)
        except EngineError as exc:
            _route_failure(dispatch_context(task, chosen=chosen, model=model), exc)
            raise
    ctx = dispatch_context(task, chosen=chosen, model=model, fingerprint=fingerprint)
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
    async with _semaphore:
        try:
            if provider == "claude":
                output, resolved = await _claude_call(ctx, prompt, schema, system_prompt)
            else:
                from backend.core import codex_engine
                output, resolved = await codex_engine.call(ctx, prompt, schema, system_prompt)
        except EngineError as exc:
            _route_failure(ctx, exc)
            raise
    _record_success(ctx)
    return Structured(output, ctx, resolved_model=resolved)


# --- catalogs ---------------------------------------------------------------------

async def refresh_catalog(provider: str) -> list[dict]:
    """Fetch the provider's model list (no prompt spent) and cache it for
    the engine picker. Failures are recorded, not raised: the picker still
    offers a custom model id."""
    previous = _signature()
    try:
        if provider == "claude":
            catalog = await claude_cli.models()
        else:
            from backend.core import codex_cli
            catalog = await asyncio.to_thread(codex_cli.models)
    except asyncio.CancelledError:
        raise
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        _put(provider, "catalog_error", f"{type(exc).__name__}: {exc}")
        _announce(previous)
        return _get_json(provider, "catalog", None) or []
    _put_json(provider, "catalog", catalog)
    _put(provider, "catalog_error", None)
    _announce(previous)
    return catalog


# --- legacy state -------------------------------------------------------------------

_LEGACY_KEYS = ("engine_status", "pinned_model", "retry_at", "last_error",
                "usage_backoff_minutes")


def migrate_legacy_state() -> None:
    """Carry the single-engine state (before rev 6) over to Claude's keys,
    once. Idempotent: with no legacy keys left it does nothing."""
    legacy = {key: db.get_state(key) for key in _LEGACY_KEYS}
    if all(value is None for value in legacy.values()):
        return
    status = legacy["engine_status"]
    pinned = legacy["pinned_model"]
    if pinned and valid_model_id(pinned):
        _put("claude", "auto_model", pinned)
        _set_model_state("claude", pinned, {
            "status": "ok", "last_error": None, "verified_at": None})
    if status in ("auth_required", "config_error"):
        _put("claude", "status", status)
        _put("claude", "last_error", legacy["last_error"])
    elif status == "waiting_usage_exhausted" or legacy["retry_at"]:
        _put("claude", "status", "waiting_usage_exhausted")
        _put("claude", "episode", uuid.uuid4().hex)
        _put("claude", "next_retry_at", legacy["retry_at"])
        _put("claude", "backoff_minutes", legacy["usage_backoff_minutes"])
        _put("claude", "last_error", legacy["last_error"])
    elif legacy["last_error"]:
        _put("claude", "last_error", legacy["last_error"])
    _bump("claude")
    for key in _LEGACY_KEYS:
        db.delete_state(key)
