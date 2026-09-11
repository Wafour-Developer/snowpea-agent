"""Approval queue — M1 stub (US-006 fills it in)."""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import ApprovalRequest


class ApprovalQueue:
    """Pending human approvals. Stub: nothing is ever pending."""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}

    def list(self, session_id: str | None = None) -> list[ApprovalRequest]:
        return []

    def get(self, request_id: str) -> Any | None:
        return self._pending.get(request_id)


__all__ = ["ApprovalQueue"]
