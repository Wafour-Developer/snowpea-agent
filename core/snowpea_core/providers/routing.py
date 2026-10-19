"""Which session a provider request belongs to, for a self-hosted router.

``llm.snowpea.ai`` puts a vllm-router in front of several vLLM workers and
picks the worker by hashing ``X-Session-ID``.  A request without it lands on
whichever worker the router chooses, so consecutive rounds of one turn rarely
find their ~100k-token prefix in that worker's cache: every round re-ran the
whole prefill (20-60 s) instead of the new tail (about 2 s).

The loop sets the session for the duration of one model call; an
OpenAI-compatible adapter for a local-style vendor reads it when it opens the
HTTP client.  Hosted vendors never get the header — the session id is nothing
they need.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

#: The header vllm-router's ``consistent_hash`` policy keys on.
SESSION_HEADER = "X-Session-ID"

_SESSION: ContextVar[str | None] = ContextVar("snowpea_provider_session", default=None)


@contextmanager
def for_session(session_id: str | None) -> Iterator[None]:
    """Mark every provider request made inside the block as ``session_id``'s."""
    token = _SESSION.set(session_id or None)
    try:
        yield
    finally:
        _SESSION.reset(token)


def current_session() -> str | None:
    return _SESSION.get()


def session_headers() -> dict[str, str]:
    """``{X-Session-ID: <id>}`` inside :func:`for_session`, else ``{}``."""
    session_id = _SESSION.get()
    return {SESSION_HEADER: session_id} if session_id else {}


__all__ = ["SESSION_HEADER", "current_session", "for_session", "session_headers"]
