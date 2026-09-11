"""The :class:`Session` entity (contract §4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.server.protocol import Mode, SessionSummary
from snowpea_core.session.history import History


@dataclass
class Session:
    """One conversation: a workdir, a mode, a message history and an event seq."""

    id: str
    workdir: Path
    mode: Mode = "accept"
    provider: str | None = None
    model: str | None = None
    agent: str | None = None
    origin_surface: str | None = None
    created_at: str = ""
    closed_at: str | None = None
    max_concurrent: int = 3
    history: History = field(default_factory=History)
    seq: int = 0
    #: The connection that created (or last resumed) the session; interactive
    #: ``approval.request`` calls go only here (contract §7).
    origin_conn: Any = None
    #: Set by ``session.interrupt``; the agent loop checks it between steps.
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    turn_task: asyncio.Task[Any] | None = None
    current_turn: str | None = None

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def summary(self) -> SessionSummary:
        return SessionSummary(
            sessionId=self.id,
            workdir=str(self.workdir),
            mode=self.mode,
            provider=self.provider,
            model=self.model,
            originSurface=self.origin_surface,
            createdAt=self.created_at,
            seq=self.seq,
        )


__all__ = ["Session"]
