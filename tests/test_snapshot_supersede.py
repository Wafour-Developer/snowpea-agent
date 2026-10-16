"""Snapshot-aware pruning (snowpea-browser addendum 23).

A host result with ``meta.kind: "snapshot"`` and ``meta.tab`` is a picture of
one tab; only the newest picture of each tab is worth its tokens, so an older
one becomes a one-line stub in the outgoing request from the next model call
on.  The call's arguments stay, the stored transcript is untouched, and a
result without that meta is pruned as before.
"""

from __future__ import annotations

from pathlib import Path

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
    out = compaction.supersede_snapshots(history("A", "A"), pruned)
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
    out = compaction.supersede_snapshots(history("A", "B", "A", None, "B"))
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
    for message in history("A", "A"):
        session.history.append(message)
    sent = agent_mod.build_messages(session, [], core=core)
    tool_texts = [message.content for message in sent if message.role == "tool"]
    assert tool_texts[0] == "[superseded by a newer snapshot of A]"
    assert str(tool_texts[1]).startswith("FULL SNAPSHOT 1")
    stored = [message.content for message in session.history.snapshot() if message.role == "tool"]
    assert str(stored[0]).startswith("FULL SNAPSHOT 0")
