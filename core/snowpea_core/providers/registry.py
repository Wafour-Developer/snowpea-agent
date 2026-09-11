"""Chat-provider registry (contract §5).

Resolution order for :meth:`ProviderRegistry.get`:

1. ``SNOWPEA_PROVIDER=fake[:script.json]`` -> the deterministic scripted
   provider (tests and CI never touch the network).
2. an explicitly registered provider for ``vendor``.
3. ``anthropic`` when ``settings.providers.anthropic.api_key`` or
   ``ANTHROPIC_API_KEY`` is set.

Anything else raises :class:`ProviderError` with ``invalid_params``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatProvider, ProviderError
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.providers.presets import (
    DEFAULT_VENDOR,
    PRESETS,
    PRESETS_BY_VENDOR,
)
from snowpea_core.server.protocol import ProviderInfo

log = logging.getLogger("snowpea.providers")

FAKE_PREFIX = "fake"


class ProviderRegistry:
    """Vendor/model -> :class:`ChatProvider` lookup."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._providers: dict[str, Any] = {}

    def bind(self, settings: Settings) -> None:
        self.settings = settings

    def register(self, vendor: str, provider: Any) -> None:
        """Install an explicit provider, overriding the built-in resolution."""
        self._providers[vendor] = provider

    # -- configuration -------------------------------------------------
    def vendor_config(self, vendor: str) -> dict[str, Any]:
        raw = self.settings.providers.get(vendor)
        return dict(raw) if isinstance(raw, dict) else {}

    def api_key_for(self, vendor: str) -> str | None:
        """Configured key from settings, else the vendor's environment variable."""
        key = self.vendor_config(vendor).get("api_key")
        if isinstance(key, str) and key:
            return key
        preset = PRESETS_BY_VENDOR.get(vendor)
        for name in preset.env_keys if preset else ():
            value = os.environ.get(name)
            if value:
                return value
        return None

    def is_configured(self, vendor: str) -> bool:
        if vendor in self._providers:
            return True
        config = self.vendor_config(vendor)
        if config.get("base_url") and vendor == "local":
            return True
        return bool(self.api_key_for(vendor))

    # -- resolution ----------------------------------------------------
    def get(self, vendor: str | None = None, model: str | None = None) -> ChatProvider:
        """Return a provider for ``vendor``/``model`` (contract §5)."""
        env = os.environ.get("SNOWPEA_PROVIDER", "")
        if env.startswith(FAKE_PREFIX):
            return FakeProvider.from_env(env)

        name = vendor or self.default_vendor()
        explicit = self._providers.get(name)
        if explicit is not None:
            return explicit  # type: ignore[no-any-return]
        if name == FAKE_PREFIX:
            return FakeProvider()
        if name == "anthropic":
            from snowpea_core.providers.anthropic_native import AnthropicProvider

            config = self.vendor_config("anthropic")
            return AnthropicProvider(
                api_key=self.api_key_for("anthropic"),
                model=model or config.get("model"),
                base_url=config.get("base_url"),
            )
        if name in PRESETS_BY_VENDOR:
            raise ProviderError(
                "not_implemented", f"{name} adapter arrives with M3; configure anthropic for now"
            )
        raise ProviderError("invalid_params", f"unknown provider vendor: {name}")

    def default_vendor(self) -> str:
        """First configured vendor, preferring Anthropic."""
        if os.environ.get("SNOWPEA_PROVIDER", "").startswith(FAKE_PREFIX):
            return FAKE_PREFIX
        configured = self.settings.providers.get("default")
        if isinstance(configured, str) and configured:
            return configured
        for preset in PRESETS:
            if self.is_configured(preset.vendor):
                return preset.vendor
        return DEFAULT_VENDOR

    # -- listing -------------------------------------------------------
    def list(self) -> list[ProviderInfo]:
        """Every known vendor with its models, auth methods and state."""
        default = self.default_vendor()
        infos: list[ProviderInfo] = []
        for preset in PRESETS:
            config = self.vendor_config(preset.vendor)
            models = list(preset.models)
            extra = config.get("models")
            if isinstance(extra, list):
                models = [str(m) for m in extra] or models
            infos.append(
                ProviderInfo(
                    vendor=preset.vendor,
                    models=models,
                    configured=self.is_configured(preset.vendor),
                    default=preset.vendor == default,
                    authMethods=list(preset.auth_methods),
                )
            )
        return infos


__all__ = ["FAKE_PREFIX", "ProviderRegistry"]
