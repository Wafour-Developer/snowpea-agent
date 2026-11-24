# ruff: noqa: E501
"""M37 T5: ``snowpea --session`` and ``snowpea term ...`` against a fake daemon."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.cli import main as cli_main
from snowpea_core.cli import term
from snowpea_core.cli.daemon_client import RpcCallError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX PTY")


class FakeClient:
    """Records calls; ``frames`` are the notifications the daemon sends."""

    def __init__(self, replies: dict[str, Any] | None = None, frames: list[dict[str, Any]] | None = None) -> None:
        self.replies = replies or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.frames = frames or []
        self.release = asyncio.Event()

    async def call(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        reply = self.replies.get(method, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def notifications(self) -> Any:
        for frame in self.frames:
            yield frame
        await self.release.wait()

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


ROW = {
    "terminalId": "t-1",
    "profile": "claude",
    "title": "work",
    "cwd": "/w",
    "status": {"state": "blocked", "kind": "permission"},
    "alive": True,
}


def test_ls_prints_rows_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient({"terminal.list": {"terminals": [ROW]}})
    assert asyncio.run(term.term_ls(client, all_=False, as_json=False)) == 0
    out = capsys.readouterr().out
    assert "t-1" in out and "blocked/permission" in out
    assert client.calls[0] == ("terminal.list", {"aliveOnly": True})
    assert asyncio.run(term.term_ls(client, all_=True, as_json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["terminalId"] == "t-1"
    assert client.calls[1] == ("terminal.list", {})


def test_ls_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert asyncio.run(term.term_ls(FakeClient(), all_=False, as_json=False)) == 0
    assert "no terminals" in capsys.readouterr().out


def test_read_and_send(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient({"terminal.read": {"text": "hello screen"}, "terminal.send": {"sent": True, "accepted": True}})
    assert asyncio.run(term.term_read(client, "t-1", lines=5, as_json=False)) == 0
    assert "hello screen" in capsys.readouterr().out
    assert client.calls[0] == ("terminal.read", {"terminalId": "t-1", "lines": 5})
    assert asyncio.run(term.term_send(client, "t-1", "run tests", submit=True, force=False)) == 0
    assert client.calls[1] == (
        "terminal.send",
        {"terminalId": "t-1", "text": "run tests", "submit": True, "force": False},
    )


def test_send_refused_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient({"terminal.send": {"sent": False, "reason": "blocked"}})
    assert asyncio.run(term.term_send(client, "t-1", "x", submit=True, force=False)) == 2
    assert "blocked" in capsys.readouterr().err


def test_rpc_error_is_reported(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient({"terminal.list": RpcCallError("not_found", "no such terminal")})

    async def go() -> int:
        async def work(c: Any) -> int:
            return await term.term_ls(c, all_=False, as_json=False)

        # Same error mapping as _with_client, without a daemon.
        try:
            return await work(client)
        except RpcCallError as exc:
            return term._fail(f"{exc.code}: {exc.message}", term.EXIT_USAGE)

    assert asyncio.run(go()) == 2
    assert "not_found" in capsys.readouterr().err


def run_attach(
    client: FakeClient,
    typed: list[bytes],
    *,
    observe: bool = False,
    stop: Callable[[FakeClient], bool] | None = None,
) -> tuple[tuple[str, int | None], bytes, list[Callable[[], None]]]:
    out = bytearray()
    winch: list[Callable[[], None]] = []
    inputs = list(typed)
    reader: list[Callable[[], None]] = []

    async def go() -> tuple[str, int | None]:
        def add_input_reader(cb: Callable[[], None]) -> Callable[[], None]:
            reader.append(cb)
            return lambda: None

        def on_winch(cb: Callable[[], None]) -> Callable[[], None]:
            winch.append(cb)
            return lambda: None

        task = asyncio.ensure_future(
            term.attach_session(
                client,
                "t-1",
                observe=observe,
                write_out=out.extend,
                read_input=lambda: inputs.pop(0) if inputs else b"",
                add_input_reader=add_input_reader,
                size=lambda: (100, 30),
                on_winch=on_winch,
            )
        )
        await asyncio.sleep(0.05)
        for _ in typed:
            reader[0]()
            await asyncio.sleep(0.02)
        if stop is None or stop(client):
            pass
        return await asyncio.wait_for(task, 3)

    return asyncio.run(go()), bytes(out), winch


ATTACH = {"terminal.attach": {"repaint": b64(b"SCREEN"), "seq": 1, "cols": 100, "rows": 30}}


def test_attach_repaints_streams_and_sends_human_input() -> None:
    client = FakeClient(
        dict(ATTACH),
        frames=[
            {"method": "terminal.data", "params": {"terminalId": "t-1", "seq": 2, "data": b64(b"out")}},
            {"method": "terminal.data", "params": {"terminalId": "other", "seq": 1, "data": b64(b"NO")}},
        ],
    )
    (reason, _), out, _ = run_attach(client, [b"ls\r", b"\x1c"])
    assert reason == "detached"
    assert out == b"SCREENout"
    attach_call = client.calls[0]
    assert attach_call == ("terminal.attach", {"terminalId": "t-1", "mode": "interactive", "cols": 100, "rows": 30})
    writes = [p for m, p in client.calls if m == "terminal.write"]
    assert writes == [{"terminalId": "t-1", "data": b64(b"ls\r"), "human": True}]
    assert client.methods()[-1] == "terminal.detach"


def test_attach_detach_key_inside_a_chunk_sends_only_what_came_before() -> None:
    client = FakeClient(dict(ATTACH))
    (reason, _), _, _ = run_attach(client, [b"ab\x1ccd"])
    assert reason == "detached"
    writes = [p["data"] for m, p in client.calls if m == "terminal.write"]
    assert writes == [b64(b"ab")]


def test_attach_ends_when_the_program_exits() -> None:
    client = FakeClient(
        dict(ATTACH),
        frames=[{"method": "terminal.exit", "params": {"terminalId": "t-1", "exitCode": 3}}],
    )
    (reason, code), _, _ = run_attach(client, [])
    assert (reason, code) == ("exited", 3)
    assert client.methods()[-1] == "terminal.detach"


def test_attach_resizes_on_winch_but_observe_never_writes_or_resizes() -> None:
    client = FakeClient(dict(ATTACH))

    async def go() -> None:
        winch: list[Callable[[], None]] = []
        reader: list[Callable[[], None]] = []
        task = asyncio.ensure_future(
            term.attach_session(
                client,
                "t-1",
                observe=False,
                write_out=lambda b: None,
                read_input=lambda: b"",
                add_input_reader=lambda cb: (reader.append(cb), lambda: None)[1],
                size=lambda: (120, 40),
                on_winch=lambda cb: (winch.append(cb), lambda: None)[1],
            )
        )
        await asyncio.sleep(0.05)
        winch[0]()
        await asyncio.sleep(0.05)
        reader[0]()  # EOF ends the attach
        await asyncio.wait_for(task, 3)

    asyncio.run(go())
    assert ("terminal.resize", {"terminalId": "t-1", "cols": 120, "rows": 40}) in client.calls

    observer = FakeClient(dict(ATTACH))
    run_attach(observer, [b"typed", b"\x1c"], observe=True)
    assert "terminal.write" not in observer.methods()
    assert observer.calls[0][1]["mode"] == "observe"


def test_attach_acks_in_batches() -> None:
    big = b"x" * term.ACK_BATCH
    client = FakeClient(
        dict(ATTACH),
        frames=[{"method": "terminal.data", "params": {"terminalId": "t-1", "seq": 2, "data": b64(big)}}],
    )
    run_attach(client, [b"\x1c"])
    assert ("terminal.ack", {"terminalId": "t-1", "bytes": term.ACK_BATCH}) in client.calls


@posix_only
def test_raw_mode_restores_the_tty_even_when_the_body_raises() -> None:
    import termios

    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        with pytest.raises(RuntimeError), term.raw_mode(slave):
            assert termios.tcgetattr(slave) != before
            raise RuntimeError("boom")
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


@posix_only
def test_a_failed_attach_leaves_the_tty_restored() -> None:
    import termios

    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        client = FakeClient({"terminal.attach": RpcCallError("not_found", "gone")})

        async def go() -> None:
            with term.raw_mode(slave):
                await term.attach_session(
                    client,
                    "t-1",
                    observe=False,
                    write_out=lambda b: None,
                    read_input=lambda: b"",
                    add_input_reader=lambda cb: lambda: None,
                    size=lambda: (80, 24),
                    on_winch=lambda cb: lambda: None,
                )

        with pytest.raises(RpcCallError):
            asyncio.run(go())
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_attach_refuses_without_a_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert asyncio.run(term.term_attach(FakeClient(), "t-1", observe=False)) == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_attach_is_unsupported_on_windows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert asyncio.run(term.term_attach(FakeClient(), "t-1", observe=False)) == 2  # type: ignore[arg-type]
    assert "not supported on Windows" in capsys.readouterr().err


def test_parser_knows_term_and_session() -> None:
    parser = cli_main.build_parser()
    args = parser.parse_args(["term", "send", "t-1", "run", "tests", "--no-submit"])
    assert (args.subcommand, args.action, args.terminal_id, args.text) == ("term", "send", "t-1", ["run", "tests"])
    assert args.no_submit is True
    assert parser.parse_args(["term", "read", "t-1", "--lines", "9"]).lines == 9
    assert parser.parse_args(["--session", "s-9"]).session == "s-9"


def test_launch_tui_passes_the_session_without_a_new_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(cli_main, "resolve_tui_command", lambda: ["node", "tui.js"])

    class _Info:
        port = 1234
        token = "t"

    async def _ensure(_home: Any) -> _Info:
        return _Info()

    monkeypatch.setattr(cli_main, "ensure_daemon", _ensure)
    monkeypatch.setattr(cli_main.subprocess, "call", lambda command, env=None, **_kw: (seen.append(command), envs.append(env or {}), 0)[2])
    args = cli_main.build_parser().parse_args(["--cwd", str(tmp_path), "--session", "s-9"])
    assert cli_main.launch_tui(args, None) == 0
    assert seen[0][seen[0].index("--session") + 1] == "s-9"
    assert "--token" not in seen[0] and "t" not in seen[0]
    assert envs[0]["SNOWPEA_TUI_TOKEN"] == "t"


def test_session_does_not_combine_with_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main.main(["--session", "s-9", "--remote", "host"]) == cli_main.EXIT_USAGE
    assert "does not combine" in capsys.readouterr().err
