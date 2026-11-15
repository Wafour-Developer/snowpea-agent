"""The Codex adapter: one shared ``codex app-server`` per daemon (M19 WP6b).

Ground truth: ``docs/design/m19-spikes/codex.md`` (codex-cli 0.167.1).

Transport
    One ``codex app-server`` child over stdio, newline-delimited JSON-RPC
    *without* the ``"jsonrpc"`` field (:class:`AppServer`).  It is started on
    first use (discovery, a list, a read or a channel), carries every thread
    snowpea has loaded, is restarted with a backoff when it dies (the threads
    it carried end their turn with an error and can be continued), and is
    stopped after ``server_idle_sec`` with no thread loaded.  The server's own
    requests (approvals) have their own id space starting at ``0``: a frame
    with both ``method`` and ``id`` is a server request, never a response.

Sessions
    Codex threads, listed with ``thread/list`` by working directory, newest
    first, and read with ``thread/turns/list`` (``itemsView: full``).  The
    adapter never opens ``~/.codex`` files except to look at a thread's writer
    lock (``thread-writer-locks/<id>.lock``) from the outside.

Open elsewhere
    A thread loaded by another codex process holds ``flock`` on its writer
    lock.  :func:`lock_holders` reads ``/proc/locks`` (Linux) or asks ``lsof``
    (macOS) and never takes the lock.  ``thread/resume`` answering ``-32600
    ... already has an active writer`` is authoritative too: the thread reads
    as open elsewhere (view only) for :data:`WRITER_FLAG_SEC`.

Modes
    ``default`` asks (``approvalPolicy: untrusted``, ``workspace-write``
    sandbox) and bridges every prompt into snowpea's approval queue;
    ``readonly`` cannot write (``read-only`` sandbox, ``approvalPolicy:
    never`` so nothing can be escalated); ``unrestricted`` is the ``--yolo``
    equivalent (``never`` + ``danger-full-access``), released only through
    :func:`proc.unrestricted_values` with the person's grant.  Every other
    outgoing payload goes through :func:`proc.scan_payload`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from snowpea_core import __version__
from snowpea_core.config.paths import resolve_home
from snowpea_core.external import proc
from snowpea_core.external.base import (
    ALL_MODES,
    ApprovalAnswer,
    ApprovalAsk,
    Approvals,
    ChannelHost,
    CliCapabilities,
    Discovery,
    ExternalAdapter,
    ExternalChannel,
    LaunchSpec,
    Liveness,
    SessionInfo,
    unavailable,
)
from snowpea_core.external.proc import (
    ExternalError,
    ExternalProcess,
    PinnedExecutable,
    ProcessSupervisor,
)
from snowpea_core.external.timeline import (
    RecordFilter,
    TimelineBuilder,
    TimelineItem,
    iso_from_ms,
    iso_from_unix,
    stable_key,
)
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.codex")

#: Oldest codex-cli whose app-server speaks the thread API used here (spike S2).
MIN_VERSION: tuple[int, int, int] = (0, 167, 0)
#: Newest codex-cli this adapter was checked against.
TESTED_VERSION: tuple[int, int, int] = (0, 169, 0)

#: Thread sources listed: the person's own (TUI, IDE, exec) and app-server
#: threads; sub-agent threads belong to their parent.
SOURCE_KINDS: tuple[str, ...] = ("cli", "vscode", "exec", "appServer")

#: Seconds a thread whose resume answered "active writer" reads as open elsewhere.
WRITER_FLAG_SEC = 30.0
#: A rollout written to this recently means the thread is working.
BUSY_MTIME_SEC = 15.0
#: Page sizes and caps for listing and reading.
LIST_PAGE = 100
LIST_MAX_PAGES = 50
SCAN_MAX_PAGES = 5
TURNS_PAGE = 100
MAX_TURNS = 5000
MODELS_MAX_PAGES = 10

#: Item kinds converted into the timeline; anything a newer codex invents is
#: dropped until someone looks at it (``hookPrompt`` carries hook text).
ITEM_FILTER = RecordFilter(
    allow=frozenset(
        {
            "userMessage",
            "agentMessage",
            "reasoning",
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "dynamicToolCall",
            "functionCallOutput",
            "plan",
        }
    )
)

#: Thread start/resume policy per mode (``unrestricted`` comes from a grant).
_THREAD_POLICY: dict[str, dict[str, Any]] = {
    "default": {"approvalPolicy": "untrusted", "sandbox": "workspace-write"},
    "readonly": {"approvalPolicy": "never", "sandbox": "read-only"},
}
#: The same as a per-turn override (a mode switch applies from the next turn).
_TURN_POLICY: dict[str, dict[str, Any]] = {
    "default": {
        "approvalPolicy": "untrusted",
        "sandboxPolicy": {
            "type": "workspaceWrite",
            "writableRoots": [],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        },
    },
    "readonly": {
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    },
}
_UNRESTRICTED_THREAD = {"approvalPolicy": "never", "sandbox": "danger-full-access"}
_UNRESTRICTED_TURN = {"approvalPolicy": "never", "sandboxPolicy": {"type": "dangerFullAccess"}}

#: Free text in a request: the prompt and the folder it runs in are not
#: protocol values (the folder is a registered project, checked by the manager).
_FREE_TEXT_KEYS = frozenset({"input", "cwd"})

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_ACTIVE_WRITER = "active writer"
_NOT_LOGGED_IN = "codex is not logged in: run `codex login` in a terminal, then try again"


class CodexRpcError(ExternalError):
    """An error answer from the app-server (``{"id", "error": {code, message}}``)."""

    def __init__(self, method: str, code: Any, message: str) -> None:
        super().__init__(errors.EXTERNAL_UNAVAILABLE, f"codex {method}: {message}")
        self.rpc_code = code
        self.rpc_message = message


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """``(0, 167, 1)`` from ``codex-cli 0.167.1``; ``None`` when there is none."""
    match = _VERSION_RE.search(text or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def check_policy(params: Mapping[str, Any]) -> None:
    """Refuse ``approvalPolicy: never`` anywhere but with a read-only sandbox.

    :func:`proc.scan_payload` catches ``danger-full-access``; ``never`` is a
    bypass only next to a sandbox that can write, so it is checked here.
    """
    if params.get("approvalPolicy") != "never":
        return
    sandbox = params.get("sandbox")
    policy = params.get("sandboxPolicy")
    policy_type = policy.get("type") if isinstance(policy, Mapping) else None
    if sandbox == "read-only" or (sandbox is None and policy_type == "readOnly"):
        return
    raise ExternalError(
        errors.INVALID_PARAMS,
        "refused approvalPolicy never with a sandbox that can write; unrestricted mode is "
        "chosen by a person",
    )


def _scan(params: Mapping[str, Any]) -> None:
    proc.scan_payload({key: value for key, value in params.items() if key not in _FREE_TEXT_KEYS})
    check_policy(params)


# ---------------------------------------------------------------------------
# writer lock probe
# ---------------------------------------------------------------------------


def lock_holders(path: str | os.PathLike[str]) -> set[int] | None:
    """Pids holding a lock on ``path``, without taking it; ``None`` when unknowable.

    Linux reads ``/proc/locks`` and matches the file's inode (and device when
    the kernel reports the same one); macOS asks ``lsof``.  An empty set
    means nobody holds it (or the file does not exist).
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    if sys.platform.startswith("linux"):
        return _proc_locks_holders(st)
    if sys.platform == "darwin":
        return _lsof_holders(path)
    return None


def _proc_locks_holders(st: os.stat_result) -> set[int] | None:
    try:
        text = Path("/proc/locks").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    exact: set[int] = set()
    by_inode: set[int] = set()
    for line in text.splitlines():
        fields = line.split()
        # "1: FLOCK  ADVISORY  WRITE 1234 08:02:5678 0 EOF"; waiters carry "->".
        if len(fields) < 6 or "->" in fields:
            continue
        try:
            pid = int(fields[4])
            major, minor, inode = fields[5].split(":")
        except ValueError:
            continue
        if not inode.isdigit() or int(inode) != st.st_ino:
            continue
        by_inode.add(pid)
        try:
            if int(major, 16) == os.major(st.st_dev) and int(minor, 16) == os.minor(st.st_dev):
                exact.add(pid)
        except ValueError:
            continue
    # Some filesystems (btrfs subvolumes) report another device than stat():
    # fall back to the inode alone then.
    return exact or by_inode


def _lsof_holders(path: str | os.PathLike[str]) -> set[int] | None:
    lsof = proc.trusted_tool("lsof")
    if lsof is None:
        return None
    try:
        out = subprocess.run(
            [lsof, "-t", "--", os.fspath(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {int(token) for token in out.split() if token.isdigit()}


# ---------------------------------------------------------------------------
# items -> timeline
# ---------------------------------------------------------------------------


def _ts(item: Mapping[str, Any], fallback: str | None = None) -> str | None:
    for key in ("startedAtMs", "completedAtMs"):
        if isinstance(item.get(key), (int, float)):
            return iso_from_ms(item[key])
    return fallback


def user_text(content: Any) -> str:
    """The prompt text of a ``userMessage`` (attachments as short markers)."""
    parts: list[str] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif kind in ("image", "localImage"):
            parts.append("[image]")
        elif kind in ("audio", "localAudio"):
            parts.append("[audio]")
        elif kind in ("mention", "skill") and isinstance(part.get("name"), str):
            parts.append(f"@{part['name']}")
    return "\n".join(part for part in parts if part)


def shell_script(command: str) -> str:
    """The script inside codex's ``/bin/bash -lc '<script>'`` wrapper, else ``command``.

    The person (and snowpea's "for this session" matching) judges the command
    that runs, not the shell that runs it.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if (
        len(words) == 3
        and os.path.basename(words[0]) in ("bash", "sh", "zsh")
        and words[1] in ("-c", "-lc")
    ):
        return words[2]
    return command


def _text_of(value: Any) -> str:
    """Readable text from an MCP / dynamic tool result."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _text_of(value.get("content"))
    if isinstance(value, list):
        return "\n".join(text for text in (_text_of(item) for item in value) if text)
    try:
        return json.dumps(value, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(value)[:4000]


def _paths_of(changes: Any) -> list[str]:
    return [
        str(change.get("path"))
        for change in (changes if isinstance(changes, list) else [])
        if isinstance(change, Mapping) and change.get("path")
    ]


def _diff_text(changes: Any) -> str:
    out: list[str] = []
    for change in changes if isinstance(changes, list) else []:
        if isinstance(change, Mapping) and isinstance(change.get("diff"), str):
            out.append(change["diff"])
    return "\n".join(out)


def _args(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else ({"arguments": value} if value else {})


def item_key(item_id: Any, part: str) -> str:
    """A key that is the same for an item read from history and streamed live."""
    return stable_key("codex-item", item_id, part)


def announce_item(builder: TimelineBuilder, item: Mapping[str, Any], *,
                  turn_id: str | None, ts: str | None) -> None:
    """The start of a tool-like item (a call, and a patch's diffs)."""
    kind = item.get("type")
    raw = item.get("id")
    raw_id = str(raw) if raw is not None else None
    if kind == "commandExecution":
        builder.tool_call(raw_id, "shell", {"command": item.get("command") or "",
                                            "cwd": item.get("cwd") or ""},
                          ts=ts, turn_id=turn_id)
    elif kind == "fileChange":
        changes = item.get("changes")
        builder.tool_call(raw_id, "apply_patch", {"paths": _paths_of(changes)},
                          ts=ts, turn_id=turn_id)
        for change in changes if isinstance(changes, list) else []:
            if isinstance(change, Mapping) and change.get("path") and isinstance(
                change.get("diff"), str
            ):
                builder.diff(str(change["path"]), change["diff"], raw_call_id=raw_id, ts=ts,
                             turn_id=turn_id)
    elif kind == "mcpToolCall":
        builder.tool_call(raw_id, f"mcp:{item.get('server')}.{item.get('tool')}",
                          _args(item.get("arguments")), ts=ts, turn_id=turn_id)
    elif kind == "dynamicToolCall":
        builder.tool_call(raw_id, str(item.get("tool") or "tool"),
                          _args(item.get("arguments")), ts=ts, turn_id=turn_id)


def finish_item(builder: TimelineBuilder, item: Mapping[str, Any], *,
                turn_id: str | None, ts: str | None) -> None:
    """A completed item: messages, reasoning and the results of tool-like items."""
    kind = item.get("type")
    raw = item.get("id")
    raw_id = str(raw) if raw is not None else None
    status = item.get("status")
    if kind == "userMessage":
        text = user_text(item.get("content"))
        if text:
            builder.user(text, key=item_key(raw_id, "user"), ts=ts, turn_id=turn_id)
    elif kind == "agentMessage":
        builder.assistant(str(item.get("text") or ""), key=item_key(raw_id, "message"), ts=ts,
                          turn_id=turn_id)
    elif kind == "reasoning":
        pieces = item.get("summary") or item.get("content") or []
        text = "\n\n".join(str(piece) for piece in pieces if piece) if isinstance(
            pieces, list) else ""
        builder.reasoning(text, key=item_key(raw_id, "reasoning"), ts=ts, turn_id=turn_id)
    elif kind == "plan":
        text = str(item.get("text") or "")
        if text:
            builder.note(f"Plan:\n{text}", key=item_key(raw_id, "plan"), ts=ts, turn_id=turn_id)
    elif kind == "commandExecution":
        if status in (None, "inProgress"):
            return
        exit_code = item.get("exitCode")
        output = str(item.get("aggregatedOutput") or "")
        if status == "declined":
            builder.tool_result(raw_id, ok=False, error="declined", name="shell", ts=ts,
                                turn_id=turn_id)
        elif status == "completed" and exit_code in (0, None):
            builder.tool_result(raw_id, ok=True, output=output, name="shell", ts=ts,
                                turn_id=turn_id)
        else:
            detail = f"exit code {exit_code}" if exit_code is not None else str(status)
            builder.tool_result(raw_id, ok=False, output=output,
                                error=f"{detail}\n{output}".strip(), name="shell", ts=ts,
                                turn_id=turn_id)
    elif kind == "fileChange":
        if status in (None, "inProgress"):
            return
        ok = status == "completed"
        builder.tool_result(raw_id, ok=ok, output="applied" if ok else "",
                            error=None if ok else str(status), name="apply_patch", ts=ts,
                            turn_id=turn_id)
    elif kind == "mcpToolCall":
        if status in (None, "inProgress"):
            return
        error = item.get("error")
        message = error.get("message") if isinstance(error, Mapping) else None
        builder.tool_result(raw_id, ok=error is None, output=_text_of(item.get("result")),
                            error=str(message or error) if error is not None else None,
                            ts=ts, turn_id=turn_id)
    elif kind == "dynamicToolCall":
        if status in (None, "inProgress"):
            return
        ok = item.get("success") is not False
        output = _text_of(item.get("contentItems"))
        builder.tool_result(raw_id, ok=ok, output=output if ok else "",
                            error=None if ok else (output or "failed"), ts=ts, turn_id=turn_id)
    elif kind == "functionCallOutput":
        name = str(item.get("name") or "tool")
        builder.tool_result(raw_id, ok=True, output=_text_of(item.get("output")), name=name,
                            ts=ts, turn_id=turn_id)


def convert_turns(turns: Sequence[Mapping[str, Any]], builder: TimelineBuilder
                  ) -> list[TimelineItem]:
    """Whole turns (``thread/turns/list`` with full items) as timeline items."""
    for turn in turns:
        turn_id = str(turn.get("id")) if turn.get("id") is not None else None
        fallback = iso_from_unix(turn["startedAt"]) if isinstance(
            turn.get("startedAt"), (int, float)) else None
        items = [item for item in turn.get("items") or [] if isinstance(item, Mapping)]
        for item in ITEM_FILTER.apply(items, lambda record: record.get("type")):
            ts = _ts(item, fallback)
            announce_item(builder, item, turn_id=turn_id, ts=ts)
            finish_item(builder, item, turn_id=turn_id, ts=ts)
        error = turn.get("error")
        if turn.get("status") == "failed" and isinstance(error, Mapping):
            builder.error(errors.EXTERNAL_UNAVAILABLE,
                          friendly_error(error) or "the turn failed",
                          key=item_key(turn_id, "error"), ts=fallback, turn_id=turn_id)
    builder.flush()
    return builder.drain()


def _mentions_auth(info: Any) -> bool:
    if isinstance(info, str):
        return info == "unauthorized"
    if isinstance(info, Mapping):
        if "unauthorized" in info:
            return True
        if info.get("httpStatusCode") == 401:
            return True
        return any(_mentions_auth(value) for value in info.values())
    return False


def is_auth_error(error: Mapping[str, Any]) -> bool:
    """A ``TurnError`` that means the login is missing or expired."""
    if _mentions_auth(error.get("codexErrorInfo")):
        return True
    details = f"{error.get('message') or ''} {error.get('additionalDetails') or ''}"
    return "401 Unauthorized" in details


def friendly_error(error: Mapping[str, Any]) -> str:
    """A ``TurnError`` as one line for the person."""
    if is_auth_error(error):
        return _NOT_LOGGED_IN
    message = str(error.get("message") or "").strip()
    details = str(error.get("additionalDetails") or "").strip()
    if details and details not in message:
        message = f"{message}: {details}" if message else details
    return message[:2000]


# ---------------------------------------------------------------------------
# the shared app-server
# ---------------------------------------------------------------------------


class AppServer:
    """One ``codex app-server`` child shared by every thread this daemon runs."""

    def __init__(self, adapter: CodexAdapter) -> None:
        self.adapter = adapter
        self.process: ExternalProcess | None = None
        self.executable: PinnedExecutable | None = None
        self.codex_home: str | None = None
        self.channels: dict[str, CodexChannel] = {}
        self._readers: dict[ExternalProcess, asyncio.Task[None]] = {}
        #: Processes being stopped on purpose (their exit is not a crash).
        self._stopping: set[ExternalProcess] = set()
        self._idle: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._next_id = 1
        self._start_lock: asyncio.Lock | None = None
        self._ready = False
        self._crashes: list[float] = []
        self._last_error = ""
        self._stop_reason: str | None = None
        self.last_used = time.monotonic()
        #: Restarts after a crash (tests and diagnostics).
        self.starts = 0

    # -- state ------------------------------------------------------------
    @property
    def alive(self) -> bool:
        process = self.process
        return (process is not None and process.alive and self._ready
                and process not in self._stopping)

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None and self.process.alive else None

    def attach(self, thread_id: str, channel: CodexChannel) -> None:
        self.channels[thread_id] = channel
        self.last_used = time.monotonic()

    def detach(self, thread_id: str | None, channel: CodexChannel) -> None:
        if thread_id is not None and self.channels.get(thread_id) is channel:
            del self.channels[thread_id]
        self.last_used = time.monotonic()

    # -- start ------------------------------------------------------------
    async def ensure(self, executable: PinnedExecutable) -> None:
        """Start (or restart) the server and hand-shake; raise ``external_unavailable``."""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            # A new codex binary (an upgrade, a changed path) waits until the
            # threads running on the old one are closed.
            if self.alive and (self.executable == executable or self.channels):
                self.last_used = time.monotonic()
                return
            if self.process is not None:
                await self._stop_locked()
            await self._backoff()
            await self._spawn(executable)

    async def _backoff(self) -> None:
        now = time.monotonic()
        window = self.adapter.crash_window_sec
        self._crashes = [moment for moment in self._crashes if now - moment < window]
        if not self._crashes:
            return
        if len(self._crashes) >= self.adapter.max_crashes:
            raise unavailable(
                f"codex app-server stopped {len(self._crashes)} times in "
                f"{int(window)} seconds; not restarting it for now"
                + (f" (last: {self._last_error})" if self._last_error else "")
            )
        delay = min(self.adapter.max_backoff_sec,
                    self.adapter.backoff_sec * 2 ** (len(self._crashes) - 1))
        wait = delay - (now - self._crashes[-1])
        if wait > 0:
            await asyncio.sleep(wait)

    async def _spawn(self, executable: PinnedExecutable) -> None:
        supervisor, env = self.adapter.spawn_context()
        argv = proc.build_argv(
            executable,
            ["app-server"],
            forbidden=self.adapter.forbidden_args,
            forbidden_pairs=self.adapter.forbidden_pairs,
        )
        process = await supervisor.spawn(
            argv, cwd=tempfile.gettempdir(), env=env, cli="codex", ref="app-server"
        )
        self.process = process
        self.executable = executable
        self._ready = False
        self._pending = {}
        self.starts += 1
        self._readers[process] = asyncio.ensure_future(self._read(process))
        try:
            result = await self._call(
                "initialize",
                {
                    "clientInfo": {"name": "snowpea", "title": "snowpea", "version": __version__},
                    "capabilities": None,
                },
                timeout=self.adapter.start_timeout_sec,
                ready=False,
            )
            await self._write({"method": "initialized"})
        except ExternalError as exc:
            reason = exc.message
            tail = _last_line(process.stderr_tail())
            self._last_error = tail or reason
            if process.alive:  # an exit was already counted by _died
                self._crashes.append(time.monotonic())
            await self._kill(process)
            raise unavailable(
                f"codex app-server did not start: {reason}" + (f" ({tail})" if tail else "")
            ) from exc
        home = result.get("codexHome") if isinstance(result, Mapping) else None
        self.codex_home = str(home) if isinstance(home, str) and home else None
        self._ready = True
        self.last_used = time.monotonic()
        if self._idle is None or self._idle.done():
            self._idle = asyncio.ensure_future(self._idle_loop())

    async def _kill(self, process: ExternalProcess) -> None:
        self._stopping.add(process)
        with contextlib.suppress(Exception):
            await process.terminate(grace=1.0)
        reader = self._readers.get(process)
        if reader is not None and reader is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(reader), 5)

    async def stop(self) -> None:
        """Close stdin (codex exits and releases every lock), then make sure it is gone."""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        process = self.process
        if process is None:
            return
        self._stopping.add(process)
        await process.close_stdin()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.proc.wait(), 3)
        await self._kill(process)
        if self.process is process:
            self.process = None
            self._ready = False
        if self._idle is not None and self._idle is not asyncio.current_task():
            self._idle.cancel()
            await asyncio.gather(self._idle, return_exceptions=True)
        self._idle = None

    async def _idle_loop(self) -> None:
        while True:
            limit = self.adapter.server_idle_sec
            await asyncio.sleep(max(0.05, min(limit / 2, 5.0)))
            if self.process is None:
                return
            if self.channels or self._pending:
                continue
            if time.monotonic() - self.last_used >= limit:
                assert self._start_lock is not None
                async with self._start_lock:
                    # Re-check: a list or a channel may have come in meanwhile.
                    if (self.channels or self._pending or self.process is None
                            or time.monotonic() - self.last_used < limit):
                        continue
                    log.info("stopping the idle codex app-server")
                    self._idle = None
                    await self._stop_locked()
                return

    # -- wire -------------------------------------------------------------
    async def _write(self, frame: dict[str, Any]) -> None:
        process = self.process
        if process is None or not process.alive:
            raise unavailable("codex app-server is not running")
        await process.write_line(frame)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        trusted: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        restart_on_timeout: bool = True,
    ) -> Any:
        """Send one request and wait for its answer.

        ``params`` is scanned for bypass values; ``trusted`` holds values from
        :func:`proc.unrestricted_values` and is merged after the scan.  A
        server that does not answer in time is taken for hung and restarted,
        unless ``restart_on_timeout`` is false (best-effort calls).
        """
        _scan(params)
        if trusted:
            params = {**params, **trusted}
        return await self._call(method, params,
                                timeout=timeout or self.adapter.request_timeout_sec,
                                restart_on_timeout=restart_on_timeout)

    def send_quietly(self, method: str, params: dict[str, Any]) -> None:
        """Fire a request without waiting (safe from inside the reader)."""

        async def run() -> None:
            with contextlib.suppress(ExternalError):
                await self.request(method, params, timeout=30, restart_on_timeout=False)

        asyncio.ensure_future(run())

    async def _call(self, method: str, params: dict[str, Any], *, timeout: float,
                    ready: bool = True, restart_on_timeout: bool = True) -> Any:
        if ready and not self.alive:
            raise unavailable("codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        self.last_used = time.monotonic()
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError as exc:
            # It stopped answering: a hung server is restarted, not waited on.
            process = self.process
            log.warning("codex app-server did not answer %s in %.0fs", method, timeout)
            self._last_error = f"no answer to {method}"
            if process is not None and ready and restart_on_timeout:
                self._crashes.append(time.monotonic())
                self._stop_reason = f"codex app-server stopped answering ({method}); restarted"
                asyncio.ensure_future(self._kill(process))
            raise unavailable(
                f"codex app-server did not answer {method} within {timeout:.0f} seconds"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            self.last_used = time.monotonic()

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        proc.scan_payload(result)
        with contextlib.suppress(ExternalError):
            await self._write({"id": request_id, "result": result})

    def send_quietly_response(self, request_id: Any, result: dict[str, Any]) -> None:
        """Answer a server request without waiting (safe from inside the reader)."""
        asyncio.ensure_future(self.respond(request_id, result))

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        with contextlib.suppress(ExternalError):
            await self._write({"id": request_id, "error": {"code": code, "message": message}})

    async def _read(self, process: ExternalProcess) -> None:
        try:
            async for frame in process.json_lines():
                try:
                    await self._dispatch(frame)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad frame must not stop the stream
                    log.warning("codex frame handling failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken pipe is a dead server
            log.debug("codex app-server stream failed", exc_info=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), 10)
        await self._died(process, process.returncode)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        frame_id = frame.get("id")
        if isinstance(method, str) and frame_id is not None:
            await self._server_request(frame_id, method, frame.get("params"))
            return
        if method is None and frame_id is not None:
            entry = self._pending.get(frame_id) if isinstance(frame_id, int) else None
            if entry is None:
                return
            name, future = entry
            if future.done():
                return
            error = frame.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, Mapping) else None
                message = str(error.get("message") if isinstance(error, Mapping) else error)
                future.set_exception(CodexRpcError(name, code, message[:2000]))
            else:
                future.set_result(frame.get("result"))
            return
        if not isinstance(method, str):
            return
        params = frame.get("params")
        if not isinstance(params, Mapping):
            return
        thread_id = params.get("threadId")
        if thread_id is None and isinstance(params.get("thread"), Mapping):
            thread_id = params["thread"].get("id")
        channel = self.channels.get(thread_id) if isinstance(thread_id, str) else None
        if channel is not None:
            await channel.on_notification(method, params)

    async def _server_request(self, request_id: Any, method: str, params: Any) -> None:
        thread_id = params.get("threadId") if isinstance(params, Mapping) else None
        channel = self.channels.get(thread_id) if isinstance(thread_id, str) else None
        if channel is None or method not in APPROVAL_METHODS:
            # An unanswered request blocks the turn for good (spike S2): refuse it.
            await self.respond_error(request_id, -32601, f"snowpea does not handle {method}")
            return
        channel.on_approval_request(request_id, method, params)

    async def _died(self, process: ExternalProcess, code: int | None) -> None:
        self._readers.pop(process, None)
        expected = process in self._stopping
        self._stopping.discard(process)
        if process.alive:
            # Its stdout closed but it still runs (and holds writer locks).
            asyncio.ensure_future(process.terminate(grace=1.0))
        if process is not self.process:
            return
        self.process = None
        self._ready = False
        tail = _last_line(process.stderr_tail())
        if not expected:
            self._crashes.append(time.monotonic())
            self._last_error = tail or f"exit status {code}"
            # Killed with nothing loaded is the daemon's shutdown sweep, not news.
            busy = bool(self.channels or self._pending)
            log.log(logging.WARNING if busy else logging.INFO,
                    "codex app-server exited (status %s)", code)
        message = (
            (self._stop_reason or "codex app-server stopped") if expected
            else f"codex app-server exited (status {code})"
        )
        self._stop_reason = None
        for _method, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(unavailable(message))
        self._pending.clear()
        channels = list(self.channels.values())
        self.channels.clear()
        for channel in channels:
            with contextlib.suppress(Exception):
                await channel.server_died(message)


def _last_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()[:200]
    return ""


#: Server requests bridged into snowpea's approval queue.
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
APPROVAL_METHODS = frozenset({COMMAND_APPROVAL, FILE_APPROVAL})
#: Approval asks one thread may have open at once; more are declined unasked.
MAX_OPEN_APPROVALS = 16
#: File-change items kept for approval diffs (oldest dropped first).
MAX_KEPT_CHANGES = 256


# ---------------------------------------------------------------------------
# channel
# ---------------------------------------------------------------------------


class CodexChannel(ExternalChannel):
    """One thread snowpea runs on the shared app-server."""

    def __init__(self, adapter: CodexAdapter, spec: LaunchSpec, host: ChannelHost) -> None:
        super().__init__(spec, host)
        self.adapter = adapter
        self.server = adapter.server
        self.attached = False
        self.dead = False
        self.closed = False
        self.applied_mode: str | None = None
        #: The snowpea turn running now, and codex's id for it.
        self.turn_open = False
        self.codex_turn: str | None = None
        self.turn_error: str | None = None
        self._interrupt_wanted = False
        self._interrupt_timer: asyncio.Task[None] | None = None
        self._approvals: dict[Any, asyncio.Task[None]] = {}
        self._changes: dict[str, Any] = {}
        #: Codex turns snowpea already ended (interrupt deadline, lost login):
        #: their late notifications must not touch the next turn.
        self._ended: set[str] = set()
        #: The app-server process this thread is loaded in.
        self._process: ExternalProcess | None = None

    # -- state ------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.attached and not self.dead and not self.closed and self.server.alive

    def pids(self) -> set[int]:
        pid = self.server.pid
        return {pid} if pid is not None and self.alive else set()

    def _policy(self, table: dict[str, dict[str, Any]], granted: Mapping[str, Any]
                ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if self.mode == "unrestricted":
            if self.grant is None:
                raise ExternalError(errors.JARVIS_PERSON_ONLY,
                                    "unrestricted needs a person's grant")
            return {}, proc.unrestricted_values(self.grant, granted)
        return dict(table.get(self.mode) or table["default"]), None

    # -- start ------------------------------------------------------------
    async def start(self) -> None:
        await self.adapter.ensure_server(self.spec.executable)
        policy, trusted = self._policy(_THREAD_POLICY, _UNRESTRICTED_THREAD)
        if self.spec.model:
            policy["model"] = proc.validate_model(self.spec.model)
        if self.spec.resume and self.session_id:
            thread_id = proc.validate_session_id(self.session_id)
            try:
                await self.server.request(
                    "thread/resume",
                    {"threadId": thread_id, "excludeTurns": True, **policy},
                    trusted=trusted,
                )
            except CodexRpcError as exc:
                if _ACTIVE_WRITER in exc.rpc_message:
                    self.adapter.flag_writer(thread_id)
                    raise ExternalError(
                        errors.EXTERNAL_VIEW_ONLY,
                        "this Codex thread is open in another codex process; it can be "
                        "continued here once that one exits",
                    ) from exc
                raise
            self.server.attach(thread_id, self)
            self._process = self.server.process
            self.attached = True
        else:
            result = await self.server.request(
                "thread/start", {"cwd": self.spec.cwd, **policy}, trusted=trusted
            )
            thread = result.get("thread") if isinstance(result, Mapping) else None
            new_id = thread.get("id") if isinstance(thread, Mapping) else None
            if not isinstance(new_id, str):
                raise unavailable("codex did not report the new thread's id")
            proc.validate_session_id(new_id)
            self.session_id = new_id
            self.server.attach(new_id, self)
            self._process = self.server.process
            self.attached = True
            await self.host_api.id_known(new_id)
        self.applied_mode = self.mode

    # -- turns ------------------------------------------------------------
    async def send(self, text: str, turn_id: str) -> None:
        if not self.alive or self.session_id is None:
            raise unavailable("codex app-server is not running for this thread")
        params: dict[str, Any] = {
            "threadId": self.session_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        }
        trusted: dict[str, Any] | None = None
        if self.mode != self.applied_mode:
            policy, trusted = self._policy(_TURN_POLICY, _UNRESTRICTED_TURN)
            params.update(policy)
        self.turn_open = True
        self.codex_turn = None
        self.turn_error = None
        self._interrupt_wanted = False
        self._changes.clear()
        try:
            result = await self.server.request("turn/start", params, trusted=trusted)
        except BaseException:
            self.turn_open = False
            raise
        self.applied_mode = self.mode
        turn = result.get("turn") if isinstance(result, Mapping) else None
        started = turn.get("id") if isinstance(turn, Mapping) else None
        if isinstance(started, str):
            if self.codex_turn is None:
                self.codex_turn = started
            if not self.turn_open:
                # Ended before codex even answered (lost login): keep it ended.
                self._ended.add(started)
        if self._interrupt_wanted:
            if self.turn_open:
                await self.interrupt()
            elif isinstance(started, str):
                self.server.send_quietly(
                    "turn/interrupt", {"threadId": self.session_id, "turnId": started})

    @property
    def can_steer(self) -> bool:
        return self.alive and self.turn_open and self.codex_turn is not None

    async def steer(self, text: str) -> None:
        """Add ``text`` to the running turn (``turn/steer {expectedTurnId}``).

        Raises ``session_busy`` when the turn changed or ended under it.  The
        manager publishes the ``message.user`` for it.
        """
        if not self.alive or self.session_id is None:
            raise unavailable("codex app-server is not running for this thread")
        if not self.turn_open or self.codex_turn is None:
            raise ExternalError(errors.SESSION_BUSY, "no Codex turn is running to add to")
        try:
            await self.server.request(
                "turn/steer",
                {
                    "threadId": self.session_id,
                    "expectedTurnId": self.codex_turn,
                    "input": [{"type": "text", "text": text, "text_elements": []}],
                },
            )
        except CodexRpcError as exc:
            raise ExternalError(
                errors.SESSION_BUSY, "the running Codex turn changed; send it as a new message"
            ) from exc

    async def interrupt(self) -> None:
        if not self.turn_open:
            return
        if self.codex_turn is None:
            # turn/start has not answered yet: interrupt as soon as it does.
            self._interrupt_wanted = True
            return
        if self.alive and self.session_id is not None:
            try:
                await self.server.request(
                    "turn/interrupt", {"threadId": self.session_id, "turnId": self.codex_turn},
                    timeout=10, restart_on_timeout=False,
                )
            except CodexRpcError:
                # "no active turn to interrupt": it already ended on codex's side.
                await self._finish("interrupted")
                return
        if self._interrupt_timer is None or self._interrupt_timer.done():
            self._interrupt_timer = asyncio.ensure_future(self._interrupt_deadline())

    async def _interrupt_deadline(self) -> None:
        await asyncio.sleep(self.adapter.interrupt_grace_sec)
        if self.turn_open:
            await self._finish("interrupted")

    async def _finish(self, reason: str, error: str | None = None) -> None:
        if not self.turn_open:
            return
        self.turn_open = False
        if self.codex_turn is not None:
            self._ended.add(self.codex_turn)
        timer = self._interrupt_timer
        self._interrupt_timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        builder = self.host_api.builder
        builder.flush()
        await self.host_api.emit(builder.drain())
        await self.host_api.turn_finished(reason, error)

    # -- notifications ----------------------------------------------------
    async def on_notification(self, method: str, params: Mapping[str, Any]) -> None:
        if self.closed or self.dead:
            return
        builder = self.host_api.builder
        item = params.get("item")
        item = item if isinstance(item, Mapping) else None
        if params.get("turnId") in self._ended:
            return
        if method == "turn/started":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, Mapping) else None
            if (self.turn_open and self.codex_turn is None and isinstance(turn_id, str)
                    and turn_id not in self._ended):
                self.codex_turn = turn_id
            return
        if method == "turn/completed":
            await self._turn_completed(params)
            return
        if method == "thread/closed":
            # Unloaded under us: the next prompt resumes it.
            await self.server_died("codex closed the thread")
            return
        if method == "thread/name/updated":
            name = params.get("threadName")
            if isinstance(name, str) and name.strip():
                await self.host_api.title_known(name.strip())
            return
        if method == "error":
            await self._error(params)
            return
        if item is not None and item.get("type") == "fileChange" and item.get("id") is not None:
            # Kept even after snowpea ended the turn: its approval card shows the diff.
            self._changes[str(item["id"])] = item.get("changes")
            while len(self._changes) > MAX_KEPT_CHANGES:  # bounded under a flood
                self._changes.pop(next(iter(self._changes)))
        if not self.turn_open:
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            item_id = params.get("itemId")
            if isinstance(delta, str) and item_id is not None:
                builder.delta(delta, message_key=item_key(item_id, "message"))
        elif method == "item/started" and item is not None:
            if ITEM_FILTER.keep(item.get("type")):
                announce_item(builder, item, turn_id=None, ts=None)
        elif method == "item/completed" and item is not None:
            kind = item.get("type")
            if kind == "userMessage" or not ITEM_FILTER.keep(kind):
                return
            announce_item(builder, item, turn_id=None, ts=None)
            finish_item(builder, item, turn_id=None, ts=None)
        else:
            return
        await self.host_api.emit(builder.drain())

    async def _turn_completed(self, params: Mapping[str, Any]) -> None:
        if not self.turn_open:
            return
        turn = params.get("turn")
        turn = turn if isinstance(turn, Mapping) else {}
        turn_id = turn.get("id")
        if turn_id in self._ended:
            return
        if self.codex_turn is not None and turn_id is not None and turn_id != self.codex_turn:
            return
        status = turn.get("status")
        if status == "interrupted":
            await self._finish("interrupted")
        elif status == "failed":
            error = turn.get("error")
            message = self.turn_error or (
                friendly_error(error) if isinstance(error, Mapping) else ""
            )
            await self._finish("error", message or "the Codex turn failed")
        else:
            await self._finish("complete")

    async def _error(self, params: Mapping[str, Any]) -> None:
        error = params.get("error")
        if not isinstance(error, Mapping) or not self.turn_open:
            return
        if is_auth_error(error):
            # Do not sit through codex's reconnect attempts: say so and stop.
            self.turn_error = _NOT_LOGGED_IN
            if params.get("willRetry"):
                # This runs on the shared reader: never wait for an answer here.
                if self.codex_turn is not None and self.session_id is not None:
                    self.server.send_quietly(
                        "turn/interrupt", {"threadId": self.session_id,
                                           "turnId": self.codex_turn})
                else:
                    self._interrupt_wanted = True
                await self._finish("error", _NOT_LOGGED_IN)
            return
        if params.get("willRetry"):
            builder = self.host_api.builder
            message = str(error.get("message") or "retrying")[:300]
            builder.note(f"codex: {message}")
            await self.host_api.emit(builder.drain())
            return
        self.turn_error = friendly_error(error) or self.turn_error

    # -- approvals --------------------------------------------------------
    def on_approval_request(self, request_id: Any, method: str, params: Mapping[str, Any]
                            ) -> None:
        previous = self._approvals.get(request_id)
        if previous is not None and not previous.done():
            # A reused id while the first ask is open: that ask answers the id,
            # once; the copy is never asked (security review, codex channel).
            log.warning("codex reused approval id %r while it was open; ignored", request_id)
            return
        if self.closed or self.dead or len(
            [t for t in self._approvals.values() if not t.done()]
        ) >= MAX_OPEN_APPROVALS:
            # A closed channel or a flood of asks: declined without asking anyone.
            self.server.send_quietly_response(request_id, {"decision": "decline"})
            return
        task = asyncio.ensure_future(self._approve(request_id, method, params))
        self._approvals[request_id] = task
        task.add_done_callback(functools.partial(self._forget_approval, request_id))

    def _forget_approval(self, request_id: Any, task: asyncio.Task[None]) -> None:
        if self._approvals.get(request_id) is task:
            del self._approvals[request_id]

    def _ask(self, method: str, params: Mapping[str, Any]) -> ApprovalAsk:
        reason = str(params.get("reason") or "")
        if method == COMMAND_APPROVAL:
            return ApprovalAsk(
                tool="shell",
                args={"command": shell_script(str(params.get("command") or "")),
                      "cwd": str(params.get("cwd") or self.spec.cwd)},
                note=reason,
            )
        changes = self._changes.get(str(params.get("itemId")))
        paths = _paths_of(changes)
        note = reason
        if params.get("grantRoot"):
            extra = f"asks to write under {params['grantRoot']} for the rest of the session"
            note = f"{note} ({extra})" if note else extra
        return ApprovalAsk(
            tool="apply_patch",
            args={"paths": paths},
            note=note,
            diff=_diff_text(changes) or None,
            paths=paths or None,
        )

    @staticmethod
    def _decision(method: str, params: Mapping[str, Any], answer: ApprovalAnswer) -> str:
        if not answer.allow:
            return "decline"
        if answer.scope != "once":
            if method == FILE_APPROVAL:
                return "acceptForSession"
            offered = params.get("availableDecisions")
            if isinstance(offered, list) and "acceptForSession" in offered:
                return "acceptForSession"
        # Never ``acceptWithExecpolicyAmendment``: it writes the owner's codex rules.
        return "accept"

    def _outside_project(self, root: Any) -> bool:
        try:
            target = Path(str(root)).resolve()
            base = Path(self.spec.cwd).resolve()
        except (OSError, RuntimeError, ValueError):
            return True
        return target != base and base not in target.parents

    async def _approve(self, request_id: Any, method: str, params: Mapping[str, Any]) -> None:
        decision = "decline"
        grant_root = params.get("grantRoot") if method == FILE_APPROVAL else None
        try:
            if grant_root and self._outside_project(grant_root):
                # Write access outside the project for the rest of the session is
                # more than an edit approval can give: refused without asking.
                builder = self.host_api.builder
                builder.note(f"Refused codex's request to write under {grant_root} "
                             "(outside the project).")
                await self.host_api.emit(builder.drain())
                return
            answer = await self.host_api.ask_approval(self._ask(method, params))
            decision = self._decision(method, params, answer)
            if grant_root and decision == "acceptForSession":
                decision = "accept"  # a root grant is never widened to the session
        except asyncio.CancelledError:
            decision = "cancel"
            raise
        except Exception:  # noqa: BLE001 - a broken bridge declines, never hangs the turn
            log.warning("codex approval bridge failed", exc_info=True)
        finally:
            # Answer the process that asked, even after this thread was closed
            # under us; never a restarted one (its ids are not ours).
            if self._process is not None and self.server.process is self._process:
                await self.server.respond(request_id, {"decision": decision})

    def _cancel_approvals(self) -> None:
        for task in list(self._approvals.values()):
            task.cancel()

    # -- close, death -----------------------------------------------------
    # A mode switch (``set_mode``, the base's) applies with the next
    # ``turn/start`` as approvalPolicy + sandboxPolicy overrides.

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._cancel_approvals()
        timer = self._interrupt_timer
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        if not self.dead and self.attached and self.server.alive and self.session_id:
            if self.turn_open and self.codex_turn is not None:
                with contextlib.suppress(ExternalError):
                    await self.server.request(
                        "turn/interrupt",
                        {"threadId": self.session_id, "turnId": self.codex_turn},
                        timeout=5, restart_on_timeout=False,
                    )
            # Unsubscribing lets codex unload the thread and release its writer lock.
            with contextlib.suppress(ExternalError):
                await self.server.request("thread/unsubscribe", {"threadId": self.session_id},
                                          timeout=5, restart_on_timeout=False)
        self.server.detach(self.session_id, self)
        self.attached = False
        self.turn_open = False

    async def server_died(self, message: str) -> None:
        self.dead = True
        self._cancel_approvals()
        self.server.detach(self.session_id, self)
        if not self.closed:
            await self.host_api.channel_died(message)


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------


class CodexAdapter(ExternalAdapter):
    """OpenAI Codex CLI over one shared ``codex app-server``."""

    id = "codex"
    title = "Codex"
    binary = "codex"
    capabilities = CliCapabilities(
        streaming=True, approvals="bridged", live_view=True, tmux=False, modes=ALL_MODES,
        steer=True,
    )
    forbidden_args = (
        "--dangerously-bypass-approvals-and-sandbox",
        "--yolo",
        "--full-auto",
        "--approve-for-me",
    )
    forbidden_pairs = (
        ("-a", "never"),
        ("--ask-for-approval", "never"),
        ("-s", "danger-full-access"),
        ("--sandbox", "danger-full-access"),
    )
    builtin_default_mode = "default"

    def __init__(self) -> None:
        self.server = AppServer(self)
        self.executable: PinnedExecutable | None = None
        self._supervisor: ProcessSupervisor | None = None
        self._env: dict[str, str] | None = None
        self._own_supervisor: ProcessSupervisor | None = None
        self._writer_flags: dict[str, float] = {}
        # Tunables (tests shorten them).
        # codex 0.167 keeps an unsubscribed thread (and its writer lock) for
        # 60 s; stopping the idle server after as long frees every lock.
        self.server_idle_sec = 60.0
        self.request_timeout_sec = 60.0
        self.list_timeout_sec = 120.0
        self.start_timeout_sec = 30.0
        self.interrupt_grace_sec = 5.0
        self.backoff_sec = 0.5
        self.max_backoff_sec = 30.0
        self.max_crashes = 6
        self.crash_window_sec = 120.0

    # -- processes --------------------------------------------------------
    def spawn_context(self) -> tuple[ProcessSupervisor, dict[str, str]]:
        """The daemon's supervisor once a channel handed it over, else our own.

        Either way the pid file lands under ``$SNOWPEA_HOME/external/``, so a
        daemon that dies leaves nothing behind (the next start reaps it).
        """
        if self._supervisor is not None:
            return self._supervisor, dict(self._env or proc.scrubbed_env())
        if self.supervisor is not None:
            # Bound by the daemon's manager before any channel ran.
            return self.supervisor, proc.scrubbed_env()
        if self._own_supervisor is None:
            self._own_supervisor = ProcessSupervisor(resolve_home(), max_processes=1)
        return self._own_supervisor, proc.scrubbed_env()

    async def ensure_server(self, executable: PinnedExecutable | None = None) -> AppServer:
        exe = executable or self.executable
        if exe is None:
            raise unavailable("codex was not found on this machine")
        await self.server.ensure(exe)
        return self.server

    async def stop_server(self) -> None:
        """Stop the shared app-server: stdin closed, so codex releases its locks and exits."""
        await self.server.stop()

    async def shutdown(self) -> None:
        """Daemon stop: the shared app-server goes gracefully, not by SIGKILL."""
        await self.stop_server()
        if self._own_supervisor is not None:
            await self._own_supervisor.shutdown()

    def codex_home(self) -> Path:
        if self.server.codex_home:
            return Path(self.server.codex_home)
        env = (self._env or os.environ).get("CODEX_HOME")
        return Path(env).expanduser() if env else Path.home() / ".codex"

    # -- discovery --------------------------------------------------------
    async def discover(self, path_override: str | None = None) -> Discovery:
        found = await super().discover(path_override)
        if not found.installed or found.executable is None:
            return found
        self.executable = found.executable
        version = parse_version(found.executable.version)
        if found.problems:
            return found
        if version is not None and version < MIN_VERSION:
            found.problems.append(
                f"codex-cli {_dotted(version)} is too old: snowpea needs "
                f"{_dotted(MIN_VERSION)} or newer (its app-server thread API); update codex"
            )
            return found
        try:
            await self.ensure_server(found.executable)
            account = await self.server.request("account/read", {}, timeout=30)
        except ExternalError as exc:
            message = exc.message
            if version is not None and version > TESTED_VERSION:
                message += (
                    f" (codex-cli {_dotted(version)} is newer than "
                    f"{_dotted(TESTED_VERSION)}, the newest snowpea was checked with; its "
                    "app-server protocol may have changed)"
                )
            found.problems.append(message)
            return found
        if isinstance(account, Mapping) and account.get("account") is None and account.get(
            "requiresOpenaiAuth", True
        ):
            found.problems.append(_NOT_LOGGED_IN)
        return found

    def approvals(self, mode: str, discovery: Discovery | None) -> Approvals:
        # readonly runs with approvalPolicy never: codex asks nothing.
        if mode in ("readonly", "unrestricted"):
            return "none"
        return "bridged"

    async def list_models(self, discovery: Discovery | None = None) -> list[str]:
        """Model ids the app-server offers (``model/list``), default first."""
        exe = discovery.executable if discovery is not None else None
        await self.ensure_server(exe)
        out: list[str] = []
        cursor: str | None = None
        for _ in range(MODELS_MAX_PAGES):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            try:
                result = await self.server.request("model/list", params)
            except CodexRpcError:
                return out
            data = result.get("data") if isinstance(result, Mapping) else None
            for model in data if isinstance(data, list) else []:
                if not isinstance(model, Mapping) or model.get("hidden"):
                    continue
                name = model.get("model") or model.get("id")
                if not isinstance(name, str) or name in out:
                    continue
                try:
                    proc.validate_model(name)
                except ExternalError:
                    continue
                if model.get("isDefault"):
                    out.insert(0, name)
                else:
                    out.append(name)
            cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
            if not cursor:
                break
        return out

    # -- sessions ---------------------------------------------------------
    def _info(self, thread: Mapping[str, Any]) -> SessionInfo | None:
        thread_id = thread.get("id")
        cwd = thread.get("cwd")
        if not isinstance(thread_id, str) or not isinstance(cwd, str) or not cwd:
            return None
        try:
            proc.validate_session_id(thread_id)
        except ExternalError:
            return None
        raw_preview = thread.get("preview")
        preview = raw_preview if isinstance(raw_preview, str) else ""
        name = thread.get("name") if isinstance(thread.get("name"), str) else None
        first = preview.strip().splitlines()[0][:80] if preview.strip() else None
        updated = thread.get("updatedAt") or thread.get("createdAt")
        channel = self.server.channels.get(thread_id)
        return SessionInfo(
            id=thread_id,
            cwd=cwd,
            title=(name or "").strip() or first,
            updated_at=iso_from_unix(updated) if updated is not None else None,
            status="running" if channel is not None and channel.turn_open else "idle",
            model=thread.get("model") if isinstance(thread.get("model"), str) else None,
            preview=preview,
            extra={
                "path": thread.get("path") if isinstance(thread.get("path"), str) else None,
                "source": thread.get("source"),
                "updated": updated,
            },
        )

    async def _list_page(self, params: dict[str, Any]) -> tuple[list[Mapping[str, Any]],
                                                                  str | None]:
        result = await self.server.request("thread/list", params,
                                           timeout=self.list_timeout_sec)
        if not isinstance(result, Mapping):
            return [], None
        data = result.get("data")
        rows = [row for row in data if isinstance(row, Mapping)] if isinstance(data, list) else []
        cursor = result.get("nextCursor")
        return rows, cursor if isinstance(cursor, str) and cursor else None

    async def list_sessions(
        self, roots: Sequence[str], *, limit: int, before: str | None = None
    ) -> list[SessionInfo]:
        if not roots or limit <= 0:
            return []
        await self.ensure_server()
        bases = [Path(root) for root in roots]

        def wanted(info: SessionInfo, *, exact_only: bool) -> bool:
            if before is not None and (info.updated_at or "") > before:
                return False
            cwd = Path(info.cwd)
            if exact_only:
                return cwd in bases
            return any(base in cwd.parents for base in bases)

        found: dict[str, SessionInfo] = {}
        base_params: dict[str, Any] = {
            "limit": LIST_PAGE,
            "sortKey": "updated_at",
            "sourceKinds": list(SOURCE_KINDS),
        }
        # 1. Exactly these folders (filtered by the server).
        cursor: str | None = None
        for _ in range(LIST_MAX_PAGES):
            params = {**base_params, "cwd": [str(base) for base in bases]}
            if cursor:
                params["cursor"] = cursor
            rows, cursor = await self._list_page(params)
            for row in rows:
                if row.get("parentThreadId"):
                    continue
                info = self._info(row)
                if info is not None and wanted(info, exact_only=True):
                    found.setdefault(info.id, info)
            if cursor is None or len(found) >= limit:
                break
        # 2. Sub-folders: the server only filters exact paths, so look through
        #    the most recent threads.
        cursor = None
        for _ in range(SCAN_MAX_PAGES):
            # The state DB alone: a rollout scan-and-repair costs a second a page.
            params = {**base_params, "useStateDbOnly": True}
            if cursor:
                params["cursor"] = cursor
            rows, cursor = await self._list_page(params)
            for row in rows:
                if row.get("parentThreadId"):
                    continue
                info = self._info(row)
                if info is not None and wanted(info, exact_only=False):
                    found.setdefault(info.id, info)
            if cursor is None:
                break
        rows_out = sorted(found.values(), key=lambda info: info.updated_at or "", reverse=True)
        return rows_out[:limit]

    async def get_session(self, session_id: str) -> SessionInfo | None:
        proc.validate_session_id(session_id)
        await self.ensure_server()
        try:
            result = await self.server.request("thread/read", {"threadId": session_id})
        except CodexRpcError:
            return None
        thread = result.get("thread") if isinstance(result, Mapping) else None
        return self._info(thread) if isinstance(thread, Mapping) else None

    async def _turns(self, thread_id: str) -> list[Mapping[str, Any]]:
        turns: list[Mapping[str, Any]] = []
        cursor: str | None = None
        try:
            while len(turns) < MAX_TURNS:
                params: dict[str, Any] = {
                    "threadId": thread_id,
                    "limit": TURNS_PAGE,
                    "itemsView": "full",
                    "sortDirection": "asc",
                }
                if cursor:
                    params["cursor"] = cursor
                result = await self.server.request("thread/turns/list", params,
                                                   timeout=self.list_timeout_sec)
                data = result.get("data") if isinstance(result, Mapping) else None
                turns.extend(turn for turn in data or [] if isinstance(turn, Mapping))
                cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
                if not cursor:
                    break
        except CodexRpcError as exc:
            if exc.rpc_code != -32601:
                raise
            # Older servers: the whole history in one read.
            result = await self.server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": True},
                timeout=self.list_timeout_sec,
            )
            thread = result.get("thread") if isinstance(result, Mapping) else None
            raw = thread.get("turns") if isinstance(thread, Mapping) else None
            turns = [turn for turn in raw or [] if isinstance(turn, Mapping)]
        if turns and all(isinstance(turn.get("startedAt"), (int, float)) for turn in turns):
            turns.sort(key=lambda turn: turn["startedAt"])
        return turns

    async def read(self, info: SessionInfo) -> list[TimelineItem]:
        await self.ensure_server()
        turns = await self._turns(proc.validate_session_id(info.id))
        return convert_turns(turns, TimelineBuilder(self.id, info.id))

    # -- liveness ---------------------------------------------------------
    def flag_writer(self, thread_id: str) -> None:
        """``thread/resume`` said another process writes this thread."""
        self._writer_flags[thread_id] = time.monotonic()

    def _flagged(self, thread_id: str) -> bool:
        stamp = self._writer_flags.get(thread_id)
        if stamp is None:
            return False
        if time.monotonic() - stamp > WRITER_FLAG_SEC:
            del self._writer_flags[thread_id]
            return False
        return True

    def lock_path(self, thread_id: str) -> Path:
        return self.codex_home() / "thread-writer-locks" / f"{thread_id}.lock"

    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        thread_id = proc.validate_session_id(info.id)
        channel = self.server.channels.get(thread_id)
        if channel is not None and channel.alive:
            return Liveness()
        busy = self._recently_written(info)
        if self._flagged(thread_id):
            return Liveness(host="terminal", busy=busy)
        holders = await asyncio.to_thread(lock_holders, self.lock_path(thread_id))
        if not holders:
            return Liveness()
        ours = set(own_pids)
        if self.server.pid is not None:
            ours.add(self.server.pid)
        if holders - ours:
            return Liveness(host="terminal", busy=busy)
        return Liveness()

    @staticmethod
    def _recently_written(info: SessionInfo) -> bool:
        path = info.extra.get("path")
        if not isinstance(path, str) or not path:
            return False
        try:
            return time.time() - os.stat(path).st_mtime < BUSY_MTIME_SEC
        except OSError:
            return False

    async def follow(
        self, info: SessionInfo, *, interval: float = 1.0
    ) -> AsyncIterator[list[TimelineItem]]:
        """Re-read only when the thread changed (rollout mtime, else ``updatedAt``)."""
        last: Any = None
        current = info
        while True:
            await asyncio.sleep(interval)
            path = current.extra.get("path")
            stamp: Any = None
            if isinstance(path, str) and path:
                with contextlib.suppress(OSError):
                    stat = os.stat(path)
                    stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp is None:
                fresh = await self.get_session(current.id)
                if fresh is not None:
                    current = fresh
                stamp = current.extra.get("updated")
            if stamp == last:
                continue
            last = stamp
            fresh = await self.get_session(current.id)
            if fresh is not None:
                current = fresh
            yield await self.read(current)

    # -- running ----------------------------------------------------------
    def channel(self, spec: LaunchSpec, host: ChannelHost) -> ExternalChannel:
        # The daemon's supervisor counts the shared server against maxProcesses
        # and stops it at shutdown; remember it for the next (re)start.
        self._supervisor = host.supervisor
        self._env = dict(host.env)
        return CodexChannel(self, spec, host)


__all__ = [
    "APPROVAL_METHODS",
    "AppServer",
    "CodexAdapter",
    "CodexChannel",
    "CodexRpcError",
    "MIN_VERSION",
    "TESTED_VERSION",
    "check_policy",
    "convert_turns",
    "friendly_error",
    "is_auth_error",
    "lock_holders",
    "parse_version",
    "user_text",
]
