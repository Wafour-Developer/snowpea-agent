"""Provider-neutral content parts and the three adapter conversions."""

from __future__ import annotations

import base64

import pytest

from snowpea_core.attachments.model import Attachment
from snowpea_core.providers.content import (
    NO_VISION_NOTE,
    FilePart,
    ImagePart,
    TextPart,
    data_uri,
    has_media,
    parts_from_attachments,
    supports_vision,
    to_anthropic,
    to_gemini,
    to_openai,
    to_text,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)
PNG_B64 = base64.b64encode(PNG).decode()
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def image() -> Attachment:
    return Attachment.from_bytes("shot.png", PNG)


def note() -> Attachment:
    return Attachment.from_bytes("note.txt", b"hello from a file")


def pdf() -> Attachment:
    return Attachment.from_bytes("paper.pdf", PDF)


def binary() -> Attachment:
    return Attachment.from_bytes("blob.bin", b"\x00\x01\x02\xff\xfe")


# ---------------------------------------------------------------------------
# building parts
# ---------------------------------------------------------------------------


def test_parts_from_attachments_sorts_by_kind() -> None:
    parts = parts_from_attachments("look at this", [image(), note(), pdf(), binary()])
    assert isinstance(parts[0], TextPart)
    assert parts[0].text == "look at this"
    assert isinstance(parts[1], ImagePart)
    assert parts[1].mime == "image/png"
    assert parts[1].base64 == PNG_B64
    assert isinstance(parts[2], FilePart)
    assert parts[2].text == "hello from a file"
    assert isinstance(parts[3], FilePart)
    assert parts[3].base64 is not None  # the pdf travels as bytes
    assert isinstance(parts[4], FilePart)
    assert parts[4].text is None and parts[4].base64 is None


def test_empty_text_produces_no_text_part() -> None:
    parts = parts_from_attachments("", [image()])
    assert len(parts) == 1
    assert isinstance(parts[0], ImagePart)


def test_has_media() -> None:
    assert not has_media(parts_from_attachments("hi"))
    assert has_media(parts_from_attachments("hi", [image()]))


def test_file_text_is_truncated_at_the_limit() -> None:
    long = Attachment.from_bytes("long.txt", b"a" * 500)
    part = parts_from_attachments("", [long], text_limit=20)[0]
    assert isinstance(part, FilePart)
    assert part.text is not None and "truncated" in part.text


# ---------------------------------------------------------------------------
# vision detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vendor", "model"),
    [
        ("anthropic", "claude-sonnet-4-5"),
        ("gemini", "gemini-2.5-pro"),
        ("openai", "gpt-4o"),
        ("openrouter", "anthropic/claude-sonnet-4.5"),
        ("local", "qwen2.5-vl-7b-instruct"),
        ("glm", "glm-4v-plus"),
    ],
)
def test_supports_vision_true(vendor: str, model: str) -> None:
    assert supports_vision(vendor, model)


@pytest.mark.parametrize(
    ("vendor", "model"),
    [
        ("deepseek", "deepseek-chat"),
        ("local", "llama-3.1-8b-instruct"),
        ("local", None),
        ("kimi", "moonshot-v1-8k"),
    ],
)
def test_supports_vision_false(vendor: str, model: str | None) -> None:
    assert not supports_vision(vendor, model)


# ---------------------------------------------------------------------------
# text fallback
# ---------------------------------------------------------------------------


def test_to_text_describes_what_cannot_be_shown() -> None:
    body = to_text(parts_from_attachments("what is this?", [image(), note(), binary()]))
    assert body.startswith("what is this?")
    assert "[image attached: shot.png]" in body
    assert NO_VISION_NOTE in body
    assert "[file: note.txt]\nhello from a file" in body
    assert "[file attached: blob.bin (application/octet-stream)]" in body


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_to_anthropic_emits_image_and_document_blocks() -> None:
    blocks = to_anthropic(parts_from_attachments("hi", [image(), pdf(), note()]))
    assert blocks[0] == {"type": "text", "text": "hi"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
    }
    assert blocks[2]["type"] == "document"
    assert blocks[2]["source"]["media_type"] == "application/pdf"
    assert blocks[3] == {"type": "text", "text": "[file: note.txt]\nhello from a file"}


def test_to_anthropic_takes_a_url_image() -> None:
    blocks = to_anthropic([ImagePart(mime="image/png", url="https://e.g/a.png")])
    assert blocks == [{"type": "image", "source": {"type": "url", "url": "https://e.g/a.png"}}]


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


def test_to_openai_uses_data_uri_image_parts_with_vision() -> None:
    content = to_openai(parts_from_attachments("hi", [image()]), vision=True)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": data_uri("image/png", PNG_B64)},
    }


def test_to_openai_falls_back_to_text_without_vision() -> None:
    content = to_openai(parts_from_attachments("hi", [image()]), vision=False)
    assert isinstance(content, str)
    assert "[image attached: shot.png]" in content
    assert NO_VISION_NOTE in content


def test_to_openai_returns_a_plain_string_when_there_is_no_media() -> None:
    assert to_openai(parts_from_attachments("just text"), vision=True) == "just text"


def test_to_openai_inlines_file_text() -> None:
    content = to_openai(parts_from_attachments("hi", [image(), note()]), vision=True)
    assert isinstance(content, list)
    assert content[2] == {"type": "text", "text": "[file: note.txt]\nhello from a file"}


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_to_gemini_emits_inline_data() -> None:
    parts = to_gemini(parts_from_attachments("hi", [image(), pdf(), binary()]))
    assert parts[0] == {"text": "hi"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
    assert parts[2]["inlineData"]["mimeType"] == "application/pdf"
    assert parts[3] == {"text": "[file attached: blob.bin (application/octet-stream)]"}


def test_to_gemini_takes_a_url_image() -> None:
    parts = to_gemini([ImagePart(mime="image/png", url="https://e.g/a.png")])
    assert parts == [{"fileData": {"mimeType": "image/png", "fileUri": "https://e.g/a.png"}}]


def test_every_converter_ignores_empty_text() -> None:
    parts = [TextPart(text="")]
    assert to_anthropic(parts) == []
    assert to_gemini(parts) == []
    assert to_openai(parts) == ""
