"""RPC handlers for ``skill.*`` (M6 contract §1).

These sit beside the session handlers for the same reason those do: the daemon
module stays about process lifecycle.  ``register_skill_handlers`` is one line
in ``build_dispatcher``; the loader itself is built by ``wire_core``.
"""

from __future__ import annotations

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
    SkillListResult,
    SkillReadParams,
    SkillReadResult,
    SkillRemoveParams,
    SkillScope,
    SkillSearchParams,
    SkillSearchResult,
    SkillWriteParams,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.skills.generate import (
    SkillCreateError,
    skill_root,
    write_skill_document,
)
from snowpea_core.skills.loader import PROJECT_DIRS
from snowpea_core.skills.marketplace import InstallError
from snowpea_core.skills.publish import PublishError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.skills.loader import SkillLoader

log = logging.getLogger("snowpea.server.skill")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "skill.list",
    "skill.search",
    "skill.install",
    "skill.reload",
    "skill.remove",
    "skill.create",
    "skill.read",
    "skill.write",
)


def _loader(core: Core) -> SkillLoader:
    loader = getattr(core, "skills", None)
    if loader is None:
        raise RpcError(errors.INTERNAL, "the skill loader is not wired")
    return loader


async def skill_list_handler(_conn: RpcConnection, _params: Empty, core: Core) -> SkillListResult:
    """``skill.list`` — plugins, skills, command files and agent definitions."""
    return SkillListResult(skills=_loader(core).list())


async def skill_search_handler(
    _conn: RpcConnection, params: SkillSearchParams, core: Core
) -> SkillSearchResult:
    """``skill.search`` — the three marketplaces, each hit labelled by source."""
    skills, unavailable = await _loader(core).search(params.query)
    return SkillSearchResult(skills=skills, unavailable=unavailable)


async def skill_install_handler(_conn: RpcConnection, params: SkillInstallParams, core: Core) -> Ok:
    """``skill.install`` — local path, git URL, ``<marketplace>/<plugin>`` or shortcut."""
    try:
        target = await _loader(core).install(params.source)
    except InstallError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    log.info("installed %s into %s", params.source, target)
    return Ok(ok=True)


async def skill_reload_handler(_conn: RpcConnection, _params: Empty, core: Core) -> Ok:
    """``skill.reload`` — re-scan and announce ``commands.changed``."""
    await _loader(core).reload()
    return Ok(ok=True)


async def skill_remove_handler(_conn: RpcConnection, params: SkillRemoveParams, core: Core) -> Ok:
    """``skill.remove`` — delete an installed plugin directory."""
    if not await _loader(core).remove(params.name):
        raise RpcError(errors.NOT_FOUND, f"no installed plugin named {params.name!r}")
    return Ok(ok=True)


def _resolve_skill_path(name: str, workdir: str, home: Path) -> tuple[Path, SkillScope] | None:
    """First existing ``SKILL.md`` for ``name``: project dirs, then global."""
    root = Path(workdir)
    for relative in reversed(PROJECT_DIRS):  # ".snowpea" wins over ".claude" (loader order)
        candidate = root / relative / "skills" / name / "SKILL.md"
        if candidate.is_file():
            return candidate, "project"
    candidate = home / "skills" / name / "SKILL.md"
    if candidate.is_file():
        return candidate, "global"
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

    if params.content is not None:
        directory = skill_root(name, workdir=params.workdir, home=home, global_=global_)
        try:
            package = write_skill_document(
                directory, params.content, requested_name=name, force=params.force
            )
        except (PublishError, SkillCreateError) as exc:
            raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
        loader = getattr(core, "skills", None)
        if loader is not None:
            await loader.reload()
        return SkillCreateResult(name=package.name, path=str(directory / "SKILL.md"))

    description = (params.description or "").strip()
    if not description:
        raise RpcError(errors.INVALID_PARAMS, "skill.create needs 'content' or 'description'")

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


async def skill_read_handler(
    _conn: RpcConnection, params: SkillReadParams, core: Core
) -> SkillReadResult:
    """``skill.read`` — the SKILL.md text for the editor form."""
    try:
        name = validate_name(params.name)
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    found = _resolve_skill_path(name, params.workdir, Path(core.paths.home))
    if found is None:
        raise RpcError(errors.NOT_FOUND, f"no skill named {name!r}")
    path, scope = found
    return SkillReadResult(
        path=str(path), content=path.read_text(encoding="utf-8"), scope=scope
    )


async def skill_write_handler(_conn: RpcConnection, params: SkillWriteParams, core: Core) -> Ok:
    """``skill.write`` — save the editor form's text verbatim."""
    try:
        name = validate_name(params.name)
    except DefinitionError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    directory = skill_root(
        name, workdir=params.workdir, home=Path(core.paths.home), global_=params.scope == "global"
    )
    try:
        write_skill_document(directory, params.content, requested_name=name, force=True)
    except PublishError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    loader = getattr(core, "skills", None)
    if loader is not None:
        await loader.reload()
    return Ok(ok=True)


def register_skill_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("skill.list", skill_list_handler)
    dispatcher.register("skill.search", skill_search_handler)
    dispatcher.register("skill.install", skill_install_handler)
    dispatcher.register("skill.reload", skill_reload_handler)
    dispatcher.register("skill.remove", skill_remove_handler)
    dispatcher.register("skill.create", skill_create_handler)
    dispatcher.register("skill.read", skill_read_handler)
    dispatcher.register("skill.write", skill_write_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_skill_handlers"]
