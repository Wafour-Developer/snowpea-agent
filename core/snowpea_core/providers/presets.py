"""The eleven supported LLM vendors (M3 contract §1, plan §1.2, §2.8).

Every vendor quirk that the adapters care about is declared here — which
adapter class speaks to it, which wire shape its stream deltas use, whether it
honours parallel tool calls — so that :mod:`snowpea_core.providers.normalize`
can stay the single normalisation point (plan §6 risk 3).

OpenAI offers a browser PKCE login (with device code as the headless
fallback), OpenRouter uses PKCE, and Gemini offers Google's browser consent
(with ``gcloud`` Application Default Credentials as the alternative).

A vendor's *adapter* is not always the one named here: signing in with a
ChatGPT or Google account produces an OAuth session that the vendor's API-key
endpoint rejects, so :class:`~snowpea_core.providers.registry.ProviderRegistry`
routes those to ``codex_transport`` / ``gemini_codeassist_transport`` instead
(CORE-codex-login).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger("snowpea.providers.presets")

Adapter = Literal["anthropic_native", "gemini_native", "openai_compat"]
WireShape = Literal["openai", "anthropic", "gemini"]


@dataclass(frozen=True)
class VendorPreset:
    """Static description of one vendor."""

    id: str
    label: str
    adapter: Adapter
    #: ``openai_compat``/``gemini_native`` API root; ``None`` for the SDK-driven
    #: Anthropic adapter.  ``local`` ships the Ollama default and expects the
    #: user to override it.
    base_url: str | None
    default_model: str
    auth_methods: tuple[str, ...] = ("api_key",)
    env_keys: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    supports_parallel_tools: bool = True
    tool_call_style: WireShape = "openai"
    stream_delta_shape: WireShape = "openai"
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Set on the ``local-*`` sub-presets only (vllm | ollama | lmstudio).
    variant: str | None = None
    #: ``False`` for vendors that authenticate with nothing at all — a
    #: self-hosted OpenAI-compatible server is configured by its ``base_url``,
    #: not by a key, and must count as set up once one is saved.
    key_required: bool = True
    #: True for the built-in ``local`` preset, its variants, and every named
    #: OpenAI-compatible server a user adds.  Everything that used to test
    #: ``vendor == "local"`` tests this instead, so a server called ``hon2``
    #: gets the same keyless auth, model discovery and context-window probing.
    local_style: bool = False
    #: True when this vendor's API takes a reasoning-effort setting at all.
    #: The *model* still has to accept it — see
    #: :func:`snowpea_core.providers.effort.supports_openai_effort` — but a
    #: vendor with this False is never sent one (CORE-effort).
    supports_effort: bool = False

    @property
    def vendor(self) -> str:
        """Alias of :attr:`id` (the name used by the RPC surface)."""
        return self.id

    def context_window(self, model: str | None = None) -> int | None:
        """Static context window of ``model`` in tokens, or ``None`` if unknown.

        Defaults to :attr:`default_model`.  This is the table lookup only; a
        settings override and ``local`` server discovery live on
        :class:`~snowpea_core.providers.registry.ProviderRegistry`, which is
        the object that knows this machine's configuration (CORE-context).
        """
        from snowpea_core.providers.context_windows import static_window

        return static_window(model or self.default_model)


_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/snowpea/snowpea-agent",
    "X-Title": "Snowpea",
}


def _preset(
    vendor_id: str,
    label: str,
    base_url: str | None,
    default_model: str,
    *,
    adapter: Adapter = "openai_compat",
    auth_methods: tuple[str, ...] = ("api_key",),
    env_keys: tuple[str, ...] = (),
    models: tuple[str, ...] = (),
    supports_parallel_tools: bool = True,
    tool_call_style: WireShape = "openai",
    stream_delta_shape: WireShape = "openai",
    extra_headers: dict[str, str] | None = None,
    variant: str | None = None,
    key_required: bool = True,
    local_style: bool = False,
    supports_effort: bool = False,
) -> VendorPreset:
    return VendorPreset(
        id=vendor_id,
        label=label,
        adapter=adapter,
        base_url=base_url,
        default_model=default_model,
        auth_methods=auth_methods,
        env_keys=env_keys,
        models=models or (default_model,),
        supports_parallel_tools=supports_parallel_tools,
        tool_call_style=tool_call_style,
        stream_delta_shape=stream_delta_shape,
        extra_headers=dict(extra_headers or {}),
        variant=variant,
        key_required=key_required,
        local_style=local_style,
        supports_effort=supports_effort,
    )


#: The eleven vendors, in the order the setup wizard and ``provider.list`` show.
PRESETS: dict[str, VendorPreset] = {
    preset.id: preset
    for preset in (
        _preset(
            "anthropic",
            "Anthropic",
            None,
            "claude-sonnet-5",
            adapter="anthropic_native",
            env_keys=("ANTHROPIC_API_KEY",),
            models=("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"),
            tool_call_style="anthropic",
            stream_delta_shape="anthropic",
            supports_effort=True,
        ),
        _preset(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            "gpt-6-astra",
            # Browser first: a ChatGPT session is what most people have, and
            # device code is the same account through a headless route.
            auth_methods=("api_key", "browser_pkce", "device_code", "oauth_token"),
            env_keys=("OPENAI_API_KEY",),
            models=("gpt-6-astra", "gpt-5.6", "gpt-5.4-nano"),
            supports_effort=True,
        ),
        _preset(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "anthropic/claude-sonnet-5",
            auth_methods=("api_key", "oauth_pkce"),
            env_keys=("OPENROUTER_API_KEY",),
            models=("anthropic/claude-sonnet-5", "openai/gpt-6-astra"),
            extra_headers=_OPENROUTER_HEADERS,
            supports_effort=True,
        ),
        _preset(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-3.8-flash",
            adapter="gemini_native",
            auth_methods=("api_key", "google_oauth", "google_adc", "oauth_token"),
            env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            models=("gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest"),
            tool_call_style="gemini",
            stream_delta_shape="gemini",
            supports_effort=True,
        ),
        _preset(
            "xai",
            "xAI Grok",
            "https://api.x.ai/v1",
            "grok-4.6",
            env_keys=("XAI_API_KEY",),
            models=("grok-4.6", "grok-4.5"),
            supports_effort=True,
        ),
        _preset(
            "glm",
            "Zhipu GLM",
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-5.3",
            env_keys=("GLM_API_KEY", "ZHIPUAI_API_KEY"),
            models=("glm-5.3", "glm-5.3-flash"),
        ),
        _preset(
            "minimax",
            "MiniMax",
            "https://api.minimax.chat/v1",
            "MiniMax-M3",
            env_keys=("MINIMAX_API_KEY",),
            models=("MiniMax-M3", "MiniMax-M2.7"),
        ),
        _preset(
            "kimi",
            "Moonshot Kimi",
            "https://api.moonshot.cn/v1",
            "kimi-k3",
            env_keys=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
            models=("kimi-k3", "kimi-k2.7-code"),
        ),
        _preset(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            "deepseek-v4-pro",
            env_keys=("DEEPSEEK_API_KEY",),
            models=("deepseek-v4-pro", "deepseek-v4-flash"),
            # DeepSeek emits one tool call per assistant turn.
            supports_parallel_tools=False,
        ),
        _preset(
            "qwen",
            "Qwen",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen3.8-max",
            env_keys=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
            models=("qwen3.8-max", "qwen3.8-flash"),
        ),
        _preset(
            "local",
            "Local / OpenAI-compatible servers",
            "http://localhost:11434/v1",
            "local-model",
            env_keys=("SNOWPEA_LOCAL_API_KEY",),
            models=(),
            supports_parallel_tools=False,
            key_required=False,
            local_style=True,
        ),
    )
}

#: ``local`` sub-presets: the same vendor id, a different default ``base_url``.
LOCAL_VARIANTS: dict[str, VendorPreset] = {
    "vllm": _preset(
        "local",
        "vLLM (local)",
        "http://localhost:8000/v1",
        "local-model",
        env_keys=("SNOWPEA_LOCAL_API_KEY",),
        supports_parallel_tools=False,
        key_required=False,
        local_style=True,
        variant="vllm",
    ),
    "ollama": _preset(
        "local",
        "Ollama (local)",
        "http://localhost:11434/v1",
        "local-model",
        env_keys=("SNOWPEA_LOCAL_API_KEY",),
        supports_parallel_tools=False,
        key_required=False,
        local_style=True,
        variant="ollama",
    ),
    "lmstudio": _preset(
        "local",
        "LM Studio (local)",
        "http://localhost:1234/v1",
        "local-model",
        env_keys=("SNOWPEA_LOCAL_API_KEY",),
        supports_parallel_tools=False,
        key_required=False,
        local_style=True,
        variant="lmstudio",
    ),
}

#: Backwards-compatible alias (``PRESETS`` used to be a tuple).
PRESETS_BY_VENDOR: dict[str, VendorPreset] = PRESETS

DEFAULT_VENDOR = "anthropic"
DEFAULT_MODEL = PRESETS["anthropic"].default_model

#: Vendors with an interactive login flow.
WEB_LOGIN_VENDORS: tuple[str, ...] = tuple(
    preset.id for preset in PRESETS.values() if len(preset.auth_methods) > 1
)


#: ``providers.<name>.preset`` values that mean "an OpenAI-compatible server".
LOCAL_PRESET_NAMES: frozenset[str] = frozenset({"local", "openai-compatible"})

#: Variant ids a local-style vendor may declare.  ``generic`` is "a plain
#: OpenAI-compatible server" — no vendor quirk, no default port to guess.
LOCAL_VARIANT_IDS: tuple[str, ...] = ("vllm", "ollama", "lmstudio", "generic")

#: What a named server may be called.  Lower-case so it reads the same in a
#: model reference (``hon2:flash-next-mtp``) as in ``settings.json``.
CUSTOM_VENDOR_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def is_local_preset_name(value: Any) -> bool:
    """True for the ``preset`` markers that declare a local-style vendor."""
    return isinstance(value, str) and value.strip().lower() in LOCAL_PRESET_NAMES


def is_local_vendor_config(config: Any) -> bool:
    """True when a ``providers.<name>`` block declares ``preset: local``."""
    return isinstance(config, Mapping) and is_local_preset_name(config.get("preset"))


def validate_custom_vendor_id(vendor_id: str) -> str:
    """Return ``vendor_id`` if it may name a user-added server, else raise.

    Two rules: the name must be a lower-case identifier (it travels inside a
    model reference, where ``:`` already means "end of vendor"), and it must
    not shadow one of the built-in presets — a block called ``openai`` that
    quietly became a self-hosted server would route every ``openai:`` model
    somewhere the user never intended.
    """
    if not isinstance(vendor_id, str) or not CUSTOM_VENDOR_ID_RE.match(vendor_id):
        raise ValueError(
            f"invalid provider name: {vendor_id!r} "
            "(lower-case letters, digits, '-' and '_', starting with a letter, max 32)"
        )
    if vendor_id in PRESETS:
        raise ValueError(f"{vendor_id!r} is a built-in provider; pick another name")
    return vendor_id


def local_vendor_ids(providers: Mapping[str, Any]) -> list[str]:
    """Names under ``settings.providers`` that are local-style servers.

    ``local`` itself is included when it is configured, because the surfaces
    that list "your OpenAI-compatible servers" must show it alongside the named
    ones.  A block whose key cannot be a vendor id is skipped rather than
    raising: one hand-edited typo must not stop the daemon from starting.
    """
    found: list[str] = []
    for name, block in providers.items():
        if name == "local" and isinstance(block, Mapping):
            found.append(name)
            continue
        if not is_local_vendor_config(block):
            continue
        try:
            validate_custom_vendor_id(str(name))
        except ValueError as exc:
            log.warning("ignoring providers.%s: %s", name, exc)
            continue
        found.append(str(name))
    return found


def synthesize_local_preset(vendor_id: str, config: Mapping[str, Any]) -> VendorPreset:
    """A :class:`VendorPreset` for one named OpenAI-compatible server.

    The block is the whole description: ``base_url`` is where it lives,
    ``variant`` says which server software it is (for the Ollama listing
    fallback and the URL the wizard suggests), and ``label`` is what pickers
    print.  Everything else matches the built-in ``local`` preset, which is
    the point — a named server is not a new kind of vendor, only another one.
    """
    local = PRESETS["local"]
    raw_variant = config.get("variant")
    variant = str(raw_variant).strip() if isinstance(raw_variant, str) else ""
    variant = variant if variant in LOCAL_VARIANT_IDS else ""
    fallback = LOCAL_VARIANTS.get(variant)
    base_url = config.get("base_url")
    resolved = str(base_url).strip() if isinstance(base_url, str) else ""
    label = config.get("label")
    return _preset(
        vendor_id,
        str(label).strip() if isinstance(label, str) and str(label).strip() else vendor_id,
        resolved or (fallback.base_url if fallback else local.base_url),
        local.default_model,
        env_keys=(),
        models=(),
        supports_parallel_tools=False,
        key_required=False,
        local_style=True,
        # A self-hosted server usually ignores ``reasoning_effort`` and
        # sometimes rejects it outright, so it is opt-in per server.
        supports_effort=bool(config.get("effort_param")),
        variant=variant or None,
    )


def preset_for(
    vendor: str, variant: str | None = None, config: Mapping[str, Any] | None = None
) -> VendorPreset:
    """Look up a preset, honouring the ``local`` variants and named servers.

    ``config`` is ``settings.providers[vendor]``.  When it declares
    ``preset: local`` the answer is synthesized from the block, so a server the
    user called ``hon2`` is a first-class vendor everywhere a built-in one is.
    """
    if vendor == "local" and variant:
        if variant in LOCAL_VARIANTS:
            return LOCAL_VARIANTS[variant]
        if variant in LOCAL_VARIANT_IDS:
            return PRESETS["local"]
        raise KeyError(f"unknown local variant: {variant}")
    if vendor in PRESETS:
        return PRESETS[vendor]
    if is_local_vendor_config(config):
        assert config is not None
        return synthesize_local_preset(vendor, config)
    raise KeyError(vendor)


@dataclass
class VendorState:
    """A preset plus whether this machine has credentials for it."""

    preset: VendorPreset
    configured: bool = False
    models: list[str] = field(default_factory=list)


__all__ = [
    "CUSTOM_VENDOR_ID_RE",
    "DEFAULT_MODEL",
    "DEFAULT_VENDOR",
    "LOCAL_PRESET_NAMES",
    "LOCAL_VARIANTS",
    "LOCAL_VARIANT_IDS",
    "PRESETS",
    "PRESETS_BY_VENDOR",
    "WEB_LOGIN_VENDORS",
    "Adapter",
    "VendorPreset",
    "VendorState",
    "WireShape",
    "is_local_preset_name",
    "is_local_vendor_config",
    "local_vendor_ids",
    "preset_for",
    "synthesize_local_preset",
    "validate_custom_vendor_id",
]
