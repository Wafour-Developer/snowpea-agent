"""Antigravity CLI (``agy``) adapter (M19 WP6c).

Ground truth: ``docs/design/m19-spikes/agy.md`` (agy 1.5.7, re-checked on
1.5.9).  Everything runs over ``agy acp`` — the Agent Client Protocol,
newline-delimited JSON-RPC 2.0 on stdio:

* **Sessions snowpea runs**: one ``agy acp`` child per open session
  (:class:`AgyChannel`): ``initialize``, then ``session/new`` or
  ``session/load``, ``session/prompt`` per turn, the ``session/cancel``
  notification to interrupt, ``session/request_permission`` (string ids)
  bridged into snowpea's approval queue.
* **Listing** reads agy's summaries database strictly read-only
  (``conversation_summaries.db``), filtered by working directory on our side;
  without a usable database, ACP ``session/list`` in a short-lived helper.
* **History** is ``session/load`` in a short-lived helper: agy replays the
  conversation as ``session/update`` notifications before the result.  It
  spends no tokens.
* **Open elsewhere**: an interactive ``agy`` flock-holds
  ``presence/<id>.lock``; we look for the lock in ``/proc/locks`` (``lsof``
  on macOS) and never take it.  While it is held nothing loads the
  conversation; the view shows the text agy indexed for search (the
  ``conversations_fts`` table) instead.
* **Approval policy** is the owner's own agy setting (``toolPermission`` in
  agy's ``settings.json``); agy ignores any per-session override.  With
  ``always-proceed`` no permission request ever arrives, so a session is
  reported as ``approvals: none`` and ``controlled: false`` instead of
  promising control snowpea does not have.
* **Modes**: ``default`` and ``unrestricted``
  (``--dangerously-skip-permissions``, only through :func:`proc.bypass_args`).
  ``readonly`` is not offered: agy's ``plan`` mode writes a plan and then
  executes it once the chat says so, and in-workspace edits never prompt,
  so nothing would actually keep the session read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import difflib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.external import proc
from snowpea_core.external.base import (
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
)
from snowpea_core.external.proc import (
    ExternalError,
    ExternalProcess,
    PinnedExecutable,
    ProcessSupervisor,
)
from snowpea_core.external.timeline import (
    TimelineBuilder,
    TimelineItem,
    iso_from_text,
    stable_key,
)
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.agy")

#: The ACP protocol version this adapter speaks.
ACP_PROTOCOL_VERSION = 1
#: Oldest agy release whose ACP behaviour the spike recorded.
MIN_VERSION: tuple[int, int, int] = (1, 5, 0)
#: The major release this adapter was written against.
SUPPORTED_MAJOR = 1
#: ``toolPermission`` values under which agy runs tools without asking.
AUTO_POLICIES: frozenset[str] = frozenset({"always-proceed", "proceed-in-sandbox"})
#: agy's own default when the setting is absent.
DEFAULT_POLICY = "request-review"
#: How to sign in, for the "not logged in" message.
LOGIN_HINT = "agy acp --login"
#: Words in a ``session/new``/``session/load`` error that mean "not signed in".
_AUTH_WORDS = re.compile(
    r"auth|sign(?:ed)?[ -]?in|log(?:ged)?[ -]?in|credential|oauth|token|"
    r"failed to start antigravity",
    re.IGNORECASE,
)
#: A version number inside ``agy --version`` output.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
#: Seconds to wait for ``initialize``.
INIT_TIMEOUT_SEC = 30.0
#: Seconds to wait for ``session/new`` / ``session/load`` (the first one
#: starts agy's language server).
SESSION_TIMEOUT_SEC = 120.0
#: Seconds to wait for ``session/list`` and small control calls.
CALL_TIMEOUT_SEC = 30.0
#: Seconds ``agy models`` may take (it asks the server).
MODELS_TIMEOUT_SEC = 30.0
#: Seconds a model list is reused.
MODELS_TTL_SEC = 600.0
#: Pages of ACP ``session/list`` followed at most.
MAX_LIST_PAGES = 20

_TEXT_UPDATES = ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk")


def default_data_dir() -> Path:
    """Where agy keeps its settings, summaries database and presence locks."""
    return Path.home() / ".gemini" / "antigravity-cli"


# ---------------------------------------------------------------------------
# versions and settings
# ---------------------------------------------------------------------------


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """``"1.5.9"`` / ``"agy 1.5.9 (build)"`` -> ``(1, 5, 9)``."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def version_problem(text: str | None) -> str | None:
    """A problem line when this agy release is outside what the adapter supports."""
    version = parse_version(text)
    if version is None:
        return f"could not read the agy version from {text!r}" if text else None
    shown = ".".join(str(part) for part in version)
    if version < MIN_VERSION:
        oldest = ".".join(str(part) for part in MIN_VERSION)
        return f"agy {shown} is too old; snowpea needs {oldest} or newer (run `agy update`)"
    if version[0] > SUPPORTED_MAJOR:
        return (
            f"agy {shown} is newer than this snowpea supports ({SUPPORTED_MAJOR}.x); "
            "update snowpea"
        )
    return None


def read_tool_permission(data_dir: Path) -> str:
    """agy's ``toolPermission`` setting (only that key is read)."""
    try:
        raw = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_POLICY
    value = raw.get("toolPermission") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else DEFAULT_POLICY


def policy_of(tool_permission: str) -> str:
    """``asks`` when agy sends permission requests, ``auto`` when it runs tools unasked."""
    return "auto" if tool_permission in AUTO_POLICIES else "asks"


def _sign_in_hint(data_dir: Path) -> bool | None:
    """``True`` when a sign-in is visible without reading any secret, else unknown.

    Only the *existence* of agy's token file and of the API-key variables is
    checked.  A missing file is not proof of "signed out" (enterprise and
    key-based sign-ins live elsewhere), so this never says ``False``.
    """
    if any(os.environ.get(name) for name in
           ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")):
        return True
    with contextlib.suppress(OSError):
        if (data_dir / "antigravity-oauth-token").exists():
            return True
    return None


@dataclass
class AgyDiscovery(Discovery):
    """:class:`Discovery` plus what agy's own settings say."""

    #: The raw ``toolPermission`` value (``always-proceed``, ``request-review``, ...).
    tool_permission: str | None = None
    #: ``True`` when a sign-in is visible; ``None`` when it is unknown until a
    #: session starts (agy then fails ``session/new`` with a clear message).
    signed_in: bool | None = None


def not_signed_in(detail: str = "") -> ExternalError:
    message = f"agy is not signed in; run `{LOGIN_HINT}` in a terminal, then try again"
    if detail:
        message += f" ({detail[:200]})"
    return ExternalError(
        errors.EXTERNAL_UNAVAILABLE, message, {"needsLogin": True, "hint": LOGIN_HINT}
    )


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------


class AcpError(Exception):
    """A JSON-RPC error answer from agy."""

    def __init__(self, code: Any, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AcpGone(Exception):
    """agy exited (or stopped reading) before answering."""


UpdateFn = Callable[[dict[str, Any]], Awaitable[None]]
RequestFn = Callable[[dict[str, Any]], Awaitable[None]]
ExitFn = Callable[[int | None], Awaitable[None]]


class AcpConnection:
    """Requests, notifications and agent-to-client requests over one ``agy acp``."""

    def __init__(
        self,
        process: ExternalProcess,
        *,
        on_update: UpdateFn | None = None,
        on_request: RequestFn | None = None,
        on_exit: ExitFn | None = None,
    ) -> None:
        self.process = process
        self.on_update = on_update
        self.on_request = on_request
        self.on_exit = on_exit
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.exit_code: int | None = None
        self.gone = False

    def start(self) -> None:
        self._reader = asyncio.ensure_future(self._read())

    def _fail_pending(self) -> None:
        self.gone = True
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(AcpGone("agy exited"))

    async def _read(self) -> None:
        try:
            async for frame in self.process.json_lines():
                await self._dispatch(frame)
        except asyncio.CancelledError:
            self._fail_pending()
            raise
        except Exception:  # noqa: BLE001 - a broken frame handler must not hide the exit
            log.debug("agy reader failed", exc_info=True)
        self._fail_pending()
        code: int | None = None
        with contextlib.suppress(Exception):
            code = await asyncio.wait_for(self.process.wait(), timeout=5)
        self.exit_code = code
        if self.on_exit is not None:
            await self.on_exit(code)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        if isinstance(method, str):
            if "id" in frame:
                if self.on_request is not None:
                    await self.on_request(frame)
                else:
                    await self.reply_error(frame.get("id"), -32601, f"Method not found: {method}")
            elif method == "session/update" and self.on_update is not None:
                params = frame.get("params")
                update = params.get("update") if isinstance(params, dict) else None
                if isinstance(update, dict):
                    await self.on_update(update)
            return
        rid = frame.get("id")
        if not isinstance(rid, int):
            return
        future = self._pending.pop(rid, None)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(AcpError(error.get("code"), str(error.get("message") or "")))
        else:
            result = frame.get("result")
            future.set_result(result if isinstance(result, dict) else {})

    async def start_request(self, method: str, params: dict[str, Any]
                            ) -> asyncio.Future[dict[str, Any]]:
        """Write a request; the future resolves with its result (or raises)."""
        if self.gone:
            raise AcpGone("agy is not running")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self.process.write_line(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            )
        except ExternalError as exc:
            self._pending.pop(rid, None)
            raise AcpGone(exc.message) from exc
        return future

    async def request(self, method: str, params: dict[str, Any], *, timeout: float
                      ) -> dict[str, Any]:
        future = await self.start_request(method, params)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"agy did not answer {method} in {timeout:.0f}s"
            ) from None

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        with contextlib.suppress(ExternalError):
            await self.process.write_line({"jsonrpc": "2.0", "method": method, "params": params})

    async def reply(self, rid: Any, result: dict[str, Any]) -> None:
        # Echo the id verbatim: agy's are strings.
        with contextlib.suppress(ExternalError):
            await self.process.write_line({"jsonrpc": "2.0", "id": rid, "result": result})

    async def reply_error(self, rid: Any, code: int, message: str) -> None:
        with contextlib.suppress(ExternalError):
            await self.process.write_line(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
            )

    async def initialize(self) -> dict[str, Any]:
        """The ACP handshake; refuses a protocol version this adapter does not speak."""
        try:
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    # agy runs its own tools; the client implements no fs or terminal.
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "snowpea", "version": "1"},
                },
                timeout=INIT_TIMEOUT_SEC,
            )
        except AcpGone as exc:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"agy acp exited during startup{self._tail()}"
            ) from exc
        except AcpError as exc:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE, f"agy refused the ACP handshake: {exc.message}"
            ) from exc
        version = result.get("protocolVersion")
        if version != ACP_PROTOCOL_VERSION:
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"agy speaks ACP protocol version {version!r}; this snowpea speaks "
                f"{ACP_PROTOCOL_VERSION}. Update snowpea (or agy) so they match",
            )
        return result

    def _tail(self) -> str:
        tail = self.process.stderr_tail(3).strip()
        return f": {tail[-300:]}" if tail else ""

    async def close(self, grace: float = 1.0) -> None:
        await self.process.terminate(grace=grace)
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def _session_error(exc: AcpError, action: str) -> ExternalError:
    """Map a ``session/new``/``session/load`` error (spike: "not signed in" is -32603)."""
    if _AUTH_WORDS.search(exc.message or ""):
        return not_signed_in(exc.message)
    return ExternalError(errors.EXTERNAL_UNAVAILABLE, f"agy could not {action}: {exc.message}")


# ---------------------------------------------------------------------------
# updates -> timeline
# ---------------------------------------------------------------------------


def _text_of(update: dict[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _tool_name(update: dict[str, Any]) -> str:
    for key in ("name", "kind"):
        value = update.get(key)
        if isinstance(value, str) and value:
            return value
    return "tool"


def _tool_args(update: dict[str, Any]) -> dict[str, Any]:
    for key in ("rawInput", "args"):
        value = update.get(key)
        if isinstance(value, dict):
            return dict(value)
    return {}


def _tool_output(update: dict[str, Any]) -> str:
    raw = update.get("rawOutput")
    if isinstance(raw, dict) and isinstance(raw.get("output"), str):
        return raw["output"]
    if isinstance(raw, str):
        return raw
    parts: list[str] = []
    for block in update.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "content":
            inner = block.get("content")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                parts.append(inner["text"])
    return "\n".join(part for part in parts if part)


def _unified(path: str, old: str | None, new: str) -> str:
    before = (old or "").splitlines(keepends=True)
    after = new.splitlines(keepends=True)
    name = path.lstrip("/")
    return "".join(
        difflib.unified_diff(
            before, after, fromfile="/dev/null" if old is None else f"a/{name}",
            tofile=f"b/{name}",
        )
    )


def _diffs(update: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for block in update.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "diff":
            continue
        path, new = block.get("path"), block.get("newText")
        if not isinstance(path, str) or not isinstance(new, str):
            continue
        old = block.get("oldText")
        out.append((path, _unified(path, old if isinstance(old, str) else None, new)))
    return out


def apply_tool_update(
    builder: TimelineBuilder,
    update: dict[str, Any],
    *,
    denied: dict[str, str] | None = None,
    turn_id: str | None = None,
) -> None:
    """``tool_call`` / ``tool_call_update`` -> ``tool.call``, ``diff``, ``tool.result``."""
    raw_id = update.get("toolCallId")
    raw_id = raw_id if isinstance(raw_id, str) and raw_id else None
    name = _tool_name(update)
    builder.tool_call(raw_id, name, _tool_args(update), turn_id=turn_id)
    status = update.get("status")
    if status not in ("completed", "failed"):
        return
    for path, patch in _diffs(update):
        if patch:
            builder.diff(path, patch, raw_call_id=raw_id, turn_id=turn_id)
    output = _tool_output(update)
    if status == "completed":
        builder.tool_result(raw_id, ok=True, output=output, name=name, turn_id=turn_id)
        return
    reason = (denied or {}).get(raw_id or "")
    error = f"Denied: {reason}" if reason else (output or "the tool failed")
    builder.tool_result(raw_id, ok=False, error=error, name=name, turn_id=turn_id)


def _user_key(session_id: str, index: int) -> str:
    return stable_key("agy", session_id, "history", "user", index)


def _assistant_key(session_id: str, index: int) -> str:
    return stable_key("agy", session_id, "history", "assistant", index)


def history_items(session_id: str, updates: Sequence[dict[str, Any]]) -> list[TimelineItem]:
    """A ``session/load`` replay as timeline items, oldest first.

    Consecutive chunks of one kind are one message.  User and assistant
    messages are keyed by their position, the same keys the search-index
    view (:func:`indexed_items`) uses, so a view shown while the session was
    open elsewhere is not repeated when the full history arrives.
    """
    builder = TimelineBuilder("agy", session_id)
    counters = {"user_message_chunk": 0, "agent_message_chunk": 0, "agent_thought_chunk": 0}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        kind, text = current, "".join(buffer)
        current, buffer = None, []
        if kind is None:
            return
        index = counters[kind]
        if kind == "user_message_chunk":
            if text.strip():
                counters[kind] += 1
                builder.user(text.strip(), key=_user_key(session_id, index))
        elif kind == "agent_message_chunk":
            if text.strip():
                counters[kind] += 1
                builder.assistant(text, key=_assistant_key(session_id, index))
        elif text.strip():
            counters[kind] += 1
            builder.reasoning(text, key=stable_key("agy", session_id, "history", "thought", index))

    for update in updates:
        kind = update.get("sessionUpdate")
        if kind in _TEXT_UPDATES:
            if kind != current:
                flush()
                current = kind
            buffer.append(_text_of(update))
        elif kind in ("tool_call", "tool_call_update"):
            flush()
            apply_tool_update(builder, update)
        # Anything else (commands, usage, titles, config, unknown kinds) is not conversation.
    flush()
    builder.flush()
    return builder.drain()


def _split_body(body: Any, offsets: Any) -> list[tuple[int, str]]:
    """``[(step, text)]`` from an FTS body and its ``[[step, byteOffset], ...]`` list."""
    if isinstance(offsets, bytes):
        offsets = offsets.decode("utf-8", "replace")
    if not isinstance(offsets, str) or not offsets.strip():
        return []
    try:
        marks = json.loads(offsets)
    except ValueError:
        return []
    data = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    clean = [
        (step, offset)
        for step, offset in (mark for mark in (marks if isinstance(marks, list) else [])
                             if isinstance(mark, list) and len(mark) == 2)
        if isinstance(step, int) and isinstance(offset, int)
    ]
    out: list[tuple[int, str]] = []
    for index, (step, offset) in enumerate(clean):
        end = clean[index + 1][1] if index + 1 < len(clean) else len(data)
        out.append((step, data[offset:end].decode("utf-8", "replace")))
    return out


# ---------------------------------------------------------------------------
# the summaries database (read-only)
# ---------------------------------------------------------------------------


class _Unusable(Exception):
    """The summaries database is missing, locked beyond reading or of an unknown shape."""


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open ``path`` strictly read-only; ``immutable`` when a plain read-only open fails."""
    if not path.is_file():
        raise _Unusable("no summaries database")
    base = path.resolve().as_uri()
    last: Exception | None = None
    for query in ("?mode=ro", "?mode=ro&immutable=1"):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(base + query, uri=True, timeout=0.5, check_same_thread=False)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.Error as exc:
            last = exc
            if conn is not None:
                conn.close()
    raise _Unusable(f"summaries database unreadable: {last}")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalise_time(value: Any) -> str | None:
    """``2026-11-15 12:03:58.610161143+00:00`` -> UTC ISO (nanoseconds trimmed)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1)
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    return iso_from_text(text)


def _path_of_uri(uri: Any) -> str | None:
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return None
    parsed = urllib.parse.urlparse(uri)
    path = urllib.request.url2pathname(parsed.path)
    if sys.platform == "win32" and re.match(r"^[\\/][A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if path else None


def _workspaces(raw: Any) -> list[str]:
    try:
        uris = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if not isinstance(uris, list):
        return []
    return [path for path in (_path_of_uri(uri) for uri in uris) if path]


_SUMMARY_COLUMNS = ("conversation_id", "workspace_uris", "last_modified_time")
_OPTIONAL_COLUMNS = ("title", "preview", "step_count", "nesting_depth")


def _info_from_row(row: dict[str, Any]) -> SessionInfo | None:
    session_id = row.get("conversation_id")
    if not isinstance(session_id, str) or not proc.SESSION_ID_RE.fullmatch(session_id):
        return None
    folders = _workspaces(row.get("workspace_uris"))
    if not folders:
        return None
    title = row.get("title") if isinstance(row.get("title"), str) else ""
    preview = row.get("preview") if isinstance(row.get("preview"), str) else ""
    return SessionInfo(
        id=session_id,
        cwd=folders[0],
        title=title or None,
        updated_at=_normalise_time(row.get("last_modified_time")),
        preview=preview or title or "",
        extra={"source": "db", "workspaces": folders},
    )


def read_summaries(path: Path, session_id: str | None = None) -> list[SessionInfo]:
    """Top-level conversations from agy's summaries database (blocking; run in a thread)."""
    conn = _connect_ro(path)
    try:
        columns = _columns(conn, "conversation_summaries")
        if not set(_SUMMARY_COLUMNS) <= columns:
            raise _Unusable("summaries database has an unknown shape")
        wanted = [*_SUMMARY_COLUMNS, *(c for c in _OPTIONAL_COLUMNS if c in columns)]
        sql = f"SELECT {', '.join(wanted)} FROM conversation_summaries"
        where: list[str] = []
        args: list[Any] = []
        if "nesting_depth" in columns:
            where.append("nesting_depth = 0")  # subagent conversations are not listed
        if session_id is not None:
            where.append("conversation_id = ?")
            args.append(session_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise _Unusable(f"summaries database unreadable: {exc}") from exc
    finally:
        conn.close()
    out = []
    for values in rows:
        info = _info_from_row(dict(zip(wanted, values, strict=False)))
        if info is not None:
            out.append(info)
    return out


def read_indexed(path: Path, session_id: str) -> list[tuple[int, str, str]] | None:
    """``[(step, "user"|"agent", text)]`` from the search index, or ``None``.

    The ``conversations_fts`` table keeps the user and agent text of each
    conversation in chunks with per-step byte offsets.  It is the only
    transcript form readable without loading the conversation.
    """
    try:
        conn = _connect_ro(path)
    except _Unusable:
        return None
    try:
        columns = _columns(conn, "conversations_fts")
        needed = {"conversation_id", "chunk_index", "user_body", "agent_body",
                  "user_step_byte_offsets", "agent_step_byte_offsets"}
        if not needed <= columns:
            return None
        rows = conn.execute(
            "SELECT chunk_index, user_body, agent_body, user_step_byte_offsets, "
            "agent_step_byte_offsets FROM conversations_fts WHERE conversation_id = ?",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    steps: list[tuple[int, str, str]] = []
    for chunk, user_body, agent_body, user_offsets, agent_offsets in rows:
        if not isinstance(chunk, int) or chunk < 0:
            continue  # chunk -1 is the title row
        steps += [(step, "user", text) for step, text in _split_body(user_body, user_offsets)]
        steps += [(step, "agent", text) for step, text in _split_body(agent_body, agent_offsets)]
    steps.sort(key=lambda entry: (entry[0], entry[1] != "user"))
    return steps


def indexed_items(session_id: str, steps: Sequence[tuple[int, str, str]]) -> list[TimelineItem]:
    """Timeline items from :func:`read_indexed` (same keys as :func:`history_items`)."""
    builder = TimelineBuilder("agy", session_id)
    users = agents = 0
    for _step, who, text in steps:
        if not text.strip():
            continue
        if who == "user":
            builder.user(text.strip(), key=_user_key(session_id, users))
            users += 1
        else:
            builder.assistant(text.strip(), key=_assistant_key(session_id, agents))
            agents += 1
    builder.flush()
    return builder.drain()


# ---------------------------------------------------------------------------
# presence locks (never taken)
# ---------------------------------------------------------------------------


def _proc_locks_holders(path: Path) -> set[int]:
    """Pids holding a lock on ``path`` per ``/proc/locks`` (Linux)."""
    try:
        stat = path.stat()
        table = Path("/proc/locks").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    exact: set[int] = set()
    inode_only: set[int] = set()
    for line in table.splitlines():
        fields = line.split()
        # "1: FLOCK  ADVISORY  WRITE 1234 fd:01:5678 0 EOF"; "->" marks a waiter.
        if len(fields) < 6 or fields[1] == "->":
            continue
        try:
            pid = int(fields[4])
            major, minor, inode = fields[5].split(":")
        except ValueError:
            continue
        if not inode.isdigit() or int(inode) != stat.st_ino:
            continue
        try:
            same_dev = (int(major, 16), int(minor, 16)) == (
                os.major(stat.st_dev), os.minor(stat.st_dev))
        except ValueError:
            same_dev = False
        (exact if same_dev else inode_only).add(pid)
    # btrfs and overlay report a different device in stat than in /proc/locks.
    return exact or inode_only


def _lsof_holders(path: Path) -> set[int]:
    """Pids with ``path`` open (macOS: the TUI keeps its lock file open while it holds it)."""
    lsof = shutil.which("lsof")
    if lsof is None or not path.exists():
        return set()
    try:
        out = subprocess.run(
            [lsof, "-t", "--", str(path)], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(token) for token in out.split() if token.isdigit()}


def lock_holders(path: Path) -> set[int]:
    """Processes holding agy's presence lock ``path``; the lock is never taken."""
    if sys.platform.startswith("linux"):
        return _proc_locks_holders(path)
    if sys.platform == "darwin":
        return _lsof_holders(path)
    return set()


# ---------------------------------------------------------------------------
# helper processes (listing, history)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _acp_helper(supervisor: ProcessSupervisor, executable: PinnedExecutable, cwd: str,
                      updates: list[dict[str, Any]] | None = None
                      ) -> AsyncIterator[AcpConnection]:
    """A short-lived ``agy acp`` for a token-free call (list, load); always stopped.

    It runs as one of the supervisor's helpers: counted, in its own process
    group, with a pid file, and stopped at daemon shutdown if still running.
    """
    argv = proc.build_argv(executable, ["acp"])
    process = await supervisor.spawn_helper(
        argv, cwd=cwd, env=proc.scrubbed_env(), cli="agy", stdin=True, stderr=True
    )

    async def collect(update: dict[str, Any]) -> None:
        if updates is not None:
            updates.append(update)

    conn = AcpConnection(process, on_update=collect)
    conn.start()
    try:
        await conn.initialize()
        yield conn
    finally:
        await conn.close(grace=1.0)


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------


class AgyAdapter(ExternalAdapter):
    """Antigravity CLI over ACP."""

    id = "agy"
    title = "Antigravity (agy)"
    binary = "agy"
    capabilities = CliCapabilities(
        streaming=True,
        approvals="bridged",
        live_view=True,
        tmux=False,
        modes=("default", "unrestricted"),
    )
    forbidden_args = ("--dangerously-skip-permissions",)
    builtin_default_mode = "default"

    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self._executable: PinnedExecutable | None = None
        self._models: tuple[float, str, list[str]] | None = None

    # -- paths ----------------------------------------------------------------
    @property
    def summaries_db(self) -> Path:
        return self.data_dir / "conversation_summaries.db"

    def presence_lock(self, session_id: str) -> Path:
        return self.data_dir / "presence" / f"{session_id}.lock"

    def holders(self, session_id: str, own_pids: set[int] | None = None) -> set[int]:
        """Other processes holding the conversation open (an interactive agy)."""
        proc.validate_session_id(session_id)
        return lock_holders(self.presence_lock(session_id)) - set(own_pids or ())

    # -- discovery ------------------------------------------------------------
    async def discover(self, path_override: str | None = None) -> Discovery:
        found = await super().discover(path_override)
        if not found.installed or found.executable is None:
            return found
        self._executable = found.executable
        problems = list(found.problems)
        problem = version_problem(found.executable.version)
        if problem:
            problems.append(problem)
        permission = read_tool_permission(self.data_dir)
        policy = policy_of(permission)
        base = {f.name: getattr(found, f.name) for f in dataclasses.fields(Discovery)}
        base.update(
            problems=problems,
            approval_policy=policy,
            capabilities=dataclasses.replace(
                self.capabilities, approvals="bridged" if policy == "asks" else "degraded"
            ),
        )
        return AgyDiscovery(
            **base, tool_permission=permission, signed_in=_sign_in_hint(self.data_dir)
        )

    def _policy(self, discovery: Discovery | None) -> str:
        if discovery is not None and discovery.approval_policy in ("asks", "auto"):
            return discovery.approval_policy
        return policy_of(read_tool_permission(self.data_dir))

    def controlled(self, mode: str, discovery: Discovery | None) -> bool:
        # With toolPermission always-proceed agy runs every tool unasked.
        return mode != "unrestricted" and self._policy(discovery) == "asks"

    def approvals(self, mode: str, discovery: Discovery | None) -> Approvals:
        if mode == "unrestricted" or self._policy(discovery) == "auto":
            return "none"
        return "bridged"

    async def _pinned(self) -> PinnedExecutable:
        if self._executable is None:
            found = await self.discover()
            if found.executable is None:
                raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "agy is not installed")
        assert self._executable is not None
        return self._executable

    # -- models ---------------------------------------------------------------
    async def list_models(self, discovery: Discovery | None = None) -> list[str]:
        """Model ids ``agy models`` offers (no tokens); empty when it cannot say."""
        executable = discovery.executable if discovery is not None else None
        executable = executable or self._executable
        if executable is None:
            return []
        cached = self._models
        if cached is not None and cached[1] == executable.path and \
                time.monotonic() - cached[0] < MODELS_TTL_SEC:
            return list(cached[2])
        code, out = await self.helper_supervisor().run_helper(
            proc.build_argv(executable, ["models"]),
            cwd=tempfile.gettempdir(),
            env=proc.scrubbed_env(),
            cli="agy",
            timeout=MODELS_TIMEOUT_SEC,
        )
        if code != 0:
            return []
        models: list[str] = []
        for line in out.decode("utf-8", "replace").splitlines():
            if "\t" not in line:
                continue  # "Fetching available models..." and other chatter
            model = line.split("\t", 1)[0].strip()
            if proc.MODEL_RE.fullmatch(model) and not proc.is_dangerous(model) \
                    and model not in models:
                models.append(model)
        self._models = (time.monotonic(), executable.path, models)
        return list(models)

    # -- stored sessions ------------------------------------------------------
    async def _all_sessions(self, cwd_hint: str | None, session_id: str | None = None
                            ) -> list[SessionInfo]:
        try:
            return await asyncio.to_thread(read_summaries, self.summaries_db, session_id)
        except _Unusable as exc:
            if self.summaries_db.exists():
                log.info("agy summaries database not used: %s", exc)
        return await self._acp_sessions(cwd_hint)

    async def _acp_sessions(self, cwd_hint: str | None) -> list[SessionInfo]:
        """ACP ``session/list`` (capped at 500 rows by agy 1.5.x, no cwd filter)."""
        executable = await self._pinned()
        cwd = cwd_hint if cwd_hint and os.path.isdir(cwd_hint) else tempfile.gettempdir()
        rows: list[dict[str, Any]] = []
        async with _acp_helper(self.helper_supervisor(), executable, cwd) as conn:
            params: dict[str, Any] = {}
            for _page in range(MAX_LIST_PAGES):
                try:
                    result = await conn.request("session/list", params, timeout=CALL_TIMEOUT_SEC)
                except (AcpError, AcpGone) as exc:
                    raise ExternalError(
                        errors.EXTERNAL_UNAVAILABLE, f"agy could not list sessions: {exc}"
                    ) from exc
                page = result.get("sessions")
                rows += [row for row in page or [] if isinstance(row, dict)]
                cursor = result.get("nextCursor")
                if not isinstance(cursor, str) or not cursor:
                    break
                params = {"cursor": cursor}
        out: list[SessionInfo] = []
        for row in rows:
            session_id, cwd_value = row.get("sessionId"), row.get("cwd")
            if not isinstance(session_id, str) or not proc.SESSION_ID_RE.fullmatch(session_id):
                continue
            if not isinstance(cwd_value, str) or not cwd_value:
                continue
            title = row.get("title") if isinstance(row.get("title"), str) else ""
            out.append(SessionInfo(
                id=session_id,
                cwd=os.path.normpath(cwd_value),
                title=title or None,
                updated_at=iso_from_text(row.get("updatedAt")) if row.get("updatedAt") else None,
                preview=title or "",
                extra={"source": "acp", "workspaces": [os.path.normpath(cwd_value)]},
            ))
        return out

    async def list_sessions(
        self, roots: Sequence[str], *, limit: int, before: str | None = None
    ) -> list[SessionInfo]:
        scope = [os.path.normpath(str(root)) for root in roots]
        rows: list[SessionInfo] = []
        for info in await self._all_sessions(scope[0] if scope else None):
            if before is not None and (info.updated_at or "") > before:
                continue
            folders = info.extra.get("workspaces") or [info.cwd]
            inside = next((folder for folder in folders
                           if proc.project_of(folder, scope) is not None), None)
            if inside is None:
                continue
            info.cwd = inside
            rows.append(info)
        rows.sort(key=lambda info: info.updated_at or "", reverse=True)
        return rows[: max(0, limit)]

    async def get_session(self, session_id: str) -> SessionInfo | None:
        proc.validate_session_id(session_id)
        for info in await self._all_sessions(None, session_id):
            if info.id == session_id:
                return info
        return None

    async def read(self, info: SessionInfo) -> list[TimelineItem]:
        if self.holders(info.id):
            # Open in an interactive agy: do not load it behind the person's back.
            return await self._indexed_or_note(info)
        executable = await self._pinned()
        updates: list[dict[str, Any]] = []
        cwd = info.cwd if os.path.isdir(info.cwd) else tempfile.gettempdir()
        async with _acp_helper(self.helper_supervisor(), executable, cwd, updates) as conn:
            try:
                await conn.request(
                    "session/load",
                    {"sessionId": info.id, "cwd": info.cwd, "mcpServers": []},
                    timeout=SESSION_TIMEOUT_SEC,
                )
            except AcpError as exc:
                raise _session_error(exc, "load the conversation") from exc
            except AcpGone as exc:
                raise ExternalError(
                    errors.EXTERNAL_UNAVAILABLE, "agy exited while loading the conversation"
                ) from exc
            # The replay arrived before the result; nothing after it is history.
            replay = list(updates)
        return history_items(info.id, replay)

    async def _indexed_or_note(self, info: SessionInfo) -> list[TimelineItem]:
        steps = await asyncio.to_thread(read_indexed, self.summaries_db, info.id)
        if steps:
            items = indexed_items(info.id, steps)
            if items:
                return items
        builder = TimelineBuilder("agy", info.id)
        builder.note(
            "This conversation is open in agy in another terminal. Its history shows here "
            "once that terminal lets go of it.",
            key=stable_key("agy", info.id, "held-note"),
        )
        return builder.drain()

    async def follow(
        self, info: SessionInfo, *, interval: float = 1.0
    ) -> AsyncIterator[list[TimelineItem]]:
        """Live view while an interactive agy holds it: the search index, never a load."""
        while True:
            await asyncio.sleep(interval)
            steps = await asyncio.to_thread(read_indexed, self.summaries_db, info.id)
            yield indexed_items(info.id, steps) if steps else []

    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        if self.holders(info.id, own_pids):
            return Liveness(host="terminal")
        return Liveness()

    # -- running --------------------------------------------------------------
    def channel(self, spec: LaunchSpec, host: ChannelHost) -> ExternalChannel:
        return AgyChannel(self, spec, host)


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------

_RISK_BY_KIND = {
    "execute": "high",
    "delete": "high",
    "edit": "medium",
    "move": "medium",
    "fetch": "medium",
    "read": "low",
    "search": "low",
    "think": "low",
}


class AgyChannel(ExternalChannel):
    """One ``agy acp`` process running one conversation."""

    #: Seconds a turn may take to stop after ``session/cancel`` before the
    #: process is killed.
    cancel_grace: float = 5.0

    def __init__(self, adapter: AgyAdapter, spec: LaunchSpec, host: ChannelHost) -> None:
        super().__init__(spec, host)
        self.adapter = adapter
        self.conn: AcpConnection | None = None
        self.started_unrestricted: bool | None = None
        self.closing = False
        self.loading = False
        self.turn_id: str | None = None
        self.prompt_task: asyncio.Task[None] | None = None
        self.watchdog: asyncio.Task[None] | None = None
        self.cancelling = False
        self.segment = 0
        self.denied: dict[str, str] = {}
        self.denied_last = False
        self.permission_tasks: dict[str, asyncio.Task[None]] = {}
        self.answered: set[str] = set()
        self.models: list[str] = []
        self.current_model: str | None = None

    # -- state ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.conn is not None and self.conn.process.alive and not self.conn.gone

    def pids(self) -> set[int]:
        if self.conn is not None and self.conn.process.alive:
            return {self.conn.process.pid}
        return set()

    # -- start ------------------------------------------------------------------
    async def start(self) -> None:
        if self.mode not in self.adapter.capabilities.modes:
            raise ExternalError(errors.INVALID_PARAMS, f"agy does not offer mode {self.mode!r}")
        await self._spawn(resume=bool(self.spec.resume and self.session_id))
        if self.spec.model:
            await self._set_model(self.spec.model)

    async def _spawn(self, *, resume: bool) -> None:
        if resume:
            assert self.session_id is not None
            if self.adapter.holders(self.session_id):
                raise ExternalError(
                    errors.EXTERNAL_VIEW_ONLY,
                    "this agy conversation is open in another terminal; it can be continued "
                    "here once that one exits",
                )
        bypass = None
        if self.mode == "unrestricted":
            if self.grant is None:
                raise ExternalError(
                    errors.JARVIS_PERSON_ONLY, "unrestricted needs a person's grant"
                )
            bypass = proc.bypass_args(self.grant, ["--dangerously-skip-permissions"])
        argv = proc.build_argv(
            self.spec.executable, ["acp"], bypass=bypass, forbidden=self.adapter.forbidden_args
        )
        process = await self.host_api.supervisor.spawn(
            argv, cwd=self.spec.cwd, env=self.host_api.env, cli="agy", ref=self.session_id
        )
        conn = AcpConnection(process, on_update=self._on_update, on_request=self._on_request)

        async def exited(code: int | None) -> None:
            await self._on_exit(conn, code)

        conn.on_exit = exited
        self.conn = conn
        self.closing = False
        self.started_unrestricted = self.mode == "unrestricted"
        conn.start()
        try:
            await conn.initialize()
            if resume:
                self.loading = True
                try:
                    result = await conn.request(
                        "session/load",
                        {"sessionId": self.session_id, "cwd": self.spec.cwd, "mcpServers": []},
                        timeout=SESSION_TIMEOUT_SEC,
                    )
                finally:
                    self.loading = False
            else:
                result = await conn.request(
                    "session/new", {"cwd": self.spec.cwd, "mcpServers": []},
                    timeout=SESSION_TIMEOUT_SEC,
                )
                session_id = result.get("sessionId")
                if not isinstance(session_id, str):
                    raise ExternalError(errors.INTERNAL, "agy did not report a session id")
                proc.validate_session_id(session_id)
                self.session_id = session_id
                await self.host_api.id_known(session_id)
        except AcpError as exc:
            await self._stop()
            raise _session_error(
                exc, "load the conversation" if resume else "start a conversation"
            ) from exc
        except AcpGone as exc:
            tail = process.stderr_tail(3).strip()
            await self._stop()
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                "agy exited while starting" + (f": {tail[-300:]}" if tail else ""),
            ) from exc
        except BaseException:
            await self._stop()
            raise
        models = result.get("models")
        if isinstance(models, dict):
            self.models = [str(m.get("modelId")) for m in models.get("availableModels") or []
                           if isinstance(m, dict) and m.get("modelId")]
            current = models.get("currentModelId")
            self.current_model = current if isinstance(current, str) else None

    async def _set_model(self, model: str) -> None:
        proc.validate_model(model)
        if self.models and model not in self.models:
            raise ExternalError(
                errors.INVALID_PARAMS,
                f"agy does not offer model {model!r}; it offers {', '.join(self.models)}",
            )
        assert self.conn is not None and self.session_id is not None
        params = {"sessionId": self.session_id, "modelId": model}
        proc.scan_payload(params)
        try:
            await self.conn.request("session/set_model", params, timeout=CALL_TIMEOUT_SEC)
        except AcpError as exc:
            if exc.code != -32601:
                raise ExternalError(
                    errors.INVALID_PARAMS, f"agy refused model {model!r}: {exc.message}"
                ) from exc
            # Older releases only have the generic config option.
            try:
                await self.conn.request(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": "model", "value": model},
                    timeout=CALL_TIMEOUT_SEC,
                )
            except AcpError as inner:
                raise ExternalError(
                    errors.INVALID_PARAMS, f"agy cannot switch models here: {inner.message}"
                ) from inner
        except AcpGone as exc:
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "agy exited") from exc
        self.current_model = model

    # -- frames -------------------------------------------------------------------
    async def _on_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "session_info_update":
            title = update.get("title")
            if isinstance(title, str) and title.strip():
                await self.host_api.title_known(title.strip())
            return
        if self.loading:
            return  # the replay of a load: the manager already read the history
        builder = self.host_api.builder
        turn = self.turn_id
        if kind == "agent_message_chunk":
            text = _text_of(update)
            if text:
                self.denied_last = False
                builder.delta(text, message_key=f"{turn or 'idle'}:m{self.segment}",
                              turn_id=turn)
        elif kind == "agent_thought_chunk":
            text = _text_of(update)
            if text:
                builder.flush(turn_id=turn)
                self.segment += 1
                builder.reasoning(text, turn_id=turn)
        elif kind in ("tool_call", "tool_call_update"):
            self.segment += 1
            apply_tool_update(builder, update, denied=self.denied, turn_id=turn)
        else:
            # user_message_chunk is the echo of our own prompt (already published);
            # commands, usage, config, mode and unknown kinds are not conversation.
            return
        await self.host_api.emit(builder.drain())

    async def _on_request(self, frame: dict[str, Any]) -> None:
        conn = self.conn
        if conn is None:
            return
        if frame.get("method") != "session/request_permission":
            await conn.reply_error(frame.get("id"), -32601, f"Method not found: {frame['method']}")
            return
        rid = frame.get("id")
        key = json.dumps(rid)
        task = asyncio.ensure_future(self._permission(conn, rid, frame.get("params") or {}))
        self.permission_tasks[key] = task

        def forget(_task: asyncio.Task[None]) -> None:
            self.permission_tasks.pop(key, None)

        task.add_done_callback(forget)

    async def _permission(self, conn: AcpConnection, rid: Any, params: dict[str, Any]) -> None:
        key = json.dumps(rid)
        raw_call = params.get("toolCall")
        call: dict[str, Any] = raw_call if isinstance(raw_call, dict) else {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        args = _tool_args(call)
        raw_kind = call.get("kind")
        kind = raw_kind if isinstance(raw_kind, str) else ""
        command = args.get("CommandLine")
        if kind == "execute" and isinstance(command, str) and "command" not in args:
            args["command"] = command
        diffs = _diffs(call)
        paths = [loc["path"] for loc in call.get("locations") or []
                 if isinstance(loc, dict) and isinstance(loc.get("path"), str)]
        paths += [path for path, _patch in diffs if path not in paths]
        raw_title = call.get("title")
        title = raw_title if isinstance(raw_title, str) else ""
        ask = ApprovalAsk(
            tool=_tool_name(call),
            args=args,
            risk=_RISK_BY_KIND.get(kind, "medium"),
            note=title,
            diff="\n".join(patch for _path, patch in diffs) or None,
            paths=paths or None,
        )
        try:
            answer = await self.host_api.ask_approval(ask)
        except asyncio.CancelledError:
            if key not in self.answered:
                self.answered.add(key)
                await conn.reply(rid, {"outcome": {"outcome": "cancelled"}})
            raise
        if key in self.answered:
            return
        self.answered.add(key)
        # Never an "always" option: that would write the owner's agy rules;
        # snowpea keeps wider answers itself (the manager's per-session cache).
        wanted = "allow_once" if answer.allow else "reject_once"
        option = next((o for o in options if o.get("kind") == wanted), None)
        if option is None:
            await conn.reply(rid, {"outcome": {"outcome": "cancelled"}})
            return
        if not answer.allow:
            call_id = call.get("toolCallId")
            if isinstance(call_id, str):
                self.denied[call_id] = answer.reason or (
                    f"by {answer.by}" if answer.by else "by the person")
            self.denied_last = True
        await conn.reply(rid, {"outcome": {"outcome": "selected",
                                           "optionId": option.get("optionId")}})

    async def _on_exit(self, conn: AcpConnection, code: int | None) -> None:
        if conn is not self.conn or self.closing:
            return
        self._drop_turn()
        await self.host_api.emit(self.host_api.builder.drain())
        await self.host_api.channel_died(
            f"agy exited with status {code}" if code is not None else "agy stopped"
        )

    # -- turns --------------------------------------------------------------------
    async def send(self, text: str, turn_id: str) -> None:
        if self.started_unrestricted is not None and \
                self.started_unrestricted != (self.mode == "unrestricted"):
            # The bypass flag is a process flag: restart, continuing the conversation.
            await self._stop()
            await self._spawn(resume=True)
        conn = self.conn
        if conn is None or self.session_id is None or not self.alive:
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "agy is not running")
        self.turn_id = turn_id
        self.cancelling = False
        self.denied.clear()
        self.denied_last = False
        self.segment = 0
        try:
            future = await conn.start_request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
            )
        except AcpGone as exc:
            self.turn_id = None
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "agy stopped reading") from exc
        self.prompt_task = asyncio.ensure_future(self._run_turn(future, turn_id))

    async def _run_turn(self, future: asyncio.Future[dict[str, Any]], turn_id: str) -> None:
        builder = self.host_api.builder
        try:
            result = await future
        except AcpGone:
            return  # _on_exit reports it
        except AcpError as exc:
            if self.turn_id != turn_id:
                return
            self._drop_turn()
            builder.flush(turn_id=turn_id)
            await self.host_api.emit(builder.drain())
            await self.host_api.turn_finished("error", f"agy: {exc.message or 'the turn failed'}")
            return
        if self.turn_id != turn_id:
            return
        stop = result.get("stopReason")
        builder.flush(turn_id=turn_id)
        reason = "complete"
        if stop == "cancelled" or self.cancelling:
            reason = "interrupted"
        elif self.denied and self.denied_last:
            # A deny ends agy's turn at once with no text: a clean end, said plainly.
            builder.note("Stopped: a tool call was denied.", turn_id=turn_id)
            reason = "denied"
        elif stop in ("max_tokens", "max_turn_requests"):
            builder.note(f"agy stopped early ({stop}).", turn_id=turn_id)
        elif stop == "refusal":
            builder.note("agy declined to continue this request.", turn_id=turn_id)
        self._drop_turn()
        await self.host_api.emit(builder.drain())
        await self.host_api.turn_finished(reason)

    def _drop_turn(self) -> None:
        self.turn_id = None
        self.cancelling = False
        if self.watchdog is not None and self.watchdog is not asyncio.current_task():
            self.watchdog.cancel()
        self.watchdog = None

    async def interrupt(self) -> None:
        conn = self.conn
        if conn is None or self.turn_id is None or self.session_id is None:
            return
        self.cancelling = True
        await conn.notify("session/cancel", {"sessionId": self.session_id})
        for key, task in list(self.permission_tasks.items()):
            task.cancel()
            self.permission_tasks.pop(key, None)
        if self.watchdog is None:
            self.watchdog = asyncio.ensure_future(self._cancel_watchdog(self.turn_id))

    async def _cancel_watchdog(self, turn_id: str) -> None:
        """agy answers a cancel within milliseconds; a process that does not is stopped."""
        await asyncio.sleep(self.cancel_grace)
        if self.turn_id != turn_id:
            return
        self.watchdog = None
        self._drop_turn()
        builder = self.host_api.builder
        builder.flush(turn_id=turn_id)
        await self.host_api.emit(builder.drain())
        await self.host_api.turn_finished("interrupted")
        await self._stop()
        await self.host_api.channel_died("agy did not stop when asked; snowpea stopped it")

    # -- close ----------------------------------------------------------------------
    async def _stop(self) -> None:
        self.closing = True
        for task in list(self.permission_tasks.values()):
            task.cancel()
        self.permission_tasks.clear()
        conn = self.conn
        if conn is not None:
            await conn.close(grace=1.0)

    async def close(self) -> None:
        current = asyncio.current_task()
        for task in (self.prompt_task, self.watchdog):
            if task is not None and task is not current and not task.done():
                task.cancel()
        self.turn_id = None
        await self._stop()


__all__ = [
    "AcpConnection",
    "AcpError",
    "AgyAdapter",
    "AgyChannel",
    "AgyDiscovery",
    "apply_tool_update",
    "default_data_dir",
    "history_items",
    "indexed_items",
    "lock_holders",
    "parse_version",
    "policy_of",
    "read_indexed",
    "read_summaries",
    "read_tool_permission",
    "version_problem",
]
