"""Escape-sequence scanner for terminal output: OSC 7501, titles and queries (M37 §2.2–§2.3).

:class:`EscapeScanner` reads the raw PTY byte stream next to the pyte screen and
reports what the core acts on:

* :class:`StatusRecord` — ``ESC ] 7501 ; key=val(:key=val)* (ST|BEL)``, the
  Program Status Protocol.  Keys ``state kind progress id app title msg``;
  ``title`` and ``msg`` are base64.  Unknown states are dropped.
* :class:`TitleChange` — OSC 0 / OSC 2.
* :class:`Query` — what a terminal must answer: DA1, DA2, DSR 5/6, OSC 10/11
  colour queries, the kitty keyboard query ``CSI ? u`` and the 7501 query
  ``ESC ] 7501 ; ? ST``.  :func:`answer` builds the reply the core sends when no
  human viewer is attached.

It also keeps the kitty keyboard flags (``CSI > n u`` push, ``CSI < n u`` pop,
``CSI = n ; m u`` set) so the input gate encodes the submit key correctly.

Every sequence may arrive split across chunks: the scanner keeps its state
between :meth:`EscapeScanner.feed` calls.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

ESC = 0x1B
BEL = 0x07
CAN = 0x18
SUB = 0x1A

#: An OSC longer than this is dropped (a title or a 7501 record is tiny).
MAX_OSC = 64 * 1024
#: A CSI longer than this is dropped.
MAX_CSI = 64

STATUS_STATES = frozenset({"idle", "working", "done", "blocked", "error", "clear"})
STATUS_KINDS = frozenset({"permission", "question", "auth"})

_GROUND, _ESC, _CSI, _OSC, _OSC_ESC, _STR, _STR_ESC = range(7)
_OSC_END = re.compile(rb"[\x07\x1b\x18\x1a]")
_STR_END = re.compile(rb"[\x1b\x18\x1a]")


@dataclass(frozen=True)
class StatusRecord:
    state: str
    kind: str | None = None
    progress: int | None = None
    id: str = ""
    app: str | None = None
    title: str | None = None
    msg: str | None = None

    @property
    def root(self) -> bool:
        return self.id == ""


@dataclass(frozen=True)
class TitleChange:
    title: str


@dataclass(frozen=True)
class Query:
    """``kind``: ``da1 da2 dsr-status dsr-cursor color-fg color-bg kitty-keys status-query``."""

    kind: str
    #: The terminator the query used (BEL or ST), so an OSC reply matches it.
    terminator: bytes = b"\x1b\\"


Event = StatusRecord | TitleChange | Query


class EscapeScanner:
    """Incremental; feed every output chunk in order."""

    def __init__(self) -> None:
        self._state = _GROUND
        self._buf = bytearray()
        self._overflow = False
        self.kitty_flags: list[int] = []

    @property
    def kitty_keyboard(self) -> int:
        return self.kitty_flags[-1] if self.kitty_flags else 0

    def feed(self, data: bytes) -> list[Event]:
        events: list[Event] = []
        i, n = 0, len(data)
        while i < n:
            state = self._state
            if state == _GROUND:
                j = data.find(b"\x1b", i)
                if j < 0:
                    return events
                self._state = _ESC
                i = j + 1
                continue
            byte = data[i]
            if state == _ESC:
                i += 1
                if byte == 0x5D:  # ]
                    self._begin(_OSC)
                elif byte == 0x5B:  # [
                    self._begin(_CSI)
                elif byte in (0x50, 0x58, 0x5E, 0x5F):  # DCS SOS PM APC
                    self._begin(_STR)
                elif byte == ESC:
                    self._state = _ESC
                else:
                    self._state = _GROUND
            elif state == _CSI:
                i += 1
                if 0x40 <= byte <= 0x7E:
                    if not self._overflow:
                        self._csi(bytes(self._buf), byte, events)
                    self._state = _GROUND
                elif byte == ESC:
                    self._state = _ESC
                elif byte in (CAN, SUB):
                    self._state = _GROUND
                elif len(self._buf) < MAX_CSI:
                    self._buf.append(byte)
                else:
                    self._overflow = True
            elif state == _OSC:
                match = _OSC_END.search(data, i)
                end = match.start() if match else n
                self._take(data[i:end], MAX_OSC)
                if match is None:
                    return events
                i = end + 1
                stop = data[end]
                if stop == BEL:
                    self._osc(b"\x07", events)
                    self._state = _GROUND
                elif stop == ESC:
                    self._state = _OSC_ESC
                else:
                    self._state = _GROUND
            elif state == _OSC_ESC:
                if byte == 0x5C:  # ST
                    i += 1
                    self._osc(b"\x1b\\", events)
                    self._state = _GROUND
                else:
                    self._state = _ESC  # an aborted OSC; this ESC starts a new sequence
            elif state == _STR:
                match = _STR_END.search(data, i)
                if match is None:
                    return events
                i = match.end()
                self._state = _STR_ESC if data[match.start()] == ESC else _GROUND
            elif state == _STR_ESC:
                if byte == 0x5C:
                    i += 1
                    self._state = _GROUND
                else:
                    self._state = _ESC
        return events

    # -- helpers ---------------------------------------------------------
    def _begin(self, state: int) -> None:
        self._state = state
        self._buf.clear()
        self._overflow = False

    def _take(self, chunk: bytes, limit: int) -> None:
        if self._overflow:
            return
        if len(self._buf) + len(chunk) > limit:
            self._overflow = True
            self._buf.clear()
            return
        self._buf += chunk

    def _csi(self, params: bytes, final: int, events: list[Event]) -> None:
        if final == 0x63:  # c
            if params in (b"", b"0"):
                events.append(Query("da1"))
            elif params in (b">", b">0"):
                events.append(Query("da2"))
        elif final == 0x6E:  # n
            if params == b"6":
                events.append(Query("dsr-cursor"))
            elif params == b"5":
                events.append(Query("dsr-status"))
        elif final == 0x75:  # u — kitty keyboard protocol
            if params == b"?":
                events.append(Query("kitty-keys"))
            elif params[:1] == b">":
                self.kitty_flags.append(_int(params[1:], 0))
                del self.kitty_flags[:-16]
            elif params[:1] == b"<":
                count = max(1, _int(params[1:], 1))
                del self.kitty_flags[max(0, len(self.kitty_flags) - count):]
            elif params[:1] == b"=":
                parts = params[1:].split(b";")
                flags = _int(parts[0], 0)
                mode = _int(parts[1], 1) if len(parts) > 1 else 1
                current = self.kitty_keyboard
                value = flags if mode == 1 else (current | flags if mode == 2 else current & ~flags)
                if self.kitty_flags:
                    self.kitty_flags[-1] = value
                else:
                    self.kitty_flags.append(value)

    def _osc(self, terminator: bytes, events: list[Event]) -> None:
        if self._overflow:
            return
        body = bytes(self._buf)
        code, _, rest = body.partition(b";")
        if code in (b"0", b"2"):
            events.append(TitleChange(rest.decode("utf-8", "replace")))
        elif code in (b"10", b"11") and rest == b"?":
            events.append(Query("color-fg" if code == b"10" else "color-bg", terminator))
        elif code == b"7501":
            if rest == b"?":
                events.append(Query("status-query", terminator))
            else:
                record = parse_status(rest.decode("utf-8", "replace"))
                if record is not None:
                    events.append(record)


def parse_status(text: str) -> StatusRecord | None:
    """``key=val(:key=val)*`` -> a record, or ``None`` (no or unknown state)."""
    fields: dict[str, str] = {}
    for part in text.split(":"):
        key, sep, value = part.partition("=")
        if sep:
            fields[key.strip()] = value
    state = fields.get("state", "")
    if state not in STATUS_STATES:
        return None
    kind = fields.get("kind")
    progress: int | None = None
    if fields.get("progress", "").strip().isdigit():
        progress = max(0, min(100, int(fields["progress"])))
    return StatusRecord(
        state=state,
        kind=kind if kind in STATUS_KINDS else None,
        progress=progress,
        id=fields.get("id", ""),
        app=fields.get("app") or None,
        title=_b64(fields.get("title")),
        msg=_b64(fields.get("msg")),
    )


def encode_status(state: str, **fields: str | int | None) -> bytes:
    """Build a 7501 record (tests, and the snowpea TUI's reference encoder)."""
    parts = [f"state={state}"]
    for key, value in fields.items():
        if value is None:
            continue
        if key in ("title", "msg"):
            value = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
        parts.append(f"{key}={value}")
    return ("\x1b]7501;" + ":".join(parts) + "\x1b\\").encode("utf-8")


def answer(query: Query, *, cursor: tuple[int, int] = (0, 0), kitty_flags: int = 0) -> bytes:
    """The reply a terminal gives; ``cursor`` is ``(row, col)``, zero-based."""
    kind = query.kind
    if kind == "da1":
        return b"\x1b[?1;2c"  # what xterm.js answers: VT100 with advanced video
    if kind == "da2":
        return b"\x1b[>0;276;0c"
    if kind == "dsr-status":
        return b"\x1b[0n"
    if kind == "dsr-cursor":
        return f"\x1b[{cursor[0] + 1};{cursor[1] + 1}R".encode("ascii")
    if kind == "color-fg":
        return b"\x1b]10;rgb:ffff/ffff/ffff" + query.terminator
    if kind == "color-bg":
        return b"\x1b]11;rgb:0000/0000/0000" + query.terminator
    if kind == "kitty-keys":
        return f"\x1b[?{kitty_flags}u".encode("ascii")
    if kind == "status-query":
        return b"\x1b]7501;?" + query.terminator
    return b""


def _int(raw: bytes, default: int) -> int:
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _b64(value: str | None) -> str | None:
    if not value:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None
    return raw.decode("utf-8", "replace")
