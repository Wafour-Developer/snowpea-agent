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

from dataclasses import dataclass, field
from typing import Literal

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
    )


#: The eleven vendors, in the order the setup wizard and ``provider.list`` show.
PRESETS: dict[str, VendorPreset] = {
    preset.id: preset
    for preset in (
        _preset(
            "anthropic",
            "Anthropic",
            None,
            "claude-sonnet-4-5",
            adapter="anthropic_native",
            env_keys=("ANTHROPIC_API_KEY",),
            models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
            tool_call_style="anthropic",
            stream_delta_shape="anthropic",
        ),
        _preset(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            "gpt-4.1",
            # Browser first: a ChatGPT session is what most people have, and
            # device code is the same account through a headless route.
            auth_methods=("api_key", "browser_pkce", "device_code", "oauth_token"),
            env_keys=("OPENAI_API_KEY",),
            models=("gpt-4.1", "gpt-4.1-mini", "o4-mini"),
        ),
        _preset(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "anthropic/claude-sonnet-4.5",
            auth_methods=("api_key", "oauth_pkce"),
            env_keys=("OPENROUTER_API_KEY",),
            models=("anthropic/claude-sonnet-4.5", "openai/gpt-4.1"),
            extra_headers=_OPENROUTER_HEADERS,
        ),
        _preset(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-2.5-pro",
            adapter="gemini_native",
            auth_methods=("api_key", "google_oauth", "google_adc", "oauth_token"),
            env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            models=("gemini-2.5-pro", "gemini-2.5-flash"),
            tool_call_style="gemini",
            stream_delta_shape="gemini",
        ),
        _preset(
            "xai",
            "xAI Grok",
            "https://api.x.ai/v1",
            "grok-4",
            env_keys=("XAI_API_KEY",),
            models=("grok-4", "grok-3-mini"),
        ),
        _preset(
            "glm",
            "Zhipu GLM",
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-4.6",
            env_keys=("GLM_API_KEY", "ZHIPUAI_API_KEY"),
            models=("glm-4.6", "glm-4.5-air"),
        ),
        _preset(
            "minimax",
            "MiniMax",
            "https://api.minimax.chat/v1",
            "minimax-m2",
            env_keys=("MINIMAX_API_KEY",),
            models=("minimax-m2",),
        ),
        _preset(
            "kimi",
            "Moonshot Kimi",
            "https://api.moonshot.cn/v1",
            "kimi-k2",
            env_keys=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
            models=("kimi-k2", "moonshot-v1-128k"),
        ),
        _preset(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            "deepseek-chat",
            env_keys=("DEEPSEEK_API_KEY",),
            models=("deepseek-chat", "deepseek-reasoner"),
            # DeepSeek emits one tool call per assistant turn.
            supports_parallel_tools=False,
        ),
        _preset(
            "qwen",
            "Qwen",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen3-max",
            env_keys=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
            models=("qwen3-max", "qwen3-coder"),
        ),
        _preset(
            "local",
            "OpenAI-compatible local (vLLM / Ollama / LM Studio)",
            "http://localhost:11434/v1",
            "local-model",
            env_keys=("SNOWPEA_LOCAL_API_KEY",),
            models=(),
            supports_parallel_tools=False,
            key_required=False,
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


def preset_for(vendor: str, variant: str | None = None) -> VendorPreset:
    """Look up a preset, honouring the ``local`` variants."""
    if vendor == "local" and variant:
        try:
            return LOCAL_VARIANTS[variant]
        except KeyError:  # pragma: no cover - guarded by callers
            raise KeyError(f"unknown local variant: {variant}") from None
    return PRESETS[vendor]


@dataclass
class VendorState:
    """A preset plus whether this machine has credentials for it."""

    preset: VendorPreset
    configured: bool = False
    models: list[str] = field(default_factory=list)


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_VENDOR",
    "LOCAL_VARIANTS",
    "PRESETS",
    "PRESETS_BY_VENDOR",
    "WEB_LOGIN_VENDORS",
    "Adapter",
    "VendorPreset",
    "VendorState",
    "WireShape",
    "preset_for",
]
