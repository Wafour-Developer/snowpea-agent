"""What a scheduled job's runs may do without asking (1.68.0, ``jobApprovals``).

A *grant* belongs to one job, keyed by its id, and is kept with the job (the
``options`` JSON of its ``jobs`` row), so it holds for every later run although
each run opens a session of its own.  Two ways make one:

* a person answers a run's approval card with scope ``task`` ("always allow in
  this routine"): the call's own shape is stored (``by: "approval"``);
* a person gives it with ``job.schedule grants`` / ``job.update addGrants``
  (``by: "person"``), so the first run never stops on that ask.

The shapes and the matching are the allowlist's, held to the same safety:

``rule``
    an allowlist regex matched against ONE plain shell command
    (:func:`~snowpea_core.permissions.shell_remember.matchable`), never a line
    that chains, pipes, substitutes or redirects; a chain is covered when each
    of its simple commands is (:func:`chain_program_ok`), as ``promote_chain``
    does for stored rules.  A catch-all pattern is refused.
``line``
    one exact command line, whitespace normalised (the shape of a compound
    line, m21), in the folder it was asked in when the card stored it.
``tool``
    a tool that is not a shell, by name (never a browser page action).

A grant only turns a mode-matrix ``ask`` into ``allow``
(:meth:`~snowpea_core.permissions.policy.PermissionPolicy.promote`): it never
covers ``config`` or ``secret``, and the security lists, an unchecked line and
the agent-switch guard still ask (``agent.loop`` forces those after the policy).
"""

from __future__ import annotations

import posixpath
import re
import uuid
from typing import Any

from snowpea_core.permissions.allowlist import (
    SHELL_TOOLS,
    _matches_pattern,
    catch_all_problem,
    command_of,
)
from snowpea_core.permissions.shell_remember import (
    chain_program_ok,
    exact_line,
    matchable,
    segments,
)

#: Most grants one job keeps; the oldest go first.
MAX_GRANTS = 100

#: The ``job.update`` / ``job.schedule`` refusal reason for a bad grant.
BAD_GRANT = "bad_grant"


def _new_id() -> str:
    return f"g-{uuid.uuid4().hex[:10]}"


def _is_shell(tool: str) -> bool:
    return tool in SHELL_TOOLS


def _folder(args: dict[str, Any], workdir: Any) -> str:
    cwd = str(args.get("cwd") or "").strip()
    if not (workdir or cwd):
        return ""
    return posixpath.normpath(posixpath.join(str(workdir or ""), cwd))


def from_input(tool: str, pattern: str | None, line: str | None, *, now: str | None) -> dict:
    """A grant a person typed (``JobGrantInput``); ``ValueError`` says what is wrong."""
    tool = (tool or "shell").strip()
    if pattern and line:
        raise ValueError("give a pattern or a line, not both")
    if pattern is not None:
        if not _is_shell(tool):
            raise ValueError(f"a pattern matches shell commands; {tool!r} is not a shell tool")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"bad pattern: {exc}") from exc
        problem = catch_all_problem(pattern)
        if problem is not None:
            raise ValueError(f"bad pattern: {problem}")
        display = pattern.removeprefix("^").removesuffix("$")
        return {"id": _new_id(), "tool": tool, "kind": "rule", "pattern": pattern,
                "display": display, "by": "person", "createdAt": now}
    if line is not None:
        if not _is_shell(tool):
            raise ValueError(f"a line is a shell command; {tool!r} is not a shell tool")
        exact = exact_line(line)
        if exact is None:
            raise ValueError("the line is empty, has an unclosed quote or a control character")
        return {"id": _new_id(), "tool": tool, "kind": "line", "line": exact,
                "display": exact, "by": "person", "createdAt": now}
    if _is_shell(tool):
        raise ValueError("a shell grant needs a pattern or a line")
    if tool.startswith("repl."):
        raise ValueError("a browser page action is never allowed for a whole routine")
    return {"id": _new_id(), "tool": tool, "kind": "tool", "display": tool,
            "by": "person", "createdAt": now}


def from_request(request: Any, workdir: Any, *, now: str | None) -> dict | None:
    """The grant an approval answered ``task`` stores, or ``None`` (kept to once).

    The call's own shape: an exact compound line in its folder, the rule the
    card offered for a plain command (``remember``), or the tool's name.
    """
    tool = str(getattr(request, "tool", "") or "")
    args = dict(getattr(request, "args", None) or {})
    if not tool or tool.startswith("repl.") or args.get("onceOnly") is True:
        return None
    if getattr(request, "guard", None) or getattr(request, "reasonCode", None):
        return None
    remember = getattr(request, "remember", None)
    if remember is not None and getattr(remember, "kind", None) == "script":
        return None
    command = command_of(args)
    if command:
        if not matchable(command):
            exact = exact_line(command)
            if exact is None:
                return None
            return {"id": _new_id(), "tool": tool, "kind": "line", "line": exact,
                    "folder": _folder(args, workdir), "display": exact,
                    "by": "approval", "createdAt": now}
        pattern = getattr(remember, "pattern", None) if remember is not None else None
        if not pattern or getattr(remember, "exactLine", None):
            return None
        return {"id": _new_id(), "tool": tool, "kind": "rule", "pattern": str(pattern),
                "display": str(getattr(remember, "display", "") or pattern),
                "by": "approval", "createdAt": now}
    if _is_shell(tool):
        return None
    return {"id": _new_id(), "tool": tool, "kind": "tool", "display": tool,
            "by": "approval", "createdAt": now}


def same(a: dict, b: dict) -> bool:
    """Two grants allow the same thing (a repeat is not stored twice)."""
    keys = ("tool", "kind", "pattern", "line", "folder")
    return all(a.get(k) == b.get(k) for k in keys)


def _rule_covers(grants: list[dict], tool: str, command: str) -> bool:
    return any(
        g.get("kind") == "rule" and g.get("tool") == tool
        and _matches_pattern(str(g.get("pattern") or ""), command)
        for g in grants
    )


def covers(grants: list[dict] | None, tool: str, args: dict[str, Any] | None,
           workdir: Any = None) -> bool:
    """True when one of ``grants`` allows this call."""
    if not grants or not tool:
        return False
    args = args or {}
    command = command_of(args)
    if command:
        exact = exact_line(command)
        folder = _folder(args, workdir)
        if exact is not None and any(
            g.get("kind") == "line" and g.get("tool") == tool and g.get("line") == exact
            and (g.get("folder") is None or g.get("folder") == folder)
            for g in grants
        ):
            return True
        if matchable(command):
            return _rule_covers(grants, tool, command)
        parts = segments(command)
        return parts is not None and all(
            chain_program_ok(part) and _rule_covers(grants, tool, part) for part in parts
        )
    if _is_shell(tool):
        return False
    return any(g.get("kind") == "tool" and g.get("tool") == tool for g in grants)


def of_session(session: Any, lookup: Any = None) -> list[dict]:
    """The grants a run's session carries; a subagent's come from its run's."""
    current, seen = session, 0
    while current is not None and seen < 16:
        grants = getattr(current, "job_grants", None)
        if isinstance(grants, list):
            # Only a turn the scheduler started runs with them (review of 1.68.0, L3).
            return grants if scheduler_turn(current) else []
        if getattr(current, "kind", None) != "subagent" or lookup is None:
            return []
        parent = getattr(current, "parent_session_id", None)
        current = lookup(parent) if parent else None
        seen += 1
    return []


def run_session(session: Any, lookup: Any = None) -> Any:
    """The ``scheduled`` session ``session`` belongs to (itself, or a subagent's
    run up its parent chain), or ``None``."""
    current, seen = session, 0
    while current is not None and seen < 16:
        kind = getattr(current, "kind", None)
        if kind == "scheduled":
            return current
        if kind != "subagent" or lookup is None:
            return None
        parent = getattr(current, "parent_session_id", None)
        current = lookup(parent) if parent else None
        seen += 1
    return None


def scheduler_turn(session: Any) -> bool:
    """``session`` is running a turn the scheduler started that nobody else's
    words joined (security review of 1.68.0, L3): the job's grants and mode are
    for those turns only.  A browser may continue a run's session (owner
    decision); its turn runs as in an ordinary session."""
    turn = getattr(session, "current_turn", None)
    return bool(turn) and turn in (getattr(session, "scheduler_turns", None) or ())


def job_ref_of(session: Any, lookup: Any = None) -> dict | None:
    """``{"id", "title"}`` of the job a session's run belongs to, or ``None``."""
    current, seen = session, 0
    while current is not None and seen < 16:
        ref = getattr(current, "job_ref", None)
        if isinstance(ref, dict) and ref.get("id"):
            return ref
        if getattr(current, "kind", None) != "subagent" or lookup is None:
            return None
        parent = getattr(current, "parent_session_id", None)
        current = lookup(parent) if parent else None
        seen += 1
    return None


__all__ = ["BAD_GRANT", "MAX_GRANTS", "covers", "from_input", "from_request",
           "job_ref_of", "of_session", "run_session", "same", "scheduler_turn"]
