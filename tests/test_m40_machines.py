"""M40 remote machines: registry, proxy allowlist, gates and direct connect."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import stat
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon
from test_cli_remote_up import FAKE_KEYGEN, FAKE_KEYSCAN, FAKE_SSH, KEY_B64

from snowpea_core import __version__
from snowpea_core.cli import remote
from snowpea_core.cli.daemon_client import DaemonInfo, read_daemon_json
from snowpea_core.cli.remote_up import fingerprint
from snowpea_core.machines.manager import MachineManager
from snowpea_core.machines.proxy import PROXY_METHODS, refuse_unknown_proxy, strip_machine
from snowpea_core.machines.registry import MachineRegistry
from snowpea_core.server import errors
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.errors import RpcError
from snowpea_core.server.machine_handlers import proxy_call
from snowpea_core.server.protocol import PROTOCOL_VERSION
from snowpea_core.server.rpc import RpcConnection

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh and ssh")


def test_registry_modes_0600_0700(tmp_path: Path) -> None:
    reg = MachineRegistry(tmp_path)
    reg.add(
        "me@box",
        label="box",
        key_path=None,
        pasted_key="-----BEGIN OPENSSH-----\n",
        remote_cwd=None,
        auto_connect=False,
    )
    reg.save()
    assert stat.S_IMODE(reg.path.stat().st_mode) == 0o600
    folder = tmp_path / "machines"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    ident = next(iter(reg.records))
    key = folder / ident / "id_key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_pasted_key_never_in_registry_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRET\n-----END"
    caplog.set_level(logging.DEBUG)
    reg = MachineRegistry(tmp_path)
    reg.add(
        "me@box",
        label="box",
        key_path=None,
        pasted_key=secret,
        remote_cwd=None,
        auto_connect=False,
    )
    assert secret not in reg.path.read_text(encoding="utf-8")
    assert secret not in caplog.text


def test_proxy_allowlist_refuses_unknown() -> None:
    with pytest.raises(RpcError) as caught:
        refuse_unknown_proxy("session.delete")
    assert caught.value.code == errors.NOT_IMPLEMENTED


def test_strip_machine_leaves_old_clients_untouched() -> None:
    assert strip_machine({}) == (None, {})
    assert strip_machine({"workdir": "/x"})[0] is None


@pytest.mark.asyncio
async def test_remote_login_needs_web_remote_machines(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        daemon.core.machines = MachineManager(daemon.core)
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            await client.ok("machine.add", {"target": "me@box", "label": "box", "pastedKey": "x"})
            mid = (await client.ok("machine.list"))["machines"][0]["id"]
            remote = RpcConnection(sender=lambda *_a, **_k: None)
            remote.authenticated = True
            remote.client_kind = "web"
            remote.remote_login = True
            with pytest.raises(RpcError) as caught:
                await proxy_call(remote, "workspace.list", mid, {}, daemon.core)
            assert caught.value.code == errors.UNAUTHORIZED
            await client.stop()
    finally:
        await daemon.stop()


@pytest_asyncio.fixture
async def daemon_pair(tmp_path: Path) -> AsyncIterator[tuple[Daemon, Daemon]]:
    local = await make_daemon(tmp_path / "local")
    remote = await make_daemon(tmp_path / "remote")
    try:
        yield local, remote
    finally:
        await local.stop()
        await remote.stop()


@pytest.mark.asyncio
async def test_direct_connect_and_proxy_workspace_list(daemon_pair: tuple[Daemon, Daemon]) -> None:
    local, remote = daemon_pair
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    async with aiohttp.ClientSession() as http:
        client = await connect(http, local)
        add = await client.ok(
            "machine.add",
            {"target": "me@box", "label": "box", "pastedKey": "fake-key-material"},
        )
        mid = add["id"]
        local.core.machines.connect(mid)
        for _ in range(60):
            row = next(m for m in local.core.machines.rows() if m.id == mid)
            if row.status == "connected":
                break
            await asyncio.sleep(0.05)
        assert row.status == "connected"
        await client.ok("workspace.list", {"machine": mid})
        assert "session.prompt" in PROXY_METHODS
        await client.stop()


def test_hello_advertises_remote_machines_capability() -> None:
    from snowpea_core.server import protocol

    assert "remoteMachines" in protocol.CAPABILITIES
    for name in (
        "machine.list",
        "machine.add",
        "machine.connect",
        "machine.answer",
    ):
        assert name in protocol.METHODS


@pytest.mark.asyncio
async def test_terminal_proxy_data_carries_machine(daemon_pair: tuple[Daemon, Daemon]) -> None:
    local, remote = daemon_pair
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    async with aiohttp.ClientSession() as http:
        client = await connect(http, local)
        add = await client.ok("machine.add", {"target": "me@box", "label": "box"})
        mid = add["id"]
        local.core.machines.connect(mid)
        for _ in range(80):
            row = next(m for m in local.core.machines.rows() if m.id == mid)
            if row.status == "connected":
                break
            await asyncio.sleep(0.05)
        assert row.status == "connected"
        created = await client.ok(
            "terminal.create", {"profile": "shell", "noProject": True, "machine": mid}
        )
        term_id = created.get("terminalId") or (created.get("terminal") or {}).get("terminalId")
        assert term_id
        await client.ok(
            "terminal.attach",
            {"terminalId": term_id, "mode": "observe", "machine": mid},
        )
        remote_client = await connect(http, remote)
        write = {"terminalId": term_id, "data": "aGk=", "human": True}
        await remote_client.ok("terminal.write", write)
        for _ in range(40):
            if any(f.get("method") == "terminal.data" for f in client.notifications):
                break
            await asyncio.sleep(0.05)
        data = [f for f in client.notifications if f.get("method") == "terminal.data"]
        assert data and data[0]["params"].get("machine") == mid
        await remote_client.stop()
        await client.ok("terminal.close", {"terminalId": term_id, "machine": mid})
        local.core.machines.disconnect(mid)
        await client.stop()


@pytest.mark.asyncio
async def test_host_key_ask_blocks_until_answered(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        from snowpea_core.machines.manager import LiveConnection, _Asks

        manager = MachineManager(daemon.core)
        manager.bind_loop(asyncio.get_running_loop())
        daemon.core.machines = manager
        live = LiveConnection()
        manager._live["m1"] = live
        asks = _Asks("m1", manager._pending_asks["m1"], live, manager._announce_sync)  # noqa: SLF001

        def run_ask() -> None:
            asks.ask("hostKey", host="box", fingerprint="ab:cd", keyType="ssh-ed25519")

        thread = threading.Thread(target=run_ask)
        thread.start()
        await asyncio.sleep(0.1)
        assert live.status == "asking"
        ident, box = manager.current_ask("m1") or ("", None)
        assert ident and box and box.kind == "hostKey"
        assert manager.answer("m1", ident, True)
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_install_ask_refused_sets_error(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        from snowpea_core.machines.manager import LiveConnection, _Asks

        manager = MachineManager(daemon.core)
        manager.bind_loop(asyncio.get_running_loop())
        daemon.core.machines = manager
        live = LiveConnection()
        manager._live["m1"] = live
        asks = _Asks("m1", manager._pending_asks["m1"], live, manager._announce_sync)  # noqa: SLF001

        def run_ask() -> None:
            assert asks.ask("install", question="install?", version="1.0") is False

        threading.Thread(target=run_ask).start()
        await asyncio.sleep(0.05)
        row = manager.current_ask("m1")
        assert row is not None
        ident = row[0]
        manager.answer("m1", ident, False)
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_browser_cannot_proxy_terminal_create(
    daemon_pair: tuple[Daemon, Daemon],
) -> None:
    local, remote = daemon_pair
    info = DaemonInfo(port=remote.port, pid=0, token=remote.token, protocolVersion=PROTOCOL_VERSION)
    local.core.machines = MachineManager(local.core, direct=lambda _id: info)
    local.core.machines.bind_loop(asyncio.get_running_loop())
    async with aiohttp.ClientSession() as http:
        client = await connect(http, local)
        add = await client.ok("machine.add", {"target": "me@box", "label": "box"})
        mid = add["id"]
        local.core.machines.connect(mid)
        for _ in range(60):
            if next(m for m in local.core.machines.rows() if m.id == mid).status == "connected":
                break
            await asyncio.sleep(0.05)
        browser = RpcConnection(sender=lambda *_a, **_k: None)
        browser.authenticated = True
        browser.client_kind = "browser"
        with pytest.raises(RpcError) as caught:
            params = {"profile": "shell", "noProject": True}
            await proxy_call(browser, "terminal.create", mid, params, local.core)
        assert caught.value.code == errors.UNAUTHORIZED
        await client.stop()


@pytest.mark.asyncio
async def test_reconnect_after_direct_drop(daemon_pair: tuple[Daemon, Daemon]) -> None:
    local, remote = daemon_pair
    start = DaemonInfo(port=remote.port, pid=0, token=remote.token)
    state: dict[str, DaemonInfo | None] = {"info": start}

    def advert(_id: str) -> DaemonInfo | None:
        return state["info"]

    manager = MachineManager(local.core, direct=advert)
    manager.bind_loop(asyncio.get_running_loop())
    local.core.machines = manager
    mid = manager.registry.add(
        "me@box",
        label="box",
        key_path=None,
        pasted_key=None,
        remote_cwd=None,
        auto_connect=False,
    ).id
    manager.connect(mid)
    for _ in range(60):
        if manager.rows()[0].status == "connected":
            break
        await asyncio.sleep(0.05)
    state["info"] = None
    live = manager._live[mid]  # noqa: SLF001
    live.status = "connecting"
    state["info"] = DaemonInfo(port=remote.port, pid=0, token=remote.token)
    manager._connect_worker(mid)
    for _ in range(60):
        if manager.rows()[0].status == "connected":
            break
        await asyncio.sleep(0.05)
    assert manager.rows()[0].status == "connected"


# ---------------------------------------------------------------------------
# fake ssh end-to-end (MachineManager + a throwaway remote daemon)
# ---------------------------------------------------------------------------


def _kill_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)


def _stop_started_processes(tmp: Path, remote_home: Path) -> None:
    tunnel = tmp / "tunnel.pid"
    if tunnel.is_file():
        _kill_pid(int(tunnel.read_text().strip()), signal.SIGKILL)
    sp_home = remote_home / ".snowpea"
    info = read_daemon_json(sp_home)
    if info and info.pid:
        _kill_pid(info.pid, signal.SIGTERM)


_FAKE_REMOTE_ADVERT_BLOCK = """\
if "-L" not in argv and "-G" not in argv and "-O" not in argv and os.environ.get(
    "FAKE_REMOTE_ADVERT"
):
    import json
    advert = json.loads(os.environ["FAKE_REMOTE_ADVERT"])
    cwd = os.environ.get("FAKE_REMOTE_CWD", "/home/me")
    print(f"SNOWPEA_ADVERT {json.dumps(advert)}\\nSNOWPEA_CWD {cwd}\\n", end="")
    sys.exit(0)
"""


def _fake_ssh_body(
    *,
    minimal_remote_path: bool = False,
    loopback_advert: bool = False,
) -> str:
    body = FAKE_SSH
    if loopback_advert:
        advert_prefix = _FAKE_REMOTE_ADVERT_BLOCK + "\n"
        home_env = 'env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"])'
        body = body.replace(home_env, advert_prefix + home_env)
    if minimal_remote_path:
        path_line = (
            'env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"], '
            "PATH=os.path.join(os.environ['FAKE_REMOTE_HOME'], '.local', 'bin') "
            "+ os.pathsep + '/usr/bin:/bin')"
        )
        body = body.replace(
            'env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"])',
            path_line,
        )
    return body


def _write_fake_ssh_tools(
    bin_dir: Path,
    *,
    minimal_remote_path: bool = False,
    loopback_advert: bool = False,
) -> None:
    ssh_body = _fake_ssh_body(
        minimal_remote_path=minimal_remote_path,
        loopback_advert=loopback_advert,
    )
    for name, body in (
        ("ssh", ssh_body),
        ("ssh-keyscan", FAKE_KEYSCAN),
        ("ssh-keygen", FAKE_KEYGEN),
    ):
        script = bin_dir / name
        script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        script.chmod(0o755)


def _configure_fake_ssh_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_home: Path,
    *,
    snowpea_on_remote: bool,
    minimal_remote_path: bool = False,
    loopback_advert: bool = False,
) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_ssh_tools(
        bin_dir,
        minimal_remote_path=minimal_remote_path,
        loopback_advert=loopback_advert,
    )
    (remote_home / ".local" / "bin").mkdir(parents=True)
    snowpea = Path(sys.executable).parent / "snowpea"
    if snowpea_on_remote:
        if not snowpea.exists():
            pytest.skip("the snowpea console script is not installed here")
        (remote_home / ".local" / "bin" / "snowpea").symlink_to(snowpea)
    tunnel_pid = tmp_path / "tunnel.pid"
    local_home = tmp_path / "local-home"
    local_home.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(remote_home))
    monkeypatch.setenv("FAKE_KNOWN_HOSTS", str(tmp_path / "legacy_known_hosts"))
    monkeypatch.setenv("FAKE_TUNNEL_PID", str(tunnel_pid))
    monkeypatch.setenv("PYTHONPATH", str(Path(remote.__file__).parents[2]))
    monkeypatch.setenv("SNOWPEA_UPDATE_CHECK", "0")
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.delenv("SNOWPEA_HOME", raising=False)
    return {
        "bin_dir": bin_dir,
        "remote_home": remote_home,
        "tmp": tmp_path,
        "tunnel_pid": tunnel_pid,
        "snowpea": snowpea,
    }


def _set_loopback_advert(
    monkeypatch: pytest.MonkeyPatch,
    remote: Daemon,
    *,
    cwd: str = "/home/me",
) -> None:
    advert = {
        "port": remote.port,
        "token": remote.token,
        "version": __version__,
        "protocolVersion": PROTOCOL_VERSION,
    }
    monkeypatch.setenv("FAKE_REMOTE_ADVERT", json.dumps(advert))
    monkeypatch.setenv("FAKE_REMOTE_CWD", cwd)


async def _wait_machine(
    client: Any,
    machine_id: str,
    *,
    status: str | None = None,
    ask_kind: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        listed = await client.ok("machine.list")
        for row in listed["machines"]:
            if row["id"] != machine_id:
                continue
            if status is not None and row.get("status") == status:
                return row
            ask = row.get("ask") or {}
            if ask_kind is not None and ask.get("kind") == ask_kind:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"machine {machine_id} did not reach status={status!r} ask={ask_kind!r}")


async def _accept_host_key(client: Any, machine_id: str) -> None:
    row = await _wait_machine(client, machine_id, ask_kind="hostKey", timeout=30)
    ask = row["ask"]
    assert ask and ask["kind"] == "hostKey"
    assert ask["fingerprint"] == fingerprint(KEY_B64)
    await client.ok(
        "machine.answer",
        {"id": machine_id, "askId": ask["id"], "answer": True},
    )
    await _wait_machine(client, machine_id, status="connected", timeout=90)


@pytest_asyncio.fixture
async def fake_ssh_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    remote_daemon = await make_daemon(remote_home)
    bundle = _configure_fake_ssh_env(
        tmp_path,
        monkeypatch,
        remote_home,
        snowpea_on_remote=False,
        loopback_advert=True,
    )
    _set_loopback_advert(monkeypatch, remote_daemon)
    daemon = await make_daemon(tmp_path / "local-daemon")
    manager = MachineManager(daemon.core, ssh=str(bundle["bin_dir"] / "ssh"))
    manager.bind_loop(asyncio.get_running_loop())
    daemon.core.machines = manager
    try:
        yield {
            **bundle,
            "daemon": daemon,
            "manager": manager,
            "remote_daemon": remote_daemon,
            "monkeypatch": monkeypatch,
        }
    finally:
        for machine_id in list(manager.registry.records):
            manager.disconnect(machine_id)
        _stop_started_processes(bundle["tmp"], bundle["remote_home"])
        await remote_daemon.stop()
        await daemon.stop()


@posix_only
@pytest.mark.asyncio
async def test_fake_ssh_refused_host_key_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    remote_daemon = await make_daemon(remote_home)
    bundle = _configure_fake_ssh_env(
        tmp_path,
        monkeypatch,
        remote_home,
        snowpea_on_remote=False,
        loopback_advert=True,
    )
    _set_loopback_advert(monkeypatch, remote_daemon)
    daemon = await make_daemon(tmp_path / "local-daemon")
    manager = MachineManager(daemon.core, ssh=str(bundle["bin_dir"] / "ssh"))
    manager.bind_loop(asyncio.get_running_loop())
    daemon.core.machines = manager
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
            await client.ok("machine.connect", {"id": mid})
            row = await _wait_machine(client, mid, ask_kind="hostKey", timeout=30)
            ask = row["ask"]
            await client.ok(
                "machine.answer",
                {"id": mid, "askId": ask["id"], "answer": False},
            )
            err_row = await _wait_machine(client, mid, status="error", timeout=30)
            assert err_row.get("error") and "not accepted" in err_row["error"].lower()
            await client.stop()
    finally:
        _stop_started_processes(bundle["tmp"], bundle["remote_home"])
        await remote_daemon.stop()
        await daemon.stop()


@posix_only
@pytest.mark.asyncio
async def test_fake_ssh_install_declined_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    bundle = _configure_fake_ssh_env(
        tmp_path,
        monkeypatch,
        remote_home,
        snowpea_on_remote=False,
        minimal_remote_path=True,
    )
    daemon = await make_daemon(tmp_path / "local-daemon")
    manager = MachineManager(daemon.core, ssh=str(bundle["bin_dir"] / "ssh"))
    manager.bind_loop(asyncio.get_running_loop())
    daemon.core.machines = manager
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
            await client.ok("machine.connect", {"id": mid})
            row = await _wait_machine(client, mid, ask_kind="hostKey", timeout=30)
            await client.ok(
                "machine.answer",
                {"id": mid, "askId": row["ask"]["id"], "answer": True},
            )
            install = await _wait_machine(client, mid, ask_kind="install", timeout=60)
            ask = install["ask"]
            assert ask and ask["kind"] == "install"
            await client.ok(
                "machine.answer",
                {"id": mid, "askId": ask["id"], "answer": False},
            )
            failed = await _wait_machine(client, mid, status="error", timeout=30)
            assert failed.get("error") and "not installed" in failed["error"].lower()
            await client.stop()
    finally:
        _stop_started_processes(bundle["tmp"], bundle["remote_home"])
        await daemon.stop()


@posix_only
@pytest.mark.asyncio
async def test_fake_ssh_connect_terminal_relay(fake_ssh_stack: dict[str, Any]) -> None:
    daemon = fake_ssh_stack["daemon"]
    manager = fake_ssh_stack["manager"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        created = await client.ok(
            "terminal.create", {"profile": "shell", "noProject": True, "machine": mid}
        )
        term_id = created.get("terminalId") or (created.get("terminal") or {}).get("terminalId")
        assert term_id
        await client.ok(
            "terminal.attach",
            {"terminalId": term_id, "mode": "observe", "machine": mid},
        )
        live = manager._live[mid]  # noqa: SLF001
        tunnel = type("_Tunnel", (), {"port": live.port, "token": live.token})()
        tunnel_client = await connect(http, tunnel)  # type: ignore[arg-type]
        await tunnel_client.ok(
            "terminal.write",
            {"terminalId": term_id, "data": "aGk=", "human": True},
        )
        for _ in range(80):
            if any(f.get("method") == "terminal.data" for f in client.notifications):
                break
            await asyncio.sleep(0.05)
        data = [f for f in client.notifications if f.get("method") == "terminal.data"]
        assert data and data[0]["params"].get("machine") == mid
        await tunnel_client.stop()
        await client.ok("terminal.close", {"terminalId": term_id, "machine": mid})
        await client.ok("machine.disconnect", {"id": mid})
        off = await _wait_machine(client, mid, status="off", timeout=15)
        assert off["status"] == "off"
        await client.stop()


@posix_only
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_fake_ssh_reconnect_after_remote_daemon_restart(
    fake_ssh_stack: dict[str, Any],
) -> None:
    daemon = fake_ssh_stack["daemon"]
    remote_home: Path = fake_ssh_stack["remote_home"]
    monkeypatch: pytest.MonkeyPatch = fake_ssh_stack["monkeypatch"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        assert (await _wait_machine(client, mid, status="connected"))["status"] == "connected"
        live = fake_ssh_stack["manager"]._live[mid]  # noqa: SLF001
        old_tunnel_port = live.port
        await fake_ssh_stack["remote_daemon"].stop()
        token_path = remote_home / ".snowpea" / "token"
        if token_path.is_file():
            token_path.unlink()
        fake_ssh_stack["remote_daemon"] = await make_daemon(remote_home)
        _set_loopback_advert(monkeypatch, fake_ssh_stack["remote_daemon"])
        if fake_ssh_stack["tunnel_pid"].is_file():
            _kill_pid(int(fake_ssh_stack["tunnel_pid"].read_text().strip()), signal.SIGKILL)
        await _wait_machine(client, mid, status="connecting", timeout=25)
        connected = await _wait_machine(client, mid, status="connected", timeout=110)
        assert connected["status"] == "connected"
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            live = fake_ssh_stack["manager"]._live[mid]  # noqa: SLF001
            if live.port != old_tunnel_port:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("ssh tunnel did not reopen after the remote daemon restarted")
        live.client = None
        await client.ok("workspace.list", {"machine": mid})
        await client.ok("machine.disconnect", {"id": mid})
        await client.stop()