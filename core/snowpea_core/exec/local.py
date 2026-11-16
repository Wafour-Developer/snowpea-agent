"""Local execution backend: the machine the daemon runs on."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from snowpea_core.exec import winproc
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

#: How often the drain checks whether the command's own process has exited.
EXIT_POLL = 0.1

#: Seconds the pipes may stay open after the command's own process exited.
#: Past that, whatever it started (an ``adb`` server, a ``cmd /c start``,
#: ``daemon &``) holds them, and waiting on would hang the call forever.
PIPE_GRACE = 2.0

#: Appended to stderr when the reading stopped for that reason.
HELD_OPEN_NOTE = (
    "[the command exited, but a process it started kept its output open; stopped "
    "reading {grace:g}s later. Start servers with background: true.]"
)

log = logging.getLogger("snowpea.exec.local")


#: Every project root the daemon knows (live sessions, the sidebar, Jarvis):
#: set by ``server/trust_handlers.wire_trust``.  Security review 5 (round-4 M3
#: re-check): a PATH entry inside ANY project, not only the one the command
#: runs in, is dropped, and no program there is ever trusted as safe.
_PROJECT_ROOTS: list[Any] = []


def set_project_roots(provider: Any) -> None:
    """``provider()`` answers the known project roots (``None`` clears it)."""
    _PROJECT_ROOTS[:] = [provider] if provider is not None else []


def project_roots(workdir: Path | str | None = None) -> list[Path]:
    """``workdir`` and every known project root, resolved (never ``/`` itself)."""
    raw: list[Any] = [workdir] if workdir is not None else []
    for provider in _PROJECT_ROOTS:
        with contextlib.suppress(Exception):
            raw.extend(provider() or ())
    roots: list[Path] = []
    for item in raw:
        with contextlib.suppress(OSError, RuntimeError, ValueError, TypeError):
            real = Path(item).resolve()
            if real != Path(real.anchor) and real not in roots:
                roots.append(real)
    return roots


def sanitized_path(value: str, workdir: Path | str | None) -> str:
    """``PATH`` without empty or relative entries and without entries inside a project.

    An empty entry, ``.`` or ``bin`` resolve against the folder a command runs
    in: a file the project (or the model) put there would run in place of the
    program the line names (security review 4, M3).  Entries inside ``workdir``
    or any other known project (:func:`project_roots`) are dropped too.
    """
    roots = project_roots(workdir)
    kept: list[str] = []
    for entry in value.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        if roots:
            try:
                real = Path(entry).resolve()
            except (OSError, RuntimeError, ValueError):
                real = Path(entry)
            if any(real == root or root in real.parents for root in roots):
                continue
        if entry not in kept:
            kept.append(entry)
    return os.pathsep.join(kept)


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

    async def stop(self) -> None:
        """End the command and what it started, as a timeout would; never raises."""
        if self.task.done():
            return
        await _terminate_process_group(self.process)

class LocalBackend:
    """Runs shell commands and file operations in the session's workdir."""

    kind = "local"

    def __init__(self, workdir: Path | str) -> None:
        self._cwd = Path(workdir).expanduser()
        #: Windows: jobs of finished commands that left processes running
        #: (``start …``, ``adb`` starting its server).  Ended by :meth:`close`.
        self._leftovers: list[winproc.ProcessTree] = []

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
        merged = {**os.environ, **(env or {})}
        # Security review 4, M3: no relative PATH entry and none inside the folder
        # the line runs in, so a planted ``./ls`` never stands for ``ls``.
        merged["PATH"] = sanitized_path(merged.get("PATH", ""), workdir)
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged,
            start_new_session=True,
            **winproc.spawn_kwargs(),
        )
        winproc.attach(process)
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        waiting: dict[str, str] = {"stdout": "", "stderr": ""}

        async def pump(reader: asyncio.StreamReader | None, name: str) -> None:
            if reader is None:
                return
            # An incremental decoder so a multi-byte character split across two
            # reads is not turned into two replacement characters (UTF-8 on
            # POSIX; UTF-8, else the console code page, on Windows).
            decoder = winproc.output_decoder()
            while True:
                data = await reader.read(READ_SIZE)
                text = decoder.decode(data, not data)
                if text:
                    captured[name].append(text)
                    waiting[name] += text
                if not data:
                    return

        # One flush at a time, and the ticker's is shielded: cancelling the
        # ticker when the command ends must not interrupt a chunk already taken
        # off ``waiting`` (it was lost, so the streamed tail missed its last
        # line). The final flush below waits for it on the lock.
        flush_lock = asyncio.Lock()

        async def flush() -> None:
            if on_chunk is None:
                return
            async with flush_lock:
                for name in ("stdout", "stderr"):
                    while waiting[name]:
                        chunk = waiting[name][:CHUNK_LIMIT]
                        waiting[name] = waiting[name][CHUNK_LIMIT:]
                        await on_chunk(name, chunk)

        async def ticker() -> None:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL)
                await asyncio.shield(flush())

        async def drain() -> int:
            code = await _drain_until_exit(
                process,
                pump(process.stdout, "stdout"),
                pump(process.stderr, "stderr"),
                on_held_open=lambda: captured["stderr"].append(
                    "\n" + HELD_OPEN_NOTE.format(grace=PIPE_GRACE) + "\n"
                ),
            )
            # What it left behind keeps running, as on POSIX; on Windows its
            # job stays open until the session or the daemon ends it.
            self._keep_leftovers(process)
            return code

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
        """End what finished commands left running on Windows (session close).

        POSIX owns nothing here: there, as before, what a finished foreground
        command left behind is not tracked.
        """
        leftovers, self._leftovers = self._leftovers, []
        for tree in leftovers:
            try:
                await asyncio.to_thread(tree.terminate)
            except Exception:  # noqa: BLE001 - close must finish
                log.warning("could not end leftovers of pid %s", tree.pid, exc_info=True)

    def _keep_leftovers(self, process: Any) -> None:
        winproc.release(process, keep_leftovers=True)
        tree = winproc.tree_of(process)
        if tree is not None and not tree.closed:
            self._leftovers = [t for t in self._leftovers if not t.closed]
            self._leftovers.append(tree)

    async def spawn_background(self, command: str, log_path: str) -> winproc.DetachedChild:
        """Start ``command`` detached on Windows, output appended to ``log_path``.

        POSIX keeps ``nohup sh -c … &`` (``tools/process.launch``); Windows has
        neither, so the command is started directly here, without a window,
        in a job of its own (:func:`winproc.start_detached`).
        """
        return await asyncio.to_thread(
            winproc.start_detached,
            command,
            cwd=str(self._cwd),
            env={**os.environ},
            log_path=self.resolve(log_path),
        )


async def _drain_until_exit(
    process: Any,
    *pumps: Any,
    on_held_open: Callable[[], None],
) -> int:
    """Read the pipes until they close, or until the command itself has exited.

    A process the command started in the background (``adb`` starting its
    server, ``daemon &``, ``start``) inherits the pipes and holds them open
    after the command's own process is gone; reading to EOF would then never
    end, and the tool call would stay "running" with no result.  So once the
    command's process has exited, the readers get :data:`PIPE_GRACE` seconds
    more, then reading stops, ``on_held_open`` is told and the exit status is
    returned with what was read.  (``process.wait()`` itself waits for the
    pipes on Python 3.11, so the exit is read from ``returncode``.)
    """
    readers = asyncio.ensure_future(asyncio.gather(*pumps))
    try:
        while not readers.done() and process.returncode is None:
            await asyncio.wait({readers}, timeout=EXIT_POLL)
        if not readers.done():
            done, _ = await asyncio.wait({readers}, timeout=PIPE_GRACE)
            if not done:
                readers.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await readers
                _close_pipes(process)
                on_held_open()
                return int(process.returncode)
        await readers
    except BaseException:
        if not readers.done():
            readers.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await readers
        raise
    if process.returncode is not None:
        return int(process.returncode)
    return await process.wait()


def _close_pipes(process: Any) -> None:
    """Stop reading a finished command's pipes that something else holds open."""
    transport = getattr(process, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 - cleanup must not raise
        log.warning("could not close the pipes of pid %s", process.pid, exc_info=True)


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Stop a shell command and anything it spawned.  Never raises.

    POSIX: SIGTERM to its process group, SIGKILL after half a second.  Windows
    (no ``killpg``): its job, or ``taskkill /T`` for its exact pid
    (:mod:`snowpea_core.exec.winproc`).
    """
    try:
        if winproc.enabled():
            await winproc.kill(process)
        else:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 0.5)
                return
            except TimeoutError:
                pass
            _signal_group(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), 5.0)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a cleanup failure is logged, never the turn's error
        log.warning("could not stop command pid %s", process.pid, exc_info=True)


def _signal_group(pgid: int, sig: int) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is None:  # pragma: no cover - only Windows, handled above
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        killpg(pgid, sig)


__all__ = ["CHUNK_LIMIT", "FLUSH_INTERVAL", "MAX_OUTPUT", "READ_SIZE", "LocalBackend"]
