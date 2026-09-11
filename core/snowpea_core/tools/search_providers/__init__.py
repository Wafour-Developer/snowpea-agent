"""The web-search provider catalog (M2 contract §3).

Thirteen ids in a fixed order — the order ``web_search`` walks when it falls
back and the order the setup screen lists.  Four have real HTTP
implementations at M2 (``ddgs``, ``brave_free``, ``tavily``, ``searxng``); the
rest are thin clients that carry their documented endpoint and raise
:class:`SearchProviderUnavailable` until they are wired up.
"""

from __future__ import annotations

from snowpea_core.tools.search_providers.base import (
    Credentials,
    SearchHit,
    SearchProvider,
    SearchProviderMeta,
    SearchProviderUnavailable,
    credentials_for,
)
from snowpea_core.tools.search_providers.providers import (
    BraveFreeProvider,
    DdgsProvider,
    SearxngProvider,
    TavilyProvider,
    ThinProvider,
)

#: Catalog order, straight from the contract table.
PROVIDER_ORDER: tuple[str, ...] = (
    "ddgs",
    "brave_free",
    "exa_free",
    "keenable_free",
    "parallel_free",
    "tavily",
    "searxng",
    "firecrawl_selfhost",
    "exa",
    "keenable",
    "parallel",
    "firecrawl",
    "xai_grok",
)

#: Keyless or self-hosted providers ``web_search`` may fall back onto, in order.
FREE_CHAIN: tuple[str, ...] = (
    "ddgs",
    "brave_free",
    "exa_free",
    "keenable_free",
    "parallel_free",
    "tavily",
    "searxng",
)


def _build() -> dict[str, SearchProvider]:
    providers: list[SearchProvider] = [
        DdgsProvider(),
        BraveFreeProvider(),
        ThinProvider(
            SearchProviderMeta(
                id="exa_free",
                label="Exa Free",
                tier="free",
                key="no key",
                env=("EXA_API_KEY",),
                endpoint="https://api.exa.ai/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="keenable_free",
                label="Keenable Free",
                tier="free",
                key="no key",
                env=("KEENABLE_API_KEY",),
                endpoint="https://api.keenable.ai/v1/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="parallel_free",
                label="Parallel Free",
                tier="free",
                key="no key",
                env=("PARALLEL_API_KEY",),
                endpoint="https://api.parallel.ai/v1beta/search",
            )
        ),
        TavilyProvider(),
        SearxngProvider(),
        ThinProvider(
            SearchProviderMeta(
                id="firecrawl_selfhost",
                label="Firecrawl (self-hosted)",
                tier="free",
                key="self-hosted",
                env=("FIRECRAWL_URL", "FIRECRAWL_API_KEY"),
                endpoint="$FIRECRAWL_URL/v1/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="exa",
                label="Exa",
                tier="paid",
                key="key required",
                env=("EXA_API_KEY",),
                endpoint="https://api.exa.ai/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="keenable",
                label="Keenable",
                tier="paid",
                key="key required",
                env=("KEENABLE_API_KEY",),
                endpoint="https://api.keenable.ai/v1/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="parallel",
                label="Parallel",
                tier="paid",
                key="key required",
                env=("PARALLEL_API_KEY",),
                endpoint="https://api.parallel.ai/v1beta/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="firecrawl",
                label="Firecrawl Cloud",
                tier="paid",
                key="key required",
                env=("FIRECRAWL_API_KEY",),
                endpoint="https://api.firecrawl.dev/v1/search",
            )
        ),
        ThinProvider(
            SearchProviderMeta(
                id="xai_grok",
                label="xAI Grok Search",
                tier="paid",
                key="key required",
                env=("XAI_API_KEY",),
                endpoint="https://api.x.ai/v1/chat/completions",
            )
        ),
    ]
    return {provider.meta.id: provider for provider in providers}


_REGISTRY: dict[str, SearchProvider] = _build()


def get(provider_id: str) -> SearchProvider | None:
    return _REGISTRY.get(provider_id)


def all_providers() -> list[SearchProvider]:
    """Every provider, in catalog order."""
    return [_REGISTRY[pid] for pid in PROVIDER_ORDER if pid in _REGISTRY]


def metas() -> list[SearchProviderMeta]:
    return [provider.meta for provider in all_providers()]


def chain(preferred: str | None) -> list[SearchProvider]:
    """``preferred`` first, then the free chain, each id appearing once."""
    order: list[str] = []
    if preferred and preferred in _REGISTRY:
        order.append(preferred)
    for pid in FREE_CHAIN:
        if pid not in order:
            order.append(pid)
    return [_REGISTRY[pid] for pid in order]


__all__ = [
    "FREE_CHAIN",
    "PROVIDER_ORDER",
    "Credentials",
    "SearchHit",
    "SearchProvider",
    "SearchProviderMeta",
    "SearchProviderUnavailable",
    "all_providers",
    "chain",
    "credentials_for",
    "get",
    "metas",
]
