"""Local execution backend: the machine the daemon runs on."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

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



#: How often a yielding run asks whether to stop waiting.
YIELD_POLL = 0.25


@dataclass
class Detached:
    """A command a caller stopped waiting for, still drained into ``captured``."""

    process: Any
    task: asyncio.Task[int]
    captured: dict[str, list[str]]

    def output(self) -> str:
        return "".join(self.captured["stdout"]) + "".join(self.captured["stderr"])

    @property
    def exit_code(self) -> int | None:
        if not self.task.done() or self.task.cancelled() or self.task.exception() is not None:
            return None
        return self.task.result()

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
        yield_when: Callable[[], bool] | None = None,
    ) -> ExecResult:
        """Run ``command``, reporting output to ``on_chunk`` as it arrives.

        ``yield_when`` is asked a few times a second; once it says yes the
        command is left running, still drained, and an ``ExecResult`` with
        ``yielded`` and the output so far comes back (the user spoke, and the
        turn must not wait out a long command to hear it).

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
            start_new_session=True,
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
        drain_task: asyncio.Task[int] | None = None
        try:
            drain_task = asyncio.ensure_future(drain())
            if yield_when is None:
                exit_code = await asyncio.wait_for(drain_task, timeout)
            else:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    left = deadline - loop.time()
                    if left <= 0:
                        raise TimeoutError
                    done, _ = await asyncio.wait({drain_task}, timeout=min(YIELD_POLL, left))
                    if done:
                        exit_code = drain_task.result()
                        break
                    if yield_when():
                        await flush()
                        return ExecResult(
                            exit_code=-1,
                            stdout=_truncate("".join(captured["stdout"])),
                            stderr=_truncate("".join(captured["stderr"])),
                            yielded=True,
                            detached=Detached(process, drain_task, captured),
                        )
        except TimeoutError:
            await _terminate_process_group(process)
            return ExecResult(
                exit_code=124,
                stderr=f"command timed out after {timeout:g}s",
                timed_out=True,
            )
        except asyncio.CancelledError:
            await _terminate_process_group(process)
            if drain_task is not None:
                drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain_task
            raise
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

    async def read_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(self.resolve(path).read_bytes)

    async def write_bytes(self, path: str, content: bytes) -> None:
        target = self.resolve(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        await asyncio.to_thread(_write)

    async def remove_file(self, path: str) -> None:
        await asyncio.to_thread(self.resolve(path).unlink, missing_ok=True)

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


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Stop a shell command and anything it spawned."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 0.5)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await process.wait()


__all__ = ["CHUNK_LIMIT", "FLUSH_INTERVAL", "MAX_OUTPUT", "READ_SIZE", "LocalBackend"]
