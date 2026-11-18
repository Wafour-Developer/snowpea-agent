"""``feedback.preview`` / ``feedback.send`` — 개선하기 (protocol 1.59.0).

A person sends one conversation to the Snowpea team (capability
``improvementFeedback``; :mod:`snowpea_core.feedback` builds and uploads the
report).  Only a person's connection may call either method — a local person
or a remote web login (``person_gate.PERSON_METHODS``, checked by the
dispatcher before these run, and again here); a connector, a browser host
(it has its own sender), Jarvis, a messenger and every other in-process caller
are refused.  Neither method is a tool: an agent never sends a conversation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    SERVER_VERSION,
    FeedbackPreviewParams,
    FeedbackPreviewResult,
    FeedbackSendParams,
    FeedbackSendResult,
    FeedbackStats,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.feedback import Built
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger("snowpea.feedback")

#: Methods this module implements.
HANDLED_METHODS: tuple[str, ...] = ("feedback.preview", "feedback.send")


def _endpoint() -> str:
    from snowpea_core.feedback.sender import EndpointError, feedback_endpoint

    try:
        return feedback_endpoint()
    except EndpointError as exc:
        raise RpcError(errors.UNAVAILABLE, str(exc), {"reason": "endpoint"}) from exc


async def _build(
    conn: RpcConnection,
    core: Core,
    params: FeedbackPreviewParams,
    surface: str,
    surface_version: str | None = None,
    attachments: list | None = None,
) -> Built:
    from snowpea_core.feedback import SessionMissing, build_report
    from snowpea_core.feedback.report import ReportError
    from snowpea_core.server.person_gate import require_person

    require_person(conn, "send feedback")
    try:
        return await build_report(
            core,
            params.sessionId,
            comment=params.comment or "",
            flagged=[f.model_dump(exclude_none=True) for f in params.flagged or []],
            surface=surface,
            surface_version=surface_version,
            attachments=attachments,
        )
    except SessionMissing as exc:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}") from exc
    except ReportError as exc:
        if exc.too_large:
            raise RpcError(errors.TOO_LARGE, str(exc), {"field": exc.field}) from exc
        raise RpcError(errors.INVALID_PARAMS, str(exc), {"field": exc.field}) from exc


async def feedback_preview_handler(
    conn: RpcConnection, params: FeedbackPreviewParams, core: Core
) -> FeedbackPreviewResult:
    """``feedback.preview`` — what would leave, and where to; nothing is sent."""
    from snowpea_core.feedback import surface_of

    endpoint = _endpoint()
    built = await _build(conn, core, params, surface_of(conn))
    return FeedbackPreviewResult(
        report=built.report, stats=FeedbackStats(**built.stats), endpoint=endpoint
    )


async def feedback_send_handler(
    conn: RpcConnection, params: FeedbackSendParams, core: Core
) -> FeedbackSendResult:
    """``feedback.send`` — build the report again and upload it once."""
    from snowpea_core.feedback.sender import UploadError, post_feedback

    endpoint = _endpoint()
    attachments = [a.model_dump() for a in params.attachments] if params.attachments else None
    built = await _build(conn, core, params, params.surface, params.surfaceVersion, attachments)
    log.info(
        "feedback: sending session %s (%d messages, %d flagged, %d bytes gzip, %d fields dropped)",
        params.sessionId,
        built.stats["messages"],
        len(built.report["flagged"]),
        len(built.body),
        built.stats["droppedResults"],
    )
    try:
        answer = await post_feedback(
            endpoint, built.body, user_agent=f"snowpea-core/{SERVER_VERSION}"
        )
    except UploadError as exc:
        details: dict[str, object] = {"status": exc.status}
        if exc.retry_after is not None:
            details["retryAfterSec"] = exc.retry_after
        if exc.server_error:
            details["serverError"] = exc.server_error
        if exc.server_message:
            details["serverMessage"] = exc.server_message
        code = {
            "rejected": errors.FEEDBACK_REJECTED,
            "busy": errors.FEEDBACK_BUSY,
        }.get(exc.kind, errors.FEEDBACK_UNREACHABLE)
        log.info("feedback: not sent (%s, status %s)", exc.kind, exc.status)
        raise RpcError(code, f"feedback was not sent: {exc}", details) from exc
    log.info("feedback: report %s received", answer["id"])
    return FeedbackSendResult(id=answer["id"], receivedAt=answer["receivedAt"])


def register_feedback_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("feedback.preview", feedback_preview_handler)
    dispatcher.register("feedback.send", feedback_send_handler)
    return dispatcher


__all__ = [
    "HANDLED_METHODS",
    "feedback_preview_handler",
    "feedback_send_handler",
    "register_feedback_handlers",
]
