"""Security review, part 2: process hygiene of the external CLI layer.

* H1 — orphan reaping trusts nothing in a pid file: hostile records (pgid 0 /
  1 / ``true``, a foreign pid, the wrong executable, a symlink, a file name
  that does not match, a missing start time, a live recording daemon) cause
  no signal at all.  Every signal sender is replaced by a recorder: no test
  here can signal a process it did not start, and the only real processes
  named in records are children this test spawned.
* L3 — the child environment is an allowlist.
* L5 — system helpers come from trusted directories.
* L1 — a deeply nested transcript line never breaks a listing.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.external import claude, cursor, proc
from snowpea_core.external.proc import ProcessSupervisor

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc records")

#: The real sender, for the children this module starts (the tests record os.kill).
_REAL_KILL = os.kill


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Record every signal instead of sending it (signal 0, a liveness probe, still works)."""
    sent: list[tuple[str, int, int]] = []
    real_kill = os.kill

    def fake_kill(pid: int, sig: int) -> None:
        if sig == 0:
            real_kill(pid, 0)
            return
        sent.append(("kill", pid, sig))

    def fake_killpg(pgid: int, sig: int) -> None:
        sent.append(("killpg", pgid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    return sent


@pytest.fixture
def child() -> Iterator[subprocess.Popen[bytes]]:
    """A process this test started (its own session), stopped at the end."""
    started = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        yield started
    finally:
        _REAL_KILL(started.pid, signal.SIGKILL)  # our own child; os.kill may be a recorder
        started.wait()


def _record(home: Path, name: str, **fields: Any) -> Path:
    folder = home / "external"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / name
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _good(pid: int) -> dict[str, Any]:
    return {
        "pid": pid,
        "pgid": pid,
        "startTime": proc.process_start_time(pid),
        "exe": sys.executable,
        "cli": "claude",
        "daemonPid": os.getpid(),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"pgid": 0},
        {"pgid": 1},
        {"pgid": True},
        {"pid": True},
        {"startTime": None},
        {"startTime": "1"},
        {"exe": "/bin/true"},
        {"exe": None},
        {"daemonPid": None},
    ],
    ids=["pgid0", "pgid1", "pgid-true", "pid-true", "no-start-time", "wrong-start-time",
         "wrong-exe", "no-exe", "no-daemon-pid"],
)
def test_hostile_records_kill_nothing(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any], change: dict[str, Any]
) -> None:
    home = tmp_path / "home"
    record = {**_good(child.pid), **change}
    path = _record(home, f"{child.pid}.json", **record)
    ProcessSupervisor(home).reap_orphans()
    # Each variant breaks one rule and sends nothing, except a hostile pgid
    # (0, 1, true) on an otherwise trusted record: that never reaches killpg,
    # only the recorded pid itself is stopped.
    if "pgid" in change:
        assert not any(name == "killpg" for name, *_ in signals)
        assert all(pid == child.pid for _name, pid, _sig in signals)
    else:
        assert signals == [], (change, signals)
    assert not path.exists()


def test_a_record_naming_another_process_kills_nothing(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any]
) -> None:
    home = tmp_path / "home"
    # The pytest process itself, the daemon's parent and init are never targets.
    for pid in (os.getpid(), os.getppid(), 1):
        _record(home, f"{pid}.json", **{**_good(child.pid), "pid": pid, "pgid": pid,
                                       "startTime": proc.process_start_time(pid)})
    ProcessSupervisor(home).reap_orphans()
    assert signals == []


def test_file_name_must_match_the_pid(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any]
) -> None:
    home = tmp_path / "home"
    _record(home, "12345.json", **_good(child.pid))
    ProcessSupervisor(home).reap_orphans()
    assert signals == []


def test_a_symlinked_record_is_never_read(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any]
) -> None:
    home = tmp_path / "home"
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_good(child.pid)), encoding="utf-8")
    folder = home / "external"
    folder.mkdir(parents=True)
    os.chmod(folder, 0o700)
    (folder / f"{child.pid}.json").symlink_to(target)
    ProcessSupervisor(home).reap_orphans()
    assert signals == []
    assert target.exists()  # the link went, not what it pointed at


def test_a_live_recording_daemon_keeps_its_child(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any]
) -> None:
    other_daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        home = tmp_path / "home"
        _record(home, f"{child.pid}.json", **{**_good(child.pid), "daemonPid": other_daemon.pid})
        ProcessSupervisor(home).reap_orphans()
        assert signals == []
    finally:
        _REAL_KILL(other_daemon.pid, signal.SIGKILL)  # our own child
        other_daemon.wait()


def test_a_trusted_record_kills_only_its_own_group(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any]
) -> None:
    home = tmp_path / "home"
    _record(home, f"{child.pid}.json", **_good(child.pid))
    assert ProcessSupervisor(home).reap_orphans() == 1
    assert ("killpg", child.pid, 9) in signals
    assert ("kill", child.pid, 9) in signals
    assert all(target == child.pid for _name, target, _sig in signals)


def test_a_wide_open_or_foreign_directory_is_not_used(
    tmp_path: Path, child: subprocess.Popen[bytes], signals: list[Any]
) -> None:
    home = tmp_path / "home"
    path = _record(home, f"{child.pid}.json", **_good(child.pid))
    os.chmod(home / "external", 0o777)
    ProcessSupervisor(home).reap_orphans()  # tightened to 0700 first, then used
    assert (os.stat(home / "external").st_mode & 0o777) == 0o700
    assert not path.exists()
    signals.clear()
    linked = tmp_path / "home2"
    linked.mkdir()
    real = tmp_path / "real-dir"
    real.mkdir()
    (linked / "external").symlink_to(real)
    (real / f"{child.pid}.json").write_text(json.dumps(_good(child.pid)), encoding="utf-8")
    assert ProcessSupervisor(linked).reap_orphans() == 0
    assert signals == []


def test_pid_files_are_written_owner_only_and_never_through_a_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    supervisor = ProcessSupervisor(home)
    trap = tmp_path / "trap"
    trap.write_text("keep", encoding="utf-8")
    (home / "external").mkdir(parents=True)
    pid = os.getpid()
    (home / "external" / f".{pid}.json.tmp").symlink_to(trap)
    path = supervisor._write_pid_file(pid, "claude", None, exe=sys.executable)
    assert path is not None
    assert trap.read_text(encoding="utf-8") == "keep"
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (os.stat(home / "external").st_mode & 0o777) == 0o700
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["exe"] == sys.executable and record["startTime"]
    path.unlink()


# -- L3: environment allowlist ----------------------------------------------


def test_the_child_environment_is_an_allowlist() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "XDG_CONFIG_HOME": "/home/x/.config",
        "CLAUDE_CONFIG_DIR": "/home/x/.claude",
        "CODEX_HOME": "/home/x/.codex",
        "ANTHROPIC_API_KEY": "secret-a",
        "OPENAI_API_KEY": "secret-o",
        "GEMINI_API_KEY": "secret-g",
        "GITHUB_TOKEN": "secret-gh",
        "AWS_SECRET_ACCESS_KEY": "secret-aws",
        "NPM_TOKEN": "secret-npm",
        "SNOWPEA_TOKEN": "secret-sp",
        "HTTPS_PROXY": "http://user:pass@proxy:3128",
        "HTTP_PROXY": "http://proxy:3128",
        "no_proxy": "localhost",
        "RANDOM_THING": "x",
    }
    env = proc.scrubbed_env(base=base)
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME",
                        "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HTTP_PROXY", "no_proxy"}
    passed = proc.scrubbed_env(base=base, pass_env=["ANTHROPIC_API_KEY", "SNOWPEA_TOKEN"])
    assert passed["ANTHROPIC_API_KEY"] == "secret-a"
    assert "SNOWPEA_TOKEN" not in passed  # snowpea's own never, even when named


def test_pass_env_names_are_validated() -> None:
    from pydantic import ValidationError

    from snowpea_core.config.settings import JarvisExternalCliSettings

    assert JarvisExternalCliSettings(passEnv=["ANTHROPIC_API_KEY"]).passEnv == ["ANTHROPIC_API_KEY"]
    for bad in (["SNOWPEA_TOKEN"], ["A B"], ["$(x)"]):
        with pytest.raises(ValidationError):
            JarvisExternalCliSettings(passEnv=bad)


# -- L5: trusted helper paths -------------------------------------------------


def test_helpers_come_from_trusted_directories(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    rogue = tmp_path / "ps"
    rogue.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rogue.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    proc.trusted_tool.cache_clear()
    found = proc.trusted_tool("ps")
    assert found is None or os.path.dirname(found) in proc.TRUSTED_TOOL_DIRS
    assert proc.trusted_tool("definitely-not-a-tool-xyz") is None


# -- L1: deep nesting --------------------------------------------------------


def test_deeply_nested_lines_are_skipped(tmp_path: Path) -> None:
    deep = b"[" * 100_000 + b"]" * 100_000
    assert claude.parse_record(deep) is None
    assert claude.parse_record(b'{"type": "user"}') == {"type": "user"}
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(deep + b"\n" + json.dumps({"role": "user", "message": {
        "content": [{"type": "text", "text": "hi"}]}}).encode() + b"\n")
    assert len(cursor._records(transcript)) == 1
    records, offset = cursor._records_tail(transcript, 0)
    assert len(records) == 1 and offset == transcript.stat().st_size


def test_cursor_tail_reads_only_what_was_added(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    line = json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "a"}]}})
    transcript.write_text(line + "\n", encoding="utf-8")
    first, offset = cursor._records_tail(transcript, 0)
    assert len(first) == 1
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n" + line[:10])  # one whole line and half of the next
    more, offset2 = cursor._records_tail(transcript, offset)
    assert len(more) == 1
    assert offset2 == offset + len(line) + 1  # the half line waits for its newline
    again, offset3 = cursor._records_tail(transcript, offset2)
    assert again == [] and offset3 == offset2
