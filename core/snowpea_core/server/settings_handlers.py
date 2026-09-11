"""RPC handlers for ``settings.*`` and ``setup.*`` (M8, additive to protocol 1.1.0).

Settings persist through the same pydantic models the daemon already loads at
startup: :class:`~snowpea_core.config.settings.Settings` for the global scope
and :class:`~snowpea_core.config.project.ProjectSettings` for the project one.
A ``settings.set`` patch is deep-merged onto the *current* document, validated
by re-running it through the model (so a bad patch is ``invalid_params``, not
a corrupt file), then persisted and echoed back with secrets masked.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from snowpea_core.config import hot_reload
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    Empty,
    SettingsGetParams,
    SettingsResult,
    SettingsSetParams,
    SetupCatalogItem,
    SetupCatalogResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.setup.catalog import (
    CatalogItem,
    browser_catalog,
    gateway_catalog,
    search_catalog,
    tools_catalog,
    vendor_catalog,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.server.settings")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = (
    "settings.get",
    "settings.set",
    "setup.catalog",
)

#: Field names masked in every ``settings.get`` / ``settings.set`` response.
_SECRET_KEYS = {"api_key", "token", "refresh_token", "password"}
_MASK = "***"


def _mask_secrets(value: Any) -> Any:
    """Recursively replace secret-named fields with ``"***"`` for the wire.

    Never mutates ``value``; used only on the response, so the persisted file
    keeps the real credentials.
    """
    if isinstance(value, dict):
        return {
            key: (_MASK if key in _SECRET_KEYS and val is not None else _mask_secrets(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into ``base``, recursing into nested dicts only.

    A list or scalar in the patch replaces the corresponding value wholesale
    (no list-merging heuristics); a dict merges key by key.
    """
    merged = dict(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _require_workdir(params: SettingsGetParams | SettingsSetParams) -> str:
    if not params.workdir:
        raise RpcError(errors.INVALID_PARAMS, 'workdir is required when scope is "project"')
    return params.workdir


async def settings_get_handler(
    _conn: RpcConnection, params: SettingsGetParams, core: Core
) -> SettingsResult:
    """``settings.get`` — read the global or project settings document."""
    if params.scope == "project":
        workdir = _require_workdir(params)
        project = ProjectSettings.load(workdir)
        return SettingsResult(settings=_mask_secrets(project.model_dump(mode="json")))
    return SettingsResult(settings=_mask_secrets(core.settings.model_dump(mode="json")))


async def settings_set_handler(
    _conn: RpcConnection, params: SettingsSetParams, core: Core
) -> SettingsResult:
    """``settings.set`` — deep-merge ``patch`` into settings and persist it."""
    if not isinstance(params.patch, dict):
        raise RpcError(errors.INVALID_PARAMS, "patch must be an object")

    if params.scope == "project":
        workdir = _require_workdir(params)
        current = ProjectSettings.load(workdir)
        merged = _deep_merge(current.model_dump(mode="json"), params.patch)
        try:
            updated = ProjectSettings.model_validate(merged)
        except ValidationError as exc:
            raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
        updated.save(workdir)
        return SettingsResult(settings=_mask_secrets(updated.model_dump(mode="json")))

    current_global = core.settings.model_dump(mode="json")
    merged_global = _deep_merge(current_global, params.patch)
    try:
        updated_settings = Settings.model_validate(merged_global)
    except ValidationError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    updated_settings.save(core.paths)
    # The daemon holds the old document in half a dozen places; rebinding here
    # is what makes settings.get and the next turn agree without a restart
    # (CORE-settings-reload).
    changed = hot_reload.changed_keys(current_global, merged_global)
    await core.adopt_settings(updated_settings, changed)
    await _sync_gateways(core)
    return SettingsResult(settings=_mask_secrets(updated_settings.model_dump(mode="json")))


async def _sync_gateways(core: Core) -> None:
    """Let a ``settings.gateway`` change take effect without a daemon restart.

    A messenger enabled here starts listening immediately, and one disabled
    here stops.  A failure is logged rather than raised: the settings write
    already succeeded, and the next daemon start syncs again.
    """
    router = getattr(core, "gateway", None)
    if router is None:
        return
    try:
        await router.sync_from_settings(core.settings)
    except Exception as exc:  # noqa: BLE001 - a dead platform must not fail the write
        log.warning("could not apply the messenger settings: %s", exc)


def _to_wire(item: CatalogItem) -> SetupCatalogItem:
    return SetupCatalogItem(
        id=item.id,
        label=item.label,
        tier=item.tier,
        key=item.key,
        default=item.default,
        description=item.description,
        active=item.active,
        tags=list(item.tags),
    )


async def setup_catalog_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> SetupCatalogResult:
    """``setup.catalog`` — the same catalogs the CLI setup wizard renders."""
    return SetupCatalogResult(
        vendors=[_to_wire(item) for item in vendor_catalog(core.settings)],
        search=[_to_wire(item) for item in search_catalog()],
        browser=[_to_wire(item) for item in browser_catalog()],
        tools=[_to_wire(item) for item in tools_catalog()],
        gateway=[_to_wire(item) for item in gateway_catalog()],
    )


def register_settings_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("settings.get", settings_get_handler)
    dispatcher.register("settings.set", settings_set_handler)
    dispatcher.register("setup.catalog", setup_catalog_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_settings_handlers"]
