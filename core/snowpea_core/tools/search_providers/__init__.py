"""The web-search provider catalog (M2 contract §3).

Thirteen ids in a fixed order — the order the setup screen lists and the order
``web_search`` walks when it falls back.  Every one of them has a real HTTP
client (see :mod:`.providers`); what differs is what each needs before it can
run, and the catalog tag says so honestly:

===========================  =========================  ===================
id                           tag                        needs
===========================  =========================  ===================
``ddgs``                     free · no key              nothing
``firecrawl``                paid · key optional        nothing (keyless, rate-limited)
``brave_free``               free · key required        ``BRAVE_API_KEY``
``exa_free`` / ``exa``       free|paid · key required   ``EXA_API_KEY``
``keenable_free``/``keenable`` free|paid · key required ``KEENABLE_API_KEY``
``parallel_free``/``parallel`` free|paid · key required ``PARALLEL_API_KEY``
``tavily``                   free · key required        ``TAVILY_API_KEY``
``xai_grok``                 paid · key required        ``XAI_API_KEY``
``searxng``                  free · self-hosted         ``SEARXNG_URL``
``firecrawl_selfhost``       free · self-hosted         ``FIRECRAWL_URL``
===========================  =========================  ===================

The ``*_free`` ids used to be tagged ``no key``, which made ``web_search``
silently fall through to ddgs while the user believed their choice was in use.
They are free *tiers* of keyed products — the endpoints answer ``402`` (Exa) or
``401`` (Parallel, Keenable, Tavily) without credentials — so they are tagged
``key required`` and :meth:`available` is false until a key is configured.
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
    ExaProvider,
    FirecrawlProvider,
    KeenableProvider,
    ParallelProvider,
    SearxngProvider,
    TavilyProvider,
    ThinProvider,
    XaiGrokProvider,
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

#: Providers ``web_search`` may fall back onto, in order.  Each is still
#: gated by :meth:`SearchProvider.available`, so the ones that need a key or a
#: URL are only reached once they have one; ``ddgs`` and the keyless Firecrawl
#: cloud endpoint are the two that always answer.
FREE_CHAIN: tuple[str, ...] = (
    "ddgs",
    "searxng",
    "brave_free",
    "tavily",
    "firecrawl_selfhost",
    "firecrawl",
)


def _build() -> dict[str, SearchProvider]:
    providers: list[SearchProvider] = [
        DdgsProvider(),
        BraveFreeProvider(),
        ExaProvider(
            SearchProviderMeta(
                id="exa_free",
                label="Exa (free tier)",
                tier="free",
                key="key required",
                env=("EXA_API_KEY",),
                endpoint="https://api.exa.ai/search",
            )
        ),
        KeenableProvider(
            SearchProviderMeta(
                id="keenable_free",
                label="Keenable (free tier)",
                tier="free",
                key="key required",
                env=("KEENABLE_API_KEY",),
                endpoint="https://api.keenable.ai/v1/search",
            )
        ),
        ParallelProvider(
            SearchProviderMeta(
                id="parallel_free",
                label="Parallel (free tier)",
                tier="free",
                key="key required",
                env=("PARALLEL_API_KEY",),
                endpoint="https://api.parallel.ai/v1beta/search",
            )
        ),
        TavilyProvider(),
        SearxngProvider(),
        FirecrawlProvider(
            SearchProviderMeta(
                id="firecrawl_selfhost",
                label="Firecrawl (self-hosted)",
                tier="free",
                key="self-hosted",
                env=("FIRECRAWL_URL", "FIRECRAWL_API_KEY"),
                endpoint="$FIRECRAWL_URL/v1/search",
            ),
            needs_url=True,
        ),
        ExaProvider(
            SearchProviderMeta(
                id="exa",
                label="Exa",
                tier="paid",
                key="key required",
                env=("EXA_API_KEY",),
                endpoint="https://api.exa.ai/search",
            )
        ),
        KeenableProvider(
            SearchProviderMeta(
                id="keenable",
                label="Keenable",
                tier="paid",
                key="key required",
                env=("KEENABLE_API_KEY",),
                endpoint="https://api.keenable.ai/v1/search",
            )
        ),
        ParallelProvider(
            SearchProviderMeta(
                id="parallel",
                label="Parallel",
                tier="paid",
                key="key required",
                env=("PARALLEL_API_KEY",),
                endpoint="https://api.parallel.ai/v1beta/search",
            )
        ),
        FirecrawlProvider(
            SearchProviderMeta(
                id="firecrawl",
                label="Firecrawl Cloud",
                tier="paid",
                key="key optional",
                env=("FIRECRAWL_API_KEY", "FIRECRAWL_CLOUD_URL"),
                endpoint="https://api.firecrawl.dev/v1/search",
            ),
            needs_url=False,
        ),
        XaiGrokProvider(),
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


def needs_key(provider_id: str) -> bool:
    """True when this id cannot run at all until an API key is configured."""
    provider = _REGISTRY.get(provider_id)
    return provider is not None and provider.meta.key == "key required"


def credential_env(provider_id: str) -> str:
    """The environment variable a user would set instead of the settings key."""
    provider = _REGISTRY.get(provider_id)
    if provider is None:
        return ""
    for name in provider.meta.env:
        if not name.endswith("_URL"):
            return name
    return ""


def chain(preferred: str | None) -> list[SearchProvider]:
    """``preferred`` first, then the fallback chain, each id appearing once."""
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
    "ThinProvider",
    "all_providers",
    "chain",
    "credential_env",
    "credentials_for",
    "get",
    "metas",
    "needs_key",
]
