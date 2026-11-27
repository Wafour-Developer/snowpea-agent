"""1.71.0 (capability providerLoginOpenBrowser): ``provider.loginWeb``
``openBrowser: false`` opens no system browser for any login method, and
``provider.loginProgress`` ``done`` comes only after the credential is saved.

No test here opens a real browser: ``webbrowser.open`` is replaced by a
recorder in every test, and every vendor endpoint is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import auth_web, openai_oauth
from snowpea_core.server import app_server
from snowpea_core.server.app_server import Core, provider_login_web_handler
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    ProviderLoginWebParams,
)

_RealAsyncClient = httpx.AsyncClient


class _FakeConn:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def spawn(self, coro: Any) -> None:
        self.tasks.append(coro)

    async def run_spawned(self) -> None:
        for coro in self.tasks:
            await coro


class _DiskCheckingHub:
    """Records loginProgress and, for each one, whether the vendor's credential
    was already on disk when it was sent."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[tuple[dict[str, Any], bool]] = []

    def _saved(self, vendor: str) -> bool:
        path = self.home / "settings.json"
        if not path.exists():
            return False
        stored = json.loads(path.read_text(encoding="utf-8"))
        return bool(stored.get("providers", {}).get(vendor))

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "provider.loginProgress":
            self.calls.append((dict(params), self._saved(str(params.get("vendor")))))

    def phases(self) -> list[str]:
        return [params["phase"] for params, _ in self.calls]

    def saved_at(self, phase: str) -> bool:
        return next(saved for params, saved in self.calls if params["phase"] == phase)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every URL a login would hand the system browser; none is really opened."""
    urls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **kw: urls.append(url) or True)
    # Without SNOWPEA_TEST the handler's default is to open the browser.
    monkeypatch.delenv("SNOWPEA_TEST", raising=False)
    return urls


def _core(tmp_path: Path) -> Core:
    paths = Paths(home=tmp_path)
    paths.ensure()
    settings = Settings()
    core = Core(settings=settings, paths=paths, token="t")
    core.providers.bind(settings)
    core.hub = _DiskCheckingHub(tmp_path)  # type: ignore[assignment]
    return core


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(
        auth_web.httpx,
        "AsyncClient",
        lambda *a, **kw: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def _device_code_handler() -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/usercode"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.openai.com/activate",
                    "interval": 0,
                    "expires_in": 600,
                },
            )
        return httpx.Response(
            200, json={"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
        )

    return handler


async def _run_device_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> tuple[Core, Any]:
    _mock_http(monkeypatch, _device_code_handler())
    monkeypatch.setattr(auth_web.asyncio, "sleep", _no_sleep)
    core = _core(tmp_path)
    conn = _FakeConn()
    result = await provider_login_web_handler(  # type: ignore[arg-type]
        conn, ProviderLoginWebParams(vendor="openai", method="device_code", **extra), core
    )
    await conn.run_spawned()
    return core, result


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


def test_capability_and_version() -> None:
    assert "providerLoginOpenBrowser" in CAPABILITIES
    assert PROTOCOL_VERSION == "1.81.0"


def test_open_browser_is_optional_and_absent_by_default() -> None:
    params = ProviderLoginWebParams(vendor="openai", method="device_code")
    assert params.openBrowser is None
    assert "openBrowser" not in params.model_dump(exclude_none=True)
    assert ProviderLoginWebParams.model_validate(
        {"vendor": "openai", "method": "web", "openBrowser": False}
    ).openBrowser is False


# ---------------------------------------------------------------------------
# device code
# ---------------------------------------------------------------------------


async def test_device_code_with_open_browser_false_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    core, result = await _run_device_code(tmp_path, monkeypatch, openBrowser=False)
    assert opened == []
    assert result.userCode == "ABCD-1234"
    assert result.verificationUri == "https://auth.openai.com/activate"
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    await_user = next(params for params, _ in hub.calls if params["phase"] == "await_user")
    assert await_user["verificationUri"] == "https://auth.openai.com/activate"
    assert await_user["userCode"] == "ABCD-1234"
    assert hub.phases() == ["started", "await_user", "polling", "done"]


async def test_device_code_by_default_still_opens_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    await _run_device_code(tmp_path, monkeypatch)
    assert opened == ["https://auth.openai.com/activate"]


async def test_device_code_open_browser_true_opens_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    await _run_device_code(tmp_path, monkeypatch, openBrowser=True)
    assert opened == ["https://auth.openai.com/activate"]


async def test_done_is_sent_after_the_credential_is_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    core, _ = await _run_device_code(tmp_path, monkeypatch, openBrowser=False)
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    assert hub.phases()[-1] == "done"
    assert hub.saved_at("done") is True
    assert hub.saved_at("await_user") is False
    done = hub.calls[-1][0]
    assert set(done) <= {"vendor", "method", "phase", "message"}
    assert done["vendor"] == "openai" and done["method"] == "device_code"


async def test_a_failed_save_reports_failed_instead_of_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    async def refuse(*_a: Any, **_kw: Any) -> None:
        raise RpcError(-32603, "disk full")

    monkeypatch.setattr(app_server, "_persist_provider", refuse)
    core, _ = await _run_device_code(tmp_path, monkeypatch, openBrowser=False)
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    assert hub.phases() == ["started", "await_user", "polling", "failed"]
    failed = hub.calls[-1][0]
    assert failed["vendor"] == "openai" and failed["method"] == "device_code"
    assert "disk full" in failed["message"]


# ---------------------------------------------------------------------------
# browser PKCE (ChatGPT)
# ---------------------------------------------------------------------------


async def _run_openai_pkce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> tuple[Core, Any]:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        ),
    )
    core = _core(tmp_path)
    conn = _FakeConn()
    try:
        result = await provider_login_web_handler(  # type: ignore[arg-type]
            conn, ProviderLoginWebParams(vendor="openai", method="browser_pkce", **extra), core
        )
    except RpcError as exc:  # pragma: no cover - depends on the machine
        if "already in use" in str(exc):
            pytest.skip("port 1455 is busy on this machine")
        raise
    url = result.verificationUri or ""
    task = asyncio.create_task(conn.run_spawned())
    await asyncio.sleep(0)
    state = parse_qs(urlparse(url).query)["state"][0]
    async with _RealAsyncClient(timeout=5.0) as browser:
        await browser.get(openai_oauth.REDIRECT_URI, params={"code": "c", "state": state})
    await asyncio.wait_for(task, timeout=5.0)
    return core, result


async def test_openai_pkce_with_open_browser_false_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    core, result = await _run_openai_pkce(tmp_path, monkeypatch, openBrowser=False)
    assert opened == []
    assert result.verificationUri and result.verificationUri.startswith(
        openai_oauth.AUTHORIZE_URL
    )
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    await_user = next(params for params, _ in hub.calls if params["phase"] == "await_user")
    assert await_user["verificationUri"] == result.verificationUri
    assert hub.phases()[-1] == "done" and hub.saved_at("done") is True


async def test_openai_pkce_by_default_still_opens_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    _, result = await _run_openai_pkce(tmp_path, monkeypatch)
    assert opened == [result.verificationUri]


# ---------------------------------------------------------------------------
# Google (gemini)
# ---------------------------------------------------------------------------


async def _run_google(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> tuple[Core, Any]:
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"access_token": "ya29.at", "refresh_token": "1//rt", "expires_in": 3599}
        ),
    )
    core = _core(tmp_path)
    conn = _FakeConn()
    extra.setdefault("method", "google_oauth")
    result = await provider_login_web_handler(  # type: ignore[arg-type]
        conn, ProviderLoginWebParams(vendor="gemini", **extra), core
    )
    url = result.verificationUri or ""
    query = parse_qs(urlparse(url).query)
    task = asyncio.create_task(conn.run_spawned())
    await asyncio.sleep(0)
    async with _RealAsyncClient(timeout=5.0) as browser:
        await browser.get(
            query["redirect_uri"][0], params={"code": "c", "state": query["state"][0]}
        )
    await asyncio.wait_for(task, timeout=5.0)
    return core, result


async def test_google_with_open_browser_false_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    core, result = await _run_google(tmp_path, monkeypatch, openBrowser=False)
    assert opened == []
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    await_user = next(params for params, _ in hub.calls if params["phase"] == "await_user")
    assert await_user["verificationUri"] == result.verificationUri
    assert hub.phases()[-1] == "done" and hub.saved_at("done") is True


async def test_google_by_default_still_opens_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    _, result = await _run_google(tmp_path, monkeypatch)
    assert opened == [result.verificationUri]


async def test_google_adc_with_open_browser_false_is_refused_without_gcloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    launched: list[Any] = []
    monkeypatch.setattr(auth_web.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        auth_web.asyncio,
        "create_subprocess_exec",
        lambda *a, **kw: launched.append(a),  # never awaited: must not be reached
    )
    core = _core(tmp_path)
    conn = _FakeConn()
    with pytest.raises(RpcError) as excinfo:
        await provider_login_web_handler(  # type: ignore[arg-type]
            conn,
            ProviderLoginWebParams(vendor="gemini", method="google_adc", openBrowser=False),
            core,
        )
    assert excinfo.value.code == "login_unsupported"
    assert "google_oauth" in excinfo.value.message
    assert launched == [] and opened == [] and conn.tasks == []
    assert core.hub.calls == []  # type: ignore[attr-defined]


async def test_an_automatic_pick_of_google_adc_becomes_google_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    """Without a browser on the daemon's machine the automatic pick for gemini
    is google_adc; with openBrowser false the client opens the page, so the
    browser flow is used instead."""
    monkeypatch.setattr(auth_web, "browser_available", lambda: False)
    assert auth_web.method_for("gemini", "web") == "google_adc"
    core, _ = await _run_google(tmp_path, monkeypatch, openBrowser=False, method="web")
    assert opened == []
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    assert {params["method"] for params, _ in hub.calls} == {"google_oauth"}
    assert hub.phases()[-1] == "done" and hub.saved_at("done") is True


# ---------------------------------------------------------------------------
# OpenRouter PKCE
# ---------------------------------------------------------------------------


async def test_openrouter_with_open_browser_false_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    _mock_http(monkeypatch, lambda request: httpx.Response(200, json={"key": "or-key"}))
    core = _core(tmp_path)
    conn = _FakeConn()
    result = await provider_login_web_handler(  # type: ignore[arg-type]
        conn, ProviderLoginWebParams(vendor="openrouter", method="web", openBrowser=False), core
    )
    callback_url = (result.verificationUri or "").split("callback_url=", 1)[1].split("&", 1)[0]
    callback_url = callback_url.replace("%3A", ":").replace("%2F", "/")
    async with _RealAsyncClient(timeout=5.0) as browser:
        await browser.get(callback_url, params={"code": "c"})
    await conn.run_spawned()
    assert opened == []
    hub: _DiskCheckingHub = core.hub  # type: ignore[assignment]
    assert hub.phases()[-1] == "done" and hub.saved_at("done") is True
