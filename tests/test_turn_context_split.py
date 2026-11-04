"""A user message keeps the user's words apart from the turn's context.

Memory recall, the host note, the plan line and the context notice used to be
glued onto the user's words in ``content``; compaction, the session list and
delegation then read them as the user's own.  They now live in
``turn_context`` and the request joins the two again — byte for byte what the
turn sent — so a server's prefix cache survives an upgrade.
"""

from __future__ import annotations

from snowpea_core.agent.loop import _append_turn_context
from snowpea_core.memory.retrieval import HEADER
from snowpea_core.providers.base import ChatMessage
from snowpea_core.session.compaction import render_conversation, verbatim_section
from snowpea_core.session.history import (
    estimate_message,
    message_from_json,
    message_to_json,
    with_turn_context,
)

RECALL = f'{HEADER} [global]\n<memory id="m1" tags="">[global] the owner likes tabs</memory>'
PLAN = "Current plan: ship it"
EXTRA = f"{RECALL}\n\n{PLAN}"


def _turn(words: str, extra: str = EXTRA) -> ChatMessage:
    message = ChatMessage(role="user", content=words)
    _append_turn_context(message, extra)
    return message


def test_the_user_words_stay_clean_and_the_context_is_kept_apart() -> None:
    message = _turn("fix the login bug")
    assert message.content == "fix the login bug"
    assert message.turn_context == EXTRA


def test_the_request_sends_the_same_bytes_the_old_format_stored() -> None:
    """The old format glued the context on with a blank line."""
    old_bytes = f"fix the login bug\n\n{EXTRA}"
    assert with_turn_context(_turn("fix the login bug")).content == old_bytes


def test_the_request_joins_a_message_with_image_blocks_as_before() -> None:
    blocks = [{"type": "text", "text": "look at this"}, {"type": "image"}]
    message = ChatMessage(role="user", content=list(blocks))
    _append_turn_context(message, EXTRA)
    assert with_turn_context(message).content == [*blocks, {"type": "text", "text": EXTRA}]


def test_a_message_without_context_is_sent_as_it_is() -> None:
    message = ChatMessage(role="user", content="hello")
    _append_turn_context(message, "")
    assert message.turn_context is None
    assert with_turn_context(message) is message


def test_compaction_reads_only_the_user_words() -> None:
    message = _turn("fix the login bug")
    assert render_conversation([message]) == "USER: fix the login bug"
    block = verbatim_section([message])
    assert "fix the login bug" in block and "the owner likes tabs" not in block


def test_the_context_still_counts_toward_the_window() -> None:
    bare = ChatMessage(role="user", content="fix the login bug")
    assert estimate_message(_turn("fix the login bug")) > estimate_message(bare)


def test_the_split_survives_a_save_and_load() -> None:
    saved = message_to_json(_turn("fix the login bug"))
    assert saved["content"] == "fix the login bug" and saved["turn_context"] == EXTRA
    loaded = message_from_json("user", saved)
    assert (loaded.content, loaded.turn_context) == ("fix the login bug", EXTRA)


# --- a session saved by an older version, opened after an update -----------------


def test_an_old_saved_message_is_split_and_sends_the_same_bytes() -> None:
    old = f"fix the login bug\n\n{EXTRA}"
    loaded = message_from_json("user", {"content": old})
    assert loaded.content == "fix the login bug"
    assert with_turn_context(loaded).content == old  # the prefix cache is untouched


def test_an_old_saved_message_with_blocks_is_split_and_sends_the_same_bytes() -> None:
    blocks = [{"type": "text", "text": "look"}, {"type": "text", "text": EXTRA}]
    loaded = message_from_json("user", {"content": [dict(b) for b in blocks]})
    assert loaded.content == [{"type": "text", "text": "look"}]
    assert with_turn_context(loaded).content == blocks


def test_an_old_message_without_a_recall_block_is_left_as_stored() -> None:
    old = f"fix the login bug\n\n{PLAN}"
    loaded = message_from_json("user", {"content": old})
    assert loaded.content == old and loaded.turn_context is None
    assert with_turn_context(loaded).content == old


def test_only_a_user_message_is_split() -> None:
    text = f"done\n\n{RECALL}"
    loaded = message_from_json("assistant", {"content": text})
    assert loaded.content == text and loaded.turn_context is None


def test_an_old_version_reading_a_new_save_just_ignores_the_new_key() -> None:
    saved = message_to_json(_turn("fix the login bug"))
    old_reader_view = {k: v for k, v in saved.items() if k != "turn_context"}
    assert message_from_json("user", old_reader_view).content == "fix the login bug"
