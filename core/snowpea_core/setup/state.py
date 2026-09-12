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
from snowpea_core.config.settings import AudioSettings, Settings
from snowpea_core.setup import catalog

#: The id every screen carries as its last row.
SKIP = "__skip__"
SKIP_LABEL = "Skip — keep defaults"


@dataclass
class WizardState:
    """Everything the wizard will write, seeded from the current settings."""

    vendor: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    #: ``local`` only: vllm | ollama | lmstudio (picks the base_url default and quirks).
    variant: str | None = None
    #: True when settings.json already holds an API key for ``vendor`` (kept unless replaced).
    has_saved_key: bool = False
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
        saved = settings.providers.get(vendor) if vendor else None
        saved = saved if isinstance(saved, dict) else {}
        return cls(
            vendor=vendor,
            model=saved.get("model") or None,
            base_url=saved.get("base_url") or None,
            variant=saved.get("variant") or None,
            has_saved_key=bool(saved.get("api_key")),
            search_provider=settings.search.provider or catalog.DEFAULT_SEARCH_PROVIDER,
            search_credentials={
                str(pid): dict(block)
                for pid, block in (getattr(settings.search, "credentials", None) or {}).items()
                if isinstance(block, dict)
            },
            browser_provider=settings.browser.provider or catalog.DEFAULT_BROWSER_PROVIDER,
            tool_categories=defaults,
            gateways=gateways,
            **_audio_from(getattr(settings, "audio", None)),
        )

    # -- mutation ------------------------------------------------------
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

    # -- output --------------------------------------------------------
    def write(self, paths: Paths, settings: Settings) -> Settings:
        """Fold the answers into ``settings`` and persist them."""
        from snowpea_core.providers.registry import ProviderRegistry

        if self.vendor:
            registry = ProviderRegistry(settings)
            config: dict[str, Any] = {}
            if self.api_key:
                config["api_key"] = self.api_key
            if self.model:
                config["model"] = self.model
            if self.base_url:
                config["base_url"] = self.base_url
            if self.variant:
                config["variant"] = self.variant
            registry.configure(self.vendor, config)
            settings.providers["default"] = self.vendor

        settings.search.provider = self.search_provider
        for pid, block in self.search_credentials.items():
            existing = dict(settings.search.credentials.get(pid) or {})
            existing.update({k: v for k, v in block.items() if v})
            if existing:
                settings.search.credentials[pid] = existing
        settings.browser.provider = self.browser_provider
        settings.tools.enabled_categories = self.enabled_categories()
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
        lines = [
            f"provider   {self.vendor or '(none configured)'}"
            + (f"  model {self.model}" if self.model else ""),
            f"search     {self.search_provider}{self._search_key_note()}",
            f"browser    {self.browser_provider}",
            f"audio      in {self.stt_provider} · out {self.tts_provider}{self._voice_note()}",
            f"tools      {len(self.enabled_categories())} categories on"
            f" ({', '.join(self.enabled_categories())})",
            "messenger  " + (", ".join(self._gateway_labels()) or "(none)"),
        ]
        return lines + list(self.notes)

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


__all__ = ["SKIP", "SKIP_LABEL", "WizardState"]
