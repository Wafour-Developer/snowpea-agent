"""Remote web access at runtime (owner decision 2b, 1.54.0): the access link,
password mode, ``web.remote.*``.  Every listener binds 127.0.0.1 with port 0."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.config.paths import Paths
from snowpea_core.server import errors, web_remote
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION
from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.web_remote import COOKIE_NAME, RemoteAuth, create_remote_app

PASSWORD = "correct horse battery"


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app></div>", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_WEB_DIST", str(root))
    return root


async def _remote(paths: Paths, username: str = "") -> tuple[TestClient, RemoteAuth]:
    auth = RemoteAuth(username, 12, paths)
    auth.failure_delay = 0.0
    core = SimpleNamespace(token="daemon-token", hub=None)
    app = create_remote_app(RpcDispatcher(core), auth)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client, auth


async def test_the_access_link_trades_the_token_for_a_cookie(tmp_path: Path, dist: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    token = web_remote.access_token(paths)
    client, auth = await _remote(paths)
    try:
        page = await client.get(f"/web/?t={token}&x=1", allow_redirects=False)
        assert page.status == 302
        assert page.headers["Location"] == "/web/?x=1"  # the token is gone from the address
        cookie = page.cookies[COOKIE_NAME]
        assert cookie["httponly"] and cookie["samesite"] == "Strict"
        session = await client.get("/web/session")
        body = await session.json()
        assert body["ok"] and body["mode"] == "token" and body["token"] == "daemon-token"
        assert auth.active() == 1
    finally:
        await client.close()


async def test_wrong_tokens_share_the_password_limit(tmp_path: Path, dist: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    web_remote.access_token(paths)
    client, _auth = await _remote(paths)
    try:
        statuses = [(await client.get("/web/?t=wrong", allow_redirects=False)).status
                    for _ in range(6)]
        assert statuses[:5] == [401] * 5 and statuses[5] == 429
        login = await client.post("/web/login", json={"username": "x", "password": "y"})
        assert login.status == 429  # the same counter
    finally:
        await client.close()


async def test_rotation_ends_every_session(tmp_path: Path, dist: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    token = web_remote.access_token(paths)
    client, auth = await _remote(paths)
    try:
        await client.get(f"/web/?t={token}", allow_redirects=False)
        assert auth.active() == 1
        fresh = web_remote.rotate_access_token(paths)
        assert fresh != token
        assert auth.active() == 0
        assert (await client.get(f"/web/?t={token}", allow_redirects=False)).status == 401
    finally:
        await client.close()


async def test_password_mode_is_stricter_than_the_link(tmp_path: Path, dist: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    token = web_remote.access_token(paths)
    web_remote.set_password(paths, PASSWORD)
    client, auth = await _remote(paths, username="owner")
    try:
        page = await client.get(f"/web/?t={token}", allow_redirects=False)
        assert page.status == 302 and COOKIE_NAME not in page.cookies
        assert page.headers["Location"] == "/web/"
        assert (await client.get("/web/session")).status == 401
        assert auth.active() == 0
        ok = await client.post("/web/login", json={"username": "owner", "password": PASSWORD})
        assert ok.status == 200 and (await (await client.get("/web/session")).json())[
            "mode"] == "password"
    finally:
        await client.close()


def test_contract() -> None:
    assert "webRemoteControl" in CAPABILITIES


@pytest.mark.parametrize("method", ["web.remote.status", "web.remote.listen",
                                    "web.remote.stop", "web.remote.setPassword",
                                    "web.remote.clearPassword", "web.remote.rotateToken"])
def test_only_a_local_person(method: str) -> None:
    from snowpea_core.server.person_gate import method_refusal

    def conn(kind: str, remote: bool = False) -> Any:
        return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                               remote_login=remote)

    method_refusal(method, conn("ide"))
    for other in (conn("web", True), conn("browser"), conn("connector")):
        with pytest.raises(RpcError) as caught:
            method_refusal(method, other)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY


async def _rpc(http: aiohttp.ClientSession, port: int, token: str, calls: list[tuple[str, dict]]
               ) -> list[dict[str, Any]]:
    ws = await http.ws_connect(f"http://127.0.0.1:{port}/ws")
    await ws.send_json({"jsonrpc": "2.0", "id": 0, "method": "system.hello",
                        "params": {"token": token, "clientVersion": "t", "clientKind": "ide",
                                   "protocolVersion": PROTOCOL_VERSION}})
    await ws.receive_json(timeout=10)
    out = []
    for index, (method, params) in enumerate(calls, start=1):
        await ws.send_json({"jsonrpc": "2.0", "id": index, "method": method, "params": params})
        while True:
            frame = await ws.receive_json(timeout=20)
            if frame.get("id") == index:
                out.append(frame)
                break
    await ws.close()
    return out


async def test_listen_and_stop_at_runtime_without_a_restart(tmp_path: Path, dist: Path) -> None:
    home = tmp_path / "home"
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            frames = await _rpc(http, daemon.port, daemon.token, [
                ("web.remote.status", {}),
                ("web.remote.listen", {"host": "127.0.0.1", "port": 0, "save": True}),
            ])
            assert frames[0]["result"]["listening"] is False
            status = frames[1]["result"]
            assert status["listening"] and status["mode"] == "token"
            assert status["accessUrl"] and "plain HTTP" in status["warning"]
            port = status["port"]
            settings = json.loads((home / "settings.json").read_text())
            assert settings["web"]["remote"]["enabled"] is True
            # The link opens the page; the socket behind it is a remote login.
            token = web_remote.access_token(Paths(home=home), create=False)
            base = f"http://127.0.0.1:{port}"
            async with http.get(f"{base}/web/?t={token}", allow_redirects=False) as page:
                assert page.status == 302
            ws = await http.ws_connect(f"{base}/ws", headers={"Origin": base})
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "system.hello",
                                "params": {"token": daemon.token, "clientVersion": "t",
                                           "clientKind": "web",
                                           "protocolVersion": PROTOCOL_VERSION}})
            await ws.receive_json(timeout=10)
            remote = [c for c in daemon.core.hub.clients() if getattr(c, "client_kind", "")
                      == "web"]
            assert remote and all(c.remote_login for c in remote)
            # A remote login cannot widen its own door.
            await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "web.remote.rotateToken",
                                "params": {}})
            refused = await ws.receive_json(timeout=10)
            assert refused["error"]["data"]["code"] == "jarvis_person_only"
            await ws.close()
            stopped = (await _rpc(http, daemon.port, daemon.token,
                                  [("web.remote.stop", {"save": True})]))[0]["result"]
            assert stopped["listening"] is False
    finally:
        await daemon.stop()
