"""Deferred tools: the model gets a handful of schemas and a list of names.

Every round re-sends the whole tool list, so a daemon with two MCP servers pays
for forty tool schemas on every single provider call whether or not the turn
could ever use one.  The fix is claw-code's: keep a small *eager* set whose
schemas are always sent, and describe everything else by **name only** in one
grouped line.  ``tool_search`` turns a name into a schema, and a tool loaded
that way stays loaded for the rest of the session.

Nothing is taken away from the model: a deferred tool that is called anyway
still runs — the registry wraps it so the call loads it on the way.

``tools.deferred: false`` restores the old behaviour, and ``tools.eager``
forces named tools back into the always-sent set.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.providers.base import ToolSpec

#: The tool that turns a deferred name into a schema.  Always eager: without it
#: the grouped line would name tools the model has no way to reach.
TOOL_SEARCH = "tool_search"

#: Always sent in full.  These are the tools a coding turn reaches for before
#: it knows anything about the task, so deferring them would only cost a round.
EAGER_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "view_image",
        "write_file",
        "patch",
        "shell",
        "execute_code",
        "grep",
        "glob",
        "ask_user",
        "delegate_task",
        "skill_view",
        "set_mode",
    }
)

#: The eager set for a read-only child (an ``explore`` or ``reviewer``
#: definition).  Its whole job is to look and report, so it starts with the
#: three tools that do that and loads an LSP or git tool if it wants one.
READONLY_EAGER_TOOLS: frozenset[str] = frozenset({"read_file", "view_image", "grep", "glob"})

#: A session that may call one of these is not read-only, whatever its role
#: says.  ``shell`` is deliberately absent: a read-only reviewer is routinely
#: allowed to run the test suite.
WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch", "git_commit", "lsp_rename"})

#: Prefix of the line appended to a deferred tool's first result.
LOADED_NOTE = "loaded {name} for this session"

#: Heading of the one line that names the deferred tools.
DEFERRED_PREFIX = "Deferred (load with tool_search):"

#: What the tools fragment says once about the scheme.
DEFERRED_HINT = (
    "The tools below are not the only ones you have. The groups on the last line "
    "are loaded on demand: call tool_search with what you need (\"select:name,name\" "
    "for exact tools, or a few keywords) and their schemas join your tool list for "
    "the rest of the session. Calling one by name works too."
)

#: Matches at most this many tools per ``tool_search`` call.
MAX_RESULTS = 5

_WORD = re.compile(r"[A-Za-z0-9_]+")


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def enabled(settings: Any) -> bool:
    """``tools.deferred``; on by default, and on when there are no settings."""
    tools = getattr(settings, "tools", None)
    if tools is None:
        return True
    return bool(getattr(tools, "deferred", True))


def forced_eager(settings: Any) -> frozenset[str]:
    """``tools.eager`` — names the user wants sent in full whatever we think."""
    tools = getattr(settings, "tools", None)
    names = getattr(tools, "eager", None) or ()
    return frozenset(str(name).strip() for name in names if str(name).strip())


# ---------------------------------------------------------------------------
# which tools a session sends in full
# ---------------------------------------------------------------------------


def loaded(session: Any) -> set[str]:
    """Tools ``tool_search`` (or a lucky guess) has already loaded."""
    names = getattr(session, "loaded_tools", None)
    return set(names) if names else set()


def load(session: Any, names: Iterable[str]) -> list[str]:
    """Mark ``names`` loaded for the rest of ``session``; returns the new ones."""
    current = getattr(session, "loaded_tools", None)
    if current is None:
        current = set()
        try:
            session.loaded_tools = current
        except AttributeError:  # pragma: no cover - a frozen stand-in in a test
            return []
    fresh = [str(name) for name in names if str(name) not in current]
    current.update(fresh)
    return fresh


def is_readonly_child(session: Any) -> bool:
    """True for a delegated session whose tool list contains nothing that writes."""
    if session is None or not getattr(session, "is_subagent", False):
        return False
    allowed = getattr(session, "allowed_tools", None)
    if not allowed:
        return False
    return not (set(allowed) & WRITE_TOOLS)


def eager_names(session: Any = None, extra: Iterable[str] = ()) -> frozenset[str]:
    """Every tool whose full schema this session's next round carries.

    A session narrowed by a skill's ``allowed-tools`` or an agent definition is
    already paying for a short list, so all of it stays eager — except a
    read-only child, whose definition hands it fifteen read tools it will use
    two of.
    """
    allowed = getattr(session, "allowed_tools", None) if session is not None else None
    if is_readonly_child(session):
        base = set(READONLY_EAGER_TOOLS)
    elif allowed:
        base = {str(name) for name in allowed}
    else:
        base = set(EAGER_TOOLS)
    base.add(TOOL_SEARCH)
    base.update(str(name) for name in extra)
    base.update(loaded(session))
    return frozenset(base)


def split(
    specs: Sequence[ToolSpec], session: Any = None, settings: Any = None
) -> tuple[list[ToolSpec], list[ToolSpec]]:
    """``(sent in full, named only)`` for one round's tool list.

    Every spec is stamped with :attr:`ToolSpec.deferred` on the way through, so
    a caller that wants the whole catalogue back can still tell the two apart.
    """
    if not enabled(settings):
        for spec in specs:
            spec.deferred = False
        return list(specs), []
    eager = eager_names(session, forced_eager(settings))
    keep: list[ToolSpec] = []
    hidden: list[ToolSpec] = []
    for spec in specs:
        spec.deferred = spec.name not in eager
        (hidden if spec.deferred else keep).append(spec)
    return keep, hidden


# ---------------------------------------------------------------------------
# the grouped line
# ---------------------------------------------------------------------------


def group_of(spec: Any) -> str:
    """The label a deferred tool is counted under: its MCP server or category."""
    source = str(getattr(spec, "source", "") or "")
    if source.startswith("mcp:"):
        return source
    category = str(getattr(spec, "category", "") or "")
    if category:
        return category
    name = str(getattr(spec, "name", ""))
    return name.partition("_")[0] or "other"


def deferred_line(specs: Sequence[Any]) -> str:
    """``Deferred (load with tool_search): browser (5), git (4), …``.

    Names only, grouped, no descriptions — the descriptions are the whole cost
    this scheme exists to avoid.
    """
    if not specs:
        return ""
    counts: dict[str, int] = {}
    for spec in specs:
        label = group_of(spec)
        counts[label] = counts.get(label, 0) + 1
    groups = ", ".join(f"{label} ({count})" for label, count in sorted(counts.items()))
    return f"{DEFERRED_HINT}\n\n{DEFERRED_PREFIX} {groups}"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _terms(query: str) -> tuple[list[str], list[str]]:
    """``"+slack send"`` -> ``(["slack"], ["send"])`` — required, then optional."""
    required: list[str] = []
    optional: list[str] = []
    for raw in str(query or "").split():
        token = raw.strip()
        if not token:
            continue
        if token.startswith("+") and len(token) > 1:
            required.append(token[1:].lower())
        else:
            optional.append(token.lower())
    return required, optional


def _haystack(spec: Any) -> str:
    return f"{getattr(spec, 'name', '')} {getattr(spec, 'description', '')}".lower()


def _score(spec: Any, terms: Sequence[str]) -> int:
    """How well one tool answers the query; name hits count double."""
    name = str(getattr(spec, "name", "")).lower()
    text = _haystack(spec)
    total = 0
    for term in terms:
        if term in name:
            total += 3
        if term in text:
            total += 1
        if any(word.startswith(term) for word in _WORD.findall(text)):
            total += 1
    return total


def search(
    specs: Sequence[ToolSpec], query: str, limit: int = MAX_RESULTS
) -> list[ToolSpec]:
    """Match ``query`` against ``specs``: ``select:a,b`` is exact, else keywords."""
    text = str(query or "").strip()
    if not text:
        return []
    if text.lower().startswith("select:"):
        wanted = [part.strip() for part in text.partition(":")[2].split(",") if part.strip()]
        by_name = {spec.name: spec for spec in specs}
        return [by_name[name] for name in wanted if name in by_name]
    required, optional = _terms(text)
    terms = required + optional
    scored: list[tuple[int, int, ToolSpec]] = []
    for index, spec in enumerate(specs):
        if required and not all(term in _haystack(spec) for term in required):
            continue
        points = _score(spec, terms)
        if points > 0:
            scored.append((points, -index, spec))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [spec for _, _, spec in scored[: max(1, limit)]]


__all__ = [
    "DEFERRED_HINT",
    "DEFERRED_PREFIX",
    "EAGER_TOOLS",
    "LOADED_NOTE",
    "MAX_RESULTS",
    "READONLY_EAGER_TOOLS",
    "TOOL_SEARCH",
    "WRITE_TOOLS",
    "deferred_line",
    "eager_names",
    "enabled",
    "forced_eager",
    "group_of",
    "is_readonly_child",
    "load",
    "loaded",
    "search",
    "split",
]
