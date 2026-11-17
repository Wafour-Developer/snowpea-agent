"""The Cursor agent CLI (``cursor-agent``) adapter (M19 WP6d, spike S4).

Cursor has no long-lived structured channel and no permission protocol, so a
session snowpea runs is a *series of processes*, one per message::

    cursor-agent -p --output-format stream-json --stream-partial-output
        --trust --sandbox enabled --workspace <root> --resume <chatId>
        [--mode ask] [--model <id>] [--yolo] -- <prompt>

The prompt is the one argument a CLI gets in argv (cursor does not read
stdin); it always follows ``--`` so it can never be read as a flag, and a
prompt too long for the OS argument limits is refused with a clear error.

Modes: ``readonly`` (``--mode ask``, the built-in default) cannot edit;
``default`` edits inside the sandboxed workspace (``--sandbox enabled``, no
``--yolo``) and runs shell commands in the sandbox with no prompt, because
cursor has no way to ask; ``unrestricted`` adds ``--yolo`` and only ever gets
it from :func:`proc.bypass_args` behind a person's grant.  ``-f``/``--force``
is never passed.  Approvals are ``none`` in readonly and unrestricted and
``degraded`` in default; ``controlled`` is true only in readonly.

Stored chats (spike S4): ``~/.cursor/chats/<md5(workspace)>/<chatId>/meta.json``
and ``~/.cursor/projects/<encoded>/agent-transcripts/<chatId>/<chatId>.jsonl``.
The transcript is lossy (no tool results, no thinking, no timestamps); it is
used for chats snowpea did not drive and for history.

Open elsewhere: a process (not ours) with ``chats/<hash>/<chatId>/store.db``
open is a terminal holding the chat (``/proc/<pid>/fd`` on Linux, ``lsof``
on macOS, unknown — reported as ``none`` — on Windows).  Cursor does not fence
concurrent writers, so :meth:`CursorChannel.send` re-checks before each turn.

Leftover processes: print mode does not exit after its ``result`` (it keeps
language servers, a shell and a worker running, some in other sessions), and
may leave a detached ``worker-server`` that leaves our process group.  Every
process snowpea starts carries a per-run marker in its environment
(:data:`TAG_ENV`) that all of them inherit; after each turn's result (and a
short grace), on interrupt and on close the channel kills exactly the processes
carrying its markers, never a worker or shell the owner started.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from snowpea_core.external import modes as native_modes
from snowpea_core.external import proc
from snowpea_core.external.base import (
    ALL_MODES,
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
from snowpea_core.external.modes import NativeMode
from snowpea_core.external.proc import ExternalError, ExternalProcess, PinnedExecutable
from snowpea_core.external.timeline import (
    RecordFilter,
    TimelineBuilder,
    TimelineItem,
    clip,
    iso_from_ms,
    iso_from_unix,
    stable_key,
)
from snowpea_core.server import errors

log = logging.getLogger("snowpea.external.cursor")

#: Oldest ``cursor-agent`` release whose print mode matches spike S4.
MIN_VERSION: tuple[int, int, int] = (2026, 10, 1)
#: Newest release line this adapter knows the stream format of; a version past
#: it is refused until snowpea is updated (cursor versions are dates).
MAX_VERSION: tuple[int, int, int] = (2027, 12, 31)
_VERSION_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")

#: Cursor's modes (1.56.0), from ``cursor-agent --help`` 2026.11.13: ``--mode``
#: choices ``plan`` ("read-only/planning (analyze, propose plans, no edits)")
#: and ``ask`` ("Q&A style ... (read-only)"); no ``--mode`` is its agent mode;
#: ``--yolo`` ("Run Everything").  Print mode has no permission prompt at all
#: (spike S4), so nothing is ever bridged: the agent edits and runs shell in
#: its sandbox unasked.  ``launch``: ``args`` added as they are, ``bypass``
#: released only by a person's grant.  ``--auto-review`` is not offered: its
#: prompts cannot be answered in print mode.  Plan is described as writing
#: its plan (not verified by a run; never called read-only).
NATIVE_MODES: tuple[NativeMode, ...] = (
    NativeMode("ask", "Ask", "never", "none", "none", False, "readonly",
               launch={"args": ("--mode", "ask")}, terminal={"cycle": True}),
    NativeMode("plan", "Plan", "never", "plan", "none", False, "readonly",
               launch={"args": ("--mode", "plan")}, requires="plan",
               terminal={"cycle": True}),
    NativeMode("agent", "Agent", "never", "workspace", "none", False, "default"),
    NativeMode("yolo", "Run Everything", "never", "anywhere", "none", True, "unrestricted",
               launch={"bypass": ("--yolo",)}),
)
LEGACY_NATIVE: dict[str, str] = {"default": "agent", "readonly": "ask", "unrestricted": "yolo"}

#: Environment variable that marks every process a channel starts (and the
#: ``worker-server`` they leave behind).  Not ``SNOWPEA_*``: those are scrubbed.
TAG_ENV = "SP_EXTERNAL_RUN"

#: Longest prompt, in UTF-8 bytes, that may go into argv.  Linux caps a single
#: argument at 128 KiB (MAX_ARG_STRLEN); this leaves room under it.
MAX_PROMPT_BYTES = 100_000
#: Windows caps the whole command line at 32767 UTF-16 units.
_WINDOWS_CMDLINE_MAX = 32_000
#: Headroom kept below ``ARG_MAX`` for argv plus environment.
_ARG_MAX_HEADROOM = 64 * 1024

#: Seconds without a stdout line before a running turn counts as hung.
STALL_SEC = 900.0
#: Seconds the CLI may take to exit after its ``result`` before its tree is stopped
#: (the real one stays up with language servers and a shell, so this is usual).
EXIT_GRACE_SEC = 2.0
#: Seconds ``create-chat``, ``status`` and ``--list-models`` may take.
COMMAND_TIMEOUT_SEC = 30.0
#: Seconds the model list is cached per executable.
MODELS_TTL_SEC = 600.0
#: Seconds the open-file scan behind :meth:`CursorAdapter.liveness` is reused.
_HOLDERS_TTL_SEC = 1.0
#: A transcript written this recently means the holder is mid-turn.
_BUSY_WINDOW_SEC = 8.0
#: Bytes of a transcript read for a listing preview.
_PREVIEW_BYTES = 64 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_MODEL_LINE_RE = re.compile(r"^(\S+)\s+-\s+(.+)$")
_TIMESTAMP_RE = re.compile(r"<timestamp>(.*?)</timestamp>\s*", re.S)
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_LOCAL_TIME_RE = re.compile(
    r"^\s*\w+,\s+(\w{3})\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s*([AP]M)"
    r"\s*\(UTC(?:([+-])(\d{1,2})(?::(\d{2}))?)?\)"
)
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_NOT_LOGGED_IN_RE = re.compile(r"not (?:logged|signed) in|log ?in|authenticat|unauthori", re.I)

#: Transcript records that are conversation; anything else is never parsed.
TRANSCRIPT_FILTER = RecordFilter(allow=frozenset({"user", "assistant", "turn_ended"}))


def _record_kind(record: Any) -> Any:
    if not isinstance(record, dict):
        return None
    return record.get("role") or record.get("type")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """``2026.11.13-eacade1`` -> ``(2026, 11, 13)``; ``None`` when it is not a date."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def version_problem(text: str | None) -> str | None:
    """Why this ``cursor-agent --version`` cannot be used, or ``None``."""
    version = parse_version(text)
    low = ".".join(str(part) for part in MIN_VERSION)
    if version is None:
        return (f"cursor-agent reported an unrecognised version {text!r}; snowpea supports "
                f"{low} and newer")
    if version < MIN_VERSION:
        return (f"cursor-agent {text} is too old; snowpea needs {low} or newer "
                "(run `cursor-agent update`)")
    if version > MAX_VERSION:
        high = ".".join(str(part) for part in MAX_VERSION)
        return (f"cursor-agent {text} is newer than this snowpea supports (up to {high}); "
                "update snowpea")
    return None


def workspace_hash(cwd: str) -> str:
    """The chat folder name cursor derives from a workspace path."""
    return hashlib.md5(cwd.encode("utf-8")).hexdigest()  # noqa: S324 - cursor's own scheme


def encoded_workspace(cwd: str) -> str:
    """The lossy ``projects/<encoded>`` folder name: runs of anything but letters and
    digits become one ``-`` (``/a/-b/c_d.e`` -> ``a-b-c-d-e``)."""
    return re.sub(r"[^A-Za-z0-9]+", "-", cwd).strip("-")


def parse_models(text: str) -> list[str]:
    """Model ids from ``--list-models`` output (``<id> - <name>`` lines)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = _INVISIBLE_RE.sub("", _ANSI_RE.sub("", raw)).strip()
        match = _MODEL_LINE_RE.match(line)
        if not match:
            continue
        model = match.group(1)
        try:
            proc.validate_model(model)
        except ExternalError:
            continue
        if model not in out:
            out.append(model)
    return out


def user_text(raw: str) -> tuple[str, str | None]:
    """A transcript user record's text without cursor's wrappers, and its local time."""
    stamp = _TIMESTAMP_RE.search(raw)
    query = _USER_QUERY_RE.search(raw)
    if query is not None:
        text = query.group(1)
    else:
        text = _TIMESTAMP_RE.sub("", raw).strip()
    return text, parse_local_time(stamp.group(1)) if stamp else None


def parse_local_time(text: str) -> str | None:
    """``Sunday, Nov 15, 2026, 9:09 PM (UTC+9)`` -> UTC ISO, or ``None``."""
    match = _LOCAL_TIME_RE.match(text or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1))
    if month is None:
        return None
    hour = int(match.group(4)) % 12 + (12 if match.group(6) == "PM" else 0)
    offset = timedelta(0)
    if match.group(7):
        offset = timedelta(hours=int(match.group(8)), minutes=int(match.group(9) or 0))
        if match.group(7) == "-":
            offset = -offset
    try:
        moment = datetime(int(match.group(3)), month, int(match.group(2)), hour,
                          int(match.group(5)), tzinfo=timezone(offset))
    except ValueError:
        return None
    return iso_from_unix(moment.timestamp())


def _text_blocks(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in content or [] if isinstance(block, dict)]


def _first_line(text: str, limit: int = 80) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:limit]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _records(path: Path, limit_bytes: int | None = None) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit_bytes) if limit_bytes else handle.read()
    except OSError:
        return []
    return _parse_lines(raw.split(b"\n"))


#: Most bytes one live-tail look reads; a longer gain is read over several looks.
_TAIL_BYTES = 4 * 1024 * 1024
#: Longest transcript line parsed; a longer one is skipped (security review L1).
_MAX_LINE_BYTES = 16 * 1024 * 1024


def _parse_lines(lines: list[bytes]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip() or len(line) > _MAX_LINE_BYTES:
            continue
        try:
            value = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            out.append(value)
    return list(TRANSCRIPT_FILTER.apply(out, _record_kind))


def _records_tail(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Complete records after byte ``offset`` (filtered) and the new offset (L2)."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(_TAIL_BYTES)
    except OSError:
        return [], offset
    end = raw.rfind(b"\n")
    if end < 0:
        # No complete line yet; a line longer than one look is skipped whole.
        return ([], offset + len(raw)) if len(raw) >= _TAIL_BYTES else ([], offset)
    return _parse_lines(raw[:end].split(b"\n")), offset + end + 1


def _write_diff(path: str, contents: str) -> str:
    lines = contents.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def _replace_diff(path: str, old: str, new: str) -> str:
    minus = "".join(f"-{line}\n" for line in old.splitlines())
    plus = "".join(f"+{line}\n" for line in new.splitlines())
    return (f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(old.splitlines())} +1,{len(new.splitlines())} @@\n{minus}{plus}")


def _history_diff(name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    """``(path, patch)`` for an edit tool in a transcript (which has no results)."""
    path = args.get("path") or args.get("file_path")
    if name == "ApplyPatch" and isinstance(args.get("patch"), str):
        found = re.search(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", args["patch"], re.M)
        return (found.group(1).strip() if found else str(path or "patch")), args["patch"]
    if not isinstance(path, str):
        return None
    if name == "StrReplace" and isinstance(args.get("old_string"), str):
        return path, _replace_diff(path, args["old_string"], str(args.get("new_string") or ""))
    if name == "Write" and isinstance(args.get("contents"), str):
        return path, _write_diff(path, args["contents"])
    return None


def tool_name(kind: str) -> str:
    """``editToolCall`` -> ``Edit``; ``shellToolCall`` -> ``Shell``."""
    stem = kind[: -len("ToolCall")] if kind.endswith("ToolCall") else kind
    return (stem[:1].upper() + stem[1:]) or "Tool"


def _clip_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {"value": clip(str(args), 4000)} if args is not None else {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        out[str(key)] = clip(value, 4000) if isinstance(value, str) else value
    return out


def _result_text(success: dict[str, Any]) -> str:
    for key in ("interleavedOutput", "output", "content", "message"):
        value = success.get(key)
        if isinstance(value, str) and value:
            return value
    parts = [success.get("stdout"), success.get("stderr")]
    text = "\n".join(part for part in parts if isinstance(part, str) and part)
    if text:
        return text
    try:
        return json.dumps({k: v for k, v in success.items() if k != "afterFullFileContent"},
                          ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# processes: who holds a chat, which processes are ours
# ---------------------------------------------------------------------------


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _proc_has_env(pid: int, needle: bytes) -> bool:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return needle in b"\0" + raw


def _zombie(pid: int) -> bool:
    """True for an exited process its parent has not reaped yet (Linux)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    fields = raw[raw.rfind(")") + 2 :].split()
    return bool(fields) and fields[0] == "Z"


def _holders_linux(prefix: str) -> dict[str, set[int]]:
    """``{path: pids}`` for every open file under ``prefix`` (own user's processes only)."""
    found: dict[str, set[int]] = {}
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(fd)
            except OSError:
                continue
            if link.startswith(prefix):
                found.setdefault(link, set()).add(int(entry.name))
    return found


def _holders_lsof(target: str) -> set[int]:
    try:
        out = subprocess.run(
            [proc.trusted_tool("lsof") or "/usr/sbin/lsof", "-t", "--", target],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(line) for line in out.split() if line.isdigit()}


def tagged_pids(tags: set[str] | frozenset[str]) -> set[int]:
    """Processes carrying ``TAG_ENV=<one of tags>`` in their environment (own user only)."""
    if not tags:
        return set()
    if sys.platform.startswith("linux"):
        needles = [f"\0{TAG_ENV}={tag}\0".encode() for tag in tags]
        pids: set[int] = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = b"\0" + (entry / "environ").read_bytes()
            except OSError:
                continue
            if any(needle in raw for needle in needles):
                pids.add(int(entry.name))
        return pids
    if proc.IS_WINDOWS:  # pragma: no cover - Windows only
        return set()
    try:  # pragma: no cover - macOS / BSD: ``ps -E`` shows our own processes' environment
        out = subprocess.run(
            [proc.trusted_tool("ps") or "/bin/ps", "-E", "-ww", "-x", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return set()
    pattern = re.compile(  # pragma: no cover
        r"(^|\s)" + re.escape(TAG_ENV) + "=(" + "|".join(re.escape(t) for t in tags) + r")(\s|$)"
    )
    found: set[int] = set()  # pragma: no cover
    for line in out.splitlines():  # pragma: no cover
        head, _, rest = line.strip().partition(" ")
        if head.isdigit() and pattern.search(rest):
            found.add(int(head))
    return found  # pragma: no cover


async def kill_pids(pids: dict[int, str | None], grace: float = proc.KILL_GRACE_SEC) -> None:
    """SIGTERM, then SIGKILL, each ``pid`` whose start time still matches."""
    if proc.IS_WINDOWS:  # pragma: no cover - Windows only
        for pid in pids:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                await asyncio.to_thread(
                    subprocess.run, ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, check=False, timeout=15,
                )
        return

    def same(pid: int) -> bool:
        stamp = pids[pid]
        return (proc.pid_alive(pid) and not _zombie(pid)
                and (stamp is None or proc.process_start_time(pid) == stamp))

    live = [pid for pid in pids if same(pid)]
    for pid in live:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(same(pid) for pid in live):
        await asyncio.sleep(0.05)
    for pid in live:
        if same(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def check_prompt_size(argv: Sequence[str], env: dict[str, str]) -> None:
    """Refuse a prompt the OS would not pass whole (``invalid_params``)."""
    prompt = argv[-1] if argv else ""
    size = len(prompt.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise ExternalError(
            errors.INVALID_PARAMS,
            f"the prompt is too long for Cursor ({size} bytes; at most {MAX_PROMPT_BYTES}): "
            "cursor-agent takes its prompt on the command line. Put the long text in a file "
            "in the project and ask Cursor to read it.",
            {"bytes": size, "limit": MAX_PROMPT_BYTES},
        )
    if proc.IS_WINDOWS:  # pragma: no cover - Windows only
        length = sum(len(arg) + 3 for arg in argv)
        if length > _WINDOWS_CMDLINE_MAX:
            raise ExternalError(
                errors.INVALID_PARAMS,
                f"the prompt is too long for Cursor on Windows ({length} characters on the "
                f"command line; at most {_WINDOWS_CMDLINE_MAX})",
            )
        return
    try:
        arg_max = int(os.sysconf("SC_ARG_MAX"))
    except (AttributeError, ValueError, OSError):
        arg_max = 256 * 1024
    total = sum(len(arg.encode("utf-8")) + 1 + 8 for arg in argv)
    total += sum(len(key.encode()) + len(value.encode()) + 2 + 8 for key, value in env.items())
    if total > arg_max - _ARG_MAX_HEADROOM:
        raise ExternalError(
            errors.INVALID_PARAMS,
            f"the prompt is too long for Cursor ({size} bytes): the command line and "
            f"environment would exceed this system's limit ({arg_max} bytes)",
            {"bytes": size},
        )


# ---------------------------------------------------------------------------
# channel
# ---------------------------------------------------------------------------


class CursorChannel(ExternalChannel):
    """A cursor chat snowpea runs: one ``cursor-agent -p`` process per message."""

    #: Seconds a new turn waits for the previous turn's reader to finish cleaning up.
    READER_SETTLE_SEC = 15.0

    def __init__(self, spec: LaunchSpec, host: ChannelHost, adapter: CursorAdapter) -> None:
        super().__init__(spec, host)
        self.adapter = adapter
        self.process: ExternalProcess | None = None
        self.reader: asyncio.Task[None] | None = None
        self.opened = False
        self.closing = False
        self.created = not spec.resume
        self.turns = 0
        self.turn_id: str | None = None
        #: Marker of every process this channel started that may still run.
        self._tags: set[str] = set()
        self._ended: set[str] = set()
        self._interrupted: set[str] = set()

    # -- state ----------------------------------------------------------
    @property
    def alive(self) -> bool:
        """Open for messages; between turns no process runs, and that is fine."""
        return self.opened and not self.closing

    def pids(self) -> set[int]:
        if self.process is None or not self.process.alive:
            return set()
        return {self.process.pid, *proc.descendants(self.process.pid)}

    def _env(self) -> tuple[str, dict[str, str]]:
        tag = f"cursor-{uuid.uuid4().hex}"
        self._tags.add(tag)
        return tag, {**self.host_api.env, TAG_ENV: tag}

    # -- start ----------------------------------------------------------
    async def start(self) -> None:
        self._check_mode(self.mode, self.grant)
        if self.spec.resume:
            if self.session_id is None:
                raise ExternalError(errors.INVALID_PARAMS, "no Cursor chat to continue")
            proc.validate_session_id(self.session_id)
            known = self.session_id in self.adapter.created_ids
            if not known and await self.adapter.get_session(self.session_id) is None:
                raise ExternalError(errors.NOT_FOUND, f"no Cursor chat {self.session_id}")
        elif self.session_id is None:
            self.session_id = await self._create_chat()
            self.adapter.created_ids.add(self.session_id)
            await self.host_api.id_known(self.session_id)
        self.opened = True

    async def _create_chat(self) -> str:
        """``cursor-agent create-chat`` prints a fresh chat id and spends no tokens."""
        argv = proc.build_argv(self.spec.executable, ["create-chat"],
                               forbidden=self.adapter.forbidden_args,
                               forbidden_pairs=self.adapter.forbidden_pairs)
        tag, env = self._env()
        child = await self.host_api.supervisor.spawn(
            argv, cwd=self.spec.cwd, env=env, cli="cursor", ref="create-chat", stdin=False,
        )
        lines: list[str] = []

        async def collect() -> int:
            async for line in child.lines():
                lines.append(line)
            return await child.wait()

        try:
            code = await asyncio.wait_for(collect(), COMMAND_TIMEOUT_SEC)
        except TimeoutError as exc:
            await child.terminate(grace=1.0)
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE,
                                "cursor-agent create-chat did not answer") from exc
        finally:
            await self._reap({tag})
        chat = next((line.strip() for line in reversed(lines) if line.strip()), "")
        if code != 0 or not proc.SESSION_ID_RE.fullmatch(chat):
            detail = child.stderr_tail(5)
            if _NOT_LOGGED_IN_RE.search(detail):
                raise ExternalError(errors.EXTERNAL_UNAVAILABLE, self.adapter.LOGIN_HINT)
            raise ExternalError(
                errors.EXTERNAL_UNAVAILABLE,
                f"cursor-agent create-chat failed (status {code})"
                + (f": {_first_line(detail, 200)}" if detail else ""),
            )
        return chat

    def _row(self, mode: str | None = None, native_mode: str | None = None) -> NativeMode:
        mode = mode or self.mode
        proc.validate_mode(mode, self.adapter.capabilities.modes)
        mode_id = native_mode or self.native_mode or LEGACY_NATIVE.get(mode, "")
        row = native_modes.find(NATIVE_MODES, mode_id)
        if row is None:
            raise ExternalError(errors.INVALID_PARAMS, f"unknown Cursor mode {mode_id!r}")
        return row

    def _check_mode(self, mode: str, grant: proc.UnrestrictedGrant | None) -> None:
        row = self._row(mode)
        if (row.person_only or row.launch.get("bypass")) and grant is None:
            raise ExternalError(errors.JARVIS_PERSON_ONLY,
                                f"Cursor mode {row.label} needs a person's confirmation")

    async def set_mode(self, mode: str, grant: proc.UnrestrictedGrant | None) -> None:
        self._check_mode(mode, grant)
        await super().set_mode(mode, grant)

    # -- argv -----------------------------------------------------------
    def argv(self, text: str) -> list[str]:
        """The command line for one message (every check in :func:`proc.build_argv`)."""
        if self.session_id is None:
            raise ExternalError(errors.INTERNAL, "the Cursor chat has no id")
        args = [
            "-p", "--output-format", "stream-json", "--stream-partial-output",
            "--trust", "--sandbox", "enabled",
            "--workspace", self.spec.cwd,
            "--resume", proc.validate_session_id(self.session_id),
        ]
        row = self._row()
        args += list(row.launch.get("args") or ())
        model = proc.validate_model(self.spec.model)
        if model:
            args += ["--model", model]
        bypass = None
        flags = list(row.launch.get("bypass") or ())
        if row.person_only or flags:
            if self.grant is None:
                raise ExternalError(errors.JARVIS_PERSON_ONLY,
                                    f"Cursor mode {row.label} needs a person's confirmation")
            if flags:
                bypass = proc.bypass_args(self.grant, flags)
        return proc.build_argv(
            self.spec.executable, args, bypass=bypass,
            forbidden=self.adapter.forbidden_args,
            forbidden_pairs=self.adapter.forbidden_pairs,
            prompt_arg=text,
        )

    # -- turns ----------------------------------------------------------
    async def send(self, text: str, turn_id: str) -> None:
        if not self.alive:
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "this Cursor chat is closed")
        previous = self.reader
        if previous is not None and not previous.done():
            # The last turn already ended; its reader is only stopping the process.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(previous), self.READER_SETTLE_SEC)
        if self.process is not None and self.process.alive:
            raise ExternalError(errors.SESSION_BUSY, "a Cursor turn is still running")
        argv = self.argv(text)
        tag, env = self._env()
        try:
            check_prompt_size(argv, env)
        except ExternalError:
            self._tags.discard(tag)
            raise
        assert self.session_id is not None
        info = await self.adapter.get_session(self.session_id)
        if info is not None:
            live = await self.adapter.liveness(info, own_pids=self.pids())
            if live.host != "none":
                self._tags.discard(tag)
                raise ExternalError(
                    errors.EXTERNAL_VIEW_ONLY,
                    "this Cursor chat is open in another terminal; cursor does not stop two "
                    "writers, so snowpea can continue it once that one exits",
                )
        self.process = await self.host_api.supervisor.spawn(
            argv, cwd=self.spec.cwd, env=env, cli="cursor", ref=self.session_id, stdin=False,
        )
        self.turn_id = turn_id
        self.turns += 1
        self.reader = asyncio.ensure_future(self._read(self.process, turn_id, tag))
        if self.created and self.turns == 1:
            await self.host_api.title_known(_first_line(text) or "Cursor chat")

    async def _finish(self, turn_id: str, reason: str, error: str | None = None) -> None:
        if self.closing or turn_id in self._ended:
            return
        self._ended.add(turn_id)
        if self.turn_id == turn_id:
            self.turn_id = None
        await self.host_api.turn_finished(reason, error)

    async def _read(self, process: ExternalProcess, turn_id: str, tag: str) -> None:
        stream = _StreamState(self.host_api.builder, turn_id)
        frames = process.json_lines().__aiter__()
        stalled = False
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(frames.__anext__(), self.adapter.stall_sec)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    stalled = True
                    break
                result = stream.feed(frame)
                await self.host_api.emit(stream.builder.drain())
                if result is not None:
                    ok, message = result
                    if ok:
                        await self._finish(turn_id, "complete")
                    else:
                        await self._finish(turn_id, "error",
                                           message or "Cursor reported an error")
                    break
            if stalled:
                await process.terminate()
            elif stream.ended:
                # The real CLI keeps running after its result (language servers,
                # a shell, the worker): give it a moment, then stop the tree.
                try:
                    await asyncio.wait_for(process.wait(), self.adapter.exit_grace_sec)
                except TimeoutError:
                    await process.terminate(grace=1.0)
            code = await process.wait()
            await self._reap({tag})
            stream.close()
            await self.host_api.emit(stream.builder.drain())
            if turn_id in self._interrupted:
                await self._finish(turn_id, "interrupted")
            elif stalled:
                await self._finish(
                    turn_id, "error",
                    f"cursor-agent stopped answering for {int(self.adapter.stall_sec)} s; "
                    "snowpea stopped it",
                )
            elif not stream.ended:
                await self._finish(turn_id, "error", self._exit_message(process, code))
            if process.garbage_lines:
                log.info("cursor-agent wrote %d non-JSON lines", process.garbage_lines)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a reader bug must still end the turn
            log.warning("reading cursor-agent output failed", exc_info=True)
            with contextlib.suppress(Exception):
                await process.terminate(grace=1.0)
            with contextlib.suppress(Exception):
                await self._reap({tag})
            await self._finish(turn_id, "error", "snowpea could not read Cursor's output")
        finally:
            if self.turn_id == turn_id:
                self.turn_id = None
            self._interrupted.discard(turn_id)

    def _exit_message(self, process: ExternalProcess, code: int) -> str:
        detail = process.stderr_tail(5)
        if _NOT_LOGGED_IN_RE.search(detail):
            return self.adapter.LOGIN_HINT
        message = f"cursor-agent exited with status {code} before finishing the turn"
        if detail:
            message += f": {_first_line(detail, 300)}"
        return message

    async def interrupt(self) -> None:
        """No in-band cancel: kill the turn's process tree; the reader ends the turn."""
        process = self.process
        if process is None or not process.alive or self.turn_id is None:
            return
        self._interrupted.add(self.turn_id)
        await process.terminate()

    async def close(self) -> None:
        self.closing = True
        self.opened = False
        process = self.process
        if process is not None and process.alive:
            await process.terminate(grace=1.0)
        reader = self.reader
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await self._reap(set(self._tags))

    async def _reap(self, tags: set[str]) -> None:
        """Kill what these runs left behind (the detached ``worker-server``)."""
        try:
            pids = await asyncio.to_thread(tagged_pids, tags)
        except Exception:  # noqa: BLE001 - leftovers are best effort, never a crash
            log.debug("scanning for leftover cursor processes failed", exc_info=True)
            return
        pids.discard(os.getpid())
        if pids:
            stamps = {pid: proc.process_start_time(pid) for pid in pids}
            await kill_pids(stamps, grace=1.0)
            if any(proc.pid_alive(pid) and not _zombie(pid) for pid in pids):
                return  # keep the tags: close() tries again
        self._tags -= tags


class _StreamState:
    """Cursor stream-json -> timeline items for one turn."""

    def __init__(self, builder: TimelineBuilder, turn_id: str) -> None:
        self.builder = builder
        self.turn_id = turn_id
        self.segment = 0
        self.open_key: str | None = None
        self.open_text = ""
        #: Streamed segments already closed in this turn (a tool call closes one).
        self.shown: list[str] = []
        self.ended = False

    def _segment_key(self) -> str:
        self.segment += 1
        return stable_key(self.builder.session_id, self.turn_id, "segment", self.segment)

    def _close_segment(self) -> None:
        if self.open_key is not None:
            self.builder.flush(turn_id=self.turn_id)
            self.shown.append(self.open_text)
        self.open_key = None
        self.open_text = ""

    def _assistant(self, frame: dict[str, Any], ts: str | None) -> None:
        texts = [block.get("text") for block in _text_blocks(frame.get("message"))
                 if block.get("type") == "text" and isinstance(block.get("text"), str)]
        text = "".join(texts)
        if "timestamp_ms" in frame:
            # A streamed delta (--stream-partial-output).
            if not text:
                return
            if self.open_key is None:
                self.open_key = self._segment_key()
            self.open_text += text
            self.builder.delta(text, message_key=self.open_key, ts=ts, turn_id=self.turn_id)
            return
        # A whole message: after streaming, it is the closing copy of what was
        # streamed and is never shown twice.
        if self.open_key is not None and text in (self.open_text, "".join(self.shown) +
                                                  self.open_text):
            self.builder.assistant(self.open_text, key=self.open_key, turn_id=self.turn_id)
            self.shown.append(self.open_text)
            self.open_key = None
            self.open_text = ""
            return
        if text in self.shown or (self.shown and text == "".join(self.shown)):
            return
        if self.open_key is not None:
            # Streamed text and a different final text: keep the final one.
            self.builder.assistant(text, key=self.open_key, turn_id=self.turn_id)
            self.shown.append(text)
            self.open_key = None
            self.open_text = ""
            return
        if text:
            self.builder.assistant(text, key=self._segment_key(), ts=ts, turn_id=self.turn_id)
            self.shown.append(text)

    def feed(self, frame: dict[str, Any]) -> tuple[bool, str | None] | None:
        """Convert one frame; returns ``(ok, message)`` when it ends the turn."""
        kind = frame.get("type")
        ts = iso_from_ms(frame["timestamp_ms"]) if "timestamp_ms" in frame else None
        if kind == "thinking":
            text = frame.get("text")
            if frame.get("subtype") == "delta" and isinstance(text, str) and text:
                self.builder.reasoning(text, ts=ts, turn_id=self.turn_id)
        elif kind == "assistant":
            self._assistant(frame, ts)
        elif kind == "tool_call":
            self._tool(frame, ts)
        elif kind == "result":
            self._close_segment()
            self.ended = True
            if frame.get("is_error") or frame.get("subtype") not in (None, "success"):
                message = frame.get("result") or frame.get("error") or frame.get("subtype")
                return False, str(message)[:2000] if message else None
            return True, None
        elif kind == "error":
            message = frame.get("message") or frame.get("error") or "Cursor reported an error"
            self.builder.error(errors.EXTERNAL_UNAVAILABLE, str(message)[:2000],
                               turn_id=self.turn_id)
        return None

    def _tool(self, frame: dict[str, Any], ts: str | None) -> None:
        raw_id = frame.get("call_id")
        raw_id = raw_id if isinstance(raw_id, str) and raw_id else None
        body = frame.get("tool_call") if isinstance(frame.get("tool_call"), dict) else {}
        kind = next((key for key in body if key.endswith("ToolCall")), None)
        call = body.get(kind) if kind else None
        call = call if isinstance(call, dict) else {}
        name = tool_name(kind or "tool")
        args = _clip_args(call.get("args"))
        self._close_segment()
        if frame.get("subtype") == "started":
            if raw_id is None or self.builder.call_id(raw_id) is None:
                self.builder.tool_call(raw_id, name, args, ts=ts, turn_id=self.turn_id)
            return
        if frame.get("subtype") != "completed":
            return
        if raw_id is None or self.builder.call_id(raw_id) is None:
            self.builder.tool_call(raw_id, name, args, ts=ts, turn_id=self.turn_id)
        result = call.get("result")
        result = result if isinstance(result, dict) else {}
        success = result.get("success")
        if isinstance(success, dict):
            exit_code = success.get("exitCode")
            failed = isinstance(exit_code, int) and exit_code != 0
            output = _result_text(success)
            self.builder.tool_result(
                raw_id, ok=not failed, output="" if failed else output,
                error=(output or f"exit status {exit_code}") if failed else None,
                name=name, ts=ts, turn_id=self.turn_id,
            )
            patch = success.get("diffString")
            path = success.get("path") or args.get("path")
            if isinstance(patch, str) and patch and isinstance(path, str):
                self.builder.diff(path, patch, raw_call_id=raw_id, ts=ts, turn_id=self.turn_id)
            return
        reason = "failed"
        for key, value in result.items():
            reason = key
            if isinstance(value, dict):
                detail = value.get("error") or value.get("message") or value.get("reason")
                if isinstance(detail, str) and detail:
                    reason = f"{key}: {detail}"
            elif isinstance(value, str) and value:
                reason = f"{key}: {value}"
            break
        self.builder.tool_result(raw_id, ok=False, error=reason, name=name, ts=ts,
                                 turn_id=self.turn_id)

    def close(self) -> None:
        self._close_segment()


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------


class CursorAdapter(ExternalAdapter):
    """``cursor-agent``: per-message print mode, no permission mechanism."""

    id = "cursor"
    title = "Cursor agent"
    binary = "cursor-agent"
    capabilities = CliCapabilities(
        streaming=True, approvals="degraded", live_view=True, tmux=False, modes=ALL_MODES,
    )
    #: ``-f``/``--force`` run everything without saying "yolo"; ``--approve-mcps``
    #: approves every MCP server; ``--sandbox disabled`` drops the sandbox.
    forbidden_args = ("-f", "--force", "--approve-mcps")
    forbidden_pairs = (("--sandbox", "disabled"),)
    builtin_default_mode = "readonly"
    native_table = NATIVE_MODES
    legacy_native = LEGACY_NATIVE

    LOGIN_HINT = "Cursor agent is not logged in; run `cursor-agent login` in a terminal"

    def __init__(self, cursor_home: str | os.PathLike[str] | None = None,
                 stall_sec: float = STALL_SEC, exit_grace_sec: float = EXIT_GRACE_SEC) -> None:
        self._home = Path(cursor_home) if cursor_home is not None else None
        self.stall_sec = stall_sec
        self.exit_grace_sec = exit_grace_sec
        #: Chats this daemon made with ``create-chat`` that have no file yet.
        self.created_ids: set[str] = set()
        self._models: dict[str, tuple[float, list[str]]] = {}
        self._holders: tuple[float, dict[str, set[int]]] | None = None

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else Path.home() / ".cursor"

    # -- discovery --------------------------------------------------------
    async def discover(self, path_override: str | None = None) -> Discovery:
        found = await super().discover(path_override)
        if not found.installed or found.executable is None or found.problems:
            return found
        problem = version_problem(found.executable.version)
        if problem:
            found.problems.append(problem)
            return found
        logged_in = await self._logged_in(found.executable)
        if logged_in is False:
            found.problems.append(self.LOGIN_HINT)
        code, out = await self._run(found.executable, ["--help"])
        choices = native_modes.help_choices(out, "--mode") if code == 0 else None
        found.native_modes = native_modes.offered(NATIVE_MODES, choices)
        return found

    async def _logged_in(self, executable: PinnedExecutable) -> bool | None:
        """``status --format json`` (local, no tokens); ``None`` when it cannot tell."""
        code, out = await self._run(executable, ["status", "--format", "json"])
        if code is None:
            return None
        try:
            value = json.loads(out)
        except (ValueError, RecursionError):
            return None if code == 0 else False
        if not isinstance(value, dict):
            return None
        # Only the flag is read: the rest holds the account's email.
        flag = value.get("isAuthenticated")
        return flag if isinstance(flag, bool) else None

    async def _run(self, executable: PinnedExecutable, args: list[str]) -> tuple[int | None, str]:
        """A short local command (no model call), outside the supervisor's cap."""
        argv = proc.build_argv(executable, args, forbidden=self.forbidden_args,
                               forbidden_pairs=self.forbidden_pairs)
        tag = f"cursor-{uuid.uuid4().hex}"
        try:
            code, out = await self.helper_supervisor().run_helper(
                argv, cwd=tempfile.gettempdir(), env={**proc.scrubbed_env(), TAG_ENV: tag},
                cli="cursor", timeout=COMMAND_TIMEOUT_SEC,
            )
        finally:
            # cursor-agent leaves node workers behind outside its group: by marker.
            leftovers = await asyncio.to_thread(tagged_pids, {tag})
            if leftovers:
                await kill_pids({pid: proc.process_start_time(pid) for pid in leftovers}, 0.5)
        return code, out.decode("utf-8", "replace")

    async def list_models(self, discovery: Discovery | None) -> list[str]:
        """Model ids from ``--list-models`` (no tokens), cached per executable."""
        if discovery is None or discovery.executable is None:
            return []
        key = discovery.executable.path
        cached = self._models.get(key)
        if cached is not None and time.monotonic() - cached[0] < MODELS_TTL_SEC:
            return list(cached[1])
        code, out = await self._run(discovery.executable, ["--list-models"])
        if code != 0:
            return list(cached[1]) if cached else []
        models = parse_models(out)
        self._models[key] = (time.monotonic(), models)
        return list(models)

    def controlled(self, mode: str, discovery: Discovery | None) -> bool:
        """Only read-only is controlled: cursor cannot ask before it acts."""
        return mode == "readonly"

    def approvals(self, mode: str, discovery: Discovery | None) -> Approvals:
        return "degraded" if mode == "default" else "none"

    # -- stored chats -----------------------------------------------------
    def _chat_dirs(self) -> list[Path]:
        root = self.home / "chats"
        try:
            return [path for path in root.glob("*/*") if path.is_dir()]
        except OSError:
            return []

    def transcript_path(self, chat_id: str, cwd: str | None) -> Path | None:
        projects = self.home / "projects"
        if cwd:
            guess = projects / encoded_workspace(cwd) / "agent-transcripts" / chat_id / (
                f"{chat_id}.jsonl")
            if guess.is_file():
                return guess
        for path in projects.glob(f"*/agent-transcripts/{chat_id}/{chat_id}.jsonl"):
            return path
        return None

    def _info(self, chat_dir: Path, *, preview: bool = True) -> SessionInfo | None:
        meta = _read_json(chat_dir / "meta.json")
        if meta is None or meta.get("isSubagent"):
            return None
        cwd = meta.get("cwd")
        chat_id = chat_dir.name
        if not isinstance(cwd, str) or not cwd or not proc.SESSION_ID_RE.fullmatch(chat_id):
            return None
        transcript = self.transcript_path(chat_id, cwd)
        if transcript is None and not meta.get("hasConversation"):
            return None  # an empty chat (create-chat, or a TUI opened and left)
        updated = meta.get("updatedAtMs") or meta.get("createdAtMs")
        updated_at = iso_from_ms(updated) if updated is not None else None
        first = ""
        if preview and transcript is not None:
            for record in _records(transcript, _PREVIEW_BYTES):
                if record.get("role") == "user":
                    texts = [b.get("text") for b in _text_blocks(record.get("message"))
                             if isinstance(b.get("text"), str)]
                    first = user_text("".join(texts))[0]
                    if first:
                        break
        title = meta.get("title") if isinstance(meta.get("title"), str) else None
        if not title or title == "New Agent":
            title = _first_line(first) or None
        return SessionInfo(
            id=chat_id,
            cwd=cwd,
            title=title,
            updated_at=updated_at,
            preview=first,
            extra={"chat_dir": str(chat_dir),
                   "transcript": str(transcript) if transcript else None,
                   "createdAtMs": meta.get("createdAtMs")},
        )

    async def list_sessions(
        self, roots: Sequence[str], *, limit: int, before: str | None = None
    ) -> list[SessionInfo]:
        return await asyncio.to_thread(self._list, list(roots), limit, before)

    def _list(self, roots: list[str], limit: int, before: str | None) -> list[SessionInfo]:
        bases = [root.rstrip("/") or "/" for root in roots]
        candidates: list[tuple[str, Path]] = []
        for chat_dir in self._chat_dirs():
            meta = _read_json(chat_dir / "meta.json")
            if meta is None or meta.get("isSubagent"):
                continue
            cwd = meta.get("cwd")
            if not isinstance(cwd, str) or not any(
                cwd == base or cwd.startswith(base.rstrip("/") + "/") for base in bases
            ):
                continue
            updated = meta.get("updatedAtMs") or meta.get("createdAtMs")
            candidates.append((iso_from_ms(updated) if updated is not None else "", chat_dir))
        candidates.sort(key=lambda row: row[0], reverse=True)
        rows: list[SessionInfo] = []
        for stamp, chat_dir in candidates:
            if before is not None and stamp > before:
                continue
            info = self._info(chat_dir)
            if info is None:
                continue
            rows.append(info)
            if len(rows) >= limit:
                break
        return rows

    async def get_session(self, session_id: str) -> SessionInfo | None:
        try:
            proc.validate_session_id(session_id)
        except ExternalError:
            return None
        root = self.home / "chats"
        for chat_dir in root.glob(f"*/{session_id}"):
            if chat_dir.is_dir():
                return await asyncio.to_thread(self._info, chat_dir)
        return None

    async def read(self, info: SessionInfo) -> list[TimelineItem]:
        transcript = info.extra.get("transcript") if info.extra else None
        path = Path(transcript) if transcript else self.transcript_path(info.id, info.cwd)
        builder = TimelineBuilder(self.id, info.id)
        if path is None:
            return []
        records = await asyncio.to_thread(_records, path)
        created = info.extra.get("createdAtMs") if info.extra else None
        return convert_transcript(records, builder, info.id,
                                  fallback_ts=iso_from_ms(created) if created else None)

    async def follow(
        self, info: SessionInfo, *, interval: float = 1.0
    ) -> AsyncIterator[list[TimelineItem]]:
        """Read what the transcript gained since the last look (it is append-only).

        Bounded reads from a byte offset (security review L2): only complete new
        lines, at most :data:`_TAIL_BYTES` per look; a file that shrank is read
        again from the start.  Keys come from record positions, so the first
        look (from offset 0) repeats what the open already showed and the
        manager drops those copies.
        """
        offset = 0
        count = 0
        path: Path | None = None
        while True:
            await asyncio.sleep(interval)
            fresh = await self.get_session(info.id) or info
            transcript = fresh.extra.get("transcript") if fresh.extra else None
            current = Path(transcript) if transcript else None
            if current is None:
                continue
            if current != path:
                path, offset, count = current, 0, 0
            try:
                size = current.stat().st_size
            except OSError:
                continue
            if size < offset:
                offset, count = 0, 0
            if size == offset:
                continue
            records, offset = await asyncio.to_thread(_records_tail, current, offset)
            if not records:
                continue
            created = fresh.extra.get("createdAtMs") if fresh.extra else None
            items = convert_transcript(
                records, TimelineBuilder(self.id, fresh.id), fresh.id, start=count,
                fallback_ts=iso_from_ms(created) if created else None,
            )
            count += len(records)
            yield items

    # -- liveness ---------------------------------------------------------
    def _store_db(self, info: SessionInfo) -> str:
        chat_dir = info.extra.get("chat_dir") if info.extra else None
        base = Path(chat_dir) if chat_dir else self.home / "chats" / workspace_hash(
            info.cwd) / info.id
        return str(base / "store.db")

    def _holders_of(self, target: str) -> set[int]:
        if sys.platform.startswith("linux"):
            now = time.monotonic()
            cached = self._holders
            if cached is None or now - cached[0] >= _HOLDERS_TTL_SEC:
                prefix = str(self.home / "chats") + os.sep
                cached = (now, _holders_linux(prefix))
                self._holders = cached
            return set(cached[1].get(target, set()))
        if proc.IS_WINDOWS:  # pragma: no cover - no open-file scan on Windows
            return set()
        return _holders_lsof(target)  # pragma: no cover - macOS

    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        target = self._store_db(info)
        if not os.path.exists(target):
            return Liveness()
        holders = await asyncio.to_thread(self._holders_of, target) - set(own_pids)
        if holders and own_pids:
            for pid in own_pids:
                holders -= set(proc.descendants(pid))
        for pid in holders:
            if pid == os.getpid():
                continue
            if sys.platform.startswith("linux"):
                if "worker-server" in _proc_cmdline(pid):
                    continue  # a helper, not somebody using the chat
                if _proc_has_env(pid, f"\0{TAG_ENV}=cursor-".encode()):
                    continue  # one of snowpea's own runs
            busy = False
            transcript = info.extra.get("transcript") if info.extra else None
            if transcript:
                with contextlib.suppress(OSError):
                    busy = time.time() - Path(transcript).stat().st_mtime < _BUSY_WINDOW_SEC
            return Liveness(host="terminal", busy=busy, pids=(pid,))
        return Liveness()

    # -- running ----------------------------------------------------------
    def channel(self, spec: LaunchSpec, host: ChannelHost) -> ExternalChannel:
        return CursorChannel(spec, host, self)


def convert_transcript(
    records: list[dict[str, Any]], builder: TimelineBuilder, chat_id: str,
    *, fallback_ts: str | None = None, start: int = 0,
) -> list[TimelineItem]:
    """A cursor transcript (already filtered) as timeline items, oldest first.

    Keys come from the record's position (the file is append-only), so a
    re-read or a live tail yields no duplicates; ``start`` is the position of
    ``records[0]`` (a tail read).  Tool calls have no ids and no results in the
    transcript: each call is shown completed with no output.
    """
    ts = fallback_ts
    for index, record in enumerate(records, start):
        role = record.get("role")
        if record.get("type") == "turn_ended":
            if record.get("error") or record.get("status") not in (None, "success"):
                detail = record.get("error")
                message = detail if isinstance(detail, str) else (
                    json.dumps(detail, ensure_ascii=False)[:500] if detail else
                    f"turn ended: {record.get('status')}")
                builder.error(errors.EXTERNAL_UNAVAILABLE, message,
                              key=stable_key(chat_id, index, "error"), ts=ts)
            continue
        blocks = _text_blocks(record.get("message"))
        if role == "user":
            raw = "".join(b.get("text") for b in blocks if isinstance(b.get("text"), str))
            text, when = user_text(raw)
            ts = when or ts
            if text:
                builder.user(text, key=stable_key(chat_id, index, "user"), ts=ts)
            continue
        if role != "assistant":
            continue
        for offset, block in enumerate(blocks):
            kind = block.get("type")
            key = stable_key(chat_id, index, offset)
            if kind == "text" and isinstance(block.get("text"), str):
                builder.assistant(block["text"], key=key, ts=ts)
            elif kind == "tool_use":
                name = str(block.get("name") or "tool")
                raw_args = block.get("input")
                args = raw_args if isinstance(raw_args, dict) else (
                    {"patch": raw_args} if isinstance(raw_args, str) else {})
                call_id = f"t-{key[:20]}"
                builder.tool_call(call_id, name, _clip_args(args), key=f"{key}:call", ts=ts)
                builder.tool_result(call_id, ok=True, name=name, key=f"{key}:result", ts=ts)
                diff = _history_diff(name, args)
                if diff is not None:
                    builder.diff(diff[0], diff[1], raw_call_id=call_id, key=f"{key}:diff",
                                 ts=ts)
    builder.flush()
    return builder.drain()


__all__ = [
    "MAX_PROMPT_BYTES",
    "TAG_ENV",
    "CursorAdapter",
    "CursorChannel",
    "check_prompt_size",
    "convert_transcript",
    "encoded_workspace",
    "parse_local_time",
    "parse_models",
    "parse_version",
    "tagged_pids",
    "user_text",
    "version_problem",
    "workspace_hash",
]
