"""The input gate (M37 §2.4): one queue per terminal, people first.

* **Human input** (a viewer's ``terminal.write`` with ``human: true``) passes
  straight through, updates ``last_human_key_at`` and tracks whether the person
  has an unfinished draft (printable keys since the last Enter or Ctrl-C).
  While an agent's text is going in, the person's keys are held and flushed
  right after it.
* **Agent input** (Jarvis, ``terminal.send``, a first prompt):

  1. startup wait — within 10 s of launch, wait up to 5 s for 800 ms of quiet;
  2. human-quiet wait — until the person has been quiet for 3 s with no
     unfinished draft (a draft is waited on for up to 60 s);
  3. paste — bracketed only when mode 2004 is on; ``ESC[201~`` stripped;
  4. a 200 ms gap;
  5. the submit key, encoded for the keyboard mode (default ``\\r``);
  6. 5 s watching for ``working`` (or a turn advance): ``accepted``.

* A terminal in ``blocked`` refuses agent text unless ``force``.
* :meth:`InputGate.keys` sends named keys 30 ms apart; :meth:`InputGate.interrupt`
  sends Esc to a program that reports status, otherwise Ctrl-C.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

STARTUP_WINDOW = 10.0
STARTUP_MAX_WAIT = 5.0
STARTUP_QUIET = 0.8
HUMAN_QUIET = 3.0
DRAFT_MAX_WAIT = 60.0
PASTE_GAP = 0.2
WATCH = 5.0
KEY_GAP = 0.03
POLL = 0.05

BLOCKED_MESSAGE = "the terminal is waiting for an answer; it is not sent"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


class GateRefused(Exception):
    """Agent input refused (``reason``: ``blocked`` | ``unknown-key`` | ``closed``)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SendResult:
    accepted: bool
    waited: float = 0.0


# -- key names ---------------------------------------------------------------

_PLAIN_KEYS: dict[str, bytes] = {
    "enter": b"\r", "return": b"\r", "tab": b"\t", "esc": b"\x1b", "escape": b"\x1b",
    "backspace": b"\x7f", "space": b" ", "delete": b"\x1b[3~", "insert": b"\x1b[2~",
    "pageup": b"\x1b[5~", "pgup": b"\x1b[5~", "pagedown": b"\x1b[6~", "pgdn": b"\x1b[6~",
    "shift+tab": b"\x1b[Z", "backtab": b"\x1b[Z",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
    "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}
_CURSOR_KEYS: dict[str, str] = {
    "up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F",
}
#: Kitty keyboard protocol: flag 8 reports every key as an escape code.
KITTY_ALL_KEYS = 8
_KITTY_CODES = {"enter": 13, "return": 13, "tab": 9, "esc": 27, "escape": 27, "backspace": 127}


def encode_key(name: str, *, app_cursor: bool = False, kitty_flags: int = 0) -> bytes:
    """Bytes for a named key (``enter``, ``ctrl+c``, ``up``, ``y`` …); ``b""`` if unknown."""
    key = name.strip().lower()
    if not key:
        return b""
    if kitty_flags & KITTY_ALL_KEYS and key in _KITTY_CODES:
        return f"\x1b[{_KITTY_CODES[key]}u".encode()
    if key in _PLAIN_KEYS:
        return _PLAIN_KEYS[key]
    if key in _CURSOR_KEYS:
        return ("\x1bO" if app_cursor else "\x1b[").encode() + _CURSOR_KEYS[key].encode()
    if key.startswith(("ctrl+", "c-")):
        rest = key.split("+", 1)[1] if "+" in key else key[2:]
        if len(rest) == 1 and "a" <= rest <= "z":
            return bytes([ord(rest) - 96])
        return {"[": b"\x1b", "\\": b"\x1c", "]": b"\x1d", "space": b"\x00", "@": b"\x00"}.get(
            rest, b""
        )
    if key.startswith("alt+") and len(name.strip()) == 5:
        return b"\x1b" + name.strip()[4:].encode()
    if len(name) == 1 and name.isprintable():
        return name.encode()
    return b""


class InputGate:
    """Serialises agent input against a person's typing; see the module doc."""

    def __init__(
        self,
        write: Callable[[bytes], Awaitable[None] | None],
        *,
        state: Callable[[], str],
        turn: Callable[[], int],
        last_output_at: Callable[[], float],
        bracketed_paste: Callable[[], bool],
        app_cursor: Callable[[], bool] = lambda: False,
        kitty_flags: Callable[[], int] = lambda: 0,
        reports_status: Callable[[], bool] = lambda: False,
        launched_at: float | None = None,
        submit_key: str = "enter",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._write = write
        self._state = state
        self._turn = turn
        self._last_output_at = last_output_at
        self._bracketed = bracketed_paste
        self._app_cursor = app_cursor
        self._kitty = kitty_flags
        self._reports_status = reports_status
        self._clock = clock
        self._sleep = sleep
        self.launched_at = clock() if launched_at is None else launched_at
        self.submit_key = submit_key
        self.last_human_key_at = float("-inf")
        self.draft = False
        self._agent_lock = asyncio.Lock()
        self._agent_active = False
        self._held = bytearray()
        #: ``on_submit()`` runs when a person presses Enter (the status machine
        #: drops a hook's blocked claim then).
        self.on_human_submit: Callable[[], None] | None = None

    async def _emit(self, data: bytes) -> None:
        result = self._write(data)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result

    # -- people ----------------------------------------------------------------
    async def human(self, data: bytes) -> None:
        if not data:
            return
        self.last_human_key_at = self._clock()
        self._track_draft(data)
        if self._agent_active:
            self._held += data
            return
        await self._emit(data)

    def _track_draft(self, data: bytes) -> None:
        if data[:1] == b"\x1b" and len(data) > 1:
            return  # an escape sequence (arrows, a terminal reply), not typing
        if data[:len(PASTE_START)] == PASTE_START:
            self.draft = True
            return
        for byte in data:
            if byte in (0x0D, 0x0A):
                self.draft = False
                if self.on_human_submit is not None:
                    self.on_human_submit()
            elif byte in (0x03, 0x15):  # Ctrl-C, Ctrl-U
                self.draft = False
            elif byte >= 0x20 and byte != 0x7F:
                self.draft = True

    # -- agents ----------------------------------------------------------------
    async def send(self, text: str, *, force: bool = False, submit: bool = True) -> SendResult:
        if self._state() == "blocked" and not force:
            raise GateRefused("blocked", BLOCKED_MESSAGE)
        start = self._clock()
        async with self._agent_lock:
            await self._startup_wait()
            await self._human_quiet_wait()
            if self._state() == "blocked" and not force:
                raise GateRefused("blocked", BLOCKED_MESSAGE)
            turn = self._turn()
            self._agent_active = True
            try:
                await self._emit(self.paste_bytes(text))
                if submit:
                    await self._sleep(PASTE_GAP)
                    await self._emit(self.submit_bytes())
            finally:
                self._agent_active = False
                held, self._held = bytes(self._held), bytearray()
                if held:
                    await self._emit(held)
            waited = self._clock() - start
            if not submit:
                return SendResult(True, waited)
            accepted = await self._watch(turn)
            return SendResult(accepted, waited)

    def paste_bytes(self, text: str) -> bytes:
        body = text.encode("utf-8").replace(PASTE_END, b"")
        if self._bracketed():
            return PASTE_START + body + PASTE_END
        return body

    def submit_bytes(self) -> bytes:
        return encode_key(self.submit_key, app_cursor=self._app_cursor(),
                          kitty_flags=self._kitty()) or b"\r"

    async def _startup_wait(self) -> None:
        if self._clock() - self.launched_at >= STARTUP_WINDOW:
            return
        deadline = self._clock() + STARTUP_MAX_WAIT
        while self._clock() < deadline:
            if self._clock() - self._last_output_at() >= STARTUP_QUIET:
                return
            await self._sleep(POLL)

    async def _human_quiet_wait(self) -> None:
        deadline = self._clock() + DRAFT_MAX_WAIT
        while self._clock() < deadline:
            quiet = self._clock() - self.last_human_key_at >= HUMAN_QUIET
            if quiet and not self.draft:
                return
            await self._sleep(POLL)

    async def _watch(self, turn: int) -> bool:
        deadline = self._clock() + WATCH
        while True:
            if self._state() == "working" or self._turn() != turn:
                return True
            if self._clock() >= deadline:
                return False
            await self._sleep(POLL)

    async def keys(self, names: list[str]) -> None:
        encoded: list[bytes] = []
        for name in names:
            data = encode_key(name, app_cursor=self._app_cursor(), kitty_flags=self._kitty())
            if not data:
                raise GateRefused("unknown-key", f"unknown key: {name}")
            encoded.append(data)
        async with self._agent_lock:
            for index, data in enumerate(encoded):
                if index:
                    await self._sleep(KEY_GAP)
                await self._emit(data)

    async def interrupt(self) -> None:
        await self._emit(b"\x1b" if self._reports_status() else b"\x03")
