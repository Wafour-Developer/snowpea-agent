"""Unified reasoning effort — one scale, four tiers, mapped per vendor.

``thinking`` is a switch; effort is a dial, and every vendor spells the dial
differently.  These cover the three halves of that: the precedence chain that
decides which tier a turn runs at, the pure mapping from a tier to one vendor's
request field, and the surfaces — ``/effort``, ``session.setEffort`` — that let
a person change it (CORE-effort).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, fake_provider, make_daemon

from snowpea_core.config.settings import Settings
from snowpea_core.providers import effort as effort_scale
from snowpea_core.providers.base import ChatMessage, ToolSpec
from snowpea_core.providers.normalize import build_gemini_request, build_openai_request
from snowpea_core.providers.presets import PRESETS, synthesize_local_preset
from snowpea_core.providers.registry import ProviderRegistry

MESSAGES = [ChatMessage(role="user", content="hi")]
TOOLS: list[ToolSpec] = []


@pytest.fixture(autouse=True)
def _forget_refusals() -> Any:
    """The unsupported-parameter memory is process-global."""
    effort_scale.UNSUPPORTED.clear()
    yield
    effort_scale.UNSUPPORTED.clear()


def _settings(**agent: Any) -> Settings:
    settings = Settings()
    for key, value in agent.items():
        setattr(settings.agent, key, value)
    return settings


# ---------------------------------------------------------------------------
# the scale itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["low", "medium", "high", "max"])
def test_every_tier_normalizes_to_itself(tier: str) -> None:
    assert effort_scale.normalize(tier) == tier
    assert effort_scale.normalize(f"  {tier.upper()} ") == tier


@pytest.mark.parametrize("value", [None, "", "auto", "AUTO", "  "])
def test_nothing_pinned_normalizes_to_none(value: str | None) -> None:
    assert effort_scale.normalize(value) is None


def test_a_vendor_spelling_a_user_copied_is_accepted() -> None:
    """``xhigh`` and ``minimal`` are what the vendors' own docs say."""
    assert effort_scale.normalize("xhigh") == "max"
    assert effort_scale.normalize("minimal") == "low"


def test_a_typo_degrades_to_nothing_rather_than_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert effort_scale.normalize("hard") is None
    assert "hard" in caplog.text


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_the_default_is_medium() -> None:
    assert effort_scale.resolve(Settings(), "openai", "o3") == ("medium", "default")


def test_agent_effort_is_the_floor() -> None:
    assert effort_scale.resolve(_settings(effort="high"), "openai", "o3") == ("high", "default")


def test_a_vendor_rule_beats_the_default() -> None:
    settings = _settings(effort="low", effortBy={"openai": "high"})
    assert effort_scale.resolve(settings, "openai", "o3") == ("high", "vendor")
    # and applies to that vendor only
    assert effort_scale.resolve(settings, "anthropic", "claude") == ("low", "default")


def test_a_model_rule_beats_a_vendor_rule() -> None:
    settings = _settings(effortBy={"openai": "low", "openai:o3": "max"})
    assert effort_scale.resolve(settings, "openai", "o3") == ("max", "model")
    assert effort_scale.resolve(settings, "openai", "gpt-4.1") == ("low", "vendor")


def test_a_session_pin_beats_every_rule() -> None:
    settings = _settings(effort="low", effortBy={"openai": "low", "openai:o3": "low"})
    assert effort_scale.resolve(settings, "openai", "o3", session_effort="max") == (
        "max",
        "session",
    )


def test_a_call_override_beats_the_session_pin() -> None:
    result = effort_scale.resolve(
        _settings(), "openai", "o3", session_effort="low", override="high"
    )
    assert result == ("high", "session")


def test_the_legacy_openai_key_is_read_but_only_for_openai() -> None:
    """A Codex user's existing setting must not change meaning on upgrade."""
    settings = Settings()
    settings.providers["openai"] = {"reasoning_effort": "high"}
    settings.providers["xai"] = {"reasoning_effort": "high"}
    assert effort_scale.resolve(settings, "openai", "gpt-5") == ("high", "vendor")
    # Not a general mechanism: no other vendor's block is consulted.
    assert effort_scale.resolve(settings, "xai", "grok-4") == ("medium", "default")


def test_a_unified_rule_outranks_the_legacy_key() -> None:
    settings = _settings(effortBy={"openai": "low"})
    settings.providers["openai"] = {"reasoning_effort": "high"}
    assert effort_scale.resolve(settings, "openai", "gpt-5") == ("low", "vendor")


def test_the_chain_works_on_a_settings_document_too() -> None:
    """The CLI reads settings over RPC as JSON and must not re-implement this."""
    document = {"agent": {"effort": "high", "effortBy": {"anthropic:opus": "max"}}}
    assert effort_scale.resolve(document, "anthropic", "opus") == ("max", "model")
    assert effort_scale.resolve(document, "anthropic", "sonnet") == ("high", "default")


def test_the_registry_resolves_against_the_session_model() -> None:
    settings = _settings(effortBy={"anthropic:claude-opus-4-1": "max"})
    settings.providers["anthropic"] = {"api_key": "sk-test", "model": "claude-opus-4-1"}
    registry = ProviderRegistry(settings)
    # The bare vendor resolves to its configured model, so the model rule hits.
    assert registry.effort_for("anthropic") == ("max", "model")


# ---------------------------------------------------------------------------
# mappings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "wire"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "high")],
)
def test_openai_mapping(tier: str, wire: str) -> None:
    assert effort_scale.openai_reasoning_effort(tier) == wire
    assert wire in effort_scale.OPENAI_EFFORTS


@pytest.mark.parametrize(
    ("tier", "wire"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "xhigh")],
)
def test_codex_mapping_has_the_fourth_rung(tier: str, wire: str) -> None:
    assert effort_scale.codex_effort(tier) == wire
    assert wire in effort_scale.CODEX_EFFORTS


def test_no_tier_maps_to_nothing() -> None:
    assert effort_scale.openai_reasoning_effort(None) is None
    assert effort_scale.codex_effort("auto") is None
    assert effort_scale.thinking_budget(None) is None


@pytest.mark.parametrize(
    ("tier", "budget"), [("low", 2048), ("medium", 8192), ("high", 32768), ("max", 65536)]
)
def test_the_budget_ladder(tier: str, budget: int) -> None:
    assert effort_scale.thinking_budget(tier, max_tokens=1_000_000) == budget


def test_a_budget_leaves_room_for_the_answer() -> None:
    """A budget equal to the whole allowance is a turn that answers nothing."""
    assert effort_scale.thinking_budget("max", max_tokens=16_384) == 12_288
    assert effort_scale.thinking_budget("max", max_tokens=16_384) < 16_384


def test_anthropic_drops_a_budget_the_output_limit_cannot_afford() -> None:
    # 1 000 tokens of output cannot carry Anthropic's 1 024 floor.
    assert effort_scale.anthropic_thinking("high", max_tokens=1_000) is None
    assert effort_scale.anthropic_thinking("high", max_tokens=100_000) == {
        "type": "enabled",
        "budget_tokens": 32_768,
    }


def test_thinking_off_beats_every_tier() -> None:
    assert effort_scale.anthropic_thinking("max", 100_000, thinking="off") is None
    # Gemini is told to spend nothing rather than simply not asked.
    assert effort_scale.gemini_thinking_config("max", 100_000, thinking="off") == {
        "thinkingBudget": 0
    }


def test_gemini_mapping() -> None:
    assert effort_scale.gemini_thinking_config("low", 100_000) == {"thinkingBudget": 2_048}
    assert effort_scale.gemini_thinking_config(None, 100_000) is None


@pytest.mark.parametrize("model", ["o3", "o4-mini", "gpt-5", "gpt-5-codex", "codex-mini"])
def test_the_reasoning_families_take_the_openai_field(model: str) -> None:
    assert effort_scale.supports_openai_effort(model) is True


@pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o", "", None, "claude-sonnet-4-5"])
def test_everything_else_does_not(model: str | None) -> None:
    assert effort_scale.supports_openai_effort(model) is False


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------


def test_the_openai_body_carries_the_effort() -> None:
    body = build_openai_request(
        PRESETS["openai"], "o3", MESSAGES, TOOLS, max_tokens=1000, effort="max"
    )
    assert body["reasoning_effort"] == "high"


def test_thinking_off_wins_over_the_effort_in_the_body() -> None:
    body = build_openai_request(
        PRESETS["openai"], "o3", MESSAGES, TOOLS, max_tokens=1000, thinking="off", effort="high"
    )
    assert "reasoning_effort" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_no_effort_leaves_the_body_as_it_was() -> None:
    body = build_openai_request(PRESETS["openai"], "o3", MESSAGES, TOOLS, max_tokens=1000)
    assert "reasoning_effort" not in body


def test_the_gemini_body_carries_a_budget() -> None:
    body = build_gemini_request(MESSAGES, TOOLS, max_tokens=100_000, effort="high")
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 32_768}
    plain = build_gemini_request(MESSAGES, TOOLS, max_tokens=100_000)
    assert "thinkingConfig" not in plain["generationConfig"]


def test_the_codex_body_uses_the_backend_scale() -> None:
    from snowpea_core.providers.codex_transport import CodexProvider

    provider = CodexProvider({"access_token": "tok"}, model="gpt-5-codex")
    body = provider.build_request(MESSAGES, TOOLS, effort="max")
    assert body["reasoning"] == {"effort": "xhigh"}
    # Without a tier the configured (legacy) value still applies.
    assert provider.build_request(MESSAGES, TOOLS)["reasoning"] == {"effort": "medium"}


def test_the_codex_body_honours_the_legacy_configured_effort() -> None:
    from snowpea_core.providers.codex_transport import CodexProvider

    provider = CodexProvider({"access_token": "tok"}, model="gpt-5-codex", reasoning_effort="high")
    assert provider.build_request(MESSAGES, TOOLS)["reasoning"] == {"effort": "high"}


# ---------------------------------------------------------------------------
# which vendors take it at all
# ---------------------------------------------------------------------------


def test_only_the_vendors_whose_api_takes_it_are_marked() -> None:
    registry = ProviderRegistry(Settings())
    takes = {info.vendor: info.supportsEffort for info in registry.list()}
    assert takes["anthropic"] and takes["openai"] and takes["gemini"]
    assert takes["openrouter"] and takes["xai"]
    # Sent nothing: their APIs ignore or reject the field.
    assert not takes["deepseek"] and not takes["qwen"] and not takes["glm"]
    assert not takes["local"]


def test_a_local_server_opts_in_per_server() -> None:
    settings = Settings()
    settings.providers["hon2"] = {
        "preset": "local",
        "base_url": "http://hon2:8000/v1",
        "effort_param": True,
    }
    settings.providers["lab"] = {"preset": "local", "base_url": "http://lab:8000/v1"}
    registry = ProviderRegistry(settings)
    assert registry.supports_effort("hon2") is True
    assert registry.supports_effort("lab") is False
    assert synthesize_local_preset("lab", {"preset": "local"}).supports_effort is False


# ---------------------------------------------------------------------------
# the unsupported-parameter fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
        '{"error": {"message": "Unknown parameter: reasoning_effort"}}',
        "reasoning_effort: extra inputs are not permitted",
    ],
)
def test_a_400_naming_the_parameter_is_recognised(detail: str) -> None:
    assert effort_scale.UnsupportedEffort.is_unsupported_error(detail) is True


@pytest.mark.parametrize(
    "detail",
    [
        "HTTP 429: rate limit exceeded",
        "Unsupported parameter: 'max_tokens'",
        "the model 'local-model' does not exist",
        "",
    ],
)
def test_an_unrelated_error_is_not_mistaken_for_it(detail: str) -> None:
    assert effort_scale.UnsupportedEffort.is_unsupported_error(detail) is False


def test_a_refusal_is_remembered_per_model() -> None:
    effort_scale.UNSUPPORTED.remember("openai", "o3")
    assert effort_scale.UNSUPPORTED.refused("openai", "o3") is True
    assert effort_scale.UNSUPPORTED.refused("openai", "gpt-5") is False
    assert effort_scale.UNSUPPORTED.refused("xai", "o3") is False


async def test_a_refused_model_is_retried_without_the_field_and_not_asked_again() -> None:
    """The prefix list is a guess; a wrong guess costs one retry, not a turn."""
    import httpx

    from snowpea_core.providers.openai_compat import OpenAICompatProvider

    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Unsupported parameter: 'reasoning_effort' is not supported"
                    }
                },
            )
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatProvider(
        PRESETS["openai"], api_key="sk-test", model="o3", base_url="https://api.test/v1"
    )
    def client() -> httpx.AsyncClient:
        """The adapter's own client, wired to the fake endpoint."""
        return httpx.AsyncClient(
            base_url="https://api.test/v1", transport=httpx.MockTransport(handle)
        )

    provider._client = client  # type: ignore[method-assign]  # noqa: SLF001 - the seam under test

    text = "".join([event.text async for event in provider.stream(MESSAGES, TOOLS, effort="high")])
    assert text == "ok"
    # One refused call, one retry without the field.
    assert "reasoning_effort" in bodies[0]
    assert "reasoning_effort" not in bodies[1]
    assert effort_scale.UNSUPPORTED.refused("openai", "o3") is True

    bodies.clear()
    async for _ in provider.stream(MESSAGES, TOOLS, effort="high"):
        pass
    # Asked once, and never sent the field again.
    assert len(bodies) == 1
    assert "reasoning_effort" not in bodies[0]


# ---------------------------------------------------------------------------
# the agent loop
# ---------------------------------------------------------------------------


def test_the_turn_config_carries_the_resolved_tier() -> None:
    from snowpea_core.agent.loop import agent_config

    class _Core:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings
            self.providers = ProviderRegistry(settings)

    settings = _settings(effortBy={"anthropic": "max"})
    settings.providers["anthropic"] = {"api_key": "sk-test"}
    settings.providers["default"] = "anthropic"
    config = agent_config(_Core(settings))  # type: ignore[arg-type]
    assert (config.effort, config.effort_source) == ("max", "vendor")


# ---------------------------------------------------------------------------
# /effort and session.setEffort
# ---------------------------------------------------------------------------

FIXTURE = [{"match": "", "text": "done"}]


async def test_set_effort_round_trips_over_rpc(tmp_path: Path, http: aiohttp.ClientSession) -> None:
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home")
        try:
            client = await connect(http, daemon)
            workdir = tmp_path / "project"
            workdir.mkdir()
            created = await client.ok("session.create", {"workdir": str(workdir)})
            session_id = str(created["sessionId"])

            pinned = await client.ok(
                "session.setEffort", {"sessionId": session_id, "effort": "max"}
            )
            assert pinned["effort"] == "max"
            assert pinned["effortSource"] == "session"
            assert pinned["pinned"] == "max"

            rows = {row["sessionId"]: row for row in (await client.ok("session.list"))["sessions"]}
            assert rows[session_id]["effort"] == "max"

            cleared = await client.ok(
                "session.setEffort", {"sessionId": session_id, "effort": None}
            )
            assert cleared["pinned"] is None
            assert cleared["effort"] == "medium"
            assert cleared["effortSource"] == "default"
            await client.stop()
        finally:
            await daemon.stop()


async def test_set_effort_refuses_a_tier_that_does_not_exist(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home")
        try:
            client = await connect(http, daemon)
            workdir = tmp_path / "project"
            workdir.mkdir()
            created = await client.ok("session.create", {"workdir": str(workdir)})
            frame = await client.call(
                "session.setEffort",
                {"sessionId": str(created["sessionId"]), "effort": "hard"},
            )
            assert frame["error"]["data"]["code"] == "invalid_params"
            await client.stop()
        finally:
            await daemon.stop()


async def test_the_pin_survives_a_resume(tmp_path: Path, http: aiohttp.ClientSession) -> None:
    """A pin that vanished on restore would silently drop the chosen tier."""
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        client = await connect(http, daemon)
        created = await client.ok("session.create", {"workdir": str(workdir)})
        session_id = str(created["sessionId"])
        await client.ok("session.setEffort", {"sessionId": session_id, "effort": "high"})
        await client.stop()
        await daemon.stop()

        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon)
            resumed = await client.ok("session.resume", {"sessionId": session_id})
            # The resume answer says what the session runs under now, so a
            # surface adopts it instead of the mode it launched with.
            assert resumed["mode"] == "accept"
            assert resumed["effort"] == "high"
            rows = {row["sessionId"]: row for row in (await client.ok("session.list"))["sessions"]}
            assert rows[session_id]["effort"] == "high"
            await client.stop()
        finally:
            await daemon.stop()


async def test_the_effort_command_reports_and_pins(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home")
        try:
            client = await connect(http, daemon)
            workdir = tmp_path / "project"
            workdir.mkdir()
            created = await client.ok("session.create", {"workdir": str(workdir)})
            session_id = str(created["sessionId"])

            bare = await client.ok(
                "command.run", {"sessionId": session_id, "name": "effort", "args": ""}
            )
            assert await client.wait_turn(str(bare["turnId"])) == "complete"
            said = client.of_kind("message.done")[-1]["payload"]["text"]
            assert "effort: medium (agent.effort)" in said

            pin = await client.ok(
                "command.run", {"sessionId": session_id, "name": "effort", "args": "high"}
            )
            assert await client.wait_turn(str(pin["turnId"])) == "complete"
            said = client.of_kind("message.done")[-1]["payload"]["text"]
            assert "effort: high (session pin)" in said
            changed = client.of_kind("model.changed")[-1]["payload"]
            assert changed["effort"] == "high"
            assert changed["effortSource"] == "session"

            bad = await client.ok(
                "command.run", {"sessionId": session_id, "name": "effort", "args": "hard"}
            )
            assert await client.wait_turn(str(bad["turnId"])) == "complete"
            said = client.of_kind("message.done")[-1]["payload"]["text"]
            assert "unknown effort" in said
            await client.stop()
        finally:
            await daemon.stop()


def test_the_command_names_the_rule_that_decided_it() -> None:
    from snowpea_core.commands.effort_cmd import describe

    assert describe("high", "session") == "effort: high (session pin)"
    assert describe("max", "model") == "effort: max (model rule)"
    assert describe("low", "vendor") == "effort: low (vendor rule)"
    assert describe("medium", "default") == "effort: medium (agent.effort)"
