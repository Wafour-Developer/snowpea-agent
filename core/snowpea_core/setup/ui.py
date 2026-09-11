"""Rendering and key handling for the setup screens.

Single-select screens draw ``(●)``/``(○)`` rows and move with ↑↓/Enter;
multi-select screens draw ``[✓]``/``[ ]`` and toggle with Space.  Tags print as
``[free · no key]`` and ``[active]``, the default row carries ``★``, and the
last row is always ``Skip — keep defaults``.

When stdin is not a TTY — CI, a pipe, ``snowpea setup < /dev/null`` — nothing
is drawn and :func:`ask` returns the screen's defaults straight away.  The
wizard must never hang waiting for a keystroke that cannot arrive.
"""

from __future__ import annotations

import os
import sys
from typing import IO

from rich.console import Console
from rich.text import Text

from snowpea_core.setup.screens import SKIP, Screen, ScreenItem

STAR = "★"


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


def render_item(item: ScreenItem, *, multi: bool, selected: bool, cursor: bool) -> Text:
    """One row, as rich markup."""
    if item.id == SKIP:
        marker = "   "
    elif multi:
        marker = "[✓]" if selected else "[ ]"
    else:
        marker = "(●)" if selected else "(○)"
    line = Text()
    line.append("❯ " if cursor else "  ", style="bold cyan" if cursor else "")
    line.append(marker + " ")
    line.append(item.label, style="bold" if cursor else "")
    if item.default:
        line.append(f" {STAR}", style="yellow")
    for tag in item.tags:
        style = "dim" if tag != "active" else "green"
        line.append(f"  [{tag}]", style=style)
    return line


def render(screen: Screen, *, cursor: int, chosen: set[str], console: Console) -> None:
    """Draw the whole screen, replacing whatever was there before."""
    console.clear()
    console.print(Text(screen.title, style="bold"))
    if screen.help:
        console.print(Text(screen.help, style="dim"))
    console.print()
    for index, item in enumerate(screen.items):
        console.print(
            render_item(
                item,
                multi=screen.multi,
                selected=item.id in chosen,
                cursor=index == cursor,
            )
        )
    console.print()


def render_lines(screen: Screen) -> list[str]:
    """The screen as plain text — used by tests and by ``--json``-less logs."""
    console = Console(file=_NullFile(), width=100, record=True, force_terminal=False)
    chosen = {item.id for item in screen.items if item.selected}
    render(screen, cursor=0, chosen=chosen, console=console)
    return console.export_text().splitlines()


class _NullFile:
    """A file object that swallows everything (rich still records it)."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


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
    if char in ("\x03", "\x04", "q"):
        return "quit"
    return char


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

    while True:
        render(screen, cursor=cursor, chosen=chosen, console=console)
        key = read_key(keys)
        item = screen.items[cursor]
        if key == "up":
            cursor = (cursor - 1) % len(screen.items)
        elif key == "down":
            cursor = (cursor + 1) % len(screen.items)
        elif key == "quit":
            return screen.default_choice
        elif key == "space" and screen.multi and item.id != SKIP:
            chosen.symmetric_difference_update({item.id})
        elif key == "enter":
            if item.id == SKIP:
                return screen.default_choice
            if screen.multi:
                return chosen
            return item.id


def ask_text(prompt: str, *, interactive: bool | None = None, secret: bool = False) -> str:
    """One free-text answer (an API key, a bot token); ``""`` when piped."""
    if interactive is None:
        interactive = is_interactive()
    if not interactive:
        return ""
    if secret:
        import getpass

        return getpass.getpass(prompt).strip()
    return input(prompt).strip()


__all__ = [
    "STAR",
    "ask",
    "ask_text",
    "is_interactive",
    "read_key",
    "render",
    "render_item",
    "render_lines",
]
