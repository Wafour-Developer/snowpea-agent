"""Where a tool actually runs.

M1 ships only :class:`~snowpea_core.exec.local.LocalBackend`; US-010 adds the
docker and ssh implementations behind the same protocol, so tools must never
touch ``subprocess`` or :mod:`pathlib` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_TIMEOUT = 120.0


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


@runtime_checkable
class ExecutionBackend(Protocol):
    """Filesystem + process access for one session."""

    kind: str

    @property
    def cwd(self) -> Path: ...

    def resolve(self, path: str | Path) -> Path:
        """Absolute path for ``path``, interpreted relative to :attr:`cwd`."""
        ...

    async def run(
        self, command: str, *, cwd: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def list_dir(self, path: str) -> list[str]: ...


__all__ = ["DEFAULT_TIMEOUT", "ExecResult", "ExecutionBackend"]
