"""``view_image``: show an on-disk image to a vision-capable model."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from snowpea_core.attachments.model import (
    MAX_BYTES,
    MAX_IMAGE_EDGE,
    Attachment,
    downscale_image,
    is_image,
    sniff_mime,
)
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.providers import content as content_parts
from snowpea_core.providers.base import ChatMessage
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

#: Supported image extensions (the MIME sniff is authoritative).
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

#: Longest edge when ``detail`` is ``low``.
LOW_DETAIL_EDGE = 768


def resolve_image_path(ctx: ToolContext, raw: str) -> tuple[Path | None, str | None]:
    """Resolve ``raw`` inside the session workdir, or ``(None, error)``."""
    path = str(raw or "").strip()
    if not path:
        return None, "path is required"
    resolved = Path(ctx.backend.resolve(path))
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    workdir = Path(ctx.session.workdir).resolve()
    try:
        resolved.relative_to(workdir)
    except ValueError:
        return None, f"path is outside the session working directory: {path}"
    if not resolved.is_file():
        return None, f"no such file: {path}"
    return resolved, None


def session_can_see(ctx: ToolContext) -> bool:
    """True when the session's model may be sent image content."""
    registry = ctx.core.providers
    vendor = ctx.session.provider or registry.default_vendor()
    model = ctx.session.model
    answer = registry.vision_for(vendor, model)
    if answer is True:
        return True
    if answer is False:
        return False
    # ``None`` means "try once" for a local-style vendor — optimistic until
    # proven otherwise (CORE-vision).
    return answer is None and registry.is_local_style(vendor)


def image_dimensions(data: bytes, mime: str) -> tuple[int | None, int | None]:
    """``(width, height)`` from image bytes, or ``(None, None)`` when unknown."""
    try:  # Pillow is optional; dimensions are nice-to-have in the tool text.
        from PIL import Image  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        return None, None
    import io

    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:  # noqa: BLE001 - a bad image must not fail the call
        return None, None


def build_image_meta(attachment: Attachment, resolved: Path) -> dict[str, Any]:
    """Structured facts the agent loop turns into a user image message."""
    width, height = image_dimensions(attachment.read_bytes(), attachment.mime)
    meta: dict[str, Any] = {
        "path": str(resolved),
        "mime": attachment.mime,
        "width": width,
        "height": height,
    }
    if attachment.data is not None:
        meta["bytes_b64"] = attachment.to_base64()
    elif attachment.path is not None:
        meta["path"] = str(attachment.path)
    return meta


def session_can_see_images(core: Any, session: Any) -> bool:
    """True when the session model may be sent image content."""
    registry = core.providers
    vendor = session.provider or registry.default_vendor()
    model = session.model
    answer = registry.vision_for(vendor, model)
    if answer is True:
        return True
    if answer is False:
        return False
    return registry.is_local_style(vendor)


def _image_metas(result: ToolResult) -> list[dict[str, Any]]:
    meta = result.meta or {}
    images: list[dict[str, Any]] = []
    single = meta.get("image")
    if isinstance(single, dict):
        images.append(single)
    many = meta.get("images")
    if isinstance(many, list):
        for item in many:
            if isinstance(item, dict):
                images.append(item)
    return images


def flush_tool_image_messages(core: Any, session: Any, *, append: bool = True) -> None:
    """Append one user message for all pending tool images, or clear without appending."""
    pending = session.pending_tool_images
    if not pending:
        return
    if not append or not session_can_see_images(core, session):
        pending.clear()
        return
    attachments: list[Attachment] = []
    segments: list[str] = []
    for source, result in pending:
        labels: list[str] = []
        for image_meta in _image_metas(result):
            path = str(image_meta.get("path") or "").strip()
            raw_b64 = image_meta.get("bytes_b64")
            mime = str(image_meta.get("mime") or "")
            name = str(image_meta.get("name") or (Path(path).name if path else "image"))
            if isinstance(raw_b64, str) and raw_b64:
                attachment = Attachment.from_bytes(name, base64.b64decode(raw_b64), mime or None)
            elif path:
                attachment = Attachment.from_path(path, name or None, mime or None)
            else:
                continue
            attachments.append(attachment)
            labels.append(path or name)
        if labels:
            segments.append(f"{source}: {', '.join(labels)}")
    pending.clear()
    if not attachments:
        return
    label = f"(images from {'; '.join(segments)})"
    session.history.append(
        ChatMessage(
            role="user",
            content=content_parts.history_blocks(label, attachments),
        )
    )


def append_tool_image_messages(
    core: Any, session: Any, result: ToolResult, *, source: str = "tool"
) -> None:
    """After a tool that carried images, append the blocks the provider will read."""
    if not session_can_see_images(core, session):
        return
    attachments: list[Attachment] = []
    labels: list[str] = []
    for image_meta in _image_metas(result):
        path = str(image_meta.get("path") or "").strip()
        raw_b64 = image_meta.get("bytes_b64")
        mime = str(image_meta.get("mime") or "")
        name = str(image_meta.get("name") or (Path(path).name if path else "image"))
        if isinstance(raw_b64, str) and raw_b64:
            attachment = Attachment.from_bytes(name, base64.b64decode(raw_b64), mime or None)
        elif path:
            attachment = Attachment.from_path(path, name or None, mime or None)
        else:
            continue
        attachments.append(attachment)
        labels.append(path or name)
    if not attachments:
        return
    label = ", ".join(labels)
    session.history.append(
        ChatMessage(
            role="user",
            content=content_parts.history_blocks(
                f"(image from {source}: {label})",
                attachments,
            ),
        )
    )


def append_view_image_message(session: Any, result: ToolResult) -> None:
    """After ``view_image``, append the image block the provider will read."""
    core = getattr(session, "core", None)
    if core is None:
        return
    append_tool_image_messages(core, session, result, source="view_image")


async def view_image(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw = str(args.get("path", "")).strip()
    resolved, error = resolve_image_path(ctx, raw)
    if error or resolved is None:
        return ToolResult(ok=False, error=error or "path is required")
    if not session_can_see(ctx):
        return ToolResult(
            ok=False,
            error=(
                "this model cannot see images; switch to a vision-capable model with "
                "/model, or describe the image in text"
            ),
        )
    suffix = resolved.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return ToolResult(
            ok=False,
            error=f"{raw} is not a supported image (png/jpg/webp/gif)",
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if len(data) > MAX_BYTES:
        return ToolResult(
            ok=False,
            error=f"image is {len(data)} bytes; the limit is {MAX_BYTES}",
        )
    mime = sniff_mime(data, resolved.name)
    if not is_image(mime):
        return ToolResult(
            ok=False,
            error=f"{raw} is not a supported image (png/jpg/webp/gif)",
        )
    detail = str(args.get("detail") or "high").strip().lower()
    max_edge = LOW_DETAIL_EDGE if detail == "low" else MAX_IMAGE_EDGE
    shrunk, mime = downscale_image(data, mime, max_edge=max_edge)
    if shrunk is not data:
        attachment = Attachment.from_bytes(resolved.name, shrunk, mime)
    else:
        attachment = Attachment.from_path(resolved, mime=mime)
    image_meta = build_image_meta(attachment, resolved)
    width = image_meta.get("width")
    height = image_meta.get("height")
    size_label = (
        f"{width}×{height}"
        if isinstance(width, int) and isinstance(height, int)
        else "unknown size"
    )
    size_kb = max(1, attachment.size // 1024) if attachment.size else 1
    output = (
        f"image attached: {attachment.name} {size_label} {attachment.mime} ({size_kb} KB)"
    )
    return ToolResult(
        ok=True,
        output=output,
        path=raw,
        meta={"image": image_meta},
    )


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="view_image",
        category="file",
        description=descriptions.VIEW_IMAGE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Image file to inspect (relative to the workdir).",
                },
                "detail": {
                    "type": "string",
                    "enum": ["low", "high"],
                    "description": "low sends a smaller image; high keeps more detail.",
                },
            },
            "required": ["path"],
        },
        permission="read",
        run=view_image,
    ),
)


__all__ = [
    "IMAGE_SUFFIXES",
    "TOOLS",
    "flush_tool_image_messages",
    "append_tool_image_messages",
    "append_view_image_message",
    "image_dimensions",
    "resolve_image_path",
    "session_can_see",
    "session_can_see_images",
    "view_image",
]
