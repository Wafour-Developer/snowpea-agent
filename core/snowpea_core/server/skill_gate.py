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


def name_key(name: str) -> str:
    """A name as compared with the built-in ones: NFKC, then casefolded."""
    import unicodedata

    return unicodedata.normalize("NFKC", str(name)).casefold().strip()


def refuse_remote_project(conn: Any, core: Any, *, method: str, what: str,
                          target: Path | None = None) -> None:
    """A remote web login writes no PROJECT control file (skills, agents,
    commands, the project ``.mcp.json``): ``protected`` with ``details.reason:
    "local_only"`` — the rule ``fs.write`` has.  The one helper for
    ``skill.*`` and ``mcp.add|update|remove {scope: "project"}`` (review 8, M-4)."""
    from snowpea_core.server import settings_gate
    from snowpea_core.server.person_gate import connection_kind

    if connection_kind(conn) != "remote_login":
        return
    settings_gate.ledger_settings(core, conn, [f"{method} ({what})"], refused=True,
                                  method=method)
    raise RpcError(
        errors.PROTECTED,
        "a project control file (hooks, agents, commands, skills, MCP, git hooks and "
        "config) is written only from a local connection",
        {"reason": settings_gate.LOCAL_ONLY, "kind": "remote_login",
         **({"path": str(target)} if target is not None else {})},
    )


#: A file in a skill's folder naming who saved it when that was not a local
#: person (security review 8, M-3): ``remote_login``.  Counted by
#: ``web.remote.status.remoteLeft.skills``; a local person's write removes it.
#: Like ``savedBy`` in an MCP entry, it is a mark — whoever can write the
#: folder can remove it.
SAVED_BY_FILE = ".saved-by"


def mark_saved_by(who: Any, directory: Path) -> None:
    """Record (or, for a local person, clear) who saved the skill in ``directory``."""
    from snowpea_core.server.person_gate import connection_kind

    kind = connection_kind(who)
    marker = Path(directory) / SAVED_BY_FILE
    try:
        if kind == "remote_login":
            marker.write_text("remote_login\n", encoding="utf-8")
        elif kind == "person" and marker.is_file():
            marker.unlink()
    except OSError:  # the skill is saved; the mark is best effort
        import logging

        logging.getLogger("snowpea.server.skill_gate").warning(
            "could not mark %s", directory, exc_info=True)


def remote_skills(home: Path) -> int:
    """Global skills a remote web login saved (:data:`SAVED_BY_FILE`)."""
    root = Path(home) / "skills"
    try:
        return sum(1 for child in root.iterdir()
                   if (child / SAVED_BY_FILE).is_file() and not child.is_symlink())
    except OSError:
        return 0


def builtin_names(core: Any) -> set[str]:
    """Names the core itself ships: commands, agent definitions, roles and skills."""
    from pathlib import Path

    from snowpea_core import prompts
    from snowpea_core.agent.definition import BUILTIN_DIR
    from snowpea_core.agent.named import RESERVED_NAMES
    from snowpea_core.config.settings import DEFAULT_AGENT_TEAM

    names = {str(name) for name in getattr(getattr(core, "commands", None), "protected", ())}
    names |= set(DEFAULT_AGENT_TEAM) | set(RESERVED_NAMES)
    roles = Path(prompts.__file__).resolve().parent / "roles"
    for folder in (BUILTIN_DIR, roles):
        if folder.is_dir():
            names |= {path.stem for path in folder.glob("*.md")}
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
        refuse_remote_project(conn, core, method=method, what=f"project {kind}", target=target)
    if kind != "skill":
        settings_gate.refuse_local_only(conn, core, f"{method} of a global {kind}",
                                        method=method, detail=kind)
    # Security review 8, Low: as a case-insensitive file system and a loader
    # that folds names would see it — "Executor" or a full-width name is "executor".
    if name_key(name) in {name_key(builtin) for builtin in builtin_names(core)}:
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


__all__ = ["SAVED_BY_FILE", "WIDENING_KEYS", "builtin_names", "content_refusal",
           "front_matter_keys", "mark_saved_by", "name_key", "refuse_remote_project",
           "remote_skills", "remote_write_refusal"]
