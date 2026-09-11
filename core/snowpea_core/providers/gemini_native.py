"""Google Gemini adapter — ``streamGenerateContent?alt=sse`` (M3 contract §2).

Gemini is its own wire format: ``contents`` with ``parts`` instead of messages
with content, ``functionDeclarations`` instead of ``tools``, and ``functionCall``
/ ``functionResponse`` parts instead of tool calls and tool results.  The
translation lives in :mod:`snowpea_core.providers.normalize`; this module does
HTTP, auth and SSE framing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from snowpea_core.providers import replay
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolSpec
from snowpea_core.providers.normalize import GeminiStreamNormalizer, build_gemini_request
from snowpea_core.providers.openai_compat import sse_payloads
from snowpea_core.providers.presets import PRESETS, VendorPreset

log = logging.getLogger("snowpea.providers.gemini")

DEFAULT_TIMEOUT = 300.0


class GeminiProvider:
    """Streaming ``ChatProvider`` for the Gemini generative-language API."""

    vendor = "gemini"

    def __init__(
        self,
        preset: VendorPreset | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.preset = preset or PRESETS["gemini"]
        self.model = model or self.preset.default_model
        self._api_key = api_key
        self._base_url = (base_url or self.preset.base_url or "").rstrip("/")
        self._timeout = timeout
        if not api_key and not replay.is_replay():
            raise ProviderError("invalid_params", "gemini: no API key configured")

    def _client(self) -> httpx.AsyncClient:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self._api_key:
            # Header auth keeps the key out of URLs, logs and fixtures.
            headers["x-goog-api-key"] = self._api_key
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
        body = build_gemini_request(messages, tools, max_tokens=max_tokens)
        path = f"/models/{self.model}:streamGenerateContent"
        normalizer = GeminiStreamNormalizer(self.preset)
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST", path, params={"alt": "sse"}, json=body
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")[:400]
                        raise ProviderError(
                            "internal", f"gemini: HTTP {response.status_code}: {detail}"
                        )
                    async for payload in sse_payloads(response):
                        for event in normalizer.feed(payload):
                            yield event
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            log.warning("gemini stream failed: %s", exc)
            raise ProviderError("internal", f"gemini: {type(exc).__name__}: {exc}") from exc
        for event in normalizer.finish():
            yield event


__all__ = ["DEFAULT_TIMEOUT", "GeminiProvider"]
