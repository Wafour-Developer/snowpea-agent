"""RPC handlers for site memory (protocol 1.9.0, snowpea-browser addendum 20).

``site.get`` / ``site.put`` / ``site.mark`` belong to the browser host; ``site.list``
and ``site.delete`` also serve a settings UI.  Every entry lives in one browser
profile — the browser client's ``clientId``, the same id its sessions carry as
``hostToolsFrom`` — and no call reaches across profiles.  Validation, limits and
retention rules are in :mod:`snowpea_core.session.site_memory`; the rows are in
``state.db`` (:mod:`snowpea_core.session.store`).

Entries are page-derived, untrusted data: they are stored and handed back to
the host, which fences them for the model.  Nothing here puts them in a prompt.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    SiteDeleteParams,
    SiteDeleteResult,
    SiteEntry,
    SiteGetParams,
    SiteGetResult,
    SiteListParams,
    SiteListResult,
    SiteMarkParams,
    SiteMarkResult,
    SitePutParams,
    SitePutResult,
    SiteSummary,
)
from snowpea_core.session import site_memory as sm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger("snowpea.site")


def _invalid(exc: sm.SiteMemoryError) -> RpcError:
    return RpcError(errors.INVALID_PARAMS, str(exc), {"field": exc.field})


def _is_gateway(conn: RpcConnection) -> bool:
    return str(getattr(conn, "surface_id", "") or "").startswith("gateway:")


def _host_profile(conn: RpcConnection) -> str:
    """The profile of a browser host; anyone else may not read or teach site memory.

    A browser-kind client with a ``clientId`` is the only client that can be a
    session's ``hostToolsFrom`` (``session.attach`` checks the same), so that
    id is the profile.  The agent never calls these directly: the host's REPL
    helpers do, after their own checks.
    """
    client_id = getattr(conn, "client_id", None)
    if _is_gateway(conn) or getattr(conn, "client_kind", None) != "browser" or not client_id:
        raise RpcError(errors.UNAUTHORIZED, "only a browser host client may use site memory")
    return str(client_id)


def _ui_profile(conn: RpcConnection, named: str | None) -> str:
    """The profile ``site.list`` / ``site.delete`` act on.

    A browser client acts on its own profile only.  Another owner surface (the
    desktop, the TUI) names the profile; one call never spans two profiles.
    """
    if _is_gateway(conn):
        raise RpcError(errors.UNAUTHORIZED, "a chat gateway may not manage site memory")
    if getattr(conn, "client_kind", None) == "browser":
        own = getattr(conn, "client_id", None)
        if not own:
            raise RpcError(
                errors.UNAUTHORIZED, "a browser client without a clientId has no profile"
            )
        if named is not None and named != own:
            raise RpcError(errors.UNAUTHORIZED, "a browser client may only manage its own profile")
        return str(own)
    if not named:
        raise RpcError(
            errors.INVALID_PARAMS,
            "hostToolsFrom must name the browser profile",
            {"field": "hostToolsFrom"},
        )
    return named


def _require_enabled(core: Core) -> None:
    """``browser.siteMemory`` off: no reading, teaching or counting.

    ``tool_inactive`` is the existing code for a capability the user switched
    off, so a host can tell it from a refused entry (``invalid_params``).
    """
    if not core.settings.browser.siteMemory:
        raise RpcError(errors.TOOL_INACTIVE, "site memory is turned off")


def _store(core: Core) -> Any:
    if core.store is None:
        raise RpcError(errors.NOT_IMPLEMENTED, "site memory needs the state store")
    return core.store


def _origin(value: str, field: str = "origin") -> str:
    try:
        return sm.normalize_origin(value, field)
    except sm.SiteMemoryError as exc:
        raise _invalid(exc) from exc


async def _sweep(store: Any, profile: str, now: datetime) -> None:
    """Retention runs on access: every call first drops the profile's expired entries."""
    unverified_before, no_success_before = sm.retention_cutoffs(now)
    dropped = await store.site_sweep(
        profile, unverified_before, no_success_before, sm.STALE_FAILURES
    )
    if dropped:
        log.info("site memory: dropped %d expired entr%s", dropped, "y" if dropped == 1 else "ies")


async def site_get_handler(conn: RpcConnection, params: SiteGetParams, core: Core) -> SiteGetResult:
    """``site.get`` — an origin's entries, most specific urlPattern first."""
    profile = _host_profile(conn)
    _require_enabled(core)
    store = _store(core)
    origin = _origin(params.origin)
    if params.path is not None and not params.path.startswith("/"):
        raise _invalid(sm.SiteMemoryError("path", "must start with '/'"))
    await _sweep(store, profile, datetime.now(UTC))
    entries = await store.site_entries(profile, origin)
    if params.pageType is not None:
        entries = [e for e in entries if e["pageType"] == params.pageType]
    if params.path is not None:
        entries = [e for e in entries if sm.pattern_matches(e["urlPattern"], params.path)]
    return SiteGetResult(entries=[SiteEntry(**e) for e in sm.order_entries(entries)])


async def site_put_handler(conn: RpcConnection, params: SitePutParams, core: Core) -> SitePutResult:
    """``site.put`` — upsert by (origin, pageType); a put is a success and clears stale."""
    profile = _host_profile(conn)
    _require_enabled(core)
    store = _store(core)
    origin = _origin(params.origin)
    data = params.entry.model_dump(mode="json")
    now = datetime.now(UTC)
    now_iso = sm.iso(now)
    try:
        sm.check_entry(data)
        verified = now
        if data.get("lastVerified"):
            # A host clock ahead of ours must not keep an entry past retention.
            verified = min(sm.parse_time(data["lastVerified"], "entry.lastVerified"), now)
    except sm.SiteMemoryError as exc:
        raise _invalid(exc) from exc

    def build(existing: dict[str, Any] | None) -> dict[str, Any]:
        entry = {
            **data,
            "entryId": existing["entryId"] if existing else f"e_{secrets.token_hex(8)}",
            "origin": origin,
            "createdAt": existing["createdAt"] if existing else now_iso,
            "lastVerified": sm.iso(verified),
            "successCount": (int(existing["successCount"]) if existing else 0) + 1,
            "failureCount": int(existing["failureCount"]) if existing else 0,
            "stale": False,
        }
        sm.check_size(entry)
        entry["_lastSuccess"] = now_iso
        return entry

    await _sweep(store, profile, now)
    try:
        entry, created, evicted = await store.site_put(
            profile,
            origin,
            data["pageType"],
            build,
            max_per_origin=sm.MAX_ENTRIES_PER_ORIGIN,
            max_origins=sm.MAX_ORIGINS_PER_PROFILE,
        )
    except sm.SiteMemoryError as exc:
        raise _invalid(exc) from exc
    except OverflowError as exc:
        raise _invalid(
            sm.SiteMemoryError(
                "entry.pageType",
                f"{origin} already has {sm.MAX_ENTRIES_PER_ORIGIN} entries; "
                "replace or delete one first",
            )
        ) from exc
    if evicted:
        log.info("site memory: evicted %d least recently verified origin(s)", len(evicted))
    return SitePutResult(entryId=entry["entryId"], created=created)


async def site_mark_handler(
    conn: RpcConnection, params: SiteMarkParams, core: Core
) -> SiteMarkResult:
    """``site.mark`` — count a verified use (``ok``) or a failure (``stale``)."""
    profile = _host_profile(conn)
    _require_enabled(core)
    store = _store(core)
    origin = _origin(params.origin)
    if params.detail:
        try:
            sm.check_text(params.detail, "detail")
        except sm.SiteMemoryError as exc:
            raise _invalid(exc) from exc
    now = datetime.now(UTC)
    now_iso = sm.iso(now)

    def change(entry: dict[str, Any]) -> dict[str, Any]:
        if params.outcome == "ok":
            entry["successCount"] = int(entry["successCount"]) + 1
            entry["lastVerified"] = now_iso
            entry["stale"] = False
            entry["_lastSuccess"] = now_iso
        else:
            entry["failureCount"] = int(entry["failureCount"]) + 1
            entry["stale"] = True
        return entry

    await _sweep(store, profile, now)
    entry = await store.site_update(profile, origin, params.entryId, change)
    if entry is None:
        raise RpcError(errors.NOT_FOUND, f"no site entry {params.entryId} for {origin}")
    log.info(
        "site memory: %s marked %s%s",
        params.entryId,
        params.outcome,
        f" ({params.detail})" if params.detail else "",
    )
    return SiteMarkResult(entry=SiteEntry(**entry))


async def site_list_handler(
    conn: RpcConnection, params: SiteListParams, core: Core
) -> SiteListResult:
    """``site.list`` — one profile's remembered sites, by origin, paged."""
    profile = _ui_profile(conn, params.hostToolsFrom)
    store = _store(core)
    origin = _origin(params.origin) if params.origin is not None else None
    await _sweep(store, profile, datetime.now(UTC))
    rows = await store.site_list(profile, origin, params.cursor, params.limit + 1)
    page = rows[: params.limit]
    return SiteListResult(
        sites=[
            SiteSummary(
                origin=row["origin"],
                entries=int(row["entries"]),
                lastVerified=row["last_verified"],
                stale=int(row["stale"] or 0),
                pageTypes=sorted(set(str(row["page_types"] or "").split(",")) - {""}),
                successCount=int(row["success_count"] or 0),
                failureCount=int(row["failure_count"] or 0),
            )
            for row in page
        ],
        cursor=page[-1]["origin"] if len(rows) > params.limit else None,
    )


async def site_delete_handler(
    conn: RpcConnection, params: SiteDeleteParams, core: Core
) -> SiteDeleteResult:
    """``site.delete`` — one entry, or every entry of an origin, in one profile."""
    profile = _ui_profile(conn, params.hostToolsFrom)
    store = _store(core)
    origin = _origin(params.origin)
    return SiteDeleteResult(deleted=await store.site_delete(profile, origin, params.entryId))


def register_site_handlers(dispatcher: RpcDispatcher) -> None:
    dispatcher.register("site.get", site_get_handler)
    dispatcher.register("site.put", site_put_handler)
    dispatcher.register("site.mark", site_mark_handler)
    dispatcher.register("site.list", site_list_handler)
    dispatcher.register("site.delete", site_delete_handler)


__all__ = ["register_site_handlers"]
