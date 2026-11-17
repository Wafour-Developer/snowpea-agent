"""The tmux bridge (1.56.0): a session open in the person's tmux pane, driven safely.

Unit tests run against ``tests/fixtures/fake_tmux.py`` (a fake tmux that plays
a JSON world and records argv and stdin) and a fake process probe: no tmux
server and no real CLI.  ``test_real_tmux_private_server`` uses the real tmux
on a PRIVATE server (``-S <tmp_path>/tmux.sock``) only; the person's default
tmux server is never touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.external import claude_tmux, modes
from snowpea_core.external import tmux as tmux_mod
from snowpea_core.external.agy import NATIVE_MODES as AGY_MODES
from snowpea_core.external.claude import NATIVE_MODES as CLAUDE_MODES
from snowpea_core.external.codex import NATIVE_MODES as CODEX_MODES
from snowpea_core.external.cursor import NATIVE_MODES as CURSOR_MODES
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.tmux import (
    PROFILES,
    Pane,
    Probe,
    ProcFacts,
    TerminalBridge,
    detect_mode,
    find_pane,
    recognise,
)
from snowpea_core.server import errors

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no tmux on Windows")

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_TMUX = FIXTURES / "fake_tmux.py"
RECORDINGS = FIXTURES / "external_recordings" / "tmux"
PTS3 = 34819


class FakeProbe(Probe):
    """Process facts from a dict: ``pid -> ProcFacts`` and ``tty path -> device``."""

    def __init__(self) -> None:
        self.procs: dict[int, ProcFacts] = {}
        self.ttys: dict[str, int] = {"/dev/pts/3": PTS3, "/dev/pts/4": 34820}

    def facts(self, pid: int) -> ProcFacts | None:
        return self.procs.get(pid)

    def tty_dev(self, path: str) -> int | None:
        return self.ttys.get(path)


class World:
    def __init__(self, tmp: Path) -> None:
        self.path = tmp / "world.json"
        self.log = tmp / "tmux.log"
        self.socket = str(tmp / "s")
        self.data: dict[str, Any] = {
            "panes": [
                {"pane": "%3", "tty": "/dev/pts/3", "pid": 90, "session": "work",
                 "window": "1", "command": "claude"},
                {"pane": "%4", "tty": "/dev/pts/4", "pid": 91, "session": "work",
                 "window": "2", "command": "bash"},
            ],
            "screens": {"%3": ["❯ \n  ? for shortcuts\n"]},
        }
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data))

    def pane(self, pane_id: str) -> dict[str, Any]:
        return next(p for p in self.data["panes"] if p["pane"] == pane_id)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def commands(self) -> list[list[str]]:
        """argv after ``-S <socket>``."""
        return [call["argv"][2:] for call in self.calls()]

    def keys_sent(self) -> list[list[str]]:
        return [argv for argv in self.commands()
                if argv[:1] in (["send-keys"], ["paste-buffer"], ["load-buffer"])]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    setup = World(tmp_path)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(setup.socket)  # a real socket file: Tmux(-S) checks it
    monkeypatch.setenv("FAKE_TMUX_WORLD", str(setup.path))
    monkeypatch.setenv("FAKE_TMUX_LOG", str(setup.log))
    monkeypatch.setattr(claude_tmux.proc, "EXTRA_ALLOW_PREFIXES", ["FAKE_"])
    claude_tmux.set_tmux_binary(str(FAKE_TMUX))
    try:
        yield setup
    finally:
        claude_tmux.set_tmux_binary(None)
        listener.close()


@pytest.fixture
def probe() -> FakeProbe:
    fake = FakeProbe()
    fake.procs[4242] = ProcFacts(start="111", tty_dev=PTS3, pgrp=4242, tpgid=4242)
    return fake


async def bridge_for(world: World, probe: FakeProbe, cli: str = "claude") -> TerminalBridge:
    pane = await find_pane(4242, sockets=[world.socket], probe=probe)
    assert pane is not None
    return TerminalBridge(cli=cli, pane=pane, cli_pid=4242, start="111",
                          profile=PROFILES[cli], probe=probe)


# -- detection -------------------------------------------------------------------------


async def test_pane_found_from_the_process_tty_only(world: World, probe: FakeProbe) -> None:
    pane = await find_pane(4242, sockets=[world.socket], probe=probe)
    assert pane == Pane(socket=world.socket, pane="%3", tty="/dev/pts/3", pid=90,
                        session="work", window="1", command="claude")
    assert pane.wire() == {"kind": "tmux", "pane": "%3", "session": "work", "window": "1"}
    # Only list-panes metadata was read: no capture, no key.
    assert [argv[0] for argv in world.commands()] == ["list-panes"]
    # No tty, an unknown pid, a dead pane, no matching pane: no bridge.
    probe.procs[7] = ProcFacts(start="1", tty_dev=None, pgrp=7, tpgid=7)
    assert await find_pane(7, sockets=[world.socket], probe=probe) is None
    assert await find_pane(8, sockets=[world.socket], probe=probe) is None
    world.pane("%3")["dead"] = True
    world.save()
    assert await find_pane(4242, sockets=[world.socket], probe=probe) is None
    assert await find_pane(4242, sockets=[], probe=probe) is None


async def test_no_tmux_installed_means_no_bridge(probe: FakeProbe,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_mod, "tmux_executable", lambda: None)
    assert await find_pane(4242, sockets=["/nonexistent"], probe=probe) is None


# -- the hard guard ------------------------------------------------------------------------


@pytest.mark.parametrize("breakage", ["pane gone", "pane dead", "tty changed", "pid dead",
                                      "pid reused", "background", "shell", "pager"])
async def test_guard_refuses_and_sends_nothing(world: World, probe: FakeProbe,
                                               breakage: str) -> None:
    bridge = await bridge_for(world, probe)
    if breakage == "pane gone":
        world.data["panes"] = [p for p in world.data["panes"] if p["pane"] != "%3"]
    elif breakage == "pane dead":
        world.pane("%3")["dead"] = True
    elif breakage == "tty changed":
        world.pane("%3")["tty"] = "/dev/pts/4"
    elif breakage == "pid dead":
        del probe.procs[4242]
    elif breakage == "pid reused":
        probe.procs[4242] = ProcFacts(start="999", tty_dev=PTS3, pgrp=4242, tpgid=4242)
    elif breakage == "background":
        probe.procs[4242] = ProcFacts(start="111", tty_dev=PTS3, pgrp=4242, tpgid=5000)
    elif breakage == "shell":
        world.pane("%3")["command"] = "bash"
    elif breakage == "pager":
        world.pane("%3")["command"] = "less"
    world.save()
    for action in ("text", "key", "slash"):
        with pytest.raises(ExternalError) as caught:
            if action == "text":
                await bridge.send_text("hello")
            elif action == "key":
                await bridge.key("Escape")
            else:
                await bridge.slash("/plan")
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        assert caught.value.details == {"reason": "terminal_guard"}
    assert world.keys_sent() == []
    assert bridge.lost is not None
    # Once lost, it stays lost even if the pane looks fine again.
    world.data = World(Path(world.path).parent).data
    world.save()
    with pytest.raises(ExternalError):
        await bridge.send_text("hello")


# -- what goes in ----------------------------------------------------------------------------


async def test_text_goes_in_as_a_bracketed_paste_from_stdin(world: World,
                                                           probe: FakeProbe) -> None:
    bridge = await bridge_for(world, probe)
    text = "fix the tests\n한국어 둘째 줄\twith a tab; $(not run) `nor this`"
    await bridge.send_text(text, by="person")
    calls = [c for c in world.calls() if c["argv"][2] != "list-panes"]
    argvs = [c["argv"][2:] for c in calls]
    buffer = argvs[0][2]
    assert argvs == [
        ["load-buffer", "-b", buffer, "-"],
        ["paste-buffer", "-p", "-d", "-b", buffer, "-t", "%3"],
        ["send-keys", "-t", "%3", "Enter"],
    ]
    assert buffer.startswith("sp-")
    assert calls[0]["stdin"] == text
    # The text never appears in any argv; every call names the private -S socket.
    assert all(text not in " ".join(c["argv"]) and "$(not" not in " ".join(c["argv"])
               for c in world.calls())
    assert all(c["argv"][:2] == ["-S", world.socket] for c in world.calls())
    # The guard ran before each key delivery (three list-panes between the calls).
    kinds = [c["argv"][2] for c in world.calls()]
    assert kinds == ["list-panes", "list-panes", "load-buffer", "list-panes", "paste-buffer",
                     "list-panes", "send-keys"]


@pytest.mark.parametrize("text", [
    "\x1b[2J clear the screen", "ring \x07", "carriage\rreturn", "nul \x00", "c1 \x9b2J",
    "/plan do it", "  /model opus", "!rm it", "﻿hidden", "",
])
async def test_control_characters_and_prefixes_are_refused(world: World, probe: FakeProbe,
                                                          text: str) -> None:
    bridge = await bridge_for(world, probe)
    with pytest.raises(ExternalError) as caught:
        await bridge.send_text(text)
    assert caught.value.code == errors.INVALID_PARAMS
    assert world.keys_sent() == []


async def test_only_listed_keys_and_table_slash_commands(world: World,
                                                         probe: FakeProbe) -> None:
    bridge = await bridge_for(world, probe)
    for key in ("C-c", "C-d", "q", "Enter Enter", "2"):
        with pytest.raises(ExternalError):
            await bridge.key(key)
    for bad in ("/plan now", "plan", "/PLAN", "/plan;ls", "/" + "a" * 40):
        with pytest.raises(ExternalError):
            await bridge.slash(bad)
    assert world.keys_sent() == []
    await bridge.slash("/plan")
    assert [a for a in world.keys_sent()] == [["send-keys", "-t", "%3", "-l", "--", "/plan"],
                                              ["send-keys", "-t", "%3", "Enter"]]


# -- modes ------------------------------------------------------------------------------------


def test_mode_tables_say_how_to_switch_in_the_terminal() -> None:
    claude = {row.id: row.terminal for row in CLAUDE_MODES}
    assert claude["plan"] == {"slash": "/plan"}
    assert claude["default"] == claude["acceptEdits"] == claude["auto"] == {"cycle": True}
    assert claude["dontAsk"] is None and claude["bypassPermissions"] is None
    assert all(row.terminal is None for row in CODEX_MODES)  # /permissions is a picker
    agy = {row.id: row.terminal for row in AGY_MODES}
    assert agy["plan"] == agy["accept-edits"] == agy["auto"] == {"cycle": True}
    assert agy["default"] is None and agy["dangerously-skip-permissions"] is None
    cursor = {row.id: row.terminal for row in CURSOR_MODES}
    assert cursor["ask"] == cursor["plan"] == {"cycle": True}
    assert cursor["agent"] is None and cursor["yolo"] is None
    # Every mode a terminal can reach is one the profile can confirm on screen.
    for cli, table in (("claude", CLAUDE_MODES), ("agy", AGY_MODES),
                       ("cursor", CURSOR_MODES)):
        profile = PROFILES[cli]
        shown = {mode for _text, mode in profile.indicators} | {profile.default_mode}
        for row in table:
            if row.terminal:
                assert row.id in shown, (cli, row.id)


def test_mode_read_from_the_recorded_status_lines() -> None:
    ready = (RECORDINGS / "claude_02_ready.txt").read_text()
    assert detect_mode(PROFILES["claude"], ready) == "auto"
    agy = (RECORDINGS / "agy_06_accept_edits_write_no_prompt.txt").read_text()
    assert detect_mode(PROFILES["agy"], agy) == "accept-edits"
    assert detect_mode(PROFILES["claude"], "❯ \n  ? for shortcuts\n") == "default"
    assert detect_mode(PROFILES["claude"], "nothing to see") is None
    assert detect_mode(PROFILES["cursor"], "  Plan (shift+tab to cycle)\n") == "plan"
    assert detect_mode(PROFILES["codex"], "› \n") is None


async def test_mode_switch_cycles_until_the_pane_shows_it(world: World,
                                                          probe: FakeProbe) -> None:
    world.data["screens"]["%3"] = ["  ? for shortcuts", "  ⏵⏵ accept edits on",
                                   "  ⏸ plan mode on", "  ⏵⏵ auto mode on"]
    world.save()
    bridge = await bridge_for(world, probe)
    assert await bridge.current_mode() == "default"
    rows = {row.id: row for row in CLAUDE_MODES}
    assert await bridge.switch_mode("auto", rows["auto"].terminal, settle=0) is True
    assert [a[-1] for a in world.keys_sent()] == ["BTab", "BTab", "BTab"]
    # A mode the pane never shows: at most MAX_CYCLE presses, then "unconfirmed".
    world.data["screens"]["%3"] = ["  ? for shortcuts"]
    world.data["at"] = {"%3": 0}
    world.save()
    world.log.unlink()
    assert await bridge.switch_mode("acceptEdits", rows["acceptEdits"].terminal,
                                    settle=0) is False
    assert len(world.keys_sent()) == tmux_mod.MAX_CYCLE
    # Plan goes in as its slash command.
    world.data["screens"]["%3"] = ["  ? for shortcuts", "  ⏸ plan mode on"]
    world.save()
    world.log.unlink()

    async def slash(command: str, *, by: str = "x") -> None:
        world.data["at"] = {"%3": 1}
        world.save()
        await TerminalBridge.slash(bridge, command, by=by)

    bridge.slash = slash  # type: ignore[method-assign]
    assert await bridge.switch_mode("plan", rows["plan"].terminal, settle=0) is True
    assert ["send-keys", "-t", "%3", "-l", "--", "/plan"] in world.keys_sent()


# -- permission prompts ---------------------------------------------------------------------------


def test_prompts_recognised_from_the_recorded_dialogs() -> None:
    claude = recognise(PROFILES["claude"],
                       (RECORDINGS / "claude_06_permission_dialog.txt").read_text())
    assert claude is not None
    assert claude.question == "Do you want to create note.txt?"
    assert claude.text.splitlines()[-1].strip().startswith("Esc to cancel")
    codex = recognise(PROFILES["codex"],
                      (RECORDINGS / "codex_05_permission_dialog.txt").read_text())
    assert codex is not None and codex.question == "Would you like to run the following command?"
    assert PROFILES["codex"].prompt is not None and PROFILES["codex"].prompt.person_only
    for name in ("claude_02_ready.txt", "claude_03_turn1.txt", "codex_02_ready.txt"):
        assert recognise(PROFILES["claude"], (RECORDINGS / name).read_text()) is None
        assert recognise(PROFILES["codex"], (RECORDINGS / name).read_text()) is None
    # agy and cursor have no rule: never guessed.
    assert recognise(PROFILES["agy"], "Do you want to proceed?\n❯ 1. Yes\nEsc to cancel") is None
    assert recognise(PROFILES["cursor"], "Run this command?\n y to confirm") is None


async def test_answer_only_while_the_same_prompt_is_on_screen(world: World,
                                                             probe: FakeProbe) -> None:
    dialog = (RECORDINGS / "claude_06_permission_dialog.txt").read_text()
    world.data["screens"]["%3"] = [dialog]
    world.save()
    bridge = await bridge_for(world, probe)
    asked = await bridge.prompt()
    assert asked is not None
    # The screen moved on (the person answered in the terminal): nothing is sent.
    world.data["screens"]["%3"] = ["❯ \n  ? for shortcuts"]
    world.save()
    assert await bridge.answer(asked, True, by="person") is False
    # Another prompt took its place: nothing is sent either.
    world.data["screens"]["%3"] = [dialog.replace("create note.txt", "delete note.txt")]
    world.save()
    assert await bridge.answer(asked, True, by="person") is False
    assert world.keys_sent() == []
    # The same prompt: "1" allows, Escape denies.
    world.data["screens"]["%3"] = [dialog]
    world.save()
    assert await bridge.answer(asked, True, by="person") is True
    assert await bridge.answer(asked, False, by="person") is True
    assert world.keys_sent() == [["send-keys", "-t", "%3", "1"],
                                 ["send-keys", "-t", "%3", "Escape"]]


def test_snapshot_text_is_plain_and_clipped() -> None:
    raw = "\x1b[31mred\x1b[0m\r\nok\x07\n" + "x" * 20_000
    text = tmux_mod.clean_screen(raw)
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text
    assert len(text) == tmux_mod.SNAPSHOT_MAX


def test_native_mode_rows_keep_terminal_out_of_the_wire() -> None:
    assert "terminal" not in modes.find(CLAUDE_MODES, "plan").to_wire()  # type: ignore[union-attr]


# -- real tmux, private server only ---------------------------------------------------


def _tmux(sock: Path, *args: str, timeout: float = 10) -> str:
    """The test's own calls to its PRIVATE server (-S): never the default one."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    exe = shutil.which("tmux")
    assert exe is not None
    return subprocess.run([exe, "-S", str(sock), *args], capture_output=True, text=True,
                          env=env, timeout=timeout, check=False).stdout


def _until(predicate: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc probe")
async def test_real_tmux_private_server(tmp_path: Path) -> None:
    sock = tmp_path / "tmux.sock"
    out = tmp_path / "received.jsonl"
    pidfile = tmp_path / "cli.pid"
    sentinel = tmp_path / "reached"
    claude_tmux.set_tmux_binary(None)
    started = False
    try:
        _tmux(sock, "-f", "/dev/null", "new-session", "-d", "-s", "bridge", "-x", "120",
              "-y", "30", "env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
              "PS1=$ ", "sh", "-i")
        started = True
        _until(lambda: "$" in _tmux(sock, "capture-pane", "-p", "-t", "bridge"))
        # The test sets its own private pane up: start the fake CLI from the shell.
        _tmux(sock, "send-keys", "-t", "bridge", "-l", "--",
              f"{sys.executable} {FIXTURES / 'fake_term_cli.py'} {out} {pidfile}")
        _tmux(sock, "send-keys", "-t", "bridge", "Enter")
        pid = int(_until(lambda: pidfile.exists() and pidfile.read_text().strip()))
        _until(lambda: "fake cli ready" in _tmux(sock, "capture-pane", "-p", "-t", "bridge"))

        pane = await find_pane(pid, sockets=[str(sock)])
        assert pane is not None and pane.command.startswith("python")
        from snowpea_core.external import proc

        bridge = TerminalBridge(cli="claude", pane=pane, cli_pid=pid,
                                start=proc.process_start_time(pid), profile=PROFILES["claude"])
        text = "hello from snowpea\nsecond line — 둘째 줄"
        await bridge.send_text(text, by="test")
        received = _until(lambda: out.exists() and out.read_text().strip())
        assert [json.loads(line) for line in received.splitlines()] == [text]

        # The fake CLI exits: the shell prompt is in the foreground now.
        os.kill(pid, 15)
        _until(lambda: not proc.pid_alive(pid))
        await asyncio.sleep(0.3)
        with pytest.raises(ExternalError) as caught:
            await bridge.send_text(f"touch {sentinel}")
        assert caught.value.code == errors.EXTERNAL_VIEW_ONLY
        await asyncio.sleep(0.5)
        assert not sentinel.exists()
        assert str(sentinel) not in _tmux(sock, "capture-pane", "-p", "-t", "bridge")
        # A fresh bridge on the same pane would see the shell and refuse too.
        fresh = TerminalBridge(cli="claude", pane=pane, cli_pid=pid, start=None,
                               profile=PROFILES["claude"])
        with pytest.raises(ExternalError):
            await fresh.key("Escape")
    finally:
        if started:
            _tmux(sock, "kill-server")
