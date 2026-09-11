"""OpenAI-compatible ``/chat/completions`` adapter (M3 contract §2).

Nine of the eleven vendors speak this dialect: OpenAI, OpenRouter, xAI, GLM,
MiniMax, Kimi, DeepSeek, Qwen and any local vLLM / Ollama / LM Studio server.
The differences between them live in
:mod:`snowpea_core.providers.presets` (base URL, headers, parallel tool calls)
and are applied by :mod:`snowpea_core.providers.normalize`; this module only
does HTTP and SSE framing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from snowpea_core.providers import replay
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolSpec
from snowpea_core.providers.normalize import OpenAIStreamNormalizer, build_openai_request
from snowpea_core.providers.presets import PRESETS, VendorPreset

log = logging.getLogger("snowpea.providers.openai_compat")

DEFAULT_TIMEOUT = 300.0
DONE = "[DONE]"


class OpenAICompatProvider:
    """Streaming ``ChatProvider`` for any OpenAI-compatible endpoint."""

    def __init__(
        self,
        preset: VendorPreset | str = "openai",
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        resolved = PRESETS[preset] if isinstance(preset, str) else preset
        self.preset = resolved
        self.vendor = resolved.id
        self.model = model or resolved.default_model
        self._api_key = api_key
        self._base_url = (base_url or resolved.base_url or "").rstrip("/")
        self._headers = {**resolved.extra_headers, **(extra_headers or {})}
        self._timeout = timeout
        if not self._base_url:
            raise ProviderError("invalid_params", f"{self.vendor}: no base_url configured")
        if not api_key and self.vendor != "local" and not replay.is_replay():
            raise ProviderError("invalid_params", f"{self.vendor}: no API key configured")

    # -- HTTP ---------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            **self._headers,
        }
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": headers,
            "timeout": self._timeout,
        }
        transport = replay.transport_for(self.vendor)
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn, normalised to :class:`StreamEvent`."""
        body = build_openai_request(self.preset, self.model, messages, tools, max_tokens=max_tokens)
        normalizer = OpenAIStreamNormalizer(self.preset)
        try:
            async with self._client() as client:
                async with client.stream("POST", "/chat/completions", json=body) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")[:400]
                        raise ProviderError(
                            "internal",
                            f"{self.vendor}: HTTP {response.status_code}: {detail}",
                        )
                    async for payload in sse_payloads(response):
                        for event in normalizer.feed(payload):
                            yield event
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            log.warning("%s stream failed: %s", self.vendor, exc)
            raise ProviderError("internal", f"{self.vendor}: {type(exc).__name__}: {exc}") from exc
        for event in normalizer.finish():
            yield event


async def sse_payloads(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Decode ``data:`` lines of an SSE response into JSON objects."""
    buffer: list[str] = []
    async for line in response.aiter_lines():
        stripped = line.strip()
        if not stripped:
            payload = _join(buffer)
            buffer.clear()
            if payload is not None:
                yield payload
            continue
        if stripped.startswith(":"):
            continue
        if stripped.startswith("data:"):
            buffer.append(stripped[5:].strip())
    payload = _join(buffer)
    if payload is not None:
        yield payload


def _join(buffer: list[str]) -> dict[str, Any] | None:
    if not buffer:
        return None
    raw = "\n".join(buffer).strip()
    if not raw or raw == DONE:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        log.debug("skipping unparsable SSE payload: %r", raw[:200])
        return None
    return value if isinstance(value, dict) else None


__all__ = ["DEFAULT_TIMEOUT", "OpenAICompatProvider", "sse_payloads"]
