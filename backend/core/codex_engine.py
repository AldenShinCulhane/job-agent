"""Codex engine: one locked-down `codex exec` per call (PLAN.md §4.1a).

The guarantee is that no executable or side-effect-capable tool is ever
offered to the model. Codex is an agent with shell, patch, web and image
tools, and job postings are untrusted text, so this module is the security
boundary for the Codex engine:

- One launch builder produces every invocation, production and
  verification alike. It disables every tool-bearing feature and web
  search, reads the model from a catalog entry the app rewrites (no patch
  tool, no skills list, no code or multi-agent mode, the app's own
  instructions instead of any of Codex's), and leaves the environment
  context out of the prompt.
- Before a configuration is used, `verify` runs that builder against a
  localhost-only stand-in for the Responses API and asserts the exact
  request Codex would send: the chosen model, the app's instructions, the
  user message only with no earlier response or stored conversation, an
  explicit `store: false`, no unknown top-level request fields, and no tool
  beyond `request_user_input` (no file, process or network access; exec
  mode rejects it). It then proves the loop guard: a model
  that insists on calling a tool is stopped at its first refusal. A pass
  is bound to a fingerprint of the executable (path and content hash), the
  launch arguments, the catalog entry and the environment allowlist; any
  change needs a new pass. Nothing is sent upstream by either check. A
  model whose request fails the check is unavailable on its own; a Codex
  that will not run at all is a problem for the whole provider.
- At run time the process gets an empty working directory, a read-only
  sandbox, an ephemeral session, closed stdin, an allowlisted environment
  and a wall-clock limit that kills its process group. Lifecycle,
  reasoning and answer events are normal; error events are routed by type;
  any other item is a lockdown breach. Codex rejects calls to tools it does
  not offer, but it does not stop a model that keeps trying (4,420 requests
  in 25 s against a mock), so the first refusal it logs ends the call.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema

from backend.core import codex_cli, db, llm

BASE_INSTRUCTIONS = (
    "You are the reasoning engine inside a job-application assistant. The "
    "user message holds the task's instructions and its inputs. Answer only "
    "with JSON matching the requested schema. You have no tools; never call one."
)

# Every Codex feature that can hand the model a tool, an agent, a plugin
# or outside content. Disabled here; `verify` proves the result.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "goals", "tool_suggest", "apps", "plugins",
    "multi_agent", "memories", "browser_use", "computer_use",
    "image_generation", "skill_search", "view_image", "hooks",
)

CONFIG_OVERRIDES = (
    'web_search="disabled"',
    "include_collaboration_mode_instructions=false",
    "include_permissions_instructions=false",
    "include_environment_context=false",
    "include_apps_instructions=false",
    "skills.include_instructions=false",
    codex_cli.FORCE_CHATGPT,
)

# The one tool Codex still declares. No file, process or network access,
# and exec mode rejects a call to it.
ALLOWED_TOOLS = frozenset({"request_user_input"})

# Every top-level field a locked request may carry; `check_request` bounds
# each one's value. Any other field fails the check: a new Codex build
# could use it to add context or a capability this check never reasoned
# about.
REQUEST_FIELDS = frozenset({
    "model", "instructions", "input", "tools", "tool_choice",
    "parallel_tool_calls", "reasoning", "store", "stream", "include", "text",
    "prompt_cache_key", "client_metadata",
})
# Fields that would bring in context from outside this one prompt: an
# earlier response, a stored conversation, a stored prompt template.
CONTEXT_FIELDS = ("previous_response_id", "conversation", "prompt")

NORMAL_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed"})
ANSWER_ITEMS = frozenset({"agent_message", "reasoning"})

# What Codex logs when it refuses a tool call.
TOOL_REFUSAL_MARK = "codex_core::tools::router"

PROBE_TEXT = "Reply with ok."
PROBE_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"], "additionalProperties": False}

VERIFY_TIMEOUT_S = 90.0
# Requests the stand-in may see before the loop guard has stopped a model
# that keeps calling a tool: the first answer, and the few Codex sends
# while it is being killed.
LOOP_GUARD_MAX_REQUESTS = 8
CALL_TIMEOUT_S = {None: 300.0, "low": 180.0, "medium": 300.0, "high": 900.0,
                  "xhigh": 1200.0, "max": 1200.0}
_STDOUT_LIMIT = 32 * 1024 * 1024
_CATALOG_TTL_S = 600.0
_VERIFIED_KEY = "engine.codex.lockdown"

# OpenAI strict structured output accepts only these schema keywords; the
# rest are dropped from the copy sent to Codex, and every answer is still
# validated against the full schema here.
_WIRE_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "anyOf", "const", "description", "$ref", "$defs", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems",
})


@dataclass(frozen=True)
class Launch:
    """Everything but the prompt."""

    argv: tuple[str, ...]
    env: dict[str, str]


# --- catalog and fingerprint ------------------------------------------------------

_catalog_cache: dict[str, tuple[float, str, list[dict]]] = {}
_binary_hashes: dict[tuple[str, int, int], str] = {}


def _binary(required: bool = True) -> Path | None:
    exe = codex_cli.binary()
    if exe is None and required:
        raise llm.ConfigError(codex_cli.INSTALL_HINT)
    return exe


def binary_identity(exe: Path) -> str:
    """Path and content hash, cached while size and mtime are unchanged."""
    stat = exe.stat()
    key = (str(exe), stat.st_size, stat.st_mtime_ns)
    digest = _binary_hashes.get(key)
    if digest is None:
        hasher = hashlib.sha256()
        with open(exe, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _binary_hashes[key] = digest
    return f"{exe}@sha256:{digest}"


async def _catalog_entry(exe: Path, model: str) -> dict:
    """The model's raw catalog entry, cached per Codex build for a few
    minutes: every call needs it for the fingerprint."""
    identity = await asyncio.to_thread(binary_identity, exe)
    cached = _catalog_cache.get(identity)
    if cached and time.monotonic() - cached[0] < _CATALOG_TTL_S:
        entries = cached[2]
    else:
        try:
            entries = await asyncio.to_thread(codex_cli.raw_catalog)
        except (OSError, RuntimeError, ValueError) as exc:
            raise llm.ConfigError(f"could not read Codex's model catalog: {exc}") from exc
        _catalog_cache[identity] = (time.monotonic(), identity, entries)
    for entry in entries:
        if entry.get("slug") == model:
            return entry
    raise llm.ModelNotEntitled(f"{model} is not in Codex's model catalog")


# Catalog fields that switch a model into a mode with tools or agents of
# its own. "code_mode_only" declares a JavaScript `exec` tool (nested tools
# on a global object) inside the request input instead of `tools`; the
# multi-agent fields add sub-agent roles; the "lite" request shape moves
# the instructions into developer messages. Newer models ship with them
# set (codex-cli 0.156.0: gpt-6-astra, gpt-5.6-*). `verify` proves what
# is left.
_AGENT_MODE_FIELDS = ("tool_mode", "multi_agent_version", "multi_agent_reasoning_effort")


def locked_entry(entry: dict) -> dict:
    """The model's catalog entry with every tool-bearing field switched off
    and Codex's own prompt text replaced by the app's instructions alone."""
    locked = json.loads(json.dumps(entry))
    locked["apply_patch_tool_type"] = None
    locked["input_modalities"] = ["text"]
    locked["experimental_supported_tools"] = []
    for flag in ("include_skills_usage_instructions",
                 "include_plugin_usage_instructions",
                 "include_apps_usage_instructions"):
        locked[flag] = False
    for field in _AGENT_MODE_FIELDS:
        locked.pop(field, None)
    locked["use_responses_lite"] = False
    locked["base_instructions"] = BASE_INSTRUCTIONS
    # Every other message Codex would add for the model (persistent mode,
    # approvals, permissions, multi-agent roles, ...) is left out.
    locked["model_messages"] = {"instructions_template": BASE_INSTRUCTIONS}
    return locked


def _catalog_file(locked: dict) -> Path:
    """Content-addressed, so a file on disk always matches its hash."""
    payload = json.dumps({"models": [locked]}, sort_keys=True)
    folder = codex_cli.home() / "catalog"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (hashlib.sha256(payload.encode()).hexdigest()[:32] + ".json")
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload)
        tmp.replace(path)
    return path


def _toml(value: str) -> str:
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(value)


def build_launch(exe: Path, model: str, effort: str | None, *,
                 catalog_path: Path, schema_path: Path, output_path: Path,
                 provider: tuple[str, str] | None = None) -> Launch:
    """The single builder for every Codex invocation. `provider` points the
    same launch at a localhost stand-in; it is used only by `verify` and
    the tests."""
    argv = [str(exe), "exec", "--skip-git-repo-check", "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
            "--model", model, "--json", "--color", "never",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path)]
    for feature in DISABLED_FEATURES:
        argv += ["--disable", feature]
    for override in CONFIG_OVERRIDES:
        argv += ["-c", override]
    argv += ["-c", f"model_catalog_json={_toml(str(catalog_path))}"]
    if effort:
        argv += ["-c", f"model_reasoning_effort={_toml(effort)}"]
    if provider is not None:
        name, url = provider
        argv += ["-c", (f"model_providers.{name}={{name={_toml(name)}, "
                        f"base_url={_toml(url)}, wire_api=\"responses\", "
                        "request_max_retries=0, stream_max_retries=0}"),
                 "-c", f"model_provider={_toml(name)}"]
    # Everything after this is the prompt, never an option.
    argv.append("--")
    return Launch(tuple(argv), codex_cli.spawn_env())


def fingerprint(exe: Path, model: str, locked: dict) -> str:
    template = build_launch(exe, model, "<effort>", catalog_path=Path("<catalog>"),
                            schema_path=Path("<schema>"), output_path=Path("<output>"))
    env_shape = {name: (value if name == "CODEX_HOME" else "<set>")
                 for name, value in sorted(template.env.items())}
    payload = json.dumps({"binary": binary_identity(exe), "argv": template.argv[1:],
                          "catalog": locked, "env": env_shape}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _verified() -> dict:
    raw = db.get_state(_VERIFIED_KEY)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _store_verified(model: str, record: dict | None) -> None:
    verified = _verified()
    if record is None:
        verified.pop(model, None)
    else:
        verified[model] = record
    db.set_state(_VERIFIED_KEY, json.dumps(verified, sort_keys=True))


# --- running codex ------------------------------------------------------------------

def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def classify_event(event: dict) -> str:
    """normal / answer / error / breach."""
    kind = event.get("type")
    if kind in NORMAL_EVENTS:
        return "normal"
    if kind in ("error", "turn.failed"):
        return "error"
    if isinstance(kind, str) and kind.startswith("item."):
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            return "answer" if kind == "item.completed" else "normal"
        if item_type in ANSWER_ITEMS:
            return "normal"
        return "breach"
    # A lifecycle event this build of the adapter does not know: not an
    # item, so not something the model did. Ignored, never trusted.
    return "normal"


def _event_error(event: dict) -> str:
    if event.get("type") == "turn.failed":
        return str((event.get("error") or {}).get("message") or "turn failed")
    return str(event.get("message") or "codex error")


@dataclass
class RunState:
    returncode: int | None = None
    answer: str | None = None
    errors: list[str] | None = None
    breach: str | None = None
    refusal: str | None = None
    stderr_tail: deque | None = None


async def execute(launch: Launch, prompt: str, *, timeout: float) -> RunState:
    """Run one codex process to completion or until the guard stops it."""
    runs = codex_cli.home() / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    cwd = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    state = RunState(errors=[], stderr_tail=deque(maxlen=40))
    proc = await asyncio.create_subprocess_exec(
        *launch.argv, prompt, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd, env=launch.env, start_new_session=True, limit=_STDOUT_LIMIT)

    async def pump_stdout() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = classify_event(event)
            if kind == "breach" and state.breach is None:
                item = event.get("item") or {}
                state.breach = f"{event.get('type')}:{item.get('type')}"
                _kill(proc)
            elif kind == "answer":
                state.answer = str((event.get("item") or {}).get("text") or "")
            elif kind == "error":
                state.errors.append(_event_error(event))

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            state.stderr_tail.append(line)
            if TOOL_REFUSAL_MARK in line and state.refusal is None:
                state.refusal = line
                _kill(proc)

    try:
        await asyncio.wait_for(asyncio.gather(pump_stdout(), pump_stderr(), proc.wait()),
                               timeout)
    except asyncio.TimeoutError as exc:
        _kill(proc)
        raise llm.TransientFailure(f"Codex did not finish within {timeout:.0f}s") from exc
    finally:
        _kill(proc)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            pass
        shutil.rmtree(cwd, ignore_errors=True)
    state.returncode = proc.returncode
    return state


_RESET_AT = re.compile(
    r"try again at (?:([A-Z][a-z]{2}) (\d{1,2})(?:st|nd|rd|th)?, (\d{4}) )?"
    r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)


def parse_codex_reset(text: str, now: datetime | None = None) -> datetime | None:
    """Codex names the reset only when the backend reported one, in local
    time: "… try again at Sep 23rd, 2026 1:00 AM." for another day, "… try
    again at 1:31 AM." for today. "try again later" is unknown and stays
    unknown, and so does a same-day time that has already passed."""
    match = _RESET_AT.search(text)
    if not match:
        return None
    month, day, year, hour, minute, half = match.groups()
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    try:
        clock = datetime.strptime(f"{hour}:{minute} {half.upper()}", "%I:%M %p").time()
        if month:
            date = datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()
        else:
            date = local_now.date()
    except ValueError:
        return None
    reset = datetime.combine(date, clock).astimezone()
    if not month and reset <= local_now:
        return None
    return reset.astimezone(timezone.utc)


def classify_error(text: str, returncode: int | None = None) -> llm.EngineError:
    lower = text.lower()
    if any(s in lower for s in ("usage limit", "rate limit", "429", "too many requests")):
        return llm.UsageExhausted(text, resets_at=parse_codex_reset(text))
    if any(s in lower for s in ("401", "unauthorized", "sign in", "signing in", "log in",
                                "not logged in", "token_expired", "refresh token",
                                "login")):
        return llm.AuthRequired(text)
    if any(s in lower for s in ("model_not_found", "does not exist", "unsupported model",
                                "not supported", "not available", "no access to model")):
        return llm.ModelNotEntitled(text)
    if returncode == 2 or any(s in lower for s in ("unexpected argument", "invalid value",
                                                    "error loading config", "unknown feature")):
        return llm.ConfigError(text)
    return llm.TransientFailure(text)


def wire_schema(schema: Any) -> Any:
    """The schema as sent to Codex: only keywords strict mode accepts."""
    if isinstance(schema, dict):
        out = {}
        for key, value in schema.items():
            if key not in _WIRE_KEYWORDS:
                continue
            if key in ("properties", "$defs"):
                out[key] = {name: wire_schema(sub) for name, sub in value.items()}
            elif key == "items":
                out[key] = wire_schema(value)
            elif key == "anyOf":
                out[key] = [wire_schema(sub) for sub in value]
            else:
                out[key] = value
        return out
    return schema


async def _call_once(ctx: llm.CallContext, prompt: str, schema: dict,
                     system_prompt: str) -> dict:
    exe = _binary()
    entry = await _catalog_entry(exe, ctx.model)
    locked = locked_entry(entry)
    current = await asyncio.to_thread(fingerprint, exe, ctx.model, locked)
    if ctx.fingerprint is not None and current != ctx.fingerprint:
        raise llm.ConfigError("Codex changed after its lockdown was verified; "
                              "re-check the engine")
    work = Path(tempfile.mkdtemp(prefix="call-", dir=codex_cli.home()))
    try:
        schema_path = work / "schema.json"
        schema_path.write_text(json.dumps(wire_schema(schema)))
        output_path = work / "answer.json"
        launch = build_launch(exe, ctx.model, ctx.effort,
                              catalog_path=_catalog_file(locked),
                              schema_path=schema_path, output_path=output_path)
        text = f"# Instructions\n{system_prompt}\n\n# Task\n{prompt}"
        state = await execute(launch, text,
                              timeout=CALL_TIMEOUT_S.get(ctx.effort, 600.0))
        if state.breach:
            raise llm.ConfigError(f"Codex lockdown breached: the model used {state.breach}")
        if state.refusal:
            raise llm.ToolCallBlocked(
                "the model tried to call a tool; Codex refused it and the call "
                "was stopped")
        if state.errors or state.returncode != 0:
            detail = state.errors[-1] if state.errors else "\n".join(
                list(state.stderr_tail)[-5:]) or f"codex exited {state.returncode}"
            raise classify_error(detail, state.returncode)
        answer = output_path.read_text() if output_path.exists() else state.answer
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        data = json.loads(answer or "")
    except ValueError as exc:
        raise llm.TransientFailure("Codex answer was not JSON") from exc
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise llm.TransientFailure(f"Codex answer failed the schema: {exc.message}") from exc
    return data


async def call(ctx: llm.CallContext, prompt: str, schema: dict,
               system_prompt: str) -> tuple[dict, str | None]:
    """Structured call with bounded retries on transient failures."""
    for attempt in range(llm._RETRY_ATTEMPTS):
        try:
            return await _call_once(ctx, prompt, schema, system_prompt), None
        except llm.TransientFailure:
            if attempt == llm._RETRY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(llm._RETRY_BASE_SECONDS * (2 ** attempt))
    raise llm.TransientFailure("retry loop fell through")  # unreachable


# --- lockdown verification ------------------------------------------------------------

class LocalResponses:
    """A localhost-only stand-in for the Responses API.

    mode "capture": record each request and refuse it.
    mode "tool_call": answer every request with a forced call to `tool`.
    mode "answer": answer with `answer` (a JSON text) as the final message.
    """

    def __init__(self, mode: str, *, tool: str = "request_user_input",
                 answer: str = '{"ok": true}'):
        self.mode = mode
        self.tool = tool
        self.answer = answer
        self.requests: list[dict] = []
        self.url = ""

    async def __aenter__(self) -> "LocalResponses":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/v1"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode(errors="replace").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            body = await reader.readexactly(length) if length else b""
            try:
                self.requests.append(json.loads(body or b"{}"))
            except ValueError:
                self.requests.append({"_unparsed": len(body)})
            if self.mode == "capture":
                payload = b'{"error":{"message":"lockdown check","type":"invalid_request_error"}}'
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
                             b"Content-Length: " + str(len(payload)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + payload)
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
                n = len(self.requests)
                if self.mode == "tool_call":
                    item = self._tool_item()
                else:
                    item = {"type": "message", "role": "assistant", "id": f"msg_{n}",
                            "content": [{"type": "output_text", "text": self.answer}]}
                usage = {"input_tokens": 1, "input_tokens_details": None,
                         "output_tokens": 1, "output_tokens_details": None,
                         "total_tokens": 2}
                for event in (
                    {"type": "response.created", "response": {"id": f"resp_{n}"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed",
                     "response": {"id": f"resp_{n}", "usage": usage}},
                ):
                    writer.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    def _tool_item(self) -> dict:
        if self.tool == "apply_patch":
            return {"type": "custom_tool_call", "id": "ct_1", "call_id": "call_1",
                    "name": "apply_patch", "input": self.patch_input}
        return {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": self.tool, "arguments": json.dumps(self.arguments)}

    # Tests point these at a sentinel path to prove nothing ran.
    arguments: dict = {"questions": [{"id": "q", "header": "Q", "question": "Pick?",
                                      "options": [{"label": "A", "description": "a"},
                                                  {"label": "B", "description": "b"}]}]}
    patch_input = "*** Begin Patch\n*** End Patch\n"


def check_request(body: dict, model: str) -> list[str]:
    """What is wrong with a captured request for `model`, empty when it is
    exactly the locked shape: the chosen model, the app's instructions, the
    prompt alone with nothing carried over, no tool beyond the allowlist,
    an explicit `store: false`, and the app's JSON schema as the answer
    format."""
    problems = []
    carried = [name for name in CONTEXT_FIELDS if name in body]
    if carried:
        problems.append(f"carries context from outside the prompt: {carried}")
    unexpected = sorted(set(body) - REQUEST_FIELDS - set(CONTEXT_FIELDS))
    if unexpected:
        problems.append(f"unexpected request fields: {unexpected}")
    if body.get("model") != model:
        problems.append(f"asks for model {body.get('model')!r}, not the chosen {model!r}")
    if body.get("store") is not False:
        # Unset means the default, which stores the response for later
        # retrieval. False asks the service not to; a captured request
        # shows only that, not what else the service retains.
        problems.append("store is not explicitly false")
    if body.get("instructions") != BASE_INSTRUCTIONS:
        problems.append("instructions are not the app's")
    tools = sorted(str(t.get("name") or t.get("type")) for t in body.get("tools") or [])
    extra = sorted(set(tools) - ALLOWED_TOOLS)
    if extra:
        problems.append(f"tools beyond the allowlist: {extra}")
    if body.get("tool_choice", "auto") not in ("auto", "none"):
        problems.append("tool_choice is not auto or none")
    items = body.get("input") or []
    if (len(items) != 1 or not isinstance(items[0], dict)
            or items[0].get("type") != "message" or items[0].get("role") != "user"):
        problems.append("input is not the user message alone: "
                        f"{[(i.get('type'), i.get('role')) for i in items if isinstance(i, dict)]}")
    else:
        texts = [c.get("text") for c in items[0].get("content") or [] if isinstance(c, dict)]
        if texts != [PROBE_TEXT]:
            problems.append("the user message carries more than the prompt")
    include = body.get("include") or []
    if not isinstance(include, list) or set(include) - {"reasoning.encrypted_content"}:
        problems.append(f"include asks for more than the encrypted reasoning: {include}")
    answer_format = (body.get("text") or {}).get("format") or {}
    if (answer_format.get("type") != "json_schema"
            or answer_format.get("schema") != PROBE_SCHEMA):
        problems.append("the answer format is not the app's JSON schema")
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict) or set(reasoning) - {"effort", "summary"}:
        problems.append("reasoning carries more than effort and summary: "
                        f"{sorted(reasoning) if isinstance(reasoning, dict) else reasoning}")
    metadata = body.get("client_metadata") or {}
    if not isinstance(metadata, dict) or not all(
            isinstance(v, str) for v in metadata.values()):
        problems.append("client_metadata is not flat text")
    return problems


async def _check_launch(exe: Path, model: str, locked: dict, mode: str) -> LocalResponses:
    work = Path(tempfile.mkdtemp(prefix="verify-", dir=codex_cli.home()))
    try:
        schema_path = work / "schema.json"
        schema_path.write_text(json.dumps(PROBE_SCHEMA))
        async with LocalResponses(mode) as server:
            launch = build_launch(exe, model, None, catalog_path=_catalog_file(locked),
                                  schema_path=schema_path, output_path=work / "answer.json",
                                  provider=("lockdown-check", server.url))
            state = await execute(launch, PROBE_TEXT, timeout=VERIFY_TIMEOUT_S)
        server.state = state  # type: ignore[attr-defined]
        return server
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stderr_note(server: "LocalResponses") -> str:
    state = getattr(server, "state", None)
    tail = list(state.stderr_tail or [])[-3:] if state is not None else []
    return (": " + " | ".join(tail)[:500]) if tail else ""


async def verify(model: str) -> str:
    """Prove the locked launch for `model`; returns its fingerprint."""
    exe = _binary()
    entry = await _catalog_entry(exe, model)
    locked = locked_entry(entry)
    fp = await asyncio.to_thread(fingerprint, exe, model, locked)
    record = _verified().get(model)
    if record and record.get("fingerprint") == fp:
        return fp

    # A launch that sends nothing means Codex itself would not run: a
    # problem for every model, so the provider's. Anything wrong with the
    # request it sends, or with how it handles a forced tool call, follows
    # from this model's entry: the model is unavailable, the rest stay
    # usable.
    captured = await _check_launch(exe, model, locked, "capture")
    if not captured.requests:
        raise llm.ConfigError("Codex lockdown check: Codex sent no request"
                              + _stderr_note(captured))
    if len(captured.requests) != 1:
        raise llm.ModelUnavailable(f"Codex lockdown check for {model}: expected one "
                                   f"request, saw {len(captured.requests)}")
    problems = check_request(captured.requests[0], model)
    if problems:
        raise llm.ModelUnavailable(f"Codex lockdown check failed for {model}: "
                                   + "; ".join(problems))

    guarded = await _check_launch(exe, model, locked, "tool_call")
    state = guarded.state  # type: ignore[attr-defined]
    if not guarded.requests:
        raise llm.ConfigError("Codex lockdown check: Codex sent no request"
                              + _stderr_note(guarded))
    if state.refusal is None:
        raise llm.ModelUnavailable(f"Codex lockdown check for {model}: a forced tool "
                                   "call was not detected, so a tool loop could not "
                                   "be stopped")
    if len(guarded.requests) > LOOP_GUARD_MAX_REQUESTS:
        raise llm.ModelUnavailable(f"Codex lockdown check for {model}: the loop guard "
                                   f"let {len(guarded.requests)} requests through")

    _store_verified(model, {"fingerprint": fp,
                            "verified_at": datetime.now(timezone.utc).isoformat(),
                            "binary": str(exe)})
    return fp


_verifications: dict[str, asyncio.Task] = {}


async def ensure_verified(model: str) -> str:
    """The current fingerprint for `model`, verifying first when needed.
    One verification at a time per model."""
    loop = asyncio.get_running_loop()
    task = _verifications.get(model)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(verify(model))
        _verifications[model] = task
    return await asyncio.shield(task)


async def probe(ctx: llm.CallContext) -> None:
    """Verify the lockdown, then prove the model answers."""
    fp = await ensure_verified(ctx.model)
    try:
        await _call_once(replace(ctx, fingerprint=fp), PROBE_TEXT, PROBE_SCHEMA,
                         "Return ok=true.")
    except llm.ToolCallBlocked as exc:
        # A fixed, benign prompt should never provoke a tool call.
        raise llm.TransientFailure(str(exc)) from exc
