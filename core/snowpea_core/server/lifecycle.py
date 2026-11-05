"""Idle tracking and graceful shutdown.

The daemon exits on its own once nothing is left to serve: no sessions, jobs,
gateway bindings or named agents for ``daemon.idleTimeoutSec`` seconds.

A client that says ``holdSec`` in ``system.hello`` (protocol 1.40.0: ``snowpea
remote up`` and the browser it serves) changes two things: the timeout becomes
``holdSec``, and a session that is merely loaded -- no turn running -- no
longer keeps the daemon up, because such a client leaves its sessions open when
it goes away.  A running turn, a job, a gateway binding, a named agent or a
connected keep-alive client still do.  An ordinary client (a TUI, the desktop
app, the IDE) restores the configured timeout when it connects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("snowpea.lifecycle")

COUNTERS: tuple[str, ...] = (
    "sessions",
    "jobs",
    "gateway_bindings",
    "named_agents",
    "keepalive_clients",
)
TICK_SECONDS = 1.0
#: Bounds of the ``holdSec`` a client may ask for.
HOLD_MIN_SEC = 30
HOLD_MAX_SEC = 3600

#: Singular/plural wording for the keepalive reasons (plan §2.6), in the order
#: they are reported: ``will not exit: 2 gateway bindings, 1 job``.
COUNTER_LABELS: dict[str, tuple[str, str]] = {
    "sessions": ("session", "sessions"),
    "gateway_bindings": ("gateway binding", "gateway bindings"),
    "jobs": ("job", "jobs"),
    "named_agents": ("named agent", "named agents"),
    # A client that said ``keepAlive`` in ``system.hello`` (a browser that
    # hosts tools) keeps the daemon up while it is connected.
    "keepalive_clients": ("keep-alive client", "keep-alive clients"),
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
        #: Seconds of idleness a hold client allows instead of ``idle_timeout_sec``.
        self.hold_sec: int | None = None
        #: Sessions with a turn in flight; consulted instead of the ``sessions``
        #: counter while a hold is in force.
        self.running_turns: Callable[[], int] | None = None

    # -- hold ----------------------------------------------------------
    def set_hold(self, seconds: int | None) -> None:
        """Apply a connector's ``holdSec`` (clamped), or lift it with ``None``."""
        if seconds is None:
            self.hold_sec = None
        else:
            self.hold_sec = max(HOLD_MIN_SEC, min(HOLD_MAX_SEC, int(seconds)))
        self._refresh_idle()

    @property
    def idle_limit(self) -> float:
        """Seconds of idleness before the daemon exits; 0 or less means never."""
        return float(self.hold_sec if self.hold_sec is not None else self.idle_timeout_sec)

    def _count(self, name: str) -> int:
        if name == "sessions" and self.hold_sec is not None and self.running_turns is not None:
            try:
                return int(self.running_turns())
            except Exception:  # noqa: BLE001 - a lookup never stops the idle timer
                return self.counters[name]
        return self.counters[name]

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
        return any(self._count(name) > 0 for name in self.counters)

    def _refresh_idle(self) -> None:
        if self.busy:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()

    # -- status --------------------------------------------------------
    def seconds_until_exit(self) -> float | None:
        """Seconds left before the idle shutdown, or ``None`` while busy."""
        if self.busy or self._idle_since is None or self.idle_limit <= 0:
            return None
        elapsed = time.monotonic() - self._idle_since
        return max(0.0, self.idle_limit - elapsed)

    def reasons(self) -> list[str]:
        """Why the daemon is staying up, e.g. ``["2 gateway bindings", "1 job"]``.

        Empty while nothing is registered, which is exactly when the idle timer
        is allowed to run (plan §2.6: sessions, jobs, gateway bindings and
        named agents must all be zero).
        """
        out: list[str] = []
        for name, (singular, plural) in COUNTER_LABELS.items():
            count = self._count(name) if name in self.counters else 0
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
        if self._task is not None:
            return
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(self._tick)
                self._refresh_idle()  # a hold counts running turns, which have no counter
                if self.idle_limit <= 0:
                    continue
                remaining = self.seconds_until_exit()
                if remaining is not None and remaining <= 0:
                    log.info("idle for %ss, shutting down", self.idle_limit)
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


__all__ = [
    "COUNTER_LABELS",
    "COUNTERS",
    "HOLD_MAX_SEC",
    "HOLD_MIN_SEC",
    "TICK_SECONDS",
    "Lifecycle",
]
