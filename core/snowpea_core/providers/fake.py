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
        {"match": "stall", "delaySec": 5, "text": "too late"},
        {"match": "review", "reasoning": "thinking hard", "reasoningTokens": 400,
         "stopReason": "length", "text": ""}
      ],
      "default": {"text": "fake default reply"}
    }

Matching rules, evaluated per ``stream()`` call, in order:
  * ``after_tool``: the last message is a tool result for that tool name.
  * ``match``: substring of the most recent *user* message text (case-insensitive).
  * Each step is consumed once unless ``"repeat": true``.
  * ``"delaySec": N`` sleeps N seconds before the step emits anything, which is
    how the CLI's ``--timeout`` exit code is driven deterministically.
  * ``"reasoning"`` is emitted as ``reasoning_delta`` and, with
    ``"reasoningTokens"`` and ``"stopReason": "length"``, reproduces a model
    that spent its whole output budget thinking (CORE-reasoning-budget).
  * ``"thinking": "off"`` on a step makes it match only a call the agent loop
    asked not to think, which is how the retry path is exercised.  Turning
    thinking off suppresses ``reasoning`` and ``reasoningTokens``; it does not
    suppress ``stopReason``, because a long answer can still run out of room.
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
from .normalize import STOP_REASONS


def _last_user_text(messages: list[ChatMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            if isinstance(m.content, str):
                return m.content
            return " ".join(
                str(part.get("text", "")) for part in m.content if isinstance(part, dict)
            )
    return ""


def _prompt_chars(messages: list[ChatMessage]) -> int:
    """Characters in the whole prompt, the way a real vendor would count it.

    The scripted provider used to report only the last user message, which is
    not what any vendor does and made context accounting (CORE-context) look
    flat across a growing conversation.
    """
    total = 0
    for message in messages:
        content = message.content
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", "")))
        for call in message.tool_calls or []:
            total += len(call.name) + len(json.dumps(call.arguments, default=str))
    return total


def _last_tool_name(messages: list[ChatMessage]) -> str | None:
    if messages and messages[-1].role == "tool":
        return messages[-1].name
    return None


class FakeProvider:
    vendor = "fake"

    #: The scripted provider honours ``thinking`` so a test can watch the loop
    #: retry a reasoning-starved turn with it off.
    supports_thinking_option = True
    #: And the effort tier, so a test can watch what the loop resolved.
    supports_effort_option = True

    def __init__(self, script: dict[str, Any] | None = None, model: str = "fake-1") -> None:
        self.model = model
        self._script = script or {"steps": [], "default": {"text": "fake default reply"}}
        self._used: set[int] = set()
        #: ``(max_tokens, thinking)`` of every call, oldest first.  The effort
        #: tier of each call is recorded alongside, in :attr:`efforts`.
        self.calls: list[tuple[int, str | None]] = []
        #: Effort tier of every call, oldest first (``None`` when none applied).
        self.efforts: list[str | None] = []

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

    def _pick(self, messages: list[ChatMessage], thinking: str | None = None) -> dict[str, Any]:
        user_text = _last_user_text(messages).lower()
        tool_name = _last_tool_name(messages)
        for idx, step in enumerate(self._script.get("steps", [])):
            if idx in self._used and not step.get("repeat"):
                continue
            wants = step.get("thinking")
            if wants is not None and str(wants) != (thinking or ""):
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
        thinking: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append((max_tokens, thinking))
        self.efforts.append(effort)
        step = self._pick(messages, thinking)
        delay = float(step.get("delaySec") or 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        reasoning = str(step.get("reasoning", ""))
        if reasoning and thinking != "off":
            for i in range(0, len(reasoning), 8):
                yield StreamEvent(kind="reasoning_delta", text=reasoning[i : i + 8])
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
        in_tokens = _prompt_chars(messages) // 4
        reasoning_tokens = 0 if thinking == "off" else int(step.get("reasoningTokens") or 0)
        yield StreamEvent(
            kind="usage",
            usage=Usage(
                input_tokens=in_tokens,
                output_tokens=len(text) // 4 + reasoning_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
        )
        scripted = step.get("stopReason")
        if scripted:
            yield StreamEvent(kind="done", stop_reason=STOP_REASONS.get(str(scripted), "end_turn"))
            return
        yield StreamEvent(kind="done", stop_reason="tool_use" if calls else "end_turn")
