"""What a remote web login may write as a skill, agent or command (security review 7, M-1).

``skill.write`` and ``skill.create {content}`` save the caller's text verbatim,
and ``skill.learn`` and ``/skill create|learn`` save what a model wrote for the
caller.  A file like that can replace a built-in agent, pre-approve tools
(``allowed-tools``), put itself into every prompt (``autoInject``), change the
mode it runs in (``permission``), pick the model, or bring hooks.  For a remote
web login (not re-authenticated) :func:`remote_write_refusal` allows only a
GLOBAL SKILL that does none of that:

* the project scope is refused: ``<workdir>/.snowpea/skills|agents|commands`` are
  project control files, written only from a local connection — the rule
  ``fs.write`` has (``fs_handlers.control_file``), with the same ``protected``
  error;
* a global agent or command is refused (``details.what``: ``"agent"`` /
  ``"command"``);
* front matter with a key of :data:`WIDENING_KEYS` is refused
  (``"front_matter"``, ``details.keys``);
* a name a built-in agent, command or skill has is refused (``"builtin_name"``).

Refusals are ``jarvis_person_only`` with ``details.reason: "local_only"``.  A
local person, a browser (its Settings page, with the marker) and in-process
callers are not judged here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError

#: Front-matter keys (compared without case, ``-`` and ``_``) that widen what a
#: skill, agent or command file makes snowpea do.
WIDENING_KEYS: frozenset[str] = frozenset({
    "allowedtools",  # pre-approves the tools it names
    "autoinject",  # joins every prompt that matches its keywords
    "permission", "permissions",  # the mode a definition runs in
    "hooks",
    "model",  # which model (and whose account) runs it
    "tools",  # an agent's tool list
    "deferredtools", "skillsindex", "defaultchild", "childcontext", "identity",
    "sandboxmode", "developerinstructions", "modelreasoningeffort", "thinking",
    "maxturns", "maxtoolrounds", "toolrounds",
    "mcp", "mcpservers",
})


def _normal(key: str) -> str:
    return key.strip().strip("\"'").replace("-", "").replace("_", "").lower()


def front_matter_keys(text: str) -> set[str]:
    """Every key of ``text``'s front matter, as either loader reads it, normalised."""
    from snowpea_core.agent.definition import split_frontmatter as agent_front
    from snowpea_core.skills.skill_md import FENCE
    from snowpea_core.skills.skill_md import split_frontmatter as skill_front

    keys: set[str] = set()
    for parse in (skill_front, agent_front):
        try:
            keys |= {_normal(str(key)) for key in parse(text)[0]}
        except Exception:  # noqa: BLE001 - an unreadable block is judged by its lines
            keys.add("unreadable")
    # Whatever the two parsers make of it: every ``key:`` line of the block.
    lines = text.lstrip("﻿").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip()), len(lines))
    if start < len(lines) and lines[start].strip() == FENCE:
        for line in lines[start + 1:]:
            if line.strip() == FENCE:
                break
            head, sep, _tail = line.partition(":")
            if sep and head.strip():
                keys.add(_normal(head.strip().lstrip("-? ")))
    return keys


def builtin_names(core: Any) -> set[str]:
    """Names the core itself ships: commands, agent definitions and skills."""
    from snowpea_core.agent.definition import BUILTIN_DIR

    names = {str(name) for name in getattr(getattr(core, "commands", None), "protected", ())}
    if BUILTIN_DIR.is_dir():
        names |= {path.stem for path in BUILTIN_DIR.glob("*.md")}
    loader = getattr(core, "skills", None)
    for name, loaded in dict(getattr(loader, "skills", {}) or {}).items():
        if getattr(loaded, "source", None) == "builtin":
            names.add(str(name))
    for agent in list(getattr(loader, "agents", []) or []):
        if getattr(agent, "source", None) == "builtin":
            names.add(str(getattr(agent, "name", "")))
    return {name for name in names if name}


def remote_write_refusal(conn: Any, core: Any, *, method: str, kind: str, name: str,
                         global_: bool, content: str | None, target: Path | None = None) -> None:
    """Refuse a remote web login's skill/agent/command write (see the module doc).

    ``content`` ``None``: the text is not known yet (a model will write it) —
    the scope, kind and name rules apply now, :func:`content_refusal` later.
    """
    from snowpea_core.server import settings_gate
    from snowpea_core.server.person_gate import connection_kind

    settings_gate.refuse_closed(conn, f"call {method}")
    if connection_kind(conn) != "remote_login":
        return
    if not global_:
        settings_gate.ledger_settings(core, conn, [f"{method} (project {kind})"], refused=True,
                                      method=method)
        raise RpcError(
            errors.PROTECTED,
            "a project control file (hooks, agents, commands, skills, MCP, git hooks and "
            "config) is written only from a local connection",
            {"reason": settings_gate.LOCAL_ONLY, "kind": "remote_login",
             **({"path": str(target)} if target is not None else {})},
        )
    if kind != "skill":
        settings_gate.refuse_local_only(conn, core, f"{method} of a global {kind}",
                                        method=method, detail=kind)
    if name in builtin_names(core):
        settings_gate.refuse_local_only(
            conn, core, f"{method} of {name!r}, a name snowpea's own {kind}s use",
            method=method, detail="builtin_name")
    if content is not None:
        content_refusal(conn, core, method=method, content=content)


def content_refusal(conn: Any, core: Any, *, method: str, content: str) -> None:
    """Refuse a remote web login's document whose front matter widens what runs."""
    from snowpea_core.server import settings_gate
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "remote_login":
        return
    found = sorted(front_matter_keys(content) & (WIDENING_KEYS | {"unreadable"}))
    if found:
        settings_gate.ledger_settings(core, conn, [f"{method} (front matter)"], refused=True,
                                      method=method)
        settings_gate.refuse(
            conn, f"{method} with front matter that widens what runs ({', '.join(found)})",
            reason=settings_gate.LOCAL_ONLY, extra={"what": "front_matter", "keys": found})


__all__ = ["WIDENING_KEYS", "builtin_names", "content_refusal", "front_matter_keys",
           "remote_write_refusal"]
