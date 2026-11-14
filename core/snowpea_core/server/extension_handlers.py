"""``extension.*`` (1.49.0, capability ``extensions``; M16 Phase 3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    ExtensionConfigureParams,
    ExtensionInfo,
    ExtensionListResult,
    ExtensionNameParams,
    ExtensionResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

HANDLED_METHODS: tuple[str, ...] = (
    "extension.list",
    "extension.enable",
    "extension.disable",
    "extension.reload",
    "extension.configure",
)


def _manager(core: Core) -> Any:
    manager = getattr(core, "extensions", None)
    if manager is None:
        from snowpea_core.extensions.manager import ExtensionManager

        manager = core.extensions = ExtensionManager(core)
    return manager


def _info(core: Core, ext: Any) -> ExtensionInfo:
    data = ext.info()
    config = (core.settings.extensions.get(ext.name) or {}).get("config") or {}
    return ExtensionInfo(**data, config=dict(config))


def _named(core: Core, name: str) -> Any:
    manager = _manager(core)
    manager.discover()
    if name not in manager.extensions:
        raise RpcError(errors.NOT_FOUND, f"no extension {name!r} in {manager.root}")
    return manager


async def _save(core: Core) -> None:
    try:
        core.settings.save(core.paths)
    except OSError as exc:  # pragma: no cover - disk failure
        raise RpcError(errors.INTERNAL, f"could not write settings.json: {exc}") from exc
    mark = getattr(core, "mark_settings_saved", None)
    if callable(mark):
        mark()


async def extension_list_handler(
    _conn: RpcConnection | None, _params: Empty, core: Core
) -> ExtensionListResult:
    manager = _manager(core)
    manager.discover()
    return ExtensionListResult(
        extensions=[_info(core, ext) for ext in manager.extensions.values()]
    )


async def extension_enable_handler(
    _conn: RpcConnection | None, params: ExtensionNameParams, core: Core
) -> ExtensionResult:
    manager = _named(core, params.name)
    ext = manager.set_enabled(params.name, True)
    await _save(core)
    return ExtensionResult(extension=_info(core, ext))


async def extension_disable_handler(
    _conn: RpcConnection | None, params: ExtensionNameParams, core: Core
) -> ExtensionResult:
    manager = _named(core, params.name)
    ext = manager.set_enabled(params.name, False)
    await _save(core)
    return ExtensionResult(extension=_info(core, ext))


async def extension_reload_handler(
    _conn: RpcConnection | None, params: ExtensionNameParams, core: Core
) -> ExtensionResult:
    manager = _named(core, params.name)
    ext = manager.reload(params.name)
    return ExtensionResult(extension=_info(core, ext))


async def extension_configure_handler(
    _conn: RpcConnection | None, params: ExtensionConfigureParams, core: Core
) -> ExtensionResult:
    manager = _named(core, params.name)
    ext = manager.extensions[params.name]
    schema = ext.manifest.get("configSchema")
    if isinstance(schema, dict):
        import jsonschema

        try:
            jsonschema.validate(params.config, schema)
        except jsonschema.ValidationError as exc:
            raise RpcError(errors.INVALID_PARAMS, f"config: {exc.message}") from exc
    core.settings.extensions.setdefault(params.name, {})["config"] = dict(params.config)
    await _save(core)
    ext = manager.reload(params.name) if ext.enabled else ext
    return ExtensionResult(extension=_info(core, ext))


def register_extension_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("extension.list", extension_list_handler)
    dispatcher.register("extension.enable", extension_enable_handler)
    dispatcher.register("extension.disable", extension_disable_handler)
    dispatcher.register("extension.reload", extension_reload_handler)
    dispatcher.register("extension.configure", extension_configure_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_extension_handlers"]
