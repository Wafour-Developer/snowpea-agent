"""Conversation history for one session.

Holds :class:`~snowpea_core.providers.base.ChatMessage` objects in order and
mirrors them into the store.  Compaction is a stub at M1: the hook exists and
records that it ran, the summarisation itself lands with the memory work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from snowpea_core.providers.base import ChatMessage, ToolCall

#: Messages kept before :meth:`History.compact` starts dropping the oldest.
DEFAULT_MAX_MESSAGES = 200


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

    def compact(self) -> bool:
        """Trim the oldest messages once the list outgrows ``max_messages``.

        M1 keeps it mechanical (drop the oldest, never split a tool call from
        its result).  A summarising compactor replaces the body later.
        """
        if len(self.messages) <= self.max_messages:
            return False
        drop = len(self.messages) - self.max_messages
        while drop < len(self.messages) and self.messages[drop].role == "tool":
            drop += 1
        self.messages = self.messages[drop:]
        self.compactions += 1
        return True

    def __len__(self) -> int:
        return len(self.messages)


__all__ = [
    "DEFAULT_MAX_MESSAGES",
    "History",
    "message_from_json",
    "message_to_json",
]
