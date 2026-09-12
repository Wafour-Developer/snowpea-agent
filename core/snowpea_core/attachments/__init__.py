"""Attachments a user pastes, drags or points at in a prompt."""

from __future__ import annotations

from snowpea_core.attachments.model import (
    MAX_BYTES,
    MAX_IMAGE_EDGE,
    Attachment,
    AttachmentError,
    is_image,
    is_text,
    sniff_mime,
)
from snowpea_core.attachments.store import AttachmentStore

__all__ = [
    "MAX_BYTES",
    "MAX_IMAGE_EDGE",
    "Attachment",
    "AttachmentError",
    "AttachmentStore",
    "is_image",
    "is_text",
    "sniff_mime",
]
