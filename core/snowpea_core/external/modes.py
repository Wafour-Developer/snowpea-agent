"""External CLI native modes (protocol 1.56.0, ``ExternalNativeMode``).

Each external CLI exposes its own modes, named as the CLI names them.  An
adapter keeps one table of :class:`NativeMode` rows; that table is the single
source of truth for what a mode means to snowpea (does the CLI ask, what may
it write, are its prompts bridged, may only a person choose it) and for how
the adapter starts the CLI in it (``launch``: argv flags, app-server policy
or an ACP mode id, read only by that adapter).

Discovery decides which rows a CLI really offers (``requires``: a token the
installed CLI's ``--help`` must list).  Old clients keep using the three
snowpea buckets: every row names the ``legacy`` mode they see, and each
adapter maps a legacy mode to one native row (``legacy_native``).

Security (owner decision, 2026-11-17): a row with ``person_only`` is chosen
only by a person, exactly as ``unrestricted`` was (person gate, ledger entry,
Jarvis can only ask).  Flags a row needs that snowpea otherwise forbids are
released only through the person's grant (:func:`proc.bypass_args`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

Asks = Literal["always", "sometimes", "never"]
Writes = Literal["none", "plan", "workspace", "anywhere"]
NativeApprovals = Literal["bridged", "none"]
Legacy = Literal["default", "readonly", "unrestricted"]


@dataclass(frozen=True)
class NativeMode:
    """One mode of one CLI, as the CLI names it."""

    id: str
    label: str
    asks: Asks
    writes: Writes
    approvals: NativeApprovals
    person_only: bool
    legacy: Legacy
    #: Adapter-specific start-up data (flags, policy values, ACP mode id).
    launch: Mapping[str, Any] = field(default_factory=dict, compare=False)
    #: A token the installed CLI must list (e.g. ``auto`` among Claude Code's
    #: ``--permission-mode`` choices); ``None`` when every supported version has it.
    requires: str | None = field(default=None, compare=False)
    #: How to switch to it in the CLI's own terminal UI (the tmux bridge):
    #: ``{"slash": "/plan"}`` (a slash command) or ``{"cycle": True}`` (the mode
    #: key, Shift+Tab, pressed until the pane shows it); ``None``: not there.
    terminal: Mapping[str, Any] | None = field(default=None, compare=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "asks": self.asks,
            "writes": self.writes,
            "approvals": self.approvals,
            "personOnly": self.person_only,
            "legacy": self.legacy,
        }

    @property
    def uncontrolled(self) -> bool:
        """snowpea sees none of what it does, yet it may write the workspace or more."""
        return self.approvals == "none" and self.writes in ("workspace", "anywhere")


#: The modes of an adapter that has no table of its own: the three buckets.
GENERIC: tuple[NativeMode, ...] = (
    NativeMode("default", "Default", "always", "workspace", "bridged", False, "default"),
    NativeMode("readonly", "Read-only", "never", "none", "bridged", False, "readonly"),
    NativeMode("unrestricted", "Unrestricted", "never", "anywhere", "none", True, "unrestricted"),
)


def offered(table: Iterable[NativeMode], tokens: Iterable[str] | None) -> tuple[NativeMode, ...]:
    """The rows of ``table`` the installed CLI offers.

    ``tokens`` is what its help listed (``None`` when it could not be read):
    rows with a ``requires`` are offered only when it is listed; rows without
    one always are.
    """
    seen = set(tokens) if tokens is not None else set()
    return tuple(row for row in table if row.requires is None or row.requires in seen)


def find(table: Iterable[NativeMode], mode_id: str | None) -> NativeMode | None:
    if not mode_id:
        return None
    return next((row for row in table if row.id == mode_id), None)


_OPTION_START = re.compile(r"^\s*(?:-\w,\s*)?--?[A-Za-z]")
_QUOTED = re.compile(r'"([A-Za-z][A-Za-z0-9_-]{0,40})"')
_PAREN = re.compile(r"\(([A-Za-z][A-Za-z0-9_, -]{0,200})\)")


def help_choices(text: str, flag: str) -> list[str] | None:
    """The values ``--help`` lists for ``flag``, or ``None`` when it does not say.

    Reads both shapes the installed CLIs print: commander's ``(choices:
    "plan", "ask")`` (Claude Code, cursor-agent) and a plain parenthesised
    list ``(accept-edits, auto, plan)`` (agy).
    """
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith(flag + " ") or stripped.startswith(flag + "\t")
                or stripped == flag or re.match(rf"^-\w,\s*{re.escape(flag)}\b", stripped)):
            continue
        block = [line]
        for follow in lines[index + 1:]:
            if _OPTION_START.match(follow) or not follow.strip():
                break
            block.append(follow)
        joined = " ".join(part.strip() for part in block)
        if "choices:" in joined:
            found = _QUOTED.findall(joined.split("choices:", 1)[1])
            return found or None
        for match in _PAREN.finditer(joined):
            values = [v.strip() for v in match.group(1).split(",")]
            if values and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", v)
                                        for v in values):
                return values
        return None
    return None


def without_asks(table: Iterable[NativeMode]) -> tuple[NativeMode, ...]:
    """The rows as they are when the CLI's own setting answers every prompt itself."""
    return tuple(replace(row, asks="never", approvals="none") for row in table)


__all__ = [
    "GENERIC",
    "Asks",
    "Legacy",
    "NativeApprovals",
    "NativeMode",
    "Writes",
    "find",
    "help_choices",
    "offered",
    "without_asks",
]
