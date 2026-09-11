"""Chat-provider registry — M1 stub (US-007 fills it in)."""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import ProviderInfo


class ProviderRegistry:
    """Vendor/model -> provider lookup. Stub: empty."""

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(self, vendor: str, provider: Any) -> None:
        self._providers[vendor] = provider

    def get(self, vendor: str | None = None, model: str | None = None) -> Any | None:
        if vendor is None:
            return next(iter(self._providers.values()), None)
        return self._providers.get(vendor)

    def list(self) -> list[ProviderInfo]:
        return []


__all__ = ["ProviderRegistry"]
