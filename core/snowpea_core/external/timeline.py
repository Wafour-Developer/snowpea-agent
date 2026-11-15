"""External CLI records -> snowpea timeline items (M19 WP5).

Clients render an external session with the reducers they already use for
``session.event``, so every item here is a session event kind with the payload
:mod:`snowpea_core.session.events` builds (and validates) for it:
``message.user``, ``message.delta``, ``message.reasoning``, ``message.done``,
``tool.call``, ``tool.result``, ``diff``, ``error``, ``turn.started``,
``turn.done``.

Each item carries a stable ``key`` derived from the CLI's own record id (or
from its position in an append-only transcript), so reading the same history
twice, or a live tail overlapping what was already read, yields no duplicate.
The manager assigns the per-session ``seq``.

Privacy: a :class:`RecordFilter` drops whole record kinds *before* anything is
converted, logged or stored — Claude's ``attachment`` and ``session-owner``
records carry the owner's email and organisation ids and must never reach a
client or Jarvis.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from snowpea_core.config.paths import utc_now
from snowpea_core.session import events as ev

#: Longest tool output kept on one timeline item.
MAX_OUTPUT_CHARS = 64_000
#: Call ids clients can use as they are; anything else (cursor's contain a
#: newline) is replaced by a hash.
_SAFE_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

T = TypeVar("T")


@dataclass
class TimelineItem:
    """One session-event-shaped entry; ``seq`` is assigned by the manager."""

    kind: str
    payload: dict[str, Any]
    ts: str
    key: str
    turn_id: str | None = None

    def to_event(self, seq: int) -> dict[str, Any]:
        """The ``ExternalTimelineEvent`` dict for this item."""
        return {
            "seq": seq,
            "kind": self.kind,
            "payload": self.payload,
            "ts": self.ts,
            "turnId": self.turn_id,
        }


def stable_key(*parts: Any) -> str:
    """A short deterministic key from record identity parts."""
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def safe_call_id(raw: str | None, *salt: Any) -> str:
    """``raw`` when it is a plain id, else a stable hash of it."""
    if raw and _SAFE_CALL_ID.fullmatch(raw):
        return raw
    return "c-" + stable_key(raw, *salt)[:20]


def iso_from_ms(value: Any) -> str:
    """UTC ISO time from epoch milliseconds; now when unusable."""
    try:
        return _iso(datetime.fromtimestamp(float(value) / 1000.0, tz=UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        return utc_now()


def iso_from_unix(value: Any) -> str:
    """UTC ISO time from epoch seconds; now when unusable."""
    try:
        return _iso(datetime.fromtimestamp(float(value), tz=UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        return utc_now()


def iso_from_text(value: Any) -> str:
    """Normalise an ISO-ish timestamp string to UTC ISO; now when unparsable."""
    if not isinstance(value, str) or not value:
        return utc_now()
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _iso(parsed.astimezone(UTC))


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


#: Terminal escape sequences (security review M7): OSC (``ESC ]`` … BEL or
#: ``ESC \\``, e.g. OSC 52 clipboard writes, OSC 8 links), DCS/SOS/PM/APC
#: strings, CSI (``ESC [`` … final byte, also the 8-bit ``\x9b``) and any other
#: two-character ``ESC x`` sequence.
_ESCAPE_RE = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
)
#: C0 (except tab and newline), DEL and C1 controls; carriage return goes too
#: (it overwrites a terminal line).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_text(text: str) -> str:
    """``text`` without terminal escape sequences or control characters (keeps \\n, \\t).

    Every string external CLIs give snowpea (transcript text, titles,
    previews, errors) passes through here before it reaches a client: a
    transcript must not be able to drive the person's terminal (OSC 52
    clipboard writes, OSC 8 links, cursor movement).  It is *not* HTML
    escaping: clients that render markup still escape it themselves.
    """
    if not isinstance(text, str) or not text:
        return text
    if "\x1b" not in text and not _CONTROL_RE.search(text):
        return text
    return _CONTROL_RE.sub("", _ESCAPE_RE.sub("", text.replace("\r\n", "\n")))


def clean_payload(value: Any, depth: int = 0) -> Any:
    """:func:`clean_text` applied to every string (and key) of a payload."""
    if isinstance(value, str):
        return clean_text(value)
    if depth > 64:
        return value
    if isinstance(value, dict):
        return {
            (clean_text(k) if isinstance(k, str) else k): clean_payload(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [clean_payload(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(clean_payload(item, depth + 1) for item in value)
    return value


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """``text`` cut to ``limit`` characters with a marker."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters not shown]"


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordFilter:
    """Which record kinds may be converted at all.

    With ``allow`` set, only those kinds pass (deny by default: a record kind
    a new CLI version invents is dropped until someone looks at it).
    ``drop`` removes kinds in every case.  Dropped records are never parsed
    further, logged or counted by content.
    """

    allow: frozenset[str] | None = None
    drop: frozenset[str] = frozenset()

    def keep(self, kind: Any) -> bool:
        if not isinstance(kind, str):
            return False
        if kind in self.drop:
            return False
        return self.allow is None or kind in self.allow

    def apply(self, records: Iterable[T], kind_of: Callable[[T], Any]) -> Iterator[T]:
        for record in records:
            try:
                kind = kind_of(record)
            except Exception:  # noqa: BLE001 - a malformed record is dropped, not logged
                continue
            if self.keep(kind):
                yield record


#: Claude Code transcripts: only ``user`` and ``assistant`` records are ever
#: rendered (``attachment``, ``session-owner``, ``api-request*``, ... hold the
#: system prompt, the owner's email and organisation ids).
USER_ASSISTANT_ONLY = RecordFilter(allow=frozenset({"user", "assistant"}))


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------


@dataclass
class _OpenMessage:
    key: str
    text: list[str] = field(default_factory=list)
    turn_id: str | None = None
    ts: str = ""


class TimelineBuilder:
    """Turns one session's records into timeline items, in order, without duplicates.

    Adapters call the methods below while walking a transcript or a live
    stream, then :meth:`drain` the items.  The same builder serves history and
    live output, so a call opened in one and finished in the other pairs up.
    """

    def __init__(self, cli: str, session_id: str, *, salt: str = "") -> None:
        self.cli = cli
        self.session_id = session_id
        #: Separates generated keys of a live builder from a history read's
        #: (whose keys must be the same on every read, so it has none).
        self.salt = salt
        self._out: list[TimelineItem] = []
        self._seen: set[str] = set()
        self._counter = 0
        self._message: _OpenMessage | None = None
        self._reasoning_chars: dict[str | None, int] = {}
        self._calls: dict[str, str] = {}
        self._call_names: dict[str, str] = {}

    # -- plumbing ---------------------------------------------------------
    def _auto_key(self, kind: str) -> str:
        self._counter += 1
        return stable_key(self.cli, self.session_id, self.salt, kind, self._counter)

    def _push(
        self,
        event: ev.Event,
        *,
        key: str | None,
        ts: str | None,
        turn_id: str | None,
    ) -> TimelineItem | None:
        kind, payload = event
        item_key = key or self._auto_key(kind)
        if item_key in self._seen:
            return None
        self._seen.add(item_key)
        payload = clean_payload(payload)
        item = TimelineItem(kind=kind, payload=payload, ts=ts or utc_now(), key=item_key,
                            turn_id=turn_id)
        self._out.append(item)
        return item

    def seen(self, key: str) -> bool:
        return key in self._seen

    def mark_seen(self, keys: Iterable[str]) -> None:
        """Keys already delivered elsewhere (a history read before a live tail)."""
        self._seen.update(keys)

    def drain(self) -> list[TimelineItem]:
        """Items built since the last drain, oldest first."""
        out, self._out = self._out, []
        return out

    # -- messages ---------------------------------------------------------
    def user(
        self,
        text: str,
        *,
        key: str | None = None,
        ts: str | None = None,
        turn_id: str | None = None,
        origin: dict[str, Any] | None = None,
    ) -> None:
        """A prompt.  ``origin`` defaults to a person typing in the CLI (``human``)."""
        self.flush(turn_id=turn_id)
        self._push(
            ev.message_user(text, origin=origin or {"kind": "human", "surface": self.cli}),
            key=key,
            ts=ts,
            turn_id=turn_id,
        )

    def delta(self, text: str, *, message_key: str, ts: str | None = None,
              turn_id: str | None = None) -> None:
        """Streamed assistant text; consecutive deltas of one message are joined at flush."""
        if not text:
            return
        if self._message is not None and self._message.key != message_key:
            self.flush(turn_id=turn_id)
        if self._message is None:
            if message_key in self._seen:
                return
            self._message = _OpenMessage(key=message_key, turn_id=turn_id, ts=ts or utc_now())
        self._message.text.append(text)
        self._push(
            ev.message_delta(text),
            key=stable_key(message_key, "delta", len(self._message.text)),
            ts=ts,
            turn_id=turn_id,
        )

    def flush(self, *, turn_id: str | None = None, text: str | None = None) -> None:
        """Close the open streamed message with ``message.done`` (``text`` overrides)."""
        message = self._message
        if message is None:
            return
        self._message = None
        full = text if text is not None else "".join(message.text)
        self._push(
            ev.message_done(full),
            key=stable_key(message.key, "done"),
            ts=utc_now(),
            turn_id=message.turn_id or turn_id,
        )

    def assistant(self, text: str, *, key: str | None = None, ts: str | None = None,
                  turn_id: str | None = None) -> None:
        """A whole assistant message (history, or a CLI that does not stream)."""
        if key is not None and self._message is not None and self._message.key == key:
            self.flush(turn_id=turn_id, text=text)
            return
        self.flush(turn_id=turn_id)
        if not text:
            return
        done_key = stable_key(key, "done") if key else None
        if done_key is not None and done_key in self._seen:
            return
        self._push(ev.message_done(text), key=done_key, ts=ts, turn_id=turn_id)

    def reasoning(self, text: str, *, key: str | None = None, ts: str | None = None,
                  turn_id: str | None = None) -> None:
        """Thinking text (may be empty: Claude only sends a signature, shown as a marker)."""
        total = self._reasoning_chars.get(turn_id, 0) + len(text)
        self._reasoning_chars[turn_id] = total
        self._push(ev.message_reasoning(text, total), key=key, ts=ts, turn_id=turn_id)

    def note(self, text: str, *, key: str | None = None, ts: str | None = None,
             turn_id: str | None = None) -> None:
        """A system line in the transcript (e.g. "Denied by the person")."""
        self.flush(turn_id=turn_id)
        self._push(ev.message_done(text, role="system"), key=key, ts=ts, turn_id=turn_id)

    # -- tools --------------------------------------------------------------
    def tool_call(self, raw_id: str | None, name: str, args: dict[str, Any] | None, *,
                  key: str | None = None, ts: str | None = None,
                  turn_id: str | None = None) -> str:
        """Announce a tool call; returns the call id its result and diff must use."""
        self.flush(turn_id=turn_id)
        call_id = safe_call_id(raw_id, self.cli, self.session_id, self._counter)
        if raw_id is not None:
            call_id = self._calls.setdefault(raw_id, call_id)
        self._call_names[call_id] = name
        self._push(
            ev.tool_call(call_id, name, dict(args or {})),
            key=key or stable_key(self.cli, self.session_id, "call", call_id),
            ts=ts,
            turn_id=turn_id,
        )
        return call_id

    def call_id(self, raw_id: str | None) -> str | None:
        """The timeline call id announced for ``raw_id``, if any."""
        if raw_id is None:
            return None
        return self._calls.get(raw_id)

    def tool_result(self, raw_id: str | None, *, ok: bool, output: str = "",
                    error: str | None = None, name: str | None = None,
                    key: str | None = None, ts: str | None = None,
                    turn_id: str | None = None) -> None:
        """A tool's result; a result with no announced call gets one first."""
        call_id = self.call_id(raw_id)
        if call_id is None:
            call_id = self.tool_call(raw_id, name or "tool", {}, ts=ts, turn_id=turn_id)
        tool_name = name or self._call_names.get(call_id, "tool")
        self._push(
            ev.tool_result(call_id, tool_name, ok, clip(output or ""),
                           clip(error) if error else None),
            key=key or stable_key(self.cli, self.session_id, "result", call_id),
            ts=ts,
            turn_id=turn_id,
        )

    def diff(self, path: str, patch: str, *, raw_call_id: str | None = None,
             key: str | None = None, ts: str | None = None,
             turn_id: str | None = None) -> None:
        self._push(
            ev.diff(path, clip(patch), call_id=self.call_id(raw_call_id)),
            key=key or stable_key(self.cli, self.session_id, "diff", raw_call_id, path),
            ts=ts,
            turn_id=turn_id,
        )

    # -- turns and errors ---------------------------------------------------
    def error(self, code: str, message: str, *, key: str | None = None,
              ts: str | None = None, turn_id: str | None = None) -> None:
        self._push(ev.error(code, message), key=key, ts=ts, turn_id=turn_id)

    def turn_started(self, turn_id: str, prompt: str | None = None, *,
                     ts: str | None = None) -> None:
        self._reasoning_chars.pop(turn_id, None)
        self._push(
            ev.turn_started(turn_id, prompt),
            key=stable_key(self.cli, self.session_id, "turn.started", turn_id),
            ts=ts,
            turn_id=turn_id,
        )

    def turn_done(self, turn_id: str, reason: str = "complete", *,
                  ts: str | None = None) -> None:
        """End a turn (``complete``, ``interrupted``, ``error``, ``denied``, ``timeout``)."""
        self.flush(turn_id=turn_id)
        self._reasoning_chars.pop(turn_id, None)
        self._push(
            ev.turn_done(turn_id, reason),
            key=stable_key(self.cli, self.session_id, "turn.done", turn_id),
            ts=ts,
            turn_id=turn_id,
        )


__all__ = [
    "MAX_OUTPUT_CHARS",
    "RecordFilter",
    "TimelineBuilder",
    "TimelineItem",
    "USER_ASSISTANT_ONLY",
    "clip",
    "iso_from_ms",
    "iso_from_text",
    "iso_from_unix",
    "safe_call_id",
    "stable_key",
]
