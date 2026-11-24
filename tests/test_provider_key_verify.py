"""Provider keys are checked with the vendor (1.74.0, capability providerKeyVerify).

Every vendor here is a local aiohttp server: no test reaches a real vendor,
and every key is a made-up string.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from _support import Recorder, make_daemon
from aiohttp import web

from snowpea_core.agent import loop as agent_loop
from snowpea_core.config.settings import Settings
from snowpea_core.providers import key_verify
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.presets import PRESETS, preset_for
from snowpea_core.server.errors import RpcError

GOOD = "sk-good-0123456789abcdefXYZ"
WRONG = "sk-wrong-9876543210fedcbaQRS"

DASHSCOPE_401 = {
    "error": {
        "message": "Incorrect API key provided. ",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    },
    "request_id": "00000000-0000-0000-0000-000000000000",
}


class FakeVendor:
    """Every vendor family the check talks to, under its own path prefix."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.chat_calls = 0
        self.runner: web.AppRunner | None = None
        self.base = ""

    def _bearer(self, request: web.Request) -> str:
        return request.headers.get("authorization", "").removeprefix("Bearer ").strip()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        self.calls.append((request.method, path))
        bearer = self._bearer(request)
        if path.endswith("/chat/completions"):
            self.chat_calls += 1
        if path.startswith("/openai/"):
            if bearer != GOOD:
                # OpenAI echoes a masked key back.
                return web.json_response(
                    {"error": {"message": "Incorrect API key provided: "
                               f"{bearer[:8]}***{bearer[-4:]}. Raw: {bearer}",
                               "code": "invalid_api_key"}},
                    status=401,
                )
            if path.endswith("/models"):
                return web.json_response({"data": [{"id": "m-1"}, {"id": "m-2"}]})
            return web.json_response({"choices": [{"message": {"content": "hi"}}]})
        if path.startswith("/dashscope/"):
            if bearer != GOOD:
                return web.json_response(DASHSCOPE_401, status=401)
            if path.endswith("/models"):
                return web.json_response({"data": [{"id": "qwen3.8-max"}]})
            return web.json_response({"choices": [{"message": {"content": "hi"}}]})
        if path.startswith("/quota/"):
            return web.json_response(
                {"error": {"message": "You exceeded your current quota, please check your "
                           "plan and billing details.", "type": "insufficient_quota"}},
                status=429,
            )
        if path.startswith("/down/"):
            return web.Response(status=503, text="upstream unavailable")
        if path.startswith("/slow/"):
            await asyncio.sleep(5)
            return web.json_response({"data": []})
        if path.startswith("/nolist/"):
            if path.endswith("/models"):
                return web.Response(status=404, text="not found")
            if bearer != GOOD:
                return web.json_response({"error": {"message": "Authentication Fails"}}, status=401)
            return web.json_response({"choices": [{"message": {"content": "h"}}]})
        if path.startswith("/scoped/"):
            if path.endswith("/models"):
                return web.json_response(
                    {"error": {"message": "You have insufficient permissions for this "
                               "operation. Missing scopes: api.model.read."}},
                    status=403,
                )
            return web.json_response({"choices": []})
        if path.startswith("/minimax/"):
            return web.json_response(
                {"base_resp": {"status_code": 1004, "status_msg": "login fail: Please carry "
                               "the API secret key in the Authorization field"}}
            )
        if path.startswith("/openrouter/"):
            if path.endswith("/key"):
                if bearer != GOOD:
                    return web.json_response(
                        {"error": {"message": "User not found.", "code": 401}}, status=401
                    )
                return web.json_response({"data": {"label": "x"}})
            return web.json_response({"data": [{"id": "public"}]})
        if path.startswith("/anthropic/"):
            if request.headers.get("x-api-key") != GOOD:
                return web.json_response(
                    {"type": "error",
                     "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
                    status=401,
                )
            return web.json_response({"data": [{"id": "claude-x"}], "has_more": False})
        if path.startswith("/gemini/"):
            if request.headers.get("x-goog-api-key") != GOOD:
                return web.json_response(
                    {"error": {"code": 400,
                               "message": "API key not valid. Please pass a valid API key.",
                               "status": "INVALID_ARGUMENT",
                               "details": [{"reason": "API_KEY_INVALID"}]}},
                    status=400,
                )
            return web.json_response({"models": [{"name": "models/gemini-x"}]})
        return web.Response(status=404)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]  # noqa: SLF001
        port = sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


@pytest.fixture(autouse=True)
def _verify_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite turns the check off; every vendor here is the local fake.
    monkeypatch.setenv("SNOWPEA_KEY_VERIFY", "1")
    monkeypatch.delenv("SNOWPEA_PROVIDER", raising=False)
    key_verify.cache_clear()
    model_discovery.cache_clear()


@pytest.fixture
async def vendor() -> AsyncIterator[FakeVendor]:
    fake = FakeVendor()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


async def _check(preset_id: str, base: str, key: str | None, **kwargs: Any) -> Any:
    return await key_verify.verify_credentials(
        PRESETS[preset_id], api_key=key, base_url=base, **kwargs
    )


# ---------------------------------------------------------------------------
# the probe, per family
# ---------------------------------------------------------------------------


async def test_openai_compatible_ok_and_wrong(vendor: FakeVendor) -> None:
    ok = await _check("openai", f"{vendor.base}/openai/v1", GOOD)
    assert ok.state == "ok" and ok.checked_at is not None
    bad = await _check("openai", f"{vendor.base}/openai/v1", WRONG)
    assert bad.state == "invalid" and bad.reason == "auth"
    assert WRONG not in bad.message and WRONG[:8] not in bad.message
    assert "Incorrect API key" in bad.message


async def test_dashscope_body_is_invalid(vendor: FakeVendor) -> None:
    bad = await _check("qwen", f"{vendor.base}/dashscope/compatible-mode/v1", WRONG)
    assert bad.state == "invalid"
    assert bad.message == "Incorrect API key provided."
    good = await _check("qwen", f"{vendor.base}/dashscope/compatible-mode/v1", GOOD)
    assert good.state == "ok"


async def test_quota_means_the_key_is_known(vendor: FakeVendor) -> None:
    result = await _check("deepseek", f"{vendor.base}/quota/v1", WRONG)
    assert (result.state, result.reason) == ("ok", "quota")
    assert "quota" in result.message


async def test_server_error_and_refused_connection_are_unreachable(vendor: FakeVendor) -> None:
    down = await _check("xai", f"{vendor.base}/down/v1", GOOD)
    assert (down.state, down.reason) == ("unreachable", "server")
    refused = await _check("xai", "http://127.0.0.1:9/v1", GOOD)
    assert (refused.state, refused.reason) == ("unreachable", "network")
    assert GOOD not in refused.message


async def test_timeout_is_unreachable(vendor: FakeVendor) -> None:
    slow = await _check("xai", f"{vendor.base}/slow/v1", GOOD, timeout=0.3)
    assert (slow.state, slow.reason) == ("unreachable", "timeout")


async def test_missing_listing_falls_back_to_a_one_token_chat(vendor: FakeVendor) -> None:
    bad = await _check("glm", f"{vendor.base}/nolist/v1", WRONG)
    assert bad.state == "invalid"
    good = await _check("glm", f"{vendor.base}/nolist/v1", GOOD)
    assert good.state == "ok"
    chats = [path for method, path in vendor.calls if method == "POST"]
    assert chats == ["/nolist/v1/chat/completions"] * 2


async def test_a_key_without_the_list_scope_is_valid(vendor: FakeVendor) -> None:
    result = await _check("openai", f"{vendor.base}/scoped/v1", GOOD)
    assert result.state == "ok"


async def test_minimax_login_fail_with_http_200(vendor: FakeVendor) -> None:
    result = await _check("minimax", f"{vendor.base}/minimax/v1", WRONG)
    assert result.state == "invalid" and "login fail" in result.message


async def test_openrouter_asks_the_key_endpoint(vendor: FakeVendor) -> None:
    bad = await _check("openrouter", f"{vendor.base}/openrouter/api/v1", WRONG)
    assert bad.state == "invalid"
    assert ("GET", "/openrouter/api/v1/key") in vendor.calls
    good = await _check("openrouter", f"{vendor.base}/openrouter/api/v1", GOOD)
    assert good.state == "ok"


async def test_anthropic_and_gemini(vendor: FakeVendor) -> None:
    bad = await _check("anthropic", f"{vendor.base}/anthropic", WRONG)
    assert bad.state == "invalid" and "invalid x-api-key" in bad.message
    assert (await _check("anthropic", f"{vendor.base}/anthropic", GOOD)).state == "ok"
    bad = await _check("gemini", f"{vendor.base}/gemini/v1beta", WRONG)
    assert bad.state == "invalid" and "API key not valid" in bad.message
    assert (await _check("gemini", f"{vendor.base}/gemini/v1beta", GOOD)).state == "ok"


async def test_local_server_without_a_key(vendor: FakeVendor) -> None:
    preset = preset_for("local", None, {"base_url": f"{vendor.base}/openai/v1"})
    result = await key_verify.verify_credentials(
        preset, api_key=None, base_url=f"{vendor.base}/openai/v1"
    )
    assert result.state == "invalid"  # this server wants a key


async def test_switched_off_asks_nothing(
    vendor: FakeVendor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNOWPEA_KEY_VERIFY", "0")
    result = await _check("openai", f"{vendor.base}/openai/v1", WRONG)
    assert (result.state, result.reason) == ("unchecked", "disabled")
    assert vendor.calls == []


def test_auth_failure_words() -> None:
    assert key_verify.is_auth_failure("qwen (qwen3.8-max): HTTP 401: {...}")
    assert key_verify.is_auth_failure("anthropic: AuthenticationError: Error code: 401")
    assert key_verify.is_auth_failure("gemini (x): HTTP 400: API key not valid.")
    assert not key_verify.is_auth_failure("openai (x): HTTP 403: unsupported_country_region")
    assert not key_verify.is_auth_failure("x: HTTP 400: context length exceeded")
    # A model the account may not use is not a bad key.
    assert not key_verify.is_auth_failure("x (m): HTTP 403: unauthorized to use this model")


def test_redact_drops_partial_echoes() -> None:
    text = f"bad key {GOOD[:8]}***{GOOD[-4:]} and {GOOD}"
    out = key_verify.redact(text, GOOD)
    assert GOOD not in out and GOOD[:8] not in out and GOOD[-4:] not in out


# ---------------------------------------------------------------------------
# provider.configure / provider.list / provider.models / provider.verify
# ---------------------------------------------------------------------------


def _core(tmp_path: Path) -> Any:
    from snowpea_core.config.paths import Paths
    from snowpea_core.server.app_server import Core

    paths = Paths(home=tmp_path)
    paths.ensure()
    settings = Settings()
    core = Core(settings=settings, paths=paths, token="t")
    core.providers.bind(settings, paths)
    return core


def _configure(vendor_id: str, config: dict[str, Any], force: bool | None = None) -> Any:
    from snowpea_core.server.protocol import ProviderConfigureParams

    return ProviderConfigureParams(vendor=vendor_id, config=config, force=force)


async def test_configure_refuses_a_rejected_key(
    vendor: FakeVendor, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from snowpea_core.server.app_server import provider_configure_handler

    caplog.set_level(logging.DEBUG)
    core = _core(tmp_path)
    base = f"{vendor.base}/dashscope/compatible-mode/v1"
    with pytest.raises(RpcError) as caught:
        await provider_configure_handler(
            None, _configure("qwen", {"api_key": WRONG, "base_url": base}), core  # type: ignore[arg-type]
        )
    error = caught.value
    assert error.code == "invalid_params"
    assert error.data["reason"] == "invalid_key" and error.data["vendor"] == "qwen"
    assert error.data["verify"]["state"] == "invalid"
    assert "Incorrect API key provided" in error.message
    # Nothing stored, in memory or on disk.
    assert "qwen" not in core.settings.providers
    assert not (tmp_path / "settings.json").exists()
    # The key never shows up in what is said or logged.
    assert WRONG not in error.message and WRONG not in json.dumps(error.data)
    assert WRONG not in caplog.text


async def test_configure_force_stores_and_reports(vendor: FakeVendor, tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_configure_handler
    from snowpea_core.server.session_handlers import provider_list_handler

    core = _core(tmp_path)
    base = f"{vendor.base}/dashscope/compatible-mode/v1"
    result = await provider_configure_handler(
        None, _configure("qwen", {"api_key": WRONG, "base_url": base}, force=True), core  # type: ignore[arg-type]
    )
    assert result.ok and result.verify is not None and result.verify.state == "invalid"
    assert core.settings.providers["qwen"]["api_key"] == WRONG
    rows = {row.vendor: row for row in (await provider_list_handler(None, None, core)).providers}  # type: ignore[arg-type]
    assert rows["qwen"].configured is True  # unchanged meaning for older clients
    assert rows["qwen"].verify is not None and rows["qwen"].verify.state == "invalid"
    assert rows["openai"].verify is None  # nothing stored, nothing to say


async def test_configure_accepts_a_good_key(vendor: FakeVendor, tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_configure_handler

    core = _core(tmp_path)
    base = f"{vendor.base}/dashscope/compatible-mode/v1"
    result = await provider_configure_handler(
        None, _configure("qwen", {"api_key": GOOD, "base_url": base}), core  # type: ignore[arg-type]
    )
    assert result.verify is not None and result.verify.state == "ok"
    assert result.verify.checkedAt
    # The answer is cached: provider.list asks nobody.
    calls = len(vendor.calls)
    assert core.providers.verify_state("qwen").state == "ok"
    assert len(vendor.calls) == calls


async def test_unreachable_vendor_never_blocks_a_save(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_configure_handler

    core = _core(tmp_path)
    result = await provider_configure_handler(
        None, _configure("xai", {"api_key": WRONG, "base_url": "http://127.0.0.1:9/v1"}), core  # type: ignore[arg-type]
    )
    assert result.verify is not None and result.verify.state == "unreachable"
    assert core.settings.providers["xai"]["api_key"] == WRONG


async def test_model_only_change_is_not_checked(vendor: FakeVendor, tmp_path: Path) -> None:
    from snowpea_core.server.app_server import provider_configure_handler

    core = _core(tmp_path)
    result = await provider_configure_handler(
        None, _configure("qwen", {"model": "qwen3.8-flash"}), core  # type: ignore[arg-type]
    )
    assert result.verify is None and vendor.calls == []


async def test_stored_key_is_checked_lazily_and_announced(
    vendor: FakeVendor, tmp_path: Path
) -> None:
    from snowpea_core.server.protocol import ProviderVerifyParams
    from snowpea_core.server.session_handlers import provider_list_handler, provider_verify_handler

    core = _core(tmp_path)
    base = f"{vendor.base}/dashscope/compatible-mode/v1"
    core.settings.providers["qwen"] = {"api_key": WRONG, "base_url": base}
    sent: list[tuple[str, dict[str, Any]]] = []

    class Hub:
        async def notify(self, method: str, params: dict[str, Any]) -> None:
            sent.append((method, params))

    core.hub = Hub()  # type: ignore[assignment]
    rows = {row.vendor: row for row in (await provider_list_handler(None, None, core)).providers}  # type: ignore[arg-type]
    assert rows["qwen"].verify is not None and rows["qwen"].verify.state == "invalid"
    await asyncio.sleep(0.05)
    announced = [p for m, p in sent if m == "provider.verified" and p["vendor"] == "qwen"]
    assert announced and announced[-1]["verify"]["state"] == "invalid"

    # The key is fixed by hand; provider.verify asks again at once.
    core.settings.providers["qwen"]["api_key"] = GOOD
    again = await provider_verify_handler(None, ProviderVerifyParams(vendor="qwen"), core)  # type: ignore[arg-type]
    assert again.verify.state == "ok"


async def test_models_are_not_usable_after_a_401(vendor: FakeVendor, tmp_path: Path) -> None:
    from snowpea_core.server.protocol import ProviderModelsParams
    from snowpea_core.server.session_handlers import provider_models_handler

    core = _core(tmp_path)
    base = f"{vendor.base}/dashscope/compatible-mode/v1"
    core.settings.providers["qwen"] = {"api_key": WRONG, "base_url": base}
    result = await provider_models_handler(None, ProviderModelsParams(vendor="qwen"), core)  # type: ignore[arg-type]
    assert result.source != "live" and result.models  # the curated fallback ...
    assert result.usable is False  # ... nobody can run
    assert result.authError and result.authError.startswith("API key for Qwen was rejected")
    assert WRONG not in result.authError
    assert core.providers.verify_state("qwen").state == "invalid"

    core.settings.providers["qwen"]["api_key"] = GOOD
    model_discovery.cache_clear()
    result = await provider_models_handler(None, ProviderModelsParams(vendor="qwen"), core)  # type: ignore[arg-type]
    assert result.source == "live" and result.usable is True and result.authError is None


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------


async def test_turn_on_a_rejected_key_says_what_to_fix(
    vendor: FakeVendor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_loop, "PROVIDER_RETRY_BASE_SEC", 0.001)
    base = f"{vendor.base}/dashscope/compatible-mode/v1"
    daemon = await make_daemon(
        tmp_path / "home",
        {"providers": {"default": "qwen", "qwen": {"api_key": WRONG, "base_url": base,
                                                   "model": "qwen3.8-max"}}},
    )
    core = daemon.core
    assert core is not None
    try:
        # The boot-time check would already know; stop it and forget, so the
        # first turn reaches the vendor and meets the 401 itself.
        if core.key_check_task is not None:
            await core.key_check_task
        key_verify.cancel_inflight()
        await asyncio.sleep(0)
        key_verify.cache_clear()
        vendor.calls.clear()
        workdir = tmp_path / "w"
        workdir.mkdir()
        session = await core.sessions.create(workdir, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)

        agent_loop.start_turn(core, session, "hello")
        assert session.turn_task is not None
        await asyncio.wait_for(session.turn_task, timeout=20)
        errors = recorder.of_kind("error")
        assert errors, recorder.kinds()
        first = errors[-1]["payload"]
        assert first["message"].startswith(
            "API key for Qwen was rejected — update it in Settings → Providers"
        )
        assert first["details"]["reason"] == "invalid_key"
        assert WRONG not in json.dumps(first)
        chats = vendor.chat_calls
        assert chats >= 1, (vendor.calls, first)

        # The next turn is refused before anything is sent.
        agent_loop.start_turn(core, session, "again")
        assert session.turn_task is not None
        await asyncio.wait_for(session.turn_task, timeout=20)
        assert vendor.chat_calls == chats
        last = recorder.of_kind("error")[-1]["payload"]
        assert last["message"].startswith("API key for Qwen was rejected")
        assert last["code"] == "invalid_params"
    finally:
        await daemon.stop()
