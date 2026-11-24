"""The terminal report socket: where CLI hooks tell the core what they see.

``snowpea-core hook report`` (see :mod:`.integrations.hook_report`) writes one
JSON line per hook call to ``$SNOWPEA_TERMINAL_REPORT``:

* Unix: a socket at ``$SNOWPEA_HOME/terminals/report.sock`` (the folder 0700,
  the socket 0600), or under a private temp folder when that path is too long
  for ``sun_path``;
* Windows: a named pipe ``\\\\.\\pipe\\snowpea-report-<random>`` (the default
  pipe DACL lets only the creating user, SYSTEM and administrators write).

Every line must carry the terminal's own ``SNOWPEA_TERMINAL_TOKEN``: a report
can only ever speak for the terminal it was started in.  The token is checked
here (constant time) before :class:`ReportSink.apply_hook_report` sees it, and
the same :meth:`ReportServer.handle_payload` serves the ``terminal.report`` RPC.

The listener answers ``{"ok": true|false}`` and closes; the hook never waits
for more than that.
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
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024
READ_TIMEOUT_S = 2.0
#: ``sun_path`` is 104 bytes on macOS, 108 on Linux; keep a margin.
MAX_UNIX_PATH = 100
CLIS = frozenset({"claude", "codex", "cursor", "agy"})
STATES = frozenset({"working", "done", "blocked", "idle"})
KINDS = frozenset({"permission", "question", "auth"})


@dataclass(frozen=True)
class HookReport:
    """One validated hook report for a terminal."""

    cli: str
    event: str
    state: str | None  # working | done | blocked | idle, None = session-only
    kind: str | None  # permission | question | auth
    session_id: str | None
    transcript_path: str | None
    received_at: float  # time.monotonic()


class ReportSink(Protocol):
    """What the terminal manager offers the report listener (T1 implements it)."""

    def report_token(self, terminal_id: str) -> str | None: ...

    def apply_hook_report(self, terminal_id: str, report: HookReport) -> None: ...


class ReportError(ValueError):
    pass


def _clean(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReportError(f"{name} must be a string")
    if len(value) > limit or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ReportError(f"{name} is too long or has control characters")
    return value or None


def parse_report(obj: Any) -> tuple[str, str, HookReport]:
    """``(terminal id, token, report)`` from a decoded line; :class:`ReportError` if bad."""
    if not isinstance(obj, dict):
        raise ReportError("report must be an object")
    terminal_id = _clean(obj.get("terminalId"), "terminalId", 128)
    token = _clean(obj.get("token"), "token", 256)
    if not terminal_id or not token:
        raise ReportError("terminalId and token are required")
    cli = _clean(obj.get("cli"), "cli", 16)
    if cli not in CLIS:
        raise ReportError("unknown cli")
    state = _clean(obj.get("state"), "state", 16)
    if state is not None and state not in STATES:
        raise ReportError("unknown state")
    kind = _clean(obj.get("kind"), "kind", 16)
    if kind is not None and kind not in KINDS:
        raise ReportError("unknown kind")
    report = HookReport(
        cli=cli,
        event=_clean(obj.get("event"), "event", 64) or "",
        state=state,
        kind=kind if state == "blocked" else None,
        session_id=_clean(obj.get("sessionId"), "sessionId", 256),
        transcript_path=_clean(obj.get("transcriptPath"), "transcriptPath", 4096),
        received_at=time.monotonic(),
    )
    return terminal_id, token, report


def unix_socket_path(home: Path) -> Path:
    """``$SNOWPEA_HOME/terminals/report.sock``, or a short private path when that is too long."""
    path = Path(home) / "terminals" / "report.sock"
    if len(os.fsencode(str(path))) <= MAX_UNIX_PATH:
        return path
    digest = hashlib.sha256(os.fsencode(str(Path(home).resolve()))).hexdigest()[:16]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"snowpea-{uid}" / f"report-{digest}.sock"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"{path} is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError(f"{path} belongs to another user")
    os.chmod(path, 0o700)


class _LineProtocol(asyncio.Protocol):
    def __init__(self, server: ReportServer) -> None:
        self.server = server
        self.buffer = bytearray()
        self.transport: asyncio.BaseTransport | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.done = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self.timer = asyncio.get_running_loop().call_later(READ_TIMEOUT_S, self._close)

    def data_received(self, data: bytes) -> None:
        if self.done:
            return
        self.buffer += data
        newline = self.buffer.find(b"\n")
        if newline < 0:
            if len(self.buffer) > MAX_LINE:
                self._answer(False)
            return
        line = bytes(self.buffer[:newline])
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._answer(False)
            return
        self._answer(self.server.handle_payload(obj))

    def eof_received(self) -> bool | None:
        if not self.done and self.buffer.strip():
            self.data_received(b"\n")
        return None

    def _answer(self, ok: bool) -> None:
        self.done = True
        transport = self.transport
        if transport is not None and hasattr(transport, "write"):
            with contextlib.suppress(Exception):
                transport.write(b'{"ok":true}\n' if ok else b'{"ok":false}\n')  # type: ignore[attr-defined]
        self._close()

    def _close(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.transport is not None:
            self.transport.close()

    def connection_lost(self, exc: Exception | None) -> None:
        if self.timer is not None:
            self.timer.cancel()


class ReportServer:
    """Listens on the report socket / pipe and hands valid reports to ``sink``."""

    def __init__(self, sink: ReportSink, home: Path) -> None:
        self.sink = sink
        self.home = Path(home)
        self.address = ""
        self._server: Any = None
        self._unix_path: Path | None = None

    def handle_payload(self, obj: Any) -> bool:
        """Validate one report (token included) and apply it; ``False`` when refused."""
        try:
            terminal_id, token, report = parse_report(obj)
        except ReportError as exc:
            log.debug("terminal report refused: %s", exc)
            return False
        expected = self.sink.report_token(terminal_id)
        if not expected or not hmac.compare_digest(expected.encode(), token.encode()):
            log.info("terminal report for %s refused: bad token", terminal_id)
            return False
        try:
            self.sink.apply_hook_report(terminal_id, report)
        except Exception:  # noqa: BLE001 - a sink bug must not kill the listener
            log.exception("applying a hook report for %s failed", terminal_id)
            return False
        return True

    async def start(self) -> str:
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            address = rf"\\.\pipe\snowpea-report-{secrets.token_hex(8)}"
            servers = await loop.start_serving_pipe(  # type: ignore[attr-defined]
                lambda: _LineProtocol(self), address)
            self._server = servers
            self.address = address
            return address
        path = unix_socket_path(self.home)
        _private_dir(path.parent)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)  # a stale socket of an earlier daemon
        self._server = await loop.create_unix_server(lambda: _LineProtocol(self), str(path))
        os.chmod(path, 0o600)
        self._unix_path = path
        self.address = str(path)
        return self.address

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        if isinstance(server, list):  # Windows pipe servers
            for pipe in server:
                with contextlib.suppress(Exception):
                    pipe.close()
        else:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if self._unix_path is not None:
            with contextlib.suppress(FileNotFoundError):
                if stat.S_ISSOCK(os.lstat(self._unix_path).st_mode):
                    os.unlink(self._unix_path)
            self._unix_path = None


def core_bin_for_env() -> str:
    """What ``SNOWPEA_CORE_BIN`` should be in a terminal: a command running ``snowpea-core``.

    A frozen core is itself ``snowpea-core``; otherwise the console script that
    sits beside the running interpreter, then the one on PATH; ``""`` when none
    is found (the hook script then tries PATH itself).
    """
    import shutil

    if getattr(sys, "frozen", False):
        return sys.executable
    exe_dir = Path(sys.executable).parent
    for name in ("snowpea-core.exe", "snowpea-core") if sys.platform == "win32" else (
            "snowpea-core",):
        candidate = exe_dir / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("snowpea-core") or ""
