"""``snowpea remote up``: JSON lines, asks on stdin, error codes, reconnects, teardown.

Everything above the last section runs without ssh or Docker.  The end-to-end tests
stand in for ssh with a script that runs the probe on this machine under a throwaway
HOME (as ``test_cli_remote`` does) and for ``ssh-keyscan`` with another script; the
daemon behind them is a real one.
"""

from __future__ import annotations

import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_cli_sandbox import FakeDocker, FakePopen, options

from snowpea_core import __version__
from snowpea_core.cli import remote, remote_up, sandbox
from snowpea_core.cli.remote import RemoteError, RemoteSession, parse_target
from snowpea_core.cli.remote_up import (
    Asks,
    DockerConnection,
    Emitter,
    Hello,
    HostKeys,
    Ready,
    SshConnection,
    Stopped,
    run_up,
)
from snowpea_core.server.protocol import PROTOCOL_VERSION

TOKEN = "tok-secret-0123456789"
MACHINE = {"id": "m-1", "hostname": "box", "os": "linux", "container": False}
KEY_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIHmKbW0o2A1pQ4FoBvUQJnVIg7E8M2nR2PzJfWq0QUpN"
ED25519 = ("ssh-ed25519", KEY_B64)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh, ssh and signals")


class Sink(io.BytesIO):
    """A stdout that remembers what was written, line by line."""

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.getvalue().decode().splitlines()]


def emitter() -> tuple[Emitter, Sink]:
    sink = Sink()
    return Emitter(sink), sink


def events(sink: Sink) -> list[str]:
    return [line["event"] for line in sink.lines()]


# ---------------------------------------------------------------------------
# stdout and stdin
# ---------------------------------------------------------------------------


def test_every_event_is_one_json_line() -> None:
    out, sink = emitter()
    out.emit("reconnecting")
    out.emit("error", code="auth", message="no key — ü")
    raw = sink.getvalue().decode()
    assert raw.count("\n") == 2 and raw.endswith("\n")
    assert sink.lines() == [
        {"event": "reconnecting"},
        {"event": "error", "code": "auth", "message": "no key — ü"},
    ]
    assert " " not in raw.splitlines()[0]


def test_the_token_is_in_the_ready_line_and_scrubbed_from_everything_else() -> None:
    out, sink = emitter()
    out.secrets.add(TOKEN)
    out.emit("ready", port=1, token=TOKEN)
    out.emit("error", code="internal", message=f"failed with {TOKEN} in it")
    ready, error = sink.lines()
    assert ready["token"] == TOKEN
    assert TOKEN not in json.dumps(error) and "***" in error["message"]
    assert out.scrub(f"x {TOKEN} y") == "x *** y"


def test_a_broken_pipe_does_not_raise() -> None:
    class Dead(io.BytesIO):
        def write(self, _data: Any) -> int:
            raise BrokenPipeError

    out = Emitter(Dead())
    out.emit("closed")
    assert out.broken is True
    out.emit("closed")  # silently dropped now


def test_an_ask_waits_for_its_answer() -> None:
    out, sink = emitter()
    stop = threading.Event()
    asks = Asks(out, stop)
    result: queue.Queue[bool] = queue.Queue()
    thread = threading.Thread(target=lambda: result.put(asks.ask("install", version="9.9.9")))
    thread.start()
    deadline = time.monotonic() + 5
    while not sink.getvalue() and time.monotonic() < deadline:
        time.sleep(0.01)
    ask = sink.lines()[0]
    assert (ask["event"], ask["kind"], ask["version"]) == ("ask", "install", "9.9.9")
    asks.feed(json.dumps({"id": "someone-else", "answer": True}))  # not ours
    asks.feed("not json")
    asks.feed(json.dumps({"id": ask["id"], "answer": "yes"}))  # not a boolean
    assert result.empty()
    asks.feed(json.dumps({"id": ask["id"], "answer": True}))
    thread.join(5)
    assert result.get_nowait() is True


def test_an_answer_may_use_accept_and_a_no_is_a_no() -> None:
    out, sink = emitter()
    asks = Asks(out, threading.Event())
    result: queue.Queue[bool] = queue.Queue()
    thread = threading.Thread(
        target=lambda: result.put(asks.ask("hostKey", fingerprint="SHA256:x"))
    )
    thread.start()
    while not sink.getvalue():
        time.sleep(0.01)
    asks.feed(json.dumps({"t": "answer", "id": sink.lines()[0]["id"], "accept": False}))
    thread.join(5)
    assert result.get_nowait() is False


def test_the_end_of_stdin_ends_a_pending_ask_and_sets_stop() -> None:
    out, _sink = emitter()
    stop = threading.Event()
    asks = Asks(out, stop)
    raised: queue.Queue[BaseException] = queue.Queue()

    def waiting() -> None:
        try:
            asks.ask("install", version="1")
        except Stopped as exc:
            raised.put(exc)

    thread = threading.Thread(target=waiting)
    thread.start()
    remote_up.pump_stdin(io.BytesIO(b""), asks, stop).join(5)  # EOF at once
    assert stop.is_set()
    thread.join(5)
    assert isinstance(raised.get_nowait(), Stopped)


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "stderr", "expected"),
    [
        (255, "me@box: Permission denied (publickey,password).", "auth"),
        (255, "Host key verification failed.", "hostKey"),
        (255, "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", "hostKey"),
        (255, "ssh: Could not resolve hostname nope: Name or service not known", "unreachable"),
        (255, "ssh: connect to host 10.0.0.9 port 22: Connection refused", "unreachable"),
        (255, "ssh: connect to host 10.0.0.9 port 22: Connection timed out", "unreachable"),
        (255, "something odd", "unreachable"),
        (1, "something odd", "internal"),
        (7, None, "internal"),
    ],
)
def test_ssh_failures_map_to_the_error_codes(code: int, stderr: str | None, expected: str) -> None:
    assert remote.classify_ssh_failure(code, stderr) == expected


ADVERT = {
    "port": 43121,
    "token": TOKEN,
    "version": "0.2.82",
    "protocolVersion": PROTOCOL_VERSION,
}


def probe_ok(advert: dict[str, Any] = ADVERT) -> str:
    return f"SNOWPEA_ADVERT {json.dumps(advert)}\nSNOWPEA_CWD /home/me\n"


class FakeSsh:
    def __init__(self, *answers: tuple[int, str, str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: str | None, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), stdin))
        code, out, err = self.answers.pop(0) if self.answers else (0, "", "")
        return subprocess.CompletedProcess(list(argv), code, out, err)


def make_session(fake: FakeSsh, *, confirm: bool = False, **kwargs: Any) -> RemoteSession:
    return RemoteSession(
        parse_target("me@box"),
        local_protocol=PROTOCOL_VERSION,
        confirm=lambda _q: confirm,
        ssh="ssh",
        run=fake,
        say=lambda _t: None,
        batch=True,
        **kwargs,
    )


def prepare_code(fake: FakeSsh, *, confirm: bool = False) -> tuple[str, str | None]:
    session = make_session(fake, confirm=confirm)
    try:
        with pytest.raises(RemoteError) as caught:
            session.prepare()
    finally:
        session.close()
    return caught.value.code, caught.value.reason


def test_a_batch_session_never_prompts_and_pins_the_host_key_option() -> None:
    fake = FakeSsh((0, probe_ok(), ""))
    session = make_session(fake, options=["StrictHostKeyChecking=yes"])
    try:
        session.prepare()
    finally:
        session.close()
    argv = fake.calls[0][0]
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert "ConnectTimeout=15" in argv


def test_the_failure_paths_of_the_probe_have_codes() -> None:
    assert prepare_code(FakeSsh((255, "", "me@box: Permission denied (publickey)."))) == (
        "auth",
        None,
    )
    assert prepare_code(FakeSsh((255, "", "Host key verification failed."))) == ("hostKey", None)
    assert prepare_code(
        FakeSsh((255, "", "ssh: connect to host box port 22: No route to host"))
    ) == (
        "unreachable",
        None,
    )
    assert prepare_code(FakeSsh((remote.START_FAILED, "SNOWPEA_START_FAILED\n", ""))) == (
        "internal",
        "daemonFailed",
    )
    major = probe_ok({**ADVERT, "protocolVersion": "2.0.0"})
    assert prepare_code(FakeSsh((0, major, ""))) == ("versionMajor", None)


def test_declining_the_install_and_a_failed_install_have_codes() -> None:
    missing = (remote.MISSING, "SNOWPEA_MISSING\n", "")
    assert prepare_code(FakeSsh(missing), confirm=False) == ("installDeclined", None)
    assert prepare_code(FakeSsh(missing, (1, "", "curl: (6)")), confirm=True) == (
        "notInstalled",
        None,
    )
    # installed, but the second probe still cannot find it
    assert prepare_code(FakeSsh(missing, (0, "", ""), missing), confirm=True) == (
        "notInstalled",
        None,
    )


def test_a_batch_install_has_no_pty() -> None:
    fake = FakeSsh((remote.MISSING, "SNOWPEA_MISSING\n", ""), (0, "", ""), (0, probe_ok(), ""))
    session = make_session(fake, confirm=True)
    try:
        session.prepare()
    finally:
        session.close()
    install_argv = fake.calls[1][0]
    assert "-t" not in install_argv and "install.sh" in install_argv[-1]
    assert "BatchMode=yes" in install_argv


def test_a_missing_ssh_has_a_reason() -> None:
    session = RemoteSession(
        parse_target("box"), local_protocol=PROTOCOL_VERSION, confirm=lambda _q: False,
        ssh="", batch=True,
    )  # fmt: skip
    try:
        with pytest.raises(RemoteError) as caught:
            session.prepare()
    finally:
        session.close()
    assert (caught.value.code, caught.value.reason) == ("internal", "sshMissing")


# ---------------------------------------------------------------------------
# the host key
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_openssh_s_sha256_of_the_key_blob() -> None:
    # computed with `ssh-keygen -lf` on this key
    import base64
    import hashlib

    blob = base64.b64decode(KEY_B64)
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert remote_up.fingerprint(KEY_B64) == expected
    assert not expected.endswith("=")


def test_the_preferred_key_is_picked_from_a_scan() -> None:
    scan = (
        "# box:22 SSH-2.0-OpenSSH_9.6\n"
        f"box ssh-rsa AAAArsa\nbox ecdsa-sha2-nistp256 AAAAecdsa\nbox ssh-ed25519 {KEY_B64}\n"
    )
    key = remote_up.parse_scan(scan)
    assert key is not None and (key.key_type, key.key_b64) == ED25519
    rsa = remote_up.parse_scan("box ssh-rsa AAAArsa\n")
    assert rsa is not None and rsa.key_type == "ssh-rsa"
    assert remote_up.parse_scan("# nothing\n") is None


SSH_G = """\
user me
hostname box.example.com
port {port}
userknownhostsfile {known} ~/.ssh/known_hosts2
globalknownhostsfile /nonexistent/ssh_known_hosts
"""


class FakeTools:
    """``ssh -G``, ``ssh-keygen -F`` and ``ssh-keyscan`` answered in process."""

    def __init__(self, tmp: Path, *, port: int = 22, known: bool = False, scan: str = "") -> None:
        self.known_file = tmp / "known_hosts"
        self.port = port
        self.scan_output = scan or f"box.example.com ssh-ed25519 {KEY_B64}\n"
        self.calls: list[list[str]] = []
        self.extra_config = ""
        if known:
            self.known_file.write_text(f"{self.lookup()} ssh-ed25519 {KEY_B64}\n")

    def lookup(self) -> str:
        return "box.example.com" if self.port == 22 else f"[box.example.com]:{self.port}"

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        name = Path(argv[0]).name
        if "-G" in argv:
            text = SSH_G.format(port=self.port, known=self.known_file) + self.extra_config
            return subprocess.CompletedProcess(list(argv), 0, text, "")
        if name.startswith("ssh-keygen"):
            wanted = argv[argv.index("-F") + 1]
            hit = self.known_file.is_file() and any(
                line.split()[0] == wanted for line in self.known_file.read_text().splitlines()
            )
            return subprocess.CompletedProcess(
                list(argv), 0 if hit else 1, "found\n" if hit else "", ""
            )
        if name.startswith("ssh-keyscan"):
            return subprocess.CompletedProcess(list(argv), 0, self.scan_output, "")
        raise AssertionError(argv)


def host_keys(tools: FakeTools, answers: list[bool]) -> tuple[HostKeys, list[dict[str, Any]]]:
    out, sink = emitter()
    stop = threading.Event()
    asks = Asks(out, stop)
    asked: list[dict[str, Any]] = []

    def answer(kind: str, **fields: Any) -> bool:
        asked.append({"kind": kind, **fields})
        return answers.pop(0)

    asks.ask = answer  # type: ignore[method-assign]
    keys = HostKeys("/usr/bin/ssh", asks, execute=tools)
    keys._keygen, keys._keyscan = "/usr/bin/ssh-keygen", "/usr/bin/ssh-keyscan"  # noqa: SLF001
    return keys, asked


def test_a_known_host_is_checked_strictly_and_nothing_is_asked(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path, known=True)
    keys, asked = host_keys(tools, [])
    options = keys.ensure(parse_target("me@box"))
    assert options == ["StrictHostKeyChecking=yes"]
    assert asked == []
    assert not any("ssh-keyscan" in call[0] for call in tools.calls)  # no scan either


def test_an_unknown_host_is_asked_about_before_the_first_connect(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path)
    keys, asked = host_keys(tools, [True])
    options = keys.ensure(parse_target("me@box"))
    assert asked == [
        {
            "kind": "hostKey",
            "fingerprint": remote_up.fingerprint(KEY_B64),
            "keyType": "ssh-ed25519",
            "host": "me@box",
        }
    ]
    assert options == ["StrictHostKeyChecking=yes", "HostKeyAlgorithms=ssh-ed25519"]
    # a yes writes the key where ssh looks, so the real ssh with strict checking succeeds
    assert tools.known_file.read_text() == f"box.example.com ssh-ed25519 {KEY_B64}\n"
    if sys.platform != "win32":
        assert tools.known_file.stat().st_mode & 0o777 == 0o600
    again, asked_again = host_keys(tools, [])
    assert again.ensure(parse_target("me@box")) == ["StrictHostKeyChecking=yes"]
    assert asked_again == []


def test_a_no_leaves_known_hosts_alone_and_is_a_hostkey_error(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path)
    keys, _ = host_keys(tools, [False])
    with pytest.raises(RemoteError) as caught:
        keys.ensure(parse_target("me@box"))
    assert caught.value.code == "hostKey"
    assert not tools.known_file.exists()


def test_a_non_default_port_is_looked_up_and_stored_in_brackets(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path, port=2222)
    tools.known_file.write_text("box.example.com ssh-ed25519 OTHERKEY\n")  # port 22 only
    keys, asked = host_keys(tools, [True])
    keys.ensure(parse_target("me@box:2222"))
    assert len(asked) == 1
    lines = tools.known_file.read_text().splitlines()
    assert lines[-1] == f"[box.example.com]:2222 ssh-ed25519 {KEY_B64}"
    assert any("2222" in part for call in tools.calls for part in call if "-p" in call)


def test_an_unreadable_host_key_is_unreachable(tmp_path: Path) -> None:
    keys, asked = host_keys(FakeTools(tmp_path, scan="# nothing\n"), [])
    with pytest.raises(RemoteError) as caught:
        keys.ensure(parse_target("me@box"))
    assert caught.value.code == "unreachable" and asked == []


def test_a_proxied_host_cannot_be_scanned_and_is_left_to_strict_ssh(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path)
    tools.extra_config = "proxyjump jump.example.com\n"
    keys, asked = host_keys(tools, [])
    assert keys.ensure(parse_target("me@box")) == ["StrictHostKeyChecking=yes"]
    assert asked == []


def test_a_key_added_to_a_file_without_a_final_newline_starts_a_new_line(tmp_path: Path) -> None:
    tools = FakeTools(tmp_path)
    tools.known_file.write_text("other ssh-ed25519 AAAAother")  # no newline
    keys, _ = host_keys(tools, [True])
    keys.ensure(parse_target("me@box"))
    assert tools.known_file.read_text().splitlines()[0] == "other ssh-ed25519 AAAAother"


# ---------------------------------------------------------------------------
# supervision and the run
# ---------------------------------------------------------------------------


class FakeConn:
    """Stands in for SshConnection/DockerConnection."""

    def __init__(self, ready: list[Ready] | None = None) -> None:
        self.readies = ready or []
        self.down = False
        self.health = True
        self.closed = False
        self.started = 0
        self.recoveries: list[Exception | Ready] = []
        self.on_recover: Callable[[], None] = lambda: None

    def start(self) -> Ready:
        self.started += 1
        return Ready(1111, TOKEN, "0.2.82", PROTOCOL_VERSION, MACHINE, "/work")

    def tunnel_down(self) -> bool:
        return self.down

    def healthy(self) -> bool:
        return self.health

    def recover(self) -> Ready:
        self.on_recover()
        item = self.recoveries.pop(0)
        if isinstance(item, Exception):
            raise item
        self.down = False
        self.health = True
        return item

    def close(self) -> None:
        self.closed = True


def new_ready(port: int, token: str = "tok-2") -> Ready:
    return Ready(port, token, "0.2.82", PROTOCOL_VERSION, MACHINE, "/work")


FAST = {"health_every": 0.01, "backoff_cap": 0.01, "poll": 0.01}


def test_run_up_announces_ready_and_closes_on_stop() -> None:
    out, sink = emitter()
    conn = FakeConn()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    assert run_up(conn, out, stop, **FAST) == 0  # type: ignore[arg-type]
    assert events(sink) == ["ready", "closed"]
    ready = sink.lines()[0]
    assert ready == {
        "event": "ready",
        "port": 1111,
        "token": TOKEN,
        "version": "0.2.82",
        "protocol": PROTOCOL_VERSION,
        "machine": MACHINE,
        "cwd": "/work",
    }
    assert conn.closed is True


def test_a_dropped_tunnel_is_reconnecting_then_ready_with_the_new_endpoint() -> None:
    out, sink = emitter()
    conn = FakeConn()
    conn.recoveries = [RemoteError("down", code="unreachable"), new_ready(2222, "tok-rotated")]
    stop = threading.Event()

    def drop() -> None:
        time.sleep(0.1)
        conn.down = True

    threading.Thread(target=drop).start()
    threading.Timer(1.5, stop.set).start()
    assert run_up(conn, out, stop, **FAST) == 0  # type: ignore[arg-type]
    assert events(sink) == ["ready", "reconnecting", "ready", "closed"]
    first, _, second, _ = sink.lines()
    assert (second["port"], second["token"]) == (2222, "tok-rotated")
    assert first["token"] == TOKEN


def test_a_daemon_that_stops_answering_is_a_reconnect_too() -> None:
    out, sink = emitter()
    conn = FakeConn()
    conn.recoveries = [new_ready(3333)]
    conn.health = False
    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    run_up(conn, out, stop, **FAST)  # type: ignore[arg-type]
    assert events(sink) == ["ready", "reconnecting", "ready", "closed"]


def test_an_error_that_a_retry_cannot_fix_ends_the_run_with_an_error_event() -> None:
    out, sink = emitter()
    conn = FakeConn()
    conn.down = True
    conn.recoveries = [RemoteError("no key", code="auth")]
    code = run_up(conn, out, threading.Event(), **FAST)  # type: ignore[arg-type]
    assert code == 1
    assert events(sink) == ["ready", "reconnecting", "error", "closed"]
    assert sink.lines()[2] == {"event": "error", "code": "auth", "message": "no key"}
    assert conn.closed


def test_a_connection_that_stays_down_is_given_up() -> None:
    out, sink = emitter()
    conn = FakeConn()
    conn.down = True
    conn.recoveries = [RemoteError("still down", code="unreachable") for _ in range(100)]
    code = run_up(conn, out, threading.Event(), give_up_sec=0.05, **FAST)  # type: ignore[arg-type]
    assert code == 1
    error = next(line for line in sink.lines() if line["event"] == "error")
    assert error["code"] == "unreachable" and "could not reconnect" in error["message"]
    assert events(sink)[-1] == "closed"


def test_start_failures_are_error_events_with_code_and_reason() -> None:
    class Failing(FakeConn):
        def start(self) -> Ready:
            raise RemoteError(
                "docker is not installed", code="dockerMissing", reason="notInstalled"
            )

    out, sink = emitter()
    assert run_up(Failing(), out, threading.Event()) == 1  # type: ignore[arg-type]
    assert sink.lines() == [
        {"event": "error", "code": "dockerMissing", "message": "docker is not installed",
         "reason": "notInstalled"},
        {"event": "closed"},
    ]  # fmt: skip


def test_an_unexpected_exception_is_internal_and_leaks_no_token() -> None:
    class Boom(FakeConn):
        def start(self) -> Ready:
            raise ValueError(f"bad thing with {TOKEN}")

    out, sink = emitter()
    out.secrets.add(TOKEN)
    assert run_up(Boom(), out, threading.Event()) == 1  # type: ignore[arg-type]
    error = sink.lines()[0]
    assert error["code"] == "internal" and TOKEN not in json.dumps(sink.lines())


def test_stdin_ending_during_an_ask_is_a_clean_close() -> None:
    class Asking(FakeConn):
        def start(self) -> Ready:
            raise Stopped

    out, sink = emitter()
    assert run_up(Asking(), out, threading.Event()) == 0  # type: ignore[arg-type]
    assert events(sink) == ["closed"]


# ---------------------------------------------------------------------------
# SshConnection with the pieces faked
# ---------------------------------------------------------------------------


def test_the_install_ask_names_the_version(tmp_path: Path) -> None:
    out, sink = emitter()
    asks = Asks(out, threading.Event())
    asked: list[tuple[str, dict[str, Any]]] = []
    asks.ask = lambda kind, **f: asked.append((kind, f)) or True  # type: ignore[method-assign, assignment, return-value]
    conn = SshConnection(parse_target("me@box"), cwd=None, hold_sec=300, emitter=out, asks=asks)
    assert conn._confirm_install("anything") is True  # noqa: SLF001
    assert asked == [("install", {"version": __version__, "target": "me@box"})]


def test_the_connection_ready_uses_the_daemons_own_hello(tmp_path: Path) -> None:
    out, _ = emitter()
    seen: dict[str, Any] = {}

    def hello(port: int, token: str, *, hold_sec: int) -> Hello:
        seen.update(port=port, token=token, hold=hold_sec)
        return Hello(version="0.2.99", protocol="1.40.1", machine=MACHINE)

    conn = SshConnection(
        parse_target("me@box"), cwd="/srv/app", hold_sec=120, emitter=out,
        asks=Asks(out, threading.Event()), hello=hello,
    )  # fmt: skip
    advert = remote.parse_probe_output(probe_ok())
    ready = conn._ready(5555, advert)  # noqa: SLF001
    assert seen == {"port": 5555, "token": TOKEN, "hold": 120}
    assert (ready.version, ready.protocol, ready.machine, ready.cwd) == (
        "0.2.99", "1.40.1", MACHINE, "/srv/app",
    )  # fmt: skip


def test_a_daemon_older_than_1_40_has_no_machine(tmp_path: Path) -> None:
    out, _ = emitter()

    def hello(*_a: Any, **_k: Any) -> Hello:
        raise RemoteError("the daemon refused system.hello")

    conn = SshConnection(
        parse_target("me@box"), cwd=None, hold_sec=300, emitter=out,
        asks=Asks(out, threading.Event()), hello=hello,
    )  # fmt: skip
    ready = conn._ready(5555, remote.parse_probe_output(probe_ok()))  # noqa: SLF001
    assert ready.machine is None and ready.version == "0.2.82" and ready.cwd == "/home/me"


# ---------------------------------------------------------------------------
# docker with the pieces faked
# ---------------------------------------------------------------------------


def test_docker_ready_carries_the_container_token_and_published_port(tmp_path: Path) -> None:
    out, sink = emitter()
    fake, popen = FakeDocker(have_image=True), FakePopen()
    seen: dict[str, Any] = {}

    def hello(port: int, token: str, *, hold_sec: int) -> Hello:
        seen.update(port=port, token=token)
        return Hello(
            version=__version__, protocol=PROTOCOL_VERSION, machine={**MACHINE, "container": True}
        )

    conn = DockerConnection(
        options(tmp_path),
        hold_sec=300,
        emitter=out,
        run=fake,
        popen=popen,
        docker="docker",
        hello=hello,
    )
    conn.box._probe = lambda _port: True  # noqa: SLF001
    try:
        ready = conn.start()
    finally:
        conn.close()
    argv = popen.argvs[0]
    assert ready.port == int(argv[argv.index("--publish") + 1].split(":")[1])
    assert ready.cwd == str(conn.box.options.cwd) and ready.machine["container"] is True  # type: ignore[index]
    assert seen["token"] == ready.token and ready.token in out.secrets
    assert all(ready.token not in part for part in argv)  # not in docker's argv
    # the container is gone afterwards
    assert "rm" in fake.verbs()
    assert sink.getvalue() == b""  # start() itself prints nothing on stdout


def test_a_failed_docker_start_has_a_dockermissing_or_dockerbuild_code(tmp_path: Path) -> None:
    out, _ = emitter()

    class NoDocker(FakeDocker):
        def __call__(self, argv: Sequence[str], capture: bool) -> subprocess.CompletedProcess[str]:
            if argv[1] == "info":
                return subprocess.CompletedProcess(
                    [], 1, "Cannot connect to the Docker daemon", None
                )
            return super().__call__(argv, capture)

    conn = DockerConnection(
        options(tmp_path), hold_sec=0, emitter=out, run=NoDocker(), docker="docker",
        popen=FakePopen(),
    )  # fmt: skip
    with pytest.raises(RemoteError) as caught:
        conn.start()
    assert (caught.value.code, caught.value.reason) == ("dockerMissing", "notRunning")

    conn = DockerConnection(
        options(tmp_path), hold_sec=0, emitter=out, run=FakeDocker(build_code=1), docker="docker",
        popen=FakePopen(),
    )  # fmt: skip
    with pytest.raises(RemoteError) as caught:
        conn.start()
    assert caught.value.code == "dockerBuild"

    no_binary = DockerConnection(options(tmp_path), hold_sec=0, emitter=out, docker="")
    with pytest.raises(RemoteError) as caught:
        no_binary.start()
    assert (caught.value.code, caught.value.reason) == ("dockerMissing", "notInstalled")


def test_a_container_that_stops_is_a_fatal_error_not_a_silent_restart(tmp_path: Path) -> None:
    out, _ = emitter()
    conn = DockerConnection(
        options(tmp_path), hold_sec=0, emitter=out, run=FakeDocker(have_image=True),
        popen=FakePopen(), docker="docker",
    )  # fmt: skip
    with pytest.raises(RemoteError) as caught:
        conn.recover()
    assert caught.value.reason == "containerExited"


def test_the_docker_build_output_never_reaches_stdout() -> None:
    run = remote_up.docker_runner()
    done = run([sys.executable, "-c", "print('building...')"], False)
    assert done.returncode == 0  # fd 2 got the text; this process's stdout is the test's


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


def run_main(argv: list[str]) -> tuple[int, Sink]:
    sink = Sink()
    code = remote_up.main(argv, stdin=io.BytesIO(b""), stdout=sink, install_signals=False)
    return code, sink


def test_without_json_it_is_a_usage_error_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    code, sink = run_main(["me@box"])
    assert code == 2 and sink.getvalue() == b""
    assert "--json" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--json"],
        ["--json", "me@box", "--docker-keep"],
        ["--json", "me@box", "--docker", "--cwd", "/tmp"],
        ["--json", "-oProxyCommand=x"],
        ["--json", "--bogus"],
    ],
)
def test_bad_arguments_are_an_error_event_and_exit_2(argv: list[str]) -> None:
    code, sink = run_main(argv)
    assert code == 2
    lines = sink.lines()
    assert lines[0]["event"] == "error" and lines[-1] == {"event": "closed"}


def test_docker_arguments_parse_in_the_documented_shapes() -> None:
    parser = remote_up.build_parser()
    args = parser.parse_args(
        ["--docker", "--docker-volume", "v1", "--docker-keep", "--cwd", "/p", "--json"]
    )
    assert (args.docker, args.docker_volume, args.docker_keep, args.cwd) == ("", "v1", True, "/p")
    assert parser.parse_args(["--docker", "img:1", "--json"]).docker == "img:1"
    ssh = parser.parse_args(["me@box:2200", "--cwd", "/srv", "--json"])
    assert (ssh.target, ssh.docker, ssh.cwd) == ("me@box:2200", None, "/srv")


def test_the_command_is_routed_from_both_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core import __main__ as core_main
    from snowpea_core.cli import main as cli_main

    seen: list[list[str]] = []

    def fake_main(argv: Sequence[str] | None = None, **_k: Any) -> int:
        seen.append(list(argv or []))
        return 7

    monkeypatch.setattr(remote_up, "main", fake_main)
    assert core_main.main(["remote", "up", "me@box", "--json"]) == 7
    assert cli_main.main(["remote", "up", "--docker", "--json"]) == 7
    assert seen == [["me@box", "--json"], ["--docker", "--json"]]


# ---------------------------------------------------------------------------
# end to end: a real daemon behind a stand-in ssh
# ---------------------------------------------------------------------------

FAKE_SSH = """\
import asyncio, os, subprocess, sys
argv = sys.argv[1:]
if "-G" in argv:
    print("user me")
    print("hostname box.example.com")
    print("port 22")
    print("userknownhostsfile " + os.environ["FAKE_KNOWN_HOSTS"])
    print("globalknownhostsfile /nonexistent/ssh_known_hosts")
    sys.exit(0)
if "-O" in argv:
    sys.exit(0)
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a") as log:
        log.write(" ".join(argv) + "\\n")
if "-L" in argv:
    # the host key must have been pinned before any real connection
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
env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"])
env.pop("SNOWPEA_HOME", None)
sys.exit(subprocess.run(["sh", "-s"], env=env).returncode)
"""

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


class UpProcess:
    """A running ``python -m snowpea_core remote up`` with its stdout read on a thread."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(  # noqa: S603 - this checkout's interpreter
            [sys.executable, "-m", "snowpea_core", "remote", "up", *argv, "--json"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.lines: queue.Queue[dict[str, Any]] = queue.Queue()
        self.seen: list[dict[str, Any]] = []
        self.stderr = b""
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = json.loads(raw)  # every stdout line is one JSON object, or this raises
            self.seen.append(line)
            self.lines.put(line)

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        self.stderr = self.proc.stderr.read()

    def next(self, timeout: float = 60) -> dict[str, Any]:
        return self.lines.get(timeout=timeout)

    def send(self, **message: Any) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def finish(self, timeout: float = 60) -> int:
        code = self.proc.wait(timeout)
        time.sleep(0.2)  # let the reader threads drain
        return code

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A PATH with fake ssh/ssh-keyscan/ssh-keygen, and a throwaway remote HOME."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("ssh", FAKE_SSH),
        ("ssh-keyscan", FAKE_KEYSCAN),
        ("ssh-keygen", FAKE_KEYGEN),
    ):
        script = bin_dir / name
        script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        script.chmod(0o755)
    home = tmp_path / "remote-home"
    (home / ".local" / "bin").mkdir(parents=True)
    snowpea = Path(sys.executable).parent / "snowpea"
    if not snowpea.exists():
        pytest.skip("the snowpea console script is not installed here")
    (home / ".local" / "bin" / "snowpea").symlink_to(snowpea)
    known = tmp_path / "known_hosts"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_REMOTE_HOME": str(home),
        "FAKE_KNOWN_HOSTS": str(known),
        "FAKE_TUNNEL_PID": str(tmp_path / "tunnel.pid"),
        "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
        "PYTHONPATH": str(Path(remote.__file__).parents[2]),
        "SNOWPEA_UPDATE_CHECK": "0",
    }
    env.pop("SNOWPEA_HOME", None)
    # the local side must not read the developer's home either
    env["HOME"] = str(tmp_path / "local-home")
    (tmp_path / "local-home").mkdir()
    remote_env = {**os.environ, "HOME": str(home), "SNOWPEA_HOME": str(home / ".snowpea")}
    yield {"env": env, "known": known, "home": home, "tmp": tmp_path, "remote_env": remote_env,
           "snowpea": snowpea}  # fmt: skip
    subprocess.run(  # noqa: S603 - stops the throwaway daemon the test started
        [str(snowpea), "daemon", "stop"], env=remote_env, check=False, timeout=60,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


def hello_over(port: int, token: str) -> dict[str, Any]:
    h = remote_up.daemon_hello(port, token, hold_sec=0)
    return {"machine": h.machine, "protocol": h.protocol, "version": h.version}


def info_over(port: int, token: str) -> dict[str, Any]:
    import asyncio

    import aiohttp

    async def go() -> dict[str, Any]:
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws,
        ):
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "system.hello",
                    "params": {
                        "token": token,
                        "clientVersion": "t",
                        "protocolVersion": PROTOCOL_VERSION,
                    },
                }
            )
            await ws.receive_json()
            await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "system.info", "params": {}})
            return dict((await ws.receive_json())["result"])

    return asyncio.run(go())


@posix_only
def test_end_to_end_ask_ready_reconnect_and_teardown(stand_in: dict[str, Any]) -> None:
    env, known, tmp = stand_in["env"], stand_in["known"], stand_in["tmp"]
    up = UpProcess(["me@box", "--cwd", "/srv/app"], env)
    try:
        # 1. an unknown host: the question comes before any real ssh call
        ask = up.next()
        assert (ask["event"], ask["kind"]) == ("ask", "hostKey")
        assert ask["keyType"] == "ssh-ed25519"
        assert ask["fingerprint"] == remote_up.fingerprint(KEY_B64)
        assert not (tmp / "ssh.log").exists() or "sh -s" not in (tmp / "ssh.log").read_text()
        assert not known.exists()
        up.send(id=ask["id"], answer=True)

        # 2. ready: the endpoint, the token, and the machine as the daemon reports it
        ready = up.next()
        assert ready["event"] == "ready", ready
        assert ready["protocol"] == PROTOCOL_VERSION and ready["version"] == __version__
        assert set(ready["machine"]) == {"id", "hostname", "os", "container"}
        assert ready["cwd"] == "/srv/app"
        assert len(ready["token"]) >= 16
        assert known.read_text() == f"box.example.com ssh-ed25519 {KEY_B64}\n"
        assert hello_over(ready["port"], ready["token"])["machine"] == ready["machine"]

        # the daemon is held: once the connector goes it exits after holdSec, not 30 minutes
        lifecycle = info_over(ready["port"], ready["token"])["lifecycle"]
        assert lifecycle["willExit"] is True and lifecycle["secondsUntilExit"] <= 300

        # 3. the tunnel drops: reconnecting, then ready again on the same daemon
        pid = int((tmp / "tunnel.pid").read_text())
        os.kill(pid, signal.SIGKILL)
        assert up.next()["event"] == "reconnecting"
        again = up.next()
        assert again["event"] == "ready" and again["token"] == ready["token"]
        assert hello_over(again["port"], again["token"])["version"] == __version__

        # 4. the remote daemon restarts with a new token: ready carries it
        subprocess.run(  # noqa: S603
            [str(stand_in["snowpea"]), "daemon", "stop"], env=stand_in["remote_env"],
            check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        (stand_in["home"] / ".snowpea" / "token").unlink()  # a reinstall: the token is new
        os.kill(int((tmp / "tunnel.pid").read_text()), signal.SIGKILL)
        assert up.next()["event"] == "reconnecting"
        rotated = up.next(timeout=90)
        assert rotated["event"] == "ready" and rotated["token"] != ready["token"]
        assert hello_over(rotated["port"], rotated["token"])["protocol"] == PROTOCOL_VERSION

        # 5. end of stdin: closed, exit 0, the ssh control directory is gone
        up.close_stdin()
        assert up.next()["event"] == "closed"
        assert up.finish() == 0
    finally:
        up.kill()
    # nothing but JSON on stdout (UpProcess parsed every line), and the token only in ready
    for line in up.seen:
        if line["event"] != "ready":
            assert rotated["token"] not in json.dumps(line)
    assert ready["token"].encode() not in up.stderr
    assert rotated["token"].encode() not in up.stderr
    assert [e["event"] for e in up.seen].count("ready") == 3
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=False).stdout  # noqa: S607
    assert ready["token"] not in ps and rotated["token"] not in ps


@posix_only
def test_end_to_end_a_known_host_is_not_asked_and_sigterm_closes(stand_in: dict[str, Any]) -> None:
    stand_in["known"].write_text(f"box.example.com ssh-ed25519 {KEY_B64}\n")
    up = UpProcess(["me@box"], stand_in["env"])
    try:
        ready = up.next()
        assert ready["event"] == "ready", ready  # no ask
        up.proc.send_signal(signal.SIGTERM)
        assert up.next()["event"] == "closed"
        assert up.finish() == 0
    finally:
        up.kill()
    assert [e["event"] for e in up.seen] == ["ready", "closed"]


@posix_only
def test_end_to_end_a_refused_host_key_is_an_error_and_nothing_is_pinned(
    stand_in: dict[str, Any],
) -> None:
    up = UpProcess(["me@box"], stand_in["env"])
    try:
        ask = up.next()
        up.send(id=ask["id"], answer=False)
        error = up.next()
        assert (error["event"], error["code"]) == ("error", "hostKey")
        assert up.next()["event"] == "closed"
        assert up.finish() == 1
    finally:
        up.kill()
    assert not stand_in["known"].exists()


@posix_only
def test_end_to_end_an_auth_failure_is_code_auth(stand_in: dict[str, Any]) -> None:
    stand_in["known"].write_text(f"box.example.com ssh-ed25519 {KEY_B64}\n")
    up = UpProcess(["me@box"], {**stand_in["env"], "FAKE_SSH_DENY": "1"})
    try:
        error = up.next()
        assert (error["event"], error["code"]) == ("error", "auth")
        assert up.next()["event"] == "closed"
        assert up.finish() == 1
    finally:
        up.kill()


@posix_only
def test_end_to_end_stdin_closing_at_an_ask_is_a_clean_exit(stand_in: dict[str, Any]) -> None:
    up = UpProcess(["me@box"], stand_in["env"])
    try:
        assert up.next()["kind"] == "hostKey"
        up.close_stdin()
        assert up.next()["event"] == "closed"
        assert up.finish() == 0
    finally:
        up.kill()
    assert not stand_in["known"].exists()


# ---------------------------------------------------------------------------
# a real container, opt in: it builds the image from a wheel of this checkout
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("SNOWPEA_TEST_REMOTE_UP_DOCKER"),
    reason="set SNOWPEA_TEST_REMOTE_UP_DOCKER=1 (builds the sandbox image; minutes)",
)
@pytest.mark.timeout(1800)
def test_a_real_container_through_remote_up(tmp_path: Path) -> None:
    from test_cli_sandbox import build_wheel, docker_usable

    if not docker_usable():
        pytest.skip("needs a working docker daemon")
    wheel = build_wheel(tmp_path / "wheel")
    if wheel is None:
        pytest.skip("could not build a wheel of this checkout")
    project = tmp_path / "project"
    project.mkdir()
    env = {**os.environ, sandbox.SOURCE_ENV: str(wheel), "HOME": str(tmp_path / "h"),
           "SNOWPEA_HOME": str(tmp_path / "h" / ".snowpea")}  # fmt: skip
    (tmp_path / "h").mkdir()
    up = UpProcess(["--docker", "--cwd", str(project)], env)
    try:
        ready = up.next(timeout=1500)
        assert ready["event"] == "ready", ready
        assert ready["machine"]["container"] is True
        assert ready["cwd"] == str(project)
        up.close_stdin()
        assert up.next()["event"] == "closed"
        assert up.finish(timeout=120) == 0
    finally:
        up.kill()
