"""``vision.unavailable`` (protocol 1.41.0): a picture went unseen and undescribed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from _support import Recorder as EventRecorder

from snowpea_core.agent import loop as agent_loop
from snowpea_core.attachments.model import Attachment
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatMessage, ProviderError, StreamEvent, ToolSpec
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import register_builtin_tools
from test_model_categories_133 import PNG

pytestmark = pytest.mark.asyncio


class Answers:
    vendor = "answers"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def stream(
        self, messages: list[ChatMessage], tools: list[ToolSpec], **_: Any
    ) -> AsyncIterator[StreamEvent]:
        if self.fail:
            raise ProviderError("internal", "connection refused")
        yield StreamEvent(kind="text_delta", text="a red pixel")
        yield StreamEvent(kind="done", stop_reason="end_turn")


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools)
    return built


def blind(tmp_path: Path) -> Session:
    return Session(id="s-blind", workdir=tmp_path, provider="deepseek", model="deepseek-chat")


async def _turn(core: Core, session: Session, fail_vision: bool = False) -> list[dict[str, Any]]:
    seen = EventRecorder()
    core.hub.subscribe(seen, session.id)
    core.providers.get = lambda provider=None, model=None: Answers(  # type: ignore[assignment]
        fail=fail_vision and provider == "openai"
    )
    shots = [Attachment.from_bytes(f"s{i}.png", PNG, "image/png") for i in range(2)]
    await agent_loop.run_turn(core, session, "look", unattended=True, attachments=shots)
    return [e["payload"] for e in seen.of_kind("vision.unavailable")]


async def test_no_vision_model_is_reported_once_per_turn(core: Core, tmp_path: Path) -> None:
    [notice] = await _turn(core, blind(tmp_path))
    assert notice["reason"] == "no_vision_model"
    assert notice["source"] == "attachment"
    assert notice["model"] == "deepseek-chat"
    assert notice["visionModel"] is None
    assert notice["count"] == 2


async def test_a_vision_slot_that_fails_says_why(core: Core, tmp_path: Path) -> None:
    core.settings.models.categories["vision"] = "openai:gpt-4o"
    [notice] = await _turn(core, blind(tmp_path), fail_vision=True)
    assert notice["reason"] == "describe_failed"
    assert notice["visionModel"] == "openai:gpt-4o"
    assert "connection refused" in notice["error"]


async def test_a_working_vision_slot_reports_nothing(core: Core, tmp_path: Path) -> None:
    core.settings.models.categories["vision"] = "openai:gpt-4o"
    assert await _turn(core, blind(tmp_path)) == []


async def test_a_model_that_sees_reports_nothing(core: Core, tmp_path: Path) -> None:
    seeing = Session(id="s-see", workdir=tmp_path, provider="openai", model="gpt-4o")
    assert await _turn(core, seeing) == []


async def test_vision_reset_forgets_a_learned_answer(core: Core) -> None:
    core.providers.remember_vision("local", "k-exaone_v2.5", False)
    assert core.providers.vision_for("local", "k-exaone_v2.5") is False
    model, forgotten = core.providers.forget_vision("local", "k-exaone_v2.5")
    assert (model, forgotten) == ("k-exaone_v2.5", True)
    assert core.providers.vision_for("local", "k-exaone_v2.5") is None
    assert core.providers.forget_vision("local", "k-exaone_v2.5")[1] is False


async def test_a_model_found_text_only_by_the_probe_is_reported(core: Core, tmp_path: Path) -> None:
    """The adapter replays the first picture request as text and learns; the surface hears it."""

    class Learning(Answers):
        async def stream(self, messages: list[ChatMessage], tools: list[ToolSpec], **kw: Any) -> Any:
            core.providers.remember_vision("local", "k-exaone_v2.5", False)
            async for event in super().stream(messages, tools, **kw):
                yield event

    session = Session(id="s-probe", workdir=tmp_path, provider="local", model="k-exaone_v2.5")
    seen = EventRecorder()
    core.hub.subscribe(seen, session.id)
    core.providers.get = lambda provider=None, model=None: Learning()  # type: ignore[assignment]
    shot = Attachment.from_bytes("s.png", PNG, "image/png")
    await agent_loop.run_turn(core, session, "look", unattended=True, attachments=[shot])
    [event] = seen.of_kind("vision.unavailable")
    assert event["payload"]["reason"] == "no_vision_model"
