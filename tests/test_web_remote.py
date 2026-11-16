"""snowpea web from another machine: login, cookie session, guarded WebSocket."""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.cli import commands
from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import web_remote
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import PROTOCOL_VERSION
from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.transport_http import create_app
from snowpea_core.server.web_remote import (
    COOKIE_NAME,
    PASSWORD_KEY,
    RemoteAuth,
    create_remote_app,
    hash_password,
    verify_password,
)

PASSWORD = "correct horse battery"


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app></div>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_WEB_DIST", str(root))
    return root


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    paths = Paths.create(tmp_path / "home")
    web_remote.set_password(paths, PASSWORD)
    return paths


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _remote(
    paths: Paths, *, trust_proxy: bool = False, origins: tuple[str, ...] = ()
) -> tuple[TestClient, RemoteAuth, Clock]:
    auth = RemoteAuth("owner", 12, paths)
    auth.failure_delay = 0.0
    clock = Clock()
    auth.clock = clock
    core = SimpleNamespace(token="daemon-token", hub=None)
    app = create_remote_app(
        RpcDispatcher(core), auth, trust_proxy=trust_proxy, allowed_origins=origins
    )
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client, auth, clock


async def _login(
    client: TestClient, username: str = "owner", password: str = PASSWORD, **headers: str
) -> aiohttp.ClientResponse:
    return await client.post(
        "/web/login", json={"username": username, "password": password}, headers=headers
    )


# -- password hash and CLI ----------------------------------------------------


def test_hash_format_and_verify() -> None:
    stored = hash_password("pw-12345")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2**15), "8", "1")
    import base64

    assert len(base64.b64decode(salt)) == 16 and len(base64.b64decode(digest)) == 32
    assert verify_password("pw-12345", stored)
    assert not verify_password("pw-12346", stored)
    assert not verify_password("pw-12345", "garbage")
    assert hash_password("pw-12345") != stored  # salted


def test_password_cli_set_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret-pass\n"))
    assert commands.web_password_command(home, from_stdin=True) == 0
    paths = Paths(home=home)
    data = json.loads(paths.credentials_json.read_text(encoding="utf-8"))
    assert verify_password("s3cret-pass", data[PASSWORD_KEY])
    assert "s3cret-pass" not in paths.credentials_json.read_text(encoding="utf-8")
    if os.name == "posix":
        assert paths.credentials_json.stat().st_mode & 0o777 == 0o600
    assert not (home / "settings.json").exists()
    assert commands.web_password_command(home, clear=True) == 0
    assert PASSWORD_KEY not in CredentialStore(paths).load()


def test_password_cli_prompts_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["first-pass", "other-pass"])
    monkeypatch.setattr(commands.getpass, "getpass", lambda _prompt: next(answers))
    assert commands.web_password_command(tmp_path / "home") != 0
    assert web_remote.stored_hash(Paths(home=tmp_path / "home")) == ""


def test_parser_knows_web_password_and_status() -> None:
    from snowpea_core.cli.main import build_parser

    args = build_parser().parse_args(["web", "password", "--clear"])
    assert args.action == "password" and args.clear
    assert build_parser().parse_args(["web", "status"]).action == "status"
    assert build_parser().parse_args(["web", "--no-open"]).no_open


def test_settings_default_off() -> None:
    remote = Settings().web.remote
    assert remote.enabled is False and remote.port == 8787 and remote.username == ""
    assert remote.sessionHours == 12 and remote.allowedOrigins == [] and not remote.trustProxy


# -- login --------------------------------------------------------------------


async def test_login_session_logout(paths: Paths, dist: Path) -> None:
    client, auth, _ = await _remote(paths)
    try:
        assert (await client.get("/web/session")).status == 401
        ok = await _login(client)
        assert ok.status == 200 and await ok.json() == {"ok": True}
        cookie = ok.headers["Set-Cookie"]
        assert f"{COOKIE_NAME}=" in cookie and "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie and "Path=/" in cookie and "Secure" not in cookie
        session = await client.get("/web/session")
        assert session.status == 200 and session.headers["Cache-Control"] == "no-store"
        body = await session.json()
        capability = body.pop("token")
        assert body == {"ok": True, "username": "owner", "mode": "password"}
        # This login's capability, never the daemon token (security review 5, H3).
        assert capability != "daemon-token" and len(capability) >= 43
        again = await (await client.get("/web/session")).json()
        assert again["token"] == capability  # stable for the life of the login
        assert auth.active() == 1
        out = await client.post("/web/logout")
        assert out.status == 200 and auth.active() == 0
        assert (await client.get("/web/session")).status == 401
    finally:
        await client.close()


async def test_wrong_credentials_do_not_say_which(paths: Paths) -> None:
    client, _, _ = await _remote(paths)
    try:
        for user, password in (("owner", "wrong"), ("intruder", PASSWORD)):
            bad = await _login(client, user, password)
            assert bad.status == 401
            assert await bad.json() == {"ok": False, "error": "invalid_credentials"}
            assert "Set-Cookie" not in bad.headers
        assert (await client.post("/web/login", data="username=owner")).status == 400
        assert (await client.post("/web/login", json=["x"])).status == 400
        huge = await client.post("/web/login", json={"username": "owner", "password": "x" * 10000})
        assert huge.status == 413
    finally:
        await client.close()


async def test_failures_wait_about_300ms(paths: Paths) -> None:
    import time

    client, auth, _ = await _remote(paths)
    auth.failure_delay = 0.3
    try:
        started = time.monotonic()
        assert (await _login(client, password="nope")).status == 401
        assert time.monotonic() - started >= 0.29
    finally:
        await client.close()


async def test_lockout_per_ip_and_retry_after(
    paths: Paths, caplog: pytest.LogCaptureFixture
) -> None:
    client, _, clock = await _remote(paths)
    try:
        with caplog.at_level(logging.INFO, logger="snowpea.web.remote"):
            for _ in range(5):
                assert (await _login(client, password="nope")).status == 401
            locked = await _login(client)  # even the right password
            assert locked.status == 429 and int(locked.headers["Retry-After"]) == 300
            clock.now += 120
            assert int((await _login(client)).headers["Retry-After"]) == 180
            clock.now += 181
            assert (await _login(client)).status == 200
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "failed from 127.0.0.1" in messages and "succeeded" in messages
        assert PASSWORD not in messages and "nope" not in messages
    finally:
        await client.close()


async def test_global_lockout(paths: Paths) -> None:
    client, _, _ = await _remote(paths, trust_proxy=True)
    try:
        for i in range(20):
            ip = f"10.0.0.{i // 4}"  # four failures each: under the per-IP limit
            bad = await _login(client, password="nope", **{"X-Forwarded-For": ip})
            assert bad.status == 401
        wrong = await _login(client, password="nope", **{"X-Forwarded-For": "10.9.9.9"})
        assert wrong.status == 429 and "Retry-After" in wrong.headers
        # Security review 5, L1: the global bucket never refuses the right password.
        fresh = await _login(client, **{"X-Forwarded-For": "10.9.9.8"})
        assert fresh.status == 200
    finally:
        await client.close()


async def test_sweep_forgets_clients_whose_failures_aged_out(paths: Paths) -> None:
    auth = RemoteAuth("owner", 12, paths)
    clock = Clock()
    auth.clock = clock
    auth.record_failure("10.0.0.1")
    await auth.sweep()
    assert "10.0.0.1" in auth._ip_failures, "a recent failure is kept"
    clock.now += web_remote.RATE_WINDOW_SEC + 1
    auth.record_failure("10.0.0.2")
    await auth.sweep()
    assert list(auth._ip_failures) == ["10.0.0.2"]
    clock.now += web_remote.RATE_WINDOW_SEC + 1
    await auth.sweep()
    assert auth._ip_failures == {}


async def test_secure_cookie_only_behind_trusted_proxy(paths: Paths) -> None:
    client, _, _ = await _remote(paths)
    try:
        ok = await _login(client, **{"X-Forwarded-Proto": "https"})
        assert "Secure" not in ok.headers["Set-Cookie"]
    finally:
        await client.close()
    client, _, _ = await _remote(paths, trust_proxy=True)
    try:
        ok = await _login(client, **{"X-Forwarded-Proto": "https"})
        assert "Secure" in ok.headers["Set-Cookie"]
    finally:
        await client.close()


async def test_session_expires(paths: Paths) -> None:
    client, auth, clock = await _remote(paths)
    try:
        assert (await _login(client)).status == 200
        clock.now += 12 * 3600 - 1
        assert (await client.get("/web/session")).status == 200
        clock.now += 2
        assert (await client.get("/web/session")).status == 401
        assert auth.active() == 0
    finally:
        await client.close()


async def test_cross_origin_login_refused(paths: Paths) -> None:
    client, _, _ = await _remote(paths)
    try:
        bad = await _login(client, Origin="http://evil.example")
        assert bad.status == 403
        own = f"http://{client.server.host}:{client.server.port}"
        assert (await _login(client, Origin=own)).status == 200
    finally:
        await client.close()


# -- surface and headers ------------------------------------------------------


async def test_only_the_page_routes_and_headers(paths: Paths, dist: Path) -> None:
    client, _, _ = await _remote(paths)
    try:
        index = await client.get("/web/")
        assert index.status == 200 and "id=app" in await index.text()
        for name, value in web_remote.SECURITY_HEADERS.items():
            assert index.headers[name] == value
        assert index.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in index.headers["Content-Security-Policy"]
        for path in ("/health", "/version", "/protocol.json", "/web/remote/status"):
            response = await client.get(path)
            assert response.status in {404, 200}
            if response.status == 200:  # the SPA fallback, never data
                assert "id=app" in await response.text()
        assert (await client.get("/health")).status == 404
        assert (await client.get("/protocol.json")).status == 404
        missing = await client.get("/nothing")
        assert missing.status == 404 and missing.headers["X-Frame-Options"] == "DENY"
        escape = await client.get("/web/../secret.txt")
        assert "nope" not in await escape.text()
        unauth = await client.get("/web/session")
        assert unauth.headers["Referrer-Policy"] == "no-referrer"
    finally:
        await client.close()


# -- WebSocket ----------------------------------------------------------------


async def test_websocket_needs_cookie_and_origin(paths: Paths) -> None:
    client, _, _ = await _remote(paths, origins=("https://proxy.example",))
    own = f"http://{client.server.host}:{client.server.port}"
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError) as no_cookie:
            await client.ws_connect("/ws", headers={"Origin": own})
        assert no_cookie.value.status == 401
        assert (await _login(client)).status == 200
        with pytest.raises(aiohttp.WSServerHandshakeError) as bad_origin:
            await client.ws_connect("/ws", headers={"Origin": "http://evil.example"})
        assert bad_origin.value.status == 403
        with pytest.raises(aiohttp.WSServerHandshakeError) as no_origin:
            await client.ws_connect("/ws")
        assert no_origin.value.status == 403
        ws = await client.ws_connect("/ws", headers={"Origin": "https://proxy.example"})
        await ws.close()
    finally:
        await client.close()


async def test_logout_closes_open_sockets(paths: Paths) -> None:
    client, _, _ = await _remote(paths)
    own = f"http://{client.server.host}:{client.server.port}"
    try:
        assert (await _login(client)).status == 200
        ws = await client.ws_connect("/ws", headers={"Origin": own})
        assert (await client.post("/web/logout")).status == 200
        message = await ws.receive(timeout=5)
        assert message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
    finally:
        await client.close()


@pytest.mark.parametrize("clear", [False, True])
async def test_password_change_signs_everyone_out(paths: Paths, clear: bool) -> None:
    client, _, _ = await _remote(paths)
    own = f"http://{client.server.host}:{client.server.port}"
    try:
        assert (await _login(client)).status == 200
        ws = await client.ws_connect("/ws", headers={"Origin": own})
        assert (await client.get("/web/session")).status == 200
        if clear:
            assert web_remote.clear_password(paths)
        else:
            web_remote.set_password(paths, "another long password")
        assert (await client.get("/web/session")).status == 401
        message = await ws.receive(timeout=5)
        assert message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
        assert ws.close_code == 4401
    finally:
        await client.close()


async def test_unchanged_hash_keeps_session(paths: Paths) -> None:
    client, _, _ = await _remote(paths)
    try:
        assert (await _login(client)).status == 200
        CredentialStore(paths).set("unrelated.key", "x")
        assert (await client.get("/web/session")).status == 200
    finally:
        await client.close()


# -- loopback listener --------------------------------------------------------


async def test_oauth_return_route_serves_the_app_on_both_listeners(
    paths: Paths, dist: Path
) -> None:
    """The IDE's host-caught OAuth redirect URI ``<origin>/web/mcp-oauth`` (M19).

    The provider sends the browser there with ``code`` and ``state``; both the
    remote listener and the loopback one answer with the app (a client-side
    route), never cached, so the page can finish with ``mcp.authorize.complete``.
    """
    remote, _, _ = await _remote(paths)
    core = SimpleNamespace(token="daemon-token", hub=None)
    loopback = TestClient(TestServer(create_app(RpcDispatcher(core), None, core),
                                     host="127.0.0.1"))
    await loopback.start_server()
    try:
        for client in (remote, loopback):
            for path in ("/web/mcp-oauth", "/web/mcp-oauth?code=abc&state=xyz",
                         "/web/some/deep/route"):
                page = await client.get(path)
                assert page.status == 200, (client, path)
                assert "id=app" in await page.text()
                assert page.headers["Cache-Control"] == "no-cache"
        page = await remote.get("/web/mcp-oauth?code=abc&state=xyz")
        assert page.headers["X-Frame-Options"] == "DENY"
        assert page.headers["Referrer-Policy"] == "no-referrer"
    finally:
        await remote.close()
        await loopback.close()


async def test_loopback_has_no_login_routes(dist: Path) -> None:
    core = SimpleNamespace(token="daemon-token", hub=None)
    app = create_app(RpcDispatcher(core), None, core)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    try:
        session = await client.get("/web/session")
        assert session.status == 404 and "daemon-token" not in await session.text()
        assert (await client.post("/web/login", json={})).status == 404
        assert (await client.get("/web/")).status == 200
        assert (await client.get("/health")).status == 200
        assert (await client.get("/web/remote/status")).status == 401
        status = await client.get(
            "/web/remote/status", headers={"Authorization": "Bearer daemon-token"}
        )
        assert status.status == 200 and await status.json() == {"enabled": False, "sessions": 0}
    finally:
        await client.close()


# -- the daemon ---------------------------------------------------------------


def _write_settings(home: Path, remote: dict[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"web": {"remote": remote}}), encoding="utf-8")


async def test_daemon_has_no_second_listener_by_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        assert not daemon.remote_listener.listening and daemon.remote_auth is None
        assert daemon.remote_status == {
            "enabled": False,
            "host": "0.0.0.0",
            "port": 8787,
            "username": "",
        }
    finally:
        await daemon.stop()


async def test_daemon_refuses_a_password_without_a_username(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "home"
    _write_settings(home, {"enabled": True, "host": "127.0.0.1", "port": 0, "username": ""})
    web_remote.set_password(Paths(home=home), PASSWORD)
    daemon = Daemon(port=0, home=home)
    with caplog.at_level(logging.ERROR, logger="snowpea"):
        await daemon.start()
    try:
        assert not daemon.remote_listener.listening
        assert "username" in daemon.remote_status["error"]
        assert any("not started" in r.getMessage() for r in caplog.records)
    finally:
        await daemon.stop()


async def test_daemon_without_a_password_opens_the_access_link(tmp_path: Path) -> None:
    """Owner decision 2b: no password needed; the access link (a token of its own)."""
    home = tmp_path / "home"
    _write_settings(home, {"enabled": True, "host": "127.0.0.1", "port": 0})
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        assert daemon.remote_listener.listening
        token = web_remote.access_token(Paths(home=home), create=False)
        assert token and token != daemon.token and len(token) >= 43
    finally:
        await daemon.stop()


async def test_daemon_remote_login_reaches_hello(tmp_path: Path, dist: Path) -> None:
    home = tmp_path / "home"
    _write_settings(home, {"enabled": True, "host": "127.0.0.1", "port": 0, "username": "owner"})
    web_remote.set_password(Paths(home=home), PASSWORD)
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        port = daemon.remote_status["port"]
        assert daemon.remote_status["listening"] and port not in (0, daemon.port)
        base = f"http://127.0.0.1:{port}"
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as http:
            async with http.post(
                f"{base}/web/login", json={"username": "owner", "password": PASSWORD}
            ) as ok:
                assert ok.status == 200
            async with http.get(f"{base}/web/session") as session:
                token = (await session.json())["token"]
            assert token != daemon.token  # security review 5, H3: a per-login capability
            ws = await http.ws_connect(f"{base}/ws", headers={"Origin": base})
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "system.hello",
                    "params": {
                        "token": "wrong",
                        "clientVersion": "t",
                        "protocolVersion": PROTOCOL_VERSION,
                    },
                }
            )
            refused = await ws.receive_json(timeout=10)
            assert "error" in refused
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "system.hello",
                    "params": {
                        "token": token,
                        "clientVersion": "t",
                        "protocolVersion": PROTOCOL_VERSION,
                    },
                }
            )
            hello = await ws.receive_json(timeout=10)
            assert hello["result"]["protocolVersion"] == PROTOCOL_VERSION
            await ws.close()
            # The loopback listener is unchanged and keeps its own routes.
            async with http.get(f"http://127.0.0.1:{daemon.port}/health") as health:
                assert health.status == 200
            async with http.get(
                f"http://127.0.0.1:{daemon.port}/web/remote/status",
                headers={"Authorization": f"Bearer {daemon.token}"},
            ) as status:
                body = await status.json()
            assert body["listening"] and body["sessions"] == 1 and body["username"] == "owner"
    finally:
        await daemon.stop()


async def test_web_command_prints_the_remote_address_and_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from snowpea_core.cli.daemon_client import DaemonInfo

    async def fake_daemon(_home: Any) -> DaemonInfo:
        return DaemonInfo(port=4321, pid=1, token="tok")

    async def fake_status(_info: Any) -> dict[str, Any]:
        return {"enabled": True, "listening": True, "host": "192.168.1.5", "port": 8787}

    monkeypatch.setattr(commands, "ensure_daemon", fake_daemon)
    monkeypatch.setattr("snowpea_core.cli.daemon_client.ensure_daemon", fake_daemon)
    monkeypatch.setattr(commands, "_remote_web_status", fake_status)
    assert await commands.web_command(tmp_path, open_browser=False) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:4321/web/?token=tok" in out
    assert "http://192.168.1.5:8787/web/" in out and "plain HTTP" in out

    web_remote.set_password(Paths.create(tmp_path), PASSWORD)
    assert await commands.web_status_command(tmp_path, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["listening"] and report["passwordSet"] and report["sessions"] == 0
    assert report["restartNeeded"]  # settings.json says disabled, the daemon runs it


async def test_parallel_attempts_cannot_outrun_the_limit(paths: Paths) -> None:
    import asyncio

    client, auth, _ = await _remote(paths)
    auth.failure_delay = 0.2
    try:
        results = await asyncio.gather(*(_login(client, password="nope") for _ in range(12)))
        statuses = sorted(r.status for r in results)
        assert statuses.count(401) == 5 and statuses.count(429) == 7
    finally:
        await client.close()
