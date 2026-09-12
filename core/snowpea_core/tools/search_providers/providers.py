"""Provider implementations behind the catalog.

Every id in the catalog has a real HTTP client here — there is no "listed but
not wired up" tier any more.  What separates them is what each one *needs*:

* keyless          ``ddgs``, ``firecrawl`` (its cloud ``/v1/search`` answers
                   without credentials, rate-limited)
* an API key       ``brave_free``, ``tavily``, ``exa``/``exa_free``,
                   ``keenable``/``keenable_free``, ``parallel``/``parallel_free``,
                   ``xai_grok``
* a base URL       ``searxng``, ``firecrawl_selfhost``

A provider that is missing what it needs reports ``available() is False`` and,
if called anyway, raises :class:`SearchProviderUnavailable` with the name of
the setting and the environment variable that would fix it.  ``web_search``
turns that sentence into the reason it shows the user.

The "free" ids that need a key (``exa_free``, ``keenable_free``,
``parallel_free``) are free *tiers* of a keyed product, not keyless endpoints:
``POST https://api.exa.ai/search`` answers ``402``, Parallel ``401 no API key``
and Keenable ``401 missing authentication`` when asked without credentials.
They are tagged ``free · key required`` for exactly that reason.
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

#: How long one provider may take before ``web_search`` moves down the chain.
REQUEST_TIMEOUT = 20.0


def _hit(raw: dict[str, Any], *, title_keys: tuple[str, ...], url_keys: tuple[str, ...],
         snippet_keys: tuple[str, ...]) -> SearchHit | None:
    def pick(keys: tuple[str, ...]) -> str:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                joined = " ".join(str(part).strip() for part in value if str(part).strip())
                if joined:
                    return joined
        return ""

    url = pick(url_keys)
    if not url:
        return None
    return SearchHit(title=pick(title_keys) or url, url=url, snippet=pick(snippet_keys))


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """The first list of objects found under ``keys`` (``"a.b"`` walks in)."""
    for key in keys:
        node: Any = payload
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, list):
            return [row for row in node if isinstance(row, dict)]
    return []


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

    # -- credential helpers -------------------------------------------
    def _creds(self, settings: Any = None) -> Any:
        if settings is not None:
            self._settings = settings
        return credentials_for(self.meta, self._settings)

    def needs_key(self) -> str:
        """The sentence shown when this provider has no API key."""
        env = " or ".join(f"${name}" for name in self.meta.env if not name.endswith("_URL"))
        return (
            f"{self.meta.label} needs an API key "
            f"(settings.search.credentials.{self.meta.id}.api_key"
            + (f" or {env}" if env else "")
            + ")"
        )

    def needs_url(self) -> str:
        env = " or ".join(f"${name}" for name in self.meta.env if name.endswith("_URL"))
        return (
            f"{self.meta.label} needs a base URL "
            f"(settings.search.credentials.{self.meta.id}.url"
            + (f" or {env}" if env else "")
            + ")"
        )


class _KeyedProvider(_Provider):
    """A provider that cannot run at all without an API key."""

    def available(self, settings: Any = None) -> bool:
        return bool(self._creds(settings).api_key)

    def _require_key(self) -> str:
        key = self._creds().api_key
        if not key:
            raise SearchProviderUnavailable(self.needs_key())
        return key


class ThinProvider(_Provider):
    """A catalog entry with no client.

    Nothing in the catalog uses this any more — every id has a real
    implementation.  It stays so a plugin can still register a placeholder and
    get an honest refusal rather than a silent fallback.
    """

    def __init__(self, meta: SearchProviderMeta, settings: Any = None) -> None:
        super().__init__(settings)
        self.meta = meta

    def available(self, settings: Any = None) -> bool:
        return False

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        raise SearchProviderUnavailable(
            f"{self.meta.label} is listed but not implemented "
            f"(endpoint {self.meta.endpoint or 'n/a'})"
        )


class DdgsProvider(_Provider):
    """DuckDuckGo through the keyless ``ddgs`` package; the default.

    The only provider in the catalog that needs no key and no URL.
    """

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


class BraveFreeProvider(_KeyedProvider):
    """Brave Search's free tier; needs ``X-Subscription-Token``."""

    meta = SearchProviderMeta(
        id="brave_free",
        label="Brave Search (free tier)",
        tier="free",
        key="key required",
        env=("BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"),
        endpoint="https://api.search.brave.com/res/v1/web/search",
    )

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        key = self._require_key()
        async with http_util.new_client() as client:
            response = await client.get(
                self.meta.endpoint,
                params={"q": query, "count": limit},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(
                    f"Brave Search returned HTTP {response.status_code}"
                )
            payload = response.json()
        return _collect(
            _rows(payload, "web.results"),
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("description", "snippet"),
            limit=limit,
        )


class TavilyProvider(_KeyedProvider):
    """Tavily.

    The key is not optional: ``POST https://api.tavily.com/search`` answers
    ``401 Unauthorized: missing or invalid API key`` without one.
    """

    meta = SearchProviderMeta(
        id="tavily",
        label="Tavily",
        tier="free",
        key="key required",
        env=("TAVILY_API_KEY",),
        endpoint="https://api.tavily.com/search",
    )

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        key = self._require_key()
        async with http_util.new_client() as client:
            response = await client.post(
                self.meta.endpoint,
                json={"query": query, "max_results": limit},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(f"Tavily returned HTTP {response.status_code}")
            payload = response.json()
        return _collect(
            _rows(payload, "results"),
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("content", "snippet"),
            limit=limit,
        )


class ExaProvider(_KeyedProvider):
    """Exa.

    There is no keyless Exa: the documented endpoint answers ``402 Payment
    required`` without an ``x-api-key``, so ``exa_free`` is the free *tier* of
    a keyed product and carries the same client as ``exa``.
    """

    def __init__(self, meta: SearchProviderMeta, settings: Any = None) -> None:
        super().__init__(settings)
        self.meta = meta

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        key = self._require_key()
        async with http_util.new_client() as client:
            response = await client.post(
                self.meta.endpoint,
                json={
                    "query": query,
                    "numResults": limit,
                    "contents": {"text": {"maxCharacters": 600}},
                },
                headers={"x-api-key": key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(
                    f"Exa returned HTTP {response.status_code}"
                    + (" (the key is missing or out of credit)" if response.status_code
                       in (401, 402, 403) else "")
                )
            payload = response.json()
        return _collect(
            _rows(payload, "results"),
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("text", "snippet", "summary", "highlights"),
            limit=limit,
        )


class KeenableProvider(_KeyedProvider):
    """Keenable; ``X-API-Key`` is mandatory (the endpoint answers ``401``)."""

    def __init__(self, meta: SearchProviderMeta, settings: Any = None) -> None:
        super().__init__(settings)
        self.meta = meta

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        key = self._require_key()
        async with http_util.new_client() as client:
            response = await client.post(
                self.meta.endpoint,
                json={"query": query, "limit": limit},
                headers={
                    "X-API-Key": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(
                    f"Keenable returned HTTP {response.status_code}"
                )
            payload = response.json()
        return _collect(
            _rows(payload, "results", "data", "hits"),
            title_keys=("title", "name"),
            url_keys=("url", "link"),
            snippet_keys=("snippet", "content", "description", "text"),
            limit=limit,
        )


class ParallelProvider(_KeyedProvider):
    """Parallel's Search API; ``x-api-key`` is mandatory (``401`` without)."""

    def __init__(self, meta: SearchProviderMeta, settings: Any = None) -> None:
        super().__init__(settings)
        self.meta = meta

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        key = self._require_key()
        async with http_util.new_client() as client:
            response = await client.post(
                self.meta.endpoint,
                json={
                    "objective": query,
                    "search_queries": [query],
                    "processor": "base",
                    "max_results": limit,
                },
                headers={"x-api-key": key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(
                    f"Parallel returned HTTP {response.status_code}"
                )
            payload = response.json()
        return _collect(
            _rows(payload, "results", "search_results"),
            title_keys=("title", "name"),
            url_keys=("url",),
            snippet_keys=("excerpts", "snippet", "content", "text"),
            limit=limit,
        )


class FirecrawlProvider(_Provider):
    """Firecrawl's ``/v1/search``.

    Two ids share this client.  ``firecrawl_selfhost`` needs ``FIRECRAWL_URL``
    and refuses without it; ``firecrawl`` talks to the cloud endpoint, which
    answers keyless requests (rate-limited) and returns more with a key — hence
    ``key optional`` rather than ``no key``.
    """

    def __init__(
        self, meta: SearchProviderMeta, *, needs_url: bool, settings: Any = None
    ) -> None:
        super().__init__(settings)
        self.meta = meta
        self._needs_url = needs_url

    def _base(self) -> str | None:
        creds = self._creds()
        if creds.base_url:
            return creds.base_url.rstrip("/")
        return None if self._needs_url else "https://api.firecrawl.dev"

    def available(self, settings: Any = None) -> bool:
        if settings is not None:
            self._settings = settings
        return self._base() is not None

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        base = self._base()
        if not base:
            raise SearchProviderUnavailable(self.needs_url())
        creds = self._creds()
        headers = {"Content-Type": "application/json"}
        if creds.api_key:
            headers["Authorization"] = f"Bearer {creds.api_key}"
        async with http_util.new_client() as client:
            response = await client.post(
                f"{base}/v1/search",
                json={"query": query, "limit": limit},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(
                    f"Firecrawl returned HTTP {response.status_code}"
                )
            payload = response.json()
        return _collect(
            _rows(payload, "data", "results", "data.web"),
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("description", "snippet", "markdown"),
            limit=limit,
        )


class XaiGrokProvider(_KeyedProvider):
    """xAI's live search, reached through ``chat/completions``.

    The answer is a model reply plus citations; each citation becomes a hit so
    the shape matches every other provider.
    """

    meta = SearchProviderMeta(
        id="xai_grok",
        label="xAI Grok Search",
        tier="paid",
        key="key required",
        env=("XAI_API_KEY",),
        endpoint="https://api.x.ai/v1/chat/completions",
    )

    #: Model used for the search call; overridable per install.
    model = "grok-4-fast"

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        key = self._require_key()
        creds = self._creds()
        model = str(creds.extra.get("model") or self.model)
        async with http_util.new_client() as client:
            response = await client.post(
                self.meta.endpoint,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": query}],
                    "search_parameters": {
                        "mode": "on",
                        "return_citations": True,
                        "max_search_results": limit,
                    },
                },
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(f"xAI returned HTTP {response.status_code}")
            payload = response.json()
        citations = payload.get("citations")
        summary = ""
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                summary = message["content"].strip()
        hits: list[SearchHit] = []
        if isinstance(citations, list):
            for raw in citations[:limit]:
                if isinstance(raw, str) and raw.strip():
                    hits.append(SearchHit(title=raw.strip(), url=raw.strip()))
                elif isinstance(raw, dict):
                    hit = _hit(
                        raw,
                        title_keys=("title",),
                        url_keys=("url",),
                        snippet_keys=("snippet", "text"),
                    )
                    if hit is not None:
                        hits.append(hit)
        if hits and summary:
            hits[0] = SearchHit(title=hits[0].title, url=hits[0].url, snippet=summary)
        if not hits and summary:
            raise SearchProviderUnavailable("xAI answered without any citation to return")
        return hits[:limit]


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
        creds = self._creds()
        return creds.base_url.rstrip("/") if creds.base_url else None

    def available(self, settings: Any = None) -> bool:
        if settings is not None:
            self._settings = settings
        return self._base() is not None

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        base = self._base()
        if not base:
            raise SearchProviderUnavailable(self.needs_url())
        async with http_util.new_client() as client:
            response = await client.get(
                f"{base}/search",
                params={"q": query, "format": "json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code >= 400:
                raise SearchProviderUnavailable(f"SearXNG returned HTTP {response.status_code}")
            payload = response.json()
        return _collect(
            _rows(payload, "results"),
            title_keys=("title",),
            url_keys=("url",),
            snippet_keys=("content", "snippet"),
            limit=limit,
        )


def _collect(
    rows: list[dict[str, Any]],
    *,
    title_keys: tuple[str, ...],
    url_keys: tuple[str, ...],
    snippet_keys: tuple[str, ...],
    limit: int,
) -> list[SearchHit]:
    hits = [
        _hit(row, title_keys=title_keys, url_keys=url_keys, snippet_keys=snippet_keys)
        for row in rows
    ]
    return [hit for hit in hits if hit is not None][:limit]


__all__ = [
    "REQUEST_TIMEOUT",
    "BraveFreeProvider",
    "DdgsProvider",
    "ExaProvider",
    "FirecrawlProvider",
    "KeenableProvider",
    "ParallelProvider",
    "SearxngProvider",
    "TavilyProvider",
    "ThinProvider",
    "XaiGrokProvider",
]
