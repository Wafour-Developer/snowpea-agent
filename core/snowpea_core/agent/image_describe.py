"""Images for a model that cannot see them: ``models.categories.vision`` (1.33.0).

When a turn carries an image — an attachment, or a tool result such as a
screenshot — and the session's own model has no vision, the image used to
reach it as ``(this model cannot see images …)``, and ``view_image`` or a
screenshot tool refused outright.  With ``models.categories.vision`` set, the
vision model looks at each image once and its description joins the stored
message or tool result as text.

The session keeps its own model for the whole turn.  Swapping the turn onto
the vision model would throw away the session's prefix cache and its reasoning
style for one picture; a description written once into the stored history is
byte-stable from then on, so every later request still reuses the same prefix.

The chain is ``vision`` → ``standard`` (only when that one can see) → nothing,
which is today's behaviour.  With ``vision`` unset nothing here runs at all.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.attachments.model import Attachment, is_image
from snowpea_core.config.model_routing import ModelRoute, category_route
from snowpea_core.providers import content as content_parts
from snowpea_core.providers.base import ChatMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.tools.registry import ToolResult

log = logging.getLogger("snowpea.agent.image_describe")

#: What the vision model is asked for each image.
DESCRIBE_PROMPT = (
    "Describe this image for an assistant that cannot see it. Transcribe any text in it "
    "verbatim, then describe the layout, the notable elements and anything that looks like "
    "an error, a warning or a highlighted state. Be factual and complete; no preamble."
)

#: Output cap for one description.
MAX_TOKENS = 1024

#: Longest one description may take; past it the image gets today's note.
DESCRIBE_TIMEOUT_SEC = 60.0


def vision_assist(core: Core, session: Any) -> ModelRoute | None:
    """The route that describes images for ``session``, or ``None`` (today's behaviour).

    ``None`` whenever ``models.categories.vision`` is unset, the session's model
    is known to see images itself, or neither ``vision`` nor ``standard`` resolves to a
    model that can.
    """
    settings = getattr(core, "settings", None)
    categories = getattr(getattr(settings, "models", None), "categories", None) or {}
    if not isinstance(categories, dict) or not str(categories.get("vision") or "").strip():
        return None
    registry = core.providers
    # A model known to see needs no help.  One nobody has probed is not asked
    # either: the person named a vision model, so it is used instead of spending
    # a request to learn what it would say.
    own_vendor = session.provider or registry.default_vendor()
    if registry.vision_for(own_vendor, session.model) is True:
        return None
    workdir = getattr(session, "workdir", None)
    for slot, need_known in (("vision", False), ("standard", True)):
        route = category_route(settings, slot, workdir=workdir)  # type: ignore[arg-type]
        if not route.resolved():
            continue
        vendor = route.provider or registry.default_vendor()
        answer = registry.vision_for(vendor, route.model)
        # The slot named for it may be a local model nobody has probed yet;
        # the standard fallback must be known to see.
        if answer is True or (answer is None and not need_known):
            return route
    return None


async def describe_with_error(
    core: Core, route: ModelRoute, attachment: Attachment
) -> tuple[str | None, str | None]:
    """``(description, None)``, or ``(None, short reason)`` when the vision model failed."""
    from snowpea_core.agent.definition import complete_text

    try:
        provider = core.providers.get(route.provider, route.model)
        message = ChatMessage(
            role="user", content=content_parts.history_blocks(DESCRIBE_PROMPT, [attachment])
        )
        text = await asyncio.wait_for(
            complete_text(provider, [message], max_tokens=MAX_TOKENS), DESCRIBE_TIMEOUT_SEC
        )
    except TimeoutError:
        log.info("the vision model timed out on %s", attachment.name)
        return None, f"timed out after {int(DESCRIBE_TIMEOUT_SEC)}s"
    except Exception as exc:  # noqa: BLE001 - a failed description falls back to today's note
        log.info("the vision model could not describe %s", attachment.name, exc_info=True)
        return None, f"{type(exc).__name__}: {str(exc)[:120]}".strip()
    text = text.strip()
    return (text, None) if text else (None, "the vision model returned nothing")


async def describe(core: Core, route: ModelRoute, attachment: Attachment) -> str | None:
    """One image described by ``route``'s model; ``None`` when that fails or times out."""
    text, _ = await describe_with_error(core, route, attachment)
    return text


def configured_vision_ref(core: Core) -> str | None:
    categories = getattr(getattr(core.settings, "models", None), "categories", None) or {}
    ref = str(categories.get("vision") or "").strip() if isinstance(categories, dict) else ""
    return ref or None


def cannot_see(core: Core, session: Any) -> bool:
    """True when the session's model is known not to take images."""
    from snowpea_core.tools.view_image import session_can_see_images

    return not session_can_see_images(core, session)


async def notify_unseen(
    core: Core,
    session: Any,
    reason: str,
    source: str,
    *,
    count: int = 1,
    error: str | None = None,
) -> None:
    """Tell the surface a picture went unseen: ``vision.unavailable``, once per turn per reason."""
    from snowpea_core.session import events

    turn = getattr(session, "current_turn", None)
    seen = getattr(session, "vision_notified", None)
    if seen is None or seen[0] != turn:
        seen = (turn, set())
        session.vision_notified = seen
    if reason in seen[1]:
        return
    seen[1].add(reason)
    try:
        vendor = session.provider or core.providers.default_vendor()
    except Exception:  # noqa: BLE001 - naming is cosmetic
        vendor = session.provider
    await core.hub.emit_event(
        session.id,
        events.vision_unavailable(
            reason, source, vendor, session.model, configured_vision_ref(core), count, error
        ),
    )


async def describe_blocks(core: Core, session: Any, blocks: list[dict[str, Any]]) -> None:
    """Add a ``description`` to every image block of a user turn, in place.

    :func:`snowpea_core.providers.content.to_text` then shows it where the
    "cannot see images" note would have been; a model that can see still gets
    the image itself.
    """
    images = [block for block in blocks if block.get("type") == "image"]
    if not images:
        return
    route = vision_assist(core, session)
    if route is None:
        if cannot_see(core, session):
            await notify_unseen(core, session, "no_vision_model", "attachment", count=len(images))
        return
    failed = 0
    last_error: str | None = None
    for block in blocks:
        if _interrupted(session):
            return
        if block.get("type") != "image" or not is_image(str(block.get("mime") or "")):
            continue
        attachment = _attachment_of(block)
        if attachment is None:
            continue
        text, error = await describe_with_error(core, route, attachment)
        if text:
            block["description"] = text
        else:
            failed += 1
            last_error = error
    if failed:
        await notify_unseen(
            core, session, "describe_failed", "attachment", count=failed, error=last_error
        )


def _interrupted(session: Any) -> bool:
    flag = getattr(session, "interrupt", None)
    return bool(flag is not None and flag.is_set())


def _attachment_of(block: dict[str, Any]) -> Attachment | None:
    name = str(block.get("name") or "image")
    mime = str(block.get("mime") or "") or None
    try:
        if block.get("data"):
            return Attachment.from_bytes(name, base64.b64decode(str(block["data"])), mime)
        if block.get("path"):
            return Attachment.from_path(Path(str(block["path"])), name, mime)
    except Exception:  # noqa: BLE001 - an unreadable image is simply not described
        return None
    return None


async def describe_tool_images(core: Core, session: Any, result: ToolResult) -> ToolResult:
    """``result`` with a description of each image it carries, for a blind model.

    Unchanged unless :func:`vision_assist` answers, and always for a result
    marked ``sensitive``: what it shows is not handed to a second model.  The
    images themselves stay in ``meta`` for surfaces, and the session's history
    gets text only.
    """
    from snowpea_core.tools.view_image import _image_metas

    if (result.meta or {}).get("sensitive"):
        return result
    route = vision_assist(core, session)
    metas = _image_metas(result)
    if route is None:
        if metas and cannot_see(core, session):
            await notify_unseen(core, session, "no_vision_model", "tool", count=len(metas))
        return result
    parts: list[str] = []
    failed = 0
    last_error: str | None = None
    for meta in metas:
        if _interrupted(session):
            break
        name = str(meta.get("name") or Path(str(meta.get("path") or "image")).name)
        attachment = _attachment_of(
            {
                "name": name,
                "mime": meta.get("mime"),
                "data": meta.get("bytes_b64"),
                "path": meta.get("path"),
            }
        )
        if attachment is None:
            continue
        text, error = await describe_with_error(core, route, attachment)
        if text:
            parts.append(f"[image {name}, described by the vision model]\n{text}")
        else:
            failed += 1
            last_error = error
    if failed:
        await notify_unseen(
            core, session, "describe_failed", "tool", count=failed, error=last_error
        )
    if not parts:
        return result
    output = "\n\n".join([result.output, *parts]) if result.output else "\n\n".join(parts)
    result.output = output
    return result


__all__ = [
    "DESCRIBE_PROMPT",
    "cannot_see",
    "configured_vision_ref",
    "describe_with_error",
    "notify_unseen",
    "describe",
    "describe_blocks",
    "describe_tool_images",
    "vision_assist",
]
