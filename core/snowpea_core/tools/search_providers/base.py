"""Shapes shared by every web-search provider (M2 contract §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Tier = Literal["free", "paid", "subscription"]
KeyKind = Literal["no key", "key optional", "key required", "self-hosted"]


class SearchProviderUnavailable(RuntimeError):
    """Raised when a provider has no credentials, no endpoint or no client.

    ``web_search`` turns this into ``error{code:"search_provider_unavailable"}``.
    """


@dataclass(frozen=True)
class SearchHit:
    """One result row; ``snippet`` may be empty when the provider omits it."""

    title: str
    url: str
    snippet: str = ""

    def render(self) -> str:
        body = f"{self.title}\n{self.url}"
        return f"{body}\n{self.snippet}" if self.snippet else body


@dataclass(frozen=True)
class SearchProviderMeta:
    """Catalog entry: what the setup screen shows and what gates the provider."""

    id: str
    label: str
    tier: Tier
    key: KeyKind
    default: bool = False
    env: tuple[str, ...] = ()
    #: Documented HTTP endpoint, so a thin client is still self-describing.
    endpoint: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """What ``web_search`` and ``web_extract`` need from a provider."""

    meta: SearchProviderMeta

    def available(self, settings: Any) -> bool: ...

    async def search(self, query: str, *, limit: int) -> list[SearchHit]: ...

    async def extract(self, url: str) -> str | None: ...


@dataclass
class Credentials:
    """Resolved credentials for one provider: settings first, then env."""

    api_key: str | None = None
    base_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def credentials_for(meta: SearchProviderMeta, settings: Any) -> Credentials:
    """Read ``settings.search.credentials[<id>]``, falling back to ``meta.env``."""
    import os

    block: dict[str, Any] = {}
    search = getattr(settings, "search", None)
    store = getattr(search, "credentials", None)
    if isinstance(store, dict):
        candidate = store.get(meta.id)
        if isinstance(candidate, dict):
            block = candidate
    api_key = block.get("api_key") or block.get("apiKey")
    base_url = block.get("base_url") or block.get("baseUrl") or block.get("url")
    for name in meta.env:
        value = os.environ.get(name)
        if not value:
            continue
        if name.endswith("_URL") and not base_url:
            base_url = value
        elif not api_key and not name.endswith("_URL"):
            api_key = value
    extra = {k: str(v) for k, v in block.items() if isinstance(v, str | int | float)}
    return Credentials(
        api_key=str(api_key) if api_key else None,
        base_url=str(base_url) if base_url else None,
        extra=extra,
    )


__all__ = [
    "Credentials",
    "KeyKind",
    "SearchHit",
    "SearchProvider",
    "SearchProviderMeta",
    "SearchProviderUnavailable",
    "Tier",
    "credentials_for",
]
