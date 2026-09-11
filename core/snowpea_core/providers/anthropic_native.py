"""Anthropic Messages API adapter (streaming).

Converts the vendor-neutral types from :mod:`snowpea_core.providers.base` to
and from the Anthropic wire format:

* ``ToolSpec`` -> ``{"name", "description", "input_schema"}``
* an assistant turn that called tools -> content blocks ``text`` + ``tool_use``
* a ``role="tool"`` message -> a *user* message holding a ``tool_result`` block

The stream yields ``text_delta`` as the model types, one ``tool_call`` per
completed ``tool_use`` block, then ``usage`` and ``done``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from snowpea_core.providers.base import (
    ChatMessage,
    ProviderError,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)

log = logging.getLogger("snowpea.providers.anthropic")

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 4096


def tool_specs_to_anthropic(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """``ToolSpec`` list -> the ``tools`` argument of the Messages API."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema or {"type": "object", "properties": {}},
        }
        for tool in tools
    ]


def _text_of(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))


def messages_to_anthropic(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]]]:
    """Split out the system prompt and convert the rest to Anthropic messages."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(_text_of(message.content))
            continue
        if message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": _text_of(message.content) or "(no output)",
            }
            if converted and converted[-1]["role"] == "user":
                previous = converted[-1]["content"]
                if isinstance(previous, list):
                    previous.append(block)
                    continue
            converted.append({"role": "user", "content": [block]})
            continue
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _text_of(message.content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.tool_calls or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        converted.append({"role": "user", "content": _text_of(message.content)})
    return "\n\n".join(part for part in system_parts if part), converted


class AnthropicProvider:
    """Streaming ``ChatProvider`` backed by ``anthropic.AsyncAnthropic``."""

    vendor = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")
        self._client: Any = None
        if not self._api_key:
            raise ProviderError("invalid_params", "anthropic: no API key configured")

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderError("internal", f"anthropic SDK unavailable: {exc}") from exc
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one assistant turn."""
        client = self._ensure_client()
        system, converted = messages_to_anthropic(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": converted,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = tool_specs_to_anthropic(tools)

        blocks: dict[int, dict[str, Any]] = {}
        usage = Usage()
        stop_reason: str | None = None
        try:
            async with client.messages.stream(**request) as stream:
                async for event in stream:
                    kind = getattr(event, "type", "")
                    if kind == "content_block_start":
                        block = getattr(event, "content_block", None)
                        blocks[int(getattr(event, "index", 0))] = {
                            "type": getattr(block, "type", "text"),
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "json": "",
                        }
                    elif kind == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", "")
                        if delta_type == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                yield StreamEvent(kind="text_delta", text=text)
                        elif delta_type == "input_json_delta":
                            entry = blocks.setdefault(
                                int(getattr(event, "index", 0)),
                                {"type": "tool_use", "id": "", "name": "", "json": ""},
                            )
                            entry["json"] += getattr(delta, "partial_json", "")
                    elif kind == "content_block_stop":
                        finished = blocks.get(int(getattr(event, "index", 0)))
                        if finished and finished.get("type") == "tool_use":
                            yield StreamEvent(
                                kind="tool_call",
                                tool_call=ToolCall(
                                    id=str(finished.get("id") or ""),
                                    name=str(finished.get("name") or ""),
                                    arguments=_parse_json(finished.get("json", "")),
                                ),
                            )
                    elif kind == "message_delta":
                        delta = getattr(event, "delta", None)
                        stop_reason = getattr(delta, "stop_reason", None) or stop_reason
                final = await stream.get_final_message()
                final_usage = getattr(final, "usage", None)
                if final_usage is not None:
                    usage = Usage(
                        input_tokens=int(getattr(final_usage, "input_tokens", 0) or 0),
                        output_tokens=int(getattr(final_usage, "output_tokens", 0) or 0),
                    )
                stop_reason = getattr(final, "stop_reason", None) or stop_reason
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - every SDK error becomes ProviderError
            log.warning("anthropic stream failed: %s", exc)
            raise ProviderError("internal", f"anthropic: {type(exc).__name__}: {exc}") from exc

        yield StreamEvent(kind="usage", usage=usage)
        yield StreamEvent(kind="done", stop_reason=stop_reason or "end_turn")


def _parse_json(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "AnthropicProvider",
    "messages_to_anthropic",
    "tool_specs_to_anthropic",
]
