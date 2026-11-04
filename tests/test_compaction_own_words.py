"""Compaction reads the user's own words, not the turn context stored after them."""

from __future__ import annotations

from snowpea_core.agent.loop import _append_turn_context
from snowpea_core.memory.retrieval import HEADER
from snowpea_core.providers.base import ChatMessage
from snowpea_core.session.compaction import render_conversation, verbatim_section
from snowpea_core.session.history import message_from_json, message_text, message_to_json

RECALL = f'{HEADER} [global]\n<memory id="m1" tags="">[global] the owner likes tabs</memory>'


def _turn(words: str) -> ChatMessage:
    message = ChatMessage(role="user", content=words)
    _append_turn_context(message, RECALL)
    return message


def test_the_summariser_does_not_read_the_recall_block() -> None:
    rendered = render_conversation([_turn("fix the login bug")])
    assert rendered == "USER: fix the login bug"


def test_the_verbatim_block_quotes_only_what_the_user_said() -> None:
    block = verbatim_section([_turn("fix the login bug")])
    assert "fix the login bug" in block
    assert "the owner likes tabs" not in block


def test_a_message_with_image_blocks_is_cut_at_the_same_place() -> None:
    message = ChatMessage(
        role="user", content=[{"type": "text", "text": "look at this"}, {"type": "image"}]
    )
    own = len(message_text(message))
    _append_turn_context(message, RECALL)
    assert message.own_chars == own
    assert "remember" not in render_conversation([message])


def test_the_boundary_survives_a_save_and_load() -> None:
    saved = message_to_json(_turn("fix the login bug"))
    loaded = message_from_json("user", saved)
    assert loaded.own_chars == len("fix the login bug")
    assert render_conversation([loaded]) == "USER: fix the login bug"


def test_a_message_stored_before_the_boundary_existed_is_cut_at_the_recall_header() -> None:
    old = ChatMessage(role="user", content=f"fix the login bug\n\n{RECALL}")
    assert old.own_chars is None
    assert render_conversation([old]) == "USER: fix the login bug"


def test_a_turn_without_context_is_left_alone() -> None:
    message = ChatMessage(role="user", content="hello")
    _append_turn_context(message, "")
    assert message.own_chars is None
    assert render_conversation([message]) == "USER: hello"
