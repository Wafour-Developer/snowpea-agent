"""Local execution backend: the machine the daemon runs on."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from snowpea_core.exec.backend import DEFAULT_TIMEOUT, ExecResult

#: Truncation limit for captured stdout/stderr, in characters.
MAX_OUTPUT = 60_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n… [truncated, {len(text) - MAX_OUTPUT} more characters]"


class LocalBackend:
    """Runs shell commands and file operations in the session's workdir."""

    kind = "local"

    def __init__(self, workdir: Path | str) -> None:
        self._cwd = Path(workdir).expanduser()

    @property
    def cwd(self) -> Path:
        return self._cwd

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._cwd / candidate
        return candidate

    async def run(
        self, command: str, *, cwd: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> ExecResult:
        """Run ``command`` through the shell, capturing output."""
        workdir = self.resolve(cwd) if cwd else self._cwd
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(os.environ),
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            return ExecResult(
                exit_code=124,
                stderr=f"command timed out after {timeout:g}s",
                timed_out=True,
            )
        return ExecResult(
            exit_code=process.returncode or 0,
            stdout=_truncate(raw_out.decode("utf-8", "replace")),
            stderr=_truncate(raw_err.decode("utf-8", "replace")),
        )

    async def read_file(self, path: str) -> str:
        target = self.resolve(path)
        return await asyncio.to_thread(target.read_text, "utf-8")

    async def write_file(self, path: str, content: str) -> None:
        target = self.resolve(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def list_dir(self, path: str) -> list[str]:
        target = self.resolve(path or ".")

        def _list() -> list[str]:
            return sorted(
                entry.name + ("/" if entry.is_dir() else "") for entry in target.iterdir()
            )

        return await asyncio.to_thread(_list)


__all__ = ["MAX_OUTPUT", "LocalBackend"]
