"""The terminal holder process (M37 §2.1): ``python -m snowpea_core.terminal.holder``.

One holder per terminal, started detached (its own session on Unix;
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` on Windows) so the terminal
survives a daemon restart or upgrade.  The holder owns the PTY and is the
program's parent; it always drains output and keeps the last 1 MiB, and sends
the exit code only after the last output.

Start-up: the daemon writes one JSON line on stdin —
``{id, argv, env, cwd, cols, rows, token, socket, dir}`` — and reads one JSON
line back on stdout: ``{"ok": true, "pid": <holder>, "child": <program>,
"socket": <address>}`` or ``{"ok": false, "error": …}``.

Link to the core: a Unix socket (mode 0600 in a 0700 directory) or, on
Windows, a loopback TCP port (``tcp:127.0.0.1:<port>``); either way the first
frame must be HELLO carrying the 32-byte token from the meta file, or the
connection is closed.  One core at a time: a newer authenticated connection
replaces the older one.

Frames are ``[kind u8][len u32 LE][payload]``:

======== ============ =======================================================
kind     direction    payload
======== ============ =======================================================
HELLO    core→holder  the token; holder→core: JSON ``{pid, child, alive, exitCode}``
DATA     holder→core  PTY output
INPUT    core→holder  bytes for the PTY
RESIZE   core→holder  JSON ``{cols, rows}``
SIGNAL   core→holder  ``hup`` / ``term`` / ``kill`` / ``int``
EXIT     holder→core  JSON ``{code}``, after the last DATA
TAIL     both         core: request (empty); holder: the buffered tail, then live DATA
BYE      core→holder  the core has the exit: the holder may go
======== ============ =======================================================

When the program has exited the holder writes ``<id>.exit.json`` (and, when no
core took the exit, the tail as ``<id>.tail``) beside the meta file, then exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any

HELLO, DATA, INPUT, RESIZE, SIGNAL, EXIT, TAIL, BYE = range(1, 9)
HEADER = struct.Struct("<BI")
MAX_FRAME = 16 * 1024 * 1024
TAIL_BYTES = 1024 * 1024
#: A core this far behind is dropped; it reconnects and asks for the tail.
MAX_BACKLOG = 8 * 1024 * 1024
#: How long an unauthenticated connection may take to say HELLO.
HELLO_TIMEOUT = 5.0
#: After the exit: how long to wait for the core's BYE before leaving files behind.
BYE_TIMEOUT = 10.0
#: After the exit: how long to keep reading output still in the PTY.
DRAIN_TIMEOUT = 0.2


def frame(kind: int, payload: bytes = b"") -> bytes:
    return HEADER.pack(kind, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readexactly(HEADER.size)
    kind, length = HEADER.unpack(head)
    if length > MAX_FRAME:
        raise ValueError("frame too large")
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


def _new_pty() -> Any:
    if sys.platform == "win32":
        from snowpea_core.terminal.pty_win import WinPty

        return WinPty()
    from snowpea_core.terminal.pty_unix import UnixPty

    return UnixPty()


class Holder:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.id = str(spec["id"])
        self.dir = Path(spec["dir"])
        self.token = bytes.fromhex(str(spec["token"]))
        self.pty = _new_pty()
        self.tail = bytearray()
        self.client: asyncio.StreamWriter | None = None
        self.exit_code: int | None = None
        self.exited = asyncio.Event()
        self.bye = asyncio.Event()
        self._eof = asyncio.Event()
        self.server: asyncio.AbstractServer | None = None
        self.address = ""

    # -- start --------------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        cols = int(self.spec.get("cols") or 160)
        rows = int(self.spec.get("rows") or 48)
        await self._listen()
        child = await self.pty.start(
            [str(a) for a in self.spec["argv"]],
            {str(k): str(v) for k, v in (self.spec.get("env") or {}).items()},
            str(self.spec.get("cwd") or os.getcwd()),
            cols,
            rows,
            self._on_data,
        )
        return {"ok": True, "pid": os.getpid(), "child": child, "socket": self.address}

    async def _listen(self) -> None:
        if sys.platform == "win32":
            self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
            port = self.server.sockets[0].getsockname()[1]
            self.address = f"tcp:127.0.0.1:{port}"
            return
        path = str(self.spec["socket"])
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        old = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(self._serve, path)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.address = path

    # -- PTY ----------------------------------------------------------------
    def _on_data(self, data: bytes) -> None:
        if not data:
            self._eof.set()
            return
        self.tail += data
        if len(self.tail) > TAIL_BYTES:
            del self.tail[: len(self.tail) - TAIL_BYTES]
        self._send(DATA, data)

    def _send(self, kind: int, payload: bytes = b"") -> None:
        writer = self.client
        if writer is None or writer.is_closing():
            return
        if writer.transport.get_write_buffer_size() > MAX_BACKLOG:
            # A stalled core never blocks the program: drop it, it re-attaches.
            writer.close()
            self.client = None
            return
        writer.write(frame(kind, payload))

    # -- core link ----------------------------------------------------------
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            kind, payload = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
        except (TimeoutError, asyncio.IncompleteReadError, ValueError, OSError):
            writer.close()
            return
        if kind != HELLO or not hmac.compare_digest(payload, self.token):
            writer.close()
            return
        hello = {"pid": os.getpid(), "child": self.pty.pid, "alive": not self.exited.is_set(),
                 "exitCode": self.exit_code}
        writer.write(frame(HELLO, json.dumps(hello).encode()))
        try:
            while True:
                kind, payload = await read_frame(reader)
                if kind == INPUT:
                    self.pty.write(payload)
                elif kind == RESIZE:
                    size = json.loads(payload or b"{}")
                    self.pty.resize(int(size["cols"]), int(size["rows"]))
                elif kind == SIGNAL:
                    self.pty.signal(payload.decode("ascii", "replace"))
                elif kind == TAIL:
                    # The stream starts here: the tail, then live DATA, nothing
                    # twice and nothing missed.  This connection replaces any older one.
                    if self.client is not None and self.client is not writer:
                        self.client.close()
                    self.client = writer
                    writer.write(frame(TAIL, bytes(self.tail)))
                    if self.exited.is_set():
                        writer.write(frame(EXIT, json.dumps({"code": self.exit_code}).encode()))
                elif kind == BYE:
                    self.bye.set()
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        finally:
            if self.client is writer:
                self.client = None
            writer.close()

    # -- run ----------------------------------------------------------------
    async def run(self) -> int:
        code = await self.pty.wait()
        # The exit code goes out only after the last output.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._eof.wait(), DRAIN_TIMEOUT)
        self.pty.drain()
        self.exit_code = code
        self.exited.set()
        self._write_json(f"{self.id}.exit.json", {"code": code, "at": time.time()})
        self._send(EXIT, json.dumps({"code": code}).encode())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.bye.wait(), BYE_TIMEOUT)
        if not self.bye.is_set():
            self._write_bytes(f"{self.id}.tail", bytes(self.tail))
        self.pty.close()
        if self.server is not None:
            self.server.close()
        if self.address and not self.address.startswith("tcp:"):
            with contextlib.suppress(OSError):
                os.unlink(self.address)
        return code

    def _write_bytes(self, name: str, data: bytes) -> None:
        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _write_json(self, name: str, value: Any) -> None:
        self._write_bytes(name, json.dumps(value).encode())


async def _amain(spec: dict[str, Any], out: Any) -> int:
    holder = Holder(spec)
    try:
        ready = await holder.start()
    except Exception as exc:  # noqa: BLE001 - reported to the daemon, then we exit
        out.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
        out.flush()
        return 1
    out.write(json.dumps(ready) + "\n")
    out.flush()
    # The daemon stops reading once it has the ready line (and may be gone
    # later): nothing else is written to that pipe.
    with contextlib.suppress(OSError):
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.close(devnull)
    return await holder.run()


def main() -> int:
    line = sys.stdin.readline()
    try:
        spec = json.loads(line)
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad spec"}), flush=True)
        return 2
    out = sys.stdout
    job = None
    if sys.platform == "win32":  # pragma: no cover - Windows only
        from snowpea_core.terminal.pty_win import contain_self

        job = contain_self()
    try:
        code = asyncio.run(_amain(spec, out))
    finally:
        del job
    return 0 if code is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
