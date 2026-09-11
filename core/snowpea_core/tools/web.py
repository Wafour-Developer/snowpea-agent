"""``web_search`` and ``web_extract``.

``web_search`` asks the configured provider first and then walks the free chain
from the contract, reporting which provider answered and which ones it skipped.
``web_extract`` fetches one page behind the vendored SSRF guard and returns
readable text, truncated to ``settings.tools.max_output_chars``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

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


def max_output_chars(ctx: ToolContext) -> int:
    settings = getattr(ctx.core, "settings", None)
    value = getattr(getattr(settings, "tools", None), "max_output_chars", None)
    if not isinstance(value, int | str):
        return DEFAULT_MAX_OUTPUT_CHARS
    try:
        return max(1000, int(value))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_CHARS


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
    preferred = str(args.get("provider") or "").strip() or (
        getattr(getattr(settings, "search", None), "provider", None) or "ddgs"
    )
    skipped: list[str] = []
    for provider in search_providers.chain(preferred):
        bind = getattr(provider, "bind", None)
        if callable(bind):
            bind(settings)
        if not provider.available(settings):
            skipped.append(f"{provider.meta.id}: not configured")
            continue
        try:
            hits = await provider.search(query, limit=limit)
        except SearchProviderUnavailable as exc:
            skipped.append(f"{provider.meta.id}: {exc}")
            log.info("web_search provider %s unavailable: %s", provider.meta.id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a provider client can raise anything
            skipped.append(f"{provider.meta.id}: {type(exc).__name__}: {exc}")
            log.info("web_search provider %s failed: %s", provider.meta.id, exc)
            continue
        if not hits:
            skipped.append(f"{provider.meta.id}: no results")
            continue
        body = "\n\n".join(hit.render() for hit in hits)
        header = f"[{provider.meta.id}] {len(hits)} result(s) for: {query}"
        return ToolResult(
            ok=True, output=http_util.truncate(f"{header}\n\n{body}", max_output_chars(ctx))
        )

    detail = "; ".join(skipped) or "no providers registered"
    return ToolResult(
        ok=False,
        error=f"{SEARCH_UNAVAILABLE}: every provider in the chain declined ({detail})",
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
    if provider is not None:
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


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="web_search",
        category="web",
        description=(
            "Search the web and return titles, urls and snippets. Uses the configured "
            "provider, falling back to the free providers."
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
    "SEARCH_UNAVAILABLE",
    "TOOLS",
    "URL_BLOCKED",
    "max_output_chars",
    "web_extract",
    "web_search",
]
