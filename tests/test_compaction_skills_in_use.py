"""R9 (1.37.0): a summary keeps a pointer to every skill the work was following."""

from __future__ import annotations

from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.session.compaction import (
    reinject_skill_pointers,
    render_conversation,
    skills_in_use,
)


def _view(call_id: str, name: str) -> list[ChatMessage]:
    call = ToolCall(id=call_id, name="skill_view", arguments={"name": name})
    return [
        ChatMessage(role="assistant", content="", tool_calls=[call]),
        ChatMessage(role="tool", content="body", tool_call_id=call_id, name="skill_view"),
    ]


def test_the_skill_used_last_comes_last_whatever_its_size() -> None:
    messages = [*_view("1", "tistory"), *_view("2", "godgrade-pipeline"), *_view("3", "tistory")]
    assert skills_in_use(messages) == ["godgrade-pipeline", "tistory"]


def test_a_pointer_is_added_only_for_a_skill_the_summary_does_not_name() -> None:
    summary = "Work state: godgrade-pipeline §6 step 3 is next."
    out = reinject_skill_pointers(summary, ["godgrade-pipeline", "tistory"])
    assert out.startswith(summary)
    assert '- tistory: skill_view(name="tistory")' in out
    assert 'skill_view(name="godgrade-pipeline")' not in out


def test_a_summary_that_names_them_all_is_left_alone() -> None:
    summary = "uses tistory and godgrade-pipeline"
    assert reinject_skill_pointers(summary, ["tistory", "godgrade-pipeline"]) == summary


def test_no_skill_no_change() -> None:
    assert skills_in_use([ChatMessage(role="user", content="hi")]) == []
    assert reinject_skill_pointers("x", []) == "x"
    assert "skill" not in render_conversation([ChatMessage(role="user", content="hi")]).lower()
