"""Handlers of the assistant features (1.53.0, WP8): presets and the briefing.

``mcp.preset.list|apply|remove`` (capability ``mcpPresets``, logic in
:mod:`snowpea_core.jarvis.presets`) and ``jarvis.briefing.get|set`` (capability
``jarvisBriefing``, :mod:`snowpea_core.jarvis.briefing`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.jarvis import briefing, presets
from snowpea_core.jarvis.service import JarvisError
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    JarvisBriefing,
    JarvisBriefingSetParams,
    McpPresetApplyParams,
    McpPresetApplyResult,
    McpPresetCheckParams,
    McpPresetCheckResult,
    McpPresetListParams,
    McpPresetListResult,
    McpPresetRemoveParams,
    McpPresetRemoveResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "mcp.preset.list",
    "mcp.preset.apply",
    "mcp.preset.remove",
    "mcp.preset.check",
    "jarvis.briefing.get",
    "jarvis.briefing.set",
)


async def preset_list_handler(
    _conn: RpcConnection | None, params: McpPresetListParams, core: Core
) -> McpPresetListResult:
    return await presets.list_presets(core, params.locale)


async def preset_check_handler(
    _conn: RpcConnection | None, params: McpPresetCheckParams, core: Core
) -> McpPresetCheckResult:
    return await presets.check(core, params.id)


async def preset_apply_handler(
    _conn: RpcConnection | None, params: McpPresetApplyParams, core: Core
) -> McpPresetApplyResult:
    return await presets.apply(core, params)


async def preset_remove_handler(
    _conn: RpcConnection | None, params: McpPresetRemoveParams, core: Core
) -> McpPresetRemoveResult:
    return await presets.remove(core, params.id)


async def briefing_get_handler(
    _conn: RpcConnection | None, _params: Empty, core: Core
) -> JarvisBriefing:
    return await briefing.get(core)


async def briefing_set_handler(
    _conn: RpcConnection | None, params: JarvisBriefingSetParams, core: Core
) -> JarvisBriefing:
    from snowpea_core.server.person_gate import snapshot

    try:
        return await briefing.set_briefing(
            core,
            enabled=params.enabled,
            schedule=params.schedule,
            instructions=params.instructions,
            timezone=params.timezone,
            sections=list(params.sections) if params.sections is not None else None,
            # Security review 8, H8-2: a remote login may only turn it off.
            who=snapshot(_conn),
        )
    except JarvisError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc


def register_jarvis_assist_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("mcp.preset.list", preset_list_handler)
    dispatcher.register("mcp.preset.apply", preset_apply_handler)
    dispatcher.register("mcp.preset.remove", preset_remove_handler)
    dispatcher.register("mcp.preset.check", preset_check_handler)
    dispatcher.register("jarvis.briefing.get", briefing_get_handler)
    dispatcher.register("jarvis.briefing.set", briefing_set_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_jarvis_assist_handlers"]
