"""The answers the wizard collects, independent of how they were collected.

Screens mutate a :class:`WizardState`; :func:`WizardState.write` is the single
place that turns those answers into ``$SNOWPEA_HOME/settings.json``.  Keeping
the two apart is what lets the non-interactive flags and the interactive
screens share one code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import DEFAULT_AGENT_TEAM, AudioSettings, ModelProfile, Settings
from snowpea_core.setup import catalog

#: The id every screen carries as its last row.
SKIP = "__skip__"
SKIP_LABEL = "Skip — keep defaults"


@dataclass
class WizardState:
    """Everything the wizard will write, seeded from the current settings."""

    vendor: str | None = None
    api_key: str | None = None
    oauth_token: str | None = None
    auth_method: str | None = None
    model: str | None = None
    base_url: str | None = None
    #: ``local`` only: vllm | ollama | lmstudio (picks the base_url default and quirks).
    variant: str | None = None
    #: True when settings.json already holds an API key for ``vendor`` (kept unless replaced).
    has_saved_key: bool = False
    #: Per-provider LLM credentials/configuration retained across provider switches.
    provider_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Named model profiles.  Profiles point at provider credentials without
    #: duplicating keys/base URLs, so the same provider can host many models.
    model_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    default_model: str | None = None
    agent_models: dict[str, str] = field(default_factory=dict)
    search_provider: str = catalog.DEFAULT_SEARCH_PROVIDER
    #: provider id -> credential block, e.g. ``{"exa": {"api_key": "..."}}``.
    #: Only providers the run actually touched appear here.
    search_credentials: dict[str, dict[str, Any]] = field(default_factory=dict)
    browser_provider: str = catalog.DEFAULT_BROWSER_PROVIDER
    #: Voice in and out.  ``"off"`` is a real answer, distinct from ``"auto"``:
    #: it means "never listen" / "never speak" rather than "pick for me".
    stt_provider: str = catalog.DEFAULT_STT_PROVIDER
    stt_command: str | None = None
    tts_provider: str = catalog.DEFAULT_TTS_PROVIDER
    tts_command: str | None = None
    tts_voice: str | None = None
    #: True when replies are spoken without being asked each time.
    auto_speak: bool = False
    #: category id -> enabled.
    tool_categories: dict[str, bool] = field(default_factory=dict)
    #: ``skills.registry.token``; ``None`` keeps whatever is already saved.
    registry_token: str | None = None
    #: True once ``settings.json`` already has a registry token (kept unless replaced).
    has_saved_registry_token: bool = False
    #: gateway id -> its config block (``{"enabled": True, "token": "...",
    #: "allowed_user_id": "123"}``).
    gateways: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Lines the providers screen shows above the list (detected keys, imports).
    hints: list[str] = field(default_factory=list)
    #: Human-readable record of what the run did, printed as the summary.
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings) -> WizardState:
        """Start from what is already configured, not from a blank sheet."""
        defaults = {item.id: item.default for item in catalog.tools_catalog()}
        enabled = list(getattr(settings.tools, "enabled_categories", []) or [])
        if enabled:
            defaults = {cid: cid in enabled for cid in defaults}
        gateways = {
            str(gid): dict(block)
            for gid, block in (getattr(settings, "gateway", None) or {}).items()
            if isinstance(block, dict)
        }
        vendor = (
            str(settings.providers.get("default"))
            if isinstance(settings.providers.get("default"), str)
            else None
        )
        provider_configs = {
            str(pid): dict(block)
            for pid, block in settings.providers.items()
            if isinstance(block, dict)
        }
        profiles = {
            str(pid): {"provider": profile.provider, "model": profile.model}
            for pid, profile in settings.models.profiles.items()
        }
        default_profile = settings.models.default
        if not default_profile and vendor:
            maybe = provider_configs.get(vendor) or {}
            legacy_model = maybe.get("model")
            if isinstance(legacy_model, str) and legacy_model:
                default_profile = profile_id(vendor, legacy_model)
                profiles.setdefault(default_profile, {"provider": vendor, "model": legacy_model})
        saved = provider_configs.get(vendor or "") if vendor else None
        saved = saved if isinstance(saved, dict) else {}
        return cls(
            vendor=vendor,
            model=saved.get("model") or None,
            base_url=saved.get("base_url") or None,
            variant=saved.get("variant") or None,
            has_saved_key=bool(saved.get("api_key")),
            provider_configs=provider_configs,
            model_profiles=profiles,
            default_model=default_profile,
            agent_models={
                str(name): str(profile)
                for name, profile in getattr(settings.agents, "models", {}).items()
                if isinstance(name, str) and isinstance(profile, str)
            },
            search_provider=settings.search.provider or catalog.DEFAULT_SEARCH_PROVIDER,
            search_credentials={
                str(pid): dict(block)
                for pid, block in (getattr(settings.search, "credentials", None) or {}).items()
                if isinstance(block, dict)
            },
            browser_provider=settings.browser.provider or catalog.DEFAULT_BROWSER_PROVIDER,
            tool_categories=defaults,
            gateways=gateways,
            has_saved_registry_token=bool(
                getattr(getattr(settings, "skills", None), "registry", None)
                and getattr(settings.skills.registry, "token", None)
            ),
            **_audio_from(getattr(settings, "audio", None)),
        )

    # -- mutation ------------------------------------------------------

    def select_vendor(self, vendor: str) -> None:
        """Switch the active provider without leaking credentials across vendors."""
        self.vendor = vendor
        saved = self.provider_configs.get(vendor) or {}
        self.api_key = None
        self.oauth_token = None
        self.auth_method = str(saved.get("auth_method") or "") or None
        self.model = str(saved.get("model") or "") or None
        self.base_url = str(saved.get("base_url") or "") or None
        self.variant = str(saved.get("variant") or "") or None
        self.has_saved_key = bool(
            saved.get("api_key") or saved.get("token") or saved.get("oauth_token")
        )

    def remember_current_provider(self) -> None:
        """Store the current provider fields in the per-vendor cache."""
        if not self.vendor:
            return
        block = dict(self.provider_configs.get(self.vendor) or {})
        if self.api_key:
            block["api_key"] = self.api_key
            block.pop("oauth_token", None)
            block.pop("auth_method", None)
        if self.oauth_token:
            block["oauth_token"] = self.oauth_token
            block["auth_method"] = "oauth_token"
            block.pop("api_key", None)
        elif self.auth_method:
            block["auth_method"] = self.auth_method
        if self.model:
            block["model"] = self.model
        if self.base_url:
            block["base_url"] = self.base_url
        if self.variant:
            block["variant"] = self.variant
        if block:
            self.provider_configs[self.vendor] = block

    def add_current_model_profile(self, *, make_default: bool = False) -> str | None:
        """Add the active provider/model as a reusable model profile."""
        if not self.vendor or not self.model:
            return None
        self.remember_current_provider()
        pid = profile_id(self.vendor, self.model)
        self.model_profiles[pid] = {"provider": self.vendor, "model": self.model}
        if make_default or not self.default_model:
            self.default_model = pid
        return pid

    def set_default_model(self, profile: str | None) -> None:
        if profile and profile in self.model_profiles:
            self.default_model = profile

    def assign_agent_model(self, agent: str, profile: str | None) -> None:
        agent = agent.strip()
        if not agent:
            return
        if not profile:
            self.agent_models.pop(agent, None)
        elif profile in self.model_profiles:
            self.agent_models[agent] = profile

    def audio_block(self) -> dict[str, Any]:
        """The ``settings.audio`` object these answers describe."""
        stt: dict[str, Any] = {"provider": self.stt_provider}
        if self.stt_command:
            stt["command"] = self.stt_command
        tts: dict[str, Any] = {
            "enabled": self.tts_provider != catalog.AUDIO_OFF,
            "provider": self.tts_provider,
            "autoSpeak": self.auto_speak,
        }
        if self.tts_command:
            tts["command"] = self.tts_command
        if self.tts_voice:
            tts["voice"] = self.tts_voice
        return {"stt": stt, "tts": tts}

    def enabled_categories(self) -> list[str]:
        """The enabled ids in catalog order."""
        return [cid for cid in catalog.known_categories() if self.tool_categories.get(cid)]

    def set_categories(self, selected: set[str]) -> None:
        """Replace the whole set (what the multi-select screen hands back)."""
        self.tool_categories = {cid: cid in selected for cid in catalog.known_categories()}

    def apply_tools_flag(self, spec: str) -> list[str]:
        """``"vision,-git"`` — enable, disable with a leading ``-``.

        Returns the ids it did not recognise so the caller can complain.
        """
        unknown: list[str] = []
        known = set(catalog.known_categories())
        for raw in spec.split(","):
            token = raw.strip()
            if not token:
                continue
            off = token.startswith("-")
            cid = token[1:] if off else token
            if cid not in known:
                unknown.append(cid)
                continue
            self.tool_categories[cid] = not off
        return unknown

    def enable_gateway(
        self,
        gateway_id: str,
        token: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Turn a messenger on, keeping any token/user id already configured.

        ``user_id`` is the platform account allowed to answer approvals from
        chat.  Without it the binding is fail-closed and can approve nothing,
        so the wizard asks for it right after the token.
        """
        block = dict(self.gateways.get(gateway_id) or {})
        block["enabled"] = True
        if token:
            block["token"] = token
        if user_id:
            block["allowed_user_id"] = str(user_id)
        self.gateways[gateway_id] = block

    def gateway_needs_answers(self, gateway_id: str) -> bool:
        """True while an enabled messenger is still missing its token or user id."""
        block = self.gateways.get(gateway_id) or {}
        if not block.get("enabled"):
            return False
        return not block.get("token") or not block.get("allowed_user_id")

    def enabled_gateways(self) -> list[str]:
        """Ids of the messengers that are on, in a stable order."""
        return sorted(gid for gid, block in self.gateways.items() if block.get("enabled"))

    def as_settings(self) -> Settings:
        """A :class:`Settings` view of the provider answers collected so far.

        The provider screen has to show the *current* state — what is saved in
        ``settings.json`` plus whatever this run has already answered — and the
        only object that can tell ``[active]`` from ``[inactive]`` is
        :class:`~snowpea_core.providers.registry.ProviderRegistry`.  Building
        this throwaway ``Settings`` is what lets the screen, ``snowpea provider
        list`` and the ``provider.list`` RPC all read the same helper instead of
        each deciding for itself (the wizard used to ask an *empty* registry and
        so printed ``[inactive]`` on every vendor).
        """
        settings = Settings()
        for vendor, block in self.provider_configs.items():
            clean = {key: value for key, value in dict(block).items() if value is not None}
            if clean:
                settings.providers[vendor] = clean
        if self.vendor:
            pending = dict(settings.providers.get(self.vendor) or {})
            if self.api_key:
                pending["api_key"] = self.api_key
            if self.oauth_token:
                pending["oauth_token"] = self.oauth_token
                pending["auth_method"] = "oauth_token"
            elif self.auth_method:
                pending["auth_method"] = self.auth_method
            if self.model:
                pending["model"] = self.model
            if self.base_url:
                pending["base_url"] = self.base_url
            if self.variant:
                pending["variant"] = self.variant
            if pending:
                settings.providers[self.vendor] = pending
            settings.providers["default"] = self.vendor
        settings.models.profiles = {
            pid: ModelProfile.model_validate(block)
            for pid, block in self.model_profiles.items()
            if block.get("provider") and block.get("model")
        }
        if self.default_model in settings.models.profiles:
            settings.models.default = self.default_model
        return settings

    # -- output --------------------------------------------------------
    def write(self, paths: Paths, settings: Settings) -> Settings:
        """Fold the answers into ``settings`` and persist them."""
        from snowpea_core.providers.registry import ProviderRegistry

        if not settings.agents.teams:
            settings.agents.teams["default"] = list(DEFAULT_AGENT_TEAM)
            settings.agents.default_team = "default"

        # Command-line setup uses the same multi-model format even though it
        # skips the interactive model-management questions.
        self.add_current_model_profile()
        self.remember_current_provider()
        registry = ProviderRegistry(settings)
        for vendor, config in self.provider_configs.items():
            if config:
                registry.configure(vendor, dict(config))
        if self.vendor:
            settings.providers["default"] = self.vendor
        settings.models.profiles = {
            pid: ModelProfile.model_validate(block)
            for pid, block in self.model_profiles.items()
            if block.get("provider") and block.get("model")
        }
        settings.models.default = (
            self.default_model if self.default_model in settings.models.profiles else None
        )
        settings.agents.models = {
            agent: profile
            for agent, profile in self.agent_models.items()
            if profile in settings.models.profiles
        }

        settings.search.provider = self.search_provider
        for pid, block in self.search_credentials.items():
            existing = dict(settings.search.credentials.get(pid) or {})
            existing.update({k: v for k, v in block.items() if v})
            if existing:
                settings.search.credentials[pid] = existing
        settings.browser.provider = self.browser_provider
        settings.tools.enabled_categories = self.enabled_categories()
        if self.registry_token:
            settings.skills.registry.token = self.registry_token
        settings.gateway = {
            gid: block for gid, block in self.gateways.items() if block.get("enabled")
        }
        # ``Settings`` allows extra keys, so the audio block round-trips through
        # settings.json before the typed model for it exists.
        current = getattr(settings, "audio", None)
        audio: dict[str, Any] = _as_dict(current)
        audio.update(self.audio_block())
        settings.audio = AudioSettings.model_validate(audio)
        settings.save(paths)
        return settings

    def summary(self) -> list[str]:
        """The lines ``snowpea setup`` prints when it is done."""
        profile_count = len(self.model_profiles)
        default = self.default_model or "(none)"
        assigned = len(self.agent_models)
        lines = [
            f"provider   {self.vendor or '(none configured)'}"
            + (f"  model {self.model}" if self.model else ""),
            f"models     {profile_count} profile" + ("s" if profile_count != 1 else "")
            + f" · default {default}"
            + (
                f" · {assigned} agent assignment" + ("s" if assigned != 1 else "")
                if assigned
                else ""
            ),
            f"search     {self.search_provider}{self._search_key_note()}",
            f"browser    {self.browser_provider}",
            f"audio      in {self.stt_provider} · out {self.tts_provider}{self._voice_note()}",
            f"tools      {len(self.enabled_categories())} categories on"
            f" ({', '.join(self.enabled_categories())})",
            f"registry   {self._registry_token_note()}",
            "messenger  " + (", ".join(self._gateway_labels()) or "(none)"),
        ]
        return lines + list(self.notes)

    def _registry_token_note(self) -> str:
        if self.registry_token:
            return "publisher token saved"
        if self.has_saved_registry_token:
            return "publisher token saved (kept)"
        return "no publisher token"

    def _voice_note(self) -> str:
        """``" · voice nova · auto-speak"`` — only what was actually chosen."""
        parts = []
        if self.tts_voice:
            parts.append(f"voice {self.tts_voice}")
        if self.auto_speak and self.tts_provider != catalog.AUDIO_OFF:
            parts.append("auto-speak")
        return (" · " + " · ".join(parts)) if parts else ""

    def _search_key_note(self) -> str:
        """``" (key saved)"`` / ``" (no key — will fall back)"`` for key providers."""
        from snowpea_core.tools import search_providers

        if not search_providers.needs_key(self.search_provider):
            return ""
        block = self.search_credentials.get(self.search_provider) or {}
        return " (key saved)" if block.get("api_key") else " (no key — will fall back)"

    def has_search_key(self, provider_id: str) -> bool:
        """True when this run knows an API key for ``provider_id``."""
        return bool((self.search_credentials.get(provider_id) or {}).get("api_key"))

    def set_search_key(self, provider_id: str, api_key: str) -> None:
        block = dict(self.search_credentials.get(provider_id) or {})
        block["api_key"] = api_key
        self.search_credentials[provider_id] = block

    def _gateway_labels(self) -> list[str]:
        """``telegram (user 12345)`` — the id, and who may approve from chat."""
        labels = []
        for gid in self.enabled_gateways():
            user = (self.gateways.get(gid) or {}).get("allowed_user_id")
            labels.append(f"{gid} (user {user})" if user else f"{gid} (no approver)")
        return labels


def profile_id(provider: str, model: str) -> str:
    """Stable id for a provider/model profile."""
    return f"{provider}:{model}"


def _as_dict(block: Any) -> dict[str, Any]:
    """``settings.audio`` as a plain dict, model or hand-written JSON alike."""
    if isinstance(block, dict):
        return dict(block)
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        result: dict[str, Any] = dump(mode="json")
        return result
    return {}


def _audio_from(block: Any) -> dict[str, Any]:
    """Seed the audio answers from ``settings.audio``, whatever shape it is in."""
    document = _as_dict(block)
    if not document:
        return {}
    stt: dict[str, Any] = _as_dict(document.get("stt"))
    tts: dict[str, Any] = _as_dict(document.get("tts"))
    provider = str(tts.get("provider") or catalog.DEFAULT_TTS_PROVIDER)
    if tts.get("enabled") is False:
        provider = catalog.AUDIO_OFF
    return {
        "stt_provider": str(stt.get("provider") or catalog.DEFAULT_STT_PROVIDER),
        "stt_command": stt.get("command") or None,
        "tts_provider": provider,
        "tts_command": tts.get("command") or None,
        "tts_voice": tts.get("voice") or None,
        "auto_speak": bool(tts.get("autoSpeak")),
    }


__all__ = ["SKIP", "SKIP_LABEL", "WizardState", "profile_id"]
