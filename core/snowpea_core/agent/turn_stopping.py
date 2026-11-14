"""``turn.stopping``: the moment a turn would end, and who may keep it going (M16 P1.5).

The model answered without calling a tool.  Before the turn ends, the listeners
on :data:`CHAIN` run in order; the first one that returns a
:class:`StopDecision` wins, and its text becomes the next instruction.  None of
them, and the turn is done (dsh ``agent/turn-stopping``).

Built in, in this order:

* ``verify-gate`` (100) — edited code with no fresh passing check, a UI never
  looked at, open to-dos (Hermes verify-on-stop, ``agent.verify_gate``);
* ``stop-hooks`` (200) — Claude Code ``Stop`` hooks (``skills.hooks.stop``),
  at most ``MAX_STOP_CONTINUATIONS`` times a turn.

A plugin registers its own listener the same way and gets a disposer back.  A
listener that raises is logged and skipped.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.turn_stopping")


@dataclass(frozen=True)
class StopContext:
    """What the listeners know about the stop."""

    last_message: str
    #: Stop-hook continuations already spent this turn.
    stop_continuations: int = 0
    #: The stop is the model's answer to the round-budget probe.
    at_budget: bool = False


@dataclass(frozen=True)
class StopDecision:
    """Keep going: ``text`` is the next instruction."""

    text: str
    #: Who asked (``verify-gate``, ``stop-hook``, a plugin name).
    producer: str
    #: ``message``: a user message of its own; ``notice``: a session notice line.
    via: Literal["message", "notice"] = "message"
    #: Spends one of the turn's stop-hook continuations.
    counts_as_stop_hook: bool = False
    #: What surfaces show for it (``hook.continue``); ``text`` when empty.
    shown: str = ""


Listener = Callable[
    ["Core", "Session", StopContext],
    "StopDecision | None | Awaitable[StopDecision | None]",
]


@dataclass(frozen=True)
class _Entry:
    name: str
    order: int
    seq: int
    listener: Listener


class TurnStoppingChain:
    """Ordered listeners; the first decision wins."""

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._seq = 0
        self._lock = threading.Lock()

    def register(self, name: str, listener: Listener, *, order: int = 0) -> Callable[[], None]:
        with self._lock:
            self._seq += 1
            entry = _Entry(name, order, self._seq, listener)
            self._entries.append(entry)

        def dispose() -> None:
            with self._lock:
                if entry in self._entries:
                    self._entries.remove(entry)

        return dispose

    def listeners(self) -> list[_Entry]:
        with self._lock:
            entries = list(self._entries)
        return sorted(entries, key=lambda entry: (entry.order, entry.seq))

    async def run(self, core: Core, session: Session, ctx: StopContext) -> StopDecision | None:
        if session.interrupt.is_set():
            return None
        for entry in self.listeners():
            try:
                decision = entry.listener(core, session, ctx)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception:  # noqa: BLE001 - a broken listener must not end the turn badly
                log.warning("turn.stopping listener %s failed", entry.name, exc_info=True)
                continue
            if decision is not None and decision.text.strip():
                return decision
        return None


# ---------------------------------------------------------------------------
# built-in listeners
# ---------------------------------------------------------------------------


def _verify_gate(core: Any, session: Any, ctx: StopContext) -> StopDecision | None:
    from snowpea_core.agent import verify_gate

    text = verify_gate.nudge(core, session, ctx.last_message)
    return StopDecision(text, "verify-gate") if text else None


async def _stop_hooks(core: Any, session: Any, ctx: StopContext) -> StopDecision | None:
    from snowpea_core.skills import hooks as plugin_hooks

    if ctx.stop_continuations >= plugin_hooks.MAX_STOP_CONTINUATIONS:
        return None
    decision = await plugin_hooks.stop(
        core, session, last_message=ctx.last_message, active=ctx.stop_continuations > 0
    )
    if not decision.block:
        return None
    return StopDecision(
        f"Stop hook: {decision.reason}",
        "stop-hook",
        via="notice",
        counts_as_stop_hook=True,
        shown=decision.reason,
    )


def default_chain() -> TurnStoppingChain:
    chain = TurnStoppingChain()
    chain.register("verify-gate", _verify_gate, order=100)
    chain.register("stop-hooks", _stop_hooks, order=200)
    return chain


#: The daemon's chain: built-ins plus whatever plugins add.
CHAIN = default_chain()


__all__ = ["CHAIN", "StopContext", "StopDecision", "TurnStoppingChain", "default_chain"]
