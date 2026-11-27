"""POSIX PTY for the terminal holder (M37 §2.1): ``openpty`` + ``setsid`` + ``TIOCSCTTY``.

The program runs in its own session with the PTY slave as its controlling
terminal; the master stays non-blocking in the holder's event loop.  Signals go
to the program's process group *and* the terminal's foreground group (a shell
running a job puts that job in its own group).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import signal
import struct
import termios
from collections.abc import Callable, Mapping, Sequence

SIGNALS: dict[str, int] = {
    "hup": signal.SIGHUP,
    "term": signal.SIGTERM,
    "kill": signal.SIGKILL,
    "int": signal.SIGINT,
}
READ_SIZE = 65536


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _controlling_tty() -> None:  # pragma: no cover - runs in the child
    # start_new_session already called setsid(); make the slave (stdin) our tty.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def session_has_processes(sid: int) -> bool:
    """True when any live process still belongs to ``sid`` (Linux ``/proc``)."""
    proc = "/proc"
    if not os.path.isdir(proc):
        return False
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.getsid(pid) == sid:
                return True
        except ProcessLookupError:
            continue
    return False


class UnixPty:
    """One program on one PTY; ``on_data(b"")`` signals EOF."""

    def __init__(self) -> None:
        self.master_fd: int = -1
        self.pid: int = 0
        self._sid: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._on_data: Callable[[bytes], None] | None = None
        self._pending = bytearray()
        self._writing = False
        self._eof = False

    async def start(self, argv: Sequence[str], env: Mapping[str, str], cwd: str,
                    cols: int, rows: int, on_data: Callable[[bytes], None]) -> int:
        master, slave = os.openpty()
        try:
            set_winsize(slave, cols, rows)
            os.set_inheritable(master, False)
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=dict(env),
                start_new_session=True,
                preexec_fn=_controlling_tty,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.master_fd = master
        self.pid = self._proc.pid
        with contextlib.suppress(OSError):
            self._sid = os.getsid(self.pid)
        self._on_data = on_data
        asyncio.get_running_loop().add_reader(master, self._readable)
        return self.pid

    def session_id(self) -> int | None:
        return self._sid

    def session_busy(self) -> bool:
        if self._sid is None:
            return False
        return session_has_processes(self._sid)

    # -- output -------------------------------------------------------------
    def _readable(self) -> None:
        try:
            data = os.read(self.master_fd, READ_SIZE)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                raise
            data = b""
        if not data:
            self._finish_reading()
            return
        if self._on_data is not None:
            self._on_data(data)

    def _finish_reading(self) -> None:
        if self._eof:
            return
        self._eof = True
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(self.master_fd)
        if self._on_data is not None:
            self._on_data(b"")

    def drain(self) -> None:
        """Read whatever is still buffered in the master (after the program exits)."""
        while not self._eof:
            try:
                data = os.read(self.master_fd, READ_SIZE)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                self._finish_reading()
                return
            if self._on_data is not None:
                self._on_data(data)

    # -- input --------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self.master_fd < 0 or not data:
            return
        self._pending += data
        self._flush()

    def _flush(self) -> None:
        loop = asyncio.get_running_loop()
        while self._pending:
            try:
                sent = os.write(self.master_fd, self._pending)
            except BlockingIOError:
                break
            except OSError:
                self._pending.clear()
                break
            del self._pending[:sent]
        if self._pending and not self._writing:
            self._writing = True
            loop.add_writer(self.master_fd, self._writable)
        elif not self._pending and self._writing:
            self._writing = False
            loop.remove_writer(self.master_fd)

    def _writable(self) -> None:
        self._flush()

    # -- control ------------------------------------------------------------
    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                set_winsize(self.master_fd, cols, rows)

    def signal(self, name: str) -> None:
        sig = SIGNALS.get(name)
        if sig is None or self.pid <= 0:
            return
        groups = {self.pid}
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                foreground = os.tcgetpgrp(self.master_fd)
                if foreground > 0:
                    groups.add(foreground)
        for group in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, sig)

    async def wait(self) -> int:
        assert self._proc is not None
        code = await self._proc.wait()
        return 128 - code if code < 0 else code

    def close(self) -> None:
        if self.master_fd >= 0:
            loop = asyncio.get_running_loop()
            with contextlib.suppress(Exception):
                loop.remove_reader(self.master_fd)
            if self._writing:
                with contextlib.suppress(Exception):
                    loop.remove_writer(self.master_fd)
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1
