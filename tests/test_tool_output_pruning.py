"""Early pruning of old tool output (CORE-repeat-guard §3).

The outgoing request keeps the last few tool rounds verbatim, stubs everything
older and head/tail trims whatever is still oversized.  The stored transcript
is never touched: a session that is resumed, exported or compacted still has
every byte the tools produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.agent import agent as agent_mod
from snowpea_core.agent.agent import build_messages
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.session import compaction
from snowpea_core.session.session import Session


def rounds(count: int, *, chars: int = 40) -> list[ChatMessage]:
    """``count`` rounds of one assistant call and its one tool result."""
    out: list[ChatMessage] = []
    for index in range(count):
        call_id = f"c{index}"
        out.append(ChatMessage(role="user", content=f"ask {index}"))
        out.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id=call_id, name="shell", arguments={"command": "ls"})],
            )
        )
        out.append(
            ChatMessage(
                role="tool",
                content=f"{index}" * chars,
                tool_call_id=call_id,
                name="shell",
            )
        )
    return out


def test_the_last_six_rounds_survive_verbatim() -> None:
    history = rounds(10)
    pruned = compaction.prune_old_tool_outputs(history, 6, 2000)
    tools = [message for message in pruned if message.role == "tool"]
    assert len(tools) == 10
    stub = "[earlier shell output pruned — 40 chars; re-run the tool if you need it again]"
    for message in tools[:4]:
        assert message.content == stub
    for original, kept in zip(history[2::3][4:], tools[4:], strict=True):
        assert kept.content == original.content


def test_a_long_kept_result_is_head_tail_trimmed() -> None:
    history = rounds(2, chars=5000)
    pruned = compaction.prune_old_tool_outputs(history, 6, 2000)
    kept = [message for message in pruned if message.role == "tool"]
    assert all(len(message.content) <= 2000 for message in kept)
    assert all(compaction.TRIM_MARKER in message.content for message in kept)
    assert kept[0].content.startswith("0" * 100)
    assert kept[0].content.endswith("0" * 100)


def test_pruning_never_mutates_the_stored_history() -> None:
    history = rounds(10, chars=5000)
    before = [message.content for message in history]
    compaction.prune_old_tool_outputs(history, 6, 2000)
    assert [message.content for message in history] == before


def test_skill_view_bodies_are_left_to_the_skill_pruner() -> None:
    body = "x" * 9000
    history = [
        ChatMessage(role="user", content="load it"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="s1", name="skill_view", arguments={"name": "release"})],
        ),
        ChatMessage(role="tool", content=body, tool_call_id="s1", name="skill_view"),
        *rounds(8),
    ]
    pruned = compaction.prune_old_tool_outputs(history, 6, 2000)
    assert pruned[2].content == body


def test_an_already_pruned_skill_marker_is_not_pruned_twice() -> None:
    marker = compaction.skill_pruned_marker("release")
    history = [
        ChatMessage(role="user", content="load it"),
        ChatMessage(role="tool", content=marker, tool_call_id="s1", name="skill_view"),
        *rounds(8),
    ]
    pruned = compaction.prune_old_tool_outputs(history, 6, 2000)
    assert pruned[1].content == marker


def test_keep_rounds_zero_stubs_everything() -> None:
    pruned = compaction.prune_old_tool_outputs(rounds(3), 0, 2000)
    assert all(
        message.content.startswith("[earlier shell output pruned")
        for message in pruned
        if message.role == "tool"
    )


# ---------------------------------------------------------------------------
# the hook and its switch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cheap_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about the history half of the request, not the prompt."""
    monkeypatch.setattr(agent_mod, "build_system_prompt", lambda *a, **k: "system")


def _core(tmp_path: Path, settings: Settings | None = None) -> Core:
    return Core(
        settings=settings or Settings(),
        paths=Paths.create(tmp_path / "home"),
        token="test-token",
    )


def test_build_messages_prunes_the_outgoing_request(tmp_path: Path) -> None:
    session = Session(id="s-prune", workdir=tmp_path)
    session.history.replace(rounds(10))
    messages = build_messages(session, [], core=_core(tmp_path))
    tools = [message for message in messages if message.role == "tool"]
    assert tools[0].content.startswith("[earlier shell output pruned")
    # The transcript itself is untouched.
    stored = [message for message in session.history.snapshot() if message.role == "tool"]
    assert stored[0].content == "0" * 40


def test_the_setting_turns_pruning_off(tmp_path: Path) -> None:
    settings = Settings()
    settings.agent.pruneToolOutputs = False
    session = Session(id="s-prune-off", workdir=tmp_path)
    session.history.replace(rounds(10))
    messages = build_messages(session, [], core=_core(tmp_path, settings))
    tools = [message for message in messages if message.role == "tool"]
    assert tools[0].content == "0" * 40


def test_keep_tool_rounds_is_read_from_settings(tmp_path: Path) -> None:
    settings = Settings()
    settings.agent.keepToolRounds = 2
    on, keep, max_chars = compaction.tool_prune_settings(_core(tmp_path, settings))
    assert (on, keep, max_chars) == (True, 2, compaction.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
