"""RPC handlers for ``skill.*`` (M6 contract §1).

These sit beside the session handlers for the same reason those do: the daemon
module stays about process lifecycle.  ``register_skill_handlers`` is one line
in ``build_dispatcher``; the loader itself is built by ``wire_core``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent.definition import DefinitionError, validate_name
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    Ok,
    SkillCreateParams,
    SkillCreateResult,
    SkillInstallParams,
    SkillLearnParams,
    SkillLearnResult,
    SkillListParams,
    SkillListResult,
    SkillReadParams,
    SkillReadResult,
    SkillReadScope,
    SkillRemoveParams,
    SkillSearchParams,
    SkillSearchResult,
    SkillSourceNotIncluded,
    SkillWriteParams,
    SkillWriteResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.skills.generate import (
    SkillCreateError,
    skill_root,
    write_skill_document,
)
from snowpea_core.skills.loader import (
    PROJECT_DIRS,
    SOURCE_BUILTIN,
    SOURCE_CLAUDE_GLOBAL,
    SOURCE_CLAUDE_PLUGIN,
    SOURCE_CLAUDE_PROJECT,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    builtin_root,
)
from snowpea_core.skills.marketplace import InstallError
from snowpea_core.skills.publish import PublishError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.skills.loader import LoadedSkill, SkillLoader

log = logging.getLogger("snowpea.server.skill")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "skill.list",
    "skill.search",
    "skill.install",
    "skill.reload",
    "skill.remove",
    "skill.create",
    "skill.learn",
    "skill.read",
    "skill.write",
)


def _loader(core: Core) -> SkillLoader:
    loader = getattr(core, "skills", None)
    if loader is None:
        raise RpcError(errors.INTERNAL, "the skill loader is not wired")
    return loader


def _workdir(raw: str | None) -> Path | None:
    """A client's ``workdir``; ``""`` is no directory, never the daemon's cwd (1.33.0).

    A relative one would be read against the daemon's cwd too, so it is
    ``invalid_params``.
    """
    text = (raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise RpcError(errors.INVALID_PARAMS, f"workdir must be an absolute path: {text!r}")
    return path


async def skill_list_handler(
    _conn: RpcConnection, params: SkillListParams, core: Core
) -> SkillListResult:
    """``skill.list`` — plugins, skills, command files and agent definitions."""
    loader = _loader(core)
    workdir = _workdir(params.workdir)
    if workdir is None:
        return SkillListResult(skills=loader.list())
    # A whole scan of the roots: off the event loop.
    return SkillListResult(skills=await asyncio.to_thread(loader.list, workdir))


async def skill_search_handler(
    _conn: RpcConnection, params: SkillSearchParams, core: Core
) -> SkillSearchResult:
    """``skill.search`` — the three marketplaces, each hit labelled by source."""
    loader = _loader(core)
    skills, unavailable = await loader.search(params.query)
    not_included = [
        SkillSourceNotIncluded(label=label, reason=reason)
        for label, reason in getattr(loader, "last_not_included", [])
    ]
    return SkillSearchResult(skills=skills, unavailable=unavailable, notIncluded=not_included)


async def skill_install_handler(_conn: RpcConnection, params: SkillInstallParams, core: Core) -> Ok:
    """``skill.install`` — local path, git URL, ``<marketplace>/<plugin>`` or shortcut."""
    try:
        target = await _loader(core).install(params.source, link=params.link)
    except InstallError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    log.info("installed %s into %s", params.source, target)
    return Ok(ok=True)


async def skill_reload_handler(_conn: RpcConnection, _params: Empty, core: Core) -> Ok:
    """``skill.reload`` — re-scan and announce ``commands.changed``."""
    await _loader(core).reload()
    return Ok(ok=True)


async def skill_remove_handler(_conn: RpcConnection, params: SkillRemoveParams, core: Core) -> Ok:
    """``skill.remove`` — a plugin, or a global or project skill, agent or command.

    ``scope`` and ``kind`` narrow where it looks (1.33.0); ``not_found`` names
    what was looked for and every path it tried.
    """
    name = params.name.strip()
    workdir = _workdir(params.workdir)
    if params.scope == "project" and workdir is None:
        raise RpcError(errors.INVALID_PARAMS, "skill.remove with scope 'project' needs a workdir")
    loader = _loader(core)
    scope, kind = params.scope, params.kind
    try:
        removed = await loader.remove(name, scope=scope, workdir=workdir, kind=kind)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    if removed is None:
        looked = loader.removal_candidates(name, scope=scope, workdir=workdir, kind=kind)
        what = params.kind or "plugin, skill, agent or command"
        where = f" in scope {params.scope}" if params.scope else ""
        raise RpcError(
            errors.NOT_FOUND,
            f"no {what} named {name!r}{where}",
            {
                "name": name,
                "scope": params.scope,
                "kind": params.kind,
                "workdir": str(workdir) if workdir else None,
                "looked": [str(path) for path in looked],
            },
        )
    return Ok(ok=True)


#: Where a loaded skill's ``source`` puts it for ``skill.read`` (1.33.0).
_READ_SCOPE: dict[str, SkillReadScope] = {
    SOURCE_BUILTIN: "builtin",
    SOURCE_GLOBAL: "global",
    SOURCE_CLAUDE_GLOBAL: "global",
    SOURCE_PROJECT: "project",
    SOURCE_CLAUDE_PROJECT: "project",
    SOURCE_CLAUDE_PLUGIN: "plugin",
}

#: Sources ``skill.write`` cannot save over: the engine's own skills, a
#: plugin's, and Claude Code's global ones (a snowpea copy would lose to them).
_READ_ONLY_SOURCES = frozenset({SOURCE_BUILTIN, SOURCE_CLAUDE_GLOBAL, SOURCE_CLAUDE_PLUGIN})


def _read_scope(source: str) -> SkillReadScope:
    return "plugin" if source.startswith("plugin:") else _READ_SCOPE.get(source, "global")


def _read_only(source: str) -> bool:
    return source in _READ_ONLY_SOURCES or source.startswith("plugin:")


def _resolve_skill_path(
    name: str,
    workdir: Path | None,
    home: Path,
    scope: SkillReadScope | None = None,
    loader: SkillLoader | None = None,
    plugin: str | None = None,
) -> tuple[Path, SkillReadScope, bool] | None:
    """The ``SKILL.md`` ``skill.read`` answers with: ``(path, scope, readOnly)``.

    With ``scope`` only that scope is looked in (1.33.0).  Without it: the
    project's ``.snowpea`` then ``.claude`` (with a ``workdir``), then the
    global directory, then whatever skill the loader has under that name —
    a built-in or a plugin's included.  ``plugin`` picks which plugin's copy
    when several ship the name (the caller has set ``scope`` to ``"plugin"``).
    """
    loaded: LoadedSkill | None = None
    if loader is not None:
        candidate_skill = loader.skills.get(name)
        if candidate_skill is not None and candidate_skill.kind == "skill":
            loaded = candidate_skill

    def from_loader(*scopes: SkillReadScope) -> tuple[Path, SkillReadScope, bool] | None:
        if loaded is None or loaded.doc.path is None or not loaded.doc.path.is_file():
            return None
        found = _read_scope(loaded.source)
        if found not in scopes:
            return None
        if found == "project" and (workdir is None or not loaded.doc.path.is_relative_to(workdir)):
            # Another project's skill is not this project's.
            return None
        return loaded.doc.path, found, _read_only(loaded.source)

    if scope in (None, "project") and workdir is not None:
        # ".snowpea" wins over ".claude" (loader order).
        for relative, _source in reversed(PROJECT_DIRS):
            candidate = workdir / relative / "skills" / name / "SKILL.md"
            if candidate.is_file():
                return candidate, "project", False
        if scope == "project":
            return from_loader("project")
    if scope in (None, "global"):
        candidate = home / "skills" / name / "SKILL.md"
        if candidate.is_file():
            return candidate, "global", False
        if scope == "global":
            return from_loader("global")
    if scope is None:
        found = from_loader("project", "global", "builtin", "plugin")
        if found is not None:
            return found
    if scope in (None, "builtin"):
        candidate = builtin_root() / name / "SKILL.md"
        if candidate.is_file():
            return candidate, "builtin", True
        if scope == "builtin":
            return from_loader("builtin")
    if scope in (None, "plugin"):
        if plugin is None or (loaded is not None and loaded.plugin == plugin):
            found = from_loader("plugin")
            if found is not None:
                return found
        for bundle in loader.plugins if loader is not None else []:
            if plugin is not None and plugin not in (bundle.name, bundle.root.name):
                continue
            candidates = [bundle.root / "skills" / name / "SKILL.md"]
            if name in (bundle.name, bundle.root.name):
                # A plugin that is one bare skill (M15 §B5c).
                candidates.append(bundle.root / "SKILL.md")
            for candidate in candidates:
                if candidate.is_file():
                    return candidate, "plugin", True
    return None


async def skill_create_handler(
    conn: RpcConnection, params: SkillCreateParams, core: Core
) -> SkillCreateResult:
    """``skill.create`` — write ``content`` directly, or start the generating turn."""
    try:
        name = validate_name(params.name)
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    global_ = params.scope == "global"
    home = Path(core.paths.home)

    directory = skill_root(name, workdir=params.workdir, home=home, global_=global_)
    if params.content is not None:
        try:
            package = write_skill_document(
                directory, params.content, requested_name=name, force=params.force
            )
        except SkillCreateError as exc:
            details = {"path": str(directory / "SKILL.md")}
            raise RpcError(errors.SKILL_EXISTS, str(exc), details) from exc
        except PublishError as exc:
            raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
        loader = getattr(core, "skills", None)
        if loader is not None:
            await loader.reload()
        return SkillCreateResult(name=package.name, path=str(directory / "SKILL.md"))

    description = (params.description or "").strip()
    if not description:
        raise RpcError(errors.INVALID_PARAMS, "skill.create needs 'content' or 'description'")
    target = directory / "SKILL.md"
    if target.is_file() and not params.force:
        # Refused before a turn starts, with the same code as the content path.
        raise RpcError(
            errors.SKILL_EXISTS,
            f"{target} already exists; pass force to overwrite it",
            {"path": str(target)},
        )

    turn_conn: RpcConnection | None
    if params.sessionId:
        session = core.sessions.get(params.sessionId)
        if session is None:
            raise RpcError(errors.INVALID_PARAMS, f"no such session: {params.sessionId!r}")
        target = Path(params.workdir).expanduser().resolve()
        if Path(session.workdir).expanduser().resolve() != target:
            raise RpcError(
                errors.INVALID_PARAMS,
                f"sessionId {params.sessionId!r} is not rooted at workdir {params.workdir!r}",
            )
        turn_conn = conn
    else:
        # Draft mode with no session: a headless one, the way a scheduled job
        # gets one, so the generating turn has somewhere to run and report.
        session = await core.sessions.create(params.workdir, origin_surface="skill")
        turn_conn = None
    # The caller gets the sessionId back either way; subscribe it to that
    # session's events so it actually sees the turn it just started
    # (subscribe() is a no-op when it is already on this session).
    core.hub.subscribe(conn, session.id)

    args = f'{name} "{description}"' + (" --global" if global_ else "")
    if params.force:
        args += " --force"
    turn_id = core.commands.start(core, session, "skill", f"create {args}", turn_conn)
    return SkillCreateResult(turnId=turn_id, sessionId=session.id)


async def skill_learn_handler(
    conn: RpcConnection, params: SkillLearnParams, core: Core
) -> SkillLearnResult:
    """``skill.learn`` — ``/skill learn`` as an RPC, with a scope (addendum 27, C8).

    The same summary and rules as the slash command; it answers once the
    SKILL.md is written and the loader has reloaded.
    """
    from snowpea_core.commands.skill_cmd import learn_skill

    if str(getattr(conn, "surface_id", "") or "").startswith("gateway:"):
        raise RpcError(errors.UNAUTHORIZED, "a chat gateway may not write skills")
    session = core.sessions.get(params.sessionId)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    requested = (params.name or "").strip()
    try:
        path = await learn_skill(
            core, session, requested, global_=params.scope == "global", force=params.force
        )
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return SkillLearnResult(name=path.parent.name, path=str(path))


async def skill_read_handler(
    _conn: RpcConnection, params: SkillReadParams, core: Core
) -> SkillReadResult:
    """``skill.read`` — the SKILL.md text for the editor form, or a viewer."""
    try:
        name = validate_name(params.name)
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    workdir = _workdir(params.workdir)
    scope = params.scope
    if params.plugin is not None:
        if scope not in (None, "plugin"):
            raise RpcError(errors.INVALID_PARAMS, "skill.read with a plugin needs scope 'plugin'")
        scope = "plugin"
    if scope == "project" and workdir is None:
        raise RpcError(errors.INVALID_PARAMS, "skill.read with scope 'project' needs a workdir")
    found = _resolve_skill_path(
        name, workdir, Path(core.paths.home), scope, getattr(core, "skills", None), params.plugin
    )
    if found is None:
        where = f" in scope {params.scope}" if params.scope else ""
        raise RpcError(
            errors.NOT_FOUND,
            f"no skill named {name!r}{where}",
            {"name": name, "scope": params.scope},
        )
    path, scope, read_only = found
    return SkillReadResult(
        path=str(path),
        content=path.read_text(encoding="utf-8"),
        scope=scope,
        readOnly=read_only,
    )


def _in_effect(core: Core, name: str, workdir: Path | None) -> Path | None:
    """The ``SKILL.md`` a session in ``workdir`` (or in no project) loads for ``name``.

    A scratch scan of the same roots, so a project the daemon has never loaded
    still answers, and a Claude Code copy that wins is seen as winning.
    """
    from snowpea_core.skills.loader import SkillLoader

    scratch = SkillLoader(core, workdirs=[workdir] if workdir is not None else [])
    scratch.scan()
    loaded = scratch.skills.get(name)
    return loaded.doc.path if loaded is not None and loaded.kind == "skill" else None


async def skill_write_handler(
    _conn: RpcConnection, params: SkillWriteParams, core: Core
) -> SkillWriteResult:
    """``skill.write`` — save the editor form's text verbatim.

    Answers where it was written and whether that copy is the one in effect
    (1.33.0): a project skill of the same name shadows a global one, and a
    Claude Code global skill shadows a snowpea global one.
    """
    try:
        name = validate_name(params.name)
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    global_ = params.scope == "global"
    workdir = _workdir(params.workdir)
    if not global_ and workdir is None:
        raise RpcError(errors.INVALID_PARAMS, "skill.write with scope 'project' needs a workdir")
    directory = skill_root(
        name, workdir=workdir or Path(core.paths.home), home=Path(core.paths.home), global_=global_
    )
    try:
        write_skill_document(directory, params.content, requested_name=name, force=True)
    except PublishError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    loader = getattr(core, "skills", None)
    if loader is not None:
        await loader.reload()
    path = directory / "SKILL.md"
    effective = await asyncio.to_thread(_in_effect, core, name, workdir)
    active = effective is not None and effective.resolve() == path.resolve()
    return SkillWriteResult(ok=True, path=str(path), active=active)


def register_skill_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("skill.list", skill_list_handler)
    dispatcher.register("skill.search", skill_search_handler)
    dispatcher.register("skill.install", skill_install_handler)
    dispatcher.register("skill.reload", skill_reload_handler)
    dispatcher.register("skill.remove", skill_remove_handler)
    dispatcher.register("skill.create", skill_create_handler)
    dispatcher.register("skill.learn", skill_learn_handler)
    dispatcher.register("skill.read", skill_read_handler)
    dispatcher.register("skill.write", skill_write_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_skill_handlers"]
