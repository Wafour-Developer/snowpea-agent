"""M37 T1: terminal.* over the wire, and terminals surviving a daemon restart."""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import aiohttp
import pytest
from _support import connect, env_vars, make_daemon

from snowpea_core.server import protocol

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY")


async def _until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _data(client) -> bytes:
    return b"".join(base64.b64decode(p["data"]) for p in client.of_method("terminal.data"))


def test_capabilities_advertised() -> None:
    assert {"terminalSessions", "terminalStatus"} <= set(protocol.CAPABILITIES)
    assert not {"terminalSessions", "terminalStatus"} & set(protocol.PENDING_CAPABILITIES)


async def test_terminal_over_rpc_and_across_a_daemon_restart(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    home = tmp_path / "home"
    with env_vars(SHELL="/bin/sh"):
        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon)
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            info = created["terminal"]
            tid = info["terminalId"]
            assert info["alive"] and info["profile"] == "shell"
            assert (await client.wait_notification("terminal.created"))["terminal"][
                "terminalId"] == tid
            attached = await client.ok("terminal.attach", {"terminalId": tid, "cols": 90,
                                                           "rows": 20})
            assert (attached["cols"], attached["rows"]) == (90, 20)
            line = base64.b64encode(b"echo hi-$((40+2))\r").decode()
            await client.ok("terminal.write", {"terminalId": tid, "data": line})
            await _until(lambda: b"hi-42" in _data(client))
            data = client.of_method("terminal.data")
            await client.ok("terminal.ack", {"terminalId": tid, "bytes": data[-1]["seq"]})
            read = await client.ok("terminal.read", {"terminalId": tid, "lines": 20})
            assert "hi-42" in read["text"] and read["alive"]
            listed = await client.ok("terminal.list", {})
            assert [t["terminalId"] for t in listed["terminals"]] == [tid]
            bad = await client.call("terminal.report", {"terminalId": tid, "token": "nope",
                                                        "event": "Stop", "state": "done"})
            assert bad["error"]["data"]["code"] == "unauthorized"
            await client.stop()
        finally:
            await daemon.stop()

        # A new daemon on the same home finds the terminal still running.
        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon)
            listed = await client.ok("terminal.list", {})
            assert [(t["terminalId"], t["alive"]) for t in listed["terminals"]] == [(tid, True)]
            read = await client.ok("terminal.read", {"terminalId": tid})
            assert "hi-42" in read["text"]
            await client.ok("terminal.send", {"terminalId": tid, "text": "echo again-$((1+1))"})
            await _until(lambda: "again-2" in daemon.core.terminals.read(tid)["text"])
            await client.ok("terminal.close", {"terminalId": tid})
            assert (await client.ok("terminal.list", {}))["terminals"] == []
            closed = await client.wait_notification("terminal.closed")
            assert closed == {"terminalId": tid}
            await client.stop()
        finally:
            if daemon.core is not None and daemon.core.terminals is not None:
                await daemon.core.terminals.close_all()
            await daemon.stop()


async def test_profiles_and_unknown_terminal(tmp_path: Path, http: aiohttp.ClientSession) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        client = await connect(http, daemon)
        profiles = await client.ok("terminal.profiles", {})
        assert "shell" in [p["id"] for p in profiles["profiles"]]
        missing = await client.call("terminal.read", {"terminalId": "t00000000"})
        assert missing["error"]["data"]["code"] == "not_found"
        await client.stop()
    finally:
        await daemon.stop()


async def test_report_socket_runs_with_the_daemon_and_reaches_the_terminal(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    import json

    home = tmp_path / "home"
    with env_vars(SHELL="/bin/sh"):
        daemon = await make_daemon(home)
        try:
            core = daemon.core
            assert core.terminal_report_server is not None
            address = core.terminal_report_address
            assert address and Path(address).exists()
            client = await connect(http, daemon)
            created = await client.ok("terminal.create", {"profile": "shell", "noProject": True})
            tid = created["terminal"]["terminalId"]
            term = core.terminals.get(tid)
            line = base64.b64encode(b"echo R=$SNOWPEA_TERMINAL_REPORT\r").decode()
            await client.ok("terminal.write", {"terminalId": tid, "data": line})
            await _until(lambda: f"R={address}" in core.terminals.read(tid)["text"])

            async def report(token: str) -> bytes:
                reader, writer = await asyncio.open_unix_connection(address)
                writer.write(json.dumps({
                    "terminalId": tid, "token": token, "cli": "claude", "event": "Notification",
                    "state": "blocked", "kind": "permission", "sessionId": "s-1",
                }).encode() + b"\n")
                answer = await reader.readline()
                writer.close()
                return answer

            assert b'"ok":false' in await report("wrong-token")
            assert term.status.state == "idle"
            assert b'"ok":true' in await report(term.report_token)
            assert (term.status.state, term.status.status.kind, term.status.status.source) == (
                "blocked", "permission", "hook")
            assert term.session_id == "s-1"
            await client.ok("terminal.close", {"terminalId": tid})
            await client.stop()
        finally:
            if daemon.core is not None and daemon.core.terminals is not None:
                await daemon.core.terminals.close_all()
            await daemon.stop()
        assert not Path(address).exists()
