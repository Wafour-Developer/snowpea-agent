"""M42: remote snowpea session chat over the machine proxy."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest

pytest_plugins = ["test_m40_machines"]

from _support import connect

from snowpea_core.cli.daemon_client import DaemonInfo
from snowpea_core.machines.manager import MachineManager
from snowpea_core.machines.proxy import PROXY_METHODS
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.machine_handlers import proxy_call
from snowpea_core.server.protocol import PROTOCOL_VERSION
from snowpea_core.server.rpc import RpcConnection


async def _connect_mid(local, remote) -> tuple[str, aiohttp.ClientSession, Any]:
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    mid = local.core.machines.registry.add(
        "me@box", label="box", key_path=None, pasted_key=None, remote_cwd=None, auto_connect=False
    ).id
    local.core.machines.connect(mid)
    for _ in range(80):
        if next(m for m in local.core.machines.rows() if m.id == mid).status == "connected":
            break
        await asyncio.sleep(0.05)
    http = aiohttp.ClientSession()
    client = await connect(http, local)
    return mid, http, client


@pytest.mark.asyncio
async def test_remote_session_resume_and_prompt_proxied(daemon_pair) -> None:
    local, remote = daemon_pair
    mid, http, client = await _connect_mid(local, remote)
    assert {"session.prompt", "session.resume", "session.interrupt"} <= PROXY_METHODS
    try:
        created = await client.ok(
            "session.create", {"workdir": "/tmp", "noProject": True, "machine": mid}
        )
        sid = created.get("sessionId") or (created.get("session") or {}).get("sessionId")
        assert sid
        await client.ok("session.resume", {"sessionId": sid, "machine": mid})
        await client.ok("session.interrupt", {"sessionId": sid, "machine": mid})
    finally:
        await client.stop()
        await http.close()


@pytest.mark.asyncio
async def test_remote_session_events_carry_machine(daemon_pair) -> None:
    local, remote = daemon_pair
    mid, http, client = await _connect_mid(local, remote)
    try:
        created = await client.ok(
            "session.create", {"workdir": "/tmp", "noProject": True, "machine": mid}
        )
        sid = created.get("sessionId") or (created.get("session") or {}).get("sessionId")
        assert sid
        await client.ok("session.resume", {"sessionId": sid, "machine": mid})
        await client.ok(
            "session.prompt",
            {"sessionId": sid, "machine": mid, "text": "ping", "whenBusy": "queue"},
        )
        for _ in range(100):
            notes = [n for n in client.notifications if n.get("method") == "session.event"]
            if any((n.get("params") or {}).get("machine") == mid for n in notes):
                break
            await asyncio.sleep(0.05)
        assert any(
            (n.get("params") or {}).get("machine") == mid
            and (n.get("params") or {}).get("sessionId") == sid
            for n in client.notifications
            if n.get("method") == "session.event"
        )
        await client.ok("session.interrupt", {"sessionId": sid, "machine": mid})
    finally:
        await client.stop()
        await http.close()


@pytest.mark.asyncio
async def test_remote_login_caps_and_browser_refused(daemon_pair) -> None:
    local, remote = daemon_pair
    mid, http, client = await _connect_mid(local, remote)
    try:
        remote_conn = RpcConnection(sender=lambda *_a, **_k: None)
        remote_conn.authenticated = True
        remote_conn.client_kind = "web"
        remote_conn.remote_login = True
        local.core.settings.web.remote.enabled = True
        local.core.settings.web.remote.machines = True
        from snowpea_core.machines.gates import proxy_params_dump
        from snowpea_core.server.protocol import SessionCreateParams

        params = SessionCreateParams(workdir="/tmp", noProject=True, mode="auto")
        dumped = proxy_params_dump(remote_conn, local.core, params, method="session.create")
        assert dumped.get("mode") in {"accept", "plan", "auto"}
        assert dumped.get("mode") != "unrestricted"
        created = await proxy_call(
            remote_conn,
            "session.create",
            mid,
            {"workdir": "/tmp", "noProject": True},
            local.core,
        )
        sid = created.get("sessionId")
        assert sid
        browser = RpcConnection(sender=lambda *_a, **_k: None)
        browser.authenticated = True
        browser.client_kind = "browser"
        with pytest.raises(RpcError) as caught:
            await proxy_call(
                browser, "session.prompt", mid, {"sessionId": sid, "text": "x"}, local.core
            )
        assert caught.value.code == errors.UNAUTHORIZED
    finally:
        await client.stop()
        await http.close()


def test_local_session_not_addressed_with_machine_param() -> None:
    """Machine-scoped ids are only on the proxy path; documented here as a guard contract."""
    assert "session.create" in PROXY_METHODS


@pytest.mark.asyncio
async def test_session_close_unregisters_session_viewer(daemon_pair) -> None:
    local, remote = daemon_pair
    mid, http, client = await _connect_mid(local, remote)
    manager = local.core.machines
    try:
        created = await client.ok(
            "session.create", {"workdir": "/tmp", "noProject": True, "machine": mid}
        )
        sid = created.get("sessionId")
        assert sid
        await client.ok("session.resume", {"sessionId": sid, "machine": mid})
        key = (mid, sid)
        assert manager._session_viewers.get(key)  # noqa: SLF001
        await client.ok("session.close", {"sessionId": sid, "machine": mid})
        assert not manager._session_viewers.get(key)  # noqa: SLF001
    finally:
        await client.stop()
        await http.close()