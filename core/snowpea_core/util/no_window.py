"""No console windows from the daemon's children on Windows (addendum 26).

A daemon the desktop app starts is detached (``DETACHED_PROCESS``): it has no
console, so every console program it runs — the agent's shell, uv, git, an
MCP or language server — gets a new, visible one unless it is created with
``CREATE_NO_WINDOW``. Rather than trust every ``subprocess`` and
``asyncio.create_subprocess_*`` call (and every library) to pass the flag,
the daemon makes it the default for ``subprocess.Popen``, which asyncio uses
underneath. A caller that asks for its own console (``CREATE_NEW_CONSOLE``)
or none at all (``DETACHED_PROCESS``) is left alone.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010


def with_no_window(flags: int) -> int:
    """``flags`` plus ``CREATE_NO_WINDOW``, unless it already decides the console."""
    if flags & (DETACHED_PROCESS | CREATE_NEW_CONSOLE):
        return flags
    return flags | CREATE_NO_WINDOW


def has_console() -> bool:
    """True when this process has a console window of its own (Windows)."""
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def hide_child_consoles(*, platform: str = sys.platform) -> bool:
    """Make ``CREATE_NO_WINDOW`` the default for this process's children.

    Only on Windows, and only once; ``True`` when the hook is in place.
    """
    if platform != "win32":
        return False
    if has_console():
        # Run in a terminal: children share its console, as they should — the
        # output and Ctrl+C reach them there. Only a detached daemon needs this.
        return False
    original = subprocess.Popen.__init__
    if getattr(original, "_snowpea_no_window", False):
        return True

    @functools.wraps(original)
    def __init__(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        # ``creationflags`` is the 14th positional parameter; nobody passes it
        # that way, but a call that does keeps exactly what it asked for.
        if len(args) <= 1:
            kwargs["creationflags"] = with_no_window(int(kwargs.get("creationflags") or 0))
        original(self, *args, **kwargs)

    __init__._snowpea_no_window = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    return True


__all__ = ["CREATE_NO_WINDOW", "hide_child_consoles", "with_no_window"]
