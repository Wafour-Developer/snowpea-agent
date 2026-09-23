"""Rendering and key handling for the setup screens.

Single-select screens draw ``(●)``/``(○)`` rows and move with ↑↓/Enter;
multi-select screens draw ``[✓]``/``[ ]`` and toggle with Space.  Tags print as
``[free · no key]`` and ``[active]``, the default row carries ``★``, and the
last row is ``Done — keep X`` (what is selected) or ``Skip — decide later``.

When stdin is not a TTY — CI, a pipe, ``snowpea setup < /dev/null`` — nothing
is drawn and :func:`ask` returns the screen's defaults straight away.  The
wizard must never hang waiting for a keystroke that cannot arrive.
"""

from __future__ import annotations

import io
import os
import sys
from typing import IO

from rich.console import Console
from rich.text import Text

from snowpea_core.setup.screens import SKIP, Screen, ScreenItem, last_row_label

STAR = "★"

#: Terminal control codes used by the in-place repaint.  ``ask`` hides the
#: cursor for as long as a menu is open and always puts it back, including on
#: Ctrl+C, so a wizard that is interrupted never leaves an invisible cursor.
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
CURSOR_UP = "\x1b[{n}A"
CLEAR_LINE = "\x1b[2K"


def _emit(console: Console, code: str) -> None:
    """Write a raw control code to the console's stream, if it has one."""
    stream = getattr(console, "file", None)
    if stream is None:
        return
    try:
        stream.write(code)
        stream.flush()
    except (OSError, ValueError):  # pragma: no cover - closed/odd stream
        pass


def is_interactive(stream: IO[str] | None = None) -> bool:
    """True only when we can actually read keystrokes from a terminal."""
    if os.environ.get("SNOWPEA_SETUP_NONINTERACTIVE"):
        return False
    stream = stream or sys.stdin
    try:
        return bool(stream.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


#: Hidden tag a screen adds to rows that are already set up; drawn as ``(●)``.
CONFIGURED = "configured"


def choice_hint(*, multi: bool) -> str:
    """The one-line footer, naming exactly the keys this screen takes.

    It is the same sentence the TUI's :file:`ChoiceList` prints (M15b §2), so
    a person who learned the wizard can drive the TUI and the other way round.
    """
    return " · ".join(
        part
        for part in (
            "↑↓ move",
            "Space toggle" if multi else None,
            "Enter confirm",
            "1-9 toggle" if multi else "1-9 pick",
            "Esc cancel",
        )
        if part
    )


def render_item(
    item: ScreenItem,
    *,
    multi: bool,
    selected: bool,
    cursor: bool,
    number: int | None = None,
) -> Text:
    """One row, as rich markup.

    ``number`` prefixes the row with ``N.`` so the digit keys are visible
    rather than folklore; rows past the ninth get the same indent and no
    number, because ``1-9`` is what the key map promises.
    """
    if item.id == SKIP or item.id.startswith("action:"):
        marker = "   "
    elif multi:
        marker = "[✓]" if selected else "[ ]"
    else:
        # A configured vendor reads as filled even before it is chosen: the
        # user asked for "active" rows to show ● so the state is visible at a
        # glance; the cursor ❯ still marks the row Enter will pick.
        marker = "(●)" if selected or CONFIGURED in item.tags else "(○)"
    line = Text()
    line.append("❯ " if cursor else "  ", style="bold cyan" if cursor else "")
    if number is not None:
        line.append(f"{number}. " if 1 <= number <= 9 else "   ", style="dim")
    line.append(marker + " ")
    line.append(item.label, style="bold" if cursor else "")
    if item.default:
        line.append(f" {STAR}", style="yellow")
    for tag in item.tags:
        if tag == CONFIGURED:
            continue  # drawn as the filled circle, not as a bracket tag
        style = {"active": "green", "default": "yellow"}.get(tag, "dim")
        line.append(f"  [{tag}]", style=style)
    return line


def screen_lines(screen: Screen, *, cursor: int, chosen: set[str]) -> list[Text]:
    """Every row the screen occupies, one :class:`Text` per terminal line.

    The count is what makes the in-place repaint possible: a screen always
    takes the same number of lines, so moving the cursor up by ``len(...)``
    lands exactly on the title again.
    """
    lines = [Text(screen.title, style="bold")]
    if screen.help:
        lines.extend(Text(line, style="dim") for line in screen.help.split("\n"))
    lines.append(Text(""))
    lines.extend(
        render_item(
            item if item.id != SKIP else item._replace(label=last_row_label(screen, chosen)),
            multi=screen.multi,
            selected=item.id in chosen,
            cursor=index == cursor,
            number=index + 1,
        )
        for index, item in enumerate(screen.items)
    )
    lines.append(Text(choice_hint(multi=screen.multi), style="dim"))
    lines.append(Text(""))
    return lines


def render(
    screen: Screen,
    *,
    cursor: int,
    chosen: set[str],
    console: Console,
    repaint: int = 0,
) -> int:
    """Draw the screen and return how many lines it took.

    ``repaint`` is the line count of the previous draw: the cursor is moved
    back up that many lines and every row is rewritten in place (each one
    erased first with ``\x1b[2K``).  The screen is **never** cleared — doing
    that on each keypress is what made every list in the wizard flicker while
    the user held ↑ or ↓.
    """
    lines = screen_lines(screen, cursor=cursor, chosen=chosen)
    if repaint > 0:
        _emit(console, CURSOR_UP.format(n=repaint))
    for line in lines:
        if repaint > 0:
            _emit(console, CLEAR_LINE)
        console.print(line, no_wrap=True, overflow="ellipsis", crop=True)
    return len(lines)


def render_lines(screen: Screen) -> list[str]:
    """The screen as plain text — used by tests and by ``--json``-less logs."""
    console = Console(file=io.StringIO(), width=100, record=True, force_terminal=False)
    chosen = {item.id for item in screen.items if item.selected}
    render(screen, cursor=0, chosen=chosen, console=console)
    return console.export_text().splitlines()


# ---------------------------------------------------------------------------
# key reading
# ---------------------------------------------------------------------------


def read_key(stream: IO[str] | None = None) -> str:
    """Block for one keypress: ``up``/``down``/``enter``/``space``/``quit``/char."""
    import termios
    import tty

    stream = stream or sys.stdin
    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = stream.read(1)
        if char == "\x1b":
            rest = stream.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(rest, "escape")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if char in ("\r", "\n"):
        return "enter"
    if char == " ":
        return "space"
    if char in ("\x03", "\x04"):
        # Ctrl+C / Ctrl+D leave the wizard, not just this screen: raw mode
        # swallows the terminal's own SIGINT, so the interrupt is ours to raise.
        raise KeyboardInterrupt
    if char == "q":
        return "quit"
    return char


def _digit(key: str) -> int | None:
    """``"3"`` -> row 2; anything else -> ``None``."""
    return int(key) - 1 if len(key) == 1 and key in "123456789" else None


# ---------------------------------------------------------------------------
# the prompt loop
# ---------------------------------------------------------------------------


def ask(
    screen: Screen,
    *,
    console: Console | None = None,
    interactive: bool | None = None,
    keys: IO[str] | None = None,
) -> str | set[str]:
    """Run one screen and return its answer.

    Non-interactive runs return :attr:`Screen.default_choice` without drawing
    anything, which is what makes ``--full`` usable from a script.
    """
    if interactive is None:
        interactive = is_interactive(keys)
    if not interactive:
        return screen.default_choice
    console = console or Console()

    chosen: set[str] = {item.id for item in screen.items if item.selected}
    cursor = 0
    for index, item in enumerate(screen.items):
        if item.id in chosen or (not chosen and item.default):
            cursor = index
            break

    painted = 0
    _emit(console, CURSOR_HIDE)
    try:
        while True:
            painted = render(screen, cursor=cursor, chosen=chosen, console=console, repaint=painted)
            key = read_key(keys)
            item = screen.items[cursor]
            row = _digit(key)
            if key == "up":
                cursor = (cursor - 1) % len(screen.items)
            elif key == "down":
                cursor = (cursor + 1) % len(screen.items)
            elif key in ("left", "right"):
                # Accepted and ignored: a wizard screen has no tab strip to
                # walk, and swallowing them beats leaving a stray ``[C`` in
                # the terminal (M15b §4).
                continue
            elif key in ("quit", "escape"):
                # Esc and q both decline; declining is the screen's default,
                # never a silent "yes to everything".
                return screen.default_choice
            elif key == "space" and screen.multi and item.id != SKIP:
                chosen.symmetric_difference_update({item.id})
            elif row is not None and row < len(screen.items):
                # 1-9 jumps to the row, and ticks it in a multi-select; it
                # never submits, exactly as the TUI's digits behave.
                cursor = row
                target = screen.items[row]
                if screen.multi and target.id != SKIP:
                    chosen.symmetric_difference_update({target.id})
            elif key == "enter":
                if screen.multi:
                    # Done keeps what is ticked — including what was ticked
                    # on the way to the last row; it is not a discard.
                    return {entry for entry in chosen if entry != SKIP}
                if item.id == SKIP:
                    return screen.default_choice
                return item.id
    finally:
        _emit(console, CURSOR_SHOW)


def ask_text(prompt: str, *, interactive: bool | None = None, secret: bool = False) -> str:
    """One free-text answer (an API key, a bot token); ``""`` when piped."""
    if interactive is None:
        interactive = is_interactive()
    if not interactive:
        return ""
    if secret:
        return _read_masked(prompt)
    return input(prompt).strip()


def _read_masked(prompt: str) -> str:
    """Read a secret echoing ``*`` per character so the user sees progress.

    Falls back to ``getpass`` (no echo at all) when the terminal cannot be put
    into raw mode, e.g. on Windows without a console or when stdin is not a TTY.
    """
    import sys

    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - windows
        import getpass

        return getpass.getpass(prompt).strip()
    fd = sys.stdin.fileno()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    saved = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x7f", "\b"):
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if ch == "\x15":  # ctrl-u clears
                sys.stdout.write("\b \b" * len(chars))
                chars.clear()
                sys.stdout.flush()
                continue
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return "".join(chars).strip()


__all__ = [
    "CLEAR_LINE",
    "CURSOR_HIDE",
    "CURSOR_SHOW",
    "CURSOR_UP",
    "STAR",
    "ask",
    "ask_text",
    "choice_hint",
    "is_interactive",
    "read_key",
    "render",
    "render_item",
    "render_lines",
    "screen_lines",
]
