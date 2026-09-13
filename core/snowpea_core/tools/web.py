"""``web_search`` and ``web_extract``.

``web_search`` asks the configured provider first and then walks the fallback
chain.  When the configured provider could not answer, the result says so in
three places rather than pretending the search went as asked:

* the text handed to the model starts with a concrete fallback reason, for
  example ``[search via ddgs — fallback from exa_free: Exa MCP rate limit …]``
  so the assistant can tell the user in its reply;
* :class:`~snowpea_core.tools.registry.ToolResult` carries ``meta`` with
  ``provider``, ``fallback_from`` and ``reason``;
* the session gets one non-fatal
  ``error{code:"search_provider_unavailable"}`` event, once per session, so the
  TUI shows it without a wall of repeats.

``web_extract`` fetches one page behind the vendored SSRF guard and returns
readable text, truncated to ``settings.tools.max_output_chars``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from snowpea_core.session import events
from snowpea_core.tools import http_util, search_providers
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.tools.search_providers.base import SearchProviderUnavailable

log = logging.getLogger("snowpea.tools.web")

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
DEFAULT_MAX_OUTPUT_CHARS = 20_000

#: Returned when no provider in the chain could answer.
SEARCH_UNAVAILABLE = "search_provider_unavailable"
#: Returned when the SSRF guard or the scheme check rejects a url.
URL_BLOCKED = "url_blocked"

#: Attribute set on a :class:`~snowpea_core.session.session.Session` once the
#: fallback warning has been emitted for it.
NOTIFIED_ATTR = "search_fallback_notified"


def max_output_chars(ctx: ToolContext) -> int:
    settings = getattr(ctx.core, "settings", None)
    value = getattr(getattr(settings, "tools", None), "max_output_chars", None)
    if not isinstance(value, int | str):
        return DEFAULT_MAX_OUTPUT_CHARS
    try:
        return max(1000, int(value))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_CHARS


def configured_provider(settings: Any) -> str:
    """The provider id ``settings`` selects, defaulting to ``ddgs``."""
    value = getattr(getattr(settings, "search", None), "provider", None)
    return str(value) if value else "ddgs"


def unavailable_reason(provider: Any, settings: Any) -> str:
    """Why ``provider`` cannot run right now, in one user-facing sentence."""
    meta = provider.meta
    if meta.key == "key required":
        env = search_providers.credential_env(meta.id)
        return f"{meta.id} needs an API key" + (f" (${env})" if env else "")
    if meta.key == "self-hosted":
        return f"{meta.id} needs its instance URL"
    return f"{meta.id} is not configured"


def search_header(
    provider_id: str, count: int, query: str, fallback_from: str = "", reason: str = ""
) -> str:
    """The first line of the tool output — the model reads this and repeats it."""
    if fallback_from and fallback_from != provider_id:
        prefix = f"[search via {provider_id} — fallback from {fallback_from}: {reason}]"
    else:
        prefix = f"[search via {provider_id}]"
    return f"{prefix} {count} result(s) for: {query}"


async def _warn_once(ctx: ToolContext, message: str) -> None:
    """Emit the non-fatal ``search_provider_unavailable`` event once a session."""
    session = ctx.session
    if getattr(session, NOTIFIED_ATTR, False):
        return
    try:
        setattr(session, NOTIFIED_ATTR, True)
    except AttributeError:  # pragma: no cover - a frozen session stub in a test
        return
    hub = getattr(ctx.core, "hub", None)
    if hub is None:
        return
    await hub.emit_event(session.id, events.error(SEARCH_UNAVAILABLE, message))


async def web_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    try:
        limit = int(args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))

    settings = getattr(ctx.core, "settings", None)
    forced = str(args.get("provider") or "").strip()
    preferred = forced or configured_provider(settings)
    skipped: list[str] = []
    #: Why the *configured* provider dropped out, which is the one the user cares about.
    reason = ""

    for provider in search_providers.chain(preferred):
        pid = provider.meta.id
        bind = getattr(provider, "bind", None)
        if callable(bind):
            bind(settings)
        if not provider.available(settings):
            why = unavailable_reason(provider, settings)
            skipped.append(f"{pid}: {why}")
            if pid == preferred:
                reason = why
            continue
        try:
            hits = await provider.search(query, limit=limit)
        except SearchProviderUnavailable as exc:
            skipped.append(f"{pid}: {exc}")
            if pid == preferred:
                reason = str(exc)
            log.info("web_search provider %s unavailable: %s", pid, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a provider client can raise anything
            skipped.append(f"{pid}: {type(exc).__name__}: {exc}")
            if pid == preferred:
                reason = f"{type(exc).__name__}: {exc}"
            log.info("web_search provider %s failed: %s", pid, exc)
            continue
        if not hits:
            skipped.append(f"{pid}: no results")
            if pid == preferred:
                reason = "it returned no results"
            continue

        fallback_from = preferred if pid != preferred else ""
        if fallback_from:
            await _warn_once(
                ctx,
                f"web_search fell back from {fallback_from} to {pid}: "
                f"{reason or 'it could not answer'}",
            )
        body = "\n\n".join(hit.render() for hit in hits)
        header = search_header(pid, len(hits), query, fallback_from, reason)
        return ToolResult(
            ok=True,
            output=http_util.truncate(f"{header}\n\n{body}", max_output_chars(ctx)),
            meta={
                "provider": pid,
                "fallback_from": fallback_from or None,
                "reason": reason or None,
            },
        )

    detail = "; ".join(skipped) or "no providers registered"
    await _warn_once(ctx, f"web_search found no usable provider ({detail})")
    return ToolResult(
        ok=False,
        error=f"{SEARCH_UNAVAILABLE}: every provider in the chain declined ({detail})",
        meta={"provider": None, "fallback_from": preferred, "reason": reason or detail},
    )


async def web_extract(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    url = str(args.get("url", "")).strip()
    if not url:
        return ToolResult(ok=False, error="url is required")
    if not url.lower().startswith(("http://", "https://")):
        return ToolResult(ok=False, error=f"{URL_BLOCKED}: only http and https urls are fetched")
    if not http_util.is_safe_url(url):
        return ToolResult(
            ok=False,
            error=(
                f"{URL_BLOCKED}: {url} resolves to a private, loopback, link-local or "
                "cloud-metadata address"
            ),
        )

    settings = getattr(ctx.core, "settings", None)
    preferred = getattr(getattr(settings, "search", None), "provider", None)
    provider = search_providers.get(str(preferred)) if preferred else None
    if provider is not None and provider.available(settings):
        bind = getattr(provider, "bind", None)
        if callable(bind):
            bind(settings)
        try:
            text = await provider.extract(url)
        except SearchProviderUnavailable:
            text = None
        if text:
            return ToolResult(
                ok=True, output=http_util.truncate(text, max_output_chars(ctx)), path=url
            )

    try:
        async with http_util.new_client(guard_ssrf=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return ToolResult(ok=False, error=f"fetch failed: {type(exc).__name__}: {exc}")
    except ValueError as exc:  # the guard raises SSRFConnectionBlocked (a ValueError)
        return ToolResult(ok=False, error=f"{URL_BLOCKED}: {exc}")
    if response.status_code >= 400:
        return ToolResult(ok=False, error=f"fetch failed: HTTP {response.status_code}")

    content_type = response.headers.get("content-type", "")
    body = response.text
    text = http_util.html_to_text(body) if "html" in content_type.lower() else body
    if not text.strip():
        return ToolResult(ok=True, output=f"{url} returned no readable text", path=url)
    header = f"{url} ({response.status_code}, {content_type or 'unknown type'})"
    return ToolResult(
        ok=True,
        output=http_util.truncate(f"{header}\n\n{text}", max_output_chars(ctx)),
        path=url,
    )


def annotate(infos: list[Any], settings: Any) -> list[Any]:
    """Fill ``ToolInfo.provider`` for the web tools (``tool.list``).

    The string is the configured id, or ``"exa_free → ddgs"`` when that id
    cannot run, so ``snowpea tools list`` shows the same truth the tool output
    does.
    """
    configured = configured_provider(settings)
    provider = search_providers.get(configured)
    label = configured
    if provider is None:
        label = f"{configured} (unknown)"
    elif not provider.available(settings):
        chain = [p for p in search_providers.chain(configured) if p.meta.id != configured]
        answering = next((p.meta.id for p in chain if p.available(settings)), "none")
        label = f"{configured} → {answering}"
    for info in infos:
        if getattr(info, "name", "") in ("web_search", "web_extract"):
            info.provider = label
    return infos


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="web_search",
        category="web",
        description=(
            "Search the web and return titles, urls and snippets. Uses the configured "
            "provider; if it is not usable the result says which provider answered "
            "instead and why."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {
                    "type": "integer",
                    "description": f"Results to return (default {DEFAULT_LIMIT}).",
                },
                "provider": {
                    "type": "string",
                    "description": "Force one provider id instead of the configured default.",
                },
            },
            "required": ["query"],
        },
        permission="network",
        run=web_search,
    ),
    Tool(
        name="web_extract",
        category="web",
        description="Fetch one http(s) page and return its readable text.",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Page to fetch."}},
            "required": ["url"],
        },
        permission="network",
        run=web_extract,
    ),
)


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "NOTIFIED_ATTR",
    "SEARCH_UNAVAILABLE",
    "TOOLS",
    "URL_BLOCKED",
    "annotate",
    "configured_provider",
    "max_output_chars",
    "search_header",
    "unavailable_reason",
    "web_extract",
    "web_search",
]
