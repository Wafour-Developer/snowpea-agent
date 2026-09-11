"""RPC handlers for ``skill.*`` (M6 contract §1).

These sit beside the session handlers for the same reason those do: the daemon
module stays about process lifecycle.  ``register_skill_handlers`` is one line
in ``build_dispatcher``; the loader itself is built by ``wire_core``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    Ok,
    SkillInstallParams,
    SkillListResult,
    SkillRemoveParams,
    SkillSearchParams,
    SkillSearchResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.skills.marketplace import InstallError

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


def register_skill_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("skill.list", skill_list_handler)
    dispatcher.register("skill.search", skill_search_handler)
    dispatcher.register("skill.install", skill_install_handler)
    dispatcher.register("skill.reload", skill_reload_handler)
    dispatcher.register("skill.remove", skill_remove_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_skill_handlers"]
