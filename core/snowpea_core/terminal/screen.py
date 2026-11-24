"""The core's copy of a terminal's screen (M37 §2.2), on top of pyte.

Every output byte goes through :meth:`Screen.feed`: pyte keeps the cells, our
own history (10 000 lines, none while the alternate screen is up) and the
alternate screen (pyte has none: 1049 / 1047 / 47 are handled here), and
:class:`~.osc.EscapeScanner` reports OSC 7501 records, titles and queries.

* :meth:`Screen.repaint` — a full ANSI repaint for a new viewer: RIS, the
  scrollback as SGR text, the screen, the alternate screen on top when it is
  up, the modes viewers need, the cursor and the title.
* :meth:`Screen.text` — plain text (screen plus N history lines) for Jarvis and
  the screen rules.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

import pyte
from pyte import graphics as g
from pyte import modes as mo
from pyte.screens import Char, Margins, StaticDefaultDict

from snowpea_core.terminal.osc import EscapeScanner, Event, TitleChange

HISTORY_LINES = 10_000
HEADLESS_COLS = 160
HEADLESS_ROWS = 48

_ALT_MODES = (1049 << 5, 1047 << 5, 47 << 5)
#: Private modes a viewer must know about, in the order a repaint sets them.
REPAINT_MODES: tuple[int, ...] = (1, 9, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 2004)
MOUSE_MODES: frozenset[int] = frozenset({9, 1000, 1002, 1003, 1005, 1006, 1015})

_FG_CODE: dict[str, int] = {name: code for code, name in g.FG_ANSI.items() if name != "default"}
_FG_CODE.update({name: code for code, name in g.FG_AIXTERM.items()})
_BG_CODE: dict[str, int] = {name: code for code, name in g.BG_ANSI.items() if name != "default"}
_BG_CODE.update({name: code for code, name in g.BG_AIXTERM.items()})
_BG_CODE["brightmagenta"] = 105  # pyte spells it "bfightmagenta"


class _PyteScreen(pyte.Screen):
    """pyte's screen plus a history deque and the alternate screen."""

    def __init__(self, columns: int, lines: int, history: int) -> None:
        self.history: deque[Any] = deque(maxlen=history)
        self.alt = False
        self._primary: Any = None
        self._primary_cursor: Any = None
        super().__init__(columns, lines)

    def _fresh_buffer(self) -> Any:
        return defaultdict(lambda: StaticDefaultDict[int, Char](self.default_char))

    def index(self) -> None:
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and not self.alt:
            self.history.append(self.buffer[top])
        super().index()

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        """Like xterm: shrinking keeps the cursor row and pushes the top rows to history.

        (pyte drops rows from the top whatever the cursor; a 160x48 headless
        screen shrunk to a viewer's 30 rows would lose a short program's output.)
        """
        lines = lines or self.lines
        columns = columns or self.columns
        if lines == self.lines and columns == self.columns:
            return
        self.dirty.update(range(lines))
        if lines < self.lines:
            drop = max(0, self.cursor.y - (lines - 1))
            old = self.buffer
            if drop and not self.alt:
                for y in range(drop):
                    self.history.append(old[y])
            fresh = self._fresh_buffer()
            for y in list(old.keys()):
                if drop <= y < drop + lines:
                    fresh[y - drop] = old[y]
            self.buffer = fresh
            self.cursor.y -= drop
        if columns < self.columns:
            for line in self.buffer.values():
                for x in range(columns, self.columns):
                    line.pop(x, None)
        if columns > self.columns:
            self.tabstops |= set(range(8 * (self.columns // 8 + 1), columns, 8))
        self.lines, self.columns = lines, columns
        self.cursor.y = min(self.cursor.y, lines - 1)
        self.cursor.x = min(self.cursor.x, columns - 1)
        self.set_margins()

    def erase_in_display(self, how: int = 0, *args: Any, **kwargs: Any) -> None:
        super().erase_in_display(how, *args, **kwargs)
        if how == 3:
            self.history.clear()

    def reset(self) -> None:
        super().reset()
        if getattr(self, "alt", False):
            self.alt = False
            self._primary = None
        if hasattr(self, "history"):
            self.history.clear()

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        super().set_mode(*modes, **kwargs)
        if kwargs.get("private") and not self.alt:
            for mode in modes:
                if mode in (1049, 1047, 47):
                    self._enter_alt(save_cursor=mode == 1049)
                    break

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private") and self.alt:
            for mode in modes:
                if mode in (1049, 1047, 47):
                    self._leave_alt(restore_cursor=mode == 1049)
                    break
        self.mode.difference_update(_ALT_MODES)
        if self.alt:
            self.mode.add(1049 << 5)

    def _enter_alt(self, *, save_cursor: bool) -> None:
        self._primary = self.buffer
        cursor = self.cursor
        self._primary_cursor = (cursor.x, cursor.y, cursor.attrs) if save_cursor else None
        self.buffer = self._fresh_buffer()
        self.alt = True
        self.dirty.update(range(self.lines))

    def _leave_alt(self, *, restore_cursor: bool) -> None:
        if self._primary is not None:
            self.buffer = self._primary
        self._primary = None
        self.alt = False
        saved = self._primary_cursor
        if restore_cursor and saved is not None:
            self.cursor.x = min(saved[0], self.columns - 1)
            self.cursor.y = min(saved[1], self.lines - 1)
            self.cursor.attrs = saved[2]
        self._primary_cursor = None
        self.dirty.update(range(self.lines))


class Screen:
    """One terminal's parsed screen; feed every output byte in order."""

    def __init__(self, cols: int = HEADLESS_COLS, rows: int = HEADLESS_ROWS,
                 history: int = HISTORY_LINES) -> None:
        self._screen = _PyteScreen(cols, rows, history)
        self._stream = pyte.ByteStream(self._screen)
        self.scanner = EscapeScanner()
        self.title = ""

    # -- input ----------------------------------------------------------------
    def feed(self, data: bytes) -> list[Event]:
        events = self.scanner.feed(data)
        self._stream.feed(data)
        for event in events:
            if isinstance(event, TitleChange):
                self.title = event.title
        return events

    def resize(self, cols: int, rows: int) -> None:
        if (cols, rows) != (self.cols, self.rows):
            self._screen.resize(lines=rows, columns=cols)

    # -- state ----------------------------------------------------------------
    @property
    def cols(self) -> int:
        return self._screen.columns

    @property
    def rows(self) -> int:
        return self._screen.lines

    @property
    def cursor(self) -> tuple[int, int]:
        """``(row, col)``, zero-based."""
        return self._screen.cursor.y, self._screen.cursor.x

    def mode(self, number: int) -> bool:
        """A private (DEC) mode, e.g. ``2004``."""
        return (number << 5) in self._screen.mode

    @property
    def bracketed_paste(self) -> bool:
        return self.mode(2004)

    @property
    def app_cursor(self) -> bool:
        return self.mode(1)

    @property
    def cursor_visible(self) -> bool:
        return not self._screen.cursor.hidden

    @property
    def alt_screen(self) -> bool:
        return self._screen.alt

    @property
    def mouse(self) -> bool:
        return any(self.mode(m) for m in MOUSE_MODES)

    @property
    def kitty_flags(self) -> int:
        return self.scanner.kitty_keyboard

    @property
    def history_size(self) -> int:
        return len(self._screen.history)

    def cell(self, row: int, col: int) -> Char:
        return self._screen.buffer[row][col]

    # -- output ---------------------------------------------------------------
    def text(self, lines: int = 0) -> str:
        """Plain text: the last ``lines`` history lines, then the screen; no ANSI."""
        screen = self._screen
        rows: list[str] = []
        if lines > 0 and not screen.alt:
            history = list(screen.history)[-lines:]
            rows.extend(_plain(line, screen.columns) for line in history)
        rows.extend(_plain(screen.buffer[y], screen.columns) for y in range(screen.lines))
        while rows and not rows[-1]:
            rows.pop()
        return "\n".join(rows)

    def repaint(self, history: int | None = None) -> bytes:
        """A byte stream that rebuilds this screen in a fresh terminal of the same size."""
        screen = self._screen
        cols, lines = screen.columns, screen.lines
        out: list[str] = ["\x1bc"]  # RIS
        primary = screen._primary if screen.alt else screen.buffer
        backlog = list(screen.history)
        if history is not None:
            backlog = backlog[-history:] if history > 0 else []
        rows = [_sgr_line(line, cols) for line in backlog]
        rows.extend(_sgr_line(primary[y], cols) for y in range(lines))
        out.append("\r\n".join(rows))
        if screen.alt:
            saved = screen._primary_cursor
            if saved is not None:
                out.append(f"\x1b[{saved[1] + 1};{saved[0] + 1}H")
            out.append("\x1b[?1049h")
            for y in range(lines):
                text = _sgr_line(screen.buffer[y], cols)
                if text:
                    out.append(f"\x1b[{y + 1};1H{text}")
        if screen.margins is not None:
            out.append(f"\x1b[{screen.margins.top + 1};{screen.margins.bottom + 1}r")
        for number in REPAINT_MODES:
            if self.mode(number):
                out.append(f"\x1b[?{number}h")
        if mo.DECAWM not in screen.mode:
            out.append("\x1b[?7l")
        if mo.IRM in screen.mode:
            out.append("\x1b[4h")
        if self.kitty_flags:
            out.append(f"\x1b[>{self.kitty_flags}u")
        if self.title:
            out.append(f"\x1b]2;{_safe_title(self.title)}\x07")
        out.append(f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}H")
        out.append(_sgr(screen.cursor.attrs) or "\x1b[0m")
        if screen.cursor.hidden:
            out.append("\x1b[?25l")
        return "".join(out).encode("utf-8")


def _plain(line: Any, columns: int) -> str:
    chars: list[str] = []
    for x in range(columns):
        data = line[x].data
        if data:
            chars.append(data)
    return "".join(chars).rstrip()


def _is_blank(char: Char) -> bool:
    return (char.data == " " and char.fg == "default" and char.bg == "default"
            and not (char.reverse or char.underscore or char.strikethrough))


def _sgr_line(line: Any, columns: int) -> str:
    last = -1
    for x in range(columns - 1, -1, -1):
        if not _is_blank(line[x]):
            last = x
            break
    parts: list[str] = []
    pen = ""
    for x in range(last + 1):
        char = line[x]
        if char.data == "":
            continue  # the second cell of a wide character
        code = _sgr(char)
        if code != pen:
            parts.append(code or "\x1b[0m")
            pen = code
        parts.append(char.data)
    if pen:
        parts.append("\x1b[0m")
    return "".join(parts)


def _sgr(char: Char) -> str:
    """The SGR that sets ``char``'s attributes from a reset; ``""`` for the default."""
    codes: list[str] = []
    for flag, code in (("bold", "1"), ("italics", "3"), ("underscore", "4"), ("blink", "5"),
                       ("reverse", "7"), ("strikethrough", "9")):
        if getattr(char, flag):
            codes.append(code)
    codes.extend(_color(char.fg, _FG_CODE, "38"))
    codes.extend(_color(char.bg, _BG_CODE, "48"))
    if not codes:
        return ""
    return "\x1b[0;" + ";".join(codes) + "m"


def _color(value: str, names: dict[str, int], extended: str) -> list[str]:
    if not value or value == "default":
        return []
    if value in names:
        return [str(names[value])]
    if len(value) == 6:
        try:
            r, gr, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
        except ValueError:
            return []
        return [extended, "2", str(r), str(gr), str(b)]
    return []


def _safe_title(title: str) -> str:
    return "".join(ch for ch in title if ch >= " " and ch != "\x7f")[:512]
