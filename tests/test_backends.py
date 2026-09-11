"""US-010 / AC-18: the three execution backends really run somewhere different.

The docker and ssh cases need infrastructure, so they ``pytest.skip`` when the
``docker`` CLI is unavailable or the SSH compose fixture does not come up.  They
never fail CI for missing infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from snowpea_core.commands.backend_cmd import parse_args
from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.exec.docker import DockerBackend, container_name_for
from snowpea_core.exec.factory import build_backend
from snowpea_core.exec.local import LocalBackend
from snowpea_core.exec.ssh import SshBackend
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.protocol import PROTOCOL_VERSION

FIXTURES = Path(__file__).parent / "fixtures" / "ssh"
COMPOSE_FILE = FIXTURES / "docker-compose.yml"
SSH_KEY = FIXTURES / "id_test"
SSH_HOST_NAME = "snowpea-ssh-fixture"
SSH_PORT = 2222
COMPOSE_TIMEOUT = 60
DOCKER_IMAGE = "python:3.11-slim"


# ---------------------------------------------------------------------------
# infrastructure helpers
# ---------------------------------------------------------------------------


def _run(*args: str, timeout: int = COMPOSE_TIMEOUT) -> tuple[int, str, str]:
    import subprocess

    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - env dependent
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def docker_available() -> bool:
    return _run("docker", "info", timeout=30)[0] == 0


@pytest.fixture(scope="module")
def docker_cli() -> None:
    if not docker_available():
        pytest.skip("the docker CLI is unavailable")


@pytest.fixture(scope="module")
def ssh_host(docker_cli: None) -> Iterator[dict[str, Any]]:
    """Bring the compose fixture up, or skip if it will not start in time."""
    compose = ("docker", "compose", "-f", str(COMPOSE_FILE))
    _run(*compose, "down", "-v", timeout=COMPOSE_TIMEOUT)  # clear a stale container
    code, _, err = _run(*compose, "up", "-d", "--wait", timeout=COMPOSE_TIMEOUT)
    if code != 0:
        _run(*compose, "down", "-v", timeout=COMPOSE_TIMEOUT)
        pytest.skip(f"ssh fixture did not start within {COMPOSE_TIMEOUT}s: {err.strip()[:200]}")
    try:
        hostname = _run(*compose, "exec", "-T", "sshd", "hostname", timeout=30)[1].strip()
        yield {
            "host": "127.0.0.1",
            "port": SSH_PORT,
            "user": "snowpea",
            "key": str(SSH_KEY),
            "cwd": "/config",
            "hostname": hostname or SSH_HOST_NAME,
        }
    finally:
        _run(*compose, "down", "-v", timeout=COMPOSE_TIMEOUT)


async def _hostname(backend: ExecutionBackend) -> str:
    result = await backend.run("hostname", timeout=60)
    assert result.ok, result.stderr
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# local
# ---------------------------------------------------------------------------


async def test_local_backend_round_trips_files_and_commands(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path)
    assert backend.kind == "local"
    assert await _hostname(backend) == socket.gethostname()

    await backend.write_file("nested/hello.txt", "hi")
    assert await backend.read_file("nested/hello.txt") == "hi"
    assert await backend.exists("nested/hello.txt")
    assert not await backend.exists("nested/missing.txt")
    assert await backend.list_dir(".") == ["nested/"]
    assert (tmp_path / "nested" / "hello.txt").read_text() == "hi"
    await backend.close()


async def test_local_backend_reports_timeouts(tmp_path: Path) -> None:
    result = await LocalBackend(tmp_path).run("sleep 5", timeout=0.2)
    assert result.timed_out and result.exit_code == 124 and not result.ok


def test_build_backend_rejects_unknown_kinds(tmp_path: Path) -> None:
    assert build_backend("local", {}, workdir=tmp_path).kind == "local"
    with pytest.raises(ValueError, match="unknown backend kind"):
        build_backend("kubernetes", {}, workdir=tmp_path)


def test_parse_args_reads_kind_and_json_config() -> None:
    assert parse_args("local") == ("local", {})
    assert parse_args('docker {"image": "alpine"}') == ("docker", {"image": "alpine"})
    with pytest.raises(ValueError, match="JSON object"):
        parse_args("docker not-json")


def test_container_name_is_derived_from_the_session_id() -> None:
    assert container_name_for("s-0123456789ab") == "snowpea-01234567"


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def docker_backend(docker_cli: None, tmp_path: Path) -> AsyncIterator[DockerBackend]:
    backend = DockerBackend(
        {"image": DOCKER_IMAGE},
        session_id=f"s-{uuid.uuid4().hex[:12]}",
        workdir=tmp_path,
    )
    try:
        yield backend
    finally:
        await backend.close()


async def test_docker_backend_runs_in_its_own_container(
    docker_backend: DockerBackend, tmp_path: Path
) -> None:
    container_hostname = await _hostname(docker_backend)
    assert container_hostname
    assert container_hostname != socket.gethostname()

    # The workdir is bind-mounted, so a write inside is visible outside.
    await docker_backend.write_file("inside.txt", "from the container")
    assert (tmp_path / "inside.txt").read_text() == "from the container"
    assert await docker_backend.exists("inside.txt")
    assert "inside.txt" in await docker_backend.list_dir(".")
    assert await docker_backend.read_file("inside.txt") == "from the container"


async def test_docker_backend_removes_the_container_on_close(
    docker_cli: None, tmp_path: Path
) -> None:
    backend = DockerBackend(
        {"image": DOCKER_IMAGE}, session_id=f"s-{uuid.uuid4().hex[:12]}", workdir=tmp_path
    )
    assert (await backend.run("true", timeout=300)).ok
    assert _run("docker", "inspect", backend.container, timeout=30)[0] == 0
    await backend.close()
    assert _run("docker", "inspect", backend.container, timeout=30)[0] != 0


# ---------------------------------------------------------------------------
# ssh
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ssh_backend(ssh_host: dict[str, Any]) -> AsyncIterator[SshBackend]:
    backend = SshBackend({k: v for k, v in ssh_host.items() if k != "hostname"})
    try:
        yield backend
    finally:
        await backend.close()


async def test_ssh_backend_runs_on_the_fixture_host(
    ssh_backend: SshBackend, ssh_host: dict[str, Any]
) -> None:
    remote = await _hostname(ssh_backend)
    assert remote == ssh_host["hostname"]
    assert remote != socket.gethostname()


async def test_ssh_writes_are_not_visible_locally(ssh_backend: SshBackend, tmp_path: Path) -> None:
    name = f"remote-{uuid.uuid4().hex[:8]}.txt"
    await ssh_backend.write_file(name, "only on the remote host")
    assert await ssh_backend.read_file(name) == "only on the remote host"
    assert await ssh_backend.exists(name)
    assert name in await ssh_backend.list_dir(".")

    local = LocalBackend(tmp_path)
    assert not await local.exists(name)
    assert not (Path("/config") / name).exists()


async def test_all_three_backends_report_different_hosts(
    ssh_backend: SshBackend, docker_backend: DockerBackend, tmp_path: Path
) -> None:
    local_name = await _hostname(LocalBackend(tmp_path))
    docker_name = await _hostname(docker_backend)
    ssh_name = await _hostname(ssh_backend)
    assert len({local_name, docker_name, ssh_name}) == 3


async def test_local_backend_is_restored_after_a_remote_detour(
    ssh_backend: SshBackend, tmp_path: Path
) -> None:
    await ssh_backend.write_file("detour.txt", "remote")
    local = build_backend("local", {}, workdir=tmp_path)
    await local.write_file("detour.txt", "local")
    assert await local.read_file("detour.txt") == "local"
    assert await ssh_backend.read_file("detour.txt") == "remote"
    await local.close()


# ---------------------------------------------------------------------------
# backend.set over RPC
# ---------------------------------------------------------------------------


class _Client:
    """Minimal JSON-RPC client that also records ``session.event`` frames."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self.events: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        while True:
            message = json.loads(await self._ws.receive_str())
            if message.get("method") == "session.event":
                self.events.append(message["params"])
                continue
            if message.get("id") == request_id and "method" not in message:
                return message

    async def drain(self, timeout: float = 1.0) -> None:
        """Collect notifications that arrive after the response we waited on."""
        try:
            while True:
                raw = await asyncio.wait_for(self._ws.receive_str(), timeout)
                message = json.loads(raw)
                if message.get("method") == "session.event":
                    self.events.append(message["params"])
        except (TimeoutError, TypeError):
            return


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    os.environ["SNOWPEA_TEST"] = "1"
    instance = Daemon(port=0, home=tmp_path / "home")
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()
        os.environ.pop("SNOWPEA_TEST", None)


async def test_backend_set_swaps_the_session_backend_and_emits_an_event(
    daemon: Daemon, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
        client = _Client(ws)
        await client.call(
            "system.hello",
            {
                "token": daemon.token,
                "clientVersion": "test-1",
                "protocolVersion": PROTOCOL_VERSION,
            },
        )
        created = await client.call("session.create", {"workdir": str(tmp_path)})
        session_id = created["result"]["sessionId"]

        core = daemon.core
        assert core is not None
        session = core.sessions.get(session_id)
        assert session is not None
        first = session.backend
        assert first.kind == "local"
        assert Path(str(first.cwd)) == tmp_path

        response = await client.call(
            "backend.set",
            {"sessionId": session_id, "kind": "local", "config": {"workdir": str(other)}},
        )
        assert response["result"] == {"ok": True}
        assert session.backend is not first
        assert Path(str(session.backend.cwd)) == other

        await client.drain()
        kinds = [event["kind"] for event in client.events]
        assert "backend.changed" in kinds
        changed = next(e for e in client.events if e["kind"] == "backend.changed")
        assert changed["payload"] == {"backend": "local"}
        await ws.close()


async def test_backend_command_switches_the_backend(daemon: Daemon, tmp_path: Path) -> None:
    other = tmp_path / "via-command"
    other.mkdir()
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
        client = _Client(ws)
        await client.call(
            "system.hello",
            {
                "token": daemon.token,
                "clientVersion": "test-1",
                "protocolVersion": PROTOCOL_VERSION,
            },
        )
        created = await client.call("session.create", {"workdir": str(tmp_path)})
        session_id = created["result"]["sessionId"]
        core = daemon.core
        assert core is not None
        session = core.sessions.get(session_id)
        assert session is not None

        await client.call(
            "command.run",
            {
                "sessionId": session_id,
                "name": "/backend",
                "args": json.dumps({"workdir": str(other)}).join(("local ", "")),
            },
        )
        await client.drain()
        kinds = [event["kind"] for event in client.events]
        assert "backend.changed" in kinds, client.events
        assert Path(str(session.backend.cwd)) == other

        # An unknown kind is answered, not raised.
        client.events.clear()
        await client.call(
            "command.run", {"sessionId": session_id, "name": "/backend", "args": "kubernetes"}
        )
        await client.drain()
        said = " ".join(
            event["payload"].get("text", "")
            for event in client.events
            if event["kind"] == "message.done"
        )
        assert "Unknown backend" in said
        assert "backend.changed" not in [e["kind"] for e in client.events]
        await ws.close()


async def test_backend_set_rejects_an_unknown_session(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
        client = _Client(ws)
        await client.call(
            "system.hello",
            {
                "token": daemon.token,
                "clientVersion": "test-1",
                "protocolVersion": PROTOCOL_VERSION,
            },
        )
        response = await client.call(
            "backend.set", {"sessionId": "s-nope", "kind": "local", "config": {}}
        )
        assert response["error"]["data"]["code"] == "not_found"
        await ws.close()
