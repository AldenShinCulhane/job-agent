"""Mocked unit tests for engine failure routing — no live Claude calls.

Covers what the live smoke test can't: fable-not-entitled fallback,
auth/quota/transient/config routing, wait-state enforcement, retry
bounds, tool-lockdown violation detection, and env hardening.
"""

import os

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from backend.core import claude_env, db, llm


# --- helpers ----------------------------------------------------------------

def make_result(structured=None, subtype="success"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id="test-session",
        structured_output=structured,
        result="ok",
    )


class FakeQuery:
    """Stands in for claude_agent_sdk.query. Each script entry is either a
    list of messages to yield or an exception to raise; entries are keyed
    by model when a dict is given, else consumed in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, prompt, options):
        self.calls.append(options)
        entry = self.script.pop(0) if self.script else Exception("script exhausted")

        async def gen():
            if isinstance(entry, Exception):
                raise entry
            for message in entry:
                yield message

        return gen()


@pytest.fixture()
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    monkeypatch.setattr(llm, "_semaphore", None)
    monkeypatch.setattr(llm, "_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(llm.claude_cli, "subscription_problem", lambda: None)
    return monkeypatch


def install_query(monkeypatch, script):
    fake = FakeQuery(script)
    monkeypatch.setattr(llm, "query", fake)
    return fake


SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}


# --- probe routing ----------------------------------------------------------

async def test_probe_fable_not_entitled_falls_back_to_opus(engine):
    fake = install_query(
        engine,
        [Exception("model claude-fable-5 not_found on this plan"), [make_result()]],
    )
    assert await llm.probe() == llm.MODEL_FALLBACK
    assert db.get_state("pinned_model") == llm.MODEL_FALLBACK
    assert len(fake.calls) == 2


async def test_probe_opus_also_not_entitled_raises(engine):
    install_query(engine, [Exception("not_found"), Exception("not_found")])
    with pytest.raises(llm.ModelNotEntitled):
        await llm.probe()


@pytest.mark.parametrize(
    "problem",
    ["not signed in — run: ...", "signed in via 'api-key', but this app only supports claude.ai ..."],
)
async def test_probe_rejects_missing_or_non_subscription_auth(engine, problem):
    engine.setattr(llm.claude_cli, "subscription_problem", lambda: problem)
    fake = install_query(engine, [])
    with pytest.raises(llm.AuthRequired):
        await llm.probe()
    assert llm.engine_status()["status"] == "auth_required"
    assert fake.calls == []  # no request spent


async def test_probe_config_error_no_retry(engine):
    fake = install_query(engine, [CLINotFoundError("claude binary missing")])
    with pytest.raises(llm.ConfigError):
        await llm.probe()
    assert llm.engine_status()["status"] == "config_error"
    assert len(fake.calls) == 1


# --- usage limits: durable and enforced -------------------------------------

async def test_usage_exhausted_durable_and_enforced(engine):
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    fake = install_query(engine, [Exception("usage limit reached for this period")])
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")

    status = llm.engine_status()
    assert status["status"] == "waiting_usage_exhausted"
    assert status["retry_in_seconds"] > 0
    assert db.get_state("retry_at") is not None  # durable, not in-memory

    # While the cooldown runs, calls are blocked before any request is made.
    with pytest.raises(llm.UsageExhausted):
        await llm.structured("p", SCHEMA, task="extract")
    assert len(fake.calls) == 1

    # Backoff doubles for the next entry.
    assert db.get_state("usage_backoff_minutes") == "10"


# --- transient retry --------------------------------------------------------

async def test_transient_retries_then_succeeds(engine):
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    fake = install_query(
        engine,
        [CLIConnectionError("hiccup"), CLIConnectionError("hiccup"), [make_result({"x": "y"})]],
    )
    assert await llm.structured("p", SCHEMA, task="extract") == {"x": "y"}
    assert len(fake.calls) == 3


async def test_transient_exhausts_bounded_retries(engine):
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    fake = install_query(engine, [CLIConnectionError("down")] * 5)
    with pytest.raises(llm.TransientFailure):
        await llm.structured("p", SCHEMA, task="extract")
    assert len(fake.calls) == llm._RETRY_ATTEMPTS


# --- lockdown ---------------------------------------------------------------

async def test_real_tool_call_is_config_error(engine):
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    breach = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
        model=llm.MODEL_PREFERRED,
    )
    install_query(engine, [[breach, make_result({"x": "y"})]])
    with pytest.raises(llm.ConfigError, match="lockdown"):
        await llm.structured("p", SCHEMA, task="extract")


async def test_structured_output_delivery_tool_is_exempt(engine):
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    delivery = AssistantMessage(
        content=[
            TextBlock(text="thinking aloud"),
            ToolUseBlock(id="t1", name="StructuredOutput", input={"x": "y"}),
        ],
        model=llm.MODEL_PREFERRED,
    )
    install_query(engine, [[delivery, make_result({"x": "y"})]])
    assert await llm.structured("p", SCHEMA, task="extract") == {"x": "y"}


async def test_logged_out_cli_envelope_routes_to_auth_required(engine):
    """Real CLI shape when signed out: an error ResultMessage with
    subtype "success", is_error=True, and the signal only in .result —
    followed by a generic SDK exception with useless text."""
    db.set_state("pinned_model", llm.MODEL_PREFERRED)

    logged_out = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="s",
        result="Not logged in · Please run /login",
    )

    class TrailingRaise(FakeQuery):
        def __call__(self, prompt, options):
            self.calls.append(options)

            async def gen():
                yield logged_out
                raise Exception("Claude Code returned an error result: success")

            return gen()

    engine.setattr(llm, "query", TrailingRaise([]))
    with pytest.raises(llm.AuthRequired):
        await llm.structured("p", SCHEMA, task="extract")
    assert llm.engine_status()["status"] == "auth_required"


# --- effort policy ----------------------------------------------------------

def test_max_effort_capped_until_m4_enables_it():
    assert llm.ALLOW_MAX_EFFORT is False
    assert llm._effective_effort("layout_final") == "xhigh"
    assert llm._effective_effort("repair") == "xhigh"
    assert llm._effective_effort("extract") == "medium"


async def test_unknown_task_tier_rejected(engine):
    with pytest.raises(llm.ConfigError):
        await llm.structured("p", SCHEMA, task="nonsense")


async def test_pinned_path_config_error_persisted(engine):
    """A lockdown breach / broken CLI after pinning must not leave
    /api/status reporting ok."""
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    db.set_state("engine_status", "ok")
    install_query(engine, [CLINotFoundError("claude binary missing")])
    with pytest.raises(llm.ConfigError):
        await llm.structured("p", SCHEMA, task="extract")
    status = llm.engine_status()
    assert status["status"] == "config_error"
    assert "missing" in status["last_error"]


# --- environment hardening --------------------------------------------------

# Written independently of the claude_env policy (from the Claude Code
# env-var reference) so a policy gap fails here — a test iterating the
# policy's own data could never catch a missing variable.
EXPECTED_FORBIDDEN = {
    # API key / token / endpoint overrides
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    # direct-auth overrides that outrank /login
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_WORKSPACE_ID",
    # external provider routing / credentials
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    # OAuth material that would override the isolated .data/claude login
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
}

# App-controlled variables the scrub must NOT touch.
EXPECTED_ALLOWED = {
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "PATH",
    "HOME",
}


def test_scrub_policy_covers_expected_set():
    missed = sorted(v for v in EXPECTED_FORBIDDEN if not claude_env.is_forbidden(v))
    assert not missed, f"scrub policy misses: {missed}"


def test_scrub_policy_spares_app_controlled_vars():
    overreach = sorted(v for v in EXPECTED_ALLOWED if claude_env.is_forbidden(v))
    assert not overreach, f"scrub policy would strip: {overreach}"


def test_harden_strips_alternate_auth_and_providers(monkeypatch):
    for var in EXPECTED_FORBIDDEN:
        monkeypatch.setenv(var, "sneaky")
    claude_env.harden_process_env()
    for var in EXPECTED_FORBIDDEN:
        assert var not in os.environ, var
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(claude_env.CLAUDE_CONFIG_DIR)
    assert os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


# --- success recovery -------------------------------------------------------

async def test_success_clears_stale_error_state(engine):
    """After an auth failure is fixed, a successful call must leave no
    stale last_error next to an ok status."""
    db.set_state("pinned_model", llm.MODEL_PREFERRED)
    db.set_state("engine_status", "auth_required")
    db.set_state("last_error", "Not logged in · Please run /login")

    install_query(engine, [[make_result({"x": "y"})]])
    assert await llm.structured("p", SCHEMA, task="extract") == {"x": "y"}

    status = llm.engine_status()
    assert status["status"] == "ok"
    assert status["last_error"] is None
