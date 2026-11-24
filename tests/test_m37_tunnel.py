# ruff: noqa: E501  (real provider log lines are kept verbatim as fixtures)
"""M37 web tunnel (design §2.10): log parsing, preconditions, restart backoff, the terminal
unlock gate, origin and forwarded-header rules, the installer's checksum refusal, and an
end to end run against fake tunnel programs (small Python scripts; nothing real is
started and nothing is downloaded)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from snowpea_core.cli import tunnel_cmd
from snowpea_core.config.credentials import CredentialStore
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.jarvis import remote_grant
from snowpea_core.server import tunnel, web_remote
from snowpea_core.server.errors import RpcError
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.server.tunnel import (
    PinnedAsset,
    TunnelError,
    TunnelService,
    check_preconditions,
    install_binary,
    parse_ngrok_api,
    parse_ngrok_url,
    parse_quick_url,
    redact,
    require_terminal_unlock,
)
from snowpea_core.server.web_remote import RemoteAuth, create_remote_app

PASSWORD = "correct horse battery"

# -- fixtures: each provider's real log format ---------------------------------------

QUICK_LOG = """\
2026-11-23T10:00:00Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out.
2026-11-23T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-11-23T10:00:01Z INF +--------------------------------------------------------------------------------------------+
2026-11-23T10:00:01Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-11-23T10:00:01Z INF |  https://random-words-here-abcd.trycloudflare.com                                          |
2026-11-23T10:00:01Z INF +--------------------------------------------------------------------------------------------+
2026-11-23T10:00:01Z INF Version 2026.11.2
2026-11-23T10:00:02Z INF Registered tunnel connection connIndex=0 connection=0a1b event=0 ip=198.41.192.7 location=icn01 protocol=quic
"""
QUICK_ERROR = (
    '2026-11-23T10:00:00Z ERR failed to request quick Tunnel: Post '
    '"https://api.trycloudflare.com/tunnel": dial tcp: lookup api.trycloudflare.com: no such host'
)
NAMED_LOG = """\
2026-11-23T10:00:00Z INF Starting tunnel tunnelID=1111-2222
2026-11-23T10:00:01Z INF Initial protocol quic
2026-11-23T10:00:02Z INF Registered tunnel connection connIndex=0 connection=aa event=0 ip=198.41.200.1 location=icn01 protocol=quic
"""
NGROK_LOG = [
    '{"lvl":"info","msg":"no configuration paths supplied","t":"2026-11-23T10:00:00Z"}',
    '{"lvl":"info","msg":"client session established","obj":"tunnels.session","t":"2026-11-23T10:00:01Z"}',
    '{"addr":"http://127.0.0.1:8787","lvl":"info","msg":"started tunnel","name":"command_line",'
    '"obj":"tunnels","t":"2026-11-23T10:00:02Z","url":"https://1a2b-3c4d.ngrok-free.app"}',
]
NGROK_API = {"tunnels": [
    {"name": "other", "public_url": "https://other.ngrok.app", "proto": "https",
     "config": {"addr": "http://localhost:3000"}},
    {"name": "command_line", "public_url": "http://1a2b.ngrok-free.app", "proto": "http",
     "config": {"addr": "http://127.0.0.1:8787"}},
    {"name": "command_line (https)", "public_url": "https://1a2b-3c4d.ngrok-free.app",
     "proto": "https", "config": {"addr": "http://127.0.0.1:8787"}},
]}


def test_parse_quick_url() -> None:
    urls = [parse_quick_url(line) for line in QUICK_LOG.splitlines()]
    assert [u for u in urls if u] == ["https://random-words-here-abcd.trycloudflare.com"]
    assert parse_quick_url(QUICK_ERROR) is None  # api.trycloudflare.com is not a tunnel
    assert parse_quick_url("see https://trycloudflare.com/docs") is None
    assert parse_quick_url("|  HTTPS://Mixed-Case.TryCloudflare.com  |") == \
        "https://mixed-case.trycloudflare.com"


def test_named_tunnel_registered_line() -> None:
    assert [tunnel.is_registered(line) for line in NAMED_LOG.splitlines()] == [False, False, True]


def test_parse_ngrok_url_and_api() -> None:
    assert [parse_ngrok_url(line) for line in NGROK_LOG] == [
        None, None, "https://1a2b-3c4d.ngrok-free.app"]
    assert parse_ngrok_url("not json") is None
    assert parse_ngrok_url('{"msg":"started tunnel","url":"http://plain.example"}') is None
    assert parse_ngrok_api(NGROK_API, 8787) == "https://1a2b-3c4d.ngrok-free.app"
    assert parse_ngrok_api({"tunnels": []}, 8787) is None
    assert parse_ngrok_api("nonsense") is None


def test_redact_removes_tokens() -> None:
    assert redact("auth token tok-123 failed", ["tok-123", ""]) == "auth token *** failed"


# -- preconditions -------------------------------------------------------------------


def _listener(host: str = "0.0.0.0", port: int = 8787, listening: bool = True) -> Any:
    return SimpleNamespace(host=host, port=port, listening=listening)


def _paths(tmp_path: Path, *, password: bool = True) -> Paths:
    paths = Paths.create(tmp_path / "home")
    if password:
        web_remote.set_password(paths, PASSWORD)
    return paths


def test_preconditions(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    ok = SimpleNamespace(enabled=True)
    check_preconditions(ok, _listener(), paths)
    with pytest.raises(TunnelError) as exc:
        check_preconditions(SimpleNamespace(enabled=False), None, paths)
    assert exc.value.code == "remote_disabled"
    with pytest.raises(TunnelError) as exc:
        check_preconditions(ok, _listener(listening=False), paths)
    assert exc.value.code == "not_listening"
    with pytest.raises(TunnelError) as exc:
        check_preconditions(ok, _listener(host="192.168.1.5"), paths)
    assert exc.value.code == "bad_listen_host"
    bare = _paths(tmp_path / "other", password=False)
    with pytest.raises(TunnelError) as exc:
        check_preconditions(ok, _listener(), bare)
    assert exc.value.code == "no_password"
    assert exc.value.to_rpc().data["reason"] == "no_password"


def test_target_address() -> None:
    assert tunnel.target_address("0.0.0.0", 1) == "127.0.0.1:1"
    assert tunnel.target_address("::", 1) == "127.0.0.1:1"
    assert tunnel.target_address("127.0.0.5", 2) == "127.0.0.5:2"
    assert tunnel.target_address("::1", 3) == "[::1]:3"


def test_find_binary_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    assert tunnel.find_binary("ngrok", None, paths) is None
    home_bin = tunnel.bin_dir(paths)
    home_bin.mkdir()
    in_home = home_bin / tunnel.binary_name("ngrok")
    in_home.write_text("#!/bin/sh\n", encoding="utf-8")
    in_home.chmod(0o755)
    assert tunnel.find_binary("ngrok", None, paths) == str(in_home)
    configured = tmp_path / "mine"
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)
    assert tunnel.find_binary("ngrok", str(configured), paths) == str(configured)
    assert tunnel.find_binary("ngrok", str(tmp_path / "missing"), paths) == str(in_home)


# -- the service with fake processes -----------------------------------------------------


class FakeProc:
    """A process whose stdout is fed by the test."""

    def __init__(self, lines: list[str] | None = None, *, exit_after: bool = True) -> None:
        self.stdout = asyncio.StreamReader()
        self.returncode: int | None = None
        self._done = asyncio.Event()
        for line in lines or []:
            self.stdout.feed_data((line + "\n").encode())
        if exit_after:
            self.stdout.feed_eof()
            self.returncode = 1
            self._done.set()

    def terminate(self) -> None:
        self.returncode = -15 if self.returncode is None else self.returncode
        self.stdout.feed_eof()
        self._done.set()

    kill = terminate

    async def wait(self) -> int:
        await self._done.wait()
        return int(self.returncode or 0)


def _service(
    tmp_path: Path, provider: str, procs: list[FakeProc], *, hostname: str | None = None,
    http: Any = None, events: list[Any] | None = None, port: int = 8787,
) -> tuple[TunnelService, list[list[str]], list[float], Paths]:
    paths = _paths(tmp_path)
    program = tmp_path / "fake-program"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    settings = Settings()
    settings.web.remote.enabled = True
    settings.web.remote.tunnel.provider = provider  # type: ignore[assignment]
    settings.web.remote.tunnel.binary = str(program)
    settings.web.remote.tunnel.hostname = hostname
    argvs: list[list[str]] = []
    slept: list[float] = []
    queue = list(procs)

    async def spawn(argv: list[str], env: dict[str, str]) -> FakeProc:
        argvs.append(argv + [f"ENV:{k}={v}" for k, v in env.items()
                             if k in {"TUNNEL_TOKEN", "NGROK_AUTHTOKEN"}])
        return queue.pop(0) if queue else FakeProc(exit_after=False)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    async def notify(method: str, params: dict[str, Any]) -> None:
        if events is not None:
            events.append((method, params))

    service = TunnelService(
        paths, settings=lambda: settings.web.remote,
        listener=lambda: _listener(port=port), notify=notify, spawn=spawn,
        http_get_json=http, sleep=sleep)
    return service, argvs, slept, paths


async def test_quick_tunnel_runs_and_reports_url(tmp_path: Path) -> None:
    events: list[Any] = []
    proc = FakeProc(QUICK_LOG.splitlines(), exit_after=False)
    service, argvs, _, _ = _service(tmp_path, "cloudflare-quick", [proc], events=events)
    status = await service.start(wait=2)
    assert status["state"] == "running"
    assert status["url"] == "https://random-words-here-abcd.trycloudflare.com"
    assert service.active and service.origin == status["url"]
    assert service.is_tunnel_host("random-words-here-abcd.trycloudflare.com:443")
    assert not service.is_tunnel_host("evil.example")
    assert argvs[0][1:] == ["tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8787"]
    assert [e[1]["state"] for e in events if e[0] == "web.tunnel.changed"] == ["starting", "running"]
    stopped = await service.stop()
    assert stopped["state"] == "off" and stopped["url"] is None and not service.active


async def test_named_tunnel_uses_hostname_and_env_token(tmp_path: Path) -> None:
    proc = FakeProc(NAMED_LOG.splitlines(), exit_after=False)
    service, argvs, _, paths = _service(
        tmp_path, "cloudflare", [proc], hostname="snow.example.com")
    with pytest.raises(TunnelError) as exc:
        await service.start(wait=0)
    assert exc.value.code == "no_token"
    CredentialStore(paths).set(tunnel.CLOUDFLARE_TOKEN_KEY, "cf-secret-token")
    status = await service.start(wait=2)
    assert status["state"] == "running" and status["url"] == "https://snow.example.com"
    # the token travels in the environment, never on the command line
    assert argvs[0][1:4] == ["tunnel", "--no-autoupdate", "run"]
    assert "cf-secret-token" not in " ".join(a for a in argvs[0] if not a.startswith("ENV:"))
    assert "ENV:TUNNEL_TOKEN=cf-secret-token" in argvs[0]
    assert "cf-secret-token" not in json.dumps(status)
    await service.stop()


async def test_named_tunnel_needs_hostname(tmp_path: Path) -> None:
    service, _, _, paths = _service(tmp_path, "cloudflare", [], hostname=None)
    CredentialStore(paths).set(tunnel.CLOUDFLARE_TOKEN_KEY, "tok")
    with pytest.raises(TunnelError) as exc:
        await service.start(wait=0)
    assert exc.value.code == "no_hostname"


async def test_ngrok_url_from_json_log(tmp_path: Path) -> None:
    proc = FakeProc(NGROK_LOG, exit_after=False)
    service, argvs, _, paths = _service(tmp_path, "ngrok", [proc])
    CredentialStore(paths).set(tunnel.NGROK_TOKEN_KEY, "ng-secret")
    status = await service.start(wait=2)
    assert status["url"] == "https://1a2b-3c4d.ngrok-free.app"
    assert argvs[0][1:] == ["http", "127.0.0.1:8787", "--log", "stdout", "--log-format", "json",
                            "ENV:NGROK_AUTHTOKEN=ng-secret"]
    await service.stop()


async def test_ngrok_falls_back_to_the_local_api(tmp_path: Path) -> None:
    proc = FakeProc(NGROK_LOG[:2], exit_after=False)  # the log never shows the address
    calls: list[str] = []

    async def http(url: str) -> Any:
        calls.append(url)
        if len(calls) < 2:
            raise OSError("not up yet")
        return NGROK_API

    service, _, _, paths = _service(tmp_path, "ngrok", [proc], http=http)
    CredentialStore(paths).set(tunnel.NGROK_TOKEN_KEY, "ng-secret")
    status = await service.start(wait=3)
    assert status["state"] == "running" and status["url"] == "https://1a2b-3c4d.ngrok-free.app"
    assert calls[0] == "http://127.0.0.1:4040/api/tunnels"
    await service.stop()


async def test_start_refuses_without_preconditions_or_binary(tmp_path: Path) -> None:
    service, _, _, paths = _service(tmp_path, "cloudflare-quick", [])
    web_remote.clear_password(paths)
    with pytest.raises(TunnelError) as exc:
        await service.start(wait=0)
    assert exc.value.code == "no_password" and service.state == "off"
    web_remote.set_password(paths, PASSWORD)
    service._remote_settings().binary = str(tmp_path / "missing")
    with pytest.raises(TunnelError) as exc:
        await service.start(wait=0)
    assert exc.value.code == "no_binary"
    with pytest.raises(TunnelError) as exc:
        await service.start("nonsense", wait=0)
    assert exc.value.code == "bad_provider"


async def test_restart_backoff_doubles_and_resets(tmp_path: Path) -> None:
    # Up, then dropped three times: delays 2, 4, 8 (each run was short, so no reset).
    procs = [FakeProc(QUICK_LOG.splitlines()[:5]) for _ in range(3)] + [
        FakeProc(QUICK_LOG.splitlines()[:5], exit_after=False)]
    service, argvs, slept, _ = _service(tmp_path, "cloudflare-quick", procs)
    await service.start(wait=2)
    for _ in range(200):
        if len(argvs) == 4:
            break
        await asyncio.sleep(0.01)
    assert len(argvs) == 4
    assert service.delays == [2.0, 4.0, 8.0]
    assert slept[:3] == [2.0, 4.0, 8.0]
    assert service.state in {"running", "starting"} and service.restarts == 3
    await service.stop()


async def test_backoff_is_capped(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path, "cloudflare-quick", [])
    ticks = iter(range(0, 10_000, 100))  # every run "lasts" 100 s: stable, count resets
    service.clock = lambda: float(next(ticks))
    procs = [FakeProc(QUICK_LOG.splitlines()[:5]) for _ in range(3)]
    queue = list(procs)

    async def spawn(argv: list[str], env: dict[str, str]) -> FakeProc:
        return queue.pop(0) if queue else FakeProc(exit_after=False)

    service._spawn = spawn
    await service.start(wait=2)
    for _ in range(200):
        if len(service.delays) >= 3:
            break
        await asyncio.sleep(0.01)
    assert service.delays[:3] == [2.0, 2.0, 2.0]  # stable runs start the count again
    await service.stop()
    assert min(tunnel.BACKOFF_MAX_SEC, tunnel.BACKOFF_BASE_SEC * 2 ** 10) == tunnel.BACKOFF_MAX_SEC


async def test_gives_up_when_it_never_shows_an_address(tmp_path: Path) -> None:
    procs = [FakeProc(["INF something went wrong"]) for _ in range(tunnel.MAX_EARLY_FAILURES)]
    service, argvs, _, _ = _service(tmp_path, "cloudflare-quick", procs)
    await service.start(wait=0)
    for _ in range(300):
        if service.state == "error":
            break
        await asyncio.sleep(0.01)
    assert service.state == "error" and len(argvs) == tunnel.MAX_EARLY_FAILURES
    assert "something went wrong" in (service.error or "")
    assert not service.active


async def test_log_lines_never_show_the_token(tmp_path: Path) -> None:
    proc = FakeProc(["ERR bad token cf-secret-token rejected"])
    service, _, _, paths = _service(tmp_path, "cloudflare", [proc], hostname="snow.example.com")
    CredentialStore(paths).set(tunnel.CLOUDFLARE_TOKEN_KEY, "cf-secret-token")
    await service.start(wait=0)
    for _ in range(100):
        if service.error:
            break
        await asyncio.sleep(0.01)
    assert service.error and "cf-secret-token" not in service.error
    assert "cf-secret-token" not in json.dumps(service.status())
    await service.stop()


async def test_autostart_follows_settings(tmp_path: Path) -> None:
    proc = FakeProc(QUICK_LOG.splitlines(), exit_after=False)
    service, argvs, _, _ = _service(tmp_path, "cloudflare-quick", [proc])
    service._remote_settings().autoStart = False
    await service.autostart()
    assert argvs == [] and service.state == "off"
    service._remote_settings().autoStart = True
    await service.autostart()
    for _ in range(100):
        if service.state == "running":
            break
        await asyncio.sleep(0.01)
    assert service.state == "running"
    await service.stop()
    # a provider set but a precondition missing: stays off, remembers why, no exception
    service2, argvs2, _, paths2 = _service(tmp_path / "b", "cloudflare-quick", [])
    web_remote.clear_password(paths2)
    await service2.autostart()
    assert argvs2 == [] and service2.state == "off" and "password" in (service2.error or "")


# -- terminal unlock -----------------------------------------------------------------------


def _conn(*, via: bool, unlocked: bool = False) -> RpcConnection:
    conn = RpcConnection(lambda obj: asyncio.sleep(0))
    conn.via_tunnel = via
    state = {"unlocked": unlocked}
    conn.tunnel_unlocked = lambda: state["unlocked"]
    conn.tunnel_mark_unlocked = lambda: state.update(unlocked=True)
    return conn


def test_unlock_gate() -> None:
    require_terminal_unlock(_conn(via=False))  # off the tunnel: nothing changes
    require_terminal_unlock(_conn(via=False), dangerous=True)
    locked = _conn(via=True)
    with pytest.raises(RpcError) as exc:
        require_terminal_unlock(locked)
    assert exc.value.data["reason"] == tunnel.TERMINAL_LOCKED
    locked.tunnel_mark_unlocked()  # type: ignore[misc]
    require_terminal_unlock(locked)
    # dangerous options also need maxMode auto
    core = SimpleNamespace(settings=SimpleNamespace(
        web=SimpleNamespace(remote=SimpleNamespace(maxMode="accept"))))
    with pytest.raises(RpcError) as exc:
        require_terminal_unlock(locked, dangerous=True, core=core)
    assert exc.value.data["reason"] == "tunnel_dangerous"
    core.settings.web.remote.maxMode = "auto"
    require_terminal_unlock(locked, dangerous=True, core=core)
    with pytest.raises(RpcError):  # no core to read the mode from: refused
        require_terminal_unlock(locked, dangerous=True)
    # a connection without the marks (a plain test double) is not over the tunnel
    require_terminal_unlock(SimpleNamespace())  # type: ignore[arg-type]


# -- origins, forwarded headers and the marks on a real remote socket -------------------


class FakeTunnel:
    def __init__(self, url: str = "https://snow-abc.trycloudflare.com", active: bool = True) -> None:
        self.active = active
        self.origin = url
        self.url = url

    def is_tunnel_host(self, host: str | None) -> bool:
        return bool(host) and str(host).split(":")[0] == self.url.removeprefix("https://")


async def _remote(tmp_path: Path, fake: Any = None) -> tuple[TestClient, RemoteAuth, set[Any]]:
    paths = _paths(tmp_path)
    auth = RemoteAuth("owner", 12, paths)
    auth.failure_delay = 0.0
    auth.penalty_base = 0.0
    auth.tunnel = fake
    connections: set[Any] = set()
    app = create_remote_app(RpcDispatcher(SimpleNamespace(token="t", hub=None)), auth,
                            connections=connections)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client, auth, connections


async def _login(client: TestClient, **headers: str) -> aiohttp.ClientResponse:
    return await client.post("/web/login", json={"username": "owner", "password": PASSWORD},
                             headers=headers)


async def test_origin_is_allowed_only_while_the_tunnel_runs(tmp_path: Path) -> None:
    fake = FakeTunnel()
    client, _, _ = await _remote(tmp_path, fake)
    try:
        assert (await _login(client)).status == 200
        ws = await client.ws_connect("/ws", headers={"Origin": fake.origin})
        await ws.close()
        with pytest.raises(aiohttp.WSServerHandshakeError) as bad:
            await client.ws_connect("/ws", headers={"Origin": "https://evil.example"})
        assert bad.value.status == 403
        fake.active = False  # the tunnel stopped: its origin is no longer allowed
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await client.ws_connect("/ws", headers={"Origin": fake.origin})
    finally:
        await client.close()


async def test_forwarded_headers_trusted_only_while_the_tunnel_runs(tmp_path: Path) -> None:
    fake = FakeTunnel()
    client, auth, _ = await _remote(tmp_path, fake)
    try:
        # Failures are counted per client address: the forwarded one while the tunnel runs.
        for _ in range(web_remote.MAX_FAILURES_PER_IP):
            bad = await client.post(
                "/web/login", json={"username": "owner", "password": "wrong"},
                headers={"CF-Connecting-IP": "203.0.113.7"})
            assert bad.status == 401
        assert auth.retry_after("203.0.113.7") > 0
        assert auth.retry_after("127.0.0.1") == 0
        # forwarded proto makes the cookie secure
        ok = await _login(client, **{"X-Forwarded-Proto": "https"})
        assert "Secure" in ok.headers["Set-Cookie"]
        # the tunnel stopped and trustProxy is off: the headers are ignored again
        fake.active = False
        await client.post("/web/login", json={"username": "owner", "password": "wrong"},
                          headers={"CF-Connecting-IP": "198.51.100.9"})
        assert auth.retry_after("198.51.100.9") == 0
        plain = await _login(client, **{"X-Forwarded-Proto": "https"})
        assert "Secure" not in plain.headers["Set-Cookie"]
    finally:
        await client.close()


async def test_connection_is_marked_by_host_and_unlock_is_per_session(tmp_path: Path) -> None:
    fake = FakeTunnel()
    client, auth, connections = await _remote(tmp_path, fake)
    try:
        assert (await _login(client)).status == 200
        local = await client.ws_connect(
            "/ws", headers={"Origin": f"http://{client.server.host}:{client.server.port}"})
        tunneled = await client.ws_connect(
            "/ws", headers={"Origin": fake.origin, "Host": "snow-abc.trycloudflare.com"})
        for _ in range(100):
            if len(connections) == 2:
                break
            await asyncio.sleep(0.01)
        by_mark = {c.via_tunnel: c for c in connections}
        assert set(by_mark) == {True, False}
        via, plain = by_mark[True], by_mark[False]
        require_terminal_unlock(plain)  # not over the tunnel
        with pytest.raises(RpcError):
            require_terminal_unlock(via)
        # wrong password does not unlock; the right one does, for this sign-in session only
        via.remote_reauth = lambda password: auth.reauthenticate(  # type: ignore[assignment]
            next(iter(auth.sessions)), "203.0.113.1", password)
        with pytest.raises(Exception):  # noqa: B017 - RemoteGrantRefused
            await remote_grant.reauthenticate(via, "not the password")
        assert not tunnel.terminal_unlocked(via)
        await remote_grant.reauthenticate(via, PASSWORD)
        via.tunnel_mark_unlocked()  # type: ignore[misc]
        require_terminal_unlock(via)
        assert next(iter(auth.sessions.values())).terminal_unlocked
        # a new sign-in is a new session: locked again
        await client.post("/web/logout")
        assert (await _login(client)).status == 200
        newest = [s for s in auth.sessions.values() if not s.terminal_unlocked]
        assert newest
        await local.close()
        await tunneled.close()
    finally:
        await client.close()


# -- installer -------------------------------------------------------------------------------


async def test_install_refuses_a_checksum_mismatch(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    payload = b"not the real cloudflared"
    key = ("linux", "amd64")
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return payload

    with pytest.raises(TunnelError) as exc:
        await install_binary("cloudflare-quick", paths, fetch=fetch, key=key)
    assert exc.value.code == "checksum_mismatch"
    assert fetched and fetched[0].startswith("https://github.com/cloudflare/cloudflared/")
    assert not tunnel.bin_dir(paths).exists() or not list(tunnel.bin_dir(paths).iterdir())


async def test_install_writes_the_verified_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    payload = b"#!/bin/sh\necho fake\n"
    pinned = PinnedAsset("https://example.invalid/cloudflared", hashlib.sha256(payload).hexdigest())
    monkeypatch.setitem(tunnel.PINNED["cloudflared"], ("linux", "amd64"), pinned)
    path = await install_binary("cloudflare-quick", paths, fetch=lambda url: payload,
                                key=("linux", "amd64"))
    assert path.read_bytes() == payload
    if os.name == "posix":
        assert path.stat().st_mode & stat.S_IXUSR
    assert [p.name for p in tunnel.bin_dir(paths).iterdir()] == [path.name]  # no temp left


async def test_install_refuses_without_a_pinned_checksum(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    def fetch(url: str) -> bytes:  # pragma: no cover - must not be reached
        raise AssertionError("nothing may be downloaded without a pinned checksum")

    for provider, key in (("ngrok", ("linux", "amd64")), ("cloudflare", ("darwin", "arm64"))):
        with pytest.raises(TunnelError) as exc:
            await install_binary(provider, paths, fetch=fetch, key=key)
        assert exc.value.code == "install_unpinned" and exc.value.details["command"]


def test_pinned_table_is_well_formed() -> None:
    for assets in tunnel.PINNED.values():
        for asset in assets.values():
            assert asset.url.startswith("https://github.com/cloudflare/cloudflared/releases/")
            assert tunnel.CLOUDFLARED_VERSION in asset.url
            assert len(asset.sha256) == 64 and int(asset.sha256, 16) >= 0


# -- CLI token ----------------------------------------------------------------------------------


def test_cli_token_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("sys.stdin", io.StringIO("ng-token-123\n"))
    assert tunnel_cmd.token_command(home, "ngrok") == 0
    assert CredentialStore(Paths(home=home)).load()[tunnel.NGROK_TOKEN_KEY] == "ng-token-123"
    assert not (home / "settings.json").exists()
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert tunnel_cmd.token_command(home, "cloudflare") == 2
    assert tunnel_cmd.token_command(home, "cloudflare-quick") == 2


# -- end to end with fake programs (small Python scripts) -------------------------------------


def _fake_program(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name != "posix", reason="fake programs are shebang scripts")
async def test_end_to_end_with_fake_programs(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    marker = tmp_path / "argv.json"
    quick = _fake_program(tmp_path / "fake-cloudflared", f"""
import json, sys, time
json.dump(sys.argv[1:], open({str(marker)!r}, "w"))
print("2026-11-23T10:00:01Z INF |  https://fake-words-one.trycloudflare.com  |", flush=True)
time.sleep(60)
""")
    settings = Settings()
    settings.web.remote.enabled = True
    settings.web.remote.tunnel.provider = "cloudflare-quick"
    settings.web.remote.tunnel.binary = str(quick)
    events: list[Any] = []

    async def notify(method: str, params: dict[str, Any]) -> None:
        events.append(params["state"])

    service = TunnelService(paths, settings=lambda: settings.web.remote,
                            listener=lambda: _listener(port=4321), notify=notify)
    try:
        status = await service.start(wait=15)
        assert status["state"] == "running"
        assert status["url"] == "https://fake-words-one.trycloudflare.com"
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert json.loads(marker.read_text(encoding="utf-8")) == [
            "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:4321"]
    finally:
        stopped = await service.stop()
    assert stopped["state"] == "off" and events[-1] == "off"
    assert service._proc is None


@pytest.mark.skipif(os.name != "posix", reason="fake programs are shebang scripts")
async def test_end_to_end_restarts_a_dying_program(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    counter = tmp_path / "runs"
    program = _fake_program(tmp_path / "fake-cloudflared", f"""
import os, sys, time
runs = int(open({str(counter)!r}).read()) if os.path.exists({str(counter)!r}) else 0
open({str(counter)!r}, "w").write(str(runs + 1))
print("INF |  https://run-%d.trycloudflare.com  |" % runs, flush=True)
if runs == 0:
    sys.exit(3)
time.sleep(60)
""")
    settings = Settings()
    settings.web.remote.enabled = True
    settings.web.remote.tunnel.provider = "cloudflare-quick"
    settings.web.remote.tunnel.binary = str(program)
    delays: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0.01)

    service = TunnelService(paths, settings=lambda: settings.web.remote,
                            listener=lambda: _listener(), sleep=fast_sleep)
    try:
        await service.start(wait=15)
        for _ in range(300):
            if service.url == "https://run-1.trycloudflare.com" and service.state == "running":
                break
            await asyncio.sleep(0.05)
        assert service.url == "https://run-1.trycloudflare.com" and service.state == "running"
        assert delays == [2.0]
    finally:
        await service.stop()


# -- the web.tunnel.* handlers -----------------------------------------------------------------


async def test_handlers_start_status_stop_and_token(tmp_path: Path) -> None:
    from snowpea_core.server import tunnel_handlers as h
    from snowpea_core.server.protocol import (
        Empty,
        TunnelSetTokenParams,
        TunnelStartParams,
        TunnelStopParams,
    )

    proc = FakeProc(QUICK_LOG.splitlines(), exit_after=False)
    service, _, _, paths = _service(tmp_path, "cloudflare-quick", [proc])
    saved: list[dict[str, Any]] = []

    async def save(core: Any, **values: Any) -> None:
        saved.append(values)

    core = SimpleNamespace(tunnel=service, paths=paths)
    conn = _conn(via=False)
    orig = h._save_tunnel
    h._save_tunnel = save  # type: ignore[assignment]
    try:
        started = await h.start_handler(conn, TunnelStartParams(save=True), core)
        assert started.state == "running" and started.url.endswith(".trycloudflare.com")
        assert saved == [{"provider": "cloudflare-quick"}]
        assert (await h.status_handler(conn, Empty(), core)).state == "running"
        token = "ng-secret-1"
        status = await h.set_token_handler(
            conn, TunnelSetTokenParams(provider="ngrok", token=token), core)
        assert status.provider == "cloudflare-quick" and token not in status.model_dump_json()
        assert CredentialStore(paths).load()[tunnel.NGROK_TOKEN_KEY] == token
        with pytest.raises(RpcError):
            await h.set_token_handler(
                conn, TunnelSetTokenParams(provider="ngrok", token="has space"), core)
        stopped = await h.stop_handler(conn, TunnelStopParams(save=True), core)
        assert stopped.state == "off" and saved[-1] == {"provider": "off", "autoStart": False}
    finally:
        h._save_tunnel = orig  # type: ignore[assignment]
        await service.stop()


async def test_handler_start_error_is_unavailable_with_reason(tmp_path: Path) -> None:
    from snowpea_core.server import tunnel_handlers as h
    from snowpea_core.server.protocol import TunnelStartParams

    service, _, _, paths = _service(tmp_path, "cloudflare-quick", [])
    web_remote.clear_password(paths)
    with pytest.raises(RpcError) as exc:
        await h.start_handler(_conn(via=False), TunnelStartParams(), SimpleNamespace(tunnel=service))
    assert exc.value.code == "unavailable" and exc.value.data["reason"] == "no_password"


async def test_handler_unlock_marks_the_session(tmp_path: Path) -> None:
    from snowpea_core.server import tunnel_handlers as h
    from snowpea_core.server.protocol import TunnelUnlockParams

    conn = _conn(via=True)
    calls: list[str] = []

    async def reauth(password: Any) -> str:
        calls.append(password)
        return "ok" if password == PASSWORD else "failed"

    conn.remote_reauth = reauth
    with pytest.raises(Exception):  # noqa: B017 - the refusal for a wrong password
        await h.unlock_handler(conn, TunnelUnlockParams(password="wrong"), SimpleNamespace())
    with pytest.raises(RpcError):
        require_terminal_unlock(conn)
    result = await h.unlock_handler(conn, TunnelUnlockParams(password=PASSWORD), SimpleNamespace())
    assert result.unlocked
    require_terminal_unlock(conn)


def test_secret_params_are_dropped_from_validation_errors() -> None:
    from snowpea_core.server import rpc

    assert {"token", "password"} <= rpc.SECRET_PARAMS


async def test_terminal_unlock_is_an_alias_of_web_tunnel_unlock() -> None:
    from snowpea_core.server import terminal_handlers
    from snowpea_core.server.protocol import TerminalUnlockParams

    conn = _conn(via=True)

    async def reauth(password: Any) -> str:
        return "ok" if password == PASSWORD else "failed"

    conn.remote_reauth = reauth
    handler = terminal_handlers._HANDLERS["terminal.unlock"]  # noqa: SLF001
    with pytest.raises(Exception):  # noqa: B017 - wrong password
        await handler(conn, TerminalUnlockParams(password="nope"), SimpleNamespace())
    result = await handler(conn, TerminalUnlockParams(password=PASSWORD), SimpleNamespace())
    assert result.unlocked and result.viaTunnel
    require_terminal_unlock(conn)
