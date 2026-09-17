"""``view_image`` and the image user message the agent loop appends."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from snowpea_core.agent import loop as agent_loop
from snowpea_core.attachments.model import MAX_BYTES
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.providers import content as content_parts
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, ToolSpec
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolContext, register_builtin_tools

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)


@pytest.fixture
def core(tmp_path: Path) -> Core:
    home = tmp_path / "home"
    built = Core(settings=Settings(), paths=Paths.create(home), token="test-token")
    register_builtin_tools(built.tools)
    return built


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


@pytest.fixture
def ctx(core: Core, workdir: Path) -> ToolContext:
    session = Session(id="s-view", workdir=workdir, provider="openai", model="gpt-4o")
    return ToolContext(session=session, core=core, backend=LocalBackend(workdir))


async def run(ctx: ToolContext, name: str, **args: object) -> object:
    tool = ctx.core.tools.get(name)
    assert tool is not None
    return await tool.run(ctx, dict(args))


def write_png(path: Path) -> None:
    path.write_bytes(PNG)


@pytest.mark.asyncio
async def test_view_image_returns_meta(ctx: ToolContext, workdir: Path) -> None:
    target = workdir / "shot.png"
    write_png(target)
    result = await run(ctx, "view_image", path="shot.png")
    assert result.ok
    assert result.meta is not None
    image = result.meta["image"]
    assert image["mime"] == "image/png"
    assert image["path"] == str(target.resolve())
    assert "width" in image and "height" in image
    assert "image attached: shot.png" in result.output


@pytest.mark.asyncio
async def test_read_file_on_png_points_at_view_image(ctx: ToolContext, workdir: Path) -> None:
    write_png(workdir / "diagram.png")
    result = await run(ctx, "read_file", path="diagram.png")
    assert result.ok
    assert "use view_image" in result.output
    assert "read_file cannot show pictures" in result.output


@pytest.mark.asyncio
async def test_oversized_image_is_refused(ctx: ToolContext, workdir: Path) -> None:
    target = workdir / "huge.png"
    target.write_bytes(PNG + b"\x00" * (MAX_BYTES + 1))
    result = await run(ctx, "view_image", path="huge.png")
    assert not result.ok
    assert "limit" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_path_outside_workdir_is_refused(ctx: ToolContext, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    write_png(outside)
    result = await run(ctx, "view_image", path=str(outside))
    assert not result.ok
    assert "outside the session working directory" in (result.error or "")


@pytest.mark.asyncio
async def test_non_vision_model_gets_error_and_no_image_message(
    core: Core, workdir: Path
) -> None:
    write_png(workdir / "shot.png")
    session = Session(
        id="s-blind",
        workdir=workdir,
        provider="deepseek",
        model="deepseek-chat",
    )
    tool_ctx = ToolContext(session=session, core=core, backend=LocalBackend(workdir))
    result = await run(tool_ctx, "view_image", path="shot.png")
    assert not result.ok
    assert "cannot see images" in (result.error or "")
    assert not any(
        isinstance(message.content, list)
        and content_parts.has_blocks(message.content)
        and any(block.get("type") == "image" for block in message.content)
        for message in session.history.snapshot()
    )


@pytest.mark.asyncio
async def test_loop_appends_image_user_message_before_next_provider_request(
    core: Core, workdir: Path
) -> None:
    write_png(workdir / "shot.png")
    session = Session(
        id="s-loop",
        workdir=workdir,
        provider="openai",
        model="gpt-4o",
    )
    provider_requests: list[list[ChatMessage]] = []

    class CapturingProvider:
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
            provider_requests.append(messages)
            if len(provider_requests) == 1:
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="call-view",
                        name="view_image",
                        arguments={"path": "shot.png"},
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
            else:
                yield StreamEvent(kind="text_delta", text="I see it")
                yield StreamEvent(kind="done", stop_reason="end_turn")

    core.providers.get = lambda _provider, _model: CapturingProvider()  # type: ignore[assignment]
    await agent_loop.run_turn(core, session, "look at shot.png", unattended=True)

    assert len(provider_requests) == 2
    saw_image = False
    for message in provider_requests[1]:
        if message.role != "user":
            continue
        blocks = message.content
        if isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "image" for block in blocks
        ):
            saw_image = True
            assert "(image from view_image: " in str(blocks[0].get("text", ""))
    assert saw_image
