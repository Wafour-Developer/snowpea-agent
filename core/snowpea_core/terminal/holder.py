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
Windows, a loopback TCP port (``tcp:127.0.0.1:<port>``); either way both sides
prove they hold the 32-byte token from the meta file (a nonce challenge, see
:func:`proof`) before anything else, or the connection is closed.  The token
itself never crosses the link.  One core at a time: a newer authenticated connection
replaces the older one.

Frames are ``[kind u8][len u32 LE][payload]``:

======== ============ =======================================================
kind     direction    payload
======== ============ =======================================================
HELLO    core→holder  its nonce; holder→core (after AUTH): JSON ``{pid, child, alive, exitCode}``
CHALLENGE holder→core the holder's nonce + its proof
AUTH     core→holder  the core's proof
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
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HELLO, DATA, INPUT, RESIZE, SIGNAL, EXIT, TAIL, BYE, CHALLENGE, AUTH = range(1, 11)
NONCE_BYTES = 32


def proof(token: bytes, role: bytes, first: bytes, second: bytes) -> bytes:
    """HMAC-SHA256 of the two nonces under the terminal token, per side (``holder``/``core``).

    The handshake (security review L2): the core sends HELLO with its nonce;
    the holder answers CHALLENGE = its nonce + ``proof(token, b"holder", nc, nh)``;
    the core checks it and sends AUTH = ``proof(token, b"core", nh, nc)``.  Neither
    side ever sends the token, and each proves it holds it before any stream.
    """
    return hmac.new(token, role + first + second, hashlib.sha256).digest()
HEADER = struct.Struct("<BI")
MAX_FRAME = 16 * 1024 * 1024
TAIL_BYTES = 1024 * 1024
#: A core this far behind is dropped; it reconnects and asks for the tail.
MAX_BACKLOG = 8 * 1024 * 1024
#: How long an unauthenticated connection may take to say HELLO.
HELLO_TIMEOUT = 5.0
#: After the exit: how long to wait for the core's BYE before leaving files behind.
BYE_TIMEOUT = 10.0
#: After the main program exits: how long to keep the PTY open for trailing output.
DRAIN_MAX = 600.0
#: Poll interval while waiting for session children or EOF.
DRAIN_POLL = 0.05
#: How often the holder checks that its terminal still exists: its home, its
#: meta file and (Unix) its socket.  Gone means nobody can ever reach it again,
#: so it ends its program (the kill sequence) and itself.
WATCH_INTERVAL = 5.0
#: Kill sequence steps (same as the daemon's, §2.1).
KILL_STEPS: tuple[tuple[str, float], ...] = (("hup", 0.5), ("term", 0.5), ("kill", 2.0))


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
        #: The terminal is gone (home, meta or socket removed): end without traces.
        self.orphaned = False
        self._ending: asyncio.Future[None] | None = None
        self._child_done = False

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
        self._eof.clear()
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
            kind, core_nonce = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
            if kind != HELLO or len(core_nonce) != NONCE_BYTES:
                raise ValueError("bad hello")
            holder_nonce = secrets.token_bytes(NONCE_BYTES)
            writer.write(frame(CHALLENGE, holder_nonce + proof(
                self.token, b"holder", core_nonce, holder_nonce)))
            kind, answer = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
            expected = proof(self.token, b"core", holder_nonce, core_nonce)
            if kind != AUTH or not hmac.compare_digest(answer, expected):
                raise ValueError("bad auth")
        except (TimeoutError, asyncio.IncompleteReadError, ValueError, OSError):
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
        self._install_terminate()
        watch = asyncio.ensure_future(self._watch())
        try:
            code = await self.pty.wait()
            self._child_done = True
        finally:
            watch.cancel()
        # The exit code goes out only after the last output (and session children).
        capped = await self._drain_after_exit()
        if capped:
            with contextlib.suppress(OSError):
                self.pty.signal("hup")
        self.exit_code = code
        self.exited.set()
        if not self.orphaned:
            self._write_json(f"{self.id}.exit.json", {"code": code, "at": time.time()})
        self._send(EXIT, json.dumps({"code": code}).encode())
        if not self.orphaned:
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

    def _pty_session_busy(self) -> bool:
        busy = getattr(self.pty, "session_busy", None)
        return bool(busy()) if busy is not None else False

    async def _drain_after_exit(self) -> bool:
        """Keep reading while the session still has children or the PTY is not at EOF.

        Returns True when the cap was hit (SIGHUP is sent before close).
        """
        max_wait = float(self.spec.get("drainMax") or DRAIN_MAX)
        poll = float(self.spec.get("drainPoll") or DRAIN_POLL)
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            self.pty.drain()
            if self._eof.is_set() and not self._pty_session_busy():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._eof.wait(), min(poll, remaining))
            except TimeoutError:
                pass
        capped = time.monotonic() >= deadline and (
            self._pty_session_busy() or not self._eof.is_set()
        )
        if capped:
            log.warning("terminal %s: drain timed out after %.0fs", self.id, max_wait)
        self.pty.drain()
        return capped

    # -- ending without a core ----------------------------------------------
    def _install_terminate(self) -> None:
        """SIGTERM / SIGINT to the holder: end the program (kill sequence), then go.

        The daemon never signals holders on its own stop (terminals outlive
        it); this is for ``snowpea daemon stop --and-terminals`` and cleanup.
        """
        if sys.platform == "win32":  # pragma: no cover - no POSIX signals there
            return
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.terminate)

    def terminate(self, *, orphaned: bool = False) -> None:
        if orphaned:
            self.orphaned = True
        if self._ending is None:
            self._ending = asyncio.ensure_future(self._kill_sequence())

    async def _kill_sequence(self) -> None:
        self.bye.set()  # nobody waits for the exit: do not linger after it
        for name, wait in KILL_STEPS:
            if self.exited.is_set():
                return
            self.pty.signal(name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._child_gone(), wait)
                return

    async def _child_gone(self) -> None:
        while not self._child_done:
            await asyncio.sleep(0.02)

    def gone(self) -> bool:
        """The terminal no longer exists for anyone: home, meta file or socket removed."""
        if not self.dir.is_dir() or not (self.dir / f"{self.id}.json").exists():
            return True
        return bool(self.address and not self.address.startswith("tcp:")
                    and not os.path.exists(self.address))

    async def _watch(self) -> None:
        interval = float(self.spec.get("watchInterval") or WATCH_INTERVAL)
        while True:
            await asyncio.sleep(interval)
            if self.gone():
                self.terminate(orphaned=True)
                return

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
