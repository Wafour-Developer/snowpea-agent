"""MCP tool results that carry image blocks."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import content as content_parts
from snowpea_core.server.app_server import Core
from snowpea_core.session import events
from snowpea_core.session.session import Session
from snowpea_core.tools import mcp_client
from snowpea_core.tools.registry import ToolResult
from snowpea_core.tools.view_image import append_tool_image_messages

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)


class _FakeBlock:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_render_content_carries_images_in_meta() -> None:
    result = type("R", (), {})()
    result.content = [
        _FakeBlock(text="done"),
        _FakeBlock(data=base64.b64encode(PNG).decode(), mimeType="image/png", name="shot.png"),
    ]
    result.isError = False
    rendered = mcp_client.render_content(result)
    assert rendered.text.startswith("1 image(s) attached")
    assert len(rendered.images) == 1
    assert rendered.images[0]["mime"] == "image/png"
    assert "bytes_b64" in rendered.images[0]
    assert PNG.decode("latin-1") not in rendered.text


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


def test_append_tool_image_message_for_vision(core: Core, workdir: Path) -> None:
    session = Session(id="s-mcp", workdir=workdir, provider="openai", model="gpt-4o")
    result = ToolResult(
        ok=True,
        output="1 image(s) attached",
        meta={
            "images": [
                {
                    "mime": "image/png",
                    "bytes_b64": base64.b64encode(PNG).decode(),
                    "name": "shot.png",
                }
            ]
        },
    )
    append_tool_image_messages(core, session, result, source="mcp__demo__snap")
    assert any(
        content_parts.has_blocks(message.content)
        and any(block.get("type") == "image" for block in message.content)  # type: ignore[union-attr]
        for message in session.history.snapshot()
    )


def test_non_vision_keeps_marker_only(core: Core, workdir: Path) -> None:
    blind = Session(id="s-blind", workdir=workdir, provider="deepseek", model="deepseek-chat")
    result = ToolResult(
        ok=True,
        output="1 image(s) attached",
        meta={
            "images": [
                {
                    "mime": "image/png",
                    "bytes_b64": base64.b64encode(PNG).decode(),
                    "name": "shot.png",
                }
            ]
        },
    )
    append_tool_image_messages(core, blind, result, source="mcp")
    assert not any(
        content_parts.has_blocks(message.content)
        for message in blind.history.snapshot()
    )


def test_tool_result_event_has_no_base64() -> None:
    b64 = base64.b64encode(PNG).decode()
    _, payload = events.tool_result("c1", "mcp__demo__snap", True, "1 image(s) attached", None)
    assert b64 not in str(payload)


@pytest.mark.asyncio
async def test_two_image_tools_in_one_round_keep_tool_messages_contiguous(
    core: Core, workdir: Path
) -> None:
    from collections.abc import AsyncIterator

    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, ToolSpec
    from snowpea_core.tools.registry import register_builtin_tools

    register_builtin_tools(core.tools)
    (workdir / "a.png").write_bytes(PNG)
    (workdir / "b.png").write_bytes(PNG)
    session = Session(id="s-two", workdir=workdir, provider="openai", model="gpt-4o")

    class TwoImageProvider:
        vendor = "capturing"

        async def stream(
            self,
            messages: list[ChatMessage],
            tools: list[ToolSpec],
            *,
            max_tokens: int = 4096,
            thinking: str | None = None,
            effort: str | None = None,
        ) -> AsyncIterator[StreamEvent]:
            if len([m for m in messages if m.role == "assistant" and m.tool_calls]) == 0:
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="c1",
                        name="view_image",
                        arguments={"path": "a.png"},
                    ),
                )
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="c2",
                        name="view_image",
                        arguments={"path": "b.png"},
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
            else:
                yield StreamEvent(kind="text_delta", text="done")
                yield StreamEvent(kind="done", stop_reason="end_turn")

    core.providers.get = lambda _provider, _model: TwoImageProvider()  # type: ignore[assignment]
    await agent_loop.run_turn(core, session, "compare images", unattended=True)

    roles = [message.role for message in session.history.snapshot()]
    assert roles.count("tool") == 2
    tool_indices = [index for index, role in enumerate(roles) if role == "tool"]
    user_after = roles[tool_indices[-1] + 1 :]
    assert user_after and user_after[0] == "user"
    assert "tool" not in user_after[:1]
    image_message = session.history.snapshot()[tool_indices[-1] + 1]
    blocks = image_message.content
    assert isinstance(blocks, list)
    assert sum(1 for block in blocks if block.get("type") == "image") == 2
    assert str(blocks[0].get("text", "")).startswith("(images from")
