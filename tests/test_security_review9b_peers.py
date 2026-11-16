"""Security review 9b: unauthenticated peers.

M1: the loopback listener refuses cross-site web pages (``Origin``) and DNS
rebinding (``Host``); a socket that has not said ``system.hello`` may send at
most 64 KiB, has 10 s to authenticate, and at most 64 such sockets stay open
(the oldest is closed).  L1: the failure queues are bounded and wrong
credentials are logged at most once a minute.  L2: ``X-Forwarded-For`` is
believed only from a proxy on this machine.  L3: a pushed message checks the
credential fingerprint.  L4: ``fs.write`` judges the NTFS spellings too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from test_security_review5_remote import _daemon, _link_login, dist  # noqa: F401
from test_security_review8_misc import _write, core  # noqa: F401

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, transport_ws, web_remote
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import PROTOCOL_VERSION, HelloParams
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.transport_http import create_app

TOKEN = "tok"


async def _hello(conn: RpcConnection, params: HelloParams, _core: Any) -> dict[str, Any]:
    if params.token != TOKEN:
        from snowpea_core.server.errors import RpcError

        raise RpcError(errors.UNAUTHORIZED, "invalid token")
    conn.authenticated = True
    return {"ok": True}


async def _client(bind_host: str = "127.0.0.1") -> TestClient:
    dispatcher = RpcDispatcher(SimpleNamespace(hub=None, sessions=None))  # type: ignore[arg-type]
    dispatcher.register("system.hello", _hello, HelloParams)
    client = TestClient(TestServer(create_app(dispatcher, None, None, bind_host=bind_host)))
    await client.start_server()
    return client


def _hello_frame(token: str = TOKEN) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "system.hello",
            "params": {"token": token, "clientVersion": "t", "protocolVersion": PROTOCOL_VERSION}}


# -- M1 a/b: Origin and Host ------------------------------------------------------------------


@pytest.mark.parametrize("origin", [None, "null", "file://", "app://snowpea",
                                    "chrome-extension://abcdef", "moz-extension://x",
                                    "vscode-webview://x", "http://127.0.0.1:{port}",
                                    "http://localhost:{port}", "http://[::1]:{port}"])
async def test_clients_without_a_foreign_web_origin_get_in(origin: str | None) -> None:
    client = await _client()
    try:
        port = client.port
        headers = {"Origin": origin.format(port=port)} if origin else {}
        async with client.get("/health", headers=headers) as answer:
            assert answer.status == 200
        ws = await client.ws_connect("/ws", headers=headers)
        await ws.send_json(_hello_frame())
        assert (await ws.receive_json(timeout=5))["result"] == {"ok": True}
        await ws.close()
    finally:
        await client.close()


@pytest.mark.parametrize("origin", ["http://evil.example", "https://evil.example",
                                    "http://localhost:5173", "http://127.0.0.1",
                                    "http://127.0.0.1.evil.example:{port}", "http://"])
async def test_a_foreign_web_page_is_refused(origin: str) -> None:
    client = await _client()
    try:
        headers = {"Origin": origin.format(port=client.port)}
        for path in ("/health", "/version", "/protocol.json", "/web/"):
            async with client.get(path, headers=headers) as answer:
                assert answer.status == 403, path
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            await client.ws_connect("/ws", headers=headers)
        assert refused.value.status == 403
    finally:
        await client.close()


async def test_the_page_through_a_tunnel_is_its_own_origin() -> None:
    """``ssh -L 9999:127.0.0.1:<port>``: the page and its socket both say 127.0.0.1:9999."""
    client = await _client()
    try:
        headers = {"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:9999"}
        async with client.get("/health", headers=headers) as answer:
            assert answer.status == 200
        headers = {"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:9998"}
        async with client.get("/health", headers=headers) as answer:
            assert answer.status == 403
    finally:
        await client.close()


@pytest.mark.parametrize(("host", "status"), [("evil.example", 403), ("evil.example:80", 403),
                                              ("127.0.0.1.nip.io", 403), ("localhost:1", 200),
                                              ("[::1]:8", 200), ("LOCALHOST", 200)])
async def test_dns_rebinding_is_refused_on_loopback(host: str, status: int) -> None:
    client = await _client()
    try:
        async with client.get("/health", headers={"Host": host}) as answer:
            assert answer.status == status
    finally:
        await client.close()


async def test_a_listener_bound_elsewhere_has_no_host_rule() -> None:
    client = await _client(bind_host="0.0.0.0")
    try:
        async with client.get("/health", headers={"Host": "box.lan:7000"}) as answer:
            assert answer.status == 200
        same = {"Host": "box.lan:7000", "Origin": "http://box.lan:7000"}
        async with client.get("/health", headers=same) as answer:
            assert answer.status == 200
        cross = {"Host": "box.lan:7000", "Origin": "http://evil.example"}
        async with client.get("/health", headers=cross) as answer:
            assert answer.status == 403
    finally:
        await client.close()


async def test_the_loopback_page_sends_no_referrer(dist: Path) -> None:  # noqa: F811
    client = await _client()
    try:
        async with client.get("/web/?token=abc") as page:
            assert page.status == 200
            assert page.headers["Referrer-Policy"] == "no-referrer"
            assert page.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    finally:
        await client.close()


# -- M1 c: before system.hello ------------------------------------------------------------------


async def test_the_gate_sits_in_front_of_aiohttps_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails loudly when aiohttp moves its frame parser (``_payload_parser``)."""
    installed: list[Any] = []
    real = transport_ws.install_pre_auth_gate

    def spy(request: web.Request, conn: RpcConnection) -> Any:
        gate = real(request, conn)
        installed.append((gate, request.protocol._payload_parser))  # noqa: SLF001
        return gate

    monkeypatch.setattr(transport_ws, "install_pre_auth_gate", spy)
    client = await _client()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json(_hello_frame())
        assert "result" in await ws.receive_json(timeout=5)
        await ws.close()
    finally:
        await client.close()
    gate, parser = installed[0]
    assert isinstance(gate, transport_ws.PreAuthGate) and parser is gate


async def test_too_much_before_hello_closes_with_1009() -> None:
    client = await _client()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_str("x" * (transport_ws.PRE_AUTH_MAX_BYTES + 1024))
        msg = await ws.receive(timeout=5)
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
        assert ws.close_code == 1009
    finally:
        await client.close()


async def test_many_small_frames_before_hello_count_together() -> None:
    client = await _client()
    try:
        ws = await client.ws_connect("/ws")
        for _ in range(40):
            if ws.closed:
                break
            await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "x",
                                          "params": {"pad": "y" * 4000}}))
        while True:
            msg = await ws.receive(timeout=5)
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                break
        assert ws.close_code == 1009
    finally:
        await client.close()


async def test_after_hello_the_big_limit_applies() -> None:
    client = await _client()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json(_hello_frame())
        assert "result" in await ws.receive_json(timeout=5)
        big = {"jsonrpc": "2.0", "id": 2, "method": "no.such", "params": {"pad": "z" * 2_000_000}}
        await ws.send_json(big)
        answer = await ws.receive_json(timeout=10)
        assert answer["id"] == 2 and "error" in answer
        assert not ws.closed
        await ws.close()
    finally:
        await client.close()


async def test_a_socket_that_never_says_hello_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_ws, "HELLO_DEADLINE_SEC", 0.2)
    client = await _client()
    try:
        silent = await client.ws_connect("/ws")
        greeted = await client.ws_connect("/ws")
        await greeted.send_json(_hello_frame())
        assert "result" in await greeted.receive_json(timeout=5)
        msg = await silent.receive(timeout=5)
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
        assert silent.close_code == transport_ws.CLOSE_POLICY
        await asyncio.sleep(0.3)
        assert not greeted.closed
        await greeted.close()
    finally:
        await client.close()


async def test_a_wrong_hello_does_not_buy_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_ws, "HELLO_DEADLINE_SEC", 0.3)
    client = await _client()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json(_hello_frame("wrong"))
        assert "error" in await ws.receive_json(timeout=5)
        msg = await ws.receive(timeout=5)
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
    finally:
        await client.close()


async def test_beyond_the_cap_the_oldest_unauthenticated_socket_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_ws, "MAX_UNAUTHENTICATED", 2)
    client = await _client()
    try:
        person = await client.ws_connect("/ws")
        await person.send_json(_hello_frame())
        assert "result" in await person.receive_json(timeout=5)
        first = await client.ws_connect("/ws")
        second = await client.ws_connect("/ws")
        third = await client.ws_connect("/ws")
        msg = await first.receive(timeout=5)
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
        assert first.close_code == transport_ws.CLOSE_POLICY
        for ws in (second, third):
            await ws.send_json(_hello_frame())
            assert "result" in await ws.receive_json(timeout=5)
        assert not person.closed  # an authenticated socket never counts
        for ws in (person, second, third):
            await ws.close()
    finally:
        await client.close()


async def test_the_remote_listener_has_the_same_pre_auth_limit(
    tmp_path: Path, dist: Path,  # noqa: F811
) -> None:
    daemon = await _daemon(tmp_path)
    try:
        base = f"http://127.0.0.1:{daemon.remote_status['port']}"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            await _link_login(http, base, daemon.paths.home)
            ws = await http.ws_connect(f"{base}/ws", headers={"Origin": base})
            await ws.send_str("x" * (transport_ws.PRE_AUTH_MAX_BYTES + 1024))
            msg = await ws.receive(timeout=5)
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
            assert ws.close_code == 1009
            # And the daemon's own listener refuses a page from the remote one.
            loopback = f"http://127.0.0.1:{daemon.port}"
            async with http.get(f"{loopback}/health", headers={"Origin": base}) as answer:
                assert answer.status == 403
    finally:
        await daemon.stop()


# -- L1: bounded queues, quiet log --------------------------------------------------------------


def _auth(tmp_path: Path) -> web_remote.RemoteAuth:
    paths = Paths.create(tmp_path / "home")
    return web_remote.RemoteAuth("me", 1, paths)


def test_bounded_failure_queues_answer_like_unbounded_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bounded = _auth(tmp_path)
    monkeypatch.setattr(web_remote, "FAILURE_HISTORY_EXTRA", 10_000)
    unbounded = _auth(tmp_path)
    now = [1000.0]
    for auth in (bounded, unbounded):
        auth.clock = lambda: now[0]
    for step in range(500):
        now[0] += 0.25
        ip = "198.51.100.7" if step % 3 else f"203.0.113.{step % 50}"
        for auth in (bounded, unbounded):
            stamp = auth.record_failure(ip, everyone=step % 5 != 0)
            if step % 7 == 0:
                auth.forgive(ip, stamp)
        for probe in ("198.51.100.7", "203.0.113.1"):
            assert bounded.retry_after(probe) == unbounded.retry_after(probe), step
            assert bounded.penalty(probe) == unbounded.penalty(probe), step
    assert len(bounded._global_failures) <= (  # noqa: SLF001
        web_remote.MAX_FAILURES_GLOBAL + 17)
    assert max(len(q) for q in bounded._ip_failures.values()) <= (  # noqa: SLF001
        web_remote.MAX_FAILURES_PER_IP + 17)


def test_wrong_credentials_are_logged_once_a_minute(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    auth = _auth(tmp_path)
    now = [50.0]
    auth.clock = lambda: now[0]
    with caplog.at_level(logging.WARNING, logger="snowpea.web.remote"):
        for _ in range(500):
            auth.log_failure("web login failed", "198.51.100.7")
            now[0] += 0.01
        assert len(caplog.records) == 1
        now[0] += 60
        auth.log_failure("web login failed", "198.51.100.8")
    assert len(caplog.records) == 2
    assert "499 more" in caplog.records[1].getMessage()


# -- L2: X-Forwarded-For only from a proxy on this machine ---------------------------------


@pytest.mark.parametrize(("peer", "trusted"), [("127.0.0.1", True), ("::1", True),
                                               ("::ffff:127.0.0.1", True),
                                               ("192.0.2.10", False), ("10.0.0.2", False)])
def test_forwarded_headers_are_believed_only_from_a_local_proxy(
    tmp_path: Path, peer: str, trusted: bool
) -> None:
    app = web.Application()
    auth = _auth(tmp_path)
    auth.trust_proxy = True
    app[web_remote.AUTH_KEY] = auth
    app[web_remote.TRUST_PROXY_KEY] = True
    request = make_mocked_request(
        "GET", "/web/", app=app,
        headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"},
    ).clone(remote=peer)
    assert web_remote._client_ip(request) == ("203.0.113.9" if trusted else peer)  # noqa: SLF001
    assert web_remote._is_https(request) is trusted  # noqa: SLF001


# -- L3: a pushed message checks the credentials --------------------------------------------


def test_a_push_stops_once_the_password_changes_outside_the_daemon(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    web_remote.access_token(auth.paths)
    now = [10.0]
    auth.clock = lambda: now[0]
    token = auth.open_session()
    assert auth.push_alive(token)
    web_remote.set_password(auth.paths, "a-new-password")  # what `snowpea web password` does
    now[0] += 1.5
    assert not auth.push_alive(token)
    assert auth.session(token) is None
    fresh = auth.open_session()  # a login after the change carries the new print
    assert auth.push_alive(fresh)


def test_a_push_stops_once_the_link_is_rotated(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    web_remote.access_token(auth.paths)
    now = [10.0]
    auth.clock = lambda: now[0]
    token = auth.open_session()
    assert auth.push_alive(token)
    web_remote.rotate_access_token(auth.paths)
    now[0] += 1.5
    assert not auth.push_alive(token)
    expired = auth.open_session()
    assert auth.push_alive(expired)
    now[0] += auth.session_sec + 1
    assert not auth.push_alive(expired)


# -- L4: fs.write and every spelling of a protected path -----------------------------------


def _protect(core: Core, path: Path, level: str = "write") -> None:  # noqa: F811
    core.settings.security.protectedPaths.custom = [  # type: ignore[assignment]
        Settings.model_validate({"security": {"protectedPaths": {"custom": [
            {"path": str(path), "level": level}]}}}).security.protectedPaths.custom[0]
    ]


@pytest.mark.parametrize("spelling", ["vault.txt::$DATA", "vault.txt.", "vault.txt "])
async def test_fs_write_judges_the_ntfs_spellings(
    core: Core, tmp_path: Path, spelling: str  # noqa: F811
) -> None:
    work = tmp_path / "project"
    work.mkdir()
    _protect(core, work / "vault.txt")
    session = await core.sessions.create(work)
    for kind in ("person", "remote_login"):
        assert await _write(core, kind, session.id, spelling) == errors.SECURITY_BLOCKED
    assert await _write(core, "remote_login", session.id, "notes.txt") == "ok"


async def test_fs_write_folds_case_where_the_file_system_does(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from snowpea_core.security import check

    work = tmp_path / "project"
    (work / "vault").mkdir(parents=True)
    (work / "VAULT").mkdir()
    _protect(core, work / "vault")
    session = await core.sessions.create(work)
    monkeypatch.setattr(check, "_FOLD_CASE", True)
    assert await _write(core, "remote_login", session.id, "VAULT/a.txt") == errors.SECURITY_BLOCKED
    assert await _write(core, "person", session.id, "Vault/a.txt") == errors.SECURITY_BLOCKED
