"""M53: ssh password login for remote machines."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon

import snowpea_core.machines.ssh_askpass as ssh_askpass_mod
from snowpea_core.cli import remote
from snowpea_core.cli.remote import RemoteError
from snowpea_core.machines.manager import MachineManager
from snowpea_core.machines.ssh import ssh_runner
from snowpea_core.machines.ssh_askpass import (
    PROC_BSDINFO_PBI_PPID_OFF,
    PROC_BSDINFO_START_SEC_OFF,
    PROC_BSDINFO_STRUCT_SIZE,
    SOCKET_ENV,
    TOKEN_ENV,
    AskpassRejected,
    SshAskpassBridge,
    classify_prompt_kind,
    parse_proc_bsdinfo,
    parse_xucred,
    request_secret,
    sanitize_ssh_prompt,
)
from snowpea_core.server import errors, protocol
from snowpea_core.server.errors import RpcError
from snowpea_core.server.machine_handlers import machine_answer_handler
from snowpea_core.server.protocol import PROTOCOL_VERSION, MachineAnswerParams
from snowpea_core.server.rpc import RpcConnection

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh and ssh")

KEY_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIHmKbW0o2A1pQ4FoBvUQJnVIg7E8M2nR2PzJfWq0QUpN"

_PASSWORD_BLOCK = r'''
def _maybe_askpass():
    if "-G" in argv or "-O" in argv:
        return True
    if "-L" in argv:
        return True
    if "BatchMode=yes" in argv:
        if argv[-2:] == ["sh", "-s"]:
            sys.stderr.write("me@box: Permission denied (publickey).\\n")
            sys.exit(255)
        return True
    ask = os.environ.get("SSH_ASKPASS")
    if not ask:
        return True
    if argv[-2:] != ["sh", "-s"]:
        return True
    prompt = "me@box.example.com's password:"
    done = subprocess.run([ask, prompt], capture_output=True, text=True, env=os.environ)
    secret = (done.stdout or "").strip()
    log_path = os.environ.get("FAKE_SSH_SECRET_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(secret + "\n")
    expect = os.environ.get("FAKE_SSH_EXPECT", "s3cret")
    bad_left = int(os.environ.get("FAKE_SSH_BAD_LEFT", "0"))
    if secret != expect:
        if bad_left > 0:
            os.environ["FAKE_SSH_BAD_LEFT"] = str(bad_left - 1)
            done = subprocess.run([ask, prompt], capture_output=True, text=True, env=os.environ)
            secret = (done.stdout or "").strip()
            if log_path:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(secret + "\n")
        if secret != expect:
            sys.stderr.write("me@box: Permission denied (publickey,password).\\n")
            sys.exit(255)
    return True
'''

FAKE_SSH = (
    """\
import asyncio, os, subprocess, sys
argv = sys.argv[1:]
"""
    + _PASSWORD_BLOCK
    + """
if "-G" in argv:
    print("user me")
    print("hostname box.example.com")
    print("port 22")
    print("userknownhostsfile " + os.environ["FAKE_KNOWN_HOSTS"])
    print("globalknownhostsfile /nonexistent/ssh_known_hosts")
    sys.exit(0)
if "-O" in argv:
    sys.exit(0)
if "-L" in argv:
    if "StrictHostKeyChecking=yes" not in argv:
        sys.exit(77)
    with open(os.environ["FAKE_TUNNEL_PID"], "w") as handle:
        handle.write(str(os.getpid()))
    spec = argv[argv.index("-L") + 1].split(":")
    local, rport = spec[1], spec[3]
    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data); await w.drain()
        finally:
            w.close()
    async def handle(r, w):
        ur, uw = await asyncio.open_connection("127.0.0.1", int(rport))
        await asyncio.gather(pipe(r, uw), pipe(ur, w))
    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", int(local))
        async with server:
            await server.serve_forever()
    asyncio.run(main())
    sys.exit(0)
if "StrictHostKeyChecking=yes" not in argv:
    sys.exit(77)
if os.environ.get("FAKE_SSH_DENY"):
    sys.stderr.write("me@box: Permission denied (publickey).\\n")
    sys.exit(255)
if not _maybe_askpass():
    sys.exit(255)
if os.environ.get("FAKE_REMOTE_ADVERT"):
    import json
    advert = json.loads(os.environ["FAKE_REMOTE_ADVERT"])
    cwd = os.environ.get("FAKE_REMOTE_CWD", "/home/me")
    print(f"SNOWPEA_ADVERT {json.dumps(advert)}\\nSNOWPEA_CWD {cwd}\\n", end="")
    sys.exit(0)
env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"])
env.pop("SNOWPEA_HOME", None)
sys.exit(subprocess.run(["sh", "-s"], env=env).returncode)
"""
)

FAKE_KEYSCAN = f"""\
import sys
print("box.example.com ssh-ed25519 {KEY_B64}")
"""

FAKE_KEYGEN = """\
import sys
argv = sys.argv[1:]
wanted = argv[argv.index("-F") + 1]
path = argv[argv.index("-f") + 1]
try:
    hit = any(line.split()[0] == wanted for line in open(path) if line.strip())
except OSError:
    hit = False
if hit:
    print("# Host found")
sys.exit(0 if hit else 1)
"""


def _write_tools(bin_dir: Path) -> None:
    for name, body in (
        ("ssh", FAKE_SSH),
        ("ssh-keyscan", FAKE_KEYSCAN),
        ("ssh-keygen", FAKE_KEYGEN),
    ):
        script = bin_dir / name
        script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        script.chmod(0o755)


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
    await client.ok(
        "machine.answer",
        {"id": machine_id, "askId": ask["id"], "answer": True},
    )


@pytest_asyncio.fixture
async def password_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    remote_home = tmp_path / "remote-home"
    remote_home.mkdir()
    remote_daemon = await make_daemon(remote_home)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_tools(bin_dir)
    local_home = tmp_path / "local-home"
    local_home.mkdir()
    secret_log = tmp_path / "secrets.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(remote_home))
    monkeypatch.setenv("FAKE_KNOWN_HOSTS", str(tmp_path / "known_hosts"))
    monkeypatch.setenv("FAKE_TUNNEL_PID", str(tmp_path / "tunnel.pid"))
    monkeypatch.setenv("FAKE_SSH_SECRET_LOG", str(secret_log))
    monkeypatch.setenv("FAKE_SSH_EXPECT", "s3cret")
    monkeypatch.setenv("PYTHONPATH", str(Path(remote.__file__).parents[2]))
    monkeypatch.setenv("SNOWPEA_UPDATE_CHECK", "0")
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.delenv("SNOWPEA_HOME", raising=False)
    advert = {
        "port": remote_daemon.port,
        "token": remote_daemon.token,
        "version": "0.0.0",
        "protocolVersion": PROTOCOL_VERSION,
    }
    monkeypatch.setenv("FAKE_REMOTE_ADVERT", json.dumps(advert))
    daemon = await make_daemon(tmp_path / "local")
    manager = MachineManager(daemon.core, ssh=str(bin_dir / "ssh"))
    manager.bind_loop(asyncio.get_running_loop())
    daemon.core.machines = manager
    try:
        yield {
            "daemon": daemon,
            "manager": manager,
            "secret_log": secret_log,
            "remote_daemon": remote_daemon,
        }
    finally:
        for mid in list(manager.registry.records):
            manager.disconnect(mid)
        await remote_daemon.stop()
        await daemon.stop()


def test_protocol_advertises_machine_password() -> None:
    assert "machinePassword" in protocol.CAPABILITIES
    assert PROTOCOL_VERSION == "1.81.0"


@posix_only
@pytest.mark.asyncio
async def test_password_ask_and_connect(password_stack: dict[str, Any]) -> None:
    daemon = password_stack["daemon"]
    secret_log: Path = password_stack["secret_log"]
    password = "s3cret"
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, ask_kind="password", timeout=30)
        ask = row["ask"]
        assert ask["kind"] == "password"
        assert ask["question"]
        await client.ok(
            "machine.answer",
            {"id": mid, "askId": ask["id"], "answer": True, "secret": password},
        )
        connected = await _wait_machine(client, mid, status="connected", timeout=90)
        assert connected["status"] == "connected"
        assert password in secret_log.read_text(encoding="utf-8")
        await client.stop()


@posix_only
@pytest.mark.asyncio
async def test_answer_false_cancels(password_stack: dict[str, Any]) -> None:
    daemon = password_stack["daemon"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, ask_kind="password", timeout=30)
        ask = row["ask"]
        await client.ok(
            "machine.answer",
            {"id": mid, "askId": ask["id"], "answer": False},
        )
        err = await _wait_machine(client, mid, status="error", timeout=60)
        assert err.get("error")
        await client.stop()


@posix_only
@pytest.mark.asyncio
async def test_wrong_password_fails(password_stack: dict[str, Any]) -> None:
    daemon = password_stack["daemon"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, ask_kind="password", timeout=30)
        ask = row["ask"]
        await client.ok(
            "machine.answer",
            {"id": mid, "askId": ask["id"], "answer": True, "secret": "wrong"},
        )
        err = await _wait_machine(client, mid, status="error", timeout=60)
        low = (err.get("error") or "").lower()
        assert "permission denied" in low or "authentication" in low or "could not reach" in low
        await client.stop()


@posix_only
@pytest.mark.asyncio
async def test_non_person_cannot_answer_password(password_stack: dict[str, Any]) -> None:
    daemon = password_stack["daemon"]
    manager: MachineManager = password_stack["manager"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        manager.connect(mid, interactive=True)
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, ask_kind="password", timeout=30)
        ask = row["ask"]
        browser = RpcConnection(sender=lambda *_a, **_k: None)
        browser.authenticated = True
        browser.client_kind = "browser"
        params = MachineAnswerParams.model_validate(
            {"id": mid, "askId": ask["id"], "answer": True, "secret": "s3cret"}
        )
        with pytest.raises(RpcError) as caught:
            await machine_answer_handler(browser, params, daemon.core)
        assert caught.value.code in (errors.UNAUTHORIZED, errors.JARVIS_PERSON_ONLY)
        manager.disconnect(mid)
        await client.stop()


@posix_only
@pytest.mark.asyncio
async def test_secret_not_in_notifications_or_logs(
    password_stack: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    daemon = password_stack["daemon"]
    secret = "s3cret"
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, ask_kind="password", timeout=30)
        ask = row["ask"]
        await client.ok(
            "machine.answer",
            {"id": mid, "askId": ask["id"], "answer": True, "secret": secret},
        )
        await _wait_machine(client, mid, status="connected", timeout=90)
        blobs = json.dumps(client.notifications) + caplog.text
        assert secret not in blobs
        await client.stop()


@posix_only
@pytest.mark.asyncio
async def test_background_reconnect_never_prompts(
    password_stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import snowpea_core.machines.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "HEALTH_EVERY_SEC", 0.05)
    monkeypatch.setattr(mgr_mod, "HEALTH_FAILURES", 1)
    daemon = password_stack["daemon"]
    manager: MachineManager = password_stack["manager"]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        mid = (await client.ok("machine.add", {"target": "me@box", "label": "box"}))["id"]
        await client.ok("machine.connect", {"id": mid})
        await _accept_host_key(client, mid)
        row = await _wait_machine(client, mid, ask_kind="password", timeout=30)
        await client.ok(
            "machine.answer",
            {
                "id": mid,
                "askId": row["ask"]["id"],
                "answer": True,
                "secret": "s3cret",
            },
        )
        await _wait_machine(client, mid, status="connected", timeout=90)
        live = manager._live[mid]  # noqa: SLF001
        monkeypatch.setattr(remote, "daemon_healthy", lambda _port: False)
        for _ in range(120):
            listed = await client.ok("machine.list")
            hit = next(m for m in listed["machines"] if m["id"] == mid)
            if hit.get("status") == "error" and "password" in (hit.get("error") or "").lower():
                break
            if hit.get("ask"):
                pytest.fail(f"background reconnect prompted: {hit['ask']}")
            await asyncio.sleep(0.05)
        else:
            pytest.fail("supervisor did not report password-needed error")
        assert live.session is None or live.status == "error"
        await client.stop()


def _ssh_child_script(body: str) -> str:
    return (
        "import os, subprocess, sys\n"
        f"{body}\n"
    )


@posix_only
def test_helper_rejects_bad_token(tmp_path: Path) -> None:
    got: list[str] = []

    def cb(prompt: str) -> str | None:
        got.append(prompt)
        return "ok"

    bridge = SshAskpassBridge(cb, home=tmp_path / "home")
    bridge.start()
    env = bridge.env()
    bad = _ssh_child_script(
        "import socket\n"
        f's=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
        f's.connect({env[SOCKET_ENV]!r})\n'
        's.sendall(b"bad-token\\n")\n'
        's.sendall(b"prompt\\n")\n'
        "line = s.recv(8)\n"
        'sys.exit(0 if line[:1] == b"\\x01" else 1)\n'
    )
    holder = subprocess.Popen([sys.executable, "-c", bad], env={**os.environ, **env})
    bridge.set_ssh_pid(holder.pid)
    try:
        assert holder.wait(timeout=10) == 0
    finally:
        bridge.stop()
    assert not got


@posix_only
def test_unrelated_process_cannot_ask(tmp_path: Path) -> None:
    got: list[str] = []

    def cb(prompt: str) -> str | None:
        got.append(prompt)
        return "x"

    bridge = SshAskpassBridge(cb, home=tmp_path / "home")
    bridge.start()
    env = bridge.env()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
    bridge.set_ssh_pid(sleeper.pid)
    try:
        with pytest.raises(AskpassRejected):
            request_secret(env[SOCKET_ENV], env[TOKEN_ENV], "rogue prompt")
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)
    legit_script = _ssh_child_script(
        'r=subprocess.run([os.environ["SSH_ASKPASS"], "me@box password:"], '
        "env=os.environ, check=False)\n"
        "sys.exit(r.returncode)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", legit_script],
        env={**os.environ, **env},
    )
    bridge.set_ssh_pid(holder.pid)
    try:
        assert holder.wait(timeout=10) == 0
        assert got == ["me@box password:"]
    finally:
        bridge.stop()


def test_prompt_sanitization_and_kind() -> None:
    dirty = "me\x00@box\u202e password:"
    clean = sanitize_ssh_prompt(dirty)
    assert "\x00" not in clean and "\u202e" not in clean
    assert len(sanitize_ssh_prompt("x" * 500)) <= 200
    assert classify_prompt_kind("Enter passphrase for key /home/me/.ssh/id_rsa:") == "passphrase"
    assert classify_prompt_kind("me@box's password:") == "password"


def test_secret_param_redacts_validation_errors() -> None:
    from pydantic import ValidationError

    from snowpea_core.server import rpc

    assert "secret" in rpc.SECRET_PARAMS
    with pytest.raises(ValidationError) as caught:
        MachineAnswerParams.model_validate(
            {"id": "m1", "askId": "a1", "answer": True, "secret": 123}
        )
    errors = rpc._without_secrets(caught.value, {"secret": 123})  # noqa: SLF001
    assert errors
    assert "input" not in errors[0]


def test_connect_defaults_non_interactive() -> None:
    import inspect

    sig = inspect.signature(MachineManager.connect)
    assert sig.parameters["interactive"].default is False
    sig_worker = inspect.signature(MachineManager._connect_worker)
    assert sig_worker.parameters["interactive"].default is False


def test_pending_ask_and_answer_hide_secrets() -> None:
    from snowpea_core.machines.manager import PendingAsk

    box = PendingAsk(kind="password", fields={})
    box.secret.append("hunter2")
    assert "hunter2" not in repr(box)
    params = MachineAnswerParams(
        id="m", askId="a", answer=True, secret="hunter2"
    )
    assert "hunter2" not in repr(params)


@posix_only
def test_bridge_decline_blocks_later_prompts(tmp_path: Path) -> None:
    bridge = SshAskpassBridge(lambda _p: None, home=tmp_path / "home")
    bridge.start()
    env = bridge.env()

    first = _ssh_child_script(
        'try:\n'
        "    from snowpea_core.machines.ssh_askpass import request_secret, AskpassRejected\n"
        f'    request_secret({env[SOCKET_ENV]!r}, {env[TOKEN_ENV]!r}, "first")\n'
        "except AskpassRejected:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)"
    )
    second = _ssh_child_script(
        'try:\n'
        "    from snowpea_core.machines.ssh_askpass import request_secret, AskpassRejected\n"
        f'    request_secret({env[SOCKET_ENV]!r}, {env[TOKEN_ENV]!r}, "second")\n'
        "except AskpassRejected:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", first],
        env={**os.environ, **env},
    )
    bridge.set_ssh_pid(holder.pid)
    try:
        assert holder.wait(timeout=10) == 0
        again = subprocess.Popen(
            [sys.executable, "-c", second],
            env={**os.environ, **env},
        )
        bridge.set_ssh_pid(again.pid)
        assert again.wait(timeout=10) == 0
    finally:
        bridge.stop()


@posix_only
def test_non_ascii_token_does_not_crash_bridge(tmp_path: Path) -> None:
    bridge = SshAskpassBridge(lambda _p: "ok", home=tmp_path / "home")
    bridge.start()
    env = bridge.env()
    script = _ssh_child_script(
        "import socket\n"
        f's=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
        f's.connect({env[SOCKET_ENV]!r})\n'
        's.sendall(b"\\xff\\xfe\\n")\n'
        's.sendall(b"prompt\\n")\n'
        "s.recv(32)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        env={**os.environ, **env},
    )
    bridge.set_ssh_pid(holder.pid)
    try:
        assert holder.wait(timeout=10) == 0
    finally:
        bridge.stop()


def test_socket_dir_cleaned_up(tmp_path: Path) -> None:
    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    path = bridge._dir  # noqa: SLF001
    assert path.is_dir()
    run_base = tmp_path / "home" / "run"
    assert run_base.is_dir()
    bridge.stop()
    assert not path.exists()
    assert bridge.env() == {}


@posix_only
def test_reused_ssh_pid_wrong_starttime_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_box: list[int] = []
    reused_root = 424_242
    phase = {"spawn": True}

    def fake_fields(pid: int) -> tuple[int, str] | None:
        if child_box and pid == child_box[0]:
            return (reused_root, "child-st")
        if pid == reused_root:
            return (1, "st-old") if phase["spawn"] else (1, "new-st")
        return ssh_askpass_mod._linux_proc_fields(pid)

    monkeypatch.setattr(ssh_askpass_mod, "_proc_fields_reader", fake_fields)
    bridge = SshAskpassBridge(lambda _p: "ok", home=tmp_path / "home")
    bridge.start()
    bridge.set_ssh_pid(reused_root)
    phase["spawn"] = False
    env = bridge.env()
    script = _ssh_child_script(
        "from snowpea_core.machines.ssh_askpass import request_secret, AskpassRejected\n"
        "try:\n"
        f'    request_secret({env[SOCKET_ENV]!r}, {env[TOKEN_ENV]!r}, "pw:")\n'
        "except AskpassRejected:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], env={**os.environ, **env})
    child_box.append(proc.pid)
    try:
        assert proc.wait(timeout=10) == 0
    finally:
        bridge.stop()


@posix_only
def test_ssh_root_cleared_after_ssh_run(tmp_path: Path) -> None:
    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    bridge.set_ssh_pid(os.getpid())
    assert bridge._ssh_root is not None  # noqa: SLF001
    bridge.finish_ssh_run()
    assert bridge._ssh_root is None  # noqa: SLF001
    bridge.stop()


@posix_only
def test_listener_closed_after_ssh_run(tmp_path: Path) -> None:
    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    assert bridge.listener_open()
    bridge.set_ssh_pid(os.getpid())
    bridge.finish_ssh_run()
    assert not bridge.listener_open()
    bridge.stop()


@posix_only
def test_bind_failure_leaves_bridge_able_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    bridge.finish_ssh_run()
    assert not bridge.listener_open()

    fail_once = {"left": 1}
    real_bind = socket.socket.bind

    def bind_once(self: socket.socket, *args: object) -> None:
        if fail_once["left"] > 0:
            fail_once["left"] -= 1
            raise OSError(98, "Address already in use")
        real_bind(self, *args)

    monkeypatch.setattr(socket.socket, "bind", bind_once)
    bridge.set_ssh_pid(os.getpid())
    assert not bridge.listener_open()
    bridge.set_ssh_pid(os.getpid())
    assert bridge.listener_open()
    bridge.stop()


@posix_only
def test_two_tracked_asks_with_finish_between(tmp_path: Path) -> None:
    got: list[str] = []

    def cb(prompt: str) -> str | None:
        got.append(prompt)
        return f"ans{len(got)}"

    bridge = SshAskpassBridge(cb, home=tmp_path / "home", max_prompts=2)
    bridge.start()
    legit = _ssh_child_script(
        'r=subprocess.run([os.environ["SSH_ASKPASS"], "me@box password:"], '
        "env=os.environ, check=False)\n"
        "sys.exit(r.returncode)"
    )
    for _ in range(2):
        env = bridge.env()
        holder = subprocess.Popen(
            [sys.executable, "-c", legit],
            env={**os.environ, **env},
        )
        bridge.set_ssh_pid(holder.pid)
        try:
            assert holder.wait(timeout=10) == 0
        finally:
            bridge.finish_ssh_run()
    assert got == ["me@box password:", "me@box password:"]
    bridge.stop()


@posix_only
def test_prepare_like_three_tracked_ssh_runs(tmp_path: Path) -> None:
    """Probe → install → probe style: finish between runs, password on the first."""
    phases: list[str] = []
    answered = {"n": 0}

    def cb(prompt: str) -> str | None:
        if answered["n"] > 0:
            return None
        answered["n"] += 1
        return "s3cret"

    bridge = SshAskpassBridge(cb, home=tmp_path / "home")
    bridge.start()
    runner = ssh_runner(
        None,
        bridge.env,
        on_spawn=bridge.set_ssh_pid,
        on_finish=bridge.finish_ssh_run,
    )
    probe_ok = _ssh_child_script(
        'r=subprocess.run([os.environ["SSH_ASKPASS"], "me@box password:"], '
        "env=os.environ, check=False)\n"
        "sys.exit(0 if r.returncode == 0 else 77)"
    )
    batch_only = _ssh_child_script("sys.exit(77)")
    scripts = [probe_ok, batch_only, batch_only]
    for script in scripts:
        phases.append("run")
        done = runner([sys.executable, "-c", script], None, True)
        assert done.returncode in (0, 77), done.stderr
    assert answered["n"] == 1
    assert len(phases) == 3
    bridge.stop()


@posix_only
def test_on_spawn_failure_kills_ssh_not_sshmissing(tmp_path: Path) -> None:
    pid_box: list[int] = []

    def boom(_pid: int) -> None:
        raise OSError(98, "Address already in use")

    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    runner = ssh_runner(
        None,
        bridge.env,
        on_spawn=boom,
        on_finish=bridge.finish_ssh_run,
    )
    child = _ssh_child_script(
        "import pathlib\n"
        f'pathlib.Path({str(tmp_path / "ssh-child.pid")!r}).write_text(str(os.getpid()))\n'
        "import time; time.sleep(120)"
    )
    with pytest.raises(RemoteError) as caught:
        runner([sys.executable, "-c", child], None, True)
    assert caught.value.reason != "sshMissing"
    assert "askpass" in str(caught.value).lower()
    pid_file = tmp_path / "ssh-child.pid"
    if pid_file.is_file():
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        pid_box.append(child_pid)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.kill(child_pid, 9)
            pytest.fail("ssh child survived on_spawn failure")
    bridge.stop()


@posix_only
def test_ask_slot_rechecks_declined(tmp_path: Path) -> None:
    class _GateLock:
        def __init__(self) -> None:
            self._inner = threading.Lock()
            self.at_pause = threading.Event()
            self.go = threading.Event()

        def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
            if timeout != -1:
                got = self._inner.acquire(blocking, timeout)
            elif blocking:
                got = self._inner.acquire(True)
            else:
                got = self._inner.acquire(False)
            if got:
                self.at_pause.set()
                self.go.wait(timeout=5)
            return got

        def release(self) -> None:
            self._inner.release()

    gate_lock = _GateLock()
    bridge = SshAskpassBridge(lambda _p: "ok", home=tmp_path / "home")
    bridge._ask_slot = gate_lock  # noqa: SLF001
    bridge.start()
    bridge.set_ssh_pid(os.getpid())
    env = bridge.env()
    script = _ssh_child_script(
        "from snowpea_core.machines.ssh_askpass import request_secret, AskpassRejected\n"
        "try:\n"
        f'    request_secret({env[SOCKET_ENV]!r}, {env[TOKEN_ENV]!r}, "hold")\n'
        "except AskpassRejected:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script], env={**os.environ, **env})
    try:
        assert gate_lock.at_pause.wait(timeout=5)
        bridge.note_declined()
        gate_lock.go.set()
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        proc.wait(timeout=5)
        bridge.stop()


def test_parse_proc_bsdinfo_fake_bytes() -> None:
    blob = bytearray(PROC_BSDINFO_STRUCT_SIZE)
    struct.pack_into("<I", blob, PROC_BSDINFO_PBI_PPID_OFF, 77)
    struct.pack_into("<Q", blob, PROC_BSDINFO_START_SEC_OFF, 1_700_000_000)
    struct.pack_into("<Q", blob, PROC_BSDINFO_START_SEC_OFF + 8, 42)
    parsed = parse_proc_bsdinfo(bytes(blob))
    assert parsed == (77, 1_700_000_000, 42)
    assert parse_proc_bsdinfo(b"short") is None
    # Offset 116 (old bug) must not match the start time stored at 120.
    struct.pack_into("<I", blob, 116, 0xCAFEBABE)
    legacy_start = struct.unpack_from("<Q", blob, 116)[0]
    assert legacy_start != 1_700_000_000


def test_parse_xucred_fake_bytes() -> None:
    good = struct.pack("II", 0, os.getuid())
    assert parse_xucred(good) == os.getuid()
    assert parse_xucred(struct.pack("II", 1, os.getuid())) is None
    assert parse_xucred(good[:4]) is None


@posix_only
def test_linux_so_peercred_missing_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("linux only")
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    sock_a, sock_b = socket.socketpair()
    try:
        assert ssh_askpass_mod._peer_pid_linux(sock_b) is None
    finally:
        sock_a.close()
        sock_b.close()


@posix_only
def test_run_dir_mode_0700(tmp_path: Path) -> None:
    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    run_base = tmp_path / "home" / "run"
    assert (run_base.stat().st_mode & 0o777) == 0o700
    bridge.stop()


@posix_only
def test_run_dir_fallback_uses_mkdtemp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_mkdtemp(*, prefix: str) -> str:
        seen.append(prefix)
        path = tmp_path / "fallback-ap"
        path.mkdir()
        return str(path)

    monkeypatch.setattr(ssh_askpass_mod.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(ssh_askpass_mod, "_SUN_PATH_MAX", 1)
    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    assert seen == ["sp-ap-"]
    bridge.stop()


@posix_only
def test_launcher_isolated_python(tmp_path: Path) -> None:
    import snowpea_core

    bridge = SshAskpassBridge(lambda _p: "x", home=tmp_path / "home")
    bridge.start()
    body = bridge._launcher.read_text(encoding="utf-8")  # noqa: SLF001
    pkg_root = str(Path(snowpea_core.__file__).resolve().parent.parent)
    assert "-E -s -P" in body
    assert "sys.path.append(" in body
    assert "sys.path.insert(0," not in body
    assert pkg_root in body
    assert "snowpea_core.machines.ssh_askpass" in body
    bridge.stop()


@pytest.mark.asyncio
async def test_machine_answer_hostkey_without_secret_field(tmp_path: Path) -> None:
    """Clients predating machinePassword omit ``secret`` on hostKey answers."""
    daemon = await make_daemon(tmp_path / "home")
    try:
        manager = MachineManager(daemon.core)
        manager.bind_loop(asyncio.get_running_loop())
        daemon.core.machines = manager
        from snowpea_core.machines.manager import LiveConnection, _Asks

        rec = manager.registry.add(
            "me@box",
            label="box",
            key_path=None,
            pasted_key=None,
            remote_cwd=None,
            auto_connect=False,
        )
        mid = rec.id
        live = LiveConnection()
        manager._live[mid] = live
        asks = _Asks(mid, manager._pending_asks[mid], live, manager._announce_sync)  # noqa: SLF001

        def run_ask() -> None:
            asks.ask("hostKey", host="box", fingerprint="ab:cd", keyType="ssh-ed25519")

        threading.Thread(target=run_ask).start()
        await asyncio.sleep(0.1)
        ident, _box = manager.current_ask(mid) or ("", None)
        assert ident
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            await client.ok(
                "machine.answer",
                {"id": mid, "askId": ident, "answer": True},
            )
            await client.stop()
    finally:
        await daemon.stop()