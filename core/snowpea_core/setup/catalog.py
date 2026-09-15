"""Everything the setup wizard can offer, as data (M3 contract §5).

Five catalogs, one per Hermes-style screen:

``vendor_catalog``   the eleven LLM vendors from ``providers/presets.py``
``search_catalog``   the web-search providers from ``tools/search_providers``
``browser_catalog``  the browser providers from ``tools/browser_providers``
``tools_catalog``    the tool categories and their default on/off state
``gateway_catalog``  telegram / discord / slack, all off

Both provider catalogs are derived from their registries rather than
duplicated here, so a provider added to ``tools/`` shows up in ``snowpea
setup`` without another edit.  The ordering AC (AC-02b) is enforced by
:func:`assert_free_first`, which every list goes through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from snowpea_core.audio import install as audio_install
from snowpea_core.providers.default_models import (
    DEFAULT_MODEL_TIMEOUT,
    DefaultModel,
    default_model_for,
    default_model_offline,
)
from snowpea_core.providers.presets import PRESETS
from snowpea_core.tools import browser_providers, search_providers

Tier = Literal["free", "paid", "subscription"]
KeyKind = Literal["no key", "key optional", "key required", "self-hosted"]

#: Marks the first (recommended) item of a single-select screen.
STAR = "★"


class CatalogOrderError(AssertionError):
    """Raised when a catalog violates the free-first ordering of AC-02b."""


@dataclass(frozen=True)
class CatalogItem:
    """One selectable row on a setup screen."""

    id: str
    label: str
    tier: Tier
    key: KeyKind
    default: bool = False
    description: str = ""
    #: ``False`` for things listed but not usable yet (media tools, for one).
    active: bool = True
    #: Extra tags appended after ``[active]``/``[inactive]``, e.g. ``default``.
    extra_tags: tuple[str, ...] = ()
    #: True when ``audio.install`` could obtain this row without root, so a
    #: surface can draw an Install button next to it (like ``lsp.catalog``).
    installable: bool = False
    #: The command a user would run by hand, when there is one.  Set for a
    #: system package we will not install for them, and as a fallback next to
    #: an Install button.
    install_hint: str | None = None
    #: True for the one row a screen should lead with and pre-select.  The
    #: ``(recommended)`` suffix in the tags is what a surface renders.
    recommended: bool = False
    #: The model this vendor row would start a session with, resolved live
    #: rather than read off the preset (CORE-default-models).  Empty on every
    #: catalog but the vendor one.
    default_model: str = ""
    #: Which rung produced :attr:`default_model` — ``live``, ``models.dev`` or
    #: ``preset``.  Shown next to the id so a fallback never passes for the
    #: account's own answer.
    default_model_source: str = ""

    @property
    def tags(self) -> tuple[str, ...]:
        """``("free · no key", "active")`` — what the screen prints."""
        tags = [f"{self.tier} · {self.key}"]
        if self.recommended:
            tags.append("recommended")
        if self.default_model:
            tags.append(f"default: {self.default_model} ({self.default_model_source})")
        # ``default`` comes before the active/inactive state so it survives the
        # ellipsis on a narrow terminal: which vendor a session will actually
        # use is the more useful of the two.
        tags.extend(self.extra_tags)
        tags.append("active" if self.active else "inactive")
        return tuple(tags)

    def rank(self) -> int:
        """0 free and keyless, 1 free but gated, 2 paid or subscription."""
        return rank_of(self.tier, self.key)


def rank_of(tier: str, key: str) -> int:
    """The AC-02b sort bucket for a ``(tier, key)`` pair."""
    if tier == "free":
        return 0 if key == "no key" else 1
    return 2


def assert_free_first(items: Sequence[CatalogItem]) -> list[CatalogItem]:
    """Check AC-02b: free·no-key → free·key/self-hosted → paid, ★ first.

    Returns ``items`` as a list so callers can use it inline.
    """
    ranks = [item.rank() for item in items]
    if ranks != sorted(ranks):
        raise CatalogOrderError(
            "catalog order must be free·no-key, then free·key/self-hosted, then paid: "
            + ", ".join(f"{item.id}({item.tier}/{item.key})" for item in items)
        )
    if items and not items[0].default:
        raise CatalogOrderError(f"the first catalog item ({items[0].id}) must be the default")
    if sum(1 for item in items if item.default) > 1:
        raise CatalogOrderError("a single-select catalog may only mark one default")
    return list(items)


def _sorted_by_rank(items: Iterable[CatalogItem]) -> list[CatalogItem]:
    """Stable sort into the three AC-02b buckets, registry order within each."""
    return sorted(items, key=lambda item: item.rank())


def _with_default(items: Sequence[CatalogItem], default_id: str) -> list[CatalogItem]:
    """Mark exactly ``default_id`` as the default (and nothing else)."""
    return [
        CatalogItem(
            id=item.id,
            label=item.label,
            tier=item.tier,
            key=item.key,
            default=item.id == default_id,
            description=item.description,
            active=item.active,
            extra_tags=item.extra_tags,
            default_model=item.default_model,
            default_model_source=item.default_model_source,
        )
        for item in items
    ]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

DEFAULT_SEARCH_PROVIDER = "ddgs"
DEFAULT_BROWSER_PROVIDER = "local_chromium"


def search_catalog() -> list[CatalogItem]:
    """The web-search providers, free and keyless first (AC-02b)."""
    items = [
        CatalogItem(
            id=meta.id,
            label=meta.label,
            tier=meta.tier,
            key=meta.key,
            description=meta.endpoint,
            active=True,
        )
        for meta in search_providers.metas()
    ]
    ordered = _sorted_by_rank(items)
    known = any(item.id == DEFAULT_SEARCH_PROVIDER for item in ordered)
    default = DEFAULT_SEARCH_PROVIDER if known else (ordered[0].id if ordered else "")
    ordered = _with_default(ordered, default)
    # The default has to lead the list for the ★ to sit on row one.
    ordered.sort(key=lambda item: (item.rank(), not item.default))
    return assert_free_first(ordered)


def browser_catalog() -> list[CatalogItem]:
    """The browser providers, same ordering rule as search."""
    items = [
        CatalogItem(
            id=meta.id,
            label=meta.label,
            tier=meta.tier,
            key=meta.key,
            description=meta.endpoint,
            active=meta.id == DEFAULT_BROWSER_PROVIDER,
        )
        for meta in browser_providers.metas()
    ]
    ordered = _sorted_by_rank(items)
    ordered = _with_default(ordered, DEFAULT_BROWSER_PROVIDER)
    ordered.sort(key=lambda item: (item.rank(), not item.default))
    return assert_free_first(ordered)


# ---------------------------------------------------------------------------
# tool categories
# ---------------------------------------------------------------------------

#: ``(id, label, default-on, active, description)`` — contract §5's table.
_TOOL_CATEGORIES: tuple[tuple[str, str, bool, bool, str], ...] = (
    ("file", "Files (read, write, edit, glob, grep)", True, True, ""),
    ("terminal", "Terminal (shell, background processes)", True, True, ""),
    ("git", "Git (status, diff, commit)", True, True, ""),
    ("web", "Web search and extract", True, True, "free providers need no key"),
    ("browser", "Browser control (headless Chromium)", True, True, ""),
    ("delegate", "Delegate to sub-agents", True, True, ""),
    ("schedule", "Schedule work for later", True, True, ""),
    ("memory", "Memory (notes that survive a session)", True, True, ""),
    ("skills", "Skills (loadable instruction packs)", True, True, ""),
    ("todo", "Todo list", True, True, ""),
    ("session-search", "Search past sessions", True, True, ""),
    ("clarify", "Ask the user a clarifying question", True, True, ""),
    ("cron", "Cron jobs", True, True, ""),
    ("media-image", "Image generation", True, False, "needs the media MCP server"),
    ("media-video", "Video generation", True, False, "needs the media MCP server"),
    ("media-tts", "Text to speech", True, False, "needs the media MCP server"),
    ("vision", "Vision (read images)", False, True, ""),
    ("computer-use", "Computer use (drive the desktop)", False, True, ""),
    ("x-search", "X / Twitter search", False, True, "needs an xAI key"),
)


def tools_catalog() -> list[CatalogItem]:
    """Every tool category with its default state; order is the table's."""
    return [
        CatalogItem(
            id=cid,
            label=label,
            tier="free",
            key="no key",
            default=on,
            description=description,
            active=active,
        )
        for cid, label, on, active, description in _TOOL_CATEGORIES
    ]


def default_enabled_categories() -> list[str]:
    """The ids that are ON by default, including the inactive media ones."""
    return [item.id for item in tools_catalog() if item.default]


def known_categories() -> list[str]:
    return [item.id for item in tools_catalog()]


# ---------------------------------------------------------------------------
# audio (speech to text, text to speech)
# ---------------------------------------------------------------------------

#: Picked when the user says nothing: let the machine decide, local first.
DEFAULT_STT_PROVIDER = "auto"
DEFAULT_TTS_PROVIDER = "auto"

#: The "no voice at all" row both audio screens end with.
AUDIO_OFF = "off"

#: ``(id, label, key kind, description)`` for speech to text.  Free and keyless
#: first so :func:`assert_free_first` is satisfied; the hosted API is last.
_STT_CHOICES: tuple[tuple[str, str, KeyKind, str], ...] = (
    ("auto", "Automatic — whichever local engine is installed, else OpenAI", "no key", ""),
    (
        "sherpa-onnx-sensevoice",
        "SenseVoiceSmall (CPU)",
        "no key",
        "zh/en/ja/ko/yue, ~17-20x real time, its own VAD — the recommended default",
    ),
    (
        "sherpa-onnx-zipformer-ko",
        "sherpa-onnx Korean Zipformer INT8 (CPU)",
        "no key",
        "streaming Korean, ~10-38x real time",
    ),
    (
        "sherpa-onnx-zipformer-en",
        "sherpa-onnx English Zipformer INT8 (CPU)",
        "no key",
        "streaming English",
    ),
    ("local-whisper", "Local whisper CLI", "no key", "whisper or faster-whisper on PATH"),
    ("command", "Custom command", "no key", "a command template containing {path}"),
    (AUDIO_OFF, "Off — no speech input", "no key", ""),
    ("openai", "OpenAI (whisper-1 / gpt-4o-transcribe)", "key required", "reuses your OpenAI key"),
)

#: The row each voice screen leads with and pre-selects when nothing is
#: installed.  Both are CPU-only and local, which is what makes them a default
#: rather than a recommendation to buy something.
RECOMMENDED_STT = "sherpa-onnx-sensevoice"
RECOMMENDED_TTS = "supertonic"

#: ``(id, label, key kind, description)`` for text to speech.
_TTS_CHOICES: tuple[tuple[str, str, KeyKind, str], ...] = (
    ("auto", "Automatic — whichever local voice is installed, else OpenAI", "no key", ""),
    (
        "supertonic",
        "Supertonic (CPU)",
        "no key",
        "on-device neural voices, 31 languages incl. ko/en — the recommended default",
    ),
    ("espeak-ng", "espeak-ng", "no key", "small, robotic, everywhere"),
    ("piper", "Piper", "no key", "local neural voices"),
    ("edge-tts", "edge-tts", "no key", "Microsoft neural voices, needs the network"),
    ("say", "macOS say", "no key", "built into macOS"),
    ("powershell", "Windows SAPI", "no key", "built into Windows"),
    ("command", "Custom command", "no key", "a template containing {text} and {out}"),
    (AUDIO_OFF, "Off — never speak", "no key", ""),
    ("openai", "OpenAI (tts-1 / gpt-4o-mini-tts)", "key required", "reuses your OpenAI key"),
)


def stt_catalog(detected: Sequence[str] = ()) -> list[CatalogItem]:
    """Speech-to-text choices; ``detected`` marks the ones usable right now."""
    usable = set(detected)
    return assert_free_first(
        [
            CatalogItem(
                id=cid,
                label=label,
                tier="free" if key == "no key" else "paid",
                key=key,
                default=cid == DEFAULT_STT_PROVIDER,
                description=description,
                active=cid in {"auto", AUDIO_OFF} or cid in usable,
                installable=audio_install.is_installable(cid),
                install_hint=audio_install.install_hint(cid),
                recommended=cid == RECOMMENDED_STT,
            )
            for cid, label, key, description in _STT_CHOICES
        ]
    )


def tts_catalog(detected: Sequence[str] = ()) -> list[CatalogItem]:
    """Text-to-speech choices; ``detected`` marks the ones usable right now."""
    usable = set(detected)
    return assert_free_first(
        [
            CatalogItem(
                id=cid,
                label=label,
                tier="free" if key in {"no key", "self-hosted"} else "paid",
                key=key,
                default=cid == DEFAULT_TTS_PROVIDER,
                description=description,
                active=cid in {"auto", AUDIO_OFF} or cid in usable,
                installable=audio_install.is_installable(cid),
                install_hint=audio_install.install_hint(cid),
                recommended=cid == RECOMMENDED_TTS,
            )
            for cid, label, key, description in _TTS_CHOICES
        ]
    )


# ---------------------------------------------------------------------------
# gateways
# ---------------------------------------------------------------------------

_GATEWAYS: tuple[tuple[str, str, str], ...] = (
    ("telegram", "Telegram", "bot token from @BotFather"),
    ("discord", "Discord", "bot token from the developer portal"),
    ("slack", "Slack", "bot token from the Slack app config"),
)


def gateway_catalog() -> list[CatalogItem]:
    """Chat gateways — all off until the user pastes a token."""
    return [
        CatalogItem(
            id=gid,
            label=label,
            tier="free",
            key="key required",
            default=False,
            description=description,
            active=False,
        )
        for gid, label, description in _GATEWAYS
    ]


# ---------------------------------------------------------------------------
# vendors
# ---------------------------------------------------------------------------


async def vendor_default_models(
    settings: Any = None,
    *,
    home: Any = None,
    timeout: float = DEFAULT_MODEL_TIMEOUT,
    transport: Any = None,
) -> dict[str, DefaultModel]:
    """``{vendor: DefaultModel}`` for every row :func:`vendor_catalog` will draw.

    The vendors this machine has a credential for are asked; the rest are
    answered from the models.dev catalog without any per-vendor I/O.  Every
    probe runs concurrently under one :data:`DEFAULT_MODEL_TIMEOUT` budget, so
    the screen costs about as long as the slowest *single* vendor and never
    more (CORE-default-models).
    """
    from snowpea_core.config.paths import resolve_home
    from snowpea_core.providers.registry import ProviderRegistry

    registry = ProviderRegistry(settings) if settings is not None else ProviderRegistry()
    resolved_home = home
    if resolved_home is None:
        resolved_home = registry.paths.home if registry.paths is not None else resolve_home()
    vendors = [*PRESETS, *registry.custom_vendors()]

    async def one(vendor: str) -> tuple[str, DefaultModel]:
        preset = registry.preset_or_none(vendor)
        return vendor, await default_model_for(
            vendor,
            preset=preset,
            configured=registry.auth_status(vendor) == "active",
            auth_method=registry.auth_method_for(vendor),
            api_key=registry.api_key_for(vendor),
            base_url=registry.base_url_for(vendor),
            credentials=registry.vendor_config(vendor),
            home=resolved_home,
            timeout=timeout,
            transport=transport,
        )

    try:
        resolved = await asyncio.wait_for(
            asyncio.gather(*(one(vendor) for vendor in vendors)), timeout * 2
        )
    except (TimeoutError, asyncio.CancelledError):
        return {
            vendor: default_model_offline(
                vendor, preset=registry.preset_or_none(vendor), home=resolved_home
            )
            for vendor in vendors
        }
    return dict(resolved)


def vendor_catalog(
    settings: Any = None, defaults: Mapping[str, DefaultModel] | None = None
) -> list[CatalogItem]:
    """The eleven vendors, in preset order, tagged with their login methods.

    ``settings`` (a :class:`~snowpea_core.config.settings.Settings`) decides the
    ``active`` flag: a vendor is active once it is configured, either by a key
    in ``settings.providers`` or by one of its environment variables.

    ``defaults`` is :func:`vendor_default_models`' answer.  Without it each row
    falls back to :func:`default_model_offline`, which reads the models.dev
    cache already on disk and asks no one anything — a synchronous caller gets
    a current default without a network call, and an async one can pass the
    live answer in.
    """
    from snowpea_core.config.paths import resolve_home
    from snowpea_core.providers.registry import ProviderRegistry

    registry = ProviderRegistry(settings) if settings is not None else ProviderRegistry()
    default_vendor = registry.default_vendor() if settings is not None else None
    home = registry.paths.home if registry.paths is not None else resolve_home()
    items: list[CatalogItem] = []
    # The presets first, then the OpenAI-compatible servers the user named, so
    # the IDE's first-run wizard and the CLI screen show the same rows.
    for vendor in [*PRESETS, *registry.custom_vendors()]:
        preset = registry.preset_or_none(vendor)
        if preset is None:  # pragma: no cover - both sources are describable
            continue
        chosen = (defaults or {}).get(vendor) or default_model_offline(
            vendor, preset=preset, home=home
        )
        logins = [m for m in preset.auth_methods if m != "api_key"]
        description = f"{chosen.model} ({chosen.detail})" if chosen.model else ""
        if preset.local_style:
            # A self-hosted server's "default" is the placeholder until it has
            # been asked; where it lives is the useful line here.
            description = registry.base_url_for(vendor) or preset.default_model
        if logins:
            description += "  (web login: " + ", ".join(logins) + ")"
        if registry.auth_status(vendor) == "expired":
            description += "  — login expired, sign in again"
        items.append(
            CatalogItem(
                id=vendor,
                label=preset.label,
                tier="free" if preset.local_style else "paid",
                key="self-hosted" if preset.local_style else "key required",
                default=False,
                description=description,
                # An expired OAuth session is configured but not usable, and
                # showing it as ``[active]`` is what left users staring at a
                # vendor that 401s on every prompt (report §6.5).
                active=registry.auth_status(vendor) == "active",
                extra_tags=("default",) if vendor == default_vendor else (),
                default_model="" if preset.local_style else chosen.model,
                default_model_source="" if preset.local_style else chosen.detail,
            )
        )
    return items


def vendor_auth_tags(vendor: str) -> tuple[str, ...]:
    """``("api_key", "device_code")`` for one vendor.

    A named OpenAI-compatible server is not in :data:`PRESETS` but takes an
    optional key like the built-in local one, so it gets ``("api_key",)``
    rather than an empty row.
    """
    preset = PRESETS.get(vendor)
    return tuple(preset.auth_methods) if preset else ("api_key",)


__all__ = [
    "AUDIO_OFF",
    "DEFAULT_BROWSER_PROVIDER",
    "DEFAULT_SEARCH_PROVIDER",
    "DEFAULT_STT_PROVIDER",
    "DEFAULT_TTS_PROVIDER",
    "STAR",
    "CatalogItem",
    "CatalogOrderError",
    "KeyKind",
    "Tier",
    "assert_free_first",
    "browser_catalog",
    "default_enabled_categories",
    "gateway_catalog",
    "known_categories",
    "rank_of",
    "search_catalog",
    "RECOMMENDED_STT",
    "RECOMMENDED_TTS",
    "stt_catalog",
    "tools_catalog",
    "tts_catalog",
    "vendor_auth_tags",
    "vendor_catalog",
    "vendor_default_models",
]
