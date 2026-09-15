"""Provider-neutral content parts, and the three conversions out of them.

A user turn used to be a string.  With attachments it becomes a list of
*parts* — text, an image, or a file whose text was extracted — and each vendor
wants that list in its own shape:

* **Anthropic** — ``{"type": "image", "source": {"type": "base64", ...}}``
  blocks (and ``document`` blocks for PDFs);
* **OpenAI-compatible** — ``{"type": "image_url", "image_url": {"url":
  "data:image/png;base64,..."}}`` parts, which many self-hosted and
  text-only models reject, so there is a plain-text fallback;
* **Gemini** — ``{"inlineData": {"mimeType": ..., "data": ...}}`` parts.

Everything here is a pure function over dataclasses: no HTTP, no settings, no
disk beyond reading the attachment bytes it is handed.
"""

from __future__ import annotations

import binascii
from base64 import b64decode, b64encode
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.attachments.model import Attachment, is_image, is_text

#: Attachment types Anthropic accepts as a ``document`` block.
DOCUMENT_MIMES = frozenset({"application/pdf"})

#: How a text-only model is told that an image it cannot see was attached.
NO_VISION_NOTE = (
    "(this model cannot see images; ask the user to describe it, "
    "or read the file from disk with a tool)"
)

#: Model-name fragments that identify a vision-capable OpenAI-compatible model.
#: The list is an allowlist on purpose: an unknown model gets the text
#: fallback, which degrades, where sending image parts would hard-fail the turn.
VISION_HINTS: tuple[str, ...] = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gpt-5",
    "o3",
    "o4",
    "chatgpt-4o",
    "claude",
    "gemini",
    "grok-2-vision",
    "grok-3",
    "grok-4",
    "pixtral",
    "llava",
    "llama-3.2-11b",
    "llama-3.2-90b",
    "llama-4",
    "internvl",
    "minicpm-v",
    "moondream",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "glm-4v",
    "glm-4.5v",
    "glm-4.6v",
    "step-1v",
    "abab6.5s",
    "minimax-vl",
    "kimi-latest",
    "moonshot-v1-8k-vision",
    "mistral-small-3",
    "vision",
    "-vl",
)

#: Vendors whose every model in the catalog takes images.
VISION_VENDORS = frozenset({"anthropic", "gemini"})


@dataclass(frozen=True)
class TextPart:
    """A run of plain text."""

    text: str


@dataclass(frozen=True)
class ImagePart:
    """An image, inline as base64 or referenced by url."""

    mime: str
    base64: str | None = None
    url: str | None = None
    name: str = ""


@dataclass(frozen=True)
class FilePart:
    """A non-image file: its extracted text, or just its name and type."""

    name: str
    mime: str
    text: str | None = None
    base64: str | None = None


ContentPart = TextPart | ImagePart | FilePart


# ---------------------------------------------------------------------------
# building parts
# ---------------------------------------------------------------------------


def parts_from_attachments(
    text: str,
    attachments: Sequence[Attachment] = (),
    *,
    text_limit: int = 20_000,
) -> list[ContentPart]:
    """The parts for one user turn: the typed text, then each attachment.

    Text-ish attachments are read and inlined as :class:`FilePart` text so
    every model — vision or not — can use them.  Images become
    :class:`ImagePart`; anything else keeps its name and type only.
    """
    parts: list[ContentPart] = []
    if text:
        parts.append(TextPart(text=text))
    for item in attachments:
        if is_image(item.mime):
            parts.append(ImagePart(mime=item.mime, base64=item.to_base64(), name=item.name))
        elif is_text(item.mime):
            parts.append(FilePart(name=item.name, mime=item.mime, text=item.to_text(text_limit)))
        elif item.mime in DOCUMENT_MIMES:
            parts.append(FilePart(name=item.name, mime=item.mime, base64=item.to_base64()))
        else:
            parts.append(FilePart(name=item.name, mime=item.mime))
    return parts


# ---------------------------------------------------------------------------
# history blocks
# ---------------------------------------------------------------------------

#: ``ChatMessage.content`` block types this module writes and reads back.
BLOCK_TYPES = frozenset({"text", "image", "file"})


def history_blocks(text: str, attachments: Sequence[Attachment] = ()) -> list[dict[str, Any]]:
    """The ``ChatMessage.content`` for a user turn that carried attachments.

    Deliberately *not* the base64: a block keeps the attachment's path, name,
    type and hash, and the bytes are re-read when a request is built.  That is
    what keeps the session store small, lets a resumed session still send the
    image, and makes the stored turn readable — each block also carries a
    ``text`` marker (``[image: shot.png]``) so history rendering and the token
    estimate see something sensible without decoding anything.
    """
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for item in attachments:
        block: dict[str, Any] = {
            "type": "image" if item.is_image else "file",
            "text": item.describe(),
            "name": item.name,
            "mime": item.mime,
            "size": item.size,
            "sha256": item.sha256,
        }
        if item.path is not None:
            block["path"] = str(item.path)
        blocks.append(block)
    return blocks


def has_blocks(content: Any) -> bool:
    """True when a message's content is the block list this module writes."""
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(block, dict) for block in content)
        and any(block.get("type") in BLOCK_TYPES for block in content)
    )


def parts_from_blocks(
    blocks: Sequence[dict[str, Any]], *, text_limit: int = 20_000
) -> list[ContentPart]:
    """Rebuild content parts from stored blocks, re-reading the files.

    An attachment whose file has since been deleted degrades to a text marker
    rather than raising: a stale image must not make a resumed session
    unanswerable.
    """
    parts: list[ContentPart] = []
    for block in blocks:
        kind = block.get("type")
        name = str(block.get("name") or "attachment")
        mime = str(block.get("mime") or "")
        marker = str(block.get("text") or f"[attachment: {name}]")
        if kind == "text" or (kind not in BLOCK_TYPES and block.get("text")):
            text = str(block.get("text") or "")
            if text:
                parts.append(TextPart(text=text))
            continue
        raw = _read_block(block)
        if raw is None:
            parts.append(TextPart(text=f"{marker} (no longer on disk)"))
            continue
        if kind == "image" and is_image(mime):
            parts.append(ImagePart(mime=mime, base64=b64encode(raw).decode("ascii"), name=name))
        elif is_text(mime):
            body = raw.decode("utf-8", "replace")
            if len(body) > text_limit:
                body = body[:text_limit] + f"\n… [truncated, {len(body)} chars total]"
            parts.append(FilePart(name=name, mime=mime, text=body))
        elif mime in DOCUMENT_MIMES:
            parts.append(FilePart(name=name, mime=mime, base64=b64encode(raw).decode("ascii")))
        else:
            parts.append(FilePart(name=name, mime=mime))
    return parts


def _read_block(block: dict[str, Any]) -> bytes | None:
    """The bytes a stored block points at, or ``None`` when they are gone."""
    inline = block.get("data")
    if isinstance(inline, str) and inline:
        try:
            return b64decode(inline, validate=False)
        except (binascii.Error, ValueError):
            return None
    path = block.get("path")
    if not path:
        return None
    try:
        return Path(str(path)).read_bytes()
    except OSError:
        return None


def has_media(parts: Iterable[ContentPart]) -> bool:
    """True when at least one part is not plain text."""
    return any(not isinstance(part, TextPart) for part in parts)


def messages_have_images(messages: Iterable[Any]) -> bool:
    """True when any message in a turn carries an image block.

    What the try-once probe keys off: there is no point spending a refused
    request to learn whether a model can see when this turn has nothing for it
    to look at (CORE-vision).
    """
    for message in messages:
        blocks = getattr(message, "content", None)
        if not isinstance(blocks, list) or not has_blocks(blocks):
            continue
        if any(block.get("type") == "image" for block in blocks):
            return True
    return False


def supports_vision(vendor: str, model: str | None) -> bool:
    """Whether ``vendor``/``model`` can be sent image content parts, by name.

    Anthropic and Gemini take images on every catalog model.  For the nine
    OpenAI-compatible vendors the model name decides, against
    :data:`VISION_HINTS`; an unrecognised name (a local GGUF, say) is treated
    as text-only so the turn degrades instead of erroring.

    This is the *name* rung only, and it is the last one that can answer from
    nothing but two strings.  A configured override, the public catalog, and
    the try-once probe for a self-hosted server all sit above it in
    :meth:`~snowpea_core.providers.registry.ProviderRegistry.vision_for`;
    keeping them out of here is what keeps this module free of settings, HTTP
    and state.
    """
    return vision_from_name(vendor, model)


def vision_from_name(vendor: str, model: str | None) -> bool:
    """The name rung, under the name that says what it is."""
    if vendor in VISION_VENDORS:
        return True
    name = (model or "").lower()
    return any(hint in name for hint in VISION_HINTS)


# ---------------------------------------------------------------------------
# text fallback
# ---------------------------------------------------------------------------


def to_text(parts: Sequence[ContentPart]) -> str:
    """Flatten parts to plain text, describing whatever cannot be shown.

    This is what a model without vision receives: the typed prompt, an
    ``[image attached: name]`` marker plus a short note for every image, and
    the inlined body of any text file.
    """
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            if part.text:
                chunks.append(part.text)
        elif isinstance(part, ImagePart):
            chunks.append(f"[image attached: {part.name or 'image'}] {NO_VISION_NOTE}")
        elif part.text is not None:
            chunks.append(f"[file: {part.name}]\n{part.text}")
        else:
            chunks.append(f"[file attached: {part.name} ({part.mime})]")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def to_anthropic(parts: Sequence[ContentPart]) -> list[dict[str, Any]]:
    """Parts to Anthropic Messages content blocks."""
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            if part.text:
                blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            if part.base64:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.mime,
                            "data": part.base64,
                        },
                    }
                )
            elif part.url:
                blocks.append({"type": "image", "source": {"type": "url", "url": part.url}})
        elif part.mime in DOCUMENT_MIMES and part.base64:
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": part.mime,
                        "data": part.base64,
                    },
                }
            )
        elif part.text is not None:
            blocks.append({"type": "text", "text": f"[file: {part.name}]\n{part.text}"})
        else:
            blocks.append({"type": "text", "text": f"[file attached: {part.name} ({part.mime})]"})
    return blocks


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


def data_uri(mime: str, payload: str) -> str:
    """``data:image/png;base64,...`` — how OpenAI takes an inline image."""
    return f"data:{mime};base64,{payload}"


def to_openai(parts: Sequence[ContentPart], *, vision: bool = True) -> str | list[dict[str, Any]]:
    """Parts to a ``/chat/completions`` ``content`` value.

    Returns a plain string when the model has no vision, or when there is
    nothing but text to send — both keep the request byte-identical to what
    the text-only path produced before attachments existed.
    """
    if not vision or not has_media(parts):
        return to_text(parts)
    out: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            if part.text:
                out.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            url = part.url or (data_uri(part.mime, part.base64) if part.base64 else None)
            if url:
                out.append({"type": "image_url", "image_url": {"url": url}})
        elif part.text is not None:
            out.append({"type": "text", "text": f"[file: {part.name}]\n{part.text}"})
        else:
            out.append({"type": "text", "text": f"[file attached: {part.name} ({part.mime})]"})
    return out


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def to_gemini(parts: Sequence[ContentPart]) -> list[dict[str, Any]]:
    """Parts to Gemini ``contents[].parts``."""
    out: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            if part.text:
                out.append({"text": part.text})
        elif isinstance(part, ImagePart):
            if part.base64:
                out.append({"inlineData": {"mimeType": part.mime, "data": part.base64}})
            elif part.url:
                out.append({"fileData": {"mimeType": part.mime, "fileUri": part.url}})
        elif part.mime in DOCUMENT_MIMES and part.base64:
            out.append({"inlineData": {"mimeType": part.mime, "data": part.base64}})
        elif part.text is not None:
            out.append({"text": f"[file: {part.name}]\n{part.text}"})
        else:
            out.append({"text": f"[file attached: {part.name} ({part.mime})]"})
    return out


__all__ = [
    "BLOCK_TYPES",
    "DOCUMENT_MIMES",
    "NO_VISION_NOTE",
    "VISION_HINTS",
    "VISION_VENDORS",
    "ContentPart",
    "FilePart",
    "ImagePart",
    "TextPart",
    "data_uri",
    "has_blocks",
    "has_media",
    "messages_have_images",
    "history_blocks",
    "parts_from_attachments",
    "parts_from_blocks",
    "supports_vision",
    "to_anthropic",
    "to_gemini",
    "to_openai",
    "to_text",
    "vision_from_name",
]
