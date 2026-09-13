"""US-011: all eleven vendors, driven through the real adapters under replay.

Only the HTTP layer is faked: :mod:`snowpea_core.providers.replay` installs a
transport that returns the recorded chunks of
``tests/fixtures/providers/<vendor>/tool_call_once.json`` in request order, and
everything above it — request building, SSE parsing, normalisation — is the
production code path.

The golden scenario is "call ``shell`` once, then answer", which every vendor
must normalise to the same :class:`StreamEvent` sequence.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.providers import auth_web, replay
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, ToolSpec
from snowpea_core.providers.presets import PRESETS, WEB_LOGIN_VENDORS
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.server.errors import RpcError

VENDORS: tuple[str, ...] = tuple(PRESETS)
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "providers"

SHELL = ToolSpec(
    name="shell",
    description="Run a shell command.",
    input_schema={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)

FIRST_TURN = [
    ChatMessage(role="system", content="You are Snowpea."),
    ChatMessage(role="user", content="list the files"),
]


def second_turn(call: ToolCall) -> list[ChatMessage]:
    """The first turn plus the assistant's tool call and its result."""
    return [
        *FIRST_TURN,
        ChatMessage(role="assistant", content="Checking the files.", tool_calls=[call]),
        ChatMessage(
            role="tool",
            content="a.txt\nb.txt\nc.txt",
            tool_call_id=call.id,
            name=call.name,
        ),
    ]


@pytest.fixture(autouse=True)
def _replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module runs with the HTTP layer replayed."""
    monkeypatch.setenv(replay.MODE_ENV, "replay")
    monkeypatch.delenv("SNOWPEA_PROVIDER", raising=False)


@pytest.fixture
def registry() -> ProviderRegistry:
    return ProviderRegistry(Settings())


async def drain(provider: Any, messages: list[ChatMessage]) -> list[StreamEvent]:
    return [event async for event in provider.stream(messages, [SHELL], max_tokens=4096)]


def kinds(events: list[StreamEvent]) -> list[str]:
    return [event.kind for event in events]


# ---------------------------------------------------------------------------
# fixtures themselves
# ---------------------------------------------------------------------------


def test_every_vendor_has_a_golden_fixture() -> None:
    missing = [v for v in VENDORS if not (FIXTURE_ROOT / v / "tool_call_once.json").is_file()]
    assert missing == []
    assert len(VENDORS) == 11


@pytest.mark.parametrize("vendor", VENDORS)
def test_fixture_is_marked_synthetic_with_two_exchanges(vendor: str) -> None:
    raw = json.loads((FIXTURE_ROOT / vendor / "tool_call_once.json").read_text(encoding="utf-8"))
    assert raw["vendor"] == vendor
    assert raw["synthetic"] is True, "no real key was used to produce these"
    assert len(raw["exchanges"]) == 2


# ---------------------------------------------------------------------------
# the matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vendor", VENDORS)
async def test_golden_scenario_normalises_identically(
    vendor: str, registry: ProviderRegistry
) -> None:
    """``text_delta* → tool_call(shell) → usage → done`` for every vendor."""
    with replay.replaying(vendor) as tape:
        provider = registry.build(vendor)
        assert provider.vendor == vendor
        assert provider.model == PRESETS[vendor].default_model

        first = await drain(provider, FIRST_TURN)
        assert kinds(first)[-3:] == ["tool_call", "usage", "done"]
        assert set(kinds(first)[:-3]) <= {"text_delta"}
        assert kinds(first).count("tool_call") == 1

        call = next(e.tool_call for e in first if e.kind == "tool_call")
        assert call is not None
        assert call.name == "shell"
        assert call.arguments == {"command": "ls"}
        assert call.id

        assert "".join(e.text for e in first if e.kind == "text_delta") == "Checking the files."
        usage = next(e.usage for e in first if e.kind == "usage")
        assert usage is not None
        assert usage.input_tokens == 42
        assert usage.output_tokens == 11
        assert first[-1].stop_reason == "tool_use"

        second = await drain(provider, second_turn(call))
        assert kinds(second)[-2:] == ["usage", "done"]
        assert set(kinds(second)[:-2]) <= {"text_delta"}
        assert "".join(e.text for e in second if e.kind == "text_delta") == "There are 3 files."
        assert second[-1].stop_reason == "end_turn"

        assert tape.remaining == 0, "both recorded exchanges must be consumed"


@pytest.mark.parametrize("vendor", VENDORS)
async def test_replayed_requests_carry_the_tool_and_no_secrets(
    vendor: str, registry: ProviderRegistry
) -> None:
    """The adapter really built a request: the tool and the model are in it."""
    if PRESETS[vendor].adapter == "anthropic_native":
        pytest.skip("the Anthropic SDK owns request framing; covered by the scenario test")
    with replay.replaying(vendor) as tape:
        provider = registry.build(vendor)
        await drain(provider, FIRST_TURN)
    assert tape.seen, "the replay transport saw no request"
    request = tape.seen[0]
    assert request["method"] == "POST"
    body = json.dumps(request["body"])
    assert "shell" in body
    assert request["headers"].get("authorization", replay.REDACTED) == replay.REDACTED


async def test_exhausted_tape_is_an_error(registry: ProviderRegistry) -> None:
    with replay.replaying("openai"), pytest.raises(Exception) as excinfo:
        provider = registry.build("openai")
        for _ in range(3):
            await drain(provider, FIRST_TURN)
    assert "exchange" in str(excinfo.value)


# ---------------------------------------------------------------------------
# provider.list / resolution
# ---------------------------------------------------------------------------


def test_provider_list_has_eleven_entries_and_three_interactive_logins(
    registry: ProviderRegistry,
) -> None:
    infos = registry.list()
    assert len(infos) == 11
    assert [info.vendor for info in infos] == list(VENDORS)
    assert all(info.label for info in infos)
    assert all(info.defaultModel for info in infos)
    assert all("api_key" in info.authMethods for info in infos)

    web = [info.vendor for info in infos if len(info.authMethods) > 1]
    assert web == ["openai", "openrouter", "gemini"]
    assert set(WEB_LOGIN_VENDORS) == {"openai", "openrouter", "gemini"}
    assert PRESETS["openai"].auth_methods == (
        "api_key",
        "browser_pkce",
        "device_code",
        "oauth_token",
    )
    assert PRESETS["openrouter"].auth_methods == ("api_key", "oauth_pkce")
    assert PRESETS["gemini"].auth_methods == (
        "api_key",
        "google_oauth",
        "google_adc",
        "oauth_token",
    )


def test_configured_follows_settings_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for preset in PRESETS.values():
        for key in preset.env_keys:
            monkeypatch.delenv(key, raising=False)
    registry = ProviderRegistry(Settings())
    assert not any(info.configured for info in registry.list())

    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert registry.is_configured("deepseek")
    assert registry.api_key_for("deepseek") == "from-env"
    # settings win over the environment
    registry.configure("deepseek", {"api_key": "from-settings"})
    assert registry.api_key_for("deepseek") == "from-settings"


def test_gemini_oauth_token_is_not_mistaken_for_an_api_key() -> None:
    settings = Settings()
    settings.providers["gemini"] = {
        "auth_method": "oauth_token",
        "oauth_token": "ya29.remote-token",
    }
    registry = ProviderRegistry(settings)
    provider = registry.build("gemini")
    assert registry.is_configured("gemini")
    assert registry.api_key_for("gemini") is None
    assert provider._api_key is None  # noqa: SLF001
    assert provider._oauth_token_value == "ya29.remote-token"  # noqa: SLF001


async def test_remote_oauth_token_login_uses_provider_configure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from snowpea_core.cli import commands
    from snowpea_core.providers import models as model_discovery

    probed: list[str | None] = []

    async def listing(_preset: Any, *, api_key: str | None = None, **_kwargs: Any) -> list[str]:
        probed.append(api_key)
        return ["gemini-2.5-pro"]

    monkeypatch.setattr(model_discovery, "list_models", listing)
    called = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(commands, "_call", called)
    assert await commands.provider_login("gemini", token="ya29.remote-token") == 0
    # The pasted token is checked with one authenticated call before it is
    # stored, instead of failing opaquely on the first prompt.
    assert probed == ["ya29.remote-token"]
    called.assert_awaited_once_with(
        None,
        "provider.configure",
        {
            "vendor": "gemini",
            "config": {
                "oauth_token": "ya29.remote-token",
                "auth_method": "oauth_token",
                # ``None`` removes the credentials this token replaces.
                "api_key": None,
                "access_token": None,
            },
        },
    )


async def test_a_pasted_token_that_fails_its_probe_is_still_stored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe can fail for reasons unrelated to the token (offline, blocked
    endpoint), so it warns rather than refusing the login."""
    from snowpea_core.cli import commands
    from snowpea_core.providers import models as model_discovery

    async def listing(*_args: Any, **_kwargs: Any) -> list[str]:
        raise RuntimeError("HTTP 401: invalid authentication")

    monkeypatch.setattr(model_discovery, "list_models", listing)
    called = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(commands, "_call", called)
    assert await commands.provider_login("openai", token="expired-token") == 0
    out = capsys.readouterr().out
    assert "warning:" in out and "401" in out
    assert called.await_count == 1


def test_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    for preset in PRESETS.values():
        for key in preset.env_keys:
            monkeypatch.delenv(key, raising=False)
    settings = Settings()
    registry = ProviderRegistry(settings)

    # 4. first configured vendor
    settings.providers["kimi"] = {"api_key": "k"}
    assert registry.default_vendor() == "kimi"
    # 3. settings.providers.default
    settings.providers["default"] = "qwen"
    assert registry.default_vendor() == "qwen"
    # 1. SNOWPEA_PROVIDER wins over everything, model included
    monkeypatch.setenv("SNOWPEA_PROVIDER", "deepseek:deepseek-reasoner")
    assert registry.default_vendor() == "deepseek"
    provider = registry.get()
    assert provider.vendor == "deepseek"
    assert provider.model == "deepseek-reasoner"


def test_fake_provider_still_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": [], "default": {"text": "x"}}), encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_PROVIDER", f"fake:{script}")
    provider = ProviderRegistry(Settings()).get("openai")
    assert provider.vendor == "fake"


def test_local_variants_change_the_base_url() -> None:
    settings = Settings()
    settings.providers["local"] = {"variant": "vllm"}
    registry = ProviderRegistry(settings)
    assert registry.preset("local").base_url == "http://localhost:8000/v1"
    assert PRESETS["local"].base_url == "http://localhost:11434/v1"


# ---------------------------------------------------------------------------
# web login
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _never_open_a_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No test may pop a browser window open."""
    opened: list[str] = []
    monkeypatch.setattr(auth_web.webbrowser, "open", lambda url, *a, **kw: opened.append(url))
    yield opened


@pytest.mark.parametrize(
    "vendor", [v for v in VENDORS if v not in ("openai", "openrouter", "gemini")]
)
async def test_login_web_is_unsupported_for_api_key_only_vendors(vendor: str) -> None:
    with pytest.raises(RpcError) as excinfo:
        await auth_web.login(vendor, "web")
    assert excinfo.value.code == "login_unsupported"
    assert "snowpea setup --vendor" in excinfo.value.message


async def test_device_code_login_polls_until_granted(
    monkeypatch: pytest.MonkeyPatch, _never_open_a_browser: list[str]
) -> None:
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        # OpenAI's real device endpoint; the old ``/oauth/device/code`` guess
        # was a placeholder that answered 403 behind Cloudflare.
        if request.url.path.endswith("/deviceauth/usercode"):
            return httpx.Response(
                200,
                json={
                    "device_auth_id": "dev-123",
                    "user_code": "WXYZ-1234",
                    "verification_uri": "https://auth.openai.com/activate",
                    "interval": 0,
                    "expires_in": 600,
                },
            )
        polls += 1
        if polls < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200, json={"access_token": "tok-abc", "refresh_token": "ref-abc", "expires_in": 3600}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    prompts: list[str] = []
    try:
        result = await auth_web.device_code_login(
            "openai", client=client, on_prompt=prompts.append, sleep=_no_sleep
        )
    finally:
        await client.aclose()

    assert polls == 3
    assert result.method == "device_code"
    # The headless flow signs into the same ChatGPT account as the browser
    # one, so it must leave the same record — including the ``auth_method``
    # that routes the vendor to the Codex transport.
    assert result.credentials["access_token"] == "tok-abc"
    assert result.credentials["refresh_token"] == "ref-abc"
    assert result.credentials["auth_method"] == "chatgpt"
    assert result.credentials["expires_at"] > time.time()
    # A stale API key must not outlive the login that replaced it.
    assert result.credentials["api_key"] is None
    assert any("WXYZ-1234" in line for line in prompts)
    assert _never_open_a_browser == ["https://auth.openai.com/activate"]


async def test_device_code_login_reports_denial(_never_open_a_browser: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "AAAA",
                    "verification_uri": "https://auth.openai.com/activate",
                    "interval": 0,
                },
            )
        return httpx.Response(400, json={"error": "access_denied"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RpcError) as excinfo:
            await auth_web.device_code_login("openai", client=client, sleep=_no_sleep)
    finally:
        await client.aclose()
    assert "access_denied" in excinfo.value.message


async def test_oauth_pkce_login_exchanges_the_callback_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchanged: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        exchanged.update(json.loads(request.content))
        return httpx.Response(200, json={"key": "or-key-xyz"})

    def fake_open(url: str, *args: Any, **kwargs: Any) -> bool:
        """Stand in for the user approving the consent screen."""

        async def visit() -> None:
            async with httpx.AsyncClient() as browser:
                callback = url.split("callback_url=", 1)[1].split("&", 1)[0]
                await browser.get(
                    httpx.URL(callback.replace("%3A", ":").replace("%2F", "/")),
                    params={"code": "code-789"},
                )

        asyncio.get_running_loop().create_task(visit())
        return True

    monkeypatch.setattr(auth_web.webbrowser, "open", fake_open)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await auth_web.oauth_pkce_login("openrouter", client=client, timeout_sec=10.0)
    finally:
        await client.aclose()

    # The minted key, plus the clearing instructions that stop an earlier
    # OAuth session from outliving it (``None`` removes the field).
    assert result.credentials == {
        "api_key": "or-key-xyz",
        "oauth_token": None,
        "token": None,
        "auth_method": None,
    }
    assert exchanged["code"] == "code-789"
    assert exchanged["code_challenge_method"] == "S256"
    assert len(exchanged["code_verifier"]) >= 43


def test_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = auth_web.new_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# RPC handlers
# ---------------------------------------------------------------------------


def _core(tmp_path: Path) -> Any:
    from snowpea_core.config.paths import Paths
    from snowpea_core.server.app_server import Core

    paths = Paths(home=tmp_path)
    paths.ensure()
    settings = Settings()
    core = Core(settings=settings, paths=paths, token="t")
    core.providers.bind(settings)
    return core


async def test_provider_list_handler_returns_eleven(tmp_path: Path) -> None:
    from snowpea_core.server.session_handlers import provider_list_handler

    result = await provider_list_handler(None, None, _core(tmp_path))  # type: ignore[arg-type]
    assert len(result.providers) == 11
    assert sum(len(p.authMethods) > 1 for p in result.providers) == 3


async def test_provider_configure_handler_persists_settings(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_configure_handler
    from snowpea_core.server.protocol import ProviderConfigureParams

    core = _core(tmp_path)
    params = ProviderConfigureParams(
        vendor="xai", config={"api_key": "xai-test", "model": "grok-3-mini", "junk": "dropped"}
    )
    assert (await provider_configure_handler(None, params, core)).ok  # type: ignore[arg-type]

    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["providers"]["xai"] == {"api_key": "xai-test", "model": "grok-3-mini"}
    assert stored["providers"]["default"] == "xai"
    assert core.providers.model_for("xai") == "grok-3-mini"


async def test_provider_configure_handler_rejects_unknown_vendor(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_configure_handler
    from snowpea_core.server.protocol import ProviderConfigureParams

    params = ProviderConfigureParams(vendor="nope", config={"api_key": "x"})
    with pytest.raises(RpcError) as excinfo:
        await provider_configure_handler(None, params, _core(tmp_path))  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_params"


async def test_provider_login_web_handler_rejects_deepseek(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_login_web_handler
    from snowpea_core.server.protocol import ProviderLoginWebParams

    params = ProviderLoginWebParams(vendor="deepseek", method="web")
    with pytest.raises(RpcError) as excinfo:
        await provider_login_web_handler(None, params, _core(tmp_path))  # type: ignore[arg-type]
    assert excinfo.value.code == "login_unsupported"
    assert "snowpea setup --vendor deepseek" in excinfo.value.message
