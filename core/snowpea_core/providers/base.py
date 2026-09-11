"""Provider abstraction shared by every LLM vendor adapter (contract §5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolSpec:
    """Tool description handed to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: Role
    content: str | list[dict[str, Any]] = ""
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    name: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class StreamEvent:
    """One incremental event from a provider stream.

    kind:
      text_delta -> ``text``
      tool_call  -> ``tool_call``
      usage      -> ``usage``
      done       -> ``stop_reason`` ("end_turn" | "tool_use" | "max_tokens" | "error")
    """

    kind: Literal["text_delta", "tool_call", "usage", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    error: str | None = None


@dataclass
class ProviderInfo:
    vendor: str
    model: str
    auth_methods: list[str] = field(default_factory=lambda: ["api_key"])
    configured: bool = False


@runtime_checkable
class ChatProvider(Protocol):
    vendor: str
    model: str

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]: ...


class ProviderError(RuntimeError):
    """Raised for configuration or transport failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
