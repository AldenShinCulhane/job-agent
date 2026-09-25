"""The Codex engine's lockdown (PLAN.md §4.1a, P6a).

Two tiers. The first runs everywhere: request and event checks, error
routing, the schema sent to Codex, the fingerprint a verification is bound
to, and the process guard, driven through a stand-in `codex` executable
written in Python. The second runs the real Codex CLI, but only against
localhost stand-ins for the Responses API: nothing is sent upstream and no
subscription is spent. It is skipped when Codex is not installed, unless
CODEX_TESTS_REQUIRED=1 makes it a gate.

The adversarial cases force every tool Codex could offer — shell,
exec_command, apply_patch, request_user_input — with arguments that would
create a sentinel file if anything ran.
"""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core import codex_cli, codex_engine, db, llm
from backend.tests import engine_fakes

REQUIRED = os.environ.get("CODEX_TESTS_REQUIRED") == "1"
RESETS_AT = int(time.time()) + 5400

MODEL = "gpt-test"
ENTRY = {
    "slug": MODEL, "display_name": "GPT Test", "visibility": "list",
    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
    "apply_patch_tool_type": "freeform",
    "experimental_supported_tools": ["some_tool"],
    "input_modalities": ["text", "image"],
    "include_skills_usage_instructions": True,
    "base_instructions": "Codex's own system prompt",
    "model_messages": {"instructions_template": "Codex's own template",
                       "persistent_instructions": "Codex's persistent mode",
                       "multi_agent": {"role": "Codex's sub-agent roles"},
                       "approvals": "Codex's approval policy"},
    # The code and multi-agent modes newer Codex models ship with.
    "tool_mode": "code_mode_only",
    "multi_agent_version": "v2",
    "multi_agent_reasoning_effort": "xhigh",
    "use_responses_lite": True,
}
SCHEMA = {"type": "object", "properties": {"x": {"type": "string", "minLength": 3}},
          "required": ["x"], "additionalProperties": False}

QUOTA_TEXT = ("You’ve hit your usage limit. Upgrade to Pro "
              "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/"
              "settings/usage to purchase more credits or try again at "
              "Sep 23rd, 2026 1:00 AM.")
AUTH_TEXT = ("unexpected status 401 Unauthorized: Your authentication token has "
             "expired. Please try signing in again., url: http://127.0.0.1:1/v1/responses")
NOT_FOUND_TEXT = json.dumps({"error": {
    "type": "invalid_request_error", "code": "model_not_found",
    "message": "The requested model 'gpt-9' does not exist."}})


@pytest.fixture()
def home(monkeypatch, tmp_path):
    """An isolated app data dir and Codex home; engine state in a temp DB."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    monkeypatch.setattr(codex_cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(codex_cli, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(codex_engine, "_catalog_cache", {})
    monkeypatch.setattr(codex_engine, "_verifications", {})
    monkeypatch.setattr(llm, "_RETRY_BASE_SECONDS", 0.001)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "must-not-reach-codex")
    return tmp_path


# --- a stand-in codex executable -------------------------------------------------

FAKE_CODEX = '''#!{python}
import json, os, subprocess, sys, time
argv = sys.argv[1:]
plan = json.load(open({plan!r}))

def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None

if plan.get("report"):
    schema = opt("--output-schema")
    json.dump({{"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(),
               "cwd_entries": os.listdir("."), "stdin": sys.stdin.read(),
               "schema": open(schema).read() if schema else None}},
              open(plan["report"], "w"))
for line in plan.get("stderr", []):
    print(line, file=sys.stderr, flush=True)
for event in plan.get("events", []):
    print(json.dumps(event), flush=True)
if plan.get("answer") is not None:
    open(opt("--output-last-message"), "w").write(plan["answer"])
if plan.get("grandchild"):
    child = subprocess.Popen([sys.executable, "-c",
        "import sys, time; time.sleep(1); open(sys.argv[1], 'w').write('ran')",
        plan["grandchild"]])
    open(plan["grandchild"] + ".pid", "w").write(str(child.pid))
if plan.get("sentinel"):
    time.sleep(1)
    open(plan["sentinel"], "w").write("ran")
time.sleep(plan.get("linger", 0))
sys.exit(plan.get("exit", 0))
'''

LIFECYCLE = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]


def answer_events(text):
    return [*LIFECYCLE,
            {"type": "item.completed", "item": {"type": "reasoning", "text": "hm"}},
            {"type": "item.completed", "item": {"id": "i", "type": "agent_message",
                                                "text": text}},
            {"type": "turn.completed", "usage": {}}]


def error_events(text):
    return [*LIFECYCLE, {"type": "error", "message": text},
            {"type": "turn.failed", "error": {"message": text}}]


@pytest.fixture()
def fake(home, monkeypatch):
    """A stand-in codex on the app's lookup path; `fake.plan(...)` sets
    what the next run does."""
    folder = home / "bin"
    folder.mkdir()
    plan_path = home / "plan.json"
    exe = folder / "codex"
    exe.write_text(FAKE_CODEX.format(python=sys.executable, plan=str(plan_path)))
    exe.chmod(0o700)
    monkeypatch.setattr(codex_cli, "binary", lambda: exe)
    monkeypatch.setattr(codex_cli, "raw_catalog", lambda: [ENTRY])

    def plan(**fields):
        plan_path.write_text(json.dumps(fields))

    plan()
    return SimpleNamespace(exe=exe, plan=plan, home=home)


def launch_for(exe):
    return codex_engine.Launch((str(exe),), codex_cli.spawn_env())


# --- request and event checks --------------------------------------------------------

DROP = object()
SAMPLE_ID = "00000000-0000-4000-8000-000000000000"


def locked_request(**overrides):
    """The request the lockdown check expects: every field the real Codex
    sends (codex-cli 0.156.0), with sample ids. `DROP` removes a field."""
    body = {
        "model": MODEL,
        "instructions": codex_engine.BASE_INSTRUCTIONS,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": codex_engine.PROBE_TEXT}]}],
        "tools": [{"type": "function", "name": "request_user_input"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": "medium"},
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "text": {"verbosity": "low",
                 "format": {"type": "json_schema", "strict": True,
                            "name": "codex_output_schema",
                            "schema": codex_engine.PROBE_SCHEMA}},
        "prompt_cache_key": SAMPLE_ID,
        "client_metadata": {"session_id": SAMPLE_ID, "turn_id": SAMPLE_ID},
    }
    for key, value in overrides.items():
        if value is DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def test_the_locked_request_shape_passes():
    assert codex_engine.check_request(locked_request(), MODEL) == []
    assert codex_engine.check_request(locked_request(tools=[]), MODEL) == []


@pytest.mark.parametrize("overrides, problem", [
    ({"instructions": "You are Codex, a coding agent."}, "instructions"),
    ({"tools": [{"type": "function", "name": "exec_command"}]}, "exec_command"),
    ({"tools": [{"type": "function", "name": "shell"}]}, "shell"),
    ({"tools": [{"type": "custom", "name": "apply_patch"}]}, "apply_patch"),
    ({"tools": [{"type": "web_search"}]}, "web_search"),
    ({"input": [{"type": "message", "role": "developer",
                 "content": [{"type": "input_text", "text": "<permissions>"}]},
                *locked_request()["input"]]}, "not the user message alone"),
    ({"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "# AGENTS.md instructions"},
        {"type": "input_text", "text": codex_engine.PROBE_TEXT}]}]}, "more than the prompt"),
    # Tools declared inside the input rather than in `tools`, as newer
    # Codex models do.
    ({"input": [{"type": "additional_tools", "role": "user",
                 "tools": [{"type": "custom", "name": "exec"}]}]},
     "not the user message alone"),
    ({"instructions": DROP}, "instructions"),
    # Context from outside this one prompt: an earlier response, a stored
    # conversation, a stored prompt template.
    ({"previous_response_id": "resp_earlier"}, "previous_response_id"),
    ({"conversation": {"id": "conv_earlier"}}, "conversation"),
    ({"prompt": {"id": "pmpt_stored"}}, "prompt"),
    # Kept by the service, so a later request could build on it.
    ({"store": True}, "store"),
    ({"store": DROP}, "store"),  # the service keeps responses by default
    # Not the model the user chose.
    ({"model": "gpt-other"}, "model"),
    ({"model": DROP}, "model"),
    # Anything this check has not reasoned about.
    ({"background": True}, "unexpected"),
    ({"some_future_field": {"x": 1}}, "unexpected"),
    ({"include": ["web_search_call.action.sources"]}, "include"),
    ({"text": {"format": {"type": "text"}}}, "schema"),
    ({"text": {"format": {"type": "json_schema", "strict": True, "name": "x",
                          "schema": {"type": "object"}}}}, "schema"),
    ({"tool_choice": {"type": "function", "name": "request_user_input"}}, "tool_choice"),
    ({"reasoning": {"effort": "low", "context": "all_turns"}}, "reasoning"),
    ({"client_metadata": {"session_id": {"nested": "value"}}}, "client_metadata"),
])
def test_any_deviation_from_the_locked_request_is_caught(overrides, problem):
    problems = codex_engine.check_request(locked_request(**overrides), MODEL)
    assert problems and any(problem in p for p in problems)


@pytest.mark.parametrize("event, kind", [
    ({"type": "thread.started"}, "normal"),
    ({"type": "turn.started"}, "normal"),
    ({"type": "turn.completed"}, "normal"),
    ({"type": "item.started", "item": {"type": "agent_message"}}, "normal"),
    ({"type": "item.completed", "item": {"type": "reasoning"}}, "normal"),
    ({"type": "item.completed", "item": {"type": "agent_message"}}, "answer"),
    ({"type": "error", "message": "x"}, "error"),
    ({"type": "turn.failed", "error": {"message": "x"}}, "error"),
    ({"type": "item.started", "item": {"type": "command_execution"}}, "breach"),
    ({"type": "item.completed", "item": {"type": "file_change"}}, "breach"),
    ({"type": "item.started", "item": {"type": "mcp_tool_call"}}, "breach"),
    ({"type": "item.started", "item": {"type": "web_search"}}, "breach"),
    ({"type": "item.updated", "item": {"type": "todo_list"}}, "breach"),
    ({"type": "item.started", "item": {"type": "something_new"}}, "breach"),
    ({"type": "some.future.lifecycle"}, "normal"),
])
def test_events_are_classified(event, kind):
    assert codex_engine.classify_event(event) == kind


# --- errors ---------------------------------------------------------------------

def test_a_reported_reset_is_read_as_local_time():
    reset = codex_engine.parse_codex_reset(QUOTA_TEXT)
    local = reset.astimezone()
    assert (local.year, local.month, local.day, local.hour, local.minute) == (
        2026, 9, 23, 1, 0)
    assert codex_engine.parse_codex_reset("You've hit your usage limit. Try again later.") is None


def test_a_same_day_reset_names_only_the_time():
    now = datetime(2026, 9, 23, 0, 30).astimezone()
    reset = codex_engine.parse_codex_reset(
        "You've hit your usage limit. … or try again at 1:31 AM.", now)
    local = reset.astimezone()
    assert (local.date(), local.hour, local.minute) == (now.date(), 1, 31)
    # A time already behind us says nothing about the next reset.
    assert codex_engine.parse_codex_reset("try again at 12:21 AM.", now) is None


@pytest.mark.parametrize("text, returncode, kind", [
    (QUOTA_TEXT, 1, llm.UsageExhausted),
    ("You've hit your usage limit. Try again later.", 1, llm.UsageExhausted),
    ("exceeded retry limit, last status: 429 Too Many Requests", 1, llm.UsageExhausted),
    (AUTH_TEXT, 1, llm.AuthRequired),
    ("Not logged in. Run codex login", 1, llm.AuthRequired),
    (NOT_FOUND_TEXT, 1, llm.ModelNotEntitled),
    ("error: unexpected argument '--frobnicate' found", 2, llm.ConfigError),
    ("Error loading config.toml: invalid type", 1, llm.ConfigError),
    ("stream disconnected before completion", 1, llm.TransientFailure),
])
def test_codex_errors_are_routed_by_type(text, returncode, kind):
    error = codex_engine.classify_error(text, returncode)
    assert type(error) is kind


def test_a_quota_error_carries_its_reset_only_when_reported():
    assert codex_engine.classify_error(QUOTA_TEXT).resets_at is not None
    assert codex_engine.classify_error("usage limit reached").resets_at is None


# --- the schema sent to Codex --------------------------------------------------------

def _keywords(schema, found):
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in ("properties", "$defs"):
                for sub in value.values():
                    _keywords(sub, found)
            else:
                found.add(key)
                _keywords(value, found)
    elif isinstance(schema, list):
        for sub in schema:
            _keywords(sub, found)
    return found


def _strict_objects(schema, where="$"):
    """Every object as OpenAI strict mode needs it: closed, and every
    property required."""
    problems = []
    if isinstance(schema, dict):
        if "properties" in schema:
            if schema.get("additionalProperties") is not False:
                problems.append(f"{where}: additionalProperties is not false")
            if sorted(schema.get("required") or []) != sorted(schema["properties"]):
                problems.append(f"{where}: not every property is required")
            for name, sub in schema["properties"].items():
                problems += _strict_objects(sub, f"{where}.{name}")
        for key in ("items", "anyOf", "$defs"):
            value = schema.get(key)
            subs = value.values() if isinstance(value, dict) and key == "$defs" else (
                value if isinstance(value, list) else [value])
            for i, sub in enumerate(subs):
                problems += _strict_objects(sub, f"{where}.{key}[{i}]")
    return problems


def app_schemas():
    from backend.normalize import requirements
    from backend.resume import schema as profile
    from backend.scoring import llm_layer
    from backend.tailor import page_limit, resume, validate

    return {"requirements": requirements.REQUIREMENTS_SCHEMA,
            "profile": profile.PROFILE_SCHEMA, "tailor": resume.TAILOR_SCHEMA,
            "condense": page_limit._CONDENSE_SCHEMA,
            "entailment": validate.ENTAILMENT_SCHEMA,
            "explain": llm_layer.EXPLAIN_SCHEMA, "rerank": llm_layer.RERANK_SCHEMA}


@pytest.mark.parametrize("name", sorted(app_schemas()))
def test_every_app_schema_goes_to_codex_in_strict_form(name):
    schema = app_schemas()[name]
    wire = codex_engine.wire_schema(schema)
    assert _keywords(wire, set()) <= codex_engine._WIRE_KEYWORDS
    assert _strict_objects(wire) == []
    assert wire.get("required") == schema.get("required")
    assert set(wire.get("properties", {})) == set(schema.get("properties", {}))


def test_property_names_are_never_mistaken_for_keywords():
    schema = {"type": "object", "properties": {
        "format": {"type": "string", "minLength": 1},
        "uniqueItems": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}},
        "required": ["format", "uniqueItems"], "additionalProperties": False}
    wire = codex_engine.wire_schema(schema)
    assert set(wire["properties"]) == {"format", "uniqueItems"}
    assert wire["properties"]["format"] == {"type": "string"}
    assert wire["properties"]["uniqueItems"] == {"type": "array", "items": {"type": "string"}}


# --- launch, environment, catalog -------------------------------------------------------

def test_one_launch_builder_locks_every_invocation(home):
    exe = Path("/opt/codex")
    launch = codex_engine.build_launch(
        exe, MODEL, "medium", catalog_path=Path("/c.json"), schema_path=Path("/s.json"),
        output_path=Path("/o.json"))
    argv = list(launch.argv)
    assert argv[-1] == "--"  # the prompt can never be read as an option
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules",
                 "--skip-git-repo-check", "--json"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert disabled == set(codex_engine.DISABLED_FEATURES)
    overrides = {argv[i + 1] for i, a in enumerate(argv) if a == "-c"}
    assert set(codex_engine.CONFIG_OVERRIDES) <= overrides
    assert 'forced_login_method="chatgpt"' in overrides
    assert not any(o.startswith(("model_provider", "model_providers")) for o in overrides)


def test_codex_sees_only_the_allowlisted_environment(home):
    env = codex_cli.spawn_env()
    assert set(env) <= set(codex_cli.ENV_ALLOWLIST) | {"CODEX_HOME"}
    assert env["CODEX_HOME"] == str(home / "codex")
    assert oct((home / "codex").stat().st_mode & 0o777) == "0o700"


def test_the_catalog_entry_is_rewritten_without_tools_or_codexs_prompt():
    locked = codex_engine.locked_entry(ENTRY)
    assert locked["apply_patch_tool_type"] is None
    assert locked["experimental_supported_tools"] == []
    assert locked["input_modalities"] == ["text"]
    assert locked["include_skills_usage_instructions"] is False
    assert locked["base_instructions"] == codex_engine.BASE_INSTRUCTIONS
    # The app's instructions are the only prompt text the entry carries.
    assert locked["model_messages"] == {"instructions_template": codex_engine.BASE_INSTRUCTIONS}
    # No code mode, no sub-agents, and the request shape with the
    # instructions in their own field.
    for field in ("tool_mode", "multi_agent_version", "multi_agent_reasoning_effort"):
        assert field not in locked
    assert locked["use_responses_lite"] is False
    assert ENTRY["apply_patch_tool_type"] == "freeform"  # the source is untouched
    assert ENTRY["tool_mode"] == "code_mode_only"


def test_the_picker_never_offers_hidden_models_or_delegation(monkeypatch):
    monkeypatch.setattr(codex_cli, "raw_catalog", lambda: [
        {"slug": "b", "visibility": "list", "priority": 2,
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]},
        {"slug": "hidden", "visibility": "hide", "priority": 0},
        {"slug": "a", "visibility": "list", "priority": 1, "display_name": "A"},
    ])
    assert codex_cli.models() == [
        {"id": "a", "label": "A", "description": "", "efforts": []},
        {"id": "b", "label": "b", "description": "", "efforts": ["low"]},
    ]


# --- the fingerprint a verification is bound to ----------------------------------------

def test_the_fingerprint_moves_with_everything_it_binds(fake, monkeypatch):
    locked = codex_engine.locked_entry(ENTRY)
    base = codex_engine.fingerprint(fake.exe, MODEL, locked)
    assert codex_engine.fingerprint(fake.exe, MODEL, locked) == base  # stable

    changed = {**locked, "experimental_supported_tools": ["shell"]}
    assert codex_engine.fingerprint(fake.exe, MODEL, changed) != base
    assert codex_engine.fingerprint(fake.exe, "gpt-other", locked) != base

    with monkeypatch.context() as m:
        m.setattr(codex_engine, "DISABLED_FEATURES", codex_engine.DISABLED_FEATURES[1:])
        assert codex_engine.fingerprint(fake.exe, MODEL, locked) != base
    with monkeypatch.context() as m:
        m.setattr(codex_cli, "ENV_ALLOWLIST", (*codex_cli.ENV_ALLOWLIST, "OPENAI_API_KEY"))
        assert codex_engine.fingerprint(fake.exe, MODEL, locked) != base
    with monkeypatch.context() as m:
        m.setattr(codex_cli, "CODEX_HOME", fake.home / "elsewhere")
        assert codex_engine.fingerprint(fake.exe, MODEL, locked) != base

    fake.exe.write_text(fake.exe.read_text() + "\n# a different build\n")
    assert codex_engine.fingerprint(fake.exe, MODEL, locked) != base


class Checks:
    """Stands in for the two stand-in runs verify() makes."""

    def __init__(self, monkeypatch):
        self.runs: list[str] = []
        self.captured = [locked_request()]
        self.refusal: str | None = "ERROR codex_core::tools::router: error=unsupported"
        self.guard_requests = 1

        async def check_launch(exe, model, locked, mode):
            self.runs.append(mode)
            if mode == "capture":
                return SimpleNamespace(requests=list(self.captured))
            return SimpleNamespace(requests=[{}] * self.guard_requests,
                                   state=codex_engine.RunState(refusal=self.refusal))

        monkeypatch.setattr(codex_engine, "_check_launch", check_launch)


async def test_a_verification_is_reused_until_something_it_bound_changes(fake, monkeypatch):
    checks = Checks(monkeypatch)
    first = await codex_engine.verify(MODEL)
    assert checks.runs == ["capture", "tool_call"]
    assert await codex_engine.verify(MODEL) == first
    assert checks.runs == ["capture", "tool_call"]  # nothing re-run

    # Codex updated its catalog entry for the model.
    monkeypatch.setattr(codex_engine, "_catalog_cache", {})
    monkeypatch.setattr(codex_cli, "raw_catalog",
                        lambda: [{**ENTRY, "context_window": 12345}])
    second = await codex_engine.verify(MODEL)
    assert second != first and len(checks.runs) == 4

    fake.exe.write_text(fake.exe.read_text() + "\n# updated\n")
    third = await codex_engine.verify(MODEL)
    assert third not in (first, second) and len(checks.runs) == 6


MODEL_ONLY = llm.ModelUnavailable


@pytest.mark.parametrize("setup, message, scope", [
    # What Codex sends for this model, or does with a forced tool call:
    # the model is unavailable, the provider is not broken.
    (lambda c: setattr(c, "captured", [locked_request(tools=[
        {"type": "function", "name": "exec_command"}])]), "tools beyond the allowlist",
     MODEL_ONLY),
    (lambda c: setattr(c, "captured", [locked_request(), locked_request()]),
     "expected one request", MODEL_ONLY),
    (lambda c: setattr(c, "captured", [locked_request(previous_response_id="resp_1")]),
     "previous_response_id", MODEL_ONLY),
    (lambda c: setattr(c, "captured", [locked_request(model="gpt-other")]),
     "not the chosen", MODEL_ONLY),
    (lambda c: setattr(c, "refusal", None), "was not detected", MODEL_ONLY),
    (lambda c: setattr(c, "guard_requests", codex_engine.LOOP_GUARD_MAX_REQUESTS + 1),
     "loop guard let", MODEL_ONLY),
    # Codex sent nothing at all: it would not run for any model.
    (lambda c: setattr(c, "captured", []), "sent no request", llm.ConfigError),
    (lambda c: setattr(c, "guard_requests", 0), "sent no request", llm.ConfigError),
])
async def test_a_failed_verification_is_a_config_error_and_never_stored(
        fake, monkeypatch, setup, message, scope):
    checks = Checks(monkeypatch)
    setup(checks)
    with pytest.raises(llm.ConfigError, match=message) as caught:
        await codex_engine.verify(MODEL)
    assert isinstance(caught.value, llm.ModelUnavailable) == (scope is MODEL_ONLY)
    assert codex_engine._verified() == {}


async def test_concurrent_verifications_are_coalesced(fake, monkeypatch):
    checks = Checks(monkeypatch)
    results = await asyncio.gather(codex_engine.ensure_verified(MODEL),
                                   codex_engine.ensure_verified(MODEL))
    assert results[0] == results[1]
    assert checks.runs == ["capture", "tool_call"]


# --- the process guard, on a stand-in executable ------------------------------------------

async def test_a_normal_run_reads_the_answer(fake):
    fake.plan(events=answer_events('{"x": "yes"}'))
    state = await codex_engine.execute(launch_for(fake.exe), "prompt", timeout=10)
    assert (state.returncode, state.answer, state.breach, state.refusal) == (
        0, '{"x": "yes"}', None, None)


async def test_a_run_gets_an_empty_private_folder_closed_stdin_and_a_clean_env(fake):
    report = fake.home / "report.json"
    fake.plan(report=str(report), events=answer_events("{}"))
    await codex_engine.execute(launch_for(fake.exe), "prompt", timeout=10)
    seen = json.loads(report.read_text())
    assert set(seen["env"]) - {"__CF_USER_TEXT_ENCODING"} <= (
        set(codex_cli.ENV_ALLOWLIST) | {"CODEX_HOME"})
    assert "OPENAI_API_KEY" not in seen["env"] and "CODEX_API_KEY" not in seen["env"]
    assert seen["stdin"] == ""
    assert seen["cwd_entries"] == []
    cwd = Path(seen["cwd"]).resolve()
    assert cwd.parent == (fake.home / "codex" / "runs").resolve()
    assert not cwd.exists()  # removed after the run


async def test_a_tool_item_is_a_breach_that_stops_the_process(fake):
    sentinel = fake.home / "sentinel"
    fake.plan(events=[*LIFECYCLE, {"type": "item.started", "item": {
        "type": "command_execution", "command": "touch sentinel"}}],
        sentinel=str(sentinel), linger=30)
    started = time.monotonic()
    state = await codex_engine.execute(launch_for(fake.exe), "prompt", timeout=30)
    assert time.monotonic() - started < 5
    assert state.breach == "item.started:command_execution"
    assert state.returncode == -signal.SIGKILL
    await asyncio.sleep(1.5)
    assert not sentinel.exists()


async def test_the_first_tool_refusal_stops_a_model_that_keeps_trying(fake):
    sentinel = fake.home / "sentinel"
    fake.plan(stderr=[f"2026-09-22T00:00:00Z ERROR {codex_engine.TOOL_REFUSAL_MARK}: "
                      "error=unsupported call: exec_command"],
              sentinel=str(sentinel), linger=30)
    started = time.monotonic()
    state = await codex_engine.execute(launch_for(fake.exe), "prompt", timeout=30)
    assert time.monotonic() - started < 5
    assert state.refusal and state.returncode == -signal.SIGKILL
    await asyncio.sleep(1.5)
    assert not sentinel.exists()


async def test_the_time_limit_kills_the_whole_process_group(fake):
    sentinel = fake.home / "grandchild-ran"
    fake.plan(grandchild=str(sentinel), linger=30)
    with pytest.raises(llm.TransientFailure, match="did not finish"):
        await codex_engine.execute(launch_for(fake.exe), "prompt", timeout=0.5)
    await asyncio.sleep(1.5)
    assert not sentinel.exists()
    pid = int(Path(str(sentinel) + ".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --- one call, on a stand-in executable ---------------------------------------------------

def context(fingerprint=None, effort="medium"):
    return llm.CallContext("codex", MODEL, effort, fingerprint=fingerprint)


async def test_a_call_refuses_to_run_a_codex_that_changed_since_verification(fake):
    report = fake.home / "report.json"
    fake.plan(report=str(report), events=answer_events('{"x": "abc"}'))
    with pytest.raises(llm.ConfigError, match="changed after its lockdown"):
        await codex_engine._call_once(context("stale-fingerprint"), "p", SCHEMA, "s")
    assert not report.exists()  # never started


async def test_a_call_sends_the_prompt_after_the_options_with_the_wire_schema(fake):
    report = fake.home / "report.json"
    fake.plan(report=str(report), answer='{"x": "abc"}',
              events=answer_events('{"x": "abc"}'))
    fp = codex_engine.fingerprint(fake.exe, MODEL, codex_engine.locked_entry(ENTRY))
    prompt = "--config model_provider=evil\nposting text"
    assert await codex_engine._call_once(context(fp), prompt, SCHEMA, "system") == {"x": "abc"}

    seen = json.loads(report.read_text())
    assert seen["argv"][-2] == "--"
    assert seen["argv"][-1] == f"# Instructions\nsystem\n\n# Task\n{prompt}"
    assert seen["argv"][seen["argv"].index("--model") + 1] == MODEL
    assert 'model_reasoning_effort="medium"' in seen["argv"]
    assert json.loads(seen["schema"]) == codex_engine.wire_schema(SCHEMA)
    assert "minLength" not in seen["schema"]
    # The per-call folder with the schema and answer files is gone.
    assert [p.name for p in (fake.home / "codex").iterdir()
            if p.name.startswith("call-")] == []


async def test_every_answer_is_checked_against_the_full_schema(fake):
    fake.plan(answer='{"x": "ab"}', events=answer_events('{"x": "ab"}'))
    with pytest.raises(llm.TransientFailure, match="failed the schema"):
        await codex_engine._call_once(context(), "p", SCHEMA, "s")
    fake.plan(answer="not json", events=answer_events("not json"))
    with pytest.raises(llm.TransientFailure, match="not JSON"):
        await codex_engine._call_once(context(), "p", SCHEMA, "s")


@pytest.mark.parametrize("text, kind", [
    (QUOTA_TEXT, llm.UsageExhausted),
    (AUTH_TEXT, llm.AuthRequired),
    (NOT_FOUND_TEXT, llm.ModelNotEntitled),
])
async def test_error_events_become_typed_engine_errors(fake, text, kind):
    fake.plan(events=error_events(text), exit=1)
    with pytest.raises(kind):
        await codex_engine._call_once(context(), "p", SCHEMA, "s")


async def test_a_breach_in_a_call_is_a_config_error_and_a_refusal_blocks_the_call(fake):
    fake.plan(events=[*LIFECYCLE, {"type": "item.started",
                                   "item": {"type": "file_change"}}], linger=30)
    with pytest.raises(llm.ConfigError, match="lockdown breached"):
        await codex_engine._call_once(context(), "p", SCHEMA, "s")
    fake.plan(stderr=[f"ERROR {codex_engine.TOOL_REFUSAL_MARK}: error=unsupported"],
              linger=30)
    with pytest.raises(llm.ToolCallBlocked):
        await codex_engine._call_once(context(), "p", SCHEMA, "s")


async def test_only_transient_failures_are_retried(home, monkeypatch):
    attempts = []

    def failing_with(exc, then=None):
        async def call_once(ctx, prompt, schema, system_prompt):
            attempts.append(1)
            if then is not None and len(attempts) == 3:
                return then
            raise exc
        return call_once

    monkeypatch.setattr(codex_engine, "_call_once",
                        failing_with(llm.TransientFailure("blip"), then={"x": "abc"}))
    assert await codex_engine.call(context(), "p", SCHEMA, "s") == ({"x": "abc"}, None)
    assert len(attempts) == 3

    for exc in (llm.ToolCallBlocked("tool"), llm.UsageExhausted("limit"),
                llm.ConfigError("breach")):
        attempts.clear()
        monkeypatch.setattr(codex_engine, "_call_once", failing_with(exc))
        with pytest.raises(type(exc)):
            await codex_engine.call(context(), "p", SCHEMA, "s")
        assert len(attempts) == 1


# --- the real Codex CLI, against localhost stand-ins ----------------------------------------

@pytest.fixture()
def real_codex(home, monkeypatch):
    exe = codex_cli.binary()
    if exe is None:
        if REQUIRED:
            pytest.fail("CODEX_TESTS_REQUIRED=1 but the Codex CLI is not on PATH")
        pytest.skip("Codex CLI not installed")
    listed = codex_cli.models()
    assert listed, "Codex lists no models"
    # Every model the picker offers is held to the lockdown, not a sample.
    return SimpleNamespace(exe=exe, models=[m["id"] for m in listed],
                           model=listed[-1]["id"], home=home)


class Answering(codex_engine.LocalResponses):
    """A stand-in that fails every request with a fixed HTTP status."""

    def __init__(self, status, body):
        super().__init__("capture")
        self.status, self.body = status, body

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(":", 1)[1]) for line in
                           head.decode().split("\r\n")
                           if line.lower().startswith("content-length:")), 0)
            await reader.readexactly(length)
            self.requests.append({})
            payload = json.dumps(self.body).encode()
            writer.write(f"HTTP/1.1 {self.status}\r\nContent-Type: application/json\r\n"
                         f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
                         .encode() + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def aim_at(monkeypatch, server, name="standin"):
    """Point production launches at a localhost stand-in. Launches that
    name their own stand-in (verify's) are left alone."""
    build = codex_engine.build_launch

    def build_launch(exe, model, effort, *, provider=None, **paths):
        return build(exe, model, effort, provider=provider or (name, server.url), **paths)

    monkeypatch.setattr(codex_engine, "build_launch", build_launch)


async def _locked_launch(codex, model, server, work):
    locked = codex_engine.locked_entry(await codex_engine._catalog_entry(codex.exe, model))
    schema = work / "schema.json"
    schema.write_text(json.dumps(codex_engine.PROBE_SCHEMA))
    return codex_engine.build_launch(
        codex.exe, model, "low", catalog_path=codex_engine._catalog_file(locked),
        schema_path=schema, output_path=work / "answer.json",
        provider=("adversary", server.url))


def adversarial_arguments(sentinel: Path) -> dict:
    return {
        "exec_command": {"cmd": f"touch {sentinel}"},
        "shell": {"command": ["bash", "-lc", f"touch {sentinel}"]},
        "request_user_input": codex_engine.LocalResponses.arguments,
    }


TOOLS = ("exec_command", "shell", "apply_patch", "request_user_input")


async def test_the_real_codex_passes_its_lockdown_check(real_codex, monkeypatch):
    runs = []
    check = codex_engine._check_launch

    async def counted(exe, model, locked, mode):
        runs.append(mode)
        return await check(exe, model, locked, mode)

    monkeypatch.setattr(codex_engine, "_check_launch", counted)
    for model in real_codex.models:
        runs.clear()
        fp = await codex_engine.verify(model)
        assert codex_engine._verified()[model]["fingerprint"] == fp
        assert runs == ["capture", "tool_call"]
        assert await codex_engine.verify(model) == fp
        assert runs == ["capture", "tool_call"], model


async def test_the_real_capture_is_exactly_the_locked_request(real_codex):
    for model in real_codex.models:
        locked = codex_engine.locked_entry(
            await codex_engine._catalog_entry(real_codex.exe, model))
        server = await codex_engine._check_launch(real_codex.exe, model, locked, "capture")
        assert len(server.requests) == 1, model
        body = server.requests[0]
        assert codex_engine.check_request(body, model) == [], model
        assert codex_engine.check_request(body, "gpt-other") != []
        assert {t.get("name") or t.get("type") for t in body.get("tools") or []} <= (
            codex_engine.ALLOWED_TOOLS)


async def test_the_catalog_rewrite_is_what_locks_the_agent_mode_models(real_codex):
    """On this Codex build the listed models that ship a code or
    multi-agent mode fail the check with those fields left in, and pass
    with the rewrite: the fields, not something else, are the difference."""
    agent_mode = []
    for model in real_codex.models:
        entry = await codex_engine._catalog_entry(real_codex.exe, model)
        if not (any(entry.get(f) for f in codex_engine._AGENT_MODE_FIELDS)
                or entry.get("use_responses_lite")):
            continue
        agent_mode.append(model)
        unlocked = codex_engine.locked_entry(entry)
        for field in (*codex_engine._AGENT_MODE_FIELDS, "use_responses_lite"):
            if field in entry:
                unlocked[field] = entry[field]
        unlocked["model_messages"] = {**(entry.get("model_messages") or {}),
                                      "instructions_template": codex_engine.BASE_INSTRUCTIONS}
        server = await codex_engine._check_launch(real_codex.exe, model, unlocked, "capture")
        assert codex_engine.check_request(server.requests[0], model) != [], model
    if not agent_mode:
        pytest.skip("no listed model ships a code or multi-agent mode on this Codex")


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_forced_tool_call_runs_nothing_on_the_real_codex(real_codex, tool):
    for model in real_codex.models:
        sentinel = real_codex.home / f"sentinel-{tool}-{model}"
        work = real_codex.home / f"work-{model}"
        work.mkdir()
        async with codex_engine.LocalResponses("tool_call", tool=tool) as server:
            if tool == "apply_patch":
                server.patch_input = (f"*** Begin Patch\n*** Add File: {sentinel}\n+ran\n"
                                      "*** End Patch\n")
            else:
                server.arguments = adversarial_arguments(sentinel)[tool]
            launch = await _locked_launch(real_codex, model, server, work)
            started = time.monotonic()
            state = await codex_engine.execute(launch, codex_engine.PROBE_TEXT, timeout=60)
            elapsed = time.monotonic() - started
        assert state.refusal or state.breach, f"{model} {tool}: the call was not stopped"
        assert state.answer is None, model
        assert len(server.requests) <= codex_engine.LOOP_GUARD_MAX_REQUESTS, model
        assert elapsed < 30, model
        await asyncio.sleep(0.5)
        assert not sentinel.exists(), model


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_forced_tool_call_fails_only_that_call(real_codex, monkeypatch, tool):
    for model in real_codex.models:
        sentinel = real_codex.home / f"sentinel-{tool}-{model}"
        async with codex_engine.LocalResponses("tool_call", tool=tool) as server:
            if tool == "apply_patch":
                server.patch_input = (f"*** Begin Patch\n*** Add File: {sentinel}\n+ran\n"
                                      "*** End Patch\n")
            else:
                server.arguments = adversarial_arguments(sentinel)[tool]
            with monkeypatch.context() as m:
                aim_at(m, server)
                ctx = llm.CallContext("codex", model, "low")
                with pytest.raises((llm.ToolCallBlocked, llm.ConfigError)):
                    await codex_engine.call(ctx, "Reply.", codex_engine.PROBE_SCHEMA, "s")
        assert len(server.requests) <= codex_engine.LOOP_GUARD_MAX_REQUESTS, model
        await asyncio.sleep(0.5)
        assert not sentinel.exists(), model


@pytest.mark.parametrize("status, body, kind", [
    (429, {"error": {"type": "usage_limit_reached", "message": "limit",
                     "plan_type": "plus", "resets_at": RESETS_AT}}, llm.UsageExhausted),
    (401, {"error": {"type": "invalid_request_error", "code": "token_expired",
                     "message": "Your authentication token has expired. "
                                "Please try signing in again."}}, llm.AuthRequired),
    (400, {"error": {"type": "invalid_request_error", "code": "model_not_found",
                     "message": "The requested model 'gpt-9' does not exist."}},
     llm.ModelNotEntitled),
])
async def test_the_real_codexs_errors_are_routed_by_type(real_codex, monkeypatch,
                                                         status, body, kind):
    async with Answering(status, body) as server:
        aim_at(monkeypatch, server)
        ctx = llm.CallContext("codex", real_codex.model, "low")
        with pytest.raises(kind) as caught:
            await codex_engine.call(ctx, "Reply.", codex_engine.PROBE_SCHEMA, "s")
    if kind is llm.UsageExhausted:
        # Codex names the reset (to the minute) when the service reports one.
        reported = datetime.fromtimestamp(RESETS_AT, timezone.utc)
        assert abs(caught.value.resets_at - reported) < timedelta(minutes=1)


async def test_an_unreported_reset_from_the_real_codex_stays_unknown(real_codex, monkeypatch):
    body = {"error": {"type": "usage_limit_reached", "message": "limit",
                      "plan_type": "plus"}}
    async with Answering(429, body) as server:
        aim_at(monkeypatch, server)
        ctx = llm.CallContext("codex", real_codex.model, "low")
        with pytest.raises(llm.UsageExhausted) as caught:
            await codex_engine.call(ctx, "Reply.", codex_engine.PROBE_SCHEMA, "s")
    assert caught.value.resets_at is None


async def test_a_structured_call_on_codex_end_to_end(real_codex, monkeypatch):
    monkeypatch.setattr(llm, "_semaphore", None)
    monkeypatch.setattr(llm, "_credential_checks", {})
    monkeypatch.setattr(llm, "credential_problem", lambda provider: None)
    schema = {"type": "object", "properties": {"x": {"type": "string", "minLength": 1}},
              "required": ["x"], "additionalProperties": False}
    for model in real_codex.models:
        llm.select("codex", model)
        llm._set_model_state("codex", model,
                             {"status": "ok", "last_error": None, "verified_at": None})
        async with codex_engine.LocalResponses("answer", answer='{"x": "y"}') as server:
            with monkeypatch.context() as m:
                aim_at(m, server)
                answer = await llm.structured("Say y.", schema, task="extract")

        assert answer == {"x": "y"}, model
        stored = codex_engine._verified()[model]["fingerprint"]
        assert answer.context.fingerprint == stored
        assert answer.context.provider == "codex" and answer.context.model == model
        [body] = server.requests
        assert body["model"] == model and body["store"] is False
        assert body["instructions"] == codex_engine.BASE_INSTRUCTIONS
        assert {t.get("name") or t.get("type") for t in body.get("tools") or []} <= (
            codex_engine.ALLOWED_TOOLS)
        [message] = body["input"]
        assert (message["type"], message["role"]) == ("message", "user"), model
        [text] = [c["text"] for c in message["content"]]
        assert text.endswith("# Task\nSay y.")
        assert engine_fakes.engine_value("status", "codex") in (None, "ok")
