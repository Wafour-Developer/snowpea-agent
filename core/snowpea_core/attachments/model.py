"""User-supplied attachments: what they are and how they are normalised.

A prompt may arrive with files pasted or dragged into the TUI.  Each one
becomes an :class:`Attachment`, which is either *inline* (bytes the client
sent as base64) or *path-backed* (a file already on the daemon's disk).  The
rules are deliberately boring and enforced in one place:

* the MIME type is sniffed from the magic bytes, never trusted from the client;
* anything over :data:`MAX_BYTES` is rejected;
* images are downscaled to :data:`MAX_IMAGE_EDGE` on the longest side when
  Pillow is importable, and passed through untouched when it is not.

Nothing here talks to a provider — :mod:`snowpea_core.providers.content` turns
attachments into vendor content blocks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.attachments")

#: Hard ceiling for a single attachment, before and after decoding.
MAX_BYTES = 20 * 1024 * 1024

#: Longest edge an image is downscaled to (the Anthropic vision sweet spot).
MAX_IMAGE_EDGE = 1568

#: Fallback when the bytes look like nothing in particular.
OCTET_STREAM = "application/octet-stream"

#: Magic-byte prefixes, longest first so ``image/webp`` beats a bare ``RIFF``.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

#: Extension used when persisting, per MIME type.
EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    OCTET_STREAM: "bin",
}

#: MIME types a vision model can look at.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class AttachmentError(ValueError):
    """Raised for an attachment the core refuses to accept.

    ``code`` is the JSON-RPC error code the server maps onto the wire
    (``invalid_params`` for a malformed payload, ``attachment_too_large`` for
    the size cap).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sniff_mime(data: bytes, name: str | None = None) -> str:
    """The MIME type of ``data``, from its magic bytes.

    The filename is only consulted to tell ``text/markdown`` from
    ``text/plain``; everything else is decided by the content, so a client
    cannot smuggle a binary in by calling it ``notes.txt``.
    """
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if not data:
        return "text/plain"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return OCTET_STREAM
    if name and Path(name).suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    return "text/plain"


def extension_for(mime: str, name: str | None = None) -> str:
    """The file extension to store ``mime`` under."""
    known = EXTENSIONS.get(mime)
    if known:
        return known
    suffix = Path(name or "").suffix.lstrip(".").lower()
    return suffix if suffix.isalnum() and suffix else "bin"


def is_image(mime: str) -> bool:
    """True for the four image types every vision model understands."""
    return mime in IMAGE_MIMES


def is_text(mime: str) -> bool:
    """True for types whose bytes can be handed to a model as text."""
    return mime.startswith("text/") or mime in {"application/json", "application/xml"}


def decode_base64(raw: str) -> bytes:
    """Decode client-sent base64, tolerating a ``data:`` URI wrapper."""
    payload = raw.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("invalid_params", f"attachment: bad base64: {exc}") from exc


@dataclass(frozen=True)
class Attachment:
    """One file attached to a prompt.

    Exactly one of :attr:`data` and :attr:`path` carries the bytes: ``data``
    for something the client sent inline, ``path`` for a file on disk (either
    dragged in by path, or persisted by
    :class:`~snowpea_core.attachments.store.AttachmentStore`).
    """

    name: str
    mime: str
    size: int
    sha256: str
    data: bytes | None = None
    path: Path | None = None

    # -- constructors -------------------------------------------------
    @classmethod
    def from_bytes(cls, name: str, data: bytes, mime: str | None = None) -> Attachment:
        """Build an inline attachment, sniffing the MIME type from the bytes."""
        _check_size(len(data), name)
        resolved = _resolve_mime(data, name, mime)
        if is_image(resolved):
            data, resolved = downscale_image(data, resolved)
        return cls(
            name=safe_name(name),
            mime=resolved,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    @classmethod
    def from_base64(cls, name: str, raw: str, mime: str | None = None) -> Attachment:
        """Build an inline attachment from a base64 (or data-URI) payload."""
        return cls.from_bytes(name, decode_base64(raw), mime)

    @classmethod
    def from_path(
        cls, path: Path | str, name: str | None = None, mime: str | None = None
    ) -> Attachment:
        """Read a file from disk and keep referring to it by path.

        The bytes are read once to sniff the type and hash them; an image that
        needs downscaling becomes inline instead, because the file on disk must
        not be rewritten under the user.
        """
        source = Path(path).expanduser()
        try:
            stat = source.stat()
        except OSError as exc:
            raise AttachmentError("invalid_params", f"attachment: {exc}") from exc
        _check_size(stat.st_size, name or source.name)
        try:
            data = source.read_bytes()
        except OSError as exc:  # pragma: no cover - stat succeeded, read failed
            raise AttachmentError("invalid_params", f"attachment: {exc}") from exc
        resolved = _resolve_mime(data, name or source.name, mime)
        if is_image(resolved):
            shrunk, resolved = downscale_image(data, resolved)
            if shrunk is not data:
                return cls(
                    name=safe_name(name or source.name),
                    mime=resolved,
                    size=len(shrunk),
                    sha256=hashlib.sha256(shrunk).hexdigest(),
                    data=shrunk,
                )
        return cls(
            name=safe_name(name or source.name),
            mime=resolved,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            path=source,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Attachment:
        """Build one from a wire ``{name, mime?, path?, data?}`` object."""
        if not isinstance(payload, dict):
            raise AttachmentError("invalid_params", "attachment: expected an object")
        name = str(payload.get("name") or "").strip()
        mime = payload.get("mime")
        mime = str(mime) if mime else None
        raw = payload.get("data")
        path = payload.get("path")
        if raw:
            return cls.from_base64(name or "attachment", str(raw), mime)
        if path:
            return cls.from_path(str(path), name or None, mime)
        raise AttachmentError("invalid_params", "attachment: needs either data or path")

    # -- access -------------------------------------------------------
    @property
    def is_image(self) -> bool:
        return is_image(self.mime)

    @property
    def extension(self) -> str:
        return extension_for(self.mime, self.name)

    def read_bytes(self) -> bytes:
        """The attachment's bytes, from memory or from disk."""
        if self.data is not None:
            return self.data
        if self.path is None:  # pragma: no cover - constructors forbid this
            raise AttachmentError("internal", f"attachment {self.name!r} has no bytes")
        try:
            return self.path.read_bytes()
        except OSError as exc:
            raise AttachmentError("invalid_params", f"attachment: {exc}") from exc

    def to_base64(self) -> str:
        """The attachment's bytes as base64, for a provider request."""
        return base64.b64encode(self.read_bytes()).decode("ascii")

    def to_text(self, limit: int = 20_000) -> str:
        """Decoded text for a text-ish attachment, truncated at ``limit``."""
        body = self.read_bytes().decode("utf-8", "replace")
        if len(body) <= limit:
            return body
        return body[:limit] + f"\n… [truncated, {len(body)} chars total]"

    def describe(self) -> str:
        """The one-line marker stored in history, e.g. ``[image: shot.png]``."""
        kind = "image" if self.is_image else "file"
        return f"[{kind}: {self.name}]"

    def to_ref(self) -> dict[str, Any]:
        """A JSON-safe reference (never the bytes) for history and events."""
        ref: dict[str, Any] = {
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
        }
        if self.path is not None:
            ref["path"] = str(self.path)
        return ref


def safe_name(name: str) -> str:
    """A display name with no path separators and no surprises."""
    cleaned = Path(str(name or "attachment")).name.strip() or "attachment"
    return cleaned[:120]


def _check_size(size: int, name: str) -> None:
    if size > MAX_BYTES:
        raise AttachmentError(
            "attachment_too_large",
            f"attachment {safe_name(name)!r} is {size} bytes; the limit is {MAX_BYTES}",
        )


def _resolve_mime(data: bytes, name: str | None, declared: str | None) -> str:
    """The sniffed type, falling back to what the client declared.

    The client is believed only when the bytes look like nothing recognisable
    *and* it is not claiming an image type: a declared ``image/png`` that does
    not start with a PNG header would be rejected by the vendor anyway, so it
    stays ``application/octet-stream`` and travels as a named file instead.
    """
    sniffed = sniff_mime(data, name)
    if sniffed == OCTET_STREAM and declared and not is_image(declared):
        return declared
    return sniffed


def downscale_image(data: bytes, mime: str, max_edge: int = MAX_IMAGE_EDGE) -> tuple[bytes, str]:
    """Shrink an image to ``max_edge`` on its longest side.

    Returns the original object unchanged when Pillow is not installed, when
    the image is already small enough, or when re-encoding fails — a slightly
    large image is far better than a dropped one.
    """
    try:  # Pillow is optional; the core must work without it.
        from PIL import Image  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        return data, mime
    import io

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if max(width, height) <= max_edge:
                return data, mime
            if getattr(image, "is_animated", False):
                # Resizing an animation would drop every frame but the first.
                return data, mime
            scale = max_edge / float(max(width, height))
            resized = image.convert("RGB" if mime == "image/jpeg" else "RGBA")
            resized = resized.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.LANCZOS,
            )
            buffer = io.BytesIO()
            out_format = "JPEG" if mime == "image/jpeg" else "PNG"
            out_mime = "image/jpeg" if out_format == "JPEG" else "image/png"
            resized.save(buffer, format=out_format)
    except Exception as exc:  # noqa: BLE001 - a bad image must not fail the turn
        log.debug("image downscale skipped: %s", exc)
        return data, mime
    return buffer.getvalue(), out_mime


__all__ = [
    "EXTENSIONS",
    "IMAGE_MIMES",
    "MAX_BYTES",
    "MAX_IMAGE_EDGE",
    "OCTET_STREAM",
    "Attachment",
    "AttachmentError",
    "decode_base64",
    "downscale_image",
    "extension_for",
    "is_image",
    "is_text",
    "safe_name",
    "sniff_mime",
]
