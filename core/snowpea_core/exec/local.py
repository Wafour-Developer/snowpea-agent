"""Local execution backend: the machine the daemon runs on."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
from pathlib import Path, PurePath

from snowpea_core.exec.backend import (
    DEFAULT_TIMEOUT,
    MAX_OUTPUT,
    ChunkSink,
    ExecResult,
)
from snowpea_core.exec.backend import (
    truncate_output as _truncate,
)

#: Bytes asked of a pipe at a time.
READ_SIZE = 8192

#: Largest ``tool.progress`` fragment, in characters.
CHUNK_LIMIT = 4096

#: Seconds between flushes of whatever the command has produced.
FLUSH_INTERVAL = 0.1


class LocalBackend:
    """Runs shell commands and file operations in the session's workdir."""

    kind = "local"

    def __init__(self, workdir: Path | str) -> None:
        self._cwd = Path(workdir).expanduser()

    @property
    def cwd(self) -> Path:
        return self._cwd

    def resolve(self, path: str | PurePath) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._cwd / candidate
        return candidate

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``command`` through the shell, capturing output."""
        return await self.run_stream(command, cwd=cwd, timeout=timeout, env=env)

    async def run_stream(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
        on_chunk: ChunkSink | None = None,
    ) -> ExecResult:
        """Run ``command``, reporting output to ``on_chunk`` as it arrives.

        The pipes are drained continuously rather than with ``communicate()``,
        so a long command can be tailed live (IDE-PROGRESS D2).  Fragments are
        coalesced into at most :data:`CHUNK_LIMIT` characters and flushed every
        :data:`FLUSH_INTERVAL` seconds — a chatty command must not turn into
        one event per line.  With no ``on_chunk`` the behaviour, the captured
        output and the timeout result are exactly what ``communicate()`` gave.
        """
        workdir = self.resolve(cwd) if cwd else self._cwd
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        waiting: dict[str, str] = {"stdout": "", "stderr": ""}

        async def pump(reader: asyncio.StreamReader | None, name: str) -> None:
            if reader is None:
                return
            # An incremental decoder so a multi-byte character split across two
            # reads is not turned into two replacement characters.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                data = await reader.read(READ_SIZE)
                text = decoder.decode(data, not data)
                if text:
                    captured[name].append(text)
                    waiting[name] += text
                if not data:
                    return

        async def flush() -> None:
            if on_chunk is None:
                return
            for name in ("stdout", "stderr"):
                while waiting[name]:
                    chunk, waiting[name] = waiting[name][:CHUNK_LIMIT], waiting[name][CHUNK_LIMIT:]
                    await on_chunk(name, chunk)

        async def ticker() -> None:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL)
                await flush()

        async def drain() -> int:
            await asyncio.gather(pump(process.stdout, "stdout"), pump(process.stderr, "stderr"))
            return await process.wait()

        flusher = asyncio.ensure_future(ticker()) if on_chunk is not None else None
        try:
            exit_code = await asyncio.wait_for(asyncio.ensure_future(drain()), timeout)
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
        finally:
            if flusher is not None:
                flusher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await flusher
        # The tail, after the ticker is gone: every chunk a caller will see is
        # emitted before ``run_stream`` returns, and therefore before the
        # ``tool.result`` its caller publishes.
        await flush()
        return ExecResult(
            exit_code=exit_code,
            stdout=_truncate("".join(captured["stdout"])),
            stderr=_truncate("".join(captured["stderr"])),
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

    async def exists(self, path: str) -> bool:
        target = self.resolve(path)
        return await asyncio.to_thread(target.exists)

    async def list_dir(self, path: str) -> list[str]:
        target = self.resolve(path or ".")

        def _list() -> list[str]:
            return sorted(
                entry.name + ("/" if entry.is_dir() else "") for entry in target.iterdir()
            )

        return await asyncio.to_thread(_list)

    async def close(self) -> None:
        """Nothing to release: the local backend owns no resources."""
        return None


__all__ = ["CHUNK_LIMIT", "FLUSH_INTERVAL", "MAX_OUTPUT", "READ_SIZE", "LocalBackend"]
