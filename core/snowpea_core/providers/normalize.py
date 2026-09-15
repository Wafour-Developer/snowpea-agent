"""The single normalisation point between vendor wire formats and the core.

Plan §6 risk 3: eleven vendors claim to speak "OpenAI compatible" but differ in
`tool_choice`, parallel tool calls and the shape of their streaming deltas.
Rather than sprinkling ``if vendor == ...`` through the adapters, every quirk is
expressed as a declarative flag on :class:`~snowpea_core.providers.presets.VendorPreset`
and consumed here.

Two directions:

* **request** — ``ChatMessage``/``ToolSpec`` to the OpenAI or Gemini body.
* **response** — raw stream chunks to :class:`~snowpea_core.providers.base.StreamEvent`,
  always in the order ``text_delta* → tool_call* → usage → done``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from snowpea_core.providers import content
from snowpea_core.providers import effort as effort_scale
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, ToolSpec, Usage
from snowpea_core.providers.presets import VendorPreset

#: Vendor finish reasons to the core's ``done`` stop reasons.
STOP_REASONS: dict[str, str] = {
    # OpenAI-compatible
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "error",
    # Gemini
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "error",
    "RECITATION": "error",
    "OTHER": "error",
}


def stop_reason(raw: str | None, *, had_tool_calls: bool) -> str:
    """Map a vendor finish reason, letting a tool call win over ``stop``."""
    if had_tool_calls:
        return "tool_use"
    if not raw:
        return "end_turn"
    return STOP_REASONS.get(raw, "end_turn")


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool-call arguments as a dict, whatever the vendor sent."""
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def text_of(content: str | list[dict[str, Any]]) -> str:
    """Flatten a ``ChatMessage.content`` to plain text."""
    if isinstance(content, str):
        return content
    return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))


# ---------------------------------------------------------------------------
# request: OpenAI-compatible
# ---------------------------------------------------------------------------


def tool_specs_to_openai(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """``ToolSpec`` list to the ``tools`` array of ``/chat/completions``."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def messages_to_openai(
    messages: list[ChatMessage], *, vision: bool = False
) -> list[dict[str, Any]]:
    """``ChatMessage`` list to the ``messages`` array of ``/chat/completions``.

    ``vision`` says whether this model can be sent image parts.  When it
    cannot, an attached image becomes an ``[image attached: name]`` marker in
    the text, which is why the argument defaults to the safe answer.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": text_of(message.content) or "(no output)",
                }
            )
            continue
        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": text_of(message.content)}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
                if not entry["content"]:
                    entry["content"] = None
            out.append(entry)
            continue
        if message.role == "user" and content.has_blocks(message.content):
            parts = content.parts_from_blocks(message.content)  # type: ignore[arg-type]
            out.append({"role": "user", "content": content.to_openai(parts, vision=vision)})
            continue
        out.append({"role": message.role, "content": text_of(message.content)})
    return out


#: What a Qwen-style server wants in order *not* to think.  vLLM and SGLang
#: both forward ``chat_template_kwargs`` into the chat template, and the
#: Qwen3 template reads ``enable_thinking`` from it; a server whose template
#: has no such variable ignores the key rather than failing the request.
THINKING_OFF_TEMPLATE_KWARGS: dict[str, Any] = {"enable_thinking": False}


def build_openai_request(
    preset: VendorPreset,
    model: str,
    messages: list[ChatMessage],
    tools: list[ToolSpec],
    *,
    max_tokens: int,
    include_usage: bool = True,
    thinking: str | None = None,
    effort: str | None = None,
    vision: bool | None = None,
) -> dict[str, Any]:
    """The full JSON body for a streaming ``/chat/completions`` call.

    ``thinking`` is ``"on"``, ``"off"`` or ``None`` ("say nothing").  Only
    ``"off"`` puts anything on the wire: hidden reasoning counts against
    ``max_tokens``, so a reviewer turn that must produce visible text asks the
    server to skip it (CORE-reasoning-budget).

    ``effort`` is one of ``low|medium|high|max`` and reaches the wire as
    ``reasoning_effort``.  The caller decides whether the vendor *and* the
    model accept it; this function only spells it (CORE-effort).

    ``vision`` says whether image parts may go out.  ``None`` falls back to the
    model-name guess, which is all this module can know on its own; the
    registry's fuller chain — a configured override, the public catalog, what a
    try-once probe learned — is passed in as a boolean (CORE-vision).
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": messages_to_openai(
            messages,
            vision=(
                vision if vision is not None else content.supports_vision(preset.id, model)
            ),
        ),
        "max_tokens": max_tokens,
        "stream": True,
    }
    if thinking == "off":
        body["chat_template_kwargs"] = dict(THINKING_OFF_TEMPLATE_KWARGS)
    elif effort:
        wire = effort_scale.openai_reasoning_effort(effort)
        if wire:
            body["reasoning_effort"] = wire
    if include_usage:
        body["stream_options"] = {"include_usage": True}
    if tools:
        body["tools"] = tool_specs_to_openai(tools)
        body["tool_choice"] = "auto"
        if preset.supports_parallel_tools:
            body["parallel_tool_calls"] = True
    return body


# ---------------------------------------------------------------------------
# request: Gemini
# ---------------------------------------------------------------------------


def tool_specs_to_gemini(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """``ToolSpec`` list to Gemini's ``tools[0].functionDeclarations``."""
    if not tools:
        return []
    return [
        {
            "functionDeclarations": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]
        }
    ]


def messages_to_gemini(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]]]:
    """Split the system instruction out and convert the rest to ``contents``."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.append(text_of(message.content))
            continue
        if message.role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.name or "",
                                "response": {"content": text_of(message.content) or "(no output)"},
                            }
                        }
                    ],
                }
            )
            continue
        if message.role == "assistant":
            parts: list[dict[str, Any]] = []
            text = text_of(message.content)
            if text:
                parts.append({"text": text})
            for call in message.tool_calls or []:
                parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        if content.has_blocks(message.content):
            blocks = content.parts_from_blocks(message.content)  # type: ignore[arg-type]
            contents.append({"role": "user", "parts": content.to_gemini(blocks)})
            continue
        contents.append({"role": "user", "parts": [{"text": text_of(message.content)}]})
    return "\n\n".join(part for part in system_parts if part), contents


def build_gemini_request(
    messages: list[ChatMessage],
    tools: list[ToolSpec],
    *,
    max_tokens: int,
    thinking: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """The full JSON body for ``streamGenerateContent?alt=sse``.

    Gemini takes a *budget* rather than a word, so the effort tier becomes
    ``thinkingConfig.thinkingBudget``; ``thinking="off"`` asks for ``0``,
    which is how this API is told not to think at all (CORE-effort).
    """
    system, contents = messages_to_gemini(messages)
    generation: dict[str, Any] = {"maxOutputTokens": max_tokens}
    thinking_config = effort_scale.gemini_thinking_config(
        effort, max_tokens, thinking=thinking
    )
    if thinking_config is not None:
        generation["thinkingConfig"] = thinking_config
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    gemini_tools = tool_specs_to_gemini(tools)
    if gemini_tools:
        body["tools"] = gemini_tools
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
    return body


# ---------------------------------------------------------------------------
# response normalisers
# ---------------------------------------------------------------------------


#: Where a server puts hidden reasoning on a streaming delta (or, for a
#: non-streaming body, on ``message``).  ``reasoning`` is what vLLM and
#: OpenRouter send; ``reasoning_content`` is DeepSeek's and SGLang's spelling.
REASONING_KEYS: tuple[str, ...] = ("reasoning", "reasoning_content")


def _reasoning_of(delta: dict[str, Any]) -> str:
    """Hidden reasoning text on one delta, whichever key the vendor used."""
    for key in REASONING_KEYS:
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            joined = "".join(
                str(part.get("text", "")) for part in value if isinstance(part, dict)
            )
            if joined:
                return joined
    return ""


class _Normalizer:
    """Common buffering: tool calls and usage are flushed before ``done``."""

    def __init__(self, preset: VendorPreset) -> None:
        self.preset = preset
        self.usage = Usage()
        self._finish: str | None = None
        self._emitted_tool_calls = 0

    def _limit(self, calls: list[ToolCall]) -> list[ToolCall]:
        if self.preset.supports_parallel_tools or len(calls) <= 1:
            return calls
        return calls[:1]

    def finish(self) -> Iterator[StreamEvent]:
        """Flush whatever is pending; always ``usage`` then ``done``."""
        yield from self._flush_tool_calls()
        yield StreamEvent(kind="usage", usage=self.usage)
        yield StreamEvent(
            kind="done",
            stop_reason=stop_reason(self._finish, had_tool_calls=self._emitted_tool_calls > 0),
        )

    def _flush_tool_calls(self) -> Iterator[StreamEvent]:  # pragma: no cover - overridden
        return iter(())


class OpenAIStreamNormalizer(_Normalizer):
    """``/chat/completions`` streaming chunks to :class:`StreamEvent`.

    Handles both index-based tool-call deltas (``delta.tool_calls[i]`` with the
    arguments arriving piecemeal) and vendors that send a whole tool call in one
    chunk, or only in a non-streaming ``message`` body.
    """

    def __init__(self, preset: VendorPreset) -> None:
        super().__init__(preset)
        self._calls: dict[int, dict[str, Any]] = {}

    def feed(self, chunk: dict[str, Any]) -> Iterator[StreamEvent]:
        """Consume one decoded ``data:`` payload."""
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._absorb_usage(usage)
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                # Non-streaming fallback: the whole message in one chunk.
                message = choice.get("message")
                delta = message if isinstance(message, dict) else {}
            reasoning = _reasoning_of(delta)
            if reasoning:
                yield StreamEvent(kind="reasoning_delta", text=reasoning)
            text = delta.get("content")
            if isinstance(text, str) and text:
                yield StreamEvent(kind="text_delta", text=text)
            elif isinstance(text, list):
                # Some vendors mirror Anthropic's block list here.
                joined = "".join(
                    str(part.get("text", "")) for part in text if isinstance(part, dict)
                )
                if joined:
                    yield StreamEvent(kind="text_delta", text=joined)
            self._absorb_tool_calls(delta.get("tool_calls"))
            reason = choice.get("finish_reason") or choice.get("finishReason")
            if reason:
                self._finish = str(reason)
                yield from self._flush_tool_calls()

    def _absorb_usage(self, usage: dict[str, Any]) -> None:
        details = usage.get("completion_tokens_details")
        reasoning = 0
        if isinstance(details, dict):
            reasoning = int(details.get("reasoning_tokens") or 0)
        self.usage = Usage(
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            reasoning_tokens=reasoning,
        )

    def _absorb_tool_calls(self, raw: Any) -> None:
        if not isinstance(raw, list):
            return
        for position, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            key = int(index) if isinstance(index, int) else len(self._calls) + position
            entry = self._calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
            if item.get("id"):
                entry["id"] = str(item["id"])
            function = item.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    entry["name"] = str(function["name"])
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    entry["arguments"] = str(entry["arguments"]) + arguments
                elif isinstance(arguments, dict):
                    entry["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif item.get("name"):
                entry["name"] = str(item["name"])

    def _flush_tool_calls(self) -> Iterator[StreamEvent]:
        if not self._calls:
            return
        calls = [
            ToolCall(
                id=str(entry["id"]) or f"call_{index}",
                name=str(entry["name"]),
                arguments=parse_arguments(entry["arguments"]),
            )
            for index, entry in sorted(self._calls.items())
            if entry.get("name")
        ]
        self._calls.clear()
        for call in self._limit(calls):
            self._emitted_tool_calls += 1
            yield StreamEvent(kind="tool_call", tool_call=call)


class GeminiStreamNormalizer(_Normalizer):
    """``streamGenerateContent?alt=sse`` chunks to :class:`StreamEvent`."""

    def __init__(self, preset: VendorPreset) -> None:
        super().__init__(preset)
        self._pending: list[ToolCall] = []

    def feed(self, chunk: dict[str, Any]) -> Iterator[StreamEvent]:
        """Consume one decoded ``data:`` payload."""
        usage = chunk.get("usageMetadata")
        if isinstance(usage, dict):
            self.usage = Usage(
                input_tokens=int(usage.get("promptTokenCount") or 0),
                output_tokens=int(usage.get("candidatesTokenCount") or 0),
            )
        for candidate in chunk.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts or []:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    yield StreamEvent(kind="text_delta", text=text)
                call = part.get("functionCall")
                if isinstance(call, dict) and call.get("name"):
                    self._pending.append(
                        ToolCall(
                            id=str(call.get("id") or f"call_{len(self._pending) + 1}"),
                            name=str(call["name"]),
                            arguments=parse_arguments(call.get("args")),
                        )
                    )
            reason = candidate.get("finishReason")
            if reason:
                self._finish = str(reason)
                yield from self._flush_tool_calls()

    def _flush_tool_calls(self) -> Iterator[StreamEvent]:
        calls, self._pending = self._pending, []
        for call in self._limit(calls):
            self._emitted_tool_calls += 1
            yield StreamEvent(kind="tool_call", tool_call=call)


__all__ = [
    "REASONING_KEYS",
    "STOP_REASONS",
    "THINKING_OFF_TEMPLATE_KWARGS",
    "GeminiStreamNormalizer",
    "OpenAIStreamNormalizer",
    "build_gemini_request",
    "build_openai_request",
    "messages_to_gemini",
    "messages_to_openai",
    "parse_arguments",
    "stop_reason",
    "text_of",
    "tool_specs_to_gemini",
    "tool_specs_to_openai",
]
