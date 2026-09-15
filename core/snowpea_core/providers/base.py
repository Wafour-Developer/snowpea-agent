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
    #: Where the tool came from — ``builtin``, ``mcp:<server>``, ``lsp``.  No
    #: provider sends it; the prompt composer groups the tool list by it
    #: (M15 §E), which is why it lives on the spec rather than being re-derived
    #: from the tool name by every caller.
    source: str = "builtin"
    #: The tool's permission tag, shown once per MCP server heading.
    permission: str = ""
    #: True when this round names the tool but does not send its schema
    #: (CORE-round-cost).  Stamped by ``tools.deferred.split``; no provider
    #: sends it, and it is False whenever the scheme is switched off.
    deferred: bool = False
    #: The registry category (``file``, ``browser``, ``lsp``…).  Only the
    #: deferred-tool grouping reads it; no provider sends it.
    category: str = ""


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
    #: Hidden reasoning tokens, when the vendor reports them
    #: (``completion_tokens_details.reasoning_tokens``).  They are part of
    #: :attr:`output_tokens` and therefore of the output budget, which is why
    #: a reasoning model can spend a whole turn and answer nothing.
    reasoning_tokens: int = 0


@dataclass
class StreamEvent:
    """One incremental event from a provider stream.

    kind:
      text_delta     -> ``text``
      reasoning_delta-> ``text`` (hidden thinking; never joins the transcript)
      tool_call      -> ``tool_call``
      usage          -> ``usage``
      done           -> ``stop_reason`` ("end_turn" | "tool_use" | "max_tokens" | "error")
    """

    kind: Literal["text_delta", "reasoning_delta", "tool_call", "usage", "done"]
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

    #: True for an adapter that can be told not to think (``thinking="off"``).
    #: The agent loop reads it before retrying a turn that spent its whole
    #: budget on hidden reasoning.
    supports_thinking_option: bool

    #: True for an adapter that takes a reasoning-effort tier
    #: (``low|medium|high|max``) and maps it to its vendor's own field.  The
    #: loop reads it with ``getattr``, so an adapter written before the option
    #: existed keeps its old signature (CORE-effort).
    supports_effort_option: bool = False

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
        thinking: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


class ProviderError(RuntimeError):
    """Raised for configuration or transport failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
