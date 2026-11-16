"""Security review 5: the remote web listener (H3, M3, M4, L1).

H3: a remote login never receives the daemon token; ``/web/session`` hands out a
per-login capability that only that login's sockets on the remote listener
accept, and that dies with the login.  M3: on the remote listener every
connection is a remote login, whatever ``system.hello`` declares.  M4: no
access log line carries the access link's token or the loopback page's token.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.server import web_remote
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.person_gate import connection_kind
from snowpea_core.server.protocol import PROTOCOL_VERSION


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app></div>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_WEB_DIST", str(root))
    return root


async def _daemon(tmp_path: Path) -> Daemon:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(
        {"web": {"remote": {"enabled": True, "host": "127.0.0.1", "port": 0}}}), encoding="utf-8")
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    assert daemon.remote_listener.listening
    return daemon


def _hello(token: str, **extra: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "system.hello",
            "params": {"token": token, "clientVersion": "t", "protocolVersion": PROTOCOL_VERSION,
                       **extra}}


async def _ws_hello(http: aiohttp.ClientSession, url: str, token: str,
                    origin: str | None = None, **extra: Any) -> tuple[Any, dict[str, Any]]:
    headers = {"Origin": origin} if origin else {}
    ws = await http.ws_connect(url, headers=headers)
    await ws.send_json(_hello(token, **extra))
    return ws, await ws.receive_json(timeout=10)


async def _link_login(http: aiohttp.ClientSession, base: str, home: Path) -> str:
    """Open the access link; answer this login's capability."""
    link = web_remote.access_token(Paths(home=home), create=False)
    async with http.get(f"{base}/web/?t={link}", allow_redirects=False) as page:
        assert page.status == 302
    async with http.get(f"{base}/web/session") as answer:
        body = await answer.json()
    return str(body["token"])


async def test_the_daemon_token_never_leaves_the_remote_listener(tmp_path: Path,
                                                                 dist: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        port = daemon.remote_status["port"]
        base = f"http://127.0.0.1:{port}"
        seen: list[str] = []
        visited: set[str] = set()
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:

            async def record(method: str, path: str, route: str, **kw: Any) -> None:
                async with http.request(method, f"{base}{path}", allow_redirects=False,
                                        **kw) as response:
                    seen.append(await response.text())
                    seen.append(json.dumps(dict(response.headers)))
                visited.add(route)

            # Before and after a login, every route the listener has.
            await record("GET", "/web/session", "/web/session")
            await record("POST", "/web/login", "/web/login",
                         json={"username": "x", "password": "y"})
            capability = await _link_login(http, base, daemon.paths.home)
            assert capability and capability != daemon.token
            await record("GET", "/web/session", "/web/session")
            await record("GET", "/web", "/web")
            await record("GET", "/web/", "/web/{tail}")
            await record("GET", "/web/assets/app.js", "/web/{tail}")
            await record("GET", "/web/some/route?x=1", "/web/{tail}")
            ws, hello = await _ws_hello(http, f"{base}/ws", capability, origin=base,
                                        clientKind="web")
            assert "result" in hello, hello
            seen.append(json.dumps(hello))
            for index, method in enumerate(("system.info", "system.health", "web.remote.status",
                                            "settings.get"), start=2):
                await ws.send_json({"jsonrpc": "2.0", "id": index, "method": method,
                                    "params": {}})
                while True:
                    frame = await ws.receive_json(timeout=10)
                    seen.append(json.dumps(frame))
                    if frame.get("id") == index:
                        break
            await ws.close()
            visited.add("/ws")
            await record("POST", "/web/logout", "/web/logout", headers={"Origin": base})
        routes = {
            str(route.resource.canonical)
            for route in daemon.remote_listener.runner.app.router.routes()  # type: ignore[union-attr]
            if route.method != "HEAD"
        }
        assert routes <= visited, sorted(routes - visited)
        for text in seen:
            assert daemon.token not in text
    finally:
        await daemon.stop()


async def test_the_capability_is_only_this_logins_on_this_listener(tmp_path: Path,
                                                                    dist: Path) -> None:
    daemon = await _daemon(tmp_path)
    try:
        base = f"http://127.0.0.1:{daemon.remote_status['port']}"
        loopback = f"http://127.0.0.1:{daemon.port}/ws"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            capability = await _link_login(http, base, daemon.paths.home)
            # The daemon token is refused on the remote listener …
            ws, frame = await _ws_hello(http, f"{base}/ws", daemon.token, origin=base)
            assert frame["error"]["data"]["code"] == "unauthorized"
            await ws.close()
            # … and the capability on the loopback listener.
            ws, frame = await _ws_hello(http, loopback, capability)
            assert frame["error"]["data"]["code"] == "unauthorized"
            await ws.close()
            ws, frame = await _ws_hello(http, f"{base}/ws", capability, origin=base)
            assert "result" in frame
            await ws.close()
            # Logout: the capability is gone with the login.
            async with http.post(f"{base}/web/logout", headers={"Origin": base}):
                pass
            second = await _link_login(http, base, daemon.paths.home)
            assert second != capability
            ws, frame = await _ws_hello(http, f"{base}/ws", capability, origin=base)
            assert frame["error"]["data"]["code"] == "unauthorized"
            await ws.close()
            # Rotation ends the login and its capability with it.
            web_remote.rotate_access_token(daemon.paths)
            async with http.get(f"{base}/web/session") as answer:
                assert answer.status == 401
        # Another login's capability does not open this login's socket.
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as one, \
                aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as two:
            first = await _link_login(one, base, daemon.paths.home)
            await _link_login(two, base, daemon.paths.home)
            ws, frame = await _ws_hello(two, f"{base}/ws", first, origin=base)
            assert frame["error"]["data"]["code"] == "unauthorized"
            await ws.close()
    finally:
        await daemon.stop()


async def test_a_remote_login_is_never_a_browser_host(tmp_path: Path, dist: Path) -> None:
    """M3: a remote login declaring a legacy browser stays a remote login."""
    daemon = await _daemon(tmp_path)
    try:
        base = f"http://127.0.0.1:{daemon.remote_status['port']}"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
            capability = await _link_login(http, base, daemon.paths.home)
            ws, frame = await _ws_hello(http, f"{base}/ws", capability, origin=base,
                                        clientKind="browser", clientVersion="0.2.0",
                                        clientId="the-browsers-id")
            assert "result" in frame
            remote = [c for c in daemon.core.hub.clients() if getattr(c, "remote_login", False)]
            assert len(remote) == 1
            conn = remote[0]
            assert connection_kind(conn) == "remote_login"
            assert conn.client_kind == "web" and conn.client_id == "remote:the-browsers-id"
            await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "tool.register",
                                "params": {"tools": []}})
            refused = await ws.receive_json(timeout=10)
            assert refused["error"]["data"]["code"] == "jarvis_person_only", refused
            await ws.close()
    finally:
        await daemon.stop()


async def test_no_log_line_carries_a_token(tmp_path: Path, dist: Path,
                                           caplog: pytest.LogCaptureFixture) -> None:
    """M4: the access link's ``?t=`` and the loopback page's ``?token=`` stay out of logs."""
    daemon = await _daemon(tmp_path)
    try:
        base = f"http://127.0.0.1:{daemon.remote_status['port']}"
        link = web_remote.access_token(daemon.paths, create=False)
        with caplog.at_level(logging.DEBUG):
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
                async with http.get(f"{base}/web/?t={link}", allow_redirects=False) as page:
                    assert page.status == 302
                async with http.get(f"{base}/web/?t=wrong-token-value") as page:
                    assert page.status in (401, 429)
                async with http.get(f"http://127.0.0.1:{daemon.port}/web/?token={daemon.token}"):
                    pass
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert link not in text and daemon.token not in text
        assert "wrong-token-value" not in text
    finally:
        await daemon.stop()
