"""``snowpea --remote``: target parsing, the host probe, the install question, the tunnel flags."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import aiohttp
import pytest

from snowpea_core.cli import remote
from snowpea_core.cli.remote import RemoteError, RemoteSession, parse_target
from snowpea_core.server.protocol import PROTOCOL_VERSION

ADVERT = {"port": 43121, "token": "secret-token", "version": "0.2.80", "protocolVersion": "1.39.0"}


def probe_output(advert: dict[str, object] = ADVERT, cwd: str = "/home/me") -> str:
    return f"SNOWPEA_ADVERT {json.dumps(advert)}\nSNOWPEA_CWD {cwd}\n"


@pytest.mark.parametrize(
    ("text", "user", "host", "port"),
    [
        ("box", None, "box", None),
        ("me@box.example.com", "me", "box.example.com", None),
        ("me@10.0.0.5:2222", "me", "10.0.0.5", 2222),
        ("[::1]", None, "[::1]", None),
    ],
)
def test_a_target_is_user_host_port(
    text: str, user: str | None, host: str, port: int | None
) -> None:
    target = parse_target(text)
    assert (target.user, target.host, target.port) == (user, host, port)


@pytest.mark.parametrize(
    "text",
    ["", "-oProxyCommand=evil", "me@-host", "a b", "box;rm -rf", "box:0", "box:99999", "$(id)"],
)
def test_a_target_that_ssh_could_read_as_an_option_or_a_command_is_refused(text: str) -> None:
    with pytest.raises(RemoteError):
        parse_target(text)


def test_the_probe_output_gives_port_token_and_directory() -> None:
    advert = remote.parse_probe_output("login banner\n" + probe_output())
    assert (advert.port, advert.token, advert.cwd) == (43121, "secret-token", "/home/me")
    assert "secret-token" not in repr(advert)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "SNOWPEA_CWD /x\n",
        "SNOWPEA_ADVERT not-json\n",
        'SNOWPEA_ADVERT {"port": 0, "token": "t"}\n',
    ],
)
def test_a_probe_without_a_usable_advert_is_an_error(output: str) -> None:
    with pytest.raises(RemoteError):
        remote.parse_probe_output(output)


def test_a_different_major_protocol_is_refused_a_minor_one_is_a_warning() -> None:
    advert = remote.parse_probe_output(probe_output())
    assert remote.check_compatible(advert, "1.39.0") is None
    assert "differs" in (remote.check_compatible(advert, "1.40.0") or "")
    with pytest.raises(RemoteError, match="update"):
        remote.check_compatible(advert, "2.0.0")


class FakeSsh:
    """Records ssh calls; the probe script answers from ``answers`` in order."""

    def __init__(self, *answers: tuple[int, str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self, argv: Sequence[str], stdin: str | None, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), stdin))
        code, out = self.answers.pop(0) if self.answers else (0, "")
        return subprocess.CompletedProcess(list(argv), code, out, None)


def session(fake: FakeSsh, *, confirm: bool = False) -> tuple[RemoteSession, list[str]]:
    said: list[str] = []
    s = RemoteSession(
        parse_target("me@box:2200"),
        local_protocol=PROTOCOL_VERSION,
        confirm=lambda _q: confirm,
        ssh="ssh",
        run=fake,
        say=said.append,
    )
    return s, said


def test_prepare_reads_the_remote_daemon() -> None:
    fake = FakeSsh((0, probe_output({**ADVERT, "protocolVersion": PROTOCOL_VERSION})))
    s, _ = session(fake)
    try:
        advert = s.prepare()
    finally:
        s.close()
    assert advert.port == 43121
    argv, script = fake.calls[0]
    assert argv[0] == "ssh" and "-p" in argv and "2200" in argv
    assert argv[-3:] == ["me@box", "sh", "-s"]
    assert script is not None and "snowpea daemon start" in script


def test_a_missing_snowpea_is_installed_only_after_a_yes() -> None:
    fake = FakeSsh((remote.MISSING, "SNOWPEA_MISSING\n"))
    s, _ = session(fake, confirm=False)
    try:
        with pytest.raises(RemoteError, match="not installed"):
            s.prepare()
    finally:
        s.close()
    assert not any(
        "install.sh" in " ".join(argv) for argv, _ in fake.calls
    )  # nothing was installed

    ok = probe_output({**ADVERT, "protocolVersion": PROTOCOL_VERSION})
    fake = FakeSsh((remote.MISSING, "SNOWPEA_MISSING\n"), (0, ""), (0, ok))
    s, _ = session(fake, confirm=True)
    try:
        s.prepare()
    finally:
        s.close()
    install_argv, _ = fake.calls[1]
    assert "-t" in install_argv and "install.sh" in install_argv[-1]
    assert "SNOWPEA_SKIP_NODE=1" in install_argv[-1]


def test_a_daemon_that_will_not_start_is_reported_with_what_to_run() -> None:
    fake = FakeSsh((remote.START_FAILED, "SNOWPEA_START_FAILED\n"))
    s, _ = session(fake)
    try:
        with pytest.raises(RemoteError, match="snowpea daemon start"):
            s.prepare()
    finally:
        s.close()


def test_the_tui_is_pointed_at_the_tunnel_and_told_it_is_remote() -> None:
    fake = FakeSsh((0, probe_output({**ADVERT, "protocolVersion": PROTOCOL_VERSION})))
    s, _ = session(fake)
    try:
        s.prepare()
        s.local_port = 40555
        argv = remote.tui_arguments(s, None)
    finally:
        s.close()
    assert argv[:4] == ["--port", "40555", "--token", "secret-token"]
    assert argv[argv.index("--cwd") + 1] == "/home/me"  # the remote directory, not a local one
    assert argv[argv.index("--remote") + 1] == "me@box:2200"
    assert remote.tui_arguments(s, "/srv/app")[5] == "/srv/app"


# ---------------------------------------------------------------------------
# The whole path with a stand-in for ssh: the "remote" is this machine with
# its own HOME, so the daemon it starts is a throwaway one.
# ---------------------------------------------------------------------------

FAKE_SSH = '''\
import asyncio, os, subprocess, sys
argv = sys.argv[1:]
if "-O" in argv:
    sys.exit(0)
if "-L" in argv:
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
env = dict(os.environ, HOME=os.environ["FAKE_REMOTE_HOME"])
env.pop("SNOWPEA_HOME", None)
sys.exit(subprocess.run(["sh", "-s"], env=env).returncode)
'''


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh and ssh")
async def test_the_remote_flow_reaches_a_real_daemon_through_the_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "remote-home"
    (home / ".local" / "bin").mkdir(parents=True)
    snowpea = Path(sys.executable).parent / "snowpea"
    if not snowpea.exists():
        pytest.skip("the snowpea console script is not installed here")
    (home / ".local" / "bin" / "snowpea").symlink_to(snowpea)
    fake = tmp_path / "ssh"
    fake.write_text(f"#!{sys.executable}\n{FAKE_SSH}", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(Path(remote.__file__).parents[2]))

    session = RemoteSession(
        parse_target("me@box"),
        local_protocol=PROTOCOL_VERSION,
        confirm=lambda _q: False,
        ssh=str(fake),
        say=lambda _t: None,
    )
    try:
        advert = await asyncio.to_thread(session.prepare)
        assert advert.protocol == PROTOCOL_VERSION and advert.cwd
        port = await asyncio.to_thread(session.open_tunnel)
        assert port != advert.port  # the terminal talks to the tunnel, not to the daemon
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(f"http://127.0.0.1:{port}/ws")
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "system.hello",
                    "params": {
                        "token": advert.token,
                        "clientVersion": "test",
                        "protocolVersion": PROTOCOL_VERSION,
                    },
                }
            )
            reply = await asyncio.wait_for(ws.receive_json(), timeout=10)
            assert reply["result"]["serverVersion"] == advert.version
            await ws.close()
    finally:
        await asyncio.to_thread(session.close)
        subprocess.run(  # noqa: S603 - stops the throwaway daemon this test started
            [str(snowpea), "daemon", "stop"],
            env={**os.environ, "HOME": str(home), "SNOWPEA_HOME": str(home / ".snowpea")},
            check=False,
            timeout=30,
        )
