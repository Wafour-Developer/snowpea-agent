"""Windows process handling, exercised on any OS with fakes (no Windows binaries run).

Covers: the job flags, assignment, whole-tree termination on timeout and
cancel, the ``taskkill`` fallback for the exact pid only, no ``AttributeError``
from ``os.killpg``, creation flags on every spawn site, ``background: true``
without ``nohup sh``, the console-window detection table, the decoder for
CP949/CP932/CP1252 output, and the prompt text.
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.exec import local as local_mod
from snowpea_core.exec import winproc
from snowpea_core.exec.local import LocalBackend
from snowpea_core.prompts import environment, tool_descriptions
from snowpea_core.tools import process as process_tools
from snowpea_core.tools import shell as shell_mod

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeApi:
    """Records every job call; behaves like kernel32 would."""

    def __init__(self, *, assign_fails: bool = False, terminate_ok: bool = True) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.assign_fails = assign_fails
        self.terminate_ok = terminate_ok
        self.running = True
        self.active = 1

    def open_process(self, pid: int) -> int:
        self.calls.append(("open", pid))
        return 1000 + pid

    def create_job(self) -> int:
        self.calls.append(("create_job",))
        return 77

    def assign(self, job: int, process: int) -> None:
        self.calls.append(("assign", job, process))
        if self.assign_fails:
            raise OSError(5, "AssignProcessToJobObject failed (winerror 5)")

    def terminate_job(self, job: int, code: int = 1) -> bool:
        self.calls.append(("terminate_job", job))
        if self.terminate_ok:
            self.running = False
            self.active = 0
        return self.terminate_ok

    def active_processes(self, job: int) -> int:
        return self.active

    def is_running(self, process: int) -> bool:
        return self.running

    def close(self, handle: int) -> None:
        self.calls.append(("close", handle))


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    """Pretend the daemon runs on Windows, with a fake job API and taskkill."""
    api = FakeApi()
    monkeypatch.setattr(winproc, "enabled", lambda: True)
    monkeypatch.setattr(winproc, "_API", api)
    killed: list[int] = []
    monkeypatch.setattr(winproc, "taskkill_tree", lambda pid: killed.append(pid) or True)
    api.taskkilled = killed  # type: ignore[attr-defined]
    winproc._LIVE.clear()
    yield api
    winproc._LIVE.clear()


# ---------------------------------------------------------------------------
# the job API itself
# ---------------------------------------------------------------------------


class _Fn:
    def __init__(self, result: Any, log: list[tuple[str, tuple[Any, ...]]], name: str) -> None:
        self.result, self.log, self.name = result, log, name
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        self.log.append((self.name, args))
        return self.result


def test_the_job_kills_on_close_and_allows_no_breakaway(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple[str, tuple[Any, ...]]] = []
    names = (
        "CreateJobObjectW", "SetInformationJobObject", "QueryInformationJobObject",
        "AssignProcessToJobObject", "TerminateJobObject", "OpenProcess",
        "WaitForSingleObject", "CloseHandle",
    )
    kernel = SimpleNamespace(
        **{n: _Fn(42 if n == "CreateJobObjectW" else 1, log, n) for n in names}
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    api = winproc._Kernel32()
    assert api.create_job() == 42
    name, args = log[-1]
    assert name == "SetInformationJobObject"
    assert args[1] == winproc.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
    info = args[2]._obj
    # Exactly KILL_ON_JOB_CLOSE: no BREAKAWAY_OK (0x800), no SILENT_BREAKAWAY_OK (0x1000).
    assert info.BasicLimitInformation.LimitFlags == 0x2000
    assert args[3] == ctypes.sizeof(info)
    api.open_process(1234)
    access = log[-1][1][0]
    assert access & 0x0100 and access & 0x0001  # PROCESS_SET_QUOTA | PROCESS_TERMINATE


def test_spawn_flags_hide_the_console_and_start_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc.sys, "platform", "win32")
    assert winproc.spawn_kwargs() == {"creationflags": 0x08000000 | 0x00000200}
    monkeypatch.setattr(winproc.sys, "platform", "linux")
    assert winproc.spawn_kwargs() == {}


def test_contain_assigns_the_process_to_its_own_job(windows: FakeApi) -> None:
    tree = winproc.contain(4321)
    assert tree is not None and tree.contained
    assert ("open", 4321) in windows.calls
    assert ("assign", 77, 1000 + 4321) in windows.calls
    tree.terminate()
    assert ("terminate_job", 77) in windows.calls
    assert ("close", 77) in windows.calls and ("close", 5321) in windows.calls
    assert windows.taskkilled == []  # type: ignore[attr-defined]
    assert tree.closed and tree not in winproc._LIVE


def test_without_a_job_taskkill_runs_for_that_exact_pid_only(windows: FakeApi) -> None:
    windows.assign_fails = True
    tree = winproc.contain(4321)
    assert tree is not None and not tree.contained
    assert ("close", 77) in windows.calls  # the unusable job was not leaked
    tree.terminate()
    assert windows.taskkilled == [4321]  # type: ignore[attr-defined]


def test_taskkill_is_not_used_once_the_process_has_exited(windows: FakeApi) -> None:
    windows.assign_fails = True
    tree = winproc.contain(4321)
    assert tree is not None
    windows.running = False
    tree.terminate()
    assert windows.taskkilled == []  # type: ignore[attr-defined]


def test_taskkill_command_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"], seen["kwargs"] = argv, kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(winproc.subprocess, "run", run)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert winproc.taskkill_tree(4321)
    assert seen["argv"][0].lower().endswith("taskkill.exe")
    assert seen["argv"][1:] == ["/PID", "4321", "/T", "/F"]
    assert "/IM" not in seen["argv"]
    assert seen["kwargs"]["creationflags"] == winproc.CREATE_NO_WINDOW


def test_release_keeps_a_job_with_leftovers_until_terminate_all(windows: FakeApi) -> None:
    tree = winproc.contain(10)
    assert tree is not None
    windows.running = False
    windows.active = 1  # something it started still runs
    tree.release(keep_leftovers=True)
    assert not tree.closed and tree in winproc._LIVE
    assert winproc.terminate_all() == 1
    assert tree.closed and ("terminate_job", 77) in windows.calls


def test_release_without_leftovers_closes(windows: FakeApi) -> None:
    tree = winproc.contain(11)
    assert tree is not None
    windows.active = 0
    tree.release(keep_leftovers=True)
    assert tree.closed


def test_no_job_api_means_no_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winproc, "enabled", lambda: True)
    monkeypatch.setattr(winproc, "_API", False)
    assert winproc.contain(5) is None


def test_kill_swallows_an_already_gone_process() -> None:
    class Gone:
        returncode = None
        pid = 9

        def kill(self) -> None:
            raise ProcessLookupError()

        def terminate(self) -> None:
            raise ProcessLookupError()

    winproc.kill_now(Gone())
    winproc.terminate_now(Gone())
    asyncio.run(winproc.kill(Gone()))


# ---------------------------------------------------------------------------
# the shell: timeout and cancel on Windows
# ---------------------------------------------------------------------------


class _Pipe:
    """A stream that never ends until closed."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def read(self, _n: int) -> bytes:
        await self.closed.wait()
        return b""


class _FakeProcess:
    def __init__(self, api: FakeApi) -> None:
        self.pid = 4321
        self.returncode: int | None = None
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self._api = api

    async def wait(self) -> int:
        while self.returncode is None:
            if not self._api.running:
                self.returncode = 1
                self.stdout.closed.set()
                self.stderr.closed.set()
                break
            await asyncio.sleep(0.01)
        return int(self.returncode)


@pytest.fixture
def fake_spawn(windows: FakeApi, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def create_subprocess_shell(command: str, **kwargs: Any) -> _FakeProcess:
        seen["command"], seen["kwargs"] = command, kwargs
        seen["process"] = _FakeProcess(windows)
        return seen["process"]

    monkeypatch.setattr(local_mod.asyncio, "create_subprocess_shell", create_subprocess_shell)
    monkeypatch.setattr(local_mod.os, "killpg", None, raising=False)
    monkeypatch.delattr(local_mod.os, "killpg", raising=False)
    return seen


async def test_a_timeout_ends_the_whole_job_with_no_attribute_error(
    tmp_path: Path, windows: FakeApi, fake_spawn: dict[str, Any]
) -> None:
    result = await LocalBackend(tmp_path).run("ping -t localhost", timeout=0.3)
    assert result.timed_out and result.exit_code == 124
    assert fake_spawn["kwargs"]["creationflags"] == winproc.SPAWN_FLAGS
    assert ("assign", 77, 1000 + 4321) in windows.calls
    assert ("terminate_job", 77) in windows.calls


async def test_a_cancel_ends_the_whole_job(
    tmp_path: Path, windows: FakeApi, fake_spawn: dict[str, Any]
) -> None:
    task = asyncio.ensure_future(LocalBackend(tmp_path).run("ping -t localhost", timeout=30))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("terminate_job", 77) in windows.calls


async def test_the_taskkill_fallback_on_timeout_names_the_exact_pid(
    tmp_path: Path, windows: FakeApi, fake_spawn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    windows.assign_fails = True

    def taskkill(pid: int) -> bool:
        windows.taskkilled.append(pid)  # type: ignore[attr-defined]
        windows.running = False
        return True

    monkeypatch.setattr(winproc, "taskkill_tree", taskkill)
    result = await LocalBackend(tmp_path).run("ping -t localhost", timeout=0.3)
    assert result.timed_out
    assert windows.taskkilled == [4321]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# background: true on Windows
# ---------------------------------------------------------------------------


class _FakeChild:
    def __init__(self) -> None:
        self.pid = 5555
        self.alive = True
        self.stopped = 0

    def running(self) -> bool:
        return self.alive

    @property
    def exit_code(self) -> int | None:
        return None if self.alive else 1

    def stop(self) -> None:
        self.stopped += 1
        self.alive = False


def _ctx(tmp_path: Path, backend: Any) -> Any:
    session = SimpleNamespace(id="s-win", pending_notices=[])
    return SimpleNamespace(session=session, backend=backend, core=None)


async def test_background_on_windows_spawns_directly_and_tracks_the_child(
    tmp_path: Path, windows: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_tools.REGISTRY.clear()
    child = _FakeChild()
    backend = LocalBackend(tmp_path)
    calls: list[tuple[str, str]] = []

    async def spawn_background(command: str, log_path: str) -> _FakeChild:
        calls.append((command, log_path))
        log = tmp_path / log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes("서버 시작\r\n".encode("cp949"))
        return child

    async def no_shell(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("background on Windows must not go through backend.run")

    monkeypatch.setattr(backend, "spawn_background", spawn_background)
    monkeypatch.setattr(backend, "run", no_shell)
    monkeypatch.setattr(winproc, "console_code_page", lambda: "cp949")
    ctx = _ctx(tmp_path, backend)
    started = await process_tools.launch(ctx, "npm run dev")
    assert started.ok and "pid 5555" in started.output
    assert calls and calls[0][0] == "npm run dev" and "nohup" not in calls[0][0]
    [entry] = process_tools.REGISTRY.list("s-win")
    out = await process_tools.process_output(ctx, {"id": entry.id})
    assert "서버 시작" in out.output and "running" in out.output
    listed = await process_tools.process_list(ctx, {})
    assert "running" in listed.output
    killed = await process_tools.process_kill(ctx, {"id": entry.id})
    assert killed.ok and child.stopped == 1
    assert process_tools.REGISTRY.list("s-win") == []
    process_tools.REGISTRY.clear()


async def test_process_tools_name_the_ids_that_exist(tmp_path: Path) -> None:
    process_tools.REGISTRY.clear()
    ctx = _ctx(tmp_path, LocalBackend(tmp_path))
    none = await process_tools.process_output(ctx, {"id": "bg-9"})
    assert "no background processes" in (none.error or "")
    process_tools.REGISTRY.add(
        session_id="s-win", pid=1, command="x", log_path="l", backend_kind="local"
    )
    missing = await process_tools.process_kill(ctx, {})
    assert "bg-1" in (missing.error or "")
    wrong = await process_tools.process_output(ctx, {"id": "bg-9"})
    assert "bg-1" in (wrong.error or "")
    process_tools.REGISTRY.clear()


def test_start_detached_uses_the_shell_with_no_window(
    tmp_path: Path, windows: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class Popen:
        pid = 6000

        def __init__(self, command: str, **kwargs: Any) -> None:
            seen["command"], seen["kwargs"] = command, kwargs

    monkeypatch.setattr(winproc.subprocess, "Popen", Popen)
    child = winproc.start_detached(
        "python -m http.server", cwd=str(tmp_path), env={}, log_path=tmp_path / "l" / "bg.log"
    )
    assert seen["command"] == "python -m http.server"
    assert seen["kwargs"]["shell"] is True
    assert seen["kwargs"]["creationflags"] == winproc.SPAWN_FLAGS
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["kwargs"]["stderr"] is subprocess.STDOUT
    assert child.tree is not None and ("assign", 77, 7000) in windows.calls


# ---------------------------------------------------------------------------
# console windows
# ---------------------------------------------------------------------------

OPENS = [
    ("start notepad", "start"),
    ('start "" python server.py', "start"),
    ("START cmd /c adb logcat", "start"),
    ("cd app && start npm run dev", "start"),
    ("cmd /c start python -m http.server", "start"),
    ("cmd /k echo hi", "cmd /k"),
    ("cmd.exe /q /k build.bat", "cmd /k"),
    ("powershell -NoExit -Command dir", "-NoExit"),
    ("pwsh -noe -c ls", "-NoExit"),
    ("powershell -Command Start-Process notepad", "Start-Process"),
    ("Start-Process python -ArgumentList app.py", "Start-Process"),
    ("saps cmd", "Start-Process"),
    ("wt -d .", "wt"),
    ("conhost cmd", "conhost"),
]

QUIET = [
    "npm start",
    "git start",
    "yarn start",
    "./start.sh",
    "start.sh",
    "start.bat",
    "python start.py",
    "start /b python server.py",
    "Start-Process python -NoNewWindow",
    "Start-Process python -WindowStyle Hidden",
    "echo restart",
    "dir",
    "powershell -NoProfile -Command Get-ChildItem",
    "cmd /c dir",
    "gwt status",
]


@pytest.mark.parametrize(("command", "what"), OPENS)
def test_console_openers_are_detected(command: str, what: str) -> None:
    assert shell_mod.opens_console_window(command) == what


@pytest.mark.parametrize("command", QUIET)
def test_near_misses_are_not(command: str) -> None:
    assert shell_mod.opens_console_window(command) is None


async def test_a_window_command_runs_and_gets_a_note_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.exec.backend import ExecResult

    ran: list[str] = []

    async def run_stream(command: str, **_k: Any) -> ExecResult:
        ran.append(command)
        return ExecResult(exit_code=0, stdout="")

    backend = LocalBackend(tmp_path)
    monkeypatch.setattr(backend, "run_stream", run_stream)
    monkeypatch.setattr(winproc, "enabled", lambda: True)
    ctx = SimpleNamespace(
        session=SimpleNamespace(id="s", user_waiting=None), backend=backend, progress=None
    )
    result = await shell_mod.shell(ctx, {"command": "start notepad"})
    assert ran == ["start notepad"]  # not refused
    assert result.ok and "background: true" in result.output and "`start`" in result.output
    monkeypatch.setattr(winproc, "enabled", lambda: False)
    plain = await shell_mod.shell(ctx, {"command": "start notepad"})
    assert "background: true" not in plain.output


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("codec", "text"),
    [("cp949", "한글 출력입니다"), ("cp932", "日本語の出力です"), ("cp1252", "café – naïve")],
)
def test_code_page_output_is_decoded(codec: str, text: str) -> None:
    data = f"ok: {text}\r\n".encode(codec)
    decoder = winproc.OutputDecoder(codec)
    # Byte by byte, so multi-byte characters are split across reads.
    out = "".join(decoder.decode(data[i : i + 1]) for i in range(len(data)))
    out += decoder.decode(b"", final=True)
    assert out == f"ok: {text}\r\n"


def test_utf8_output_stays_utf8_on_a_korean_windows() -> None:
    decoder = winproc.OutputDecoder("cp949")
    data = "git: 한글 커밋\n".encode()
    out = "".join(decoder.decode(data[i : i + 2]) for i in range(0, len(data), 2))
    assert out + decoder.decode(b"", final=True) == "git: 한글 커밋\n"
    assert not decoder.switched


def test_posix_decoder_is_plain_replacing_utf8() -> None:
    decoder = winproc.OutputDecoder("utf-8")
    assert decoder.decode(b"a\xffb", final=True) == "a\ufffdb"


def test_code_page_names_map_to_codecs() -> None:
    assert winproc._codec_for_code_page(949) == "cp949"
    assert winproc._codec_for_code_page(65001) == "utf-8"
    assert winproc._codec_for_code_page(0) is None


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def test_shell_description_gets_one_windows_sentence() -> None:
    assert tool_descriptions.shell_description("linux") == tool_descriptions.SHELL
    windows_text = tool_descriptions.shell_description("win32")
    assert windows_text.startswith(tool_descriptions.SHELL)
    for word in ("start", "Start-Process", "cmd /k", "-NoExit", "background: true"):
        assert word in windows_text


def test_environment_names_cmd_on_windows() -> None:
    env = {"COMSPEC": r"C:\WINDOWS\system32\cmd.exe"}
    assert environment.shell_name("win32", env).startswith("cmd.exe (commands run as `cmd.exe /c")
    assert environment.shell_name("win32", {}).startswith("cmd.exe")
    assert environment.shell_name("linux", {"SHELL": "/bin/zsh"}) == "zsh"
