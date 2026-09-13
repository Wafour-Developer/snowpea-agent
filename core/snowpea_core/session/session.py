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
    team: str | None = None
    team_agents: tuple[str, ...] = ()
    origin_surface: str | None = None
    #: Set on a child session created by ``delegate_task`` / ``agent.spawn``
    #: (M7 contract §3); ``None`` for a session a human opened.
    parent_session_id: str | None = None
    #: An ``AgentDefinition``'s own prompt, composed after the role file so a
    #: subagent speaks with its definition's voice (M7 §3) *and* keeps the
    #: coding discipline every other agent has (CORE-prompts).
    system_prompt: str | None = None
    #: Name of a file in ``prompts/roles/`` to compose into a child's prompt;
    #: ``None`` when the definition has no matching built-in role.
    prompt_role: str | None = None
    #: True for a session a parent agent spawned: it gets the subagent preamble
    #: (no user is watching; the final message is the whole report).
    is_subagent: bool = False
    #: ``"on"`` | ``"off"`` | ``"auto"`` for this session only — an agent
    #: definition's ``thinking:``.  ``None`` falls back to the vendor block and
    #: then ``agent.thinking`` (CORE-reasoning-budget).
    thinking: str | None = None
    #: Long-term memory namespace (M5 contract §1): ``"default"`` for
    #: interactive sessions, ``"agent:<name>"`` for named agents.
    memory_namespace: str = "default"
    created_at: str = ""
    closed_at: str | None = None
    max_concurrent: int = 3
    history: History = field(default_factory=History)
    seq: int = 0
    #: True for sessions nobody is watching — a scheduled job (M5 contract §2)
    #: or a gateway message.  Their approvals go to the shared queue.
    unattended: bool = False
    #: The connection that created (or last resumed) the session; interactive
    #: ``approval.request`` calls go only here (contract §7).
    origin_conn: Any = None
    #: Tool names a skill command restricts the current turn to
    #: (``allowed-tools`` in its front matter); ``None`` means every tool.
    allowed_tools: set[str] | None = None
    #: Set by ``session.interrupt``; the agent loop checks it between steps.
    interrupt: asyncio.Event = field(default_factory=asyncio.Event)
    turn_task: asyncio.Task[Any] | None = None
    current_turn: str | None = None
    #: Prompts submitted while a turn is running.  They are drained FIFO by
    #: the same task so two turns never mutate one history concurrently.
    queued_turns: list[Any] = field(default_factory=list)
    #: Tokens the current prompt occupies, provider-reported when
    #: :attr:`context_estimated` is False (CORE-context).
    context_used: int = 0
    #: True while :attr:`context_used` is a local estimate rather than a count
    #: the provider itself reported.
    context_estimated: bool = True
    #: Context window of :attr:`model` in tokens; ``None`` when unknown.
    context_window: int | None = None
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
            contextUsed=self.context_used,
            contextWindow=self.context_window,
        )


__all__ = ["Session"]
