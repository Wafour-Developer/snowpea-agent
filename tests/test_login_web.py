"""CORE-login-progress: ``provider.loginWeb`` answers immediately with the
device code / PKCE URL and finishes the flow in the background, reporting
each phase via ``provider.loginProgress``.

See ``docs/design/deviations/CORE-login-progress.md`` for scope notes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import auth_web
from snowpea_core.server.app_server import Core, provider_login_web_handler
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import ProviderLoginWebParams

#: The real class, captured before any test patches ``auth_web.httpx.AsyncClient``.
_RealAsyncClient = httpx.AsyncClient


class _FakeConn:
    """Stands in for :class:`RpcConnection`: records the spawned background task
    and lets the test await it directly instead of racing the event loop."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def spawn(self, coro: Any) -> None:
        self.tasks.append(coro)

    async def run_spawned(self) -> None:
        for coro in self.tasks:
            await coro


class _FakeHub:
    """Records every ``provider.loginProgress`` notification a test observes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.calls.append((method, dict(params)))


def _core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Core:
    monkeypatch.setenv("SNOWPEA_TEST", "1")  # keeps auth_web from opening a browser
    paths = Paths(home=tmp_path)
    paths.ensure()
    settings = Settings()
    core = Core(settings=settings, paths=paths, token="t")
    core.providers.bind(settings)
    core.hub = _FakeHub()  # type: ignore[assignment]
    return core


def _phases(hub: _FakeHub, vendor: str) -> list[str]:
    return [
        params["phase"]
        for method, params in hub.calls
        if method == "provider.loginProgress" and params.get("vendor") == vendor
    ]


async def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# device code (openai)
# ---------------------------------------------------------------------------


async def test_login_web_returns_user_code_immediately_and_finishes_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "WXYZ-1234",
                    "verification_uri": "https://auth.openai.com/activate",
                    "interval": 0,
                    "expires_in": 600,
                },
            )
        polls += 1
        if polls < 2:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200, json={"access_token": "tok-abc", "refresh_token": "ref-abc", "expires_in": 3600}
        )

    monkeypatch.setattr(
        auth_web.httpx,
        "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(auth_web.asyncio, "sleep", _no_sleep)

    core = _core(tmp_path, monkeypatch)
    conn = _FakeConn()
    params = ProviderLoginWebParams(vendor="openai", method="web")

    result = await provider_login_web_handler(conn, params, core)  # type: ignore[arg-type]

    assert result.ok
    assert result.status == "await_user"
    assert result.userCode == "WXYZ-1234"
    assert result.verificationUri == "https://auth.openai.com/activate"

    # Nothing is persisted yet — the background task hasn't run.
    assert not (tmp_path / "settings.json").exists()

    await conn.run_spawned()

    assert _phases(core.hub, "openai") == ["started", "await_user", "polling", "done"]  # type: ignore[arg-type]
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["providers"]["openai"]["token"] == "tok-abc"
    assert stored["providers"]["openai"]["refresh_token"] == "ref-abc"


async def test_login_web_reports_failure_phase_and_does_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(
        auth_web.httpx,
        "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(auth_web.asyncio, "sleep", _no_sleep)

    core = _core(tmp_path, monkeypatch)
    conn = _FakeConn()
    params = ProviderLoginWebParams(vendor="openai", method="web")

    result = await provider_login_web_handler(conn, params, core)  # type: ignore[arg-type]
    assert result.status == "await_user"

    await conn.run_spawned()

    assert _phases(core.hub, "openai") == ["started", "await_user", "polling", "failed"]  # type: ignore[arg-type]
    failed = next(
        params
        for method, params in core.hub.calls  # type: ignore[attr-defined]
        if method == "provider.loginProgress" and params.get("phase") == "failed"
    )
    assert "access_denied" in failed["message"]
    assert not (tmp_path / "settings.json").exists()


# ---------------------------------------------------------------------------
# Gemini Google ADC
# ---------------------------------------------------------------------------


async def test_gemini_google_adc_login_persists_only_auth_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"Credentials saved\n", b""

    monkeypatch.setattr(auth_web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        auth_web.asyncio, "create_subprocess_exec", lambda *a, **kw: _async_value(Process())
    )
    core = _core(tmp_path, monkeypatch)
    conn = _FakeConn()
    result = await provider_login_web_handler(  # type: ignore[arg-type]
        conn, ProviderLoginWebParams(vendor="gemini", method="web"), core
    )
    assert result.status == "await_user"
    await conn.run_spawned()
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["providers"]["gemini"] == {"auth_method": "google_adc"}


async def _async_value(value: Any) -> Any:
    return value


# ---------------------------------------------------------------------------
# API-key-only vendors stay unsupported
# ---------------------------------------------------------------------------


async def test_login_web_unsupported_vendor_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = _core(tmp_path, monkeypatch)
    conn = _FakeConn()
    params = ProviderLoginWebParams(vendor="deepseek", method="web")

    with pytest.raises(RpcError) as excinfo:
        await provider_login_web_handler(conn, params, core)  # type: ignore[arg-type]
    assert excinfo.value.code == "login_unsupported"
    assert "snowpea setup --vendor deepseek" in excinfo.value.message
    assert conn.tasks == []


# ---------------------------------------------------------------------------
# oauth pkce (openrouter)
# ---------------------------------------------------------------------------


async def test_login_web_pkce_returns_verification_uri_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchanged: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        exchanged.update(json.loads(request.content))
        return httpx.Response(200, json={"key": "or-key-xyz"})

    monkeypatch.setattr(
        auth_web.httpx,
        "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )

    core = _core(tmp_path, monkeypatch)
    conn = _FakeConn()
    params = ProviderLoginWebParams(vendor="openrouter", method="web")

    result = await provider_login_web_handler(conn, params, core)  # type: ignore[arg-type]
    assert result.status == "await_user"
    assert result.verificationUri is not None
    assert "openrouter.ai/auth" in result.verificationUri

    # Approve the login by hitting the one-shot localhost callback server the
    # background task started when it built ``verificationUri``.
    callback_url = result.verificationUri.split("callback_url=", 1)[1].split("&", 1)[0]
    callback_url = callback_url.replace("%3A", ":").replace("%2F", "/")
    async with _RealAsyncClient() as browser:
        await browser.get(callback_url, params={"code": "code-789"})

    await conn.run_spawned()

    assert _phases(core.hub, "openrouter") == ["started", "await_user", "polling", "done"]  # type: ignore[arg-type]
    assert exchanged["code"] == "code-789"
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["providers"]["openrouter"]["api_key"] == "or-key-xyz"
