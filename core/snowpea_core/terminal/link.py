"""The core's side of a holder (M37 §2.1): spawn it detached, then talk to it.

:func:`spawn_holder` starts ``python -m snowpea_core.terminal.holder`` (or the
frozen core's ``--terminal-holder``) in its own session / detached process
group, hands it the launch spec on stdin and returns its ready line.  It uses
:class:`subprocess.Popen`, never an asyncio subprocess: asyncio kills a child
it still tracks when its loop closes, and a holder must outlive the daemon.

:class:`HolderLink` is one authenticated connection to a holder.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from snowpea_core.terminal.holder import (
    BYE,
    DATA,
    EXIT,
    HELLO,
    INPUT,
    RESIZE,
    SIGNAL,
    TAIL,
    frame,
    read_frame,
)

IS_WINDOWS = sys.platform == "win32"
#: Longest Unix socket path we use as is (sun_path is 104 bytes on macOS).
MAX_SOCKET_PATH = 100
SPAWN_TIMEOUT = 20.0
HELLO_TIMEOUT = 5.0


class HolderError(Exception):
    """The holder could not be started or refused the connection."""


def terminals_dir(home: Path) -> Path:
    folder = Path(home) / "terminals"
    folder.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(folder, 0o700)
    return folder


def socket_path(home: Path, terminal_id: str) -> str:
    """``$SNOWPEA_HOME/terminals/<id>.sock``, or a short owner-only temp path when too long."""
    path = terminals_dir(home) / f"{terminal_id}.sock"
    if len(str(path)) <= MAX_SOCKET_PATH:
        return str(path)
    tag = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:8]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    short = Path(tempfile.gettempdir()) / f"snowpea-term-{uid}"
    short.mkdir(mode=0o700, exist_ok=True)
    os.chmod(short, 0o700)
    return str(short / f"{tag}-{terminal_id}.sock")


def holder_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--terminal-holder"]
    return [sys.executable, "-m", "snowpea_core.terminal.holder"]


def _spawn_kwargs() -> dict[str, Any]:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        from snowpea_core.win_detach import DETACH_FLAGS

        return {"creationflags": DETACH_FLAGS}
    return {"start_new_session": True}


def _start(command: list[str], folder: Path, log_name: str, spec: dict[str, Any]) -> dict[str, Any]:
    log_path = folder / log_name
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    kwargs = _spawn_kwargs()
    try:
        try:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=fd,
                cwd=str(folder), close_fds=True, **kwargs,
            )
        except OSError:
            if not IS_WINDOWS:
                raise
            # A job that forbids breakaway: start without it.
            from snowpea_core.win_detach import CREATE_BREAKAWAY_FROM_JOB

            kwargs["creationflags"] &= ~CREATE_BREAKAWAY_FROM_JOB
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=fd,
                cwd=str(folder), close_fds=True, **kwargs,
            )
    finally:
        os.close(fd)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write((json.dumps(spec) + "\n").encode())
        proc.stdin.close()
        line = proc.stdout.readline()
    except OSError as exc:
        line = json.dumps({"ok": False, "error": str(exc)}).encode()
    finally:
        proc.stdout.close()
    try:
        ready = json.loads(line or b"{}")
    except ValueError:
        ready = {}
    if not ready.get("ok"):
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)
        raise HolderError(str(ready.get("error") or "the terminal holder did not start"))
    ready["_popen"] = proc
    return ready


async def spawn_holder(
    home: Path,
    terminal_id: str,
    *,
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: str,
    cols: int,
    rows: int,
    command: list[str] | None = None,
    watch_interval: float | None = None,
) -> dict[str, Any]:
    """Start a holder; returns ``{pid, child, socket, token}``."""
    folder = terminals_dir(home)
    token = secrets.token_hex(32)
    spec = {
        "id": terminal_id,
        "argv": list(argv),
        "env": dict(env),
        "cwd": cwd,
        "cols": cols,
        "rows": rows,
        "token": token,
        "socket": socket_path(home, terminal_id),
        "dir": str(folder),
        "watchInterval": watch_interval,
    }
    ready = await asyncio.wait_for(
        asyncio.to_thread(_start, command or holder_command(), folder, f"{terminal_id}.log", spec),
        SPAWN_TIMEOUT,
    )
    _remember(ready.pop("_popen", None))
    return {"pid": int(ready["pid"]), "child": int(ready["child"]), "socket": str(ready["socket"]),
            "token": token}


#: Holders this daemon started: kept so a running one is never garbage
#: collected (a ResourceWarning), and polled so an exited one is reaped.
_STARTED: list[subprocess.Popen[bytes]] = []


def _remember(proc: subprocess.Popen[bytes] | None) -> None:
    reap()
    if proc is not None:
        _STARTED.append(proc)


def reap() -> None:
    """Collect the holders this daemon started that have exited (no zombies)."""
    _STARTED[:] = [p for p in _STARTED if p.poll() is None]


class HolderLink:
    """One authenticated connection to a holder; callbacks run on the event loop.

    ``on_data(data, tail)`` gets the reattach tail once (``tail=True``) and then
    live output; ``on_exit(code)`` comes after the last output; ``on_close()``
    when the connection drops.
    """

    def __init__(
        self,
        address: str,
        token: str,
        *,
        on_data: Callable[[bytes, bool], None],
        on_exit: Callable[[int | None], None],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.address = address
        self.token = token
        self.on_data = on_data
        self.on_exit = on_exit
        self.on_close = on_close
        self.hello: dict[str, Any] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self.closed = False

    async def connect(self, *, tail: bool = True) -> dict[str, Any]:
        try:
            if self.address.startswith("tcp:"):
                _, host, port = self.address.split(":", 2)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, int(port)), HELLO_TIMEOUT
                )
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.address), HELLO_TIMEOUT
                )
        except (OSError, TimeoutError) as exc:
            raise HolderError(f"cannot reach the terminal holder: {exc}") from exc
        try:
            writer.write(frame(HELLO, bytes.fromhex(self.token)))
            kind, payload = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            writer.close()
            raise HolderError("the terminal holder refused the connection") from exc
        if kind != HELLO:
            writer.close()
            raise HolderError("unexpected first frame from the terminal holder")
        self.hello = json.loads(payload or b"{}")
        self._reader, self._writer = reader, writer
        # The holder streams only after TAIL: the tail is read here, before
        # connect() returns, so the caller's screen is complete at once.
        writer.write(frame(TAIL))
        try:
            while True:
                kind, payload = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
                if kind == TAIL:
                    break
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            writer.close()
            raise HolderError("the terminal holder did not send its tail") from exc
        if tail:
            self.on_data(payload, True)
        self._task = asyncio.ensure_future(self._read_loop())
        return self.hello

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                kind, payload = await read_frame(self._reader)
                if kind == DATA:
                    self.on_data(payload, False)
                elif kind == TAIL:
                    self.on_data(payload, True)
                elif kind == EXIT:
                    code = json.loads(payload or b"{}").get("code")
                    self.on_exit(code if isinstance(code, int) else None)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self.closed = True
            if self._writer is not None:
                self._writer.close()
            if self.on_close is not None:
                self.on_close()

    def _send(self, kind: int, payload: bytes = b"") -> None:
        if self._writer is None or self._writer.is_closing():
            return
        self._writer.write(frame(kind, payload))

    def write(self, data: bytes) -> None:
        if data:
            self._send(INPUT, data)

    def resize(self, cols: int, rows: int) -> None:
        self._send(RESIZE, json.dumps({"cols": cols, "rows": rows}).encode())

    def signal(self, name: str) -> None:
        self._send(SIGNAL, name.encode("ascii"))

    def bye(self) -> None:
        self._send(BYE)

    async def drain(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            with contextlib.suppress(ConnectionError, OSError):
                await self._writer.drain()

    async def close(self) -> None:
        """Drop the connection (the holder and its program keep running)."""
        self.on_close = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self.closed = True
