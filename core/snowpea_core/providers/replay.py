"""Fixture replay/record for the provider adapters (M3 contract §4).

``SNOWPEA_PROVIDER_MODE`` selects the mode:

``live`` (default)
    Adapters talk to the vendor.
``replay``
    The **HTTP layer only** is swapped for a transport that returns the recorded
    chunks of ``tests/fixtures/providers/<vendor>/<case>.json`` in request order
    (not by content).  Everything above it — request building, SSE parsing,
    normalisation — is the real code, which is the point: the matrix test
    exercises the adapters, not a stub.
``record``
    Maintainers only.  The real transport is wrapped, the exchange is written to
    the fixture and secrets are scrubbed on the way out.

Fixture shape::

    {"vendor": "openai", "model": "gpt-4.1", "wire": "openai", "synthetic": true,
     "exchanges": [{"request": {...scrubbed...}, "response_stream": [ ...chunks... ]}]}

``response_stream`` holds decoded chunks, not raw bytes, so the files stay
reviewable; :func:`sse_bytes` turns them back into the wire format.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

from snowpea_core.providers.base import ProviderError

MODE_ENV = "SNOWPEA_PROVIDER_MODE"
FIXTURE_ENV = "SNOWPEA_PROVIDER_FIXTURES"

#: Header names never written to a fixture.
SCRUBBED_HEADERS = frozenset(
    {"authorization", "x-api-key", "cookie", "set-cookie", "api-key", "x-goog-api-key"}
)
#: Body keys never written to a fixture.
SCRUBBED_KEYS = frozenset({"api_key", "apikey", "organization", "org_id", "account_id", "key"})
REDACTED = "***"

DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "providers"


def mode() -> str:
    """``live`` | ``replay`` | ``record``."""
    return (os.environ.get(MODE_ENV) or "live").strip().lower() or "live"


def is_replay() -> bool:
    return mode() == "replay"


def is_record() -> bool:
    return mode() == "record"


def fixture_root() -> Path:
    override = os.environ.get(FIXTURE_ENV)
    return Path(override).expanduser() if override else DEFAULT_FIXTURE_ROOT


def fixture_path(vendor: str, case: str) -> Path:
    return fixture_root() / vendor / f"{case}.json"


# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------


def scrub(value: Any) -> Any:
    """Recursively redact credential-shaped keys and bearer tokens."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SCRUBBED_HEADERS or str(key).lower() in SCRUBBED_KEYS:
                out[key] = REDACTED
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("bearer "):
        return REDACTED
    return value


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def sse_bytes(chunks: list[Any], wire: str) -> bytes:
    """Recorded chunks back to the vendor's SSE wire format."""
    lines: list[str] = []
    for chunk in chunks:
        if wire == "anthropic":
            if not isinstance(chunk, dict):  # pragma: no cover - malformed fixture
                continue
            event = str(chunk.get("event") or chunk.get("data", {}).get("type") or "message")
            payload = chunk.get("data", chunk)
            lines.append(f"event: {event}\ndata: {json.dumps(payload)}\n\n")
            continue
        if isinstance(chunk, str):
            lines.append(f"data: {chunk}\n\n")
            continue
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    if wire == "openai":
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


@dataclass
class Tape:
    """One loaded fixture, consumed one exchange per HTTP request."""

    vendor: str
    model: str
    wire: str
    exchanges: list[dict[str, Any]]
    synthetic: bool = True
    path: Path | None = None
    _cursor: int = 0
    #: Requests seen while replaying, for assertions in tests.
    seen: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, vendor: str, case: str = "tool_call_once") -> Tape:
        path = fixture_path(vendor, case)
        if not path.is_file():
            raise ProviderError("not_found", f"provider fixture not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            vendor=str(raw.get("vendor", vendor)),
            model=str(raw.get("model", "")),
            wire=str(raw.get("wire", "openai")),
            exchanges=list(raw.get("exchanges") or []),
            synthetic=bool(raw.get("synthetic", True)),
            path=path,
        )

    def next_response(self) -> bytes:
        """The SSE body of the next exchange (matched by order, not content)."""
        if self._cursor >= len(self.exchanges):
            raise ProviderError(
                "not_found",
                f"replay tape for {self.vendor} has only {len(self.exchanges)} exchange(s); "
                f"request {self._cursor + 1} has nothing recorded",
            )
        exchange = self.exchanges[self._cursor]
        self._cursor += 1
        return sse_bytes(list(exchange.get("response_stream") or []), self.wire)

    @property
    def remaining(self) -> int:
        return len(self.exchanges) - self._cursor

    def transport(self, http: ModuleType = httpx) -> Any:
        """A replay transport for ``http`` (``httpx`` or the SDK's ``httpx2``)."""
        return replay_transport_class(http)(self, http)

    def save(self) -> None:
        """Write the tape back to its fixture file (record mode)."""
        if self.path is None:  # pragma: no cover - guarded by callers
            raise ProviderError("invalid_params", "this tape has no fixture path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "vendor": self.vendor,
            "model": self.model,
            "wire": self.wire,
            "synthetic": self.synthetic,
            "exchanges": self.exchanges,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class _ReplayLogic:
    """Answers every request from the tape, in order."""

    def __init__(self, tape: Tape, http: ModuleType = httpx) -> None:
        self._tape = tape
        self._http = http

    async def handle_async_request(self, request: Any) -> Any:
        body: Any = None
        raw = request.content
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode("utf-8", "replace")
        self._tape.seen.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": scrub(dict(request.headers)),
                "body": scrub(body),
            }
        )
        return self._http.Response(
            200,
            content=self._tape.next_response(),
            headers={"content-type": "text/event-stream; charset=utf-8"},
            request=request,
        )


class _RecordLogic:
    """Wraps a live transport and appends each exchange to ``tape``."""

    def __init__(self, tape: Tape, http: ModuleType = httpx, inner: Any = None) -> None:
        self._tape = tape
        self._http = http
        self._inner = inner or http.AsyncHTTPTransport()

    async def handle_async_request(self, request: Any) -> Any:
        response = await self._inner.handle_async_request(request)
        payload = await response.aread()
        await response.aclose()
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:  # pragma: no cover - non-JSON request
            body = None
        self._tape.exchanges.append(
            {
                "request": scrub(
                    {
                        "method": request.method,
                        "url": str(request.url),
                        "headers": dict(request.headers),
                        "body": body,
                    }
                ),
                "response_stream": [scrub(chunk) for chunk in decode_sse(payload, self._tape.wire)],
            }
        )
        self._tape.synthetic = False
        self._tape.save()
        return self._http.Response(
            response.status_code,
            content=payload,
            headers=response.headers,
            request=request,
        )


@lru_cache(maxsize=8)
def replay_transport_class(http: ModuleType) -> type:
    """``_ReplayLogic`` bound to ``http``'s transport base class."""
    return type("ReplayTransport", (_ReplayLogic, http.AsyncBaseTransport), {})


@lru_cache(maxsize=8)
def record_transport_class(http: ModuleType) -> type:
    """``_RecordLogic`` bound to ``http``'s transport base class."""
    return type("RecordingTransport", (_RecordLogic, http.AsyncBaseTransport), {})


ReplayTransport = replay_transport_class(httpx)
RecordingTransport = record_transport_class(httpx)


def decode_sse(payload: bytes, wire: str) -> list[Any]:
    """Raw SSE bytes back to the decoded chunk list stored in fixtures."""
    chunks: list[Any] = []
    for block in payload.decode("utf-8", "replace").split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        data = "\n".join(data_lines)
        if not data or data == "[DONE]":
            continue
        try:
            decoded: Any = json.loads(data)
        except ValueError:
            decoded = data
        chunks.append({"event": event, "data": decoded} if wire == "anthropic" else decoded)
    return chunks


# ---------------------------------------------------------------------------
# the active tape
# ---------------------------------------------------------------------------

_ACTIVE: ContextVar[Tape | None] = ContextVar("snowpea_replay_tape", default=None)


def active_tape() -> Tape | None:
    """The tape adapters should use, if one is installed."""
    return _ACTIVE.get()


@contextmanager
def use_tape(tape: Tape) -> Iterator[Tape]:
    """Install ``tape`` for the duration of the block."""
    token = _ACTIVE.set(tape)
    try:
        yield tape
    finally:
        _ACTIVE.reset(token)


@contextmanager
def replaying(vendor: str, case: str = "tool_call_once") -> Iterator[Tape]:
    """Load ``<vendor>/<case>.json`` and install it as the active tape."""
    with use_tape(Tape.load(vendor, case)) as tape:
        yield tape


def transport_for(vendor: str, http: ModuleType = httpx) -> Any:
    """The transport an adapter should pass to ``httpx.AsyncClient``.

    ``None`` means "use the network".  In replay mode a tape must be installed;
    adapters call this once per client so the whole HTTP layer is swapped.
    """
    tape = active_tape()
    if tape is None:
        if is_replay():
            raise ProviderError(
                "invalid_params",
                f"{MODE_ENV}=replay but no fixture is loaded for {vendor}; "
                "wrap the call in providers.replay.replaying(vendor)",
            )
        return None
    if is_record():
        return record_transport_class(http)(tape, http)
    return tape.transport(http)


__all__ = [
    "DEFAULT_FIXTURE_ROOT",
    "MODE_ENV",
    "REDACTED",
    "SCRUBBED_HEADERS",
    "SCRUBBED_KEYS",
    "RecordingTransport",
    "ReplayTransport",
    "record_transport_class",
    "replay_transport_class",
    "Tape",
    "active_tape",
    "decode_sse",
    "fixture_path",
    "fixture_root",
    "is_record",
    "is_replay",
    "mode",
    "replaying",
    "scrub",
    "sse_bytes",
    "transport_for",
    "use_tape",
]
