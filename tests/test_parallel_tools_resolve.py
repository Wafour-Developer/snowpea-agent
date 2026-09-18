"""Tests for parallel_tool_calls resolution."""

from __future__ import annotations

from snowpea_core.config.settings import Settings
from snowpea_core.providers.presets import PRESETS, resolve_parallel_tools
from snowpea_core.providers.registry import ProviderRegistry


def test_local_preset_defaults_parallel_on() -> None:
    assert PRESETS["local"].supports_parallel_tools is True


def test_deepseek_preset_defaults_parallel_off() -> None:
    assert PRESETS["deepseek"].supports_parallel_tools is False


def test_resolve_parallel_tools_vendor_override() -> None:
    preset = PRESETS["local"]
    assert resolve_parallel_tools(preset, vendor_config={"parallelToolCalls": False}) is False
    assert resolve_parallel_tools(preset, vendor_config={"supportsParallelTools": True}) is True


def test_resolve_parallel_tools_model_denylist() -> None:
    preset = PRESETS["deepseek"]
    assert (
        resolve_parallel_tools(preset, model="deepseek-v4-pro", vendor_config={}) is False
    )
    assert resolve_parallel_tools(PRESETS["qwen"], model="deepseek-v4-flash") is False


def test_resolve_parallel_tools_profile_override() -> None:
    preset = PRESETS["local"]
    assert (
        resolve_parallel_tools(
            preset,
            model="qwen38-flash-next",
            profile={"supportsParallelTools": False},
        )
        is False
    )


def test_registry_effective_preset_enables_parallel_for_local_model() -> None:
    registry = ProviderRegistry(
        Settings(
            providers={
                "hon2": {
                    "preset": "local",
                    "base_url": "http://hon2.example.com:8000/v1",
                    "model": "qwen38-flash-next",
                }
            }
        )
    )
    preset = registry.effective_preset("hon2")
    assert preset.supports_parallel_tools is True
