"""One daemon.log line per model call: when it started, first answered, ended.

A ``usage`` event carries only the end, so a slow call could not be split into
time spent waiting for the server (queueing and prefill, up to the first
token) and time spent decoding.  Every call the loop or ``llm.sample`` makes
logs both, with its token counts::

    llm call session=s-… kind=turn vendor=local model=qwen38 ttft=2.10s
        decode=14.31s total=16.41s in=98213 cached=97900 out=1530 reasoning=1488
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("snowpea.llm")


@dataclass
class CallTiming:
    """The clock of one model call; :meth:`first` on every streamed event."""

    session_id: str
    kind: str
    vendor: str = ""
    model: str = ""
    started: float = field(default_factory=time.monotonic)
    first_at: float | None = None

    def first(self) -> None:
        """Mark the first token, reasoning or tool call (later calls are no-ops)."""
        if self.first_at is None:
            self.first_at = time.monotonic()

    def done(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        cached_input_tokens: int = 0,
        outcome: str = "ok",
        timings: dict[str, float] | None = None,
    ) -> None:
        ended = time.monotonic()
        first = self.first_at if self.first_at is not None else ended
        log.info(
            "llm call session=%s kind=%s vendor=%s model=%s ttft=%.2fs decode=%.2fs "
            "total=%.2fs in=%d cached=%d out=%d reasoning=%d outcome=%s%s%s",
            self.session_id,
            self.kind,
            self.vendor,
            self.model,
            first - self.started,
            ended - first,
            ended - self.started,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            reasoning_tokens,
            outcome,
            "" if self.first_at is not None else " (no output)",
            server_timings(timings, first - self.started),
        )


def server_timings(timings: dict[str, float] | None, ttft: float) -> str:
    """`` server: prefill=…s cache_n=… wait=…s`` from llama.cpp timings, or ``""``.

    ``wait`` is the first-token time not spent on prefill: a queue for a slot.
    """
    if not timings:
        return ""
    prompt_ms = float(timings.get("prompt_ms") or 0.0)
    parts = [
        f"prefill={prompt_ms / 1000:.2f}s",
        f"prompt_n={int(timings.get('prompt_n') or 0)}",
        f"cache_n={int(timings.get('cache_n') or 0)}",
        f"decode={float(timings.get('predicted_ms') or 0.0) / 1000:.2f}s",
        f"wait={max(0.0, ttft - prompt_ms / 1000):.2f}s",
    ]
    return " server: " + " ".join(parts)


__all__ = ["CallTiming", "server_timings"]
