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
from dataclasses import replace
from typing import Any

from snowpea_core.config.paths import Paths, resolve_home
from snowpea_core.config.settings import THINKING_CHOICES, Settings
from snowpea_core.providers import content as content_parts
from snowpea_core.providers import context_windows
from snowpea_core.providers import effort as effort_scale
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers import vision as vision_scale
from snowpea_core.providers.base import ChatProvider, ProviderError
from snowpea_core.providers.fake import FakeProvider
from snowpea_core.providers.presets import (
    DEFAULT_VENDOR,
    PRESETS,
    VendorPreset,
    is_local_vendor_config,
    local_vendor_ids,
    preset_for,
    resolve_parallel_tools,
    validate_custom_vendor_id,
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
        """The preset for ``vendor``, honouring variants and named servers.

        A key under ``providers`` that declares ``preset: local`` is a
        self-hosted OpenAI-compatible server the user named, so it gets a
        preset synthesized from its own block rather than a lookup failure.
        """
        config = self.vendor_config(vendor)
        variant = config.get("variant")
        try:
            return preset_for(vendor, str(variant) if variant else None, config)
        except KeyError:
            raise ProviderError("invalid_params", f"unknown provider vendor: {vendor}") from None

    def preset_or_none(self, vendor: str) -> VendorPreset | None:
        """:meth:`preset`, or ``None`` for a vendor nothing describes."""
        try:
            return self.preset(vendor)
        except ProviderError:
            return None

    def _profile_record_for(self, vendor: str, model: str) -> dict[str, Any] | None:
        for profile in self.settings.models.profiles.values():
            if profile.provider == vendor and profile.model == model:
                return profile.model_dump()
        return None

    def effective_preset(self, vendor: str, model: str | None = None) -> VendorPreset:
        """Static preset with per-model ``supports_parallel_tools`` resolved."""
        preset = self.preset(vendor)
        resolved_model = self.model_for(vendor, model)
        parallel = resolve_parallel_tools(
            preset,
            model=resolved_model,
            vendor_config=self.vendor_config(vendor),
            profile=self._profile_record_for(vendor, resolved_model),
        )
        if parallel == preset.supports_parallel_tools:
            return preset
        return replace(preset, supports_parallel_tools=parallel)

    def is_local_style(self, vendor: str) -> bool:
        """True for ``local`` and for every named OpenAI-compatible server.

        This is the test that replaced ``vendor == "local"``: keyless auth,
        ``/v1/models`` discovery, the Ollama listing fallback and live
        context-window probing all key off it, so a server the user called
        ``hon2`` behaves exactly like the built-in one.
        """
        preset = self.preset_or_none(vendor)
        return bool(preset is not None and preset.local_style)

    def local_vendors(self) -> list[str]:
        """Configured local-style vendor ids, ``local`` first, then by name."""
        found = local_vendor_ids(self.settings.providers)
        return sorted(found, key=lambda name: (name != "local", name))

    def custom_vendors(self) -> list[str]:
        """Local-style vendors the user added, i.e. everything but ``local``."""
        return [name for name in self.local_vendors() if name not in PRESETS]

    def known_vendors(self) -> list[str]:
        """Every vendor a surface may show: the presets, then the named servers."""
        return [*PRESETS, *self.custom_vendors()]

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
        preset = self.preset_or_none(vendor)
        for name in preset.env_keys if preset else ():
            from_env = os.environ.get(name)
            if from_env:
                return from_env
        return None

    def base_url_for(self, vendor: str) -> str | None:
        configured = self.vendor_config(vendor).get("base_url")
        if isinstance(configured, str) and configured:
            return configured
        preset = self.preset_or_none(vendor)
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
        preset = self.preset_or_none(vendor)
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
        preset = self.preset_or_none(vendor)
        # A keyless vendor (the self-hosted OpenAI-compatible ``local`` one and
        # its vLLM/Ollama/LM Studio variants) is configured by what it points
        # at, not by a credential: a saved ``base_url`` or ``model`` is the
        # whole setup, and reporting it as unconfigured is what made the setup
        # wizard print ``[inactive]`` next to a provider the user had finished
        # configuring and selected as the default.
        if preset is not None and not preset.key_required:
            if config.get("base_url") or config.get("model"):
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
        merged = {**self.vendor_config(vendor)}
        for key, value in config.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        if vendor not in PRESETS:
            # A name nothing knows is only acceptable when the block says what
            # it is: ``preset: local`` (or ``openai-compatible``) declares a
            # self-hosted server, and anything else is a typo we must refuse
            # rather than silently persist as a vendor that can never answer.
            if not is_local_vendor_config(merged):
                raise ProviderError("invalid_params", f"unknown provider vendor: {vendor}")
            try:
                validate_custom_vendor_id(vendor)
            except ValueError as exc:
                raise ProviderError("invalid_params", str(exc)) from None
        self.settings.providers[vendor] = merged
        return merged

    def remove(self, vendor: str) -> bool:
        """Forget ``vendor`` entirely: its block, its profiles, its assignments.

        Removing only ``providers.<vendor>`` would leave ``models.profiles``
        pointing at a server that no longer exists, which is how a deleted
        provider comes back as ``unknown provider vendor`` on the next prompt.
        Returns False when there was nothing to remove.
        """
        removed = self.settings.providers.pop(vendor, None) is not None
        if self.settings.providers.get("default") == vendor:
            self.settings.providers.pop("default", None)
        stale = {
            pid
            for pid, profile in self.settings.models.profiles.items()
            if profile.provider == vendor
        }
        for pid in stale:
            self.settings.models.profiles.pop(pid, None)
            removed = True
        if self.settings.models.default in stale:
            self.settings.models.default = None
        self.settings.agents.models = {
            agent: pid
            for agent, pid in self.settings.agents.models.items()
            if pid not in stale
        }
        return removed

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
        return (await self.model_listing(vendor, refresh=refresh)).models

    async def model_listing(
        self, vendor: str, *, refresh: bool = False, transport: Any = None
    ) -> model_discovery.ModelListing:
        """The catalog *and* which rung answered — live, settings, cache, curated.

        Every surface that lists models goes through here, so the wizard, the
        ``provider.models`` RPC, ``snowpea provider models`` and the TUI
        ``/model`` picker cannot disagree about what an account can run, or
        about whether the list is the vendor's own answer or a fallback.
        """
        auth_method = self.auth_method_for(vendor)
        preset = self.preset(vendor)

        def remember(credentials: dict[str, Any]) -> None:
            """Keep a token refreshed during discovery instead of burning it."""
            self.settings.providers[vendor] = {**self.vendor_config(vendor), **credentials}

        return await model_discovery.resolve_models(
            vendor,
            preset=preset,
            auth_method=auth_method,
            api_key=self.api_key_for(vendor),
            base_url=self.base_url_for(vendor),
            credentials=self.vendor_config(vendor),
            home=self.paths.home if self.paths is not None else resolve_home(),
            refresh=refresh,
            transport=transport,
            on_credentials=remember,
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
        preset = self.preset_or_none(vendor)
        if preset is None:
            return None
        return preset.context_window(resolved_model)

    # -- output budget and thinking (CORE-reasoning-budget) ------------
    def max_tokens_for(self, vendor: str, model: str | None = None) -> int:
        """Output tokens one call to ``vendor``/``model`` may produce.

        ``settings.providers.<vendor>.max_tokens`` overrides the global
        ``agent.max_tokens``; either way the answer is clamped to what the
        model actually accepts, because a budget above a vendor's ceiling is
        an HTTP 400 rather than a longer answer.
        """
        override = _as_positive_int(self.vendor_config(vendor).get("max_tokens"))
        budget = override if override is not None else self.settings.agent.max_tokens
        return context_windows.clamp_output_tokens(self.model_for(vendor, model), int(budget))

    def supports_effort(self, vendor: str) -> bool:
        """True when ``vendor``'s API takes a reasoning-effort setting.

        A local-style server is opt-in: ``providers.<name>.effort_param: true``
        says this one accepts ``reasoning_effort``.  Most do not — vLLM passes
        it to the template, which either ignores it or 400s depending on the
        model — so sending it by default would break working setups.
        """
        preset = self.preset_or_none(vendor)
        return bool(preset is not None and preset.supports_effort)

    def effort_for(
        self,
        vendor: str,
        model: str | None = None,
        *,
        session_effort: str | None = None,
        override: str | None = None,
    ) -> tuple[str, str]:
        """``(effort, source)`` in force for ``vendor``/``model`` (CORE-effort).

        The chain lives in :func:`snowpea_core.providers.effort.resolve`; this
        is the binding that knows which model a bare vendor resolves to.
        """
        return effort_scale.resolve(
            self.settings,
            vendor,
            self.model_for(vendor, model),
            session_effort=session_effort,
            override=override,
        )

    # -- vision (CORE-vision) ------------------------------------------
    def vision_for(self, vendor: str, model: str | None = None) -> bool | None:
        """Whether ``vendor``/``model`` may be sent images.

        ``True``/``False`` are answers; ``None`` means "nobody knows yet, and
        this is a server we may ask" — the adapter then sends the images once
        and remembers what came back.  Only a local-style vendor ever gets
        ``None``: a hosted catalog is knowable, and probing it would spend a
        real request to learn something the name already implies.
        """
        resolved = self.model_for(vendor, model)
        configured = vision_scale.settings_override(self.vendor_config(vendor), resolved)
        if configured is not None:
            return configured
        from_catalog = self._models_dev_vision(vendor, resolved)
        if from_catalog is not None:
            return from_catalog
        if content_parts.vision_from_name(vendor, resolved):
            return True
        if not self.is_local_style(vendor):
            # An unrecognised hosted model degrades to the text fallback, as it
            # always has: a 400 there costs a request and tells us nothing the
            # vendor's own catalog would not have.
            return False
        learned = self._vision_memory().get(
            vendor, self.base_url_for(vendor) or "", resolved
        )
        return learned

    def _vision_memory(self) -> vision_scale.VisionMemory:
        """The learned-capability store, pointed at this machine's home."""
        home = self.paths.home if self.paths is not None else None
        if home is not None and vision_scale.MEMORY.home != home:
            vision_scale.MEMORY.home = home
            vision_scale.MEMORY.clear()
        return vision_scale.MEMORY

    def remember_vision(self, vendor: str, model: str | None, vision: bool) -> None:
        """Record what a try-once probe found, so it is asked at most once."""
        self._vision_memory().remember(
            vendor, self.base_url_for(vendor) or "", self.model_for(vendor, model), vision
        )

    def _models_dev_vision(self, vendor: str, model: str) -> bool | None:
        """The public catalog's answer, from the disk cache only (no I/O)."""
        home = self.paths.home if self.paths is not None else resolve_home()
        catalog = model_discovery.models_dev_cached(home)
        return vision_scale.from_models_dev(
            catalog, model_discovery.MODELS_DEV_IDS.get(vendor, ()), model
        )

    def vision_map(self, vendor: str, models: list[str]) -> dict[str, bool]:
        """``{model: can_see}`` for the ids a picker is about to show.

        Only the models with a *known* answer appear; a missing key means "not
        known yet", which is what a surface draws as no badge rather than as a
        crossed-out eye.
        """
        known: dict[str, bool] = {}
        for name in models:
            answer = self.vision_for(vendor, name)
            if answer is not None:
                known[name] = answer
        return known

    def thinking_for(self, vendor: str) -> str:
        """``"on"`` | ``"off"`` | ``"auto"`` for ``vendor``.

        ``settings.providers.<vendor>.thinking`` wins over ``agent.thinking``;
        an unrecognised value is read as ``"auto"`` rather than refused, so a
        typo degrades to today's behaviour instead of failing every turn.
        """
        configured = self.vendor_config(vendor).get("thinking")
        if not isinstance(configured, str) or not configured:
            configured = self.settings.agent.thinking
        return configured if configured in THINKING_CHOICES else "auto"

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
        preset = self.preset_or_none(vendor)
        static = preset.context_window(resolved_model) if preset is not None else None
        if not self.is_local_style(vendor):
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
                # Only an API-key model id (gpt-4.1) is dropped.  A model the
                # account's own Codex catalog offers must be sent as chosen,
                # even when this build has never heard of it — the live list is
                # where new and preview models turn up first.
                if model_discovery.rejected_for_oauth(vendor, "chatgpt", model):
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
                if model_discovery.rejected_for_oauth(vendor, "google_oauth", model):
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
        preset = self.effective_preset(vendor, model)
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

        def learned(can_see: bool) -> None:
            """Persist what the try-once probe found (CORE-vision)."""
            self.remember_vision(vendor, model, can_see)

        return OpenAICompatProvider(
            preset,
            api_key=api_key,
            model=resolved_model,
            base_url=base_url,
            model_resolver=resolver,
            vision=self.vision_for(vendor, model),
            on_vision=learned,
        )

    def default_vendor(self) -> str:
        """``settings.providers.default``, else the first configured vendor."""
        env = (os.environ.get("SNOWPEA_PROVIDER") or "").strip()
        if env:
            env_vendor, _ = _split_env(env)
            if env_vendor:
                return env_vendor
        profile = self.default_profile()
        known = set(self.known_vendors())
        if profile is not None and profile[0] in known:
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
        for vendor in self.known_vendors():
            if self.is_configured(vendor):
                return vendor
        return DEFAULT_VENDOR

    # -- listing -------------------------------------------------------
    def list(self) -> list[ProviderInfo]:
        """Every known vendor with its models, auth methods and state."""
        default = self.default_vendor()
        infos: list[ProviderInfo] = []
        for vendor in self.known_vendors():
            preset = self.preset_or_none(vendor)
            if preset is None:  # pragma: no cover - known_vendors only yields describable ids
                continue
            config = self.vendor_config(vendor)
            # The same rungs ``model_listing`` uses, minus the live one: a
            # synchronous listing may not make eleven HTTP calls, but it must
            # not contradict the picker either, so a catalog discovered live
            # once is served from the cache here.
            models = model_discovery.offline_models(
                vendor,
                self.auth_method_for(vendor),
                config,
                home=self.paths.home if self.paths is not None else resolve_home(),
            )
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
                    preset="local" if preset.local_style else vendor,
                    custom=vendor not in PRESETS,
                    supportsEffort=preset.supports_effort,
                )
            )
        return infos


__all__ = ["FAKE_PREFIX", "NO_MODEL_HINT", "ProviderRegistry"]
