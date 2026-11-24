"""M37 security review fixes in the terminal runtime (T1): H4, M5, L1, L2, L3."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.terminal import link as link_mod
from snowpea_core.terminal import osc
from snowpea_core.terminal.holder import CHALLENGE, HELLO, frame, read_frame
from snowpea_core.terminal.link import HolderError, HolderLink
from snowpea_core.terminal.manager import (
    TerminalManager,
    TerminalSpec,
    _holder_alive,
    close_all_holders,
)
from snowpea_core.terminal.status import (
    LAUNCH_SILENCE,
    STALE_OSC_WORKING,
    RuleVerdict,
    StatusMachine,
)

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


class Clock:
    def __init__(self) -> None:
        self.t = 50.0

    def __call__(self) -> float:
        return self.t


class Rules:
    def __init__(self) -> None:
        self.verdict: RuleVerdict | None = None

    def apply_rules(self, screen_text: str, title: str) -> RuleVerdict | None:
        return self.verdict


def _screen() -> tuple[str, str]:
    return "", ""


async def _until(predicate, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


# -- H4: OSC 7501 vs what the screen shows -------------------------------------


def test_visible_blocker_outranks_osc_for_a_profile_with_rules() -> None:
    rules, clock = Rules(), Clock()
    machine = StatusMachine(rules=rules, clock=clock)
    clock.t += LAUNCH_SILENCE
    machine.apply_osc("working")  # e.g. printed by a cat'ed file
    rules.verdict = RuleVerdict("blocked", kind="permission", detail="rm -r build")
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("blocked", "screen")


def test_idle_prompt_rule_outranks_a_stale_osc_working() -> None:
    rules, clock = Rules(), Clock()
    machine = StatusMachine(rules=rules, clock=clock)
    clock.t += LAUNCH_SILENCE
    machine.apply_osc("working")
    rules.verdict = RuleVerdict("idle", visible_idle=True)
    machine.on_output()
    machine.poll(_screen)
    assert machine.state == "idle"


def test_osc_ranks_first_without_screen_rules() -> None:
    machine = StatusMachine(rules=None, clock=Clock())
    machine.apply_osc("blocked", kind="question")
    assert (machine.state, machine.status.source) == ("blocked", "osc")


def test_osc_working_expires_without_a_refresh() -> None:
    clock = Clock()
    machine = StatusMachine(rules=None, clock=clock)
    machine.apply_osc("working")
    clock.t += STALE_OSC_WORKING - 1
    machine.on_output()  # output keeps it alive
    machine.poll(_screen)
    assert machine.state == "working"
    clock.t += STALE_OSC_WORKING - 0.5
    machine.poll(_screen)
    assert machine.state == "working"
    clock.t += 1
    machine.poll(_screen)
    assert machine.state == "idle"
    machine.apply_osc("blocked", kind="question")
    clock.t += STALE_OSC_WORKING * 3
    machine.poll(_screen)
    assert machine.state == "blocked"  # only "working" lapses


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


#: Exactly what the snowpea TUI writes (tui/src/util/osc-status.ts), plus the
#: older form that carried id=<terminalId>.
TUI_PROGRAM = r"""
import os, sys, time
sys.stdout.write("\x1b]7501;state=blocked:kind=permission:app=snowpea\x07ready\r\n")
sys.stdout.flush()
time.sleep(0.3)
tid = os.environ["SNOWPEA_TERMINAL_ID"]
sys.stdout.write("\x1b]7501;state=working:app=snowpea:id=" + tid + "\x07")
sys.stdout.write("\x1b]7501;state=error:id=someone-else\x07")
sys.stdout.flush()
time.sleep(30)
"""


@POSIX
async def test_the_tui_status_record_applies(tmp_path: Path) -> None:
    manager = TerminalManager(tmp_path / "home")
    await manager.start()
    try:
        info = await manager.create(TerminalSpec(
            argv=[sys.executable, "-c", TUI_PROGRAM], env=_env(), cwd=str(tmp_path),
            profile="snowpea"))
        term = manager.get(info["terminalId"])
        await _until(lambda: term.status.state == "blocked")
        assert (term.status.status.kind, term.status.status.source) == ("permission", "osc")
        await _until(lambda: term.status.state == "working")
        await asyncio.sleep(0.3)
        assert term.status.state == "working"  # the id=someone-else record is not the root
    finally:
        await manager.close_all()
        await manager.shutdown()


# -- M5: terminal.wait output is literal ---------------------------------------


@POSIX
async def test_wait_output_is_a_literal_and_cannot_stall_the_loop(tmp_path: Path) -> None:
    manager = TerminalManager(tmp_path / "home")
    await manager.start()
    try:
        program = "import time; time.sleep(0.5); print('a+b ' + 'a' * 40 + '!'); time.sleep(30)"
        info = await manager.create(TerminalSpec(
            argv=[sys.executable, "-c", program], env=_env(), cwd=str(tmp_path)))
        tid = info["terminalId"]
        evil = asyncio.ensure_future(manager.wait(tid, timeout=3, output="(a+)+$"))
        plain = asyncio.ensure_future(manager.wait(tid, timeout=5, output="a+b"))
        started = time.monotonic()
        result = await plain
        assert result["reason"] == "match" and result["text"] == "a+b"
        assert (await evil)["reason"] == "timeout"  # literal "(a+)+$" never appears
        assert time.monotonic() - started < 4.5
    finally:
        await manager.close_all()
        await manager.shutdown()


# -- L1: the short socket directory must be ours --------------------------------


@POSIX
def test_terminals_dir_refuses_another_users_directory(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.terminal import report

    home = tmp_path / "home"
    (home / "terminals").mkdir(parents=True)
    real = os.getuid()
    monkeypatch.setattr(report.os, "getuid", lambda: real + 1)
    with pytest.raises(OSError, match="another user"):
        link_mod.terminals_dir(home)


@POSIX
def test_terminals_dir_refuses_a_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    (home / "terminals").symlink_to(elsewhere)
    with pytest.raises(OSError):
        link_mod.terminals_dir(home)
    assert elsewhere.stat().st_mode & 0o777 == 0o755  # never chmod-ed through the link


# -- L2: the holder proves it has the token; the token never crosses ------------


@POSIX
async def test_an_impersonator_at_the_socket_is_refused_and_never_sees_the_token(
    tmp_path: Path,
) -> None:
    token = os.urandom(32).hex()
    received = bytearray()

    async def impostor(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        kind, payload = await read_frame(reader)
        received.extend(payload)
        assert kind == HELLO
        writer.write(frame(CHALLENGE, os.urandom(64)))  # cannot compute the proof
        received.extend(await reader.read(4096))
        writer.close()

    path = str(tmp_path / "s.sock")
    server = await asyncio.start_unix_server(impostor, path)
    try:
        link = HolderLink(path, token, on_data=lambda d, t: None, on_exit=lambda c: None)
        with pytest.raises(HolderError, match="not its holder"):
            await link.connect()
        await asyncio.sleep(0.1)
    finally:
        server.close()
    assert bytes.fromhex(token) not in bytes(received)
    assert token.encode() not in bytes(received)


def test_unknown_start_time_is_not_alive() -> None:
    assert not _holder_alive(os.getpid(), None)
    assert not _holder_alive(os.getpid(), "not-the-start-time")


# -- L3: close_all_holders never signals a process it cannot prove is its holder -


@POSIX
def test_close_all_holders_leaves_unproven_pids_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = home / "terminals"
    folder.mkdir(parents=True)
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        cases: list[dict[str, Any]] = [
            {"id": "t0000000a", "pid": bystander.pid, "holderStarted": "bogus"},
            {"id": "t0000000b", "pid": bystander.pid},  # no start time, no link
        ]
        for meta in cases:
            (folder / f"{meta['id']}.json").write_text(json.dumps(meta))
        assert close_all_holders(home, timeout=0.5) == 0
        assert bystander.poll() is None  # never signalled
        assert not list(folder.glob("t*.json"))
    finally:
        bystander.kill()
        bystander.wait()


def test_scanner_still_reads_a_bel_terminated_tui_record() -> None:
    events = osc.EscapeScanner().feed(b"\x1b]7501;state=blocked:kind=permission:app=snowpea\x07")
    assert events == [osc.StatusRecord(state="blocked", kind="permission", app="snowpea")]
