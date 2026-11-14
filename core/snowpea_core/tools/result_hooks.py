"""What happens to a tool result before the model reads it (M16 P1.4).

A listener on :data:`POST_EXECUTE` sees every finished call and may

* **accept** it — optionally replacing the text the model reads and adding
  context notes (sent before the next model call, never between the results
  of one batch);
* **block** it — the call becomes an error whose text is the listener's
  corrective feedback, and no later listener runs.

Listeners run in ``order`` (then registration order).  Registration returns a
disposer, so a plugin's listener leaves with the plugin.  A listener that
raises is logged and skipped: one broken plugin must not fail a call
(dsh ``tools/post-execute``, ``PostToolDecision``).
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.tools.registry import ToolContext, ToolResult

log = logging.getLogger("snowpea.tools.hooks")


@dataclass(frozen=True)
class PostDecision:
    """A listener's verdict on one result."""

    kind: Literal["accept", "block"] = "accept"
    #: ``accept``: the text the model reads instead of the tool's own output.
    output: str | None = None
    #: ``block``: what the model is told instead (why, and what to do).
    feedback: str | None = None
    #: Notes for the model, sent before its next call.
    contexts: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def accept(cls, output: str | None = None, *contexts: str) -> PostDecision:
        return cls("accept", output=output, contexts=tuple(contexts))

    @classmethod
    def block(cls, feedback: str, *contexts: str) -> PostDecision:
        return cls("block", feedback=feedback, contexts=tuple(contexts))


Listener = Callable[
    ["ToolContext", str, dict[str, Any], "ToolResult"],
    "PostDecision | None | Awaitable[PostDecision | None]",
]


@dataclass(frozen=True)
class _Entry:
    name: str
    order: int
    seq: int
    listener: Listener


class PostExecuteChain:
    """Ordered listeners over finished tool calls."""

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

    async def run(
        self, ctx: ToolContext, name: str, args: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        for entry in self.listeners():
            try:
                decision = entry.listener(ctx, name, args, result)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception:  # noqa: BLE001 - a broken listener must not fail the call
                log.warning(
                    "post-execute listener %s failed on %s", entry.name, name, exc_info=True
                )
                continue
            if decision is None:
                continue
            contexts = [*result.contexts, *decision.contexts]
            if decision.kind == "block":
                return replace(
                    result,
                    ok=False,
                    output="",
                    error=decision.feedback or f"{name}: blocked by {entry.name}",
                    contexts=contexts,
                    concludes_turn=False,
                )
            result = replace(
                result,
                output=decision.output if decision.output is not None else result.output,
                contexts=contexts,
            )
        return result


#: The daemon's chain: plugins and built-ins register here.
POST_EXECUTE = PostExecuteChain()


__all__ = ["POST_EXECUTE", "PostDecision", "PostExecuteChain"]
