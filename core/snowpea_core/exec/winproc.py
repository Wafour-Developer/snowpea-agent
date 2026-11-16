"""Child processes on Windows: no console windows, whole-tree termination.

On POSIX a tool command runs in its own session and is stopped with
``killpg``.  Windows has neither, so this module gives every process snowpea
starts for a tool, a hook, a language server or an audio helper the same two
guarantees there:

* **No console window.**  It is created with ``CREATE_NO_WINDOW |
  CREATE_NEW_PROCESS_GROUP`` (stdio piped by the caller), so a console program
  never opens a visible window of its own.

* **The whole tree dies.**  Right after it starts it is assigned to a Job
  Object of its own with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and no
  breakaway; everything it starts lands in the same job.  Stopping it is
  ``TerminateJobObject``: the command, the npm ``.cmd`` shim and the node it
  launched, all at once.

Daemon lifetime: the daemon is the only holder of every job handle (they are
not inheritable).  When the daemon exits — cleanly or killed — the kernel
closes them and ``KILL_ON_JOB_CLOSE`` ends every tree still inside, so nothing
snowpea started for a tool outlives it.  That is the daemon-level job: one job
per child, all owned by the daemon, rather than one shared job, so a single
command can be stopped without touching the others.  On Windows 8 and later a
job may nest inside the job the daemon itself runs in (a terminal or a
service manager may put it in one); on older Windows that assignment fails
and the child is stopped with ``taskkill /PID <pid> /T /F`` instead — only
for the exact pid snowpea started, while a handle to that process is still
held, so the pid cannot have been reused for someone else's process.

Assignment race: the child is assigned to its job immediately after
``CreateProcess`` returns, not created suspended.  Between the two, the child
would have to start a grandchild of its own within microseconds for that
grandchild to escape; ``cmd.exe`` and every interpreter take milliseconds to
start.  A process that did escape is still stopped by the ``taskkill``
fallback path only if the job could not be created at all, so the window is
documented rather than closed with an undocumented ``NtResumeProcess``.

Never by name, never by enumeration: every call here acts on a pid or a job
this module created.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

log = logging.getLogger("snowpea.exec.winproc")

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
#: What every snowpea-started child gets on Windows.
SPAWN_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102

#: Seconds the ``taskkill`` fallback may take.
TASKKILL_TIMEOUT = 15.0

#: Attribute an asyncio/subprocess process object carries its tree under.
_TREE_ATTR = "_snowpea_tree"


def enabled() -> bool:
    """True when this module changes anything: the daemon runs on Windows."""
    return sys.platform == "win32"


def spawn_kwargs() -> dict[str, Any]:
    """Extra keyword arguments for ``subprocess``/``asyncio`` spawns.

    ``{"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}`` on
    Windows; nothing on POSIX, where every call keeps exactly what it had.
    """
    return {"creationflags": SPAWN_FLAGS} if enabled() else {}


# ---------------------------------------------------------------------------
# the kernel32 calls, behind one small object tests replace
# ---------------------------------------------------------------------------


class _Kernel32:
    """Pure-ctypes Job Object API.  Raises ``AttributeError`` off Windows."""

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = handle
        kernel32.SetInformationJobObject.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, dword]
        kernel32.SetInformationJobObject.restype = boolean
        kernel32.QueryInformationJobObject.argtypes = [
            handle,
            ctypes.c_int,
            ctypes.c_void_p,
            dword,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = boolean
        kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
        kernel32.AssignProcessToJobObject.restype = boolean
        kernel32.TerminateJobObject.argtypes = [handle, ctypes.c_uint]
        kernel32.TerminateJobObject.restype = boolean
        kernel32.OpenProcess.argtypes = [dword, boolean, dword]
        kernel32.OpenProcess.restype = handle
        kernel32.WaitForSingleObject.argtypes = [handle, dword]
        kernel32.WaitForSingleObject.restype = dword
        kernel32.CloseHandle.argtypes = [handle]
        kernel32.CloseHandle.restype = boolean
        self._k = kernel32

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32),
                ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32),
                ("TotalTerminatedProcesses", ctypes.c_uint32),
            ]

        self._extended = ExtendedLimit
        self._accounting = Accounting

    def _error(self, what: str) -> OSError:
        code = self._ctypes.get_last_error()  # type: ignore[attr-defined]
        return OSError(code, f"{what} failed (winerror {code})")

    def create_job(self) -> int:
        """A new job that kills its members when its last handle closes; no breakaway."""
        job = self._k.CreateJobObjectW(None, None)
        if not job:
            raise self._error("CreateJobObject")
        info = self._extended()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
        )
        if not ok:
            error = self._error("SetInformationJobObject")
            self._k.CloseHandle(job)
            raise error
        return int(job)

    def open_process(self, pid: int) -> int:
        access = PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION
        handle = self._k.OpenProcess(access | SYNCHRONIZE, False, pid)
        if not handle:
            raise self._error("OpenProcess")
        return int(handle)

    def assign(self, job: int, process: int) -> None:
        if not self._k.AssignProcessToJobObject(job, process):
            raise self._error("AssignProcessToJobObject")

    def terminate_job(self, job: int, code: int = 1) -> bool:
        return bool(self._k.TerminateJobObject(job, code))

    def active_processes(self, job: int) -> int | None:
        info = self._accounting()
        ok = self._k.QueryInformationJobObject(
            job,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            None,
        )
        return int(info.ActiveProcesses) if ok else None

    def is_running(self, process: int) -> bool:
        return int(self._k.WaitForSingleObject(process, 0)) == WAIT_TIMEOUT

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)


#: The API in use: ``None`` until first needed, ``False`` when unavailable.
#: Tests put a fake object here.
_API: Any = None


def _api() -> Any:
    global _API
    if _API is None:
        try:
            _API = _Kernel32()
        except (AttributeError, OSError, ImportError):
            _API = False
    return _API or None


def _taskkill_path() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "System32" / "taskkill.exe")


def taskkill_tree(pid: int) -> bool:
    """``taskkill /PID <pid> /T /F`` for exactly ``pid``, with no console window.

    Only ever called for a pid this module started and still holds a handle
    to (see :meth:`ProcessTree.terminate`).
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed system binary, a pid we started
            [_taskkill_path(), "/PID", str(int(pid)), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=TASKKILL_TIMEOUT,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("taskkill for pid %s failed: %s", pid, exc)
        return False
    return done.returncode == 0


#: Every tree whose handles are still open, so daemon stop can end them all.
_LIVE: set[ProcessTree] = set()
_LIVE_LOCK = threading.Lock()


class ProcessTree:
    """One process snowpea started on Windows and everything it starts."""

    def __init__(self, pid: int, *, job: int | None, process: int | None, api: Any) -> None:
        self.pid = pid
        self._job = job
        self._process = process
        self._api = api
        self._lock = threading.Lock()
        self.closed = False

    @property
    def contained(self) -> bool:
        """True when the tree is in a job (otherwise only ``taskkill`` can stop it)."""
        return self._job is not None

    def running(self) -> bool:
        """True while the process snowpea started (not its children) runs."""
        if self.closed or self._process is None:
            return False
        try:
            return bool(self._api.is_running(self._process))
        except OSError:
            return False

    def active(self) -> int | None:
        """Processes still in the job (the started one and what it left behind)."""
        if self.closed or self._job is None:
            return None
        try:
            return self._api.active_processes(self._job)
        except OSError:
            return None

    def terminate(self) -> None:
        """End the process and everything it started, then close the handles.

        Job: ``TerminateJobObject``.  No job: ``taskkill /T /F`` for exactly
        our pid, only while our handle says it still runs (the handle pins the
        pid, so it cannot belong to another process).
        """
        with self._lock:
            if self.closed:
                return
            ended = False
            if self._job is not None:
                try:
                    ended = bool(self._api.terminate_job(self._job, 1))
                except OSError:
                    ended = False
            if not ended and self.running():
                taskkill_tree(self.pid)
            self._close_locked()

    def release(self, *, keep_leftovers: bool = False) -> None:
        """The process ended: let go of the handles.

        Closing the job kills whatever is still in it (``KILL_ON_JOB_CLOSE``).
        With ``keep_leftovers`` a job that still has members stays open — as on
        POSIX, what a finished command left behind keeps running — until
        :func:`terminate_all` or the daemon's exit ends it.
        """
        with self._lock:
            if self.closed:
                return
            if keep_leftovers and (self.active() or 0) > 0:
                return
            self._close_locked()

    def _close_locked(self) -> None:
        for handle in (self._job, self._process):
            if handle is not None:
                with contextlib.suppress(OSError):
                    self._api.close(handle)
        self._job = None
        self._process = None
        self.closed = True
        with _LIVE_LOCK:
            _LIVE.discard(self)


def _sweep() -> None:
    """Close parked jobs whose leftovers have all exited."""
    with _LIVE_LOCK:
        parked = list(_LIVE)
    for tree in parked:
        if not tree.running() and tree.active() == 0:
            tree.release()


def contain(pid: int) -> ProcessTree | None:
    """Put the freshly started ``pid`` in a job of its own (Windows only).

    Returns ``None`` off Windows.  On Windows it always returns a tree: one in
    a job, or — when the job API is unavailable or the assignment is refused
    — one that can only be stopped with ``taskkill`` for this exact pid.
    """
    if not enabled() or pid <= 0:
        return None
    api = _api()
    if api is None:
        return None
    _sweep()
    try:
        process = api.open_process(pid)
    except OSError as exc:
        log.info("cannot open pid %s for containment: %s", pid, exc)
        return None
    job: int | None = None
    try:
        job = api.create_job()
        api.assign(job, process)
    except OSError as exc:
        log.info("no job for pid %s (falling back to taskkill): %s", pid, exc)
        if job is not None:
            with contextlib.suppress(OSError):
                api.close(job)
        job = None
    tree = ProcessTree(pid, job=job, process=process, api=api)
    with _LIVE_LOCK:
        _LIVE.add(tree)
    return tree


def attach(process: Any) -> ProcessTree | None:
    """:func:`contain` ``process.pid`` and remember the tree on the object."""
    tree = contain(int(getattr(process, "pid", 0) or 0))
    if tree is not None:
        with contextlib.suppress(AttributeError):
            setattr(process, _TREE_ATTR, tree)
    return tree


def tree_of(process: Any) -> ProcessTree | None:
    tree = getattr(process, _TREE_ATTR, None)
    return tree if isinstance(tree, ProcessTree) else None


def release(process: Any, *, keep_leftovers: bool = False) -> None:
    """:meth:`ProcessTree.release` for ``process``; nothing without a tree."""
    tree = tree_of(process)
    if tree is not None:
        tree.release(keep_leftovers=keep_leftovers)


async def kill(process: Any) -> None:
    """Stop ``process`` for good, on every platform, without ever raising.

    Windows with a tree: the whole tree (job, or ``taskkill`` for the pid).
    Otherwise ``process.kill()``, with the "already gone" errors
    (``ProcessLookupError``, and ``PermissionError`` from a process that is
    exiting) swallowed — asyncio raises them when the process ended a moment
    earlier, and a cleanup must never become the turn's error.
    """
    tree = tree_of(process)
    if tree is not None:
        try:
            await asyncio.to_thread(tree.terminate)
        except Exception:  # noqa: BLE001 - cleanup must not raise
            log.warning("could not end the tree of pid %s", tree.pid, exc_info=True)
        return
    kill_now(process)


def kill_now(process: Any) -> None:
    """Synchronous ``process.kill()`` that never raises (see :func:`kill`)."""
    if getattr(process, "returncode", None) is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:  # noqa: BLE001 - cleanup must not raise
        log.warning("could not kill pid %s", getattr(process, "pid", "?"), exc_info=True)


def terminate_now(process: Any) -> None:
    """``process.terminate()`` that never raises; the whole tree on Windows."""
    if getattr(process, "returncode", None) is not None:
        return
    tree = tree_of(process)
    if tree is not None:
        try:
            tree.terminate()
        except Exception:  # noqa: BLE001 - cleanup must not raise
            log.warning("could not end the tree of pid %s", tree.pid, exc_info=True)
        return
    try:
        process.terminate()
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:  # noqa: BLE001 - cleanup must not raise
        log.warning("could not terminate pid %s", getattr(process, "pid", "?"), exc_info=True)


def terminate_all() -> int:
    """End every tree still held (daemon stop).  Returns how many there were."""
    with _LIVE_LOCK:
        trees = list(_LIVE)
    for tree in trees:
        try:
            tree.terminate()
        except Exception:  # noqa: BLE001 - shutdown must finish
            log.warning("could not end the tree of pid %s", tree.pid, exc_info=True)
    return len(trees)


# ---------------------------------------------------------------------------
# background commands
# ---------------------------------------------------------------------------


@dataclass
class DetachedChild:
    """A ``background: true`` command started directly on Windows."""

    popen: subprocess.Popen[bytes]
    tree: ProcessTree | None

    @property
    def pid(self) -> int:
        return int(self.popen.pid)

    def running(self) -> bool:
        return self.popen.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.popen.poll()

    def stop(self) -> None:
        """End the command and everything it started; never raises."""
        if self.tree is not None:
            try:
                self.tree.terminate()
            except Exception:  # noqa: BLE001 - cleanup must not raise
                log.warning("could not end background pid %s", self.pid, exc_info=True)
        else:
            if self.popen.poll() is None:
                taskkill_tree(self.pid)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            self.popen.wait(timeout=5)


def start_detached(
    command: str, *, cwd: str, env: dict[str, str], log_path: Path
) -> DetachedChild:
    """Run ``command`` through the shell with output to ``log_path`` (Windows).

    No ``nohup``, no ``sh``: ``cmd.exe /c <command>`` with no window, stdin
    closed, stdout and stderr appended to the log, in a job of its own.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as sink:
        popen = subprocess.Popen(  # noqa: S602 - the shell tool's own command, approved
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            **spawn_kwargs(),
        )
    return DetachedChild(popen=popen, tree=contain(popen.pid))


# ---------------------------------------------------------------------------
# a bare python3 in third-party commands
# ---------------------------------------------------------------------------

#: Program names a plugin written on macOS/Linux uses for "a Python".
BARE_PYTHON_NAMES = frozenset({"python3", "python", "python3.exe", "python.exe"})


def is_store_alias(path: str, environ: Mapping[str, str] | None = None) -> bool:
    """True for a Microsoft Store App Execution Alias (``…\\Microsoft\\WindowsApps\\x.exe``).

    Without the Store Python installed that alias is a stub that prints
    "Python was not found", exits 9009 and opens a console window of its
    own; even with it, it is a zero-byte reparse point a piped child should
    not rely on.  Judged by folder, so it can be tested anywhere.
    """
    env = os.environ if environ is None else environ
    parent = str(PureWindowsPath(path).parent).rstrip("\\").lower()
    local = env.get("LOCALAPPDATA") or ""
    if local:
        alias_dir = str(PureWindowsPath(local) / "Microsoft" / "WindowsApps").lower()
        if parent == alias_dir:
            return True
    return parent.endswith("\\microsoft\\windowsapps")


def python_substitute(
    program: str,
    *,
    which: Callable[..., str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when ``program`` should become the core's own interpreter (Windows rule).

    ``program`` must be exactly one of :data:`BARE_PYTHON_NAMES` (any case, no
    directory).  It is kept when it resolves on PATH to something other than
    a Store alias; it is replaced when it does not resolve or resolves only
    to the alias.
    """
    if program.lower() not in BARE_PYTHON_NAMES:
        return False
    env = os.environ if environ is None else environ
    finder = which or shutil.which
    try:
        found = finder(program, path=env.get("PATH"))
    except (OSError, ValueError, TypeError):
        found = None
    return not found or is_store_alias(str(found), env)


_LOGGED_PROGRAMS: set[str] = set()


def python_program(program: str, *, platform: str | None = None) -> str:
    """An argv[0] with the Windows rule applied (:func:`python_substitute`).

    For third-party commands run as an argv (MCP stdio servers): a bare
    ``python3`` that is missing or only the Store alias becomes the core's
    interpreter.  A frozen core has no interpreter to hand out, so there the
    name is kept (and the failure is reported as the server's own).
    """
    if (platform or sys.platform) != "win32" or getattr(sys, "frozen", False):
        return program
    if not python_substitute(program):
        return program
    if program not in _LOGGED_PROGRAMS:
        _LOGGED_PROGRAMS.add(program)
        log.info("program %r is not a usable Python here; using %s", program, sys.executable)
    return sys.executable


# ---------------------------------------------------------------------------
# output decoding
# ---------------------------------------------------------------------------


def _codec_for_code_page(page: int) -> str | None:
    if page <= 0:
        return None
    name = "utf-8" if page == 65001 else f"cp{page}"
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def console_code_page() -> str:
    """The codec a console program on this machine writes in (Windows).

    ``GetConsoleOutputCP`` when the daemon has a console, else ``GetOEMCP``
    (what ``cmd.exe`` and most console programs use when piped), else the
    ANSI code page; ``utf-8`` when none of these is known.
    """
    if not enabled():
        return "utf-8"
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for probe in ("GetConsoleOutputCP", "GetOEMCP", "GetACP"):
            codec = _codec_for_code_page(int(getattr(kernel32, probe)()))
            if codec is not None:
                return codec
    except (AttributeError, OSError, ValueError):
        pass
    return "utf-8"


class OutputDecoder:
    """Incremental decoder for a child's output: UTF-8 first, then the code page.

    Programs on a Korean or Japanese Windows mix: ``cmd.exe`` and most native
    tools write the OEM code page (CP949, CP932), while git, node and Python in
    UTF-8 mode write UTF-8.  The output is read as UTF-8 until a byte sequence
    proves it is not; from then on (for this stream) it is decoded with
    ``fallback``, the bytes that failed included.  ASCII reads the same either
    way.  With ``fallback`` ``utf-8`` this is the plain replacing UTF-8
    decoder POSIX has always used.
    """

    def __init__(self, fallback: str = "utf-8") -> None:
        self.fallback = codecs.lookup(fallback).name
        self._pending = b""
        self._switched = self.fallback == "utf-8"
        self._decoder = codecs.getincrementaldecoder(
            "utf-8" if self._switched else self.fallback
        )("replace")

    @property
    def switched(self) -> bool:
        """True once the stream is being decoded with the fallback code page."""
        return self._switched and self.fallback != "utf-8"

    def decode(self, data: bytes, final: bool = False) -> str:
        if self._switched:
            return self._decoder.decode(data, final)
        buffer = self._pending + data
        self._pending = b""
        try:
            return buffer.decode("utf-8")
        except UnicodeDecodeError as exc:
            tail = buffer[exc.start :]
            if not final and exc.reason == "unexpected end of data" and len(tail) < 4:
                # A character split across two reads; wait for the rest.
                self._pending = tail
                return buffer[: exc.start].decode("utf-8")
        self._switched = True
        return self._decoder.decode(buffer, final)


def output_decoder() -> OutputDecoder:
    """The decoder for a child's piped output on this platform."""
    return OutputDecoder(console_code_page() if enabled() else "utf-8")


def decode_output(data: bytes) -> str:
    """Decode a complete output in one go (see :class:`OutputDecoder`)."""
    return output_decoder().decode(data, final=True)


__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "SPAWN_FLAGS",
    "DetachedChild",
    "OutputDecoder",
    "ProcessTree",
    "attach",
    "console_code_page",
    "contain",
    "decode_output",
    "enabled",
    "kill",
    "kill_now",
    "output_decoder",
    "release",
    "spawn_kwargs",
    "start_detached",
    "taskkill_tree",
    "terminate_all",
    "terminate_now",
    "tree_of",
]
