"""Claude Code adapter (M19 WP6a).

Ground truth: ``docs/design/m19-spikes/claude.md`` (S1) and ``tmux.md`` (S5),
recorded against ``claude`` 2.1.325.

* **Discovery**: the binary, ``--version``, and ``claude auth status --json``
  for the ``loggedIn`` flag only (no credential file is ever read; the rest of
  that answer — email, organisation — is dropped unread).
* **Stored sessions**: ``$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/<id>.jsonl``
  (``~/.claude`` without the variable), listed by working directory, newest
  first, from bounded head/tail reads of each transcript.
* **History**: only ``user`` and ``assistant`` records are converted
  (:data:`~snowpea_core.external.timeline.USER_ASSISTANT_ONLY`); ``attachment``
  and ``session-owner`` records carry the owner's email and organisation ids
  and are dropped before they are parsed any further.
* **Liveness**: ``$CLAUDE_CONFIG_DIR/sessions/<pid>.json`` with the pid alive
  (and its start time matching ``procStart`` on Linux); a pid inside a tmux
  pane is reported as host ``tmux``.
* **Structured channel** (host ``snowpea``): one long-lived
  ``claude -p --input-format stream-json --output-format stream-json`` per
  open session, permission prompts bridged through ``control_request
  can_use_tool``.  A session that is open elsewhere is never resumed (a
  second ``--resume`` silently forks the history).
* **tmux channel** (host ``tmux``, Linux/macOS): see :mod:`.claude_tmux`.
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
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.external import modes as native_modes
from snowpea_core.external import proc
from snowpea_core.external.base import (
    ALL_MODES,
    ApprovalAsk,
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
from snowpea_core.external.modes import NativeMode
from snowpea_core.external.proc import ExternalError, ExternalProcess, UnrestrictedGrant
from snowpea_core.external.timeline import (
    USER_ASSISTANT_ONLY,
    RecordFilter,
    TimelineBuilder,
    TimelineItem,
    iso_from_text,
    iso_from_unix,
    stable_key,
)
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.claude")

#: Oldest Claude Code this adapter drives (stream-json both ways and host
#: permission prompts); the newest major it knows.
MIN_VERSION: tuple[int, int, int] = (2, 1, 0)
MAX_MAJOR = 2
#: Model aliases the CLI's ``--model`` accepts (a full model id works too).
MODEL_ALIASES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

#: Claude Code's permission modes (1.56.0), checked against ``claude --help``
#: of 2.1.326: ``--permission-mode`` choices ``acceptEdits, auto,
#: bypassPermissions, manual, dontAsk, plan`` (``default`` is still accepted
#: as the old name of ``manual``).  ``launch``: ``permissionMode`` is the
#: value passed (``--permission-mode=<value>``), ``bypass`` the flags only a
#: person's grant releases.  A row with ``requires`` is offered only when the
#: installed version lists that choice.
NATIVE_MODES: tuple[NativeMode, ...] = (
    NativeMode("default", "Default", "always", "workspace", "bridged", False, "default",
               launch={"permissionMode": "default"}, terminal={"cycle": True}),
    NativeMode("acceptEdits", "Accept edits", "sometimes", "workspace", "bridged", True,
               "default", launch={"permissionMode": "acceptEdits", "granted": True},
               requires="acceptEdits", terminal={"cycle": True}),
    NativeMode("plan", "Plan", "always", "plan", "bridged", False, "readonly",
               launch={"permissionMode": "plan"}, terminal={"slash": "/plan"}),
    NativeMode("auto", "Auto", "sometimes", "workspace", "bridged", True, "default",
               launch={"permissionMode": "auto", "granted": True}, requires="auto",
               terminal={"cycle": True}),
    NativeMode("dontAsk", "Don't ask", "never", "workspace", "none", True, "default",
               launch={"permissionMode": "dontAsk", "granted": True}, requires="dontAsk"),
    NativeMode("bypassPermissions", "Bypass permissions", "never", "anywhere", "none", True,
               "unrestricted", launch={"bypass": ("--dangerously-skip-permissions",)}),
)
LEGACY_NATIVE: dict[str, str] = {
    "default": "default", "readonly": "plan", "unrestricted": "bypassPermissions",
}


def claude_native_modes(help_text: str | None) -> tuple[NativeMode, ...]:
    """The rows ``claude --help`` offers; the default row is labelled as that
    version names it (``Manual`` once ``manual`` is the listed choice)."""
    choices = native_modes.help_choices(help_text or "", "--permission-mode")
    rows = native_modes.offered(NATIVE_MODES, choices)
    if choices and "manual" in choices and "default" not in choices:
        rows = tuple(
            dataclasses.replace(row, label="Manual", launch={"permissionMode": "manual"})
            if row.id == "default" else row
            for row in rows
        )
    return rows

#: Claude's own session ids are uuids; transcripts with other stems (old
#: ``agent-*.jsonl`` sidechains) are not sessions.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: ``tmux`` field of a status file: ``<session>:@<window id>.%<pane id>``.
_TMUX_FIELD_RE = re.compile(r"^(?P<session>.+):@(?P<window>\d+)\.(?P<pane>%\d+)$")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_AUTH_HINTS = (
    "invalid api key",
    "please run /login",
    "not logged in",
    "authentication_error",
    "oauth token has expired",
    "run `claude auth login`",
)

#: Transcript bytes read for history; anything older is summarised by a note.
MAX_READ_BYTES = 48 * 1024 * 1024
#: A single transcript line longer than this is skipped (a giant tool result).
MAX_LINE_BYTES = 8 * 1024 * 1024
#: Bytes scanned at the head / tail of a transcript for the session list.
HEAD_BYTES = 2 * 1024 * 1024
TAIL_BYTES = 256 * 1024
#: Status files larger than this are not Claude's.
MAX_STATUS_BYTES = 64 * 1024
#: Rows kept in the metadata cache.
META_CACHE_SIZE = 2048

#: Record kinds the session list may look at (titles, prompts, model, cwd).
#: Everything else — ``attachment``, ``session-owner``, ``api-request*`` — is
#: dropped unparsed.
META_FILTER = RecordFilter(
    allow=frozenset(
        {"user", "assistant", "summary", "custom-title", "ai-title", "agent-name", "last-prompt"}
    )
)

_RISK = {
    "Bash": "high",
    "PowerShell": "high",
    "KillShell": "medium",
    "Write": "medium",
    "Edit": "medium",
    "MultiEdit": "medium",
    "NotebookEdit": "medium",
    "Read": "low",
    "Glob": "low",
    "Grep": "low",
    "WebFetch": "low",
    "WebSearch": "low",
}


# ---------------------------------------------------------------------------
# paths and small helpers
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """``$CLAUDE_CONFIG_DIR`` or ``~/.claude`` (read at call time)."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".claude"


def encode_cwd(cwd: str) -> str:
    """Claude's project folder name: every char outside ``[A-Za-z0-9]`` -> ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_path(cwd: str, session_id: str) -> Path:
    return config_dir() / "projects" / encode_cwd(cwd) / f"{session_id}.jsonl"


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    if not text:
        return None
    found = _VERSION_RE.search(text)
    if not found:
        return None
    return int(found.group(1)), int(found.group(2)), int(found.group(3))


def scrub(text: str) -> str:
    """``text`` with anything that looks like an email address replaced."""
    return _EMAIL_RE.sub("<email>", text)


def _under(path: str, roots: Sequence[str]) -> bool:
    for root in roots:
        base = root.rstrip("/") or "/"
        if path == base or path.startswith(base.rstrip("/") + "/"):
            return True
    return False


def _looks_like_auth_error(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


# ---------------------------------------------------------------------------
# bounded line reading
# ---------------------------------------------------------------------------


def iter_lines(
    path: Path, start: int = 0, *, limit: int | None = None
) -> Iterator[tuple[int, bytes]]:
    """``(offset after the line, line)`` for complete lines from ``start``.

    Reads in chunks, never holds more than :data:`MAX_LINE_BYTES` of one line,
    and skips longer lines whole.  A trailing line without ``\\n`` (still being
    written) is not yielded.  ``limit`` stops after that many bytes.
    """
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed in finally
    except OSError:
        return
    try:
        handle.seek(start)
        offset = start
        buffer = bytearray()
        skipping = False
        read = 0
        while True:
            if limit is not None and read >= limit:
                return
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return
            read += len(chunk)
            position = 0
            while True:
                index = chunk.find(b"\n", position)
                if index < 0:
                    rest = chunk[position:]
                    if not skipping:
                        buffer.extend(rest)
                        if len(buffer) > MAX_LINE_BYTES:
                            buffer.clear()
                            skipping = True
                    offset += len(rest)
                    break
                piece = chunk[position:index]
                offset += len(piece) + 1
                position = index + 1
                if skipping:
                    skipping = False
                    continue
                if len(buffer) + len(piece) > MAX_LINE_BYTES:
                    buffer.clear()
                    continue
                buffer.extend(piece)
                line = bytes(buffer)
                buffer.clear()
                yield offset, line
    finally:
        handle.close()


#: Longest transcript line parsed; a longer one is skipped (security review L1).
MAX_RECORD_BYTES = 32 * 1024 * 1024


def parse_record(line: bytes) -> dict[str, Any] | None:
    if not line.strip() or len(line) > MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _kind(record: dict[str, Any]) -> Any:
    return record.get("type")


def iter_records(
    path: Path, start: int = 0, *, record_filter: RecordFilter = USER_ASSISTANT_ONLY,
    limit: int | None = None,
) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """``(offset after the line, record)`` for every complete line.

    ``record`` is ``None`` for a line that is not JSON or whose kind
    ``record_filter`` drops: a dropped kind goes no further than this function.
    """
    for offset, line in iter_lines(path, start, limit=limit):
        record = parse_record(line)
        # Privacy first: a dropped kind goes no further than this line.
        if record is not None and not record_filter.keep(_kind(record)):
            record = None
        yield offset, record


def read_start(size: int) -> int:
    """Where a bounded history read starts in a file of ``size`` bytes."""
    return max(0, size - MAX_READ_BYTES)


# ---------------------------------------------------------------------------
# transcript -> timeline
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<(?P<tag>[a-z-]+)>(?P<body>.*?)</(?P=tag)>", re.DOTALL)


def _command_text(text: str) -> tuple[str, str] | None:
    """A slash command or local output stored as tagged text: (kind, text)."""
    if not text.startswith("<"):
        return None
    tags = {match.group("tag"): match.group("body").strip() for match in _TAG_RE.finditer(text)}
    if "command-name" in tags:
        name = tags["command-name"]
        args = tags.get("command-args", "")
        return "user", f"{name} {args}".strip()
    if "bash-input" in tags:
        return "user", f"!{tags['bash-input']}"
    for tag in ("local-command-stdout", "bash-stdout", "bash-stderr", "local-command-stderr"):
        if tag in tags:
            return ("note", tags[tag]) if tags[tag] else ("skip", "")
    if "local-command-caveat" in tags or "system-reminder" in tags:
        return "skip", ""
    return None


def _block_text(content: Any) -> str:
    """Text of a tool_result / message content (string or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "image":
                parts.append("[image]")
            elif block.get("type") == "document":
                parts.append("[document]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _display_path(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    return path


def patch_from_result(result: Any, cwd: str | None) -> tuple[str, str] | None:
    """``(path, unified diff)`` from a Write/Edit ``toolUseResult``, if it has one."""
    if not isinstance(result, dict):
        return None
    path = result.get("filePath")
    if not isinstance(path, str) or not path:
        return None
    shown = _display_path(path, cwd)
    hunks = result.get("structuredPatch")
    if isinstance(hunks, list) and hunks:
        lines = [f"--- a/{shown}", f"+++ b/{shown}"]
        for hunk in hunks:
            if not isinstance(hunk, dict):
                continue
            lines.append(
                f"@@ -{hunk.get('oldStart', 0)},{hunk.get('oldLines', 0)} "
                f"+{hunk.get('newStart', 0)},{hunk.get('newLines', 0)} @@"
            )
            lines.extend(str(line) for line in hunk.get("lines") or [])
        return path, "\n".join(lines) + "\n"
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        body = result["content"].splitlines()
        lines = ["--- /dev/null", f"+++ b/{shown}", f"@@ -0,0 +1,{len(body)} @@"]
        lines.extend("+" + line for line in body)
        return path, "\n".join(lines) + "\n"
    return None


def _previewable(path: str, cwd: str | None) -> Path | None:
    """``path`` resolved when its old contents may be shown, else ``None``.

    Only a file inside the session's real folder that is neither a secret
    (``classify.is_secret_path``) nor snowpea configuration (``config_guard``).
    """
    if not cwd:
        return None
    try:
        base = Path(cwd).resolve(strict=True)
        raw = Path(path).expanduser()
        target = (raw if raw.is_absolute() else base / raw).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if target != base and base not in target.parents:
        return None
    try:
        from snowpea_core.jarvis.classify import is_secret_path
        from snowpea_core.tools.config_guard import is_config_path

        if is_secret_path(target) or is_config_path(str(target), workdir=str(base)):
            return None
    except Exception:  # noqa: BLE001 - unknown is protected
        return None
    return target


def preview_diff(tool: str, args: dict[str, Any], cwd: str | None) -> str | None:
    """A diff for an approval card (Write / Edit / MultiEdit input)."""
    path = args.get("file_path")
    if not isinstance(path, str):
        return None
    shown = _display_path(path, cwd)
    if tool == "Write" and isinstance(args.get("content"), str):
        old_text = ""
        target = _previewable(path, cwd)
        if target is not None:
            with contextlib.suppress(OSError, UnicodeDecodeError):
                if target.is_file() and target.stat().st_size <= 1024 * 1024:
                    old_text = target.read_text(encoding="utf-8")
        from snowpea_core.util.udiff import unified

        # On "\n" only, with the no-newline marker (browser peer, 2026-11-16).
        body = unified(old_text, args["content"], f"a/{shown}", f"b/{shown}").rstrip("\n")
        if target is None:
            # The file is outside the session's folder or a secret / config
            # file: its current contents are never read into the approval card
            # every surface sees (security review M1).
            return (
                f"# {shown}: the current contents are not shown (outside the project or a "
                f"protected file); the new contents follow\n{body}"
            )
        return body
    edits: list[dict[str, Any]] = []
    if tool == "Edit":
        edits = [args]
    elif tool == "MultiEdit" and isinstance(args.get("edits"), list):
        edits = [edit for edit in args["edits"] if isinstance(edit, dict)]
    chunks: list[str] = []
    for edit in edits:
        old_text, new_text = edit.get("old_string"), edit.get("new_string")
        if isinstance(old_text, str) and isinstance(new_text, str):
            # Fragments, not files: split on "\n" only, no end-of-file marker.
            chunks.extend(difflib.unified_diff(old_text.split("\n"), new_text.split("\n"),
                                               f"a/{shown}", f"b/{shown}", lineterm=""))
    return "\n".join(chunks) if chunks else None


class Converter:
    """Feeds transcript records (or live stdout frames) into a :class:`TimelineBuilder`."""

    def __init__(self, builder: TimelineBuilder, cwd: str | None = None) -> None:
        self.builder = builder
        self.cwd = cwd
        #: User prompts seen (the tmux channel uses it to see a turn start).
        self.prompts = 0
        #: Calls announced without a result yet: raw id -> (name, args).
        self.open_calls: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        #: A prompt snowpea typed itself (tmux): its transcript copy is not shown
        #: again, the manager already published it.
        self.expect_prompt: str | None = None

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if record.get("isSidechain"):
            return
        if kind == "user":
            self.user_record(record)
        elif kind == "assistant":
            self.assistant_record(record)

    # -- user ---------------------------------------------------------------
    def user_record(self, record: dict[str, Any], *, live: bool = False) -> None:
        builder = self.builder
        ts = iso_from_text(record.get("timestamp"))
        uid = record.get("uuid")
        message = record.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("isMeta") or record.get("isVisibleInTranscriptOnly"):
            return
        if record.get("isCompactSummary"):
            builder.note("Earlier conversation was compacted by Claude Code.",
                         key=f"{uid}:compact", ts=ts)
            return
        if isinstance(content, str):
            if not live:
                self._user_text(content, uid, ts)
            elif content.startswith("[Request interrupted by user"):
                self.builder.note("Interrupted.", key=f"{uid}:note", ts=ts)
            return
        if not isinstance(content, list):
            return
        texts: list[str] = []
        result = record.get("toolUseResult", record.get("tool_use_result"))
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                raw_id = block.get("tool_use_id")
                error = bool(block.get("is_error"))
                text = _block_text(block.get("content"))
                name = self.open_calls.pop(raw_id, ("", {}))[0] if raw_id else ""
                builder.tool_result(
                    raw_id,
                    ok=not error,
                    output="" if error else text,
                    error=(text or "failed") if error else None,
                    name=name or None,
                    ts=ts,
                )
                patch = None if error else patch_from_result(result, self.cwd)
                if patch is not None:
                    builder.diff(patch[0], patch[1], raw_call_id=raw_id, ts=ts)
            elif block.get("type") in ("text", "image", "document"):
                texts.append(_block_text([block]))
        if texts and not live:
            self._user_text("\n".join(texts), uid, ts)

    def _user_text(self, text: str, uid: Any, ts: str) -> None:
        tagged = _command_text(text)
        if tagged is not None:
            kind, body = tagged
            if kind == "skip":
                return
            if kind == "note":
                self.builder.note(body, key=f"{uid}:note", ts=ts)
                return
            text = body
        if text.startswith("[Request interrupted by user"):
            self.builder.note("Interrupted.", key=f"{uid}:note", ts=ts)
            return
        self.prompts += 1
        if self.expect_prompt is not None and text.strip() == self.expect_prompt.strip():
            self.expect_prompt = None
            return
        self.builder.user(text, key=uid if isinstance(uid, str) else None, ts=ts)

    # -- assistant ------------------------------------------------------------
    def assistant_record(self, record: dict[str, Any]) -> None:
        ts = iso_from_text(record.get("timestamp"))
        uid = record.get("uuid")
        message = record.get("message") or {}
        for index, block in enumerate(message.get("content") or []):
            if isinstance(block, dict):
                self.assistant_block(block, uid=uid, index=index, ts=ts)

    def assistant_block(self, block: dict[str, Any], *, uid: Any, index: int = 0,
                        ts: str | None = None, text_key: str | None = None) -> None:
        builder = self.builder
        kind = block.get("type")
        suffix = f":{index}" if index else ""
        if kind == "text":
            builder.assistant(str(block.get("text", "")),
                              key=text_key or (f"{uid}{suffix}" if uid else None), ts=ts)
        elif kind in ("thinking", "redacted_thinking"):
            text = block.get("thinking") if kind == "thinking" else ""
            builder.reasoning(str(text or ""), key=f"{uid}{suffix}:think" if uid else None, ts=ts)
        elif kind == "tool_use":
            raw_id = block.get("id")
            name = str(block.get("name") or "tool")
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            builder.tool_call(raw_id, name, args, ts=ts)
            if isinstance(raw_id, str):
                self.open_calls[raw_id] = (name, args)
                while len(self.open_calls) > 500:
                    self.open_calls.popitem(last=False)

    def seed_calls(self, calls: dict[str, tuple[str, dict[str, Any]]]) -> None:
        """Calls a history read already announced, so their results pair up."""
        for raw_id, (name, args) in calls.items():
            self.builder.tool_call(raw_id, name, args)
            self.open_calls[raw_id] = (name, args)
        self.builder.drain()


def read_transcript(path: Path, session_id: str, cwd: str | None
                    ) -> tuple[list[TimelineItem], int, dict[str, tuple[str, dict[str, Any]]]]:
    """History items, the offset read up to, and calls still without a result."""
    builder = TimelineBuilder("claude", session_id)
    converter = Converter(builder, cwd)
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0, {}
    start = read_start(size)
    if start:
        builder.note("Older parts of this long conversation are not shown.",
                     key=stable_key("claude", session_id, "truncated"))
    offset = start
    # A partial first line after a bounded seek is skipped by the parser.
    for offset, record in iter_records(path, start):  # noqa: B007 - offset is used below
        if record is not None:
            converter.feed(record)
    builder.flush()
    return builder.drain(), offset, dict(converter.open_calls)


class TranscriptTail:
    """Appended records of one transcript, survives truncation and replacement."""

    def __init__(self, path: Path, session_id: str, cwd: str | None, *,
                 start: int | None = None, builder: TimelineBuilder | None = None) -> None:
        self.path = path
        self.session_id = session_id
        self.builder = builder or TimelineBuilder("claude", session_id, salt="tail")
        self.converter = Converter(self.builder, cwd)
        self.offset = start
        self._inode: int | None = None
        self.resets = 0

    def poll(self) -> list[TimelineItem]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if self.offset is None:
            self.offset = read_start(st.st_size)
            self._inode = st.st_ino
        if self._inode is None:
            self._inode = st.st_ino
        if st.st_ino != self._inode or st.st_size < self.offset:
            # Rotated, replaced or rewritten (compaction): read it again; the
            # builder's keys drop what was already delivered.
            self._inode = st.st_ino
            self.offset = read_start(st.st_size)
            self.resets += 1
        if st.st_size == self.offset:
            return []
        for offset, record in iter_records(self.path, self.offset):
            self.offset = offset
            if record is not None:
                self.converter.feed(record)
        return self.builder.drain()


# ---------------------------------------------------------------------------
# session metadata (list rows)
# ---------------------------------------------------------------------------


@dataclass
class _Meta:
    cwd: str | None
    title: str | None
    preview: str
    model: str | None


def _first_prompt(record: dict[str, Any]) -> str | None:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return None
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = _block_text(content)
    if not isinstance(content, str) or not content.strip():
        return None
    tagged = _command_text(content)
    if tagged is not None:
        return tagged[1] if tagged[0] == "user" and tagged[1] else None
    if content.startswith("[Request interrupted"):
        return None
    return content


def scan_meta(path: Path) -> _Meta:
    """cwd, title, preview and model from the head and tail of a transcript."""
    cwd: str | None = None
    first: str | None = None
    for _offset, record in iter_records(path, 0, record_filter=META_FILTER, limit=HEAD_BYTES):
        if record is None:
            continue
        if cwd is None and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        if first is None:
            first = _first_prompt(record)
        if cwd is not None and first is not None:
            break
    titles: dict[str, str] = {}
    last_prompt: str | None = None
    model: str | None = None
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    start = max(0, size - TAIL_BYTES)
    for _offset, record in iter_records(path, start, record_filter=META_FILTER):
        if record is None:
            continue
        kind = record.get("type")
        if cwd is None and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        for field_kind, field_name in (("custom-title", "customTitle"), ("ai-title", "aiTitle"),
                                       ("summary", "summary"), ("agent-name", "agentName")):
            if kind == field_kind and isinstance(record.get(field_name), str):
                titles[field_kind] = record[field_name]
        if kind == "last-prompt" and isinstance(record.get("lastPrompt"), str):
            last_prompt = record["lastPrompt"]
        prompt = _first_prompt(record)
        if prompt:
            last_prompt = prompt
        if kind == "assistant":
            value = (record.get("message") or {}).get("model")
            if isinstance(value, str) and value and not value.startswith("<"):
                model = value
    title = (titles.get("custom-title") or titles.get("ai-title") or titles.get("summary")
             or (first.strip().splitlines()[0][:80] if first and first.strip() else None)
             or titles.get("agent-name"))
    return _Meta(cwd=cwd, title=title, preview=(last_prompt or first or "")[:2000], model=model)


# ---------------------------------------------------------------------------
# liveness (status files)
# ---------------------------------------------------------------------------


def status_dir() -> Path:
    return config_dir() / "sessions"


def read_status(pid: int) -> dict[str, Any] | None:
    """``sessions/<pid>.json`` as a dict, or ``None``."""
    path = status_dir() / f"{pid}.json"
    try:
        if path.stat().st_size > MAX_STATUS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def status_time(data: dict[str, Any]) -> float:
    """Milliseconds of the status file's last status change."""
    for key in ("statusUpdatedAt", "updatedAt"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _same_process(pid: int, data: dict[str, Any]) -> bool:
    if not proc.pid_alive(pid):
        return False
    recorded = data.get("procStart")
    if recorded is None or not sys.platform.startswith("linux"):
        # Only Linux start times are known to use Claude's format; elsewhere a
        # live pid counts (refusing a resume is the safe side).
        return True
    return proc.process_start_time(pid) == str(recorded)


def live_holders(session_id: str, own_pids: set[int]) -> list[dict[str, Any]]:
    """Status records of live processes (not ``own_pids``) holding ``session_id``."""
    found: list[dict[str, Any]] = []
    folder = status_dir()
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return found
    for path in entries:
        if not path.stem.isdigit():
            continue
        pid = int(path.stem)
        if pid in own_pids:
            continue
        data = read_status(pid)
        if data is None or data.get("sessionId") != session_id:
            continue
        try:
            if int(data.get("pid", pid)) != pid:
                continue
        except (TypeError, ValueError):
            continue
        if _same_process(pid, data):
            found.append(data)
    return found


def tmux_field(value: Any) -> dict[str, str] | None:
    """``{session, pane}`` from a status file's ``tmux`` field."""
    if not isinstance(value, str):
        return None
    match = _TMUX_FIELD_RE.match(value)
    if not match:
        return None
    return {"session": match.group("session"), "pane": match.group("pane")}


def liveness_of(session_id: str, own_pids: set[int]) -> tuple[Liveness, dict[str, Any] | None]:
    holders = live_holders(session_id, own_pids)
    if not holders:
        return Liveness(), None
    # The most recently updated holder speaks for the session.
    data = max(holders, key=status_time)
    busy = data.get("status") in ("busy", "waiting")
    pane = tmux_field(data.get("tmux"))
    pid = data.get("pid")
    pids = (pid,) if isinstance(pid, int) and pid > 0 else ()
    if pane is not None:
        return Liveness(host="tmux", busy=busy, tmux=pane, pids=pids), data
    return Liveness(host="terminal", busy=busy, pids=pids), data


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------


class ClaudeAdapter(ExternalAdapter):
    """Claude Code (``claude``)."""

    id = "claude"
    title = "Claude Code"
    binary = "claude"
    capabilities = CliCapabilities(
        streaming=True,
        approvals="bridged",
        live_view=True,
        tmux=not proc.IS_WINDOWS,
        modes=ALL_MODES,
    )
    forbidden_args = ("--allow-dangerously-skip-permissions",)
    #: Modes that let the CLI approve on its own, out of snowpea's sight: only
    #: the mode table produces them, behind a person's grant.
    forbidden_pairs = (
        ("--permission-mode", "auto"),
        ("--permission-mode", "acceptEdits"),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode", "dontAsk"),
    )
    native_table = NATIVE_MODES
    legacy_native = LEGACY_NATIVE

    def __init__(
        self,
        *,
        tmux_socket: str = "snowpea",
        poll_interval: float = 0.25,
        init_timeout: float = 45.0,
        interrupt_grace: float = 10.0,
        ready_timeout: float = 30.0,
        auth_timeout: float = 15.0,
    ) -> None:
        #: ``tmux -L <name>``: the dedicated server snowpea starts panes on.
        self.tmux_socket = tmux_socket
        self.poll_interval = poll_interval
        self.init_timeout = init_timeout
        self.interrupt_grace = interrupt_grace
        self.ready_timeout = ready_timeout
        self.auth_timeout = auth_timeout
        self._meta: OrderedDict[str, tuple[int, int, _Meta]] = OrderedDict()

    # -- discovery ----------------------------------------------------------
    async def discover(self, path_override: str | None = None) -> Discovery:
        found = await super().discover(path_override)
        if not found.installed or found.executable is None:
            return found
        problems = list(found.problems)
        version = parse_version(found.executable.version)
        if found.executable.version and version is None:
            problems.append(f"could not read the Claude Code version "
                            f"({found.executable.version[:60]!r})")
        elif version is not None and version < MIN_VERSION:
            problems.append(
                f"Claude Code {'.'.join(map(str, version))} is too old; snowpea needs "
                f"{'.'.join(map(str, MIN_VERSION))} or later (run `claude update`)"
            )
        elif version is not None and version[0] > MAX_MAJOR:
            problems.append(
                f"Claude Code {'.'.join(map(str, version))} is newer than this snowpea "
                f"supports ({MAX_MAJOR}.x); update snowpea"
            )
        if not problems:
            logged_in = await self._logged_in(found.executable.path)
            if logged_in is False:
                problems.append("Claude Code is not logged in: run `claude auth login`")
        help_text = await self._help(found.executable.path)
        tmux_ok = await _tmux_usable()
        capabilities = CliCapabilities(
            streaming=True, approvals="bridged", live_view=True, tmux=tmux_ok, modes=ALL_MODES
        )
        return Discovery(
            installed=True,
            executable=found.executable,
            capabilities=capabilities,
            problems=problems,
            native_modes=claude_native_modes(help_text),
        )

    async def _help(self, path: str) -> str | None:
        """``claude --help`` (local, no tokens): which permission modes it accepts."""
        code, out = await self.helper_supervisor().run_helper(
            [path, "--help"],
            cwd=tempfile.gettempdir(),
            env=proc.scrubbed_env(),
            cli="claude",
            timeout=self.auth_timeout,
        )
        if code != 0:
            return None
        return out.decode("utf-8", "replace")[:200_000]

    async def _logged_in(self, path: str) -> bool | None:
        """``loggedIn`` from ``claude auth status --json``; ``None`` when unknown.

        Only that one key is read; the answer's account fields are discarded.
        """
        code, out = await self.helper_supervisor().run_helper(
            [path, "auth", "status", "--json"],
            cwd=tempfile.gettempdir(),
            env=proc.scrubbed_env(),
            cli="claude",
            timeout=self.auth_timeout,
        )
        if code is None:
            return None
        try:
            data = json.loads(out.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            return None
        value = data.get("loggedIn") if isinstance(data, dict) else None
        del data
        return value if isinstance(value, bool) else None

    async def list_models(self, discovery: Discovery | None = None) -> list[str]:
        """Model names ``--model`` accepts (aliases; a full model id works too)."""
        return list(MODEL_ALIASES)

    # -- stored sessions ----------------------------------------------------
    def _session_info(self, path: Path) -> SessionInfo | None:
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path)
        cached = self._meta.get(key)
        if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            meta = cached[2]
            self._meta.move_to_end(key)
        else:
            meta = scan_meta(path)
            self._meta[key] = (st.st_size, st.st_mtime_ns, meta)
            while len(self._meta) > META_CACHE_SIZE:
                self._meta.popitem(last=False)
        if not meta.cwd:
            return None
        return SessionInfo(
            id=path.stem,
            cwd=meta.cwd,
            title=meta.title,
            updated_at=iso_from_unix(st.st_mtime),
            model=meta.model,
            preview=meta.preview,
            extra={"path": str(path)},
        )

    def _list(self, roots: Sequence[str], limit: int, before: str | None) -> list[SessionInfo]:
        projects = config_dir() / "projects"
        clean = [root.rstrip("/") or "/" for root in roots]
        prefixes = [encode_cwd(root) for root in clean]
        try:
            folders = [entry for entry in projects.iterdir() if entry.is_dir()]
        except OSError:
            return []
        candidates: list[tuple[float, Path]] = []
        for folder in folders:
            name = folder.name
            if not any(name == prefix or name.startswith(prefix + "-") or prefix == "-"
                       for prefix in prefixes):
                continue
            try:
                entries = list(folder.glob("*.jsonl"))
            except OSError:
                continue
            for path in entries:
                if not UUID_RE.match(path.stem):
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if before is not None and iso_from_unix(mtime) > before:
                    continue
                candidates.append((mtime, path))
        candidates.sort(key=lambda item: item[0], reverse=True)
        rows: list[SessionInfo] = []
        for _mtime, path in candidates:
            if len(rows) >= limit:
                break
            info = self._session_info(path)
            # The folder name is lossy: the transcript's own cwd decides.
            if info is None or not _under(info.cwd, clean):
                continue
            rows.append(info)
        return rows

    async def list_sessions(
        self, roots: Sequence[str], *, limit: int, before: str | None = None
    ) -> list[SessionInfo]:
        if limit <= 0 or not roots:
            return []
        return await asyncio.to_thread(self._list, list(roots), limit, before)

    def _find(self, session_id: str) -> SessionInfo | None:
        if not UUID_RE.match(session_id):
            return None
        projects = config_dir() / "projects"
        try:
            matches = list(projects.glob(f"*/{session_id}.jsonl"))
        except OSError:
            return None
        best: SessionInfo | None = None
        for path in matches:
            info = self._session_info(path)
            if info is not None and (best is None or (info.updated_at or "")
                                     > (best.updated_at or "")):
                best = info
        return best

    async def get_session(self, session_id: str) -> SessionInfo | None:
        return await asyncio.to_thread(self._find, session_id)

    async def read(self, info: SessionInfo) -> list[TimelineItem]:
        path = Path(info.extra.get("path") or "")
        if not path.is_file():
            found = await self.get_session(info.id)
            if found is None:
                return []
            path = Path(found.extra["path"])
            info.extra["path"] = str(path)
        items, offset, calls = await asyncio.to_thread(read_transcript, path, info.id, info.cwd)
        info.extra["read_offset"] = offset
        info.extra["open_calls"] = calls
        return items

    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        live, _data = await asyncio.to_thread(liveness_of, info.id, set(own_pids))
        return live

    def holder(self, session_id: str, own_pids: set[int]) -> dict[str, Any] | None:
        """The status record of a live process holding ``session_id`` (or ``None``)."""
        return liveness_of(session_id, own_pids)[1]

    async def follow(
        self, info: SessionInfo, *, interval: float = 1.0
    ) -> AsyncIterator[list[TimelineItem]]:
        """Tail the transcript from where :meth:`read` stopped."""
        path = Path(info.extra.get("path") or transcript_path(info.cwd, info.id))
        start = info.extra.get("read_offset")
        tail = TranscriptTail(path, info.id, info.cwd,
                              start=start if isinstance(start, int) else None,
                              builder=TimelineBuilder("claude", info.id))
        calls = info.extra.get("open_calls")
        if isinstance(calls, dict):
            tail.converter.seed_calls(calls)
        while True:
            await asyncio.sleep(interval)
            items = await asyncio.to_thread(tail.poll)
            if items:
                yield items

    # -- running ------------------------------------------------------------
    def channel(self, spec: LaunchSpec, host: ChannelHost) -> ExternalChannel:
        if spec.transport == "tmux":
            from snowpea_core.external.claude_tmux import ClaudeTmuxChannel

            return ClaudeTmuxChannel(spec, host, self)
        return ClaudeChannel(spec, host, self)

    def refuse_if_open_elsewhere(self, session_id: str, own_pids: set[int]) -> None:
        """``external_view_only`` when another process holds ``session_id``.

        A second ``--resume`` of a live session is neither refused nor forked by
        the CLI: both processes append to one transcript and history diverges.
        """
        data = self.holder(session_id, own_pids)
        if data is None:
            return
        where = "a tmux pane" if tmux_field(data.get("tmux")) else "another terminal"
        raise ExternalError(
            errors.EXTERNAL_VIEW_ONLY,
            f"this Claude Code session is open in {where}; it can be continued here once "
            "that one exits",
        )

    def mode_row(self, mode: str, native_mode: str | None) -> NativeMode:
        """The table row a channel runs (``native_mode``, else the legacy ``mode``'s)."""
        if mode not in ALL_MODES:
            raise ExternalError(errors.INVALID_PARAMS, f"unknown mode {mode!r}")
        row = native_modes.find(self.native_table, native_mode or LEGACY_NATIVE[mode])
        if row is None:
            raise ExternalError(errors.INVALID_PARAMS, f"unknown Claude Code mode {native_mode!r}")
        return row

    def argv(self, spec: LaunchSpec, mode: str, grant: UnrestrictedGrant | None,
             session_args: list[str], *, structured: bool,
             native_mode: str | None = None) -> list[str]:
        """The full command line for ``spec`` in ``mode`` (both transports).

        Everything mode-related comes from the row of :data:`NATIVE_MODES`;
        the flags of a person-only row are released only by ``grant``.
        """
        row = self.mode_row(mode, native_mode if native_mode is not None else spec.native_mode)
        args: list[str] = []
        if structured:
            args += [
                "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--replay-user-messages",
            ]
        launch = row.launch
        value = launch.get("permissionMode")
        released: list[str] = list(launch.get("bypass") or ())
        if isinstance(value, str):
            if launch.get("granted"):
                released.insert(0, f"--permission-mode={value}")
            else:
                args += ["--permission-mode", value]
        if structured and row.approvals == "bridged":
            args += ["--permission-prompts", "host", "--permission-prompt-tool", "stdio"]
        elif structured and not launch.get("bypass"):
            # Nobody answers here: anything that would still prompt is denied.
            args += ["--permission-prompts", "none"]
        args += session_args
        model = proc.validate_model(spec.model)
        if model:
            args += ["--model", model]
        bypass = None
        if row.person_only or released:
            if grant is None:
                raise ExternalError(errors.JARVIS_PERSON_ONLY,
                                    f"Claude Code mode {row.label} needs a person's grant")
            if released:
                bypass = proc.bypass_args(grant, released)
        return proc.build_argv(spec.executable, args, bypass=bypass,
                               forbidden=self.forbidden_args,
                               forbidden_pairs=self.forbidden_pairs)


async def _tmux_usable() -> bool:
    if not proc.tmux_supported():
        return False
    from snowpea_core.external.claude_tmux import tmux_version_ok

    return await tmux_version_ok()


# ---------------------------------------------------------------------------
# structured channel (host "snowpea")
# ---------------------------------------------------------------------------


class ClaudeChannel(ExternalChannel):
    """One ``claude -p`` stream-json process for one session."""

    host = "snowpea"

    def __init__(self, spec: LaunchSpec, host: ChannelHost, adapter: ClaudeAdapter) -> None:
        super().__init__(spec, host)
        self.adapter = adapter
        self.process: ExternalProcess | None = None
        self.reader: asyncio.Task[None] | None = None
        self.closing = False
        self.started_mode: str | None = None
        self.started_native: str | None = None
        self.started_grant: UnrestrictedGrant | None = None
        self.started_model: str | None = None
        self.initialized: asyncio.Future[None] | None = None
        self.approval_tasks: dict[str, asyncio.Task[None]] = {}
        self.watchdog: asyncio.Task[None] | None = None
        self.turn_id: str | None = None
        self.turns_done = 0
        self.interrupting = False
        self.model: str | None = spec.model
        self.converter = Converter(host.builder, spec.cwd)
        self._stream_key: str | None = None
        self._stream_message: str | None = None
        self._stream_counter = 0
        self._title: str | None = None

    # -- state ----------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.alive

    def pids(self) -> set[int]:
        return {self.process.pid} if self.alive and self.process is not None else set()

    # -- start ------------------------------------------------------------------
    async def start(self) -> None:
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            await self.host_api.id_known(self.session_id)
        else:
            proc.validate_session_id(self.session_id)
        if self.spec.resume:
            self.adapter.refuse_if_open_elsewhere(
                self.session_id, self.host_api.supervisor.pids() | self.pids()
            )
        await self._spawn(resume=self.spec.resume)

    def _resume_now(self) -> bool:
        assert self.session_id is not None
        return (self.spec.resume or self.turns_done > 0
                or transcript_path(self.spec.cwd, self.session_id).exists())

    async def _spawn(self, *, resume: bool) -> None:
        assert self.session_id is not None
        session_args = ["--resume" if resume else "--session-id", self.session_id]
        argv = self.adapter.argv(self.spec, self.mode, self.grant, session_args, structured=True,
                                 native_mode=self.native_mode)
        process = await self.host_api.supervisor.spawn(
            argv, cwd=self.spec.cwd, env=self.host_api.env, cli="claude", ref=self.session_id
        )
        self.process = process
        self.started_mode = self.mode
        self.started_native = self.native_mode
        self.started_grant = self.grant
        self.started_model = self.spec.model
        self.closing = False
        self.initialized = asyncio.get_running_loop().create_future()
        self.reader = asyncio.ensure_future(self._read(process))
        try:
            await process.write_line({"type": "control_request", "request_id": "init_1",
                                      "request": {"subtype": "initialize"}})
            await asyncio.wait_for(asyncio.shield(self.initialized), self.adapter.init_timeout)
        except (TimeoutError, ExternalError) as exc:
            message = self._start_problem(process)
            await self.close()
            raise unavailable(message) from exc

    def _start_problem(self, process: ExternalProcess) -> str:
        tail = scrub(process.stderr_tail(10))
        lowered = tail.lower()
        if "unknown option" in lowered or "unexpected argument" in lowered:
            return ("this Claude Code version does not understand snowpea's options; "
                    "update it with `claude update`")
        if _looks_like_auth_error(tail):
            return "Claude Code is not logged in: run `claude auth login`"
        if not process.alive:
            last = next((line for line in reversed(tail.splitlines()) if line.strip()), "")
            detail = f": {last[:300]}" if last else ""
            return f"Claude Code exited at start (status {process.returncode}){detail}"
        return "Claude Code did not answer its start-up handshake"

    # -- stdout -----------------------------------------------------------------
    async def _read(self, process: ExternalProcess) -> None:
        try:
            async for frame in process.json_lines():
                try:
                    await self._frame(process, frame)
                except ExternalError:
                    log.debug("claude frame handling failed", exc_info=True)
                except Exception:  # noqa: BLE001 - one bad frame must not end the session
                    log.warning("could not handle a Claude Code frame", exc_info=True)
        finally:
            builder = self.host_api.builder
            builder.flush()
            items = builder.drain()
            if items and process is self.process:
                with contextlib.suppress(Exception):
                    await self.host_api.emit(items)
        code = await process.wait()
        if self.initialized is not None and not self.initialized.done():
            self.initialized.set_exception(unavailable("Claude Code exited"))
            self.initialized.exception()  # retrieved: no "never retrieved" warning
        if not self.closing and process is self.process:
            self._cancel_approvals()
            self._stop_watchdog()
            tail = scrub(process.stderr_tail(5))
            last = next((line for line in reversed(tail.splitlines()) if line.strip()), "")
            detail = f": {last[:300]}" if last else ""
            self.turn_id = None
            await self.host_api.channel_died(f"Claude Code exited (status {code}){detail}")

    async def _frame(self, process: ExternalProcess, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        builder = self.host_api.builder
        if kind == "control_response":
            response = frame.get("response") or {}
            if response.get("request_id") == "init_1" and self.initialized is not None:
                if not self.initialized.done():
                    self.initialized.set_result(None)
            return
        if kind == "control_request":
            await self._control_request(process, frame)
            return
        if kind == "control_cancel_request":
            task = self.approval_tasks.pop(str(frame.get("request_id")), None)
            if task is not None:
                task.cancel()
            return
        if frame.get("parent_tool_use_id"):
            return  # a subagent's own messages: the parent's tool call stands for them
        if kind == "system":
            if frame.get("subtype") == "init":
                model = frame.get("model")
                if isinstance(model, str) and model:
                    self.model = model
            return
        if kind == "stream_event":
            self._stream_event(frame.get("event") or {})
        elif kind == "assistant":
            message = frame.get("message") or {}
            uid = frame.get("uuid")
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and self._stream_key is not None:
                    key, self._stream_key = self._stream_key, None
                    builder.assistant(str(block.get("text", "")), key=key)
                    continue
                self.converter.assistant_block(block, uid=uid)
        elif kind == "user":
            if frame.get("isReplay"):
                return  # the ack of our own prompt; the manager already showed it
            self.converter.user_record(frame, live=True)
        elif kind == "result":
            await self._result(frame)
            return
        items = builder.drain()
        if items:
            await self.host_api.emit(items)

    def _stream_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self._stream_message = message.get("id") if isinstance(message.get("id"), str) else None
            return
        if etype != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return
        if self._stream_key is None:
            self._stream_counter += 1
            base = self._stream_message or f"{self.turn_id}:{self._stream_counter}"
            self._stream_key = f"stream:{base}:{event.get('index', 0)}:{self._stream_counter}"
        self.host_api.builder.delta(text, message_key=self._stream_key)

    async def _result(self, frame: dict[str, Any]) -> None:
        builder = self.host_api.builder
        if self._stream_key is not None:
            builder.flush()
            self._stream_key = None
        self._stream_message = None
        items = builder.drain()
        if items:
            await self.host_api.emit(items)
        self._stop_watchdog()
        self._cancel_approvals()
        interrupted = self.interrupting or frame.get("terminal_reason") == "interrupted"
        self.interrupting = False
        was_turn = self.turn_id is not None
        self.turn_id = None
        if was_turn:
            self.turns_done += 1
        if interrupted:
            await self.host_api.turn_finished("interrupted")
        elif frame.get("is_error"):
            text = frame.get("result")
            text = text if isinstance(text, str) else ""
            if _looks_like_auth_error(text):
                message = "Claude Code is not logged in: run `claude auth login`"
            else:
                subtype = str(frame.get("subtype") or "error")
                message = scrub(text.strip()[:500]) or f"Claude Code ended the turn ({subtype})"
            await self.host_api.turn_finished("error", message)
        else:
            await self.host_api.turn_finished("complete")
        await self._refresh_title()

    async def _refresh_title(self) -> None:
        if self.session_id is None:
            return
        path = transcript_path(self.spec.cwd, self.session_id)
        try:
            meta = await asyncio.to_thread(scan_meta, path)
        except Exception:  # noqa: BLE001 - a title is a nicety
            return
        if meta.title and meta.title != self._title:
            self._title = meta.title
            await self.host_api.title_known(meta.title)

    # -- approvals --------------------------------------------------------------
    async def _control_request(self, process: ExternalProcess, frame: dict[str, Any]) -> None:
        request = frame.get("request") or {}
        request_id = frame.get("request_id")
        if not isinstance(request_id, str):
            return
        if request.get("subtype") == "can_use_tool":
            task = asyncio.ensure_future(self._approve(process, request_id, request))
            self.approval_tasks[request_id] = task
            task.add_done_callback(lambda _t, rid=request_id: self.approval_tasks.pop(rid, None))
            return
        # Anything else (hook callbacks, MCP messages) is not snowpea's to answer.
        with contextlib.suppress(ExternalError):
            await process.write_line({"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": f"snowpea does not handle {request.get('subtype')!r}"}})

    async def _approve(self, process: ExternalProcess, request_id: str,
                       request: dict[str, Any]) -> None:
        tool = str(request.get("tool_name") or "tool")
        args = request.get("input") if isinstance(request.get("input"), dict) else {}
        body: dict[str, Any]
        if tool == "ExitPlanMode":
            body = await self._plan_exit(args)
        else:
            path = args.get("file_path") or args.get("notebook_path") or args.get("path")
            description = request.get("description")
            note = description if isinstance(description, str) else ""
            reason = request.get("decision_reason")
            if isinstance(reason, str) and reason:
                note = f"{note} ({reason})" if note else reason
            ask = ApprovalAsk(
                tool=tool,
                args=dict(args),
                risk=_RISK.get(tool, "medium"),
                note=note[:500],
                diff=preview_diff(tool, args, self.spec.cwd),
                paths=[path] if isinstance(path, str) else None,
            )
            try:
                answer = await self.host_api.ask_approval(ask)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a broken queue denies, never allows
                log.warning("approval bridge failed; denying", exc_info=True)
                answer = None
            if answer is not None and answer.allow:
                body = {"behavior": "allow", "updatedInput": dict(args)}
            else:
                reason_text = (answer.reason if answer is not None else "") or "Denied"
                body = {"behavior": "deny", "message": reason_text, "interrupt": False}
        with contextlib.suppress(ExternalError):
            await process.write_line({"type": "control_response", "response": {
                "subtype": "success", "request_id": request_id, "response": body}})

    async def _plan_exit(self, args: dict[str, Any]) -> dict[str, Any]:
        """Plan mode's "ready to code?" prompt: the person's to answer, never a rule's.

        Bridged as a person-only approval (tool ``ExitPlanMode``, the plan in
        ``args.plan``).  Allowed: Claude Code leaves plan mode for the default
        mode (said explicitly with ``setMode``), and the session row follows.
        """
        ask = ApprovalAsk(
            tool="ExitPlanMode",
            args=dict(args),
            risk="medium",
            note="Claude Code finished its plan and asks to start working on it "
                 "(it leaves plan mode for the default mode, which asks before each change)",
            person_only=True,
        )
        try:
            answer = await self.host_api.ask_approval(ask)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken queue keeps planning
            log.warning("plan approval bridge failed; staying in plan mode", exc_info=True)
            answer = None
        if answer is None or not answer.allow:
            reason = (answer.reason if answer is not None else "") or \
                "The person wants to keep planning; refine the plan instead."
            return {"behavior": "deny", "message": reason, "interrupt": False}
        leave_to = "default"
        if self.native_mode == "plan" or (self.native_mode is None and self.mode == "readonly"):
            # The next turns run as the CLI now does; no restart for it.
            self.mode = self.started_mode = "default"
            self.native_mode = self.started_native = leave_to
            with contextlib.suppress(Exception):
                await self.host_api.mode_known(leave_to)
        return {
            "behavior": "allow",
            "updatedInput": dict(args),
            "updatedPermissions": [
                {"type": "setMode", "mode": leave_to, "destination": "session"}
            ],
        }

    def _cancel_approvals(self) -> None:
        for task in list(self.approval_tasks.values()):
            task.cancel()
        self.approval_tasks.clear()

    # -- turns ------------------------------------------------------------------
    async def send(self, text: str, turn_id: str) -> None:
        if "\x00" in text:
            raise ExternalError(errors.INVALID_PARAMS, "prompt contains NUL")
        restart = self.process is None or not self.process.alive or (
            self.started_mode != self.mode or self.started_native != self.native_mode
            or self.started_grant is not self.grant or self.started_model != self.spec.model
        )
        if restart:
            # A mode or model switch takes effect between turns: the process
            # restarts on the same session with the new flags.
            await self._stop_process()
            await self._spawn(resume=self._resume_now())
        assert self.process is not None
        self.turn_id = turn_id
        self.interrupting = False
        self._stream_key = None
        await self.process.write_line(
            {"type": "user", "message": {"role": "user", "content": text},
             "parent_tool_use_id": None, "session_id": ""}
        )

    async def interrupt(self) -> None:
        process = self.process
        if process is None or not process.alive or self.turn_id is None:
            return
        self.interrupting = True
        self._cancel_approvals()
        await process.write_line(
            {"type": "control_request", "request_id": f"int_{uuid.uuid4().hex[:8]}",
             "request": {"subtype": "interrupt"}}
        )
        self._stop_watchdog()
        self.watchdog = asyncio.ensure_future(self._interrupt_watchdog(process, self.turn_id))

    async def _interrupt_watchdog(self, process: ExternalProcess, turn_id: str) -> None:
        """A CLI that does not end the turn after an interrupt is stopped (tree kill)."""
        await asyncio.sleep(self.adapter.interrupt_grace)
        if self.turn_id != turn_id or process is not self.process or self.closing:
            return
        log.info("Claude Code did not stop after an interrupt; stopping it")
        self.closing = True
        self.turn_id = None
        await process.terminate(grace=1.0)
        await self.host_api.turn_finished("interrupted")
        await self.host_api.channel_died(
            "Claude Code did not stop when asked, so snowpea stopped it"
        )

    def _stop_watchdog(self) -> None:
        task = self.watchdog
        self.watchdog = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def set_mode(self, mode: str, grant: UnrestrictedGrant | None) -> None:
        # Applied by ``send`` when the next turn starts (the process restarts).
        self.mode = mode
        self.grant = grant

    # -- close --------------------------------------------------------------------
    async def _stop_process(self) -> None:
        self.closing = True
        self._cancel_approvals()
        self._stop_watchdog()
        process = self.process
        if process is not None and process.alive:
            # Closing stdin ends Claude cleanly (transcript flushed); then the tree.
            await process.close_stdin()
            deadline = time.monotonic() + 3.0
            while process.alive and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            await process.terminate(grace=1.0)
        reader = self.reader
        if reader is not None and reader is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(reader), timeout=5)
            if not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        self.reader = None

    async def close(self) -> None:
        await self._stop_process()
        self.turn_id = None


__all__ = [
    "MODEL_ALIASES",
    "ClaudeAdapter",
    "ClaudeChannel",
    "Converter",
    "TranscriptTail",
    "config_dir",
    "encode_cwd",
    "iter_lines",
    "live_holders",
    "liveness_of",
    "parse_version",
    "read_status",
    "read_transcript",
    "scan_meta",
    "status_time",
    "tmux_field",
    "transcript_path",
]
