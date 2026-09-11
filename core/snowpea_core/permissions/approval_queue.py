"""Human-in-the-loop approvals (contract §7).

An interactive turn asks its *origin* connection and nobody else: the surface
that started the session is the one holding a human.  An unattended turn (a
scheduled job, a gateway message) has no origin to ask, so its request sits in
the queue until some authenticated client answers with ``approval.respond`` or
``approvals.timeoutSec`` expires and it is denied.

Every resolution, including timeouts, is appended to
``$SNOWPEA_HOME/logs/approvals.jsonl``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import ApprovalRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.manager import EventHub
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.approvals")

#: Scopes that cache an "allow" for the rest of the session.  ``project`` and
#: ``always`` are recorded but behave like ``session`` until M4 adds the
#: persistent allowlist.
CACHING_SCOPES: frozenset[str] = frozenset({"session", "project", "always"})

#: Extra seconds the outer wait gives the origin call to report its own timeout.
GRACE_SECONDS = 2.0


@dataclass
class Decision:
    """Resolution of one approval request."""

    decision: str
    scope: str = "once"
    by: str = "unknown"
    code: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


@dataclass
class _Pending:
    request: ApprovalRequest
    future: asyncio.Future[Decision]
    unattended: bool
    origin_conn: Any = None
    task: asyncio.Task[None] | None = field(default=None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ApprovalQueue:
    """Pending approvals plus the audit log."""

    def __init__(
        self,
        settings: Settings | None = None,
        paths: Paths | None = None,
        hub: EventHub | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.paths = paths
        self.hub = hub
        self._pending: dict[str, _Pending] = {}
        self._cache: set[tuple[str, str]] = set()

    def bind(self, settings: Settings, paths: Paths, hub: EventHub) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.settings = settings
        self.paths = paths
        self.hub = hub

    # -- queries -------------------------------------------------------
    def list(self, session_id: str | None = None) -> list[ApprovalRequest]:
        return [
            entry.request
            for entry in self._pending.values()
            if session_id is None or entry.request.sessionId == session_id
        ]

    def get(self, request_id: str) -> ApprovalRequest | None:
        entry = self._pending.get(request_id)
        return entry.request if entry else None

    def cached(self, session_id: str, tool: str) -> bool:
        """True when this session already approved ``tool`` for its scope."""
        return (session_id, tool) in self._cache

    @property
    def timeout_sec(self) -> int:
        return max(1, int(self.settings.approvals.timeoutSec))

    # -- the ask -------------------------------------------------------
    async def request(
        self,
        session: Session,
        tool: str,
        args: dict[str, Any],
        *,
        risk: str = "medium",
        unattended: bool = False,
        timeout_sec: int | None = None,
        scope_hint: str = "once",
        cancel_event: asyncio.Event | None = None,
    ) -> Decision:
        """Ask for permission to run ``tool``; block until answered or denied."""
        if self.cached(session.id, tool):
            return Decision("allow", "session", "cache")

        timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        request = ApprovalRequest(
            requestId=f"ap-{uuid.uuid4().hex[:12]}",
            sessionId=session.id,
            tool=tool,
            args=args,
            risk=risk,
            timeoutSec=timeout,
            scopeHint=scope_hint,  # type: ignore[arg-type]
        )
        loop = asyncio.get_running_loop()
        origin = None if unattended else getattr(session, "origin_conn", None)
        entry = _Pending(
            request=request,
            future=loop.create_future(),
            unattended=unattended,
            origin_conn=origin,
        )
        self._pending[request.requestId] = entry
        if origin is not None:
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
        outer = float(timeout) + (GRACE_SECONDS if entry.task is not None else 0.0)
        try:
            decision = await self._await_decision(entry, outer, cancel_event)
        except RpcError as exc:  # pragma: no cover - transport failure
            decision = Decision("deny", "once", "error", exc.code)
        finally:
            self._pending.pop(request.requestId, None)
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await entry.task
        await self._resolve(request, decision, notify_exclude=origin)
        return decision

    async def _await_decision(
        self, entry: _Pending, timeout: float, cancel_event: asyncio.Event | None
    ) -> Decision:
        """Wait for the answer, the timeout, or an interrupt — whichever comes first."""
        waiters: list[asyncio.Future[Any]] = [entry.future]
        watcher: asyncio.Task[bool] | None = None
        if cancel_event is not None:
            watcher = asyncio.ensure_future(cancel_event.wait())
            waiters.append(watcher)
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        if entry.future.done():
            return entry.future.result()
        if cancel_event is not None and cancel_event.is_set():
            return Decision("deny", "once", "interrupted", errors.APPROVAL_DENIED)
        return Decision("deny", "once", "timeout", errors.APPROVAL_TIMEOUT)

    async def _ask_origin(self, entry: _Pending) -> None:
        """Send ``approval.request`` to the origin connection and record the answer."""
        request = entry.request
        try:
            answer = await entry.origin_conn.call(
                "approval.request",
                request.model_dump(mode="json"),
                timeout=float(request.timeoutSec),
            )
            decision = Decision(
                decision="allow" if answer.get("decision") == "allow" else "deny",
                scope=str(answer.get("scope", "once")),
                by="origin",
            )
        except TimeoutError:
            decision = Decision("deny", "once", "timeout", errors.APPROVAL_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead surface denies, it never hangs
            log.warning("approval %s could not reach its origin: %s", request.requestId, exc)
            decision = Decision("deny", "once", "origin-unreachable", errors.APPROVAL_DENIED)
        if not entry.future.done():
            entry.future.set_result(decision)

    # -- the answer ----------------------------------------------------
    async def respond(
        self,
        request_id: str,
        decision: str,
        scope: str = "once",
        by: str = "client",
        conn: Any = None,
    ) -> None:
        """Resolve a pending request (``approval.respond``)."""
        entry = self._pending.get(request_id)
        if entry is None:
            raise RpcError(errors.NOT_FOUND, f"no pending approval {request_id}")
        if not entry.unattended and entry.origin_conn is not None and conn is not None:
            if conn is not entry.origin_conn:
                raise RpcError(
                    errors.UNAUTHORIZED,
                    "interactive approvals may only be answered by the originating surface",
                )
        if entry.future.done():
            return
        entry.future.set_result(
            Decision(
                decision="allow" if decision == "allow" else "deny",
                scope=scope,
                by=by,
            )
        )

    async def _resolve(
        self, request: ApprovalRequest, decision: Decision, *, notify_exclude: Any = None
    ) -> None:
        """Cache, log and announce a finished request."""
        if decision.allowed and decision.scope in CACHING_SCOPES:
            self._cache.add((request.sessionId, request.tool))
        self._append_log(request, decision)
        if self.hub is not None:
            await self.hub.notify(
                "approval.resolved",
                {
                    "requestId": request.requestId,
                    "decision": decision.decision,
                    "by": decision.by,
                },
                exclude=notify_exclude,
            )

    def _append_log(self, request: ApprovalRequest, decision: Decision) -> None:
        if self.paths is None:
            return
        record = {
            "ts": _utc_now(),
            "requestId": request.requestId,
            "sessionId": request.sessionId,
            "tool": request.tool,
            "args": request.args,
            "risk": request.risk,
            "decision": decision.decision,
            "scope": decision.scope,
            "by": decision.by,
            "code": decision.code,
        }
        try:
            self.paths.ensure()
            with self.paths.approvals_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:  # pragma: no cover - audit log must never break a turn
            log.warning("could not append to %s", self.paths.approvals_log, exc_info=True)


__all__ = ["CACHING_SCOPES", "GRACE_SECONDS", "ApprovalQueue", "Decision"]
