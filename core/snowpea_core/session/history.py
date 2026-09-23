"""Conversation history for one session.

Holds :class:`~snowpea_core.providers.base.ChatMessage` objects in order and
mirrors them into the store.  Two kinds of compaction live here:

``compact()``
    the mechanical trim — drop the oldest messages once the list outgrows
    ``max_messages``, never splitting a tool call from its result.
``replace()``
    what the summarising compactor in :mod:`snowpea_core.session.compaction`
    calls when it has swapped the conversation for a summary (CORE-context).

The size estimate (:func:`estimate_tokens`) is deliberately tokenizer-free by
default: ``tiktoken`` is used when it happens to be installed, otherwise one
token per four characters, which is within a few percent for English and
generous for CJK.  Whatever it says is only used until the provider reports
the real ``input_tokens`` for the turn, which then becomes authoritative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from snowpea_core.providers.base import ChatMessage, ToolCall

#: Messages kept before :meth:`History.compact` starts dropping the oldest.
#: A safety net behind the token-based, summarising compaction: 200 was hit
#: by a normal afternoon of work at a quarter of a 262k window, and the
#: opening request went with the oldest messages.
DEFAULT_MAX_MESSAGES = 600

#: Characters per token when no tokenizer is available.
CHARS_PER_TOKEN = 4

#: Rough per-message overhead every wire format adds (role, delimiters).
MESSAGE_OVERHEAD_TOKENS = 4

#: Cached ``tiktoken`` encoder, or ``False`` once we know there is none.
_ENCODER: Any = None


def _encoder() -> Any:
    """The ``tiktoken`` encoder if it is installed, else ``None``.

    Resolved once.  ``tiktoken`` is not a dependency of this project; it is
    honoured when present because a user who has it gets an exact count for
    free.
    """
    global _ENCODER
    if _ENCODER is None:
        try:  # pragma: no cover - depends on the developer's environment
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 - any failure means "no tokenizer"
            _ENCODER = False
    return _ENCODER or None


def estimate_tokens(text: str) -> int:
    """Token count of ``text``: ``tiktoken`` when available, else chars / 4."""
    if not text:
        return 0
    encoder = _encoder()
    if encoder is not None:  # pragma: no cover - environment dependent
        try:
            return len(encoder.encode(text))
        except Exception:  # noqa: BLE001 - a broken encoder falls back
            pass
    return -(-len(text) // CHARS_PER_TOKEN)


def message_text(message: ChatMessage) -> str:
    """Everything in ``message`` that costs tokens, flattened to one string."""
    parts: list[str] = []
    content = message.content
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                parts.append(str(text) if text is not None else json.dumps(block, default=str))
            else:  # pragma: no cover - providers only ever send dicts
                parts.append(str(block))
    if message.name:
        parts.append(message.name)
    for call in message.tool_calls or []:
        parts.append(call.name)
        parts.append(json.dumps(call.arguments, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)


def estimate_message(message: ChatMessage) -> int:
    """Estimated tokens one message costs, including wire overhead."""
    return estimate_tokens(message_text(message)) + MESSAGE_OVERHEAD_TOKENS


def estimate_messages(messages: list[ChatMessage]) -> int:
    """Estimated tokens a whole prompt costs (system prompt included)."""
    return sum(estimate_message(message) for message in messages)


def message_to_json(message: ChatMessage) -> dict[str, Any]:
    """Serialisable form of a chat message (what goes into ``messages``)."""
    return {
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in (message.tool_calls or [])
        ]
        or None,
    }


def message_from_json(role: str, data: dict[str, Any]) -> ChatMessage:
    """Inverse of :func:`message_to_json`."""
    raw_calls = data.get("tool_calls") or []
    calls = [
        ToolCall(id=str(c["id"]), name=str(c["name"]), arguments=dict(c.get("arguments") or {}))
        for c in raw_calls
    ]
    return ChatMessage(
        role=role,  # type: ignore[arg-type]
        content=data.get("content", ""),
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        tool_calls=calls or None,
    )


@dataclass
class History:
    """Ordered message list with an append hook for persistence."""

    messages: list[ChatMessage] = field(default_factory=list)
    max_messages: int = DEFAULT_MAX_MESSAGES
    compactions: int = 0

    def append(self, message: ChatMessage) -> int:
        """Append ``message`` and return its index."""
        self.messages.append(message)
        return len(self.messages) - 1

    def extend(self, messages: list[ChatMessage]) -> None:
        self.messages.extend(messages)

    def snapshot(self) -> list[ChatMessage]:
        """Copy of the current messages, safe to hand to a provider."""
        return list(self.messages)

    def estimate_tokens(self) -> int:
        """Estimated tokens the stored messages cost, without the system prompt."""
        return estimate_messages(self.messages)

    def replace(self, messages: list[ChatMessage]) -> None:
        """Swap the whole conversation, counting it as one compaction.

        Used by the summarising compactor, which builds the replacement list
        (summary plus the last few verbatim messages) and hands it over in one
        step so no turn ever sees a half-replaced history (CORE-context).
        """
        self.messages = list(messages)
        self.compactions += 1

    def compact(self) -> bool:
        """Trim the oldest messages once the list outgrows ``max_messages``.

        The mechanical fallback: drop the oldest, never split a tool call from
        its result.  The summarising path is
        :func:`snowpea_core.session.compaction.compact_session`.
        """
        if len(self.messages) <= self.max_messages:
            return False
        # The first user message is the session's brief — the paths, the
        # constraints, what "done" means — and stays whatever is dropped
        # after it (Hermes protects the same head). The owner's opening line
        # naming an Android project was gone by the afternoon without this.
        head = next((m for m in self.messages if m.role == "user"), None)
        drop = len(self.messages) - self.max_messages
        while drop < len(self.messages) and self.messages[drop].role == "tool":
            drop += 1
        kept = self.messages[drop:]
        if head is not None and head not in kept:
            kept = [head, *kept]
        self.messages = kept
        self.compactions += 1
        return True

    def __len__(self) -> int:
        return len(self.messages)


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_MAX_MESSAGES",
    "MESSAGE_OVERHEAD_TOKENS",
    "History",
    "estimate_message",
    "estimate_messages",
    "estimate_tokens",
    "message_from_json",
    "message_text",
    "message_to_json",
]
