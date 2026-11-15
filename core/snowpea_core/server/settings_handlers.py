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

from snowpea_core.audio import capabilities as audio_capabilities
from snowpea_core.config import hot_reload
from snowpea_core.config.patch import deep_merge, mask_secrets, reject_masked_secrets
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.audio_handlers import audio_config, speech_caller
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
    stt_catalog,
    tools_catalog,
    tts_catalog,
    vendor_catalog,
    vendor_default_models,
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

_mask_secrets = mask_secrets


#: Merge for ``settings.set``: a dict merges key by key, a list or scalar
#: replaces, and ``null`` **deletes** the key — the only way to remove a model
#: profile, an agent assignment or a team over RPC (CORE-model-assignment).
#: Shared with the ``settings_set`` tool so the two cannot drift.
_deep_merge = deep_merge


#: Global settings only a person at a local connection may change over RPC
#: (security review H2): every Jarvis switch, and the MCP tags that decide
#: which tools read and which send.  Project scope: its ``jarvis`` block.
PERSON_ONLY_KEYS: tuple[tuple[str, ...], ...] = (
    ("jarvis",),
    ("mcp", "permissions"),
    ("mcp", "allowVars"),
)
#: Settings changes written to the Jarvis ledger (who changed what).
_LEDGERED: tuple[tuple[str, ...], ...] = (
    ("jarvis", "delegation", "default"),
    ("jarvis", "external", "clis", "*", "path"),
    ("jarvis", "external", "clis", "*", "defaultMode"),
    ("jarvis", "external", "clis", "*", "passEnv"),
)


def _touches(patch: Any, key: tuple[str, ...]) -> bool:
    node = patch
    for part in key:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _leaves(node: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    if isinstance(node, dict) and node:
        out: list[tuple[tuple[str, ...], Any]] = []
        for key, value in node.items():
            out.extend(_leaves(value, (*prefix, str(key))))
        return out
    return [(prefix, node)]


def _matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    return len(path) >= len(pattern) and all(
        want in ("*", got) for want, got in zip(pattern, path, strict=False)
    )


def guard_patch(conn: Any, patch: dict[str, Any], *, project: bool) -> None:
    """Refuse a patch a caller may not make (security review H2)."""
    from snowpea_core.server.person_gate import require_person

    keys = (("jarvis",),) if project else PERSON_ONLY_KEYS
    touched = [".".join(key) for key in keys if _touches(patch, key)]
    if touched:
        require_person(conn, f"change {', '.join(touched)}", local=True)
    if project or not isinstance(patch.get("jarvis"), dict):
        return
    for path, value in _leaves(patch["jarvis"]):
        full = ("jarvis", *path)
        if _matches(full, ("jarvis", "delegation", "default")) and value not in (None, "staged"):
            raise RpcError(
                errors.INVALID_PARAMS,
                "jarvis.delegation.default can only be 'staged'; to delegate everything, "
                "make a global grant (jarvis.delegation.set {scope: 'global', level: 'full'})",
            )
        if _matches(full, ("jarvis", "external", "clis", "*", "defaultMode")) and (
            value == "unrestricted"
        ):
            raise RpcError(
                errors.INVALID_PARAMS,
                "defaultMode cannot be 'unrestricted'; choose unrestricted for one session "
                "when you open or create it",
            )
        if _matches(full, ("jarvis", "external", "clis", "*", "confirmedPath")):
            raise RpcError(
                errors.INVALID_PARAMS, "confirmedPath is set only by external.clis.confirmPath"
            )


def _ledger_changes(core: Any, conn: Any, patch: dict[str, Any]) -> None:
    """One ledger record per change of a ledgered Jarvis setting (who changed what)."""
    raw = patch.get("jarvis")
    if not isinstance(raw, dict):
        return
    changes = [
        (".".join(("jarvis", *path)), value)
        for path, value in _leaves(raw)
        if any(_matches(("jarvis", *path), pattern) for pattern in _LEDGERED)
    ]
    if not changes:
        return
    try:
        from snowpea_core.jarvis.delegation import engine_for

        ledger = engine_for(core).ledger
        by = str(getattr(conn, "client_kind", None) or "local")
        for key, value in changes:
            ledger.record_decision(
                kind="escalate",
                decided_by="person",
                summary=f"setting {key} changed to {value!r} (settings.set from {by})"[:300],
                reason="settings",
                importance="normal",
            )
    except Exception:  # noqa: BLE001 - the change itself was allowed and saved
        log.warning("could not ledger a Jarvis settings change", exc_info=True)


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
    conn: RpcConnection, params: SettingsSetParams, core: Core
) -> SettingsResult:
    """``settings.set`` — deep-merge ``patch`` into settings and persist it."""
    if not isinstance(params.patch, dict):
        raise RpcError(errors.INVALID_PARAMS, "patch must be an object")
    guard_patch(conn, params.patch, project=params.scope == "project")

    # A client that reads back ``settings.get``'s masked response and echoes
    # it into a patch (naive read-modify-write) must not be able to persist
    # the literal "***" mask as a real credential.
    try:
        reject_masked_secrets(params.patch)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc

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
    unknown = _unknown_profile_vendors(params.patch, updated_settings)
    if unknown:
        raise RpcError(errors.INVALID_PARAMS, unknown)
    raw_jarvis = params.patch.get("jarvis")
    if isinstance(raw_jarvis, dict) and "folder" in raw_jarvis:
        from snowpea_core.jarvis.service import jarvis_folder_problem

        service = getattr(core, "jarvis", None)
        roots = service.projects.known_roots() if service is not None else []
        problem = jarvis_folder_problem(
            updated_settings.jarvis.folder, core.paths.home, roots=roots
        )
        if problem:
            raise RpcError(errors.INVALID_PARAMS, problem)
    updated_settings.save(core.paths)
    _ledger_changes(core, conn, params.patch)
    # The daemon holds the old document in half a dozen places; rebinding here
    # is what makes settings.get and the next turn agree without a restart
    # (CORE-settings-reload).
    changed = hot_reload.changed_keys(current_global, merged_global)
    await core.adopt_settings(updated_settings, changed)
    await _sync_gateways(core)
    return SettingsResult(settings=_mask_secrets(updated_settings.model_dump(mode="json")))


def _unknown_profile_vendors(patch: dict[str, Any], settings: Settings) -> str | None:
    """Why a profile this patch points at cannot run, or ``None`` when every one can.

    Only what the patch touches is checked — a profile it adds, an agent it
    assigns, the default it sets — so an old hand-edited entry elsewhere does
    not block an unrelated write.  Seen live: ``deep-a6000`` -> ``hon2`` was
    accepted while ``hon2`` was not a registered server, and every stage
    routed to it failed with "unknown provider vendor".
    """
    from snowpea_core.providers.presets import PRESETS, local_vendor_ids

    raw_models = patch.get("models")
    raw_agents = patch.get("agents")
    models: dict[str, Any] = raw_models if isinstance(raw_models, dict) else {}
    agents: dict[str, Any] = raw_agents if isinstance(raw_agents, dict) else {}
    touched: set[str] = set()
    if isinstance(models.get("profiles"), dict):
        touched.update(k for k, v in models["profiles"].items() if v is not None)
    if isinstance(models.get("default"), str):
        touched.add(models["default"])
    if isinstance(agents.get("models"), dict):
        touched.update(v for v in agents["models"].values() if isinstance(v, str))
    known = set(PRESETS) | set(local_vendor_ids(settings.providers))
    for profile_id in sorted(touched):
        profile = settings.models.profiles.get(profile_id)
        if profile is None or profile.provider in known:
            continue
        return (
            f"model profile {profile_id!r} uses vendor {profile.provider!r}, which is neither "
            f"a built-in vendor nor a registered server; add it first with "
            f"`snowpea provider add-local {profile.provider} --url <base-url>`"
        )
    return None


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
        installable=item.installable,
        installHint=item.install_hint,
        recommended=item.recommended,
        defaultModel=item.default_model,
        defaultModelSource=item.default_model_source,
    )


async def setup_catalog_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> SetupCatalogResult:
    """``setup.catalog`` — the same catalogs the CLI setup wizard renders."""
    # Detection has to match the CLI's audio screen exactly (CORE-setup-catalog-audio),
    # so this reuses ``audio.capabilities`` rather than re-deriving PATH/key checks.
    report = audio_capabilities(audio_config(core), caller=speech_caller(core))
    # The vendor rows carry a live default model (CORE-default-models); asking
    # here rather than inside ``vendor_catalog`` keeps that function synchronous
    # for the CLI wizard, which has its own budget for the same call.
    defaults = await vendor_default_models(core.settings)
    return SetupCatalogResult(
        vendors=[_to_wire(item) for item in vendor_catalog(core.settings, defaults)],
        search=[_to_wire(item) for item in search_catalog()],
        browser=[_to_wire(item) for item in browser_catalog()],
        tools=[_to_wire(item) for item in tools_catalog()],
        gateway=[_to_wire(item) for item in gateway_catalog()],
        stt=[_to_wire(item) for item in stt_catalog(report["sttProviders"])],
        tts=[_to_wire(item) for item in tts_catalog(report["ttsProviders"])],
    )


def register_settings_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("settings.get", settings_get_handler)
    dispatcher.register("settings.set", settings_set_handler)
    dispatcher.register("setup.catalog", setup_catalog_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_settings_handlers"]
