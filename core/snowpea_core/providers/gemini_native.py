"""Google Gemini adapter — ``streamGenerateContent?alt=sse`` (M3 contract §2).

Gemini is its own wire format: ``contents`` with ``parts`` instead of messages
with content, ``functionDeclarations`` instead of ``tools``, and ``functionCall``
/ ``functionResponse`` parts instead of tool calls and tool results.  The
translation lives in :mod:`snowpea_core.providers.normalize`; this module does
HTTP, auth and SSE framing.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
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
    #: No thinking switch on this backend; the agent loop does not offer one.
    supports_thinking_option = False
    #: Effort becomes ``thinkingConfig.thinkingBudget`` (CORE-effort).
    supports_effort_option = True

    def __init__(
        self,
        preset: VendorPreset | None = None,
        *,
        api_key: str | None = None,
        auth_method: str | None = None,
        oauth_token: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.preset = preset or PRESETS["gemini"]
        self.model = model or self.preset.default_model
        self._api_key = api_key
        self._auth_method = auth_method
        self._oauth_token_value = oauth_token
        self._base_url = (base_url or self.preset.base_url or "").rstrip("/")
        self._timeout = timeout
        if (
            not api_key
            and auth_method not in ("google_adc", "oauth_token")
            and not replay.is_replay()
        ):
            raise ProviderError("invalid_params", "gemini: no API key or Google OAuth configured")

    def _tag(self) -> str:
        """``gemini (gemini-2.5-pro)`` — errors name the vendor *and* the model."""
        return f"{self.vendor} ({self.model})" if self.model else self.vendor

    async def _oauth_token(self) -> str:
        executable = shutil.which("gcloud")
        if not executable:
            raise ProviderError("invalid_params", "gemini: gcloud is required for Google OAuth")
        process = await asyncio.create_subprocess_exec(
            executable,
            "auth",
            "application-default",
            "print-access-token",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        token = stdout.decode().strip()
        if process.returncode or not token:
            detail = stderr.decode("utf-8", "replace").strip()[-300:]
            raise ProviderError(
                "invalid_params", f"gemini: could not refresh Google OAuth token: {detail}"
            )
        return token

    async def _client(self) -> httpx.AsyncClient:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self._api_key:
            # Header auth keeps the key out of URLs, logs and fixtures.
            headers["x-goog-api-key"] = self._api_key
        elif self._auth_method == "google_adc":
            headers["authorization"] = f"Bearer {await self._oauth_token()}"
        elif self._auth_method == "oauth_token" and self._oauth_token_value:
            headers["authorization"] = f"Bearer {self._oauth_token_value}"
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
        thinking: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn, normalised to :class:`StreamEvent`."""
        body = build_gemini_request(
            messages, tools, max_tokens=max_tokens, thinking=thinking, effort=effort
        )
        path = f"/models/{self.model}:streamGenerateContent"
        normalizer = GeminiStreamNormalizer(self.preset)
        try:
            async with await self._client() as client:
                async with client.stream(
                    "POST", path, params={"alt": "sse"}, json=body
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")[:400]
                        raise ProviderError(
                            "internal", f"{self._tag()}: HTTP {response.status_code}: {detail}"
                        )
                    async for payload in sse_payloads(response):
                        for event in normalizer.feed(payload):
                            yield event
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            log.warning("%s stream failed: %s", self._tag(), exc)
            raise ProviderError(
                "internal", f"{self._tag()}: {type(exc).__name__}: {exc}"
            ) from exc
        for event in normalizer.finish():
            yield event


__all__ = ["DEFAULT_TIMEOUT", "GeminiProvider"]
