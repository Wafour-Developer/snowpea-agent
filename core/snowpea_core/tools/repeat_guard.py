"""Stop a turn paying twice for the same answer (CORE-repeat-guard).

Three mechanisms live here, all of them keyed on one per-session tracker that
:func:`guard_for` hangs off the :class:`~snowpea_core.session.session.Session`:

1. **Re-read stubs and blocks.**  A ``read_file`` of a window this session has
   already read, whose file has not changed since, comes back as a one-line
   stub instead of the content; after two stubs the call is refused outright.
   Any write to the path (or any change to its bytes) clears the key.

2. **Consecutive and identical-result repeats.**  The third identical call of
   *any* tool in a row gets a warning appended to its result and the fourth is
   refused.  For the scanning tools (``grep``, ``glob``, ``shell``, ``list_dir``
   and MCP tools) a repeat whose *output* matches the previous one is replaced
   by a stub from the second call and refused from the fourth.

3. **Loop suspicion.**  A rolling window of the last twenty calls; once one
   ``(name, arguments)`` pair fills five slots of it the result carries a note
   and the session emits ``loop.suspected`` — once per turn, so a model that
   ignores the note is not shouted at on every call.

Ported in shape from Hermes ``tools/file_tools_read_tracking.py`` (MIT) and
gemini-cli ``loopDetectionService.ts`` (Apache-2.0); see
``docs/design/deviations/CORE-repeat-guard.md``.  No code was vendored.

Turn it off with ``tools.repeatGuard: false``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.tools.file_state import resolve
from snowpea_core.tools.registry import ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.tools.repeat_guard")

#: Error code every refusal carries, so a surface can recognise it.
BLOCKED_CODE = "repeat_blocked"

#: Tools whose repeat is judged by comparing the text they produced.  A write
#: tool is never compared: running it twice is the model's business.
RESULT_TOOLS: frozenset[str] = frozenset({"shell", "grep", "glob", "list_dir"})

#: Tools whose arguments name a path a write invalidates.
WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch"})

#: Identical calls in a row before the result carries a warning, and before the
#: call is refused (Hermes' ``notify_other_tool_call`` resets the count).
CONSECUTIVE_WARN = 3
CONSECUTIVE_BLOCK = 4

#: ``read_file`` stubs served for one key before the call is refused.
MAX_READ_STUBS = 2

#: Identical-result repeats before a stub, and before a refusal.  Counted in
#: calls: the second identical call stubs, the fourth is refused — the same
#: shape as the ``read_file`` rule above, so one mental model covers both.
RESULT_STUB_AT = 1
RESULT_BLOCK_AT = 2

#: Loop detection: calls remembered, and how many of them one hash may fill.
LOOP_WINDOW = 20
LOOP_THRESHOLD = 5

#: Keys remembered per session before the oldest are dropped.
MAX_KEYS = 2048


def _canonical(arguments: dict[str, Any] | None) -> str:
    """The arguments of one call, in a form two identical calls agree on."""
    try:
        return json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - exotic argument types
        return repr(sorted((arguments or {}).items()))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _file_digest(path: str) -> str | None:
    """Hash of the bytes on disk, or ``None`` when they cannot be read.

    A path the daemon cannot stat — a remote backend, a deleted file — simply
    opts out of the read guard rather than guessing that nothing changed.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _lines(text: str) -> int:
    return len(text.splitlines()) if text else 0


@dataclass
class Verdict:
    """Why a call is not being run, and what the model gets instead."""

    text: str
    blocked: bool = False

    def as_result(self) -> ToolResult:
        if self.blocked:
            return ToolResult(ok=False, error=f"{BLOCKED_CODE}: {self.text}")
        return ToolResult(ok=True, output=self.text)


@dataclass
class _ReadEntry:
    """One session's last ``read_file`` of one window of one path."""

    hash: str | None
    lines: int
    stubs: int = 0


@dataclass
class _ResultEntry:
    """The text one scanning call last produced, and how often it repeated."""

    hash: str
    lines: int
    repeats: int = 0


@dataclass
class RepeatGuard:
    """Everything one session remembers about what it has already asked for."""

    session_id: str = "-"
    reads: dict[tuple[str, Any, Any], _ReadEntry] = field(default_factory=dict)
    results: dict[tuple[str, str], _ResultEntry] = field(default_factory=dict)
    last_call: tuple[str, str] | None = None
    consecutive: int = 0
    window: deque[str] = field(default_factory=lambda: deque(maxlen=LOOP_WINDOW))
    #: ``(turn id, call hash)`` pairs already noted, so one loop is reported once.
    noted: set[tuple[str, str]] = field(default_factory=set)

    # -- housekeeping --------------------------------------------------
    @staticmethod
    def _evict(table: dict[Any, Any]) -> None:
        for _ in range(len(table) - MAX_KEYS):
            table.pop(next(iter(table)), None)

    def forget_path(self, path: str) -> None:
        """A write landed: every read window of that path is stale."""
        for key in [key for key in self.reads if key[0] == path]:
            self.reads.pop(key, None)

    # -- consecutive ---------------------------------------------------
    def note_call(self, name: str, args: str) -> int:
        """Record one call; returns how many identical ones ran in a row."""
        signature = (name, args)
        if signature == self.last_call:
            self.consecutive += 1
        else:
            self.last_call = signature
            self.consecutive = 1
        self.window.append(_digest(f"{name}\x00{args}"))
        return self.consecutive

    def loop_count(self, name: str, args: str) -> int:
        """How many of the last :data:`LOOP_WINDOW` calls were this one."""
        return self.window.count(_digest(f"{name}\x00{args}"))


def guard_for(session: Any) -> RepeatGuard:
    """The session's tracker, created on first use.

    Hung off the session as a dynamic attribute rather than declared on it, so
    ``session/session.py`` stays a description of a conversation and this stays
    an optimisation that can be removed without touching it.
    """
    guard = getattr(session, "repeat_guard", None)
    if not isinstance(guard, RepeatGuard):
        guard = RepeatGuard(session_id=str(getattr(session, "id", "") or "-"))
        try:
            session.repeat_guard = guard
        except (AttributeError, TypeError):  # pragma: no cover - frozen session
            pass
    return guard


def enabled(core: Any) -> bool:
    """``tools.repeatGuard`` (default true)."""
    tools = getattr(getattr(core, "settings", None), "tools", None)
    return bool(getattr(tools, "repeatGuard", True))


def _compares_results(core: Any, name: str) -> bool:
    if name in RESULT_TOOLS:
        return True
    try:
        tool = core.tools.get(name)
    except Exception:  # noqa: BLE001 - a registry that cannot answer opts out
        return False
    return bool(tool is not None and str(getattr(tool, "source", "")).startswith("mcp:"))


def _read_key(session: Any, arguments: dict[str, Any]) -> tuple[str, Any, Any] | None:
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return (resolve(session, path), arguments.get("offset"), arguments.get("limit"))


# ---------------------------------------------------------------------------
# the two hooks the agent loop calls
# ---------------------------------------------------------------------------


def check(core: Any, session: Session, name: str, arguments: dict[str, Any]) -> Verdict | None:
    """What to return *instead of* running this call, or ``None`` to run it.

    Called once per dispatched call, before the tool runs, and records the call
    as it goes: a caller that gets a verdict back must not also call
    :func:`record`.
    """
    if not enabled(core):
        return None
    try:
        return _check(core, session, name, arguments)
    except Exception:  # noqa: BLE001 - the guard must never break a call
        log.debug("repeat guard check failed for %s", name, exc_info=True)
        return None


def _check(core: Any, session: Session, name: str, arguments: dict[str, Any]) -> Verdict | None:
    guard = guard_for(session)
    args = _canonical(arguments)

    if name in WRITE_TOOLS:
        path = arguments.get("path")
        if isinstance(path, str) and path.strip():
            guard.forget_path(resolve(session, path))

    repeats = guard.note_call(name, args)

    if name == "read_file":
        verdict = _check_read(session, guard, arguments)
        if verdict is not None:
            return _blocked(guard, verdict)

    if repeats >= CONSECUTIVE_BLOCK:
        return _blocked(
            guard,
            Verdict(
                f"STOP calling {name} with the same arguments — this is the "
                f"{repeats}th identical call in a row and the result has not changed; "
                "continue with the task",
                blocked=True,
            ),
        )

    entry = guard.results.get((name, args))
    if entry is not None and entry.repeats >= RESULT_BLOCK_AT:
        return _blocked(
            guard,
            Verdict(
                f"STOP calling {name} with these arguments — it has returned the same "
                f"{entry.lines} lines every time; continue with the task",
                blocked=True,
            ),
        )
    return None


def _blocked(guard: RepeatGuard, verdict: Verdict) -> Verdict:
    if verdict.blocked:
        log.info("repeat guard blocked a call in %s: %s", guard.session_id, verdict.text)
    return verdict


def _check_read(session: Any, guard: RepeatGuard, arguments: dict[str, Any]) -> Verdict | None:
    key = _read_key(session, arguments)
    if key is None:
        return None
    entry = guard.reads.get(key)
    if entry is None or entry.hash is None:
        return None
    if _file_digest(key[0]) != entry.hash:
        guard.reads.pop(key, None)
        return None
    if entry.stubs >= MAX_READ_STUBS:
        return Verdict(
            f"STOP calling read_file for {arguments.get('path')} — the content from your "
            "earlier result is still current; continue with the task",
            blocked=True,
        )
    entry.stubs += 1
    return Verdict(
        f"unchanged since your earlier read_file of {arguments.get('path')} "
        f"({entry.lines} lines, sha256 {entry.hash[:8]}); the content in that result "
        "is still current"
    )


async def record(
    core: Any, session: Session, name: str, arguments: dict[str, Any], result: ToolResult
) -> ToolResult:
    """Remember what this call produced, and annotate the result if it repeats.

    Called once per call that actually ran, after ``output_spill`` has trimmed
    it, so what is remembered is what the model will see.  Emits
    ``loop.suspected`` when the rolling window says the turn is going in
    circles.  Never raises.
    """
    if not enabled(core):
        return result
    try:
        return await _record(core, session, name, arguments, result)
    except Exception:  # noqa: BLE001 - annotation must never break a call
        log.debug("repeat guard record failed for %s", name, exc_info=True)
        return result


async def _record(
    core: Any, session: Session, name: str, arguments: dict[str, Any], result: ToolResult
) -> ToolResult:
    guard = guard_for(session)
    args = _canonical(arguments)
    notes: list[str] = []

    if name == "read_file" and result.ok:
        key = _read_key(session, arguments)
        if key is not None:
            guard.reads[key] = _ReadEntry(hash=_file_digest(key[0]), lines=_lines(result.output))
            guard._evict(guard.reads)
    elif name in WRITE_TOOLS and result.ok:
        path = arguments.get("path")
        if isinstance(path, str) and path.strip():
            guard.forget_path(resolve(session, path))
    elif result.ok and _compares_results(core, name):
        result = _note_same_result(guard, name, args, result)

    if guard.consecutive == CONSECUTIVE_WARN:
        notes.append(
            f"this is the {CONSECUTIVE_WARN}rd identical {name} call in a row; "
            "the result has not changed"
        )

    count = guard.loop_count(name, args)
    if count >= LOOP_THRESHOLD:
        turn = str(getattr(session, "current_turn", "") or "")
        marker = (turn, _digest(f"{name}\x00{args}"))
        if marker not in guard.noted:
            guard.noted.add(marker)
            notes.append(
                f"loop suspected: {name} with the same arguments has run {count} times "
                "this turn; change approach or finish with what you have"
            )
            await _emit_loop(core, session, name, count)

    if notes:
        result = _append(result, notes)
    return result


def _note_same_result(
    guard: RepeatGuard, name: str, args: str, result: ToolResult
) -> ToolResult:
    """Replace a repeat whose output matched the previous one with a stub."""
    key = (name, args)
    digest = _digest(result.output or "")
    entry = guard.results.get(key)
    if entry is None or entry.hash != digest:
        guard.results[key] = _ResultEntry(hash=digest, lines=_lines(result.output))
        guard._evict(guard.results)
        return result
    entry.repeats += 1
    if entry.repeats < RESULT_STUB_AT:
        return result
    return replace(
        result,
        output=f"same result as your earlier {name} call ({entry.lines} lines)",
        diff=None,
    )


def _append(result: ToolResult, notes: list[str]) -> ToolResult:
    """Put the guard's notes where the model reads the tool's answer."""
    suffix = "\n".join(notes)
    if result.ok:
        text = f"{result.output}\n\n{suffix}" if result.output else suffix
        return replace(result, output=text)
    error = f"{result.error}\n\n{suffix}" if result.error else suffix
    return replace(result, error=error)


async def _emit_loop(core: Any, session: Session, name: str, count: int) -> None:
    from snowpea_core.session import events

    hub = getattr(core, "hub", None)
    if hub is None:  # pragma: no cover - a core without a hub (tests)
        return
    log.info("loop suspected in %s: %s ran %d times", session.id, name, count)
    await hub.emit_event(session.id, events.loop_suspected(name, count))


__all__ = [
    "BLOCKED_CODE",
    "CONSECUTIVE_BLOCK",
    "CONSECUTIVE_WARN",
    "LOOP_THRESHOLD",
    "LOOP_WINDOW",
    "MAX_READ_STUBS",
    "RESULT_TOOLS",
    "RepeatGuard",
    "Verdict",
    "check",
    "enabled",
    "guard_for",
    "record",
]


def forget_pruned(session: Any, history: list[Any], tool_call_ids: list[str]) -> None:
    """Drop what the guard remembers about calls whose results were pruned.

    ``prune_old_tool_outputs`` stubs results older than the kept window in the
    outgoing request; from then on the model has only "re-run the tool if you
    need it again", so the guard must not answer that re-run with "the content
    in your earlier result is still current" — it no longer is, for the model.
    """
    wanted = set(tool_call_ids)
    if not wanted:
        return
    guard = guard_for(session)
    for message in history:
        calls = getattr(message, "tool_calls", None) or []
        for call in calls:
            if getattr(call, "id", None) not in wanted:
                continue
            name = str(getattr(call, "name", "") or "")
            arguments = dict(getattr(call, "arguments", None) or {})
            if name == "read_file":
                key = _read_key(session, arguments)
                if key is not None:
                    guard.reads.pop(key, None)
            else:
                guard.results.pop((name, _canonical(arguments)), None)

