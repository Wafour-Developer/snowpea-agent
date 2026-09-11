"""Chat-provider registry (M1 contract §5, M3 contract §2).

Resolution order for :meth:`ProviderRegistry.get`:

1. ``SNOWPEA_PROVIDER`` — ``fake:<script.json>`` for the deterministic scripted
   provider, or ``<vendor>[:model]`` to pin a vendor for the whole process.
2. the ``vendor`` argument (a session's ``provider``), or a provider registered
   explicitly with :meth:`register`.
3. ``settings.providers.default``.
4. the first configured vendor, in preset order.

Credentials come from ``settings.providers[vendor].api_key`` or, failing that,
the vendor's ``env_keys``.
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
    VendorPreset,
    preset_for,
)
from snowpea_core.server.protocol import ProviderInfo

log = logging.getLogger("snowpea.providers")

FAKE_PREFIX = "fake"


def _split_env(value: str) -> tuple[str, str | None]:
    """``"openai:gpt-4.1"`` -> ``("openai", "gpt-4.1")``."""
    vendor, _, model = value.partition(":")
    return vendor.strip(), (model.strip() or None)


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

    def preset(self, vendor: str) -> VendorPreset:
        """The preset for ``vendor``, honouring a configured ``local`` variant."""
        variant = self.vendor_config(vendor).get("variant")
        try:
            return preset_for(vendor, str(variant) if variant else None)
        except KeyError:
            raise ProviderError("invalid_params", f"unknown provider vendor: {vendor}") from None

    def api_key_for(self, vendor: str) -> str | None:
        """Configured key from settings, else the vendor's environment variable."""
        config = self.vendor_config(vendor)
        for field in ("api_key", "token"):
            value = config.get(field)
            if isinstance(value, str) and value:
                return value
        preset = PRESETS_BY_VENDOR.get(vendor)
        for name in preset.env_keys if preset else ():
            from_env = os.environ.get(name)
            if from_env:
                return from_env
        return None

    def base_url_for(self, vendor: str) -> str | None:
        configured = self.vendor_config(vendor).get("base_url")
        if isinstance(configured, str) and configured:
            return configured
        preset = PRESETS_BY_VENDOR.get(vendor)
        return preset.base_url if preset else None

    def model_for(self, vendor: str, model: str | None = None) -> str:
        if model:
            return model
        configured = self.vendor_config(vendor).get("model")
        if isinstance(configured, str) and configured:
            return configured
        preset = PRESETS_BY_VENDOR.get(vendor)
        return preset.default_model if preset else ""

    def is_configured(self, vendor: str) -> bool:
        if vendor in self._providers:
            return True
        config = self.vendor_config(vendor)
        if vendor == "local" and config.get("base_url"):
            return True
        return bool(self.api_key_for(vendor))

    def configure(self, vendor: str, config: dict[str, Any]) -> dict[str, Any]:
        """Merge ``config`` into ``settings.providers[vendor]`` and return it.

        Persisting to ``settings.json`` is the caller's job (the RPC handler
        knows the daemon's :class:`~snowpea_core.config.paths.Paths`).
        """
        if vendor not in PRESETS:
            raise ProviderError("invalid_params", f"unknown provider vendor: {vendor}")
        merged = {**self.vendor_config(vendor)}
        for key, value in config.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        self.settings.providers[vendor] = merged
        return merged

    # -- resolution ----------------------------------------------------
    def get(self, vendor: str | None = None, model: str | None = None) -> ChatProvider:
        """Return a provider for ``vendor``/``model`` (M3 contract §2)."""
        env = (os.environ.get("SNOWPEA_PROVIDER") or "").strip()
        if env:
            env_vendor, env_model = _split_env(env)
            if env_vendor == FAKE_PREFIX:
                return FakeProvider.from_env(env)
            vendor, model = env_vendor, model or env_model

        name = vendor or self.default_vendor()
        explicit = self._providers.get(name)
        if explicit is not None:
            return explicit  # type: ignore[no-any-return]
        if name == FAKE_PREFIX:
            return FakeProvider()
        return self.build(name, model)

    def build(self, vendor: str, model: str | None = None) -> ChatProvider:
        """Instantiate the adapter a preset names, with resolved credentials."""
        preset = self.preset(vendor)
        resolved_model = self.model_for(vendor, model)
        api_key = self.api_key_for(vendor)
        base_url = self.base_url_for(vendor)
        if preset.adapter == "anthropic_native":
            from snowpea_core.providers.anthropic_native import AnthropicProvider

            return AnthropicProvider(api_key=api_key, model=resolved_model, base_url=base_url)
        if preset.adapter == "gemini_native":
            from snowpea_core.providers.gemini_native import GeminiProvider

            return GeminiProvider(preset, api_key=api_key, model=resolved_model, base_url=base_url)
        from snowpea_core.providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            preset, api_key=api_key, model=resolved_model, base_url=base_url
        )

    def default_vendor(self) -> str:
        """``settings.providers.default``, else the first configured vendor."""
        env = (os.environ.get("SNOWPEA_PROVIDER") or "").strip()
        if env:
            env_vendor, _ = _split_env(env)
            if env_vendor:
                return env_vendor
        configured = self.settings.providers.get("default")
        if isinstance(configured, str) and configured:
            return configured
        for vendor in PRESETS:
            if self.is_configured(vendor):
                return vendor
        return DEFAULT_VENDOR

    # -- listing -------------------------------------------------------
    def list(self) -> list[ProviderInfo]:
        """Every known vendor with its models, auth methods and state."""
        default = self.default_vendor()
        infos: list[ProviderInfo] = []
        for vendor, preset in PRESETS.items():
            config = self.vendor_config(vendor)
            models = list(preset.models)
            extra = config.get("models")
            if isinstance(extra, list):
                models = [str(m) for m in extra] or models
            infos.append(
                ProviderInfo(
                    vendor=vendor,
                    label=preset.label,
                    defaultModel=self.model_for(vendor),
                    models=models,
                    configured=self.is_configured(vendor),
                    default=vendor == default,
                    authMethods=list(preset.auth_methods),
                )
            )
        return infos


__all__ = ["FAKE_PREFIX", "ProviderRegistry"]
