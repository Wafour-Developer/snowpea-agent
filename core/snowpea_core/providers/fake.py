"""Deterministic scripted provider for tests and CI (contract §5).

Selected with ``SNOWPEA_PROVIDER=fake:<path-to-script.json>``.

Script format::

    {
      "steps": [
        {"match": "hello", "text": "Hi from fake"},
        {"match": "run ls",
         "tool_calls": [{"name": "shell", "arguments": {"command": "ls"}}],
         "text": "running"},
        {"after_tool": "shell", "text": "done"},
        {"match": "stall", "delaySec": 5, "text": "too late"}
      ],
      "default": {"text": "fake default reply"}
    }

Matching rules, evaluated per ``stream()`` call, in order:
  * ``after_tool``: the last message is a tool result for that tool name.
  * ``match``: substring of the most recent *user* message text (case-insensitive).
  * Each step is consumed once unless ``"repeat": true``.
  * ``"delaySec": N`` sleeps N seconds before the step emits anything, which is
    how the CLI's ``--timeout`` exit code is driven deterministically.
  * If nothing matches, ``default`` is used (or an empty completion).
The same input always yields the same output.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .base import ChatMessage, ProviderError, StreamEvent, ToolCall, ToolSpec, Usage


def _last_user_text(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            if isinstance(m.content, str):
                return m.content
            return " ".join(
                str(part.get("text", "")) for part in m.content if isinstance(part, dict)
            )
    return ""


def _last_tool_name(messages: list[ChatMessage]) -> str | None:
    if messages and messages[-1].role == "tool":
        return messages[-1].name
    return None


class FakeProvider:
    vendor = "fake"

    def __init__(self, script: dict[str, Any] | None = None, model: str = "fake-1") -> None:
        self.model = model
        self._script = script or {"steps": [], "default": {"text": "fake default reply"}}
        self._used: set[int] = set()

    @classmethod
    def from_env(cls, value: str | None = None) -> FakeProvider:
        value = value if value is not None else os.environ.get("SNOWPEA_PROVIDER", "fake")
        _, _, path = value.partition(":")
        if not path:
            return cls()
        p = Path(path).expanduser()
        if not p.is_file():
            raise ProviderError("invalid_params", f"fake provider script not found: {p}")
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def _pick(self, messages: list[ChatMessage]) -> dict[str, Any]:
        user_text = _last_user_text(messages).lower()
        tool_name = _last_tool_name(messages)
        for idx, step in enumerate(self._script.get("steps", [])):
            if idx in self._used and not step.get("repeat"):
                continue
            after = step.get("after_tool")
            if after is not None:
                if tool_name == after:
                    self._used.add(idx)
                    return step
                continue
            needle = str(step.get("match", "")).lower()
            if needle and needle in user_text and tool_name is None:
                self._used.add(idx)
                return step
        return self._script.get("default", {"text": ""})

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        step = self._pick(messages)
        delay = float(step.get("delaySec") or 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        text = str(step.get("text", ""))
        # Emit text in small deltas so streaming consumers are exercised.
        for i in range(0, len(text), 8):
            yield StreamEvent(kind="text_delta", text=text[i : i + 8])
        calls = step.get("tool_calls") or []
        for call in calls:
            yield StreamEvent(
                kind="tool_call",
                tool_call=ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=str(call["name"]),
                    arguments=dict(call.get("arguments", {})),
                ),
            )
        in_tokens = len(_last_user_text(messages)) // 4
        yield StreamEvent(
            kind="usage", usage=Usage(input_tokens=in_tokens, output_tokens=len(text) // 4)
        )
        yield StreamEvent(kind="done", stop_reason="tool_use" if calls else "end_turn")
