"""Tools that changed their name, and the old names that still mean them.

``memory_write`` / ``memory_search`` became ``save_memory`` / ``search_memory``
(the names Gemini CLI, mem0 and most memory servers use): models trained on
other agents' transcripts kept calling ``update_memory`` or ``save_memory`` and
were refused.  An old name in a user's agent definition, a skill's
``allowed-tools`` or an allowlist entry still means the renamed tool, so a
rename never silently takes a tool away from anyone.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Old tool name -> current tool name.
RENAMED: dict[str, str] = {
    "memory_write": "save_memory",
    "memory_search": "search_memory",
}


def canonical(name: str) -> str:
    """The current name for ``name`` (itself when it was never renamed)."""
    return RENAMED.get(name, name)


def canonical_names(names: Iterable[str]) -> list[str]:
    """``names`` with every old name replaced by the current one, order kept."""
    out: list[str] = []
    for name in names:
        current = canonical(name)
        if current not in out:
            out.append(current)
    return out


def former_names(name: str) -> list[str]:
    """The old names ``name`` used to have."""
    return [old for old, new in RENAMED.items() if new == name]


__all__ = ["RENAMED", "canonical", "canonical_names", "former_names"]
