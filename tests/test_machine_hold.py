"""Protocol 1.40.0: ``system.hello`` machine identity, ``session.list`` machine, ``holdSec``."""

from __future__ import annotations

import asyncio
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect, make_daemon

from snowpea_core import machine as machine_mod
from snowpea_core.server.lifecycle import Lifecycle
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# -- identity --------------------------------------------------------------------------


def test_the_machine_id_is_made_once_and_kept_private(tmp_path: Path) -> None:
    first = machine_mod.machine_id(tmp_path)
    assert UUID.match(first)
    assert machine_mod.machine_id(tmp_path) == first  # stable
    path = tmp_path / "machine-id"
    assert path.read_text().strip() == first
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # another state home is another machine to a client that pins ids
    other = tmp_path / "other"
    other.mkdir()
    assert machine_mod.machine_id(other) != first


def test_a_damaged_machine_id_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "machine-id").write_text("not a uuid")
    fresh = machine_mod.machine_id(tmp_path)
    assert UUID.match(fresh)
    assert machine_mod.machine_id(tmp_path) == fresh


def test_the_id_is_random_not_a_hardware_identifier(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert machine_mod.machine_id(a) != machine_mod.machine_id(b)
    machine_id_file = Path("/etc/machine-id")
    if machine_id_file.exists():
        assert machine_id_file.read_text().strip().replace("-", "") not in machine_mod.machine_id(
            a
        ).replace("-", "")


def test_os_name_and_hostname(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = machine_mod.machine_info(tmp_path)
    assert info.os in ("linux", "darwin", "windows")
    assert info.hostname
    for platform, name in (("linux", "linux"), ("darwin", "darwin"), ("win32", "windows")):
        monkeypatch.setattr(sys, "platform", platform)
        assert machine_mod.os_name() == name


def test_a_container_is_recognised_by_the_sandbox_marker_or_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SNOWPEA_SANDBOX", raising=False)
    monkeypatch.setenv("SNOWPEA_SANDBOX", "1")
    assert machine_mod.in_container() is True
    monkeypatch.delenv("SNOWPEA_SANDBOX")
    seen: dict[str, str] = {}
    real_exists, real_read = Path.exists, Path.read_text

    def fake_exists(self: Path, *a: Any, **k: Any) -> bool:
        return (
            True
            if str(self) == "/.dockerenv" and "dockerenv" in seen
            else real_exists(self, *a, **k)
        )

    def fake_read(self: Path, *a: Any, **k: Any) -> str:
        if str(self) == "/proc/1/cgroup" and "cgroup" in seen:
            return seen["cgroup"]
        return real_read(self, *a, **k)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read)
    seen["dockerenv"] = "1"
    assert machine_mod.in_container() is True
    del seen["dockerenv"]
    seen["cgroup"] = "12:pids:/docker/abcdef\n"
    assert machine_mod.in_container() is True
    seen["cgroup"] = "0::/user.slice/session-1.scope\n"
    monkeypatch.setattr(Path, "exists", lambda self, *a, **k: False)
    assert machine_mod.in_container() is False


# -- hello / session.list -----------------------------------------------------------------


async def test_hello_reports_the_machine_and_the_capabilities(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "system.hello",
                "params": {
                    "token": daemon.token,
                    "clientVersion": "t",
                    "protocolVersion": PROTOCOL_VERSION,
                },
            }
        )
        reply = (await ws.receive_json())["result"]
        await ws.close()
    finally:
        await daemon.stop()
    assert reply["protocolVersion"] == "1.41.0"
    machine = reply["machine"]
    assert set(machine) == {"id", "hostname", "os", "container"}
    assert machine["id"] == machine_mod.machine_id(tmp_path / "home")
    assert isinstance(machine["container"], bool)
    assert {"fs", "machineIdentity", "holdSec"} <= set(reply["capabilities"])
    assert {"fs", "machineIdentity", "holdSec"} <= set(CAPABILITIES)


async def test_session_list_rows_name_the_machine(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    daemon = await make_daemon(tmp_path / "home", {"memory": {"enabled": False}})
    client = await connect(http, daemon)
    try:
        work = tmp_path / "w"
        work.mkdir()
        created = await client.ok("session.create", {"workdir": str(work)})
        await client.ok("session.close", {"sessionId": created["sessionId"]})
        again = await client.ok("session.create", {"workdir": str(work)})
        rows = (await client.ok("session.list", {"includeClosed": True}))["sessions"]
        ids = {row["sessionId"] for row in rows}
        assert {created["sessionId"], again["sessionId"]} <= ids  # a live and a stored row
        expected = machine_mod.machine_info(tmp_path / "home").model_dump()
        for row in rows:
            assert row["machine"] == expected
    finally:
        await client.stop()
        await daemon.stop()


# -- holdSec ----------------------------------------------------------------------------


async def test_a_hold_shortens_the_idle_timeout_and_ignores_loaded_idle_sessions() -> None:
    life = Lifecycle(idle_timeout_sec=1800)
    running = {"n": 0}
    life.running_turns = lambda: running["n"]
    life.set_counter("sessions", 3)  # three loaded sessions, none working
    assert life.seconds_until_exit() is None  # ordinary: sessions keep the daemon up
    life.set_hold(300)
    remaining = life.seconds_until_exit()
    assert remaining is not None and 295 < remaining <= 300
    assert life.status()["willExit"] is True
    running["n"] = 1  # a turn runs: it stays up
    assert life.seconds_until_exit() is None
    assert life.reasons() == ["1 session"]
    running["n"] = 0
    life.set_counter("jobs", 1)  # a job, binding or agent still holds it
    assert life.seconds_until_exit() is None
    life.set_counter("jobs", 0)
    life.set_hold(None)
    assert life.seconds_until_exit() is None  # back to the ordinary rule


def test_the_hold_is_clamped() -> None:
    life = Lifecycle()
    life.set_hold(1)
    assert life.hold_sec == 30
    life.set_hold(10**9)
    assert life.hold_sec == 3600


async def test_a_held_daemon_exits_when_nothing_but_loaded_sessions_is_left() -> None:
    fired = asyncio.Event()

    async def on_idle() -> None:
        fired.set()

    life = Lifecycle(idle_timeout_sec=1800, on_idle=on_idle, tick=0.01)
    life.running_turns = lambda: 0
    life.set_counter("sessions", 2)
    life.start()
    try:
        await asyncio.sleep(0.1)
        assert not fired.is_set()
        life.set_hold(30)
        life._idle_since = time.monotonic() - 31  # noqa: SLF001 - "idle for 31 s", without waiting
        await asyncio.wait_for(fired.wait(), 2)
    finally:
        await life.stop()


async def test_hello_with_holdsec_sets_the_hold_and_a_tui_lifts_it(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        assert core is not None and core.lifecycle.hold_sec is None

        async def hello(**extra: Any) -> dict[str, Any]:
            ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "system.hello",
                    "params": {
                        "token": daemon.token,
                        "clientVersion": "t",
                        "protocolVersion": PROTOCOL_VERSION,
                        **extra,
                    },
                }
            )
            reply = await ws.receive_json()
            await ws.close()
            return reply

        assert "result" in await hello(holdSec=300, clientKind="connector")
        assert core.lifecycle.hold_sec == 300
        await hello(clientKind="cli")  # a one-shot CLI call does not lift it
        assert core.lifecycle.hold_sec == 300
        await hello(clientKind="tui")  # someone sits down at the machine
        assert core.lifecycle.hold_sec is None
        assert (await hello(holdSec=5))["error"]["code"] == -32602  # out of range
    finally:
        await daemon.stop()
