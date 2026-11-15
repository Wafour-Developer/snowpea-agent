"""M19 WP5: external records -> timeline items, and the privacy filter."""

from __future__ import annotations

import json
from typing import Any

from _external_adapter import convert

from snowpea_core.external.timeline import (
    USER_ASSISTANT_ONLY,
    RecordFilter,
    TimelineBuilder,
    clip,
    iso_from_ms,
    iso_from_text,
    iso_from_unix,
    safe_call_id,
    stable_key,
)
from snowpea_core.server.protocol import SESSION_EVENT_MODELS

OWNER = "owner@example.com"

TRANSCRIPT: list[dict[str, Any]] = [
    {"type": "attachment", "attachment": {"userEmail": OWNER, "organizationUuid": "org-1"}},
    {"type": "session-owner", "accountEmail": OWNER, "accountUuid": "acct-1"},
    {"type": "user", "uuid": "u1", "timestamp": "2026-11-15T10:00:00.000Z", "cwd": "/w",
     "message": {"role": "user", "content": "write a note"}},
    {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "content": [
        {"type": "thinking", "thinking": "", "signature": "s"}]}},
    {"type": "assistant", "uuid": "a2", "message": {"id": "m1", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Write",
         "input": {"file_path": "/w/n.txt", "content": "hi"}}]}},
    {"type": "user", "uuid": "u2", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "File created"}]}},
    {"type": "api-request", "body": {"email": OWNER}},
    {"type": "assistant", "uuid": "a3", "message": {"id": "m2", "content": [
        {"type": "text", "text": "Done."}]}},
]


def _kinds(items: list[Any]) -> list[str]:
    return [item.kind for item in items]


def test_privacy_filter_drops_whole_record_kinds() -> None:
    kept = list(USER_ASSISTANT_ONLY.apply(TRANSCRIPT, lambda r: r.get("type")))
    assert [r["type"] for r in kept] == ["user", "assistant", "assistant", "user", "assistant"]
    items = convert(kept, TimelineBuilder("claude", "s1"))
    assert OWNER not in json.dumps([item.payload for item in items])
    deny = RecordFilter(drop=frozenset({"attachment"}))
    assert not deny.keep("attachment") and deny.keep("anything") and not deny.keep(None)
    assert list(deny.apply([{"t": 1}], lambda r: r["missing"])) == []  # malformed: dropped


def test_transcript_conversion_shapes_and_order() -> None:
    kept = list(USER_ASSISTANT_ONLY.apply(TRANSCRIPT, lambda r: r.get("type")))
    items = convert(kept, TimelineBuilder("claude", "s1"))
    assert _kinds(items) == [
        "message.user", "message.reasoning", "tool.call", "tool.result", "message.done"
    ]
    user, _think, call, result, done = items
    assert user.payload["text"] == "write a note"
    assert user.payload["origin"] == {"kind": "human", "surface": "claude"}
    assert user.ts == "2026-11-15T10:00:00.000Z"
    assert call.payload["callId"] == "toolu_1" and call.payload["name"] == "Write"
    assert result.payload["callId"] == "toolu_1" and result.payload["ok"] is True
    assert result.payload["name"] == "Write"
    assert done.payload["text"] == "Done." and done.payload["role"] == "assistant"
    # Every payload is what session.event carries for that kind.
    for item in items:
        model = SESSION_EVENT_MODELS[item.kind]
        model.model_validate({**item.payload, "kind": item.kind})


def test_keys_are_stable_and_dedupe() -> None:
    kept = list(USER_ASSISTANT_ONLY.apply(TRANSCRIPT, lambda r: r.get("type")))
    first = convert(kept, TimelineBuilder("claude", "s1"))
    second = convert(kept, TimelineBuilder("claude", "s1"))
    assert [i.key for i in first] == [i.key for i in second]
    assert len({i.key for i in first}) == len(first)
    # A builder that already delivered a key does not deliver it again.
    builder = TimelineBuilder("claude", "s1")
    builder.mark_seen([first[0].key])
    again = convert(kept, builder)
    assert _kinds(again)[0] == "message.reasoning"


def test_streaming_deltas_flush_into_one_message() -> None:
    builder = TimelineBuilder("codex", "t1")
    builder.turn_started("xt-1", "hi")
    for part in ("Hel", "lo", " world"):
        builder.delta(part, message_key="msg-1", turn_id="xt-1")
    builder.reasoning("thinking", turn_id="xt-1")  # does not close the message
    builder.tool_call("call-1", "shell", {"command": "ls"}, turn_id="xt-1")  # closes it
    builder.tool_result("call-1", ok=False, error="x" * 70_000, turn_id="xt-1")
    builder.diff("a.txt", "@@\n+hi\n", raw_call_id="call-1", turn_id="xt-1")
    builder.delta("Bye", message_key="msg-2", turn_id="xt-1")
    builder.turn_done("xt-1", "complete")
    items = builder.drain()
    assert _kinds(items) == [
        "turn.started", "message.delta", "message.delta", "message.delta", "message.reasoning",
        "message.done", "tool.call", "tool.result", "diff", "message.delta", "message.done",
        "turn.done",
    ]
    assert items[5].payload["text"] == "Hello world"
    assert items[4].payload == {"text": "thinking", "chars": 8}
    assert items[7].payload["error"].endswith("more characters not shown]")
    assert items[8].payload["callId"] == "call-1"
    assert items[10].payload["text"] == "Bye"
    assert all(item.turn_id == "xt-1" for item in items)
    # The whole message arriving later (history read) is not shown twice.
    builder.assistant("Hello world", key="msg-1")
    assert builder.drain() == []


def test_unsafe_call_ids_are_hashed_and_paired() -> None:
    builder = TimelineBuilder("cursor", "c1")
    raw = "call\nabc"
    call_id = builder.tool_call(raw, "Write", {"path": "/w/x"})
    assert "\n" not in call_id and call_id.startswith("c-")
    builder.tool_result(raw, ok=True, output="ok")
    _call, result = builder.drain()
    assert result.payload["callId"] == call_id
    assert safe_call_id("toolu_01AB") == "toolu_01AB"
    assert safe_call_id(None, 1) != safe_call_id(None, 2)
    # A result for a call never announced gets a call first.
    builder.tool_result("orphan", ok=True, name="Read")
    kinds = _kinds(builder.drain())
    assert kinds == ["tool.call", "tool.result"]


def test_error_note_and_event_dict() -> None:
    builder = TimelineBuilder("agy", "a1")
    builder.note("Denied by the person", turn_id="xt-2")
    builder.error("external_unavailable", "agy stopped", turn_id="xt-2")
    note, error = builder.drain()
    assert note.payload["role"] == "system"
    assert error.payload == {"code": "external_unavailable", "message": "agy stopped"}
    event = error.to_event(7)
    assert event == {"seq": 7, "kind": "error", "payload": error.payload, "ts": error.ts,
                     "turnId": "xt-2"}


def test_time_helpers() -> None:
    assert iso_from_ms(0) == "1970-01-01T00:00:00.000Z"
    assert iso_from_unix(1) == "1970-01-01T00:00:01.000Z"
    assert iso_from_text("2026-11-15T10:00:00+09:00") == "2026-11-15T01:00:00.000Z"
    assert iso_from_text("garbage").endswith("Z")
    assert iso_from_ms("nope").endswith("Z")
    assert stable_key("a", 1) == stable_key("a", 1) != stable_key("a", 2)
    # A live builder's generated keys never collide with a history read's.
    history, live = TimelineBuilder("x", "s"), TimelineBuilder("x", "s", salt="live-1")
    history.error("c", "m")
    live.error("c", "m")
    assert history.drain()[0].key != live.drain()[0].key
    assert clip("abc", 10) == "abc"
