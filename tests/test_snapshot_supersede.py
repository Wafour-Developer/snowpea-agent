"""Snapshot-aware pruning (snowpea-browser addendum 23).

A host result with ``meta.kind: "snapshot"`` and ``meta.tab`` is a picture of
one tab; only the newest picture of each tab is worth its tokens, so an older
one becomes a one-line stub in the outgoing request from the next model call
on.  The call's arguments stay, the stored transcript is untouched, and a
result without that meta is pruned as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.agent import agent as agent_mod
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.session import compaction
from snowpea_core.session.history import message_from_json, message_to_json
from snowpea_core.session.session import Session
from snowpea_core.tools.host_tools import result_from_answer, snapshot_tab


def call(call_id: str, name: str = "repl", **arguments: object) -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
    )


def result(call_id: str, text: str, tab: str | None = None, name: str = "repl") -> ChatMessage:
    return ChatMessage(
        role="tool", content=text, tool_call_id=call_id, name=name, snapshot_tab=tab
    )


def history(*tabs: str | None) -> list[ChatMessage]:
    out: list[ChatMessage] = [ChatMessage(role="user", content="book a table")]
    for index, tab in enumerate(tabs):
        out.append(call(f"c{index}", code=f"snapshot({index})"))
        out.append(result(f"c{index}", f"FULL SNAPSHOT {index} " + "x" * 400, tab))
    return out


def test_two_snapshots_of_one_tab_stub_the_first() -> None:
    pruned: list[str] = []
    out = compaction.supersede_snapshots(history("A", "A"), pruned, batch=1)
    assert out[2].content == "[superseded by a newer snapshot of A]"
    assert out[4].content.startswith("FULL SNAPSHOT 1")
    # The call that took it keeps its arguments.
    assert out[1].tool_calls is not None and out[1].tool_calls[0].arguments == {
        "code": "snapshot(0)"
    }
    assert pruned == ["c0"]


def test_snapshots_of_two_tabs_are_both_kept() -> None:
    before = history("A", "B")
    assert compaction.supersede_snapshots(before) == before


def test_results_without_snapshot_meta_are_unchanged() -> None:
    before = history(None, None)
    assert compaction.supersede_snapshots(before) == before


def test_the_newest_of_each_tab_survives_in_an_interleaved_run() -> None:
    out = compaction.supersede_snapshots(history("A", "B", "A", None, "B"), batch=1)
    contents = [message.content for message in out if message.role == "tool"]
    assert contents[0] == "[superseded by a newer snapshot of A]"
    assert contents[1] == "[superseded by a newer snapshot of B]"
    assert contents[2].startswith("FULL SNAPSHOT 2")
    assert contents[3].startswith("FULL SNAPSHOT 3")
    assert contents[4].startswith("FULL SNAPSHOT 4")


def test_the_host_meta_decides_which_results_are_snapshots() -> None:
    def tab_of(meta: dict[str, object] | None) -> str | None:
        answer = {"ok": True, "output": "page", **({"meta": meta} if meta else {})}
        return snapshot_tab(result_from_answer("repl", answer).meta)

    assert tab_of({"kind": "snapshot", "tab": "t-7"}) == "t-7"
    assert tab_of({"kind": "snapshot", "tab": 7}) == "7"
    assert tab_of({"kind": "extract", "tab": "t-7"}) is None
    assert tab_of({"kind": "snapshot"}) is None
    assert tab_of({"tab": "t-7"}) is None
    assert tab_of(None) is None


def test_the_marker_survives_a_restart() -> None:
    message = result("c1", "FULL", "A")
    again = message_from_json("tool", message_to_json(message))
    assert again.snapshot_tab == "A"
    plain = message_from_json("tool", message_to_json(result("c2", "FULL")))
    assert plain.snapshot_tab is None and "snapshot_tab" not in message_to_json(plain)


def test_the_outgoing_request_carries_the_stub_and_the_transcript_does_not(
    tmp_path: Path,
) -> None:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    session = Session(id="s-1", workdir=tmp_path)
    for message in history("A", "A", "A", "A", "A"):
        session.history.append(message)
    # Only past the compact threshold (a long turn); otherwise as stored.
    session.context_window = 1_000_000
    plain = agent_mod.build_messages(session, [], core=core)
    assert all(not str(m.content).startswith("[superseded") for m in plain)
    session.context_window = 100
    sent = agent_mod.build_messages(session, [], core=core)
    tool_texts = [message.content for message in sent if message.role == "tool"]
    assert tool_texts[:4] == ["[superseded by a newer snapshot of A]"] * 4
    assert str(tool_texts[4]).startswith("FULL SNAPSHOT 4")
    stored = [message.content for message in session.history.snapshot() if message.role == "tool"]
    assert str(stored[0]).startswith("FULL SNAPSHOT 0")


def test_snapshots_are_stubbed_a_batch_at_a_time() -> None:
    """Stubbing the previous snapshot on every new one rewrote an earlier
    message in nearly every browser round, and the server's prefix cache
    recomputed the rest (~20 s a round on glm-5.3 in s-df4802e0d357)."""
    batch = compaction.SUPERSEDE_BATCH
    previous: list[str] = []
    changes = []
    for count in range(1, 3 * batch + 2):
        out = compaction.supersede_snapshots(history(*["A"] * count))
        contents = [str(m.content) for m in out if m.role == "tool"]
        stubbed = [c.startswith("[superseded") for c in contents]
        assert stubbed == sorted(stubbed, reverse=True), "only the oldest are stubbed"
        assert not stubbed[-1], "the newest snapshot is always kept"
        assert sum(stubbed) == ((count - 1) // batch) * batch
        # A message stubbed once stays stubbed; the others are unchanged.
        if contents[: len(previous)] != previous:
            changes.append(count)
        previous = contents
    assert changes == [batch + 1, 2 * batch + 1, 3 * batch + 1]


async def test_compaction_stubs_older_snapshots_in_the_kept_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    core.settings.context.keepLastMessages = 6

    async def summary(*args: object, **kwargs: object) -> str:
        return "summary"

    monkeypatch.setattr(compaction, "summarise", summary)

    async def nothing(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(compaction, "emit_context", nothing)
    monkeypatch.setattr(core.hub, "emit_event", nothing)
    session = Session(id="s-2", workdir=tmp_path)
    session.history.replace([ChatMessage(role="user", content="earlier " * 50)] * 4)
    for message in history("A", "A", "A"):
        session.history.append(message)
    result = await compaction.compact_session(core, session)
    assert result.compacted
    tools = [str(m.content) for m in session.history.snapshot() if m.role == "tool"]
    assert tools[-1].startswith("FULL SNAPSHOT 2")
    assert all(t.startswith("[superseded") for t in tools[:-1]), tools


def test_a_huge_snapshot_is_capped_once_when_it_arrives(tmp_path: Path) -> None:
    from snowpea_core.agent.loop import _spill_long_result

    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    core.settings.tools.snapshotMaxChars = 5000
    page = "link " * 4000
    snap = result_from_answer(
        "repl", {"ok": True, "output": page, "meta": {"kind": "snapshot", "tab": "A"}}
    )
    capped = _spill_long_result(core, "repl", snap, host=True).output
    assert len(capped) < 5400 and "characters omitted" in capped
    other = result_from_answer(
        "repl", {"ok": True, "output": page, "meta": {"kind": "extract"}}
    )
    assert _spill_long_result(core, "repl", other, host=True).output == page


def test_a_compacted_host_snapshot_passes_uncut_by_default(tmp_path: Path) -> None:
    # The host compacts a page to ~8000 tokens, which is up to ~32k ASCII chars.
    from snowpea_core.agent.loop import _spill_long_result

    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    page = "- link [ref=e1] Product\n" * 1250
    assert 29_000 < len(page) < 31_000
    snap = result_from_answer(
        "repl", {"ok": True, "output": page, "meta": {"kind": "snapshot", "tab": "A"}}
    )
    assert _spill_long_result(core, "repl", snap, host=True).output == page
    huge = "- link [ref=e1] Product\n" * 1800
    assert len(huge) > 40_000
    big = result_from_answer(
        "repl", {"ok": True, "output": huge, "meta": {"kind": "snapshot", "tab": "A"}}
    )
    capped = _spill_long_result(core, "repl", big, host=True).output
    assert len(capped) < 41_000 and "characters omitted" in capped
