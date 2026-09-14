"""Where a tool actually runs.

Three backends implement one protocol: :class:`~snowpea_core.exec.local.LocalBackend`
(the daemon's own machine), :class:`~snowpea_core.exec.docker.DockerBackend` and
:class:`~snowpea_core.exec.ssh.SshBackend`.  Tools must never touch
``subprocess`` or :mod:`pathlib` directly, so that ``/backend docker`` moves the
whole tool surface at once (AC-18).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Protocol, runtime_checkable

DEFAULT_TIMEOUT = 120.0

#: Truncation limit for captured stdout/stderr, in characters.
MAX_OUTPUT = 60_000


def truncate_output(text: str, limit: int = MAX_OUTPUT) -> str:
    """Clip captured output, saying how much was dropped."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more characters]"


@dataclass
class ExecResult:
    """Outcome of one command."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


#: Contract §1 spells this ``RunResult``; the M1 name stays the canonical one.
RunResult = ExecResult

#: Called with ``("stdout" | "stderr", text)`` for each coalesced fragment a
#: still-running command has produced (IDE-PROGRESS D2).
ChunkSink = Callable[[str, str], Awaitable[None]]


@runtime_checkable
class StreamingBackend(Protocol):
    """A backend that can report a command's output while it still runs.

    Optional on purpose: only :class:`~snowpea_core.exec.local.LocalBackend`
    implements it today, and the ``shell`` tool falls back to :meth:`run` for
    the docker and ssh backends, which hand back the output in one piece.  The
    return value is the same :class:`ExecResult` ``run`` produces, so the
    captured output — not the chunks — stays authoritative.
    """

    async def run_stream(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
        on_chunk: ChunkSink | None = None,
    ) -> ExecResult: ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """Filesystem + process access for one session."""

    kind: str

    @property
    def cwd(self) -> PurePath:
        """Working directory commands start in, on the backend's filesystem."""
        ...

    def resolve(self, path: str | PurePath) -> PurePath:
        """Absolute path for ``path``, interpreted relative to :attr:`cwd`."""
        ...

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def exists(self, path: str) -> bool: ...

    async def list_dir(self, path: str) -> list[str]: ...

    async def close(self) -> None:
        """Release whatever the backend holds (container, connection)."""
        ...


def remote_resolve(cwd: PurePath, path: str | PurePath) -> PurePosixPath:
    """``resolve`` for a POSIX remote filesystem, against ``cwd``."""
    candidate = PurePosixPath(str(path))
    if not candidate.is_absolute():
        candidate = PurePosixPath(str(cwd)) / candidate
    parts: list[str] = []
    for part in candidate.parts:
        if part == ".":
            continue
        if part == ".." and parts and parts[-1] not in ("/", ".."):
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath("/")


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_OUTPUT",
    "ChunkSink",
    "ExecResult",
    "ExecutionBackend",
    "RunResult",
    "StreamingBackend",
    "remote_resolve",
    "truncate_output",
]
