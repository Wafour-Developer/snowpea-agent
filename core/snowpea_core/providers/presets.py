"""The eleven supported LLM vendors (plan §1.2, §2.8).

Only the Anthropic adapter exists at M1; the rest are declared so
``provider.list`` and the setup wizard (M3) agree on names, default models and
which login flows a vendor supports.  Exactly two vendors offer a web login:
OpenAI (device code) and OpenRouter (OAuth PKCE).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VendorPreset:
    """Static description of one vendor."""

    vendor: str
    label: str
    models: tuple[str, ...] = ()
    auth_methods: tuple[str, ...] = ("api_key",)
    env_keys: tuple[str, ...] = ()
    adapter: str = "openai_compat"


PRESETS: tuple[VendorPreset, ...] = (
    VendorPreset(
        vendor="anthropic",
        label="Anthropic",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
        env_keys=("ANTHROPIC_API_KEY",),
        adapter="anthropic_native",
    ),
    VendorPreset(
        vendor="openai",
        label="OpenAI",
        models=("gpt-4.1", "gpt-4.1-mini", "o4-mini"),
        auth_methods=("api_key", "device_code"),
        env_keys=("OPENAI_API_KEY",),
    ),
    VendorPreset(
        vendor="openrouter",
        label="OpenRouter",
        models=("anthropic/claude-sonnet-4.5", "openai/gpt-4.1"),
        auth_methods=("api_key", "oauth_pkce"),
        env_keys=("OPENROUTER_API_KEY",),
    ),
    VendorPreset(
        vendor="gemini",
        label="Google Gemini",
        models=("gemini-2.5-pro", "gemini-2.5-flash"),
        env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        adapter="gemini_native",
    ),
    VendorPreset(
        vendor="xai",
        label="xAI Grok",
        models=("grok-4", "grok-3-mini"),
        env_keys=("XAI_API_KEY",),
    ),
    VendorPreset(
        vendor="glm",
        label="Zhipu GLM",
        models=("glm-4.6", "glm-4.5-air"),
        env_keys=("GLM_API_KEY", "ZHIPUAI_API_KEY"),
    ),
    VendorPreset(
        vendor="minimax",
        label="MiniMax",
        models=("minimax-m2",),
        env_keys=("MINIMAX_API_KEY",),
    ),
    VendorPreset(
        vendor="kimi",
        label="Moonshot Kimi",
        models=("kimi-k2", "moonshot-v1-128k"),
        env_keys=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    ),
    VendorPreset(
        vendor="deepseek",
        label="DeepSeek",
        models=("deepseek-chat", "deepseek-reasoner"),
        env_keys=("DEEPSEEK_API_KEY",),
    ),
    VendorPreset(
        vendor="qwen",
        label="Qwen",
        models=("qwen3-max", "qwen3-coder"),
        env_keys=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    ),
    VendorPreset(
        vendor="local",
        label="OpenAI-compatible local (vLLM / Ollama / LM Studio)",
        models=(),
        env_keys=("SNOWPEA_LOCAL_BASE_URL",),
    ),
)

PRESETS_BY_VENDOR: dict[str, VendorPreset] = {preset.vendor: preset for preset in PRESETS}

DEFAULT_VENDOR = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-5"

#: Vendors with a browser login flow (plan §2.8): exactly two.
WEB_LOGIN_VENDORS: tuple[str, ...] = tuple(
    preset.vendor for preset in PRESETS if len(preset.auth_methods) > 1
)


@dataclass
class VendorState:
    """A preset plus whether this machine has credentials for it."""

    preset: VendorPreset
    configured: bool = False
    models: list[str] = field(default_factory=list)


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_VENDOR",
    "PRESETS",
    "PRESETS_BY_VENDOR",
    "WEB_LOGIN_VENDORS",
    "VendorPreset",
    "VendorState",
]
