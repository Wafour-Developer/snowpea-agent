"""From a prompt's attachments to the provider request (CORE-multimodal).

The unit tests for the pieces live in ``test_attachments.py`` and
``test_provider_content.py``; this module is about the seam — what the agent
loop stores in history, and what each of the three adapters then sends.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from snowpea_core.attachments.model import Attachment
from snowpea_core.providers import content
from snowpea_core.providers.anthropic_native import messages_to_anthropic
from snowpea_core.providers.base import ChatMessage
from snowpea_core.providers.normalize import (
    build_openai_request,
    messages_to_gemini,
    messages_to_openai,
)
from snowpea_core.providers.presets import PRESETS
from snowpea_core.session.history import History, message_from_json, message_text, message_to_json

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
PNG_B64 = base64.b64encode(PNG).decode()


@pytest.fixture
def stored_image(tmp_path: Path) -> Attachment:
    """An attachment as the store leaves it: on disk, path-backed."""
    target = tmp_path / "shot.png"
    target.write_bytes(PNG)
    return Attachment.from_path(target)


# ---------------------------------------------------------------------------
# history blocks
# ---------------------------------------------------------------------------


def test_blocks_keep_the_path_not_the_bytes(stored_image: Attachment) -> None:
    blocks = content.history_blocks("what is this?", [stored_image])
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    image = blocks[1]
    assert image["type"] == "image"
    assert image["path"] == str(stored_image.path)
    assert image["text"] == "[image: shot.png]"
    # The base64 is nowhere near the session store.
    assert "data" not in image
    assert PNG_B64 not in str(blocks)


def test_blocks_round_trip_through_the_session_store(stored_image: Attachment) -> None:
    message = ChatMessage(role="user", content=content.history_blocks("look", [stored_image]))
    restored = message_from_json("user", message_to_json(message))
    assert restored.content == message.content
    # The token estimate sees the marker, not a JSON dump of the block.
    assert "[image: shot.png]" in message_text(restored)
    assert "sha256" not in message_text(restored)


def test_history_keeps_a_plain_string_when_nothing_is_attached() -> None:
    history = History()
    history.append(ChatMessage(role="user", content="just text"))
    assert history.snapshot()[0].content == "just text"


def test_parts_are_rebuilt_from_the_blocks(stored_image: Attachment) -> None:
    parts = content.parts_from_blocks(content.history_blocks("hi", [stored_image]))
    assert isinstance(parts[0], content.TextPart)
    assert isinstance(parts[1], content.ImagePart)
    assert parts[1].base64 == PNG_B64
    assert parts[1].name == "shot.png"


def test_a_deleted_attachment_degrades_to_a_marker(stored_image: Attachment) -> None:
    """A stale image must not make a resumed session unanswerable."""
    blocks = content.history_blocks("hi", [stored_image])
    assert stored_image.path is not None
    stored_image.path.unlink()
    parts = content.parts_from_blocks(blocks)
    assert isinstance(parts[1], content.TextPart)
    assert "no longer on disk" in parts[1].text


def test_text_files_are_re_read_at_request_time(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("first", encoding="utf-8")
    blocks = content.history_blocks("read it", [Attachment.from_path(source)])
    source.write_text("second", encoding="utf-8")
    part = content.parts_from_blocks(blocks)[1]
    assert isinstance(part, content.FilePart)
    assert part.text == "second"


# ---------------------------------------------------------------------------
# the three adapters
# ---------------------------------------------------------------------------


def user_turn(stored_image: Attachment) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="be brief"),
        ChatMessage(role="user", content=content.history_blocks("what is this?", [stored_image])),
    ]


def test_anthropic_sends_an_image_block(stored_image: Attachment) -> None:
    system, converted = messages_to_anthropic(user_turn(stored_image))
    assert system == "be brief"
    blocks = converted[0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
    }


def test_openai_sends_a_data_uri_to_a_vision_model(stored_image: Attachment) -> None:
    messages = messages_to_openai(user_turn(stored_image), vision=True)
    parts = messages[1]["content"]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_falls_back_to_text_without_vision(stored_image: Attachment) -> None:
    messages = messages_to_openai(user_turn(stored_image), vision=False)
    body = messages[1]["content"]
    assert isinstance(body, str)
    assert "[image attached: shot.png]" in body
    assert "cannot see images" in body


def test_the_request_builder_decides_vision_from_the_model(stored_image: Attachment) -> None:
    """The same turn, two models: one gets the picture, one gets the marker."""
    seeing = build_openai_request(
        PRESETS["openai"], "gpt-4o", user_turn(stored_image), [], max_tokens=64
    )
    blind = build_openai_request(
        PRESETS["deepseek"], "deepseek-chat", user_turn(stored_image), [], max_tokens=64
    )
    assert isinstance(seeing["messages"][1]["content"], list)
    assert isinstance(blind["messages"][1]["content"], str)
    assert "[image attached: shot.png]" in blind["messages"][1]["content"]


def test_gemini_sends_inline_data(stored_image: Attachment) -> None:
    system, contents = messages_to_gemini(user_turn(stored_image))
    assert system == "be brief"
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "what is this?"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}


def test_a_text_only_turn_is_unchanged_everywhere() -> None:
    """No attachments must mean byte-identical requests to before."""
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="hello"),
    ]
    assert messages_to_anthropic(messages) == ("sys", [{"role": "user", "content": "hello"}])
    assert messages_to_openai(messages, vision=True) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    assert messages_to_gemini(messages) == ("sys", [{"role": "user", "parts": [{"text": "hello"}]}])
