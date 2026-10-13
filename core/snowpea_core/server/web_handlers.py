"""``web.search`` — the core's search providers for a browser host (protocol 1.11.0).

snowpea-browser addendum 21: the host's ``google_search`` tool searches
google.com in the user's browser, and when Google answers with its bot check
or consent page it needs a fallback inside the same call.  That fallback is
the provider chain the ``web_search`` tool uses — same settings, caps and
timeouts (:func:`snowpea_core.tools.web.search_chain`) — not a second search
path in the host.

Only a browser host client (``clientKind`` ``browser`` with a ``clientId``)
may call it; it is not a general way to run core tools.  Results are web
content: the host fences them as untrusted, exactly like Google's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import WebSearchHit, WebSearchParams, WebSearchResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("web.search",)


def _require_browser_host(conn: RpcConnection) -> None:
    is_gateway = str(getattr(conn, "surface_id", "") or "").startswith("gateway:")
    if (
        is_gateway
        or getattr(conn, "client_kind", None) != "browser"
        or not getattr(conn, "client_id", None)
    ):
        raise RpcError(errors.UNAUTHORIZED, "only a browser host client may use web.search")


async def web_search_handler(
    conn: RpcConnection, params: WebSearchParams, core: Core
) -> WebSearchResult:
    """``web.search`` — the configured provider, then the fallback chain."""
    from snowpea_core.tools.web import search_chain

    _require_browser_host(conn)
    query = params.query.strip()
    if not query:
        raise RpcError(errors.INVALID_PARAMS, "query is empty", {"field": "query"})
    outcome = await search_chain(getattr(core, "settings", None), query, params.num)
    if not outcome.provider:
        reason = outcome.detail
        raise RpcError(
            errors.UNAVAILABLE,
            f"no search provider could answer ({reason})",
            {"reason": reason},
        )
    return WebSearchResult(
        results=[
            WebSearchHit(title=hit.title, url=hit.url, snippet=hit.snippet or "")
            for hit in outcome.hits[: params.num]
        ],
        provider=outcome.provider,
    )


def register_web_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("web.search", web_search_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_web_handlers", "web_search_handler"]
