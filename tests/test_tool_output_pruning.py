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


def test_a_kept_result_is_never_trimmed_by_default() -> None:
    """What the model is still working from reaches it whole: a read cut short
    made it believe the file was truncated."""
    history = rounds(2, chars=9000)
    pruned = compaction.prune_old_tool_outputs(history, 6, compaction.DEFAULT_TOOL_OUTPUT_MAX_CHARS)
    assert [m.content for m in pruned] == [m.content for m in history]


def test_a_long_kept_result_is_head_tail_trimmed_when_asked() -> None:
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


def test_a_subagent_report_is_never_pruned() -> None:
    """A parent told to 're-run' a delegation redid its children's work."""
    report = ChatMessage(
        role="tool",
        content="status: complete\n\nSeoul population: 9.4M (source: …)",
        tool_call_id="d0",
        name="delegate_task",
    )
    waited = ChatMessage(
        role="tool", content="task bg-1: Busan 3.3M", tool_call_id="w0", name="subagent_wait"
    )
    history = [
        ChatMessage(role="user", content="compare cities"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="d0", name="delegate_task", arguments={"task": "seoul"}),
                ToolCall(id="w0", name="subagent_wait", arguments={"task_ids": ["bg-1"]}),
            ],
        ),
        report,
        waited,
        *rounds(10),
    ]
    pruned = compaction.prune_old_tool_outputs(history, 6, 0)
    assert pruned[2].content == report.content
    assert pruned[3].content == waited.content
    # Ordinary old results are still stubbed.
    assert "pruned" in str(pruned[6].content)


def test_old_notice_bundles_are_pruned_like_tool_output() -> None:
    notice = ChatMessage(
        role="user",
        name=compaction.NOTICE_MESSAGE_NAME,
        content="[system] child evidence\n" + ("x" * 2000),
    )
    history = [notice, *rounds(10)]
    pruned = compaction.prune_old_tool_outputs(history, 6, 2000)

    assert str(pruned[0].content).startswith("[earlier session notices pruned")
    assert history[0].content == notice.content


def test_a_long_report_is_capped_with_a_pointer_to_the_full_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.agent.subagent import SubagentResult
    from snowpea_core.tools import delegate

    monkeypatch.setenv("SNOWPEA_HOME", str(tmp_path))
    long_line = "word " * 8000  # one 40 000-char line: no line cap applies
    result = SubagentResult(agent_id="a-1", ok=True, summary=long_line)
    text = delegate.render_report(result)
    assert len(text) < delegate.REPORT_MAX_CHARS + 2000
    assert "characters omitted" in text and "read_file(" in text
    spilled = next((tmp_path / "cache" / "tool-output").glob("report-*.txt"))
    assert spilled.read_text(encoding="utf-8") == long_line.strip()


def test_a_missing_tool_result_is_stubbed_and_an_orphan_result_dropped() -> None:
    """Meta's API refused 'Missing tool response for tool_call_id'; the request
    is repaired instead of wedging the thread."""
    history = [
        ChatMessage(role="user", content="build it"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="a", name="shell", arguments={"command": "npm run build"}),
                ToolCall(id="b", name="shell", arguments={"command": "relaunch"}),
            ],
        ),
        ChatMessage(role="tool", content="built", tool_call_id="a", name="shell"),
        ChatMessage(role="tool", content="stray", tool_call_id="zzz", name="shell"),
        ChatMessage(role="assistant", content="done"),
    ]
    paired = compaction.pair_tool_results(history)
    assert [(m.role, m.tool_call_id, m.content) for m in paired[2:4]] == [
        ("tool", "a", "built"),
        ("tool", "b", compaction.MISSING_RESULT),
    ]
    assert all(m.tool_call_id != "zzz" for m in paired)
    assert paired[-1].content == "done"
    # A well-formed history passes through unchanged.
    assert compaction.pair_tool_results(rounds(3)) == rounds(3)
