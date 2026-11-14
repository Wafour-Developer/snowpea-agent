"""Messages the daemon writes say who wrote them and what they are (M16 P1.3)."""

from __future__ import annotations

import pytest

from snowpea_core.providers.base import ChatMessage
from snowpea_core.session import history
from snowpea_core.session.compaction import _verbatim_entries, summary_message


def test_a_source_survives_storage() -> None:
    message = ChatMessage(role="user", content="x", source=history.source("verify-gate", "notice"))
    back = history.message_from_json("user", history.message_to_json(message))
    assert back.source == {"producer": "verify-gate", "form": "notice"}


def test_a_message_stored_before_sources_reads_as_the_users() -> None:
    back = history.message_from_json("user", {"content": "hi"})
    assert back.source is None and not history.is_synthetic(back)


def test_an_unknown_form_is_refused() -> None:
    with pytest.raises(ValueError):
        history.source("x", "chit-chat")


def test_only_the_users_words_and_steers_count_as_theirs() -> None:
    messages = [
        ChatMessage(role="user", content="build the login page"),
        ChatMessage(
            role="user",
            content="Run the tests before you stop.",
            source=history.source("verify-gate", "notice"),
        ),
        ChatMessage(
            role="user",
            content="[skill pdf applies here]",
            source=history.source("skills", "instructions"),
        ),
        ChatMessage(
            role="user", content="also add a logout", source=history.source("user", "steer")
        ),
    ]
    assert _verbatim_entries(messages) == ["build the login page", "also add a logout"]


def test_a_summary_is_marked_and_still_carries_the_earlier_words() -> None:
    from snowpea_core.session.compaction import verbatim_section

    earlier = [ChatMessage(role="user", content="keep the API stable")]
    summary = summary_message("did things\n\n" + verbatim_section(earlier))
    assert summary.source == {"producer": "compaction", "form": "summary"}
    assert _verbatim_entries([summary]) == ["keep the API stable"]
