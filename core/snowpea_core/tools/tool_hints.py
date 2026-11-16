"""Answers that tell the model where a missing tool is (1.55.0).

A model that calls ``snapshot`` (a REPL global) as a tool, or searches for MCP
tools in an agent that has none, used to read "unknown tool" or "No tool
matched" and kept guessing.  Three hints turn those answers into instructions:

* a host tool's ``helpers`` (``tool.register``): "<name> is not a tool: it is
  a helper inside <tool>; call it in that tool's code";
* an agent definition's ``unavailable`` map (frontmatter), ``{<namespace or
  pattern>: "<message>"}``: the message, when the name or the search matches;
* the nearest existing names (edit distance and shared words), at most five.
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from collections.abc import Iterable
from typing import Any

#: At most this many near names in a hint.
MAX_NEAREST = 5

HELPER_NOTE = "{name} is not a tool: it is a helper inside {tool}; call it in that tool's code"

_WORD = re.compile(r"[a-z0-9]+")


def helper_note(session: Any, name: str) -> str | None:
    """:data:`HELPER_NOTE` when ``name`` is a helper of one of the session's host tools."""
    from snowpea_core.tools.host_tools import HOST_TOOLS

    try:
        owner = HOST_TOOLS.helper_owner(session, name)
    except Exception:  # noqa: BLE001 - a hint is a bonus
        return None
    return HELPER_NOTE.format(name=name, tool=owner) if owner else None


def matches(pattern: str, name: str) -> bool:
    """True when ``name`` falls under ``pattern``: a glob, a name, or a namespace.

    ``mcp`` covers ``mcp__github__search`` and ``browser`` covers
    ``browser_click`` (a prefix up to an underscore); ``mcp__github*`` is a glob.
    """
    key = pattern.strip().lower()
    text = name.strip().lower()
    if not key or not text:
        return False
    if any(char in key for char in "*?["):
        return fnmatch.fnmatchcase(text, key)
    return text == key or text.startswith(key + "_")


def unavailable_note(session: Any, name: str = "", query: str = "") -> str | None:
    """The agent definition's ``unavailable`` message for ``name`` or a search ``query``."""
    table = getattr(session, "agent_unavailable", None)
    if not isinstance(table, dict) or not table:
        return None
    terms = query_terms(query) if query else []
    for pattern, message in table.items():
        if not isinstance(message, str) or not message.strip():
            continue
        if name and matches(str(pattern), name):
            return message.strip()
        if any(matches(str(pattern), term) for term in terms):
            return message.strip()
    return None


def query_terms(query: str) -> list[str]:
    """The names and words of a ``tool_search`` query (``select:a,b`` or keywords)."""
    text = query.strip()
    if text.lower().startswith("select:"):
        text = text[len("select:") :]
    return [part.strip().lstrip("+") for part in re.split(r"[,\s]+", text) if part.strip()]


def nearest(target: str, names: Iterable[str], limit: int = MAX_NEAREST) -> list[str]:
    """Existing names closest to ``target``: similar spelling or shared words."""
    wanted = target.strip().lower()
    if not wanted:
        return []
    pool = sorted({str(name) for name in names if name})
    words = set(_WORD.findall(wanted))
    scored: list[tuple[float, str]] = []
    for name in pool:
        lower = name.lower()
        ratio = difflib.SequenceMatcher(None, wanted, lower).ratio()
        shared = words & set(_WORD.findall(lower))
        overlap = len(shared) / max(1, len(words))
        score = max(ratio, overlap)
        if score >= 0.5:
            scored.append((score, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _score, name in scored[: max(0, limit)]]


def search_hints(session: Any, query: str, names: Iterable[str]) -> list[str]:
    """Lines for a ``tool_search`` that matched nothing: helper, unavailable, nearest."""
    lines: list[str] = []
    terms = query_terms(query)
    for term in terms:
        helper = helper_note(session, term)
        if helper:
            lines.append(helper + ".")
    note = unavailable_note(session, query=query)
    if note:
        lines.append(note)
    pool = list(names)
    close: list[str] = []
    for term in terms or [query]:
        for name in nearest(term, pool):
            if name not in close:
                close.append(name)
    if close:
        lines.append(f"Nearest tools: {', '.join(close[:MAX_NEAREST])}.")
    return lines


__all__ = [
    "HELPER_NOTE",
    "MAX_NEAREST",
    "helper_note",
    "matches",
    "nearest",
    "query_terms",
    "search_hints",
    "unavailable_note",
]
