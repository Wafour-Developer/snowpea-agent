"""CORE-round-cost §1: deferred tools and ``tool_search``.

Every round re-sends the whole tool list, so forty tool schemas are paid for on
every provider call whether or not the turn could use one.  The scheme here —
claw-code's — sends the core tools in full, names the rest in one grouped line,
and lets ``tool_search`` turn a name into a schema that then stays loaded.

Nothing becomes unreachable: a deferred tool called without a search still
runs, and ``tools.deferred: false`` restores the old list exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.agent import agent as agent_mod
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.prompts import compose
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import deferred
from snowpea_core.tools.registry import ToolContext, register_builtin_tools


@pytest.fixture
def core(tmp_path: Path) -> Core:
    built = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(built.tools.bind(built.settings))
    return built


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    target = tmp_path / "work"
    target.mkdir()
    return target


@pytest.fixture
def session(workdir: Path) -> Session:
    return Session(id="s-deferred", workdir=workdir)


def names(specs: list) -> set[str]:
    return {spec.name for spec in specs}


def ctx_for(
    core: Core, session: Session, workdir: Path, call_id: str = "call-1"
) -> ToolContext:
    """A context that looks like the agent loop's: ``call_id`` means a model call."""
    return ToolContext(
        session=session, core=core, backend=LocalBackend(workdir), call_id=call_id
    )


# ---------------------------------------------------------------------------
# the eager set
# ---------------------------------------------------------------------------


def test_the_eager_set_is_the_core_tools_plus_tool_search(
    core: Core, session: Session
) -> None:
    sent = names(core.tools.specs(session))
    assert sent == set(deferred.EAGER_TOOLS) | {"tool_search"}


def test_everything_else_is_deferred_and_still_listed(core: Core, session: Session) -> None:
    hidden = names(core.tools.deferred_specs(session))
    assert "browser_navigate" in hidden
    assert "web_search" in hidden
    assert "git_commit" in hidden
    assert not hidden & names(core.tools.specs(session))
    # ``tool.list`` keeps describing everything; it only says which are deferred.
    listed = {info.name: info for info in core.tools.list(session)}
    assert listed["browser_navigate"].deferred is True
    assert listed["read_file"].deferred is False


def test_the_settings_switch_restores_the_whole_list(core: Core, session: Session) -> None:
    core.settings.tools.deferred = False
    core.tools.bind(core.settings)
    assert core.tools.deferred_specs(session) == []
    assert names(core.tools.specs(session)) == names(core.tools.all_specs(session))


def test_a_named_tool_can_be_forced_eager(core: Core, session: Session) -> None:
    core.settings.tools.eager = ["web_search"]
    core.tools.bind(core.settings)
    assert "web_search" in names(core.tools.specs(session))
    assert "web_extract" not in names(core.tools.specs(session))


# ---------------------------------------------------------------------------
# the grouped line
# ---------------------------------------------------------------------------


def test_deferred_tools_are_named_by_group_and_never_described(
    core: Core, session: Session
) -> None:
    hidden = core.tools.deferred_specs(session)
    block = compose.tools_block(core.tools.specs(session), hidden)

    assert deferred.DEFERRED_PREFIX in block
    line = next(row for row in block.splitlines() if row.startswith(deferred.DEFERRED_PREFIX))
    assert "browser (5)" in line
    assert "git (4)" in line
    # Names only: no deferred tool's description reaches the prompt.
    for spec in hidden:
        assert spec.description not in block


def test_no_deferred_line_when_nothing_is_deferred() -> None:
    assert deferred.deferred_line([]) == ""


# ---------------------------------------------------------------------------
# tool_search
# ---------------------------------------------------------------------------


async def test_select_returns_exactly_those_tools(
    core: Core, session: Session, workdir: Path
) -> None:
    tool = core.tools.get("tool_search")
    assert tool is not None
    result = await tool.run(
        ctx_for(core, session, workdir), {"query": "select:web_search,git_commit"}
    )
    assert result.ok, result.error
    assert result.meta == {"loaded": ["web_search", "git_commit"]}
    assert "web_search" in result.output
    assert "input schema" in result.output


async def test_a_keyword_query_ranks_and_caps_at_five(
    core: Core, session: Session, workdir: Path
) -> None:
    tool = core.tools.get("tool_search")
    assert tool is not None
    result = await tool.run(ctx_for(core, session, workdir), {"query": "+browser click"})
    assert result.ok, result.error
    loaded = (result.meta or {})["loaded"]
    assert loaded[0] == "browser_click"
    assert len(loaded) <= deferred.MAX_RESULTS
    assert all(name.startswith("browser_") for name in loaded)


async def test_a_loaded_tool_joins_the_next_round_s_list(
    core: Core, session: Session, workdir: Path
) -> None:
    assert "web_search" not in names(core.tools.specs(session))
    tool = core.tools.get("tool_search")
    assert tool is not None
    await tool.run(ctx_for(core, session, workdir), {"query": "select:web_search"})

    assert session.loaded_tools == {"web_search"}
    assert "web_search" in names(core.tools.specs(session))
    assert "web_search" not in names(core.tools.deferred_specs(session))


async def test_a_query_that_matches_nothing_says_so(
    core: Core, session: Session, workdir: Path
) -> None:
    tool = core.tools.get("tool_search")
    assert tool is not None
    result = await tool.run(ctx_for(core, session, workdir), {"query": "zzzqqq"})
    assert result.ok
    assert "No tool matched" in result.output
    assert session.loaded_tools == set()


# ---------------------------------------------------------------------------
# calling a deferred tool without searching first
# ---------------------------------------------------------------------------


async def test_calling_an_unloaded_deferred_tool_works_and_loads_it(
    core: Core, session: Session, workdir: Path
) -> None:
    tool = core.tools.get("git_status")
    assert tool is not None
    result = await tool.run(ctx_for(core, session, workdir), {})
    assert "loaded git_status for this session" in (result.output or result.error or "")
    assert session.loaded_tools == {"git_status"}
    assert "git_status" in names(core.tools.specs(session))

    # Only the first call carries the note; the tool is in the list after that.
    again = await tool.run(ctx_for(core, session, workdir), {})
    assert "loaded git_status" not in (again.output or again.error or "")


async def test_a_call_outside_the_loop_is_left_alone(
    core: Core, session: Session, workdir: Path
) -> None:
    """A test or an internal caller has no model to tell, so nothing is added."""
    tool = core.tools.get("git_status")
    assert tool is not None
    result = await tool.run(ctx_for(core, session, workdir, call_id=""), {})
    assert "loaded git_status" not in (result.output or result.error or "")
    assert session.loaded_tools == set()


async def test_an_eager_tool_never_carries_the_note(
    core: Core, session: Session, workdir: Path
) -> None:
    tool = core.tools.get("read_file")
    assert tool is not None
    (workdir / "a.txt").write_text("hello", encoding="utf-8")
    result = await tool.run(ctx_for(core, session, workdir), {"path": "a.txt"})
    assert result.ok, result.error
    assert "loaded" not in result.output
    assert session.loaded_tools == set()


# ---------------------------------------------------------------------------
# a byte-stable prefix
# ---------------------------------------------------------------------------


def test_two_unchanged_rounds_render_the_identical_tool_fragment(
    core: Core, session: Session
) -> None:
    agent_mod.invalidate_tools()
    specs = core.tools.specs(session)
    hidden = core.tools.deferred_specs(session)
    first = agent_mod.tools_fragment(session, specs, hidden)
    second = agent_mod.tools_fragment(session, core.tools.specs(session), hidden)
    assert first == second
    assert first is second


def test_two_unchanged_rounds_render_the_identical_system_prompt(
    core: Core, session: Session
) -> None:
    agent_mod.invalidate_tools()
    agent_mod.invalidate_environment()
    first = agent_mod.build_system_prompt(session, core.tools.specs(session), core=core)
    second = agent_mod.build_system_prompt(session, core.tools.specs(session), core=core)
    assert first == second


async def test_loading_a_tool_invalidates_the_cached_fragment(
    core: Core, session: Session, workdir: Path
) -> None:
    before = agent_mod.tools_fragment(
        session, core.tools.specs(session), core.tools.deferred_specs(session)
    )
    tool = core.tools.get("tool_search")
    assert tool is not None
    await tool.run(ctx_for(core, session, workdir), {"query": "select:web_search"})
    after = agent_mod.tools_fragment(
        session, core.tools.specs(session), core.tools.deferred_specs(session)
    )
    assert before != after
    assert "web_search" in after


async def test_consecutive_rounds_produce_byte_identical_system_prompts_via_provider(
    core: Core, session: Session, workdir: Path
) -> None:
    from collections.abc import AsyncIterator

    from snowpea_core.agent import loop as agent_loop
    from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, ToolSpec

    captured_prompts: list[str] = []

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
            assert messages and messages[0].role == "system"
            assert isinstance(messages[0].content, str)
            captured_prompts.append(messages[0].content)
            if len(captured_prompts) == 1:
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "sample.txt"},
                    ),
                )
                yield StreamEvent(kind="done", stop_reason="tool_use")
            else:
                yield StreamEvent(kind="text_delta", text="All done")
                yield StreamEvent(kind="done", stop_reason="end_turn")

    (workdir / "sample.txt").write_text("content", encoding="utf-8")
    provider = CapturingProvider()
    core.providers.get = lambda p, m: provider  # type: ignore[assignment]
    outcome = await agent_loop.run_turn(
        core, session, "please read sample.txt", unattended=True
    )
    assert outcome.startswith("t-")
    assert len(captured_prompts) == 2
    assert captured_prompts[0] == captured_prompts[1]
    assert captured_prompts[0].encode("utf-8") == captured_prompts[1].encode("utf-8")
