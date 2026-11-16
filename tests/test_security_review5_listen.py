"""Remote web access: a stale access link in a browser, and moving the listener.

Everything binds 127.0.0.1 with port 0 (or a port this test itself holds), on
tmp homes; nothing is started on the default home.
"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.config.paths import Paths
from snowpea_core.server import errors, web_remote
from snowpea_core.server.errors import RpcError
from snowpea_core.server.rpc import RpcDispatcher
from snowpea_core.server.web_remote import RemoteAuth, RemoteListener, create_remote_app


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app></div>", encoding="utf-8")
    monkeypatch.setenv("SNOWPEA_WEB_DIST", str(root))
    return root


async def _client(paths: Paths) -> tuple[TestClient, RemoteAuth]:
    auth = RemoteAuth("", 12, paths)
    auth.failure_delay = 0.0
    core = SimpleNamespace(token="daemon-token", hub=None)
    client = TestClient(TestServer(create_remote_app(RpcDispatcher(core), auth),
                                   host="127.0.0.1"))
    await client.start_server()
    return client, auth


# -- a stale or wrong access link ---------------------------------------------------


async def test_a_stale_link_in_a_browser_gets_a_page(tmp_path: Path, dist: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    old = web_remote.access_token(paths)
    web_remote.rotate_access_token(paths)
    client, _auth = await _client(paths)
    try:
        page = await client.get(f"/web/?t={old}", headers={
            "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"})
        assert page.status == 401
        assert page.headers["Content-Type"].startswith("text/html")
        text = await page.text()
        assert "no longer works" in text and old not in text
        assert "invalid_token" not in text
        korean = await client.get(f"/web/?t={old}", headers={
            "Accept": "text/html", "Accept-Language": "ko-KR,ko;q=0.9"})
        assert korean.status == 401 and "새 링크" in await korean.text()
        api = await client.get(f"/web/?t={old}", headers={"Accept": "application/json"})
        assert api.status == 401
        assert await api.json() == {"ok": False, "error": "invalid_token"}
        # Each counted: three more wrong links reach the per-address limit.
        for _ in range(2):
            await client.get(f"/web/?t={old}", headers={"Accept": "text/html"})
        locked = await client.get(f"/web/?t={old}", headers={"Accept": "text/html"})
        assert locked.status == 429
    finally:
        await client.close()


# -- moving the listener -------------------------------------------------------------


@pytest.fixture
def listener(tmp_path: Path) -> RemoteListener:
    paths = Paths.create(tmp_path / "home")
    core = SimpleNamespace(token="daemon-token", hub=None)
    return RemoteListener(RpcDispatcher(core), paths, connections=set(), sockets=set())


async def test_a_failed_move_keeps_the_old_listener(listener: RemoteListener,
                                                    dist: Path) -> None:
    await listener.start(host="127.0.0.1", port=0)
    old_port = listener.port
    assert listener.listening and old_port
    session = listener.auth.open_session() if listener.auth else None
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        with pytest.raises(OSError) as caught:
            await listener.start(host="127.0.0.1", port=busy)
        assert listener.listening and listener.port == old_port  # the old one stays on
        assert (caught.value.field, caught.value.reason) == ("port", "in_use")  # type: ignore[attr-defined]
        assert listener.auth is not None and listener.auth.session(session) is not None
        async with aiohttp.ClientSession() as http, \
                http.get(f"http://127.0.0.1:{old_port}/web/") as page:
            assert page.status == 200
        with pytest.raises(web_remote.ListenError) as bad:
            await listener.start(host="no-such-host.invalid", port=0)
        assert (bad.value.field, bad.value.reason) == ("host", "bad_address")
        assert listener.listening and listener.port == old_port
        with pytest.raises(web_remote.ListenError) as wide:
            await listener.start(host="127.0.0.1", port=70000)
        assert (wide.value.field, wide.value.reason) == ("port", "out_of_range")
        # Same address: an update of options, the sessions stay.
        await listener.start(host="127.0.0.1", port=old_port, session_hours=2)
        assert listener.port == old_port and listener.auth.session(session) is not None
        # A real move succeeds and replaces it (port 0 on the same host is "keep it").
        await listener.start(host="127.0.0.1", port=0)
        assert listener.port == old_port
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
        probe.close()
        await listener.start(host="127.0.0.1", port=free)
        assert listener.listening and listener.port == free != old_port
    finally:
        blocker.close()
        await listener.stop()


async def test_listen_errors_are_typed(tmp_path: Path, dist: Path) -> None:
    from snowpea_core.server.protocol import WebRemoteListenParams, WebRemoteSetPasswordParams
    from snowpea_core.server.web_remote_handlers import listen_handler, set_password_handler

    paths = Paths.create(tmp_path / "home")
    listener = RemoteListener(RpcDispatcher(SimpleNamespace(token="t", hub=None)), paths,
                              connections=set(), sockets=set())
    remote = SimpleNamespace(username="", sessionHours=12, trustProxy=False, allowedOrigins=[])
    core: Any = SimpleNamespace(web_remote=listener, paths=paths,
                                settings=SimpleNamespace(web=SimpleNamespace(remote=remote)))
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        with pytest.raises(RpcError) as caught:
            await listen_handler(None, WebRemoteListenParams(
                host="127.0.0.1", port=blocker.getsockname()[1]), core)
        assert caught.value.code == errors.UNAVAILABLE
        assert caught.value.data == {"field": "port", "reason": "in_use"}
        with pytest.raises(RpcError) as wide:
            await listen_handler(None, WebRemoteListenParams(host="127.0.0.1", port=70000), core)
        assert wide.value.code == errors.INVALID_PARAMS
        assert wide.value.data == {"field": "port", "reason": "out_of_range"}
        with pytest.raises(RpcError) as nameless:
            await set_password_handler(None, WebRemoteSetPasswordParams(password="x" * 12), core)
        assert nameless.value.code == errors.INVALID_PARAMS
        assert nameless.value.data == {"field": "username", "reason": "required"}
    finally:
        blocker.close()
        await listener.stop()
