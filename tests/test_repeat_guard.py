"""The repeat guard (CORE-repeat-guard).

A turn that reads the same file, runs the same command or grepped the same
pattern over and over pays for every copy.  These tests drive
``tools/repeat_guard.py`` the way ``agent/loop.py`` does — ``check`` before the
call, ``record`` after the one that actually ran — and assert on what the model
would read back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.session.session import Session
from snowpea_core.tools import repeat_guard
from snowpea_core.tools.registry import ToolResult


class _Hub:
    """Records what the guard publishes instead of reaching a real session."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit_event(self, session_id: str, event: tuple[str, dict[str, Any]]) -> None:
        kind, payload = event
        self.events.append((session_id, kind, payload))


class _Tool:
    def __init__(self, source: str) -> None:
        self.source = source


class _Tools:
    def __init__(self, sources: dict[str, str] | None = None) -> None:
        self._sources = sources or {}

    def get(self, name: str) -> _Tool | None:
        source = self._sources.get(name)
        return _Tool(source) if source else None


class _Core:
    def __init__(self, settings: Settings | None = None, **sources: str) -> None:
        self.settings = settings or Settings()
        self.hub = _Hub()
        self.tools = _Tools(sources)


@pytest.fixture
def core() -> _Core:
    return _Core()


@pytest.fixture
def session(tmp_path: Path) -> Session:
    return Session(id="s-guard", workdir=tmp_path)


async def call(
    core: _Core, session: Session, name: str, args: dict[str, Any], output: str = "out"
) -> ToolResult:
    """One dispatched call, guard hooks and all."""
    verdict = repeat_guard.check(core, session, name, args)
    if verdict is not None:
        return verdict.as_result()
    result = ToolResult(ok=True, output=output)
    return await repeat_guard.record(core, session, name, args, result)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_read_stubs_then_blocks(core: _Core, session: Session) -> None:
    target = session.workdir / "a.txt"
    target.write_text("one\ntwo\nthree\n")
    args = {"path": "a.txt"}

    first = await call(core, session, "read_file", args, output="one\ntwo\nthree")
    assert first.output == "one\ntwo\nthree"

    for _ in range(repeat_guard.MAX_READ_STUBS):
        stub = await call(core, session, "read_file", args)
        assert stub.ok
        assert stub.output.startswith("unchanged since your earlier read_file of a.txt")
        assert "3 lines" in stub.output

    blocked = await call(core, session, "read_file", args)
    assert not blocked.ok
    assert blocked.error is not None
    assert blocked.error.startswith(f"{repeat_guard.BLOCKED_CODE}: STOP calling read_file")


@pytest.mark.asyncio
async def test_a_different_window_of_the_same_file_is_not_a_repeat(
    core: _Core, session: Session
) -> None:
    (session.workdir / "a.txt").write_text("one\ntwo\n")
    await call(core, session, "read_file", {"path": "a.txt"}, output="one\ntwo")
    other = await call(core, session, "read_file", {"path": "a.txt", "offset": 2}, output="two")
    assert other.output.startswith("two")


@pytest.mark.asyncio
async def test_a_write_clears_the_read_key(core: _Core, session: Session) -> None:
    target = session.workdir / "a.txt"
    target.write_text("one\n")
    args = {"path": "a.txt"}
    await call(core, session, "read_file", args, output="one")
    assert (await call(core, session, "read_file", args)).output.startswith("unchanged since")

    await call(core, session, "write_file", {"path": "a.txt", "content": "two\n"}, output="ok")
    target.write_text("two\n")

    again = await call(core, session, "read_file", args, output="two")
    assert again.output == "two"


@pytest.mark.asyncio
async def test_an_edit_clears_the_read_key(core: _Core, session: Session) -> None:
    target = session.workdir / "b.txt"
    target.write_text("hello\n")
    args = {"path": "b.txt"}
    await call(core, session, "read_file", args, output="hello")
    assert (await call(core, session, "read_file", args)).output.startswith("unchanged since")

    await call(
        core,
        session,
        "patch",
        {"path": "b.txt", "old_str": "hello", "new_str": "world"},
        output="ok",
    )
    target.write_text("world\n")

    again = await call(core, session, "read_file", args, output="world")
    assert again.output == "world"


@pytest.mark.asyncio
async def test_content_changing_under_the_guard_clears_the_key(
    core: _Core, session: Session
) -> None:
    target = session.workdir / "a.txt"
    target.write_text("one\n")
    args = {"path": "a.txt"}
    await call(core, session, "read_file", args, output="one")
    target.write_text("changed\n")
    again = await call(core, session, "read_file", args, output="changed")
    assert again.output == "changed"


# ---------------------------------------------------------------------------
# consecutive identical calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_identical_shell_warns_then_blocks(
    core: _Core, session: Session
) -> None:
    args = {"command": "uv run pytest -q"}
    outputs = [await call(core, session, "shell", args, output="42 passed") for _ in range(3)]

    assert "42 passed" in outputs[0].output
    assert "same result as your earlier shell call" in outputs[1].output
    assert "this is the 3rd identical shell call in a row" in outputs[2].output

    blocked = await call(core, session, "shell", args)
    assert not blocked.ok
    assert blocked.error is not None
    assert repeat_guard.BLOCKED_CODE in blocked.error
    assert "4th identical call in a row" in blocked.error


@pytest.mark.asyncio
async def test_a_different_call_resets_the_streak(core: _Core, session: Session) -> None:
    """Hermes' ``notify_other_tool_call``: anything else in between starts over."""
    args = {"command": "ls"}
    for _ in range(2):
        await call(core, session, "shell", args, output="a")
        await call(core, session, "web_search", {"query": "x"}, output="hits")
    third = await call(core, session, "shell", args, output="a")
    # Three shell calls, but never three in a row, so no streak warning.
    assert "identical shell call in a row" not in third.output


# ---------------------------------------------------------------------------
# identical results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_repeat_with_an_identical_result_stubs_then_blocks(
    core: _Core, session: Session
) -> None:
    args = {"pattern": "TODO"}
    hits = "a.py:1:TODO\nb.py:2:TODO"
    assert (await call(core, session, "grep", args, output=hits)).output == hits

    for index in (2, 3):
        other = {"pattern": f"other-{index}"}
        await call(core, session, "glob", other, output=f"nothing-{index}")
        stub = await call(core, session, "grep", args, output=hits)
        assert stub.output == "same result as your earlier grep call (2 lines)"

    await call(core, session, "glob", {"pattern": "last"}, output="nothing-last")
    blocked = await call(core, session, "grep", args)
    assert not blocked.ok
    assert blocked.error is not None
    assert "STOP calling grep with these arguments" in blocked.error


@pytest.mark.asyncio
async def test_a_changed_result_is_not_a_repeat(core: _Core, session: Session) -> None:
    args = {"pattern": "TODO"}
    await call(core, session, "grep", args, output="one hit")
    changed = await call(core, session, "grep", args, output="two hits")
    assert changed.output.startswith("two hits")


@pytest.mark.asyncio
async def test_an_mcp_tool_is_compared_by_its_result(session: Session) -> None:
    core = _Core(**{"studio_list": "mcp:snowpea-studio"})
    args = {"kind": "video"}
    await call(core, session, "studio_list", args, output="job-1\njob-2")
    stub = await call(core, session, "studio_list", args, output="job-1\njob-2")
    assert stub.output == "same result as your earlier studio_list call (2 lines)"


# ---------------------------------------------------------------------------
# loop detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_suspected_after_five_non_consecutive_repeats(
    core: _Core, session: Session
) -> None:
    session.current_turn = "t-1"
    args = {"query": "snowpea"}
    results = []
    for index in range(repeat_guard.LOOP_THRESHOLD):
        await call(core, session, "glob", {"pattern": f"p{index}"}, output=f"g{index}")
        results.append(await call(core, session, "web_search", args, output=f"hits-{index}"))

    assert "loop suspected" not in results[-2].output
    assert "loop suspected: web_search" in results[-1].output
    assert "has run 5 times this turn" in results[-1].output

    kinds = [kind for _, kind, _ in core.hub.events]
    assert kinds == ["loop.suspected"]
    assert core.hub.events[0][2] == {"tool": "web_search", "count": 5}


@pytest.mark.asyncio
async def test_the_loop_note_fires_once_per_turn(core: _Core, session: Session) -> None:
    session.current_turn = "t-1"
    args = {"query": "snowpea"}
    for index in range(repeat_guard.LOOP_THRESHOLD + 1):
        await call(core, session, "glob", {"pattern": f"p{index}"}, output=f"g{index}")
        await call(core, session, "web_search", args, output=f"hits-{index}")
    assert len(core.hub.events) == 1


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_setting_turns_the_whole_guard_off(session: Session) -> None:
    settings = Settings()
    settings.tools.repeatGuard = False
    core = _Core(settings)
    target = session.workdir / "a.txt"
    target.write_text("one\n")
    args = {"path": "a.txt"}
    for _ in range(5):
        result = await call(core, session, "read_file", args, output="one")
        assert result.ok
        assert result.output == "one"


def test_a_read_whose_result_was_pruned_can_be_asked_for_again(tmp_path) -> None:
    """Pruning stubs an old read in the request; the guard must then hand the
    file back in full instead of pointing at a result the model cannot see."""
    from types import SimpleNamespace

    from snowpea_core.session import compaction
    from snowpea_core.session.history import ChatMessage, ToolCall
    from snowpea_core.tools import repeat_guard

    target = tmp_path / "npc.ts"
    target.write_text("export const npc = 1;\n" * 20, encoding="utf-8")
    session = SimpleNamespace(id="s-prune", workdir=str(tmp_path))
    guard = repeat_guard.guard_for(session)
    key = repeat_guard._read_key(session, {"path": "npc.ts"})
    assert key is not None
    guard.reads[key] = repeat_guard._ReadEntry(
        hash=repeat_guard._file_digest(str(target)), lines=20
    )

    # Seven rounds after the read, so the read's result falls out of the window.
    history: list[ChatMessage] = []
    history.append(
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c-read", name="read_file", arguments={"path": "npc.ts"})],
        )
    )
    history.append(
        ChatMessage(role="tool", content="x" * 500, tool_call_id="c-read", name="read_file")
    )
    for index in range(7):
        history.append(
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id=f"c{index}", name="shell", arguments={"command": f"echo {index}"})
                ],
            )
        )
        history.append(
            ChatMessage(role="tool", content=str(index), tool_call_id=f"c{index}", name="shell")
        )

    pruned: list[str] = []
    compaction.prune_old_tool_outputs(history, 6, 0, pruned)
    assert "c-read" in pruned
    repeat_guard.forget_pruned(session, history, pruned)
    assert key not in guard.reads
