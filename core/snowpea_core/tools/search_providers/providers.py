"""Provider implementations behind the catalog.

Four are real at M2. :class:`ThinProvider` covers the remaining nine: it knows
its documented endpoint and its credential names, and says so when it cannot
run, which is what the contract asks of the ids that are catalog-only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from snowpea_core.tools import http_util
from snowpea_core.tools.search_providers.base import (
    SearchHit,
    SearchProviderMeta,
    SearchProviderUnavailable,
    credentials_for,
)


def _hit(raw: dict[str, Any], *, title_keys: tuple[str, ...], url_keys: tuple[str, ...],
         snippet_keys: tuple[str, ...]) -> SearchHit | None:
    def pick(keys: tuple[str, ...]) -> str:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    url = pick(url_keys)
    if not url:
        return None
    return SearchHit(title=pick(title_keys) or url, url=url, snippet=pick(snippet_keys))


class _Provider:
    """Shared plumbing: hold the settings the tool layer binds in."""

    meta: SearchProviderMeta

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    def bind(self, settings: Any) -> None:
        """Give the provider the daemon settings it reads credentials from."""
        self._settings = settings

    def available(self, settings: Any = None) -> bool:
        if settings is not None:
            self._settings = settings
        return True

    async def extract(self, url: str) -> str | None:
        """Providers with no extraction endpoint let ``web_extract`` fetch."""
        return None


class ThinProvider(_Provider):
    """Catalog-only provider: correct tags, documented endpoint, clean refusal."""

    def __init__(self, meta: SearchProviderMeta, settings: Any = None) -> None:
        super().__init__(settings)
        self.meta = meta

    def available(self, settings: Any = None) -> bool:
        return False

    def _refuse(self) -> SearchProviderUnavailable:
        return SearchProviderUnavailable(
            f"{self.meta.label} is listed but not implemented yet "
            f"(endpoint {self.meta.endpoint or 'n/a'}; "
            f"credentials: {', '.join(self.meta.env) or 'none'})"
        )

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        raise self._refuse()


class DdgsProvider(_Provider):
    """DuckDuckGo through the keyless ``ddgs`` package; the default."""

    meta = SearchProviderMeta(
        id="ddgs",
        label="DuckDuckGo (ddgs)",
        tier="free",
        key="no key",
        default=True,
        endpoint="https://duckduckgo.com/html/",
    )

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        try:
            from ddgs import DDGS
        except ImportError as exc:  # pragma: no cover - ddgs is a hard dependency
            raise SearchProviderUnavailable("the ddgs package is not installed") from exc

        def _run() -> list[dict[str, Any]]:
            with DDGS() as client:
                return list(client.text(query, max_results=limit))

        try:
            rows = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001 - ddgs raises its own error types
            raise SearchProviderUnavailable(f"ddgs search failed: {exc}") from exc
        hits = [
            _hit(
                row,
                title_keys=("title", "heading"),
                url_keys=("href", "url", "link"),
                snippet_keys=("body", "snippet", "description"),
            )
            for row in rows
            if isinstance(row, dict)
        ]
        return [hit for hit in hits if hit is not None][:limit]


class BraveFreeProvider(_Provider):
    """Brave Search's free tier; needs ``X-Subscription-Token``."""

    meta = SearchProviderMeta(
        id="brave_free",
        label="Brave Search (free tier)",
        tier="free",
        key="key required",
        env=("BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"),
        endpoint="https://api.search.brave.com/res/v1/web/search",
    )

    def available(self, settings: Any = None) -> bool:
        if settings is not None:
            self._settings = settings
        return bool(credentials_for(self.meta, self._settings).api_key)

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        creds = credentials_for(self.meta, self._settings)
        if not creds.api_key:
            raise SearchProviderUnavailable(
                "Brave Search needs an API key (settings.search.credentials.brave_free.api_key "
                "or $BRAVE_API_KEY)"
            )
        async with http_util.new_client() as client:
            response = await client.get(
                self.meta.endpoint,
                params={"q": query, "count": limit},
                headers={
                    "X-Subscription-Token": creds.api_key,
                    "Accept": "application/json",
                },
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(
                    f"Brave Search returned HTTP {response.status_code}"
                )
            payload = response.json()
        rows = (payload.get("web") or {}).get("results") or []
        hits = [
            _hit(
                row,
                title_keys=("title",),
                url_keys=("url",),
                snippet_keys=("description", "snippet"),
            )
            for row in rows
            if isinstance(row, dict)
        ]
        return [hit for hit in hits if hit is not None][:limit]


class TavilyProvider(_Provider):
    """Tavily; the key is optional, so a keyless call is still attempted."""

    meta = SearchProviderMeta(
        id="tavily",
        label="Tavily",
        tier="free",
        key="key optional",
        env=("TAVILY_API_KEY",),
        endpoint="https://api.tavily.com/search",
    )

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        creds = credentials_for(self.meta, self._settings)
        body: dict[str, Any] = {"query": query, "max_results": limit}
        if creds.api_key:
            body["api_key"] = creds.api_key
        headers = {"Content-Type": "application/json"}
        if creds.api_key:
            headers["Authorization"] = f"Bearer {creds.api_key}"
        async with http_util.new_client() as client:
            response = await client.post(self.meta.endpoint, json=body, headers=headers)
            if response.status_code >= 400:
                raise SearchProviderUnavailable(f"Tavily returned HTTP {response.status_code}")
            payload = response.json()
        rows = payload.get("results") or []
        hits = [
            _hit(
                row,
                title_keys=("title",),
                url_keys=("url",),
                snippet_keys=("content", "snippet"),
            )
            for row in rows
            if isinstance(row, dict)
        ]
        return [hit for hit in hits if hit is not None][:limit]


class SearxngProvider(_Provider):
    """A self-hosted SearXNG instance; ``SEARXNG_URL`` points at it."""

    meta = SearchProviderMeta(
        id="searxng",
        label="SearXNG (self-hosted)",
        tier="free",
        key="self-hosted",
        env=("SEARXNG_URL",),
        endpoint="$SEARXNG_URL/search?format=json",
    )

    def _base(self) -> str | None:
        creds = credentials_for(self.meta, self._settings)
        return creds.base_url.rstrip("/") if creds.base_url else None

    def available(self, settings: Any = None) -> bool:
        if settings is not None:
            self._settings = settings
        return self._base() is not None

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        base = self._base()
        if not base:
            raise SearchProviderUnavailable(
                "SearXNG needs an instance URL (settings.search.credentials.searxng.url "
                "or $SEARXNG_URL)"
            )
        async with http_util.new_client() as client:
            response = await client.get(
                f"{base}/search", params={"q": query, "format": "json"}
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(f"SearXNG returned HTTP {response.status_code}")
            payload = response.json()
        rows = payload.get("results") or []
        hits = [
            _hit(
                row,
                title_keys=("title",),
                url_keys=("url",),
                snippet_keys=("content", "snippet"),
            )
            for row in rows
            if isinstance(row, dict)
        ]
        return [hit for hit in hits if hit is not None][:limit]


__all__ = [
    "BraveFreeProvider",
    "DdgsProvider",
    "SearxngProvider",
    "TavilyProvider",
    "ThinProvider",
]
