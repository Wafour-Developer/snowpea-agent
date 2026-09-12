"""The hand-off between ``session.prompt`` and the turn that consumes it.

``session.prompt`` validates and stores a turn's attachments, but the agent
loop is what actually turns them into provider content parts.  The two live in
different modules with different owners, so rather than widening
``agent.loop.start_turn``'s signature, the handler *stashes* the attachments
here and the loop takes them at the top of the turn.

The queue is per session and take-once: a turn consumes exactly the
attachments that were sent with its prompt, and an interrupted or failed turn
never leaks them into the next one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from snowpea_core.attachments.model import Attachment

log = logging.getLogger("snowpea.attachments.pending")

#: ``session id -> the attachments its next turn should consume``.
_PENDING: dict[str, list[Attachment]] = {}


def stash(session_id: str, attachments: Sequence[Attachment]) -> None:
    """Hold ``attachments`` for the next turn of ``session_id``.

    A second prompt before the first turn started replaces the stash rather
    than appending to it — the newer prompt is the one about to run.
    """
    if not attachments:
        _PENDING.pop(session_id, None)
        return
    _PENDING[session_id] = list(attachments)


def take(session_id: str) -> list[Attachment]:
    """The attachments for this turn, removing them from the queue."""
    return _PENDING.pop(session_id, [])


def peek(session_id: str) -> list[Attachment]:
    """What is waiting, without consuming it (for tests and diagnostics)."""
    return list(_PENDING.get(session_id, ()))


def clear(session_id: str | None = None) -> None:
    """Drop one session's stash, or every stash when called with nothing."""
    if session_id is None:
        _PENDING.clear()
        return
    _PENDING.pop(session_id, None)


__all__ = ["clear", "peek", "stash", "take"]
