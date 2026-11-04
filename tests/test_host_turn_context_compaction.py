"""R1 (1.37.0): the host is asked again after a compaction, and may cover skills."""

# ruff: noqa: F811  (pytest fixtures imported from test_host_turn_context)
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_host_turn_context import (  # noqa: F401
    NOTE,
    FakeHost,
    Recording,
    _session,
    daemon,
    hosts,
)

from snowpea_core.agent import host_context
from snowpea_core.agent.agent import build_messages
from snowpea_core.agent.loop import _auto_inject_skills
from snowpea_core.providers.base import ChatMessage
from snowpea_core.session.compaction import compact_session, render_conversation
from snowpea_core.session.history import History
from snowpea_core.tools.host_tools import HOST_TOOLS


def _fill(session: Any, turns: int = 12) -> None:
    for n in range(turns):
        session.history.append(ChatMessage(role="user", content=f"step {n}: blog on tistory"))
        session.history.append(ChatMessage(role="assistant", content=f"did {n}"))


def _declare(core: Any, session: Any, host: FakeHost, capabilities: list[str]) -> None:
    HOST_TOOLS.register(core, host, [], capabilities)
    session.host_tools_from = host.surface_id


async def test_a_compaction_asks_the_host_again_and_keeps_the_answer_with_the_summary(
    daemon: Any, tmp_path: Path
) -> None:
    core = daemon.core
    core.providers.register("local", Recording())
    session = await _session(core, tmp_path)
    host = FakeHost({"text": NOTE, "coversSkills": ["tistory"]})
    _declare(core, session, host, ["turnContext", "turnContextCompaction"])
    _fill(session)
    try:
        result = await compact_session(core, session)
        assert result.compacted
        (method, params, _at) = host.calls[-1]
        assert method == "host.turnContext" and params["reason"] == "compaction"
        assert params["prompt"] == "step 11: blog on tistory"
        summary = session.history.messages[0]
        assert summary.role == "system" and summary.turn_context
        assert host_context.OPEN in summary.turn_context and "danawa" in summary.turn_context
        # The request carries the note; the next summariser never reads it.
        wire = build_messages(session, [], core=core)
        assert any("danawa" in str(m.content) for m in wire if m.role == "system")
        assert "danawa" not in render_conversation(session.history.messages)
        assert session.host_covered_skills == frozenset({"tistory"})
    finally:
        HOST_TOOLS.drop_connection(host)


async def test_a_host_without_the_compaction_capability_is_not_asked(
    daemon: Any, tmp_path: Path
) -> None:
    core = daemon.core
    core.providers.register("local", Recording())
    session = await _session(core, tmp_path)
    host = FakeHost()
    _declare(core, session, host, ["turnContext"])
    _fill(session)
    try:
        assert (await compact_session(core, session)).compacted
        assert host.calls == []
        assert session.history.messages[0].turn_context is None
    finally:
        HOST_TOOLS.drop_connection(host)


async def test_a_failing_host_costs_the_compaction_nothing(daemon: Any, tmp_path: Path) -> None:
    core = daemon.core
    core.providers.register("local", Recording())
    session = await _session(core, tmp_path)
    host = FakeHost(RuntimeError("gone"))
    _declare(core, session, host, ["turnContext", "turnContextCompaction"])
    _fill(session)
    try:
        assert (await compact_session(core, session)).compacted
        assert session.history.messages[0].turn_context is None
    finally:
        HOST_TOOLS.drop_connection(host)


async def test_the_cover_is_read_clean(daemon: Any, tmp_path: Path) -> None:
    core = daemon.core
    session = await _session(core, tmp_path)
    names = ["tistory", "  naver ", 5, "", "x" * 100, *[f"s{i}" for i in range(30)]]
    host = FakeHost({"text": NOTE, "coversSkills": names})
    _declare(core, session, host, ["turnContext"])
    try:
        note = await host_context.fetch_note(core, session, "t", "open danawa")
        assert {"tistory", "naver"} <= note.covers
        assert len(note.covers) == host_context.MAX_COVERED_SKILLS
        assert "x" * 100 not in note.covers and "" not in note.covers
    finally:
        HOST_TOOLS.drop_connection(host)


async def test_a_covered_skill_is_not_injected_and_not_marked_as_injected() -> None:
    skill = SimpleNamespace(
        doc=SimpleNamespace(name="tistory", auto_inject=["tistory"], body="short body")
    )
    core = SimpleNamespace(skills=SimpleNamespace(skills={"tistory": skill}, enabled=None))
    session = SimpleNamespace(auto_injected_skills=set(), history=History())
    covered = frozenset({"tistory"})
    assert await _auto_inject_skills(core, session, "tistory post", skip=covered) == []  # type: ignore[arg-type]
    assert session.auto_injected_skills == set() and len(session.history) == 0
    # A later turn the host does not cover still gets the skill.
    assert await _auto_inject_skills(core, session, "tistory post") == ["tistory"]  # type: ignore[arg-type]
    assert len(session.history) == 1


def test_the_lead_line_says_where_the_note_ranks() -> None:
    assert "project skill outranks this note" in host_context.LEAD
    assert "not save_memory" in host_context.LEAD


def test_a_block_stored_with_the_old_lead_line_is_still_recognised() -> None:
    old = (
        f"{host_context._LEAD_BEFORE_1_37}\n{host_context.OPEN}\nsite notes\n{host_context.CLOSE}"
    )
    assert host_context.strip(f"open it\n\n{old}").strip() == "open it"
    new = host_context.render("site notes")
    assert host_context.strip(f"open it\n\n{new}").strip() == "open it"
