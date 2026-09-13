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
import time
from collections.abc import Awaitable, Callable
from typing import Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import context_windows
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers.base import ChatProvider, ProviderError
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.providers.presets import (
    DEFAULT_VENDOR,
    PRESETS,
    PRESETS_BY_VENDOR,
    VendorPreset,
    preset_for,
)
from snowpea_core.server.protocol import AuthStatus, ProviderInfo

log = logging.getLogger("snowpea.providers")

FAKE_PREFIX = "fake"

#: ``auth_method`` values that mean "this vendor is authenticated by an OAuth
#: session, not an API key".  They make the key fields in the block
#: irrelevant — see :meth:`ProviderRegistry.api_key_for`.
OAUTH_AUTH_METHODS: frozenset[str] = frozenset(
    {"chatgpt", "device_code", "google_oauth", "google_adc", "oauth_token"}
)

#: Renew this long before a stored token actually expires, so a turn never
#: starts with a credential that dies mid-request.
REFRESH_SKEW_SEC = 60.0

#: What to tell a user whose vendor has no usable model id.
NO_MODEL_HINT = "run `snowpea setup provider`, or pick one in a session with `/model <name>`"


def _as_positive_int(value: Any) -> int | None:
    """A positive int from a settings value, else ``None`` (CORE-context)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return None
        return number if number > 0 else None
    return None


def _split_env(value: str) -> tuple[str, str | None]:
    """``"openai:gpt-4.1"`` -> ``("openai", "gpt-4.1")``."""
    vendor, _, model = value.partition(":")
    return vendor.strip(), (model.strip() or None)


class ProviderRegistry:
    """Vendor/model -> :class:`ChatProvider` lookup."""

    def __init__(self, settings: Settings | None = None, paths: Paths | None = None) -> None:
        self.settings = settings or Settings()
        self.paths = paths
        self._providers: dict[str, Any] = {}
        #: Called after a successful :meth:`save`; the daemon points it at
        #: ``Core.mark_settings_saved`` so its own write is not mistaken for an
        #: outside edit on the next prompt (CORE-settings-reload).
        self.on_saved: Callable[[], None] | None = None

    def bind(self, settings: Settings, paths: Paths | None = None) -> None:
        self.settings = settings
        if paths is not None:
            self.paths = paths

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

    def auth_method_for(self, vendor: str) -> str | None:
        """``settings.providers[vendor].auth_method``, when one was recorded."""
        value = self.vendor_config(vendor).get("auth_method")
        return str(value) if isinstance(value, str) and value else None

    def api_key_for(self, vendor: str) -> str | None:
        """Configured key from settings, else the vendor's environment variable.

        A vendor whose ``auth_method`` names an OAuth flow has **no** API key,
        whatever is still lying in its block: a key left over from an earlier
        setup used to outlive the login that replaced it and silently win here,
        so the user was told the sign-in worked while every request kept using
        the stale key (report §6.7 A-P1-2).
        """
        config = self.vendor_config(vendor)
        if self.auth_method_for(vendor) in OAUTH_AUTH_METHODS:
            return None
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

    def _profile(self, profile_id: str | None) -> tuple[str, str] | None:
        if not profile_id:
            return None
        profile = self.settings.models.profiles.get(profile_id)
        if profile is None:
            return None
        return profile.provider, profile.model

    def default_profile(self) -> tuple[str, str] | None:
        """Configured default model profile, if one exists."""
        return self._profile(self.settings.models.default)

    def model_for(self, vendor: str, model: str | None = None) -> str:
        if model:
            return model
        default = self.default_profile()
        if default is not None and default[0] == vendor:
            return default[1]
        configured = self.vendor_config(vendor).get("model")
        if isinstance(configured, str) and configured:
            return configured
        preset = PRESETS_BY_VENDOR.get(vendor)
        return preset.default_model if preset else ""

    def is_configured(self, vendor: str) -> bool:
        """True when this machine has *some* credential for ``vendor``.

        An expired OAuth session still counts as configured — it is a session
        that needs renewing, not an absent one — and :meth:`auth_status` is
        what tells the two apart for a surface that shows it.
        """
        if vendor in self._providers:
            return True
        config = self.vendor_config(vendor)
        if vendor == "local" and config.get("base_url"):
            return True
        if self.auth_method_for(vendor) in OAUTH_AUTH_METHODS:
            return bool(
                config.get("access_token")
                or config.get("oauth_token")
                or config.get("token")
                or self.auth_method_for(vendor) == "google_adc"
            )
        if isinstance(config.get("oauth_token"), str) and config.get("oauth_token"):
            return True
        return bool(self.api_key_for(vendor))

    def auth_status(self, vendor: str) -> AuthStatus:
        """``"unconfigured" | "active" | "expired"`` for ``vendor``.

        ``expired`` means the stored OAuth session is past (or within the
        refresh skew of) its expiry.  It is still renewable without the user
        typing anything as long as a refresh token is stored, so surfaces show
        it as a state, not an error; only a refresh that *fails* asks for a new
        login (``auth_expired``).
        """
        if not self.is_configured(vendor):
            return "unconfigured"
        config = self.vendor_config(vendor)
        method = self.auth_method_for(vendor)
        if method not in OAUTH_AUTH_METHODS or method == "google_adc":
            # google_adc has no token of ours: gcloud owns the refresh.
            return "active"
        expires_at = config.get("expires_at")
        if not isinstance(expires_at, int | float):
            return "active"
        return "expired" if float(expires_at) <= time.time() + REFRESH_SKEW_SEC else "active"

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

    def save(self) -> bool:
        """Write ``settings.json`` when a :class:`Paths` is bound; else do nothing."""
        if self.paths is None:
            return False
        try:
            self.settings.save(self.paths)
        except OSError as exc:  # pragma: no cover - disk failure
            log.warning("could not persist settings.json: %s", exc)
            return False
        if self.on_saved is not None:
            self.on_saved()
        return True

    # -- model discovery -----------------------------------------------
    async def list_models(self, vendor: str, *, refresh: bool = False) -> list[str]:
        """Model ids ``vendor``'s endpoint offers (M3 contract §2, model discovery)."""
        static = model_discovery.oauth_models(vendor, self.auth_method_for(vendor))
        if static is not None:
            return static
        preset = self.preset(vendor)
        return await model_discovery.list_models(
            preset,
            api_key=self.api_key_for(vendor),
            base_url=self.base_url_for(vendor),
            refresh=refresh,
        )

    # -- context windows (CORE-context) --------------------------------
    def context_window(self, vendor: str, model: str | None = None) -> int | None:
        """Context window of ``vendor``/``model`` in tokens, without any I/O.

        ``settings.providers.<vendor>.context_window`` wins over the static
        table, so a model the table has never heard of can still be given a
        window by hand.  A ``local`` window already discovered from the server
        is served from cache here; :meth:`resolve_context_window` is the one
        that may go and ask.  ``None`` means unknown — surfaces render ``?``.
        """
        override = _as_positive_int(self.vendor_config(vendor).get("context_window"))
        if override is not None:
            return override
        resolved_model = self.model_for(vendor, model)
        base_url = self.base_url_for(vendor) or ""
        hit, cached = context_windows.cache_get(vendor, base_url.rstrip("/"), resolved_model)
        if hit:
            return cached
        preset = PRESETS_BY_VENDOR.get(vendor)
        if preset is None:
            return None
        return preset.context_window(resolved_model)

    async def resolve_context_window(
        self, vendor: str, model: str | None = None, *, refresh: bool = False
    ) -> int | None:
        """Like :meth:`context_window`, but asks a ``local`` server when it must.

        Only the ``local`` vendor is queried: every hosted vendor publishes a
        fixed window that the static table already carries, and a round trip
        per turn to learn a constant would be pure latency.  The answer (even
        a negative one) is cached, so at most one lookup per model per TTL.
        """
        override = _as_positive_int(self.vendor_config(vendor).get("context_window"))
        if override is not None:
            return override
        resolved_model = self.model_for(vendor, model)
        preset = PRESETS_BY_VENDOR.get(vendor)
        static = preset.context_window(resolved_model) if preset is not None else None
        if vendor != "local":
            return static
        base_url = (self.base_url_for(vendor) or "").rstrip("/")
        if not base_url or model_discovery.is_placeholder(resolved_model):
            return static
        if not refresh:
            hit, cached = context_windows.cache_get(vendor, base_url, resolved_model)
            if hit:
                return cached
        discovered = await context_windows.discover_window(
            base_url, resolved_model, api_key=self.api_key_for(vendor)
        )
        window = discovered if discovered is not None else static
        context_windows.cache_put(vendor, base_url, resolved_model, window)
        return window

    async def resolve_model(self, vendor: str, model: str | None = None) -> str:
        """Like :meth:`model_for`, but never returns a placeholder.

        When the configured model is a placeholder (the ``local`` preset ships
        ``local-model``, which no server recognises) the endpoint is asked for
        its list and the first id wins.  The choice is persisted so the next
        prompt costs no round trip.
        """
        resolved = self.model_for(vendor, model)
        if not model_discovery.is_placeholder(resolved):
            return resolved
        try:
            available = await self.list_models(vendor)
        except ProviderError as exc:
            raise ProviderError(
                "model_not_configured",
                f"{vendor}: no model configured and the server could not be asked "
                f"({exc}); {NO_MODEL_HINT}",
            ) from exc
        available = [name for name in available if not model_discovery.is_placeholder(name)]
        if not available:
            raise ProviderError(
                "model_not_configured",
                f"{vendor}: no model configured and {self.base_url_for(vendor)} listed "
                f"none; {NO_MODEL_HINT}",
            )
        picked = available[0]
        self.configure(vendor, {"model": picked})
        self.save()
        log.info("%s: auto-selected model %s", vendor, picked)
        return picked

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

    def _oauth_saver(self, vendor: str) -> Callable[[dict[str, Any]], None]:
        """Persist credentials a transport refreshed, so the renewal survives.

        A refresh that is not written back is one round trip saved and then
        paid for again on the next process start; the transports hand the
        merged record here the moment they get it.
        """

        def save(credentials: dict[str, Any]) -> None:
            self.configure(vendor, dict(credentials))
            self.save()

        return save

    def _oauth_provider(self, vendor: str, resolved_model: str) -> ChatProvider | None:
        """The adapter an OAuth session needs, or ``None`` for an API key.

        A ChatGPT or Google sign-in cannot use the vendor's ordinary endpoint —
        ``api.openai.com`` rejects a subscription token outright, and Gemini's
        API-key host has no notion of a Google account — so those sessions are
        routed to the backend their own CLI uses (CORE-codex-login).
        """
        config = self.vendor_config(vendor)
        if vendor == "openai":
            from snowpea_core.providers import openai_oauth

            if openai_oauth.is_chatgpt_auth(config):
                from snowpea_core.providers.codex_transport import CodexProvider

                credentials = openai_oauth.normalize_stored_credentials(config)
                model = resolved_model
                if model not in (model_discovery.oauth_models(vendor, "chatgpt") or []):
                    # An API-key model id (gpt-4.1) is not served by Codex.
                    model = ""
                return CodexProvider(
                    credentials,
                    model=model or None,
                    reasoning_effort=str(config.get("reasoning_effort") or "") or None,
                    on_credentials=self._oauth_saver(vendor),
                )
        if vendor == "gemini":
            from snowpea_core.providers import google_oauth

            if google_oauth.is_google_oauth(config):
                from snowpea_core.providers.gemini_codeassist_transport import CodeAssistProvider

                model = resolved_model
                if model not in (model_discovery.oauth_models(vendor, "google_oauth") or []):
                    model = ""
                return CodeAssistProvider(
                    dict(config),
                    model=model or None,
                    project_id=str(config.get("project_id") or "") or None,
                    on_credentials=self._oauth_saver(vendor),
                )
        return None

    def build(self, vendor: str, model: str | None = None) -> ChatProvider:
        """Instantiate the adapter a preset names, with resolved credentials."""
        preset = self.preset(vendor)
        resolved_model = self.model_for(vendor, model)
        oauth = self._oauth_provider(vendor, resolved_model)
        if oauth is not None:
            return oauth
        api_key = self.api_key_for(vendor)
        if vendor == "openai" and not api_key:
            oauth_token = self.vendor_config(vendor).get("oauth_token")
            api_key = oauth_token if isinstance(oauth_token, str) and oauth_token else None
        base_url = self.base_url_for(vendor)
        if preset.adapter == "anthropic_native":
            from snowpea_core.providers.anthropic_native import AnthropicProvider

            return AnthropicProvider(api_key=api_key, model=resolved_model, base_url=base_url)
        if preset.adapter == "gemini_native":
            from snowpea_core.providers.gemini_native import GeminiProvider

            return GeminiProvider(
                preset,
                api_key=api_key,
                auth_method=str(self.vendor_config(vendor).get("auth_method") or "") or None,
                oauth_token=str(self.vendor_config(vendor).get("oauth_token") or "") or None,
                model=resolved_model,
                base_url=base_url,
            )
        from snowpea_core.providers.openai_compat import OpenAICompatProvider

        resolver: Callable[[], Awaitable[str]] | None = None
        if model_discovery.is_placeholder(resolved_model):
            # Resolve at first use: the constructor is sync, discovery is not.
            async def _resolve() -> str:
                return await self.resolve_model(vendor, model)

            resolver = _resolve

        return OpenAICompatProvider(
            preset,
            api_key=api_key,
            model=resolved_model,
            base_url=base_url,
            model_resolver=resolver,
        )

    def default_vendor(self) -> str:
        """``settings.providers.default``, else the first configured vendor."""
        env = (os.environ.get("SNOWPEA_PROVIDER") or "").strip()
        if env:
            env_vendor, _ = _split_env(env)
            if env_vendor:
                return env_vendor
        profile = self.default_profile()
        if profile is not None and profile[0] in PRESETS:
            return profile[0]
        if profile is not None:
            # ``ModelProfile`` only checks that the strings are non-empty, so a
            # default profile naming a vendor that does not exist used to be
            # returned here ahead of every other candidate — bricking *every*
            # session, not just routed ones (CORE-model-assignment B-P1-3).
            log.warning(
                "models.default profile names unknown provider %r; "
                "falling back to the configured vendor",
                profile[0],
            )
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
            static = model_discovery.oauth_models(vendor, self.auth_method_for(vendor))
            if static is not None:
                models = static
            infos.append(
                ProviderInfo(
                    vendor=vendor,
                    label=preset.label,
                    defaultModel=self.model_for(vendor),
                    models=models,
                    configured=self.is_configured(vendor),
                    default=vendor == default,
                    authMethods=list(preset.auth_methods),
                    authStatus=self.auth_status(vendor),
                )
            )
        return infos


__all__ = ["FAKE_PREFIX", "NO_MODEL_HINT", "ProviderRegistry"]
