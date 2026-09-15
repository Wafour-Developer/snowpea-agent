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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from snowpea_core.providers import content, replay
from snowpea_core.providers import effort as effort_scale
from snowpea_core.providers import models as model_discovery
from snowpea_core.providers import vision as vision_scale
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolSpec
from snowpea_core.providers.normalize import OpenAIStreamNormalizer, build_openai_request
from snowpea_core.providers.presets import PRESETS, VendorPreset

log = logging.getLogger("snowpea.providers.openai_compat")

DEFAULT_TIMEOUT = 300.0
DONE = "[DONE]"


class OpenAICompatProvider:
    """Streaming ``ChatProvider`` for any OpenAI-compatible endpoint."""

    #: ``chat_template_kwargs`` reaches every server in this dialect, so the
    #: agent loop may ask this adapter to turn thinking off.
    supports_thinking_option = True
    #: And how hard to think, for the vendors and models that take it.
    supports_effort_option = True

    def __init__(
        self,
        preset: VendorPreset | str = "openai",
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        model_resolver: Callable[[], Awaitable[str]] | None = None,
        vision: bool | None = None,
        on_vision: Callable[[bool], None] | None = None,
    ) -> None:
        resolved = PRESETS[preset] if isinstance(preset, str) else preset
        self.preset = resolved
        self.vendor = resolved.id
        self.model = model or resolved.default_model
        self._api_key = api_key
        self._base_url = (base_url or resolved.base_url or "").rstrip("/")
        self._headers = {**resolved.extra_headers, **(extra_headers or {})}
        self._timeout = timeout
        #: Called once, at first use, when :attr:`model` is still a placeholder.
        self._model_resolver = model_resolver
        #: ``True``/``False`` settle it; ``None`` means "try once and learn"
        #: — see :mod:`snowpea_core.providers.vision`.
        self._vision = vision
        self._on_vision = on_vision
        if not self._base_url:
            raise ProviderError("invalid_params", f"{self.vendor}: no base_url configured")
        if not api_key and resolved.key_required and not replay.is_replay():
            raise ProviderError("invalid_params", f"{self.vendor}: no API key configured")

    def _tag(self) -> str:
        """``local (qwen3-8b)`` — every error names the vendor *and* the model."""
        return f"{self.vendor} ({self.model})" if self.model else self.vendor

    def _learned_vision(self, model: str, can_see: bool) -> None:
        """Record the probe's answer, in this adapter and on disk."""
        self._vision = can_see
        if self._on_vision is not None:
            self._on_vision(can_see)

    def _forget_vision(self, model: str) -> None:
        self._learned_vision(model, False)

    def _effort_for(self, model: str, effort: str | None) -> str | None:
        """The tier to send for ``model``, or ``None`` to send none.

        Three gates, all of which must pass: the vendor's API takes the field,
        this model's family takes it, and this model has not already refused
        it in this process.
        """
        if not effort or not self.preset.supports_effort:
            return None
        if not self.preset.local_style and not effort_scale.supports_openai_effort(model):
            # A self-hosted server opted in by configuration, so its model ids
            # are not matched against OpenAI's reasoning families.
            return None
        if effort_scale.UNSUPPORTED.refused(self.vendor, model):
            return None
        return effort_scale.normalize(effort)

    async def _retry(
        self, client: Any, body: dict[str, Any], normalizer: Any
    ) -> AsyncIterator[StreamEvent]:
        """Re-send a body the server refused, with the effort field removed."""
        async with client.stream("POST", "/chat/completions", json=body) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")[:400]
                raise ProviderError(
                    "internal", f"{self._tag()}: HTTP {response.status_code}: {detail}"
                )
            async for payload in sse_payloads(response):
                for event in normalizer.feed(payload):
                    yield event

    async def _ensure_model(self) -> str:
        """Resolve a placeholder model id before it can reach the server.

        ``local``'s preset default is ``local-model``, which every OpenAI-
        compatible server rejects with a 404; the registry hands us a resolver
        that asks ``GET /models`` and picks the first id instead.
        """
        if replay.is_replay():
            # Fixtures were recorded against whatever model id they name; there
            # is no server to ask and no request to protect.
            return self.model
        if model_discovery.is_placeholder(self.model) and self._model_resolver is not None:
            self.model = await self._model_resolver()
            self._model_resolver = None
        if model_discovery.is_placeholder(self.model):
            raise ProviderError(
                "model_not_configured",
                f"{self.vendor}: no model configured; run `snowpea setup provider`, "
                f"or pick one in a session with `/model <name>`",
            )
        return self.model

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
        thinking: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn, normalised to :class:`StreamEvent`."""
        model = await self._ensure_model()
        wanted = self._effort_for(model, effort)
        # ``None`` is the optimistic case: nobody knows whether this server's
        # model can see, so the images go out and the answer teaches us
        # (CORE-vision).
        probing = self._vision is None and content.messages_have_images(messages)
        body = build_openai_request(
            self.preset,
            model,
            messages,
            tools,
            max_tokens=max_tokens,
            thinking=thinking,
            effort=wanted,
            vision=True if probing else self._vision,
        )
        normalizer = OpenAIStreamNormalizer(self.preset)
        try:
            async with self._client() as client:
                async with client.stream("POST", "/chat/completions", json=body) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")[:400]
                        if probing and vision_scale.is_rejection(response.status_code, detail):
                            # The server cannot take image parts. Remember it,
                            # say so once, and finish the turn with the text
                            # fallback rather than failing it.
                            self._forget_vision(model)
                            log.warning(
                                "%s refused image content (%s); "
                                "falling back to the text description",
                                self._tag(),
                                detail[:120],
                            )
                            async for event in self._retry(
                                client,
                                build_openai_request(
                                    self.preset,
                                    model,
                                    messages,
                                    tools,
                                    max_tokens=max_tokens,
                                    thinking=thinking,
                                    effort=wanted,
                                    vision=False,
                                ),
                                normalizer,
                            ):
                                yield event
                            return
                        if wanted and effort_scale.UNSUPPORTED.is_unsupported_error(detail):
                            # The supported-model list is a guess about someone
                            # else's catalog; a wrong guess costs one retry, not
                            # every prompt from here on (CORE-effort).
                            effort_scale.UNSUPPORTED.remember(self.vendor, model)
                            body.pop("reasoning_effort", None)
                            async for event in self._retry(client, body, normalizer):
                                yield event
                            return
                        raise ProviderError(
                            "internal",
                            f"{self._tag()}: HTTP {response.status_code}: {detail}",
                        )
                    if probing:
                        # It took the images: never probe this one again.
                        self._learned_vision(model, True)
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
