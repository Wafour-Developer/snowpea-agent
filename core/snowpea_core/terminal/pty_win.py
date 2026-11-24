"""Windows ConPTY for the terminal holder (M37 §2.1), through ``pywinpty``.

``pywinpty`` is a Windows-only dependency (``pywinpty>=2; sys_platform ==
"win32"``); importing this module elsewhere works, :class:`WinPty` only fails
when started.  The holder puts itself in a job object that kills everything in
it when the holder dies (:func:`contain_self`), so the program never outlives
its holder.

pywinpty reads and writes text: output is re-encoded as UTF-8 and input decoded
with ``surrogateescape``.  Reads block, so they run on a thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

IS_WINDOWS = sys.platform == "win32"
READ_SIZE = 65536


def available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winpty  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def contain_self() -> Any:  # pragma: no cover - Windows only
    """Put this process in a new job with KILL_ON_JOB_CLOSE; returns the handle (keep it)."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Win32 names
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(job)
        return None
    return job


class WinPty:  # pragma: no cover - Windows only
    """One program on one ConPTY; ``on_data(b"")`` signals EOF."""

    def __init__(self) -> None:
        self.pid: int = 0
        self._proc: Any = None
        self._on_data: Callable[[bytes], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exited: asyncio.Future[int] | None = None
        self._eof = False
        self._write_lock = threading.Lock()

    async def start(self, argv: Sequence[str], env: Mapping[str, str], cwd: str,
                    cols: int, rows: int, on_data: Callable[[bytes], None]) -> int:
        from winpty import PtyProcess  # type: ignore[import-not-found]

        self._loop = asyncio.get_running_loop()
        self._on_data = on_data
        self._exited = self._loop.create_future()
        self._proc = PtyProcess.spawn(list(argv), cwd=cwd, env=dict(env), dimensions=(rows, cols))
        self.pid = int(self._proc.pid)
        threading.Thread(target=self._reader, name="pty-read", daemon=True).start()
        return self.pid

    def _reader(self) -> None:
        assert self._loop is not None
        while True:
            try:
                text = self._proc.read(READ_SIZE)
            except EOFError:
                break
            except Exception:  # noqa: BLE001 - a closed console ends the stream
                break
            if text:
                data = text.encode("utf-8", "surrogateescape")
                self._loop.call_soon_threadsafe(self._deliver, data)
            elif not self._proc.isalive():
                break
        code = self._proc.exitstatus
        self._loop.call_soon_threadsafe(self._deliver, b"")
        self._loop.call_soon_threadsafe(self._set_exit, 0 if code is None else int(code))

    def _deliver(self, data: bytes) -> None:
        if self._eof:
            return
        if not data:
            self._eof = True
        if self._on_data is not None:
            self._on_data(data)

    def _set_exit(self, code: int) -> None:
        if self._exited is not None and not self._exited.done():
            self._exited.set_result(code)

    def drain(self) -> None:
        """The reader thread delivers everything before EOF; nothing to do here."""

    def write(self, data: bytes) -> None:
        if self._proc is None or not data:
            return
        text = data.decode("utf-8", "surrogateescape")
        with self._write_lock, contextlib.suppress(Exception):
            self._proc.write(text)

    def resize(self, cols: int, rows: int) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.setwinsize(rows, cols)

    def signal(self, name: str) -> None:
        if self._proc is None:
            return
        if name == "int":
            self.write(b"\x03")
            return
        # Hang-up and anything harder end the program (ConPTY has no signals).
        with contextlib.suppress(Exception):
            self._proc.terminate(force=True)

    async def wait(self) -> int:
        assert self._exited is not None
        return await self._exited

    def close(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.close(force=True)
