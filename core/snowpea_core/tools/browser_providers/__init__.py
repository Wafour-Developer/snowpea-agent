"""The browser provider catalog (M2 contract §4).

Five ids.  ``local_chromium`` drives a real headless Chromium; the other four
carry their tags and refuse with ``browser_provider_unavailable`` until a later
milestone wires them up.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.tools.browser_providers.base import (
    BrowserNotInstalled,
    BrowserProvider,
    BrowserProviderMeta,
    BrowserProviderUnavailable,
    PageState,
)
from snowpea_core.tools.browser_providers.local_chromium import LocalChromiumProvider

PROVIDER_ORDER: tuple[str, ...] = (
    "local_chromium",
    "camoufox",
    "browser_use_local",
    "browserbase",
    "firecrawl_cloud",
)


class ThinBrowserProvider:
    """Catalog-only provider: right tags, clear refusal, no behaviour."""

    def __init__(self, meta: BrowserProviderMeta) -> None:
        self.meta = meta

    def available(self, settings: Any = None) -> bool:
        return False

    def _refuse(self) -> BrowserProviderUnavailable:
        return BrowserProviderUnavailable(
            f"{self.meta.label} is listed but not implemented yet"
            + (f" (endpoint {self.meta.endpoint})" if self.meta.endpoint else "")
        )

    async def navigate(self, session_id: str, url: str) -> PageState:
        raise self._refuse()

    async def click(self, session_id: str, selector: str) -> PageState:
        raise self._refuse()

    async def type_text(
        self, session_id: str, selector: str, text: str, *, submit: bool = False
    ) -> PageState:
        raise self._refuse()

    async def scroll(self, session_id: str, delta_y: int) -> PageState:
        raise self._refuse()

    async def snapshot(self, session_id: str) -> PageState:
        raise self._refuse()

    async def close_session(self, session_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _build() -> dict[str, BrowserProvider]:
    providers: list[BrowserProvider] = [
        LocalChromiumProvider(),
        ThinBrowserProvider(
            BrowserProviderMeta(
                id="camoufox",
                label="Camoufox (local, anti-detect)",
                tier="free",
                key="no key",
            )
        ),
        ThinBrowserProvider(
            BrowserProviderMeta(
                id="browser_use_local",
                label="Browser Use (local)",
                tier="free",
                key="no key",
            )
        ),
        ThinBrowserProvider(
            BrowserProviderMeta(
                id="browserbase",
                label="Browserbase",
                tier="paid",
                key="key required",
                env=("BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"),
                endpoint="https://api.browserbase.com/v1/sessions",
            )
        ),
        ThinBrowserProvider(
            BrowserProviderMeta(
                id="firecrawl_cloud",
                label="Firecrawl Cloud",
                tier="paid",
                key="key required",
                env=("FIRECRAWL_API_KEY",),
                endpoint="https://api.firecrawl.dev/v1/scrape",
            )
        ),
    ]
    return {provider.meta.id: provider for provider in providers}


_REGISTRY: dict[str, BrowserProvider] = _build()


def get(provider_id: str) -> BrowserProvider | None:
    return _REGISTRY.get(provider_id)


def all_providers() -> list[BrowserProvider]:
    return [_REGISTRY[pid] for pid in PROVIDER_ORDER if pid in _REGISTRY]


def metas() -> list[BrowserProviderMeta]:
    return [provider.meta for provider in all_providers()]


def resolve(settings: Any) -> BrowserProvider:
    """The provider ``settings.browser.provider`` names, else the default."""
    name = getattr(getattr(settings, "browser", None), "provider", None) or "local_chromium"
    return _REGISTRY.get(str(name)) or _REGISTRY["local_chromium"]


async def close_all_sessions(session_id: str) -> None:
    """Drop ``session_id`` from every provider that holds a context for it."""
    for provider in _REGISTRY.values():
        await provider.close_session(session_id)


__all__ = [
    "PROVIDER_ORDER",
    "BrowserNotInstalled",
    "BrowserProvider",
    "BrowserProviderMeta",
    "BrowserProviderUnavailable",
    "LocalChromiumProvider",
    "PageState",
    "ThinBrowserProvider",
    "all_providers",
    "close_all_sessions",
    "get",
    "metas",
    "resolve",
]
