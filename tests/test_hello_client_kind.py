"""Security review C3(b): a connection's ``clientKind`` is fixed by its first hello.

A browser host (or the connector) that says hello again as a person's app,
or with no kind at all (which counts as a person), is refused; the same
kind again is fine, and a new connection may be anything it authenticates as.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
from _support import RpcClient, make_daemon

from snowpea_core.server.protocol import PROTOCOL_VERSION
from snowpea_core.server.transport_ws import CONNECTIONS_KEY


async def _open(http: aiohttp.ClientSession, port: int) -> RpcClient:
    ws = await http.ws_connect(f"http://127.0.0.1:{port}/ws")
    client = RpcClient(ws)
    client.start()
    return client


async def test_a_second_hello_may_not_change_the_client_kind(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        async with aiohttp.ClientSession() as http:
            client = await _open(http, daemon.port)
            hello: dict[str, Any] = {
                "token": daemon.token,
                "clientVersion": "browser-test",
                "protocolVersion": PROTOCOL_VERSION,
                "clientKind": "browser",
            }
            await client.ok("system.hello", hello)
            for kind in ("ide", "desktop", "tui", None):
                again = dict(hello)
                if kind is None:
                    again.pop("clientKind")
                else:
                    again["clientKind"] = kind
                frame = await client.call("system.hello", again)
                error = frame.get("error") or {}
                assert error, (kind, frame)
                assert "clientKind is fixed" in str(error.get("message")), error
            # The same kind again is an ordinary re-hello.
            assert (await client.ok("system.hello", hello))["protocolVersion"]
            # The connection is still the browser host it said it was.
            server_side = [conn for conn in daemon.app[CONNECTIONS_KEY]
                           if conn.client_version == "browser-test"]
            assert server_side and all(conn.client_kind == "browser" for conn in server_side)
            await client.stop()
            # A new connection authenticates as whatever it is.
            person = await _open(http, daemon.port)
            await person.ok("system.hello", {**hello, "clientKind": "ide",
                                             "clientVersion": "ide-test"})
            await person.stop()
    finally:
        await daemon.stop()


async def test_a_failed_hello_does_not_pin_anything(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        async with aiohttp.ClientSession() as http:
            client = await _open(http, daemon.port)
            hello: dict[str, Any] = {
                "token": "wrong",
                "clientVersion": "t",
                "protocolVersion": PROTOCOL_VERSION,
                "clientKind": "browser",
            }
            assert (await client.call("system.hello", hello)).get("error")
            await client.ok("system.hello", {**hello, "token": daemon.token,
                                             "clientKind": "tui"})
            await client.stop()
    finally:
        await daemon.stop()
