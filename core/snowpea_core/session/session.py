"""The :class:`Session` entity (contract §4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.exec.local import LocalBackend
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
    #: Where this session's tools run; ``backend.set`` / ``/backend`` swaps it
    #: (US-010).  Defaults to the local machine, rooted at :attr:`workdir`.
    backend: ExecutionBackend = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.backend is None:  # type: ignore[unreachable]
            self.backend = LocalBackend(self.workdir)  # type: ignore[unreachable]

    async def set_backend(self, backend: ExecutionBackend) -> None:
        """Replace the backend, closing whatever it was using before."""
        previous = self.backend
        self.backend = backend
        if previous is not None and previous is not backend:
            await previous.close()

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
