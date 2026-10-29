"""Protocol 1.33.0: the ``deep`` and ``vision`` slots of ``models.categories``.

``deep`` answers ``llm.sample {modelCategory: "deep"}`` and ``delegate_task
{model: "deep"}`` (deep → standard → the session's model), runs a delegated
planner/architect/critic that has no model of its own, and a root turn in plan
mode — the last two only when ``deep`` is set.  ``vision`` describes the images
a turn carries for a session whose model cannot see them; the session keeps its
own model.  Unset, neither slot changes a single request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from test_host_tools import Daemon, daemon, new_session, open_client, spec  # noqa: F401

from snowpea_core.agent import image_describe
from snowpea_core.agent import loop as agent_loop
from snowpea_core.agent.subagent import get_manager
from snowpea_core.attachments.model import Attachment
from snowpea_core.config.model_routing import (
    ModelRoute,
    deep_if_set,
    deep_route,
    route_for,
)
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.providers import content as content_parts
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, ToolSpec
from snowpea_core.providers.normalize import messages_to_openai
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import CAPABILITIES, MODEL_CATEGORIES, HelloResult
from snowpea_core.session.session import Session
from snowpea_core.tools.delegate import fast_model_reference
from snowpea_core.tools.registry import ToolContext, ToolResult, register_builtin_tools

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)

SESSION = ModelRoute("mainv", "big-1")


def with_categories(**slots: str) -> Settings:
    return Settings.model_validate({"models": {"categories": slots}})


def test_the_capability_and_the_slots() -> None:
    assert "modelCategories" in CAPABILITIES and "jobRuns" in CAPABILITIES
    assert MODEL_CATEGORIES == ["fast", "standard", "deep", "vision"]
    assert HelloResult(protocolVersion="1", serverVersion="1").modelCategories == []


# ---------------------------------------------------------------------------
# deep: the routing helpers
# ---------------------------------------------------------------------------


def test_deep_falls_back_to_standard_then_the_session() -> None:
    assert deep_route(Settings(), SESSION) == SESSION
    standard = with_categories(standard="stdv:mid-1")
    assert deep_route(standard, SESSION) == ModelRoute("stdv", "mid-1")
    both = with_categories(standard="stdv:mid-1", deep="deepv:think-1")
    assert deep_route(both, SESSION) == ModelRoute("deepv", "think-1")


def test_deep_if_set_has_no_fallback() -> None:
    assert not deep_if_set(with_categories(standard="stdv:mid-1")).resolved()
    assert deep_if_set(with_categories(deep="deepv:think-1")) == ModelRoute("deepv", "think-1")


def test_a_role_default_ranks_below_an_assignment_and_above_the_pin(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "models": {"profiles": {"crit": {"provider": "assigned", "model": "c-1"}}},
            "agents": {"models": {"critic": "crit"}},
        }
    )
    deep = ModelRoute("deepv", "think-1")
    pin = ModelRoute("pinned", "p-1")
    assert route_for(settings, agent="planner", session_pin=pin, role_default=deep) == deep
    assert route_for(settings, agent="critic", session_pin=pin, role_default=deep) == ModelRoute(
        "assigned", "c-1"
    )
    assert route_for(
        settings, agent="planner", definition_model="defn:x-1", role_default=deep
    ) == ModelRoute("defn", "x-1")
    assert route_for(settings, agent="planner", session_pin=pin, role_default=ModelRoute()) == pin
    assert route_for(settings, provider="explicit", model="e-1", role_default=deep) == ModelRoute(
        "explicit", "e-1"
    )


def test_delegate_task_deep_names_the_deep_model() -> None:
    session = SimpleNamespace(provider="mainv", model="big-1", workdir=None)
    core = SimpleNamespace(settings=Settings())
    ctx = SimpleNamespace(session=session, core=core)
    assert fast_model_reference(ctx, "deep") == "mainv:big-1"  # type: ignore[arg-type]
    core.settings = with_categories(standard="stdv:mid-1")
    assert fast_model_reference(ctx, "deep") == "stdv:mid-1"  # type: ignore[arg-type]
    core.settings = with_categories(standard="stdv:mid-1", deep="deepv:think-1")
    assert fast_model_reference(ctx, "deep") == "deepv:think-1"  # type: ignore[arg-type]
    # "fast" is untouched by the deep slot.
    assert fast_model_reference(ctx) == "mainv:big-1"  # type: ignore[arg-type]


def test_the_delegate_task_schema_text_is_unchanged() -> None:
    """No session's tool list moves for 1.33.0: 'deep' is accepted, not advertised."""
    from snowpea_core.tools.delegate import MODEL_CATEGORIES as DELEGATE_CATEGORIES

    assert DELEGATE_CATEGORIES == ("standard", "fast")


# ---------------------------------------------------------------------------
# deep: delegated roles and planning root turns
# ---------------------------------------------------------------------------


async def test_a_planner_child_runs_on_deep_only_when_it_is_set(
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,  # noqa: F811
) -> None:
    core = daemon.core
    assert core is not None
    manager = get_manager(core)
    parent = await core.sessions.create(tmp_path, mode="accept", provider="mainv", model="big-1")

    child = await manager._child_session(parent, manager.new_record(parent, "x", "planner"), None)
    assert (child.provider, child.model) == ("mainv", "big-1")

    core.settings.models.categories["deep"] = "deepv:think-1"
    planner = await manager._child_session(parent, manager.new_record(parent, "y", "planner"), None)
    assert (planner.provider, planner.model) == ("deepv", "think-1")
    executor = await manager._child_session(
        parent, manager.new_record(parent, "z", "executor"), None
    )
    assert (executor.provider, executor.model) == ("mainv", "big-1")

    core.settings.agents.models["architect"] = "assigned:a-1"
    architect = await manager._child_session(
        parent, manager.new_record(parent, "w", "architect"), None
    )
    assert (architect.provider, architect.model) == ("assigned", "a-1")


class Recorder:
    """A provider that answers once and remembers which route it was got for."""

    vendor = "recorder"

    def __init__(self, route: tuple[str | None, str | None], text: str = "done") -> None:
        self.route = route
        self.text = text
        self.requests: list[list[ChatMessage]] = []

    async def stream(
        self, messages: list[ChatMessage], tools: list[ToolSpec], **_kwargs: Any
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        yield StreamEvent(kind="text_delta", text=self.text)
        yield StreamEvent(kind="done", stop_reason="end_turn")


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools)
    return built


def routed(core: Core, script: dict[tuple[str | None, str | None], Any] | None = None) -> list[Any]:
    """Swap ``providers.get`` for one that records each route it is asked for."""
    asked: list[Any] = []

    def get(provider: str | None = None, model: str | None = None) -> Any:
        made = (script or {}).get((provider, model)) or Recorder((provider, model))
        asked.append(made)
        return made

    core.providers.get = get  # type: ignore[assignment]
    return asked


async def test_a_planning_root_turn_uses_deep_only_when_it_is_set(
    core: Core, tmp_path: Path
) -> None:
    session = Session(id="s-plan", workdir=tmp_path, provider="mainv", model="big-1", mode="plan")
    asked = routed(core)
    await agent_loop.run_turn(core, session, "plan it", unattended=True)
    assert {made.route for made in asked if made.requests} == {("mainv", "big-1")}

    core.settings.models.categories["deep"] = "deepv:think-1"
    asked.clear()
    await agent_loop.run_turn(core, session, "plan it again", unattended=True)
    assert {made.route for made in asked if made.requests} == {("deepv", "think-1")}

    session.mode = "accept"
    asked.clear()
    await agent_loop.run_turn(core, session, "build it", unattended=True)
    assert {made.route for made in asked if made.requests} == {("mainv", "big-1")}


async def test_a_planning_child_keeps_its_own_route(core: Core, tmp_path: Path) -> None:
    core.settings.models.categories["deep"] = "deepv:think-1"
    session = Session(id="s-kid", workdir=tmp_path, provider="mainv", model="big-1", mode="plan")
    session.is_subagent = True
    asked = routed(core)
    await agent_loop.run_turn(core, session, "look", unattended=True)
    assert {made.route for made in asked if made.requests} == {("mainv", "big-1")}


# ---------------------------------------------------------------------------
# llm.sample {modelCategory: "deep" | "standard"}
# ---------------------------------------------------------------------------


async def test_llm_sample_deep_and_standard(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    from test_llm_sample import stub

    _, asked = stub(monkeypatch, daemon, "x")
    host = await open_client(http, daemon)
    await host.ok("tool.register", {"tools": [spec("repl", "read")]})
    session_id = await new_session(host, tmp_path / "w")
    try:
        session = daemon.core.sessions.get(session_id)
        session.provider, session.model = "mainv", "big-1"
        deep = {"sessionId": session_id, "input": "x", "modelCategory": "deep"}
        standard = {"sessionId": session_id, "input": "x", "modelCategory": "standard"}
        await host.ok("llm.sample", deep)
        assert asked[-1] == ("mainv", "big-1")
        daemon.core.settings.models.categories["standard"] = "stdv:mid-1"
        await host.ok("llm.sample", deep)
        assert asked[-1] == ("stdv", "mid-1")
        await host.ok("llm.sample", standard)
        assert asked[-1] == ("stdv", "mid-1")
        daemon.core.settings.models.categories["deep"] = "deepv:think-1"
        await host.ok("llm.sample", deep)
        assert asked[-1] == ("deepv", "think-1")
    finally:
        await host.stop()


# ---------------------------------------------------------------------------
# vision
# ---------------------------------------------------------------------------


def blind(tmp_path: Path) -> Session:
    # A hosted model whose name implies no vision: images reach it as text.
    return Session(id="s-blind", workdir=tmp_path, provider="deepseek", model="deepseek-chat")


def test_vision_assist_needs_the_slot_and_a_blind_session(core: Core, tmp_path: Path) -> None:
    session = blind(tmp_path)
    assert image_describe.vision_assist(core, session) is None
    core.settings.models.categories["standard"] = "openai:gpt-4o"
    assert image_describe.vision_assist(core, session) is None  # standard alone is not opt-in
    core.settings.models.categories["vision"] = "openai:gpt-4o-mini"
    assert image_describe.vision_assist(core, session) == ModelRoute("openai", "gpt-4o-mini")
    # A vision slot that cannot see falls back to a standard one that can.
    core.settings.models.categories["vision"] = "deepseek:deepseek-chat"
    assert image_describe.vision_assist(core, session) == ModelRoute("openai", "gpt-4o")
    core.settings.models.categories["standard"] = "deepseek:deepseek-chat"
    assert image_describe.vision_assist(core, session) is None
    # A session that sees needs nothing.
    core.settings.models.categories["vision"] = "openai:gpt-4o"
    seeing = Session(id="s-see", workdir=tmp_path, provider="openai", model="gpt-4o")
    assert image_describe.vision_assist(core, seeing) is None


async def test_an_attached_image_is_described_into_the_stored_turn(
    core: Core, tmp_path: Path
) -> None:
    core.settings.models.categories["vision"] = "openai:gpt-4o"
    describer = Recorder(("openai", "gpt-4o"), text="A single red pixel.")
    asked = routed(core, {("openai", "gpt-4o"): describer})
    session = blind(tmp_path)
    shot = Attachment.from_bytes("shot.png", PNG, "image/png")
    await agent_loop.run_turn(core, session, "what is this?", unattended=True, attachments=[shot])

    [described] = describer.requests
    assert any(block.get("type") == "image" for block in described[0].content)
    main = [made for made in asked if made.route == ("deepseek", "deepseek-chat")]
    assert main and main[0].requests
    sent = messages_to_openai(main[0].requests[0], vision=False)
    user = next(entry for entry in sent if entry["role"] == "user")
    assert "described by the vision model" in user["content"]
    assert "A single red pixel." in user["content"]
    assert content_parts.NO_VISION_NOTE not in user["content"]
    # The description is stored with the turn, so later requests send it as is.
    stored = next(m for m in session.history.snapshot() if m.role == "user")
    assert any(block.get("description") == "A single red pixel." for block in stored.content)


async def test_without_the_slot_an_attached_image_is_today_s_note(
    core: Core, tmp_path: Path
) -> None:
    asked = routed(core)
    session = blind(tmp_path)
    shot = Attachment.from_bytes("shot.png", PNG, "image/png")
    await agent_loop.run_turn(core, session, "what is this?", unattended=True, attachments=[shot])
    assert {made.route for made in asked} == {("deepseek", "deepseek-chat")}
    sent = messages_to_openai(asked[0].requests[0], vision=False)
    user = next(entry for entry in sent if entry["role"] == "user")
    assert content_parts.NO_VISION_NOTE in user["content"]
    stored = next(m for m in session.history.snapshot() if m.role == "user")
    assert not any("description" in block for block in stored.content)


async def test_view_image_on_a_blind_model_returns_the_description(
    core: Core, tmp_path: Path
) -> None:
    (tmp_path / "shot.png").write_bytes(PNG)
    core.settings.models.categories["vision"] = "openai:gpt-4o"
    describer = Recorder(("openai", "gpt-4o"), text="A single red pixel.")
    routed(core, {("openai", "gpt-4o"): describer})
    session = blind(tmp_path)
    ctx = ToolContext(session=session, core=core, backend=LocalBackend(tmp_path))
    tool = core.tools.get("view_image")
    assert tool is not None
    result = await tool.run(ctx, {"path": "shot.png"})
    assert result.ok, result.error
    described = await image_describe.describe_tool_images(core, session, result)
    assert "[image shot.png, described by the vision model]" in described.output
    assert "A single red pixel." in described.output


async def test_describe_tool_images_is_a_no_op_without_the_slot(core: Core, tmp_path: Path) -> None:
    routed(core)
    result = ToolResult(ok=True, output="1 image(s) attached", meta={"images": [{"name": "x"}]})
    same = await image_describe.describe_tool_images(core, blind(tmp_path), result)
    assert same.output == "1 image(s) attached"


async def test_a_tool_image_is_described_in_the_loop(core: Core, tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG)
    core.settings.models.categories["vision"] = "openai:gpt-4o"
    describer = Recorder(("openai", "gpt-4o"), text="A single red pixel.")
    calls: list[list[ChatMessage]] = []

    class Main:
        vendor = "main"

        async def stream(
            self, messages: list[ChatMessage], tools: list[ToolSpec], **_kwargs: Any
        ) -> AsyncIterator[StreamEvent]:
            calls.append(list(messages))
            if len(calls) == 1:
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(id="c1", name="view_image", arguments={"path": "shot.png"}),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
            else:
                yield StreamEvent(kind="text_delta", text="ok")
                yield StreamEvent(kind="done", stop_reason="end_turn")

    routed(core, {("openai", "gpt-4o"): describer, ("deepseek", "deepseek-chat"): Main()})
    session = blind(tmp_path)
    await agent_loop.run_turn(core, session, "look at shot.png", unattended=True)
    tool_message = next(m for m in calls[1] if m.role == "tool")
    assert "A single red pixel." in str(tool_message.content)


async def test_settings_set_and_get_carry_the_new_slots(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
) -> None:
    client = await open_client(http, daemon)
    try:
        slots = {"deep": "deepv:think-1", "vision": "openai:gpt-4o"}
        await client.ok("settings.set", {"patch": {"models": {"categories": slots}}})
        got = await client.ok("settings.get", {})
        assert got["settings"]["models"]["categories"] == slots
        assert daemon.core.settings.models.categories == slots
        await client.ok("settings.set", {"patch": {"models": {"categories": {"vision": None}}}})
        assert daemon.core.settings.models.categories == {"deep": "deepv:think-1"}
    finally:
        await client.stop()
