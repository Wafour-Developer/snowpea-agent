"""Idle tracking and graceful shutdown.

The daemon exits on its own once nothing is left to serve: no sessions, jobs,
gateway bindings or named agents for ``daemon.idleTimeoutSec`` seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("snowpea.lifecycle")

COUNTERS: tuple[str, ...] = ("sessions", "jobs", "gateway_bindings", "named_agents")
TICK_SECONDS = 1.0

#: Singular/plural wording for the keepalive reasons (plan §2.6), in the order
#: they are reported: ``will not exit: 2 gateway bindings, 1 job``.
COUNTER_LABELS: dict[str, tuple[str, str]] = {
    "sessions": ("session", "sessions"),
    "gateway_bindings": ("gateway binding", "gateway bindings"),
    "jobs": ("job", "jobs"),
    "named_agents": ("named agent", "named agents"),
}


class Lifecycle:
    """Counts live resources and fires ``on_idle`` when they stay at zero."""

    def __init__(
        self,
        idle_timeout_sec: int = 1800,
        on_idle: Callable[[], Awaitable[None]] | None = None,
        *,
        tick: float = TICK_SECONDS,
    ) -> None:
        self.idle_timeout_sec = idle_timeout_sec
        self.counters: dict[str, int] = dict.fromkeys(COUNTERS, 0)
        self._on_idle = on_idle
        self._tick = tick
        self._idle_since: float | None = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # -- counters ------------------------------------------------------
    def set_counter(self, name: str, value: int) -> None:
        """Set one counter; unknown names are rejected loudly."""
        if name not in self.counters:
            raise KeyError(f"unknown lifecycle counter: {name}")
        self.counters[name] = max(0, int(value))
        self._refresh_idle()

    def increment(self, name: str, delta: int = 1) -> None:
        self.set_counter(name, self.counters[name] + delta)

    @property
    def busy(self) -> bool:
        return any(value > 0 for value in self.counters.values())

    def _refresh_idle(self) -> None:
        if self.busy:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()

    # -- status --------------------------------------------------------
    def seconds_until_exit(self) -> float | None:
        """Seconds left before the idle shutdown, or ``None`` while busy."""
        if self.busy or self._idle_since is None:
            return None
        elapsed = time.monotonic() - self._idle_since
        return max(0.0, self.idle_timeout_sec - elapsed)

    def reasons(self) -> list[str]:
        """Why the daemon is staying up, e.g. ``["2 gateway bindings", "1 job"]``.

        Empty while nothing is registered, which is exactly when the idle timer
        is allowed to run (plan §2.6: sessions, jobs, gateway bindings and
        named agents must all be zero).
        """
        out: list[str] = []
        for name, (singular, plural) in COUNTER_LABELS.items():
            count = self.counters.get(name, 0)
            if count > 0:
                out.append(f"{count} {singular if count == 1 else plural}")
        return out

    def summary(self) -> str:
        """One line for ``snowpea daemon status``."""
        remaining = self.seconds_until_exit()
        if remaining is not None:
            return f"will exit in {remaining:.0f}s"
        reasons = self.reasons()
        if not reasons:  # idle shutdown disabled
            return "will not exit: idle shutdown is off"
        return "will not exit: " + ", ".join(reasons)

    def status(self) -> dict[str, Any]:
        """Snapshot for ``system.info`` / diagnostics."""
        remaining = self.seconds_until_exit()
        will_exit = remaining is not None
        return {
            "counters": dict(self.counters),
            "willExit": will_exit,
            "reason": "idle" if will_exit else "busy",
            "secondsUntilExit": remaining,
            "reasons": self.reasons(),
            "summary": self.summary(),
        }

    # -- loop ----------------------------------------------------------
    def start(self) -> None:
        """Begin the idle timer (no-op when the timeout is disabled)."""
        if self._task is not None or self.idle_timeout_sec <= 0:
            return
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(self._tick)
                remaining = self.seconds_until_exit()
                if remaining is not None and remaining <= 0:
                    log.info("idle for %ss, shutting down", self.idle_timeout_sec)
                    if self._on_idle is not None:
                        await self._on_idle()
                    return
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    async def stop(self) -> None:
        """Cancel the idle timer."""
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = ["COUNTER_LABELS", "COUNTERS", "TICK_SECONDS", "Lifecycle"]
