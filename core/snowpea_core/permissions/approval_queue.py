"""Human-in-the-loop approvals (contract §7).

An interactive turn asks its *origin* connection and nobody else: the surface
that started the session is the one holding a human.  An unattended turn (a
scheduled job, a gateway message) has no origin to ask, so its request sits in
the queue until some authenticated client answers with ``approval.respond`` or
``approvals.timeoutSec`` expires and it is denied.

Every resolution, including timeouts, is appended to
``$SNOWPEA_HOME/logs/approvals.jsonl``.

Answering with a scope wider than ``once`` remembers the decision: ``session``
caches it for this session's ``(tool, first command token)``, while ``project``
and ``always`` also write an allowlist entry (contract §7) into the project or
global settings so it survives a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from snowpea_core.config.paths import Paths, utc_now
from snowpea_core.config.settings import Settings
from snowpea_core.permissions.allowlist import (
    SHELL_TARGET,
    Allowlist,
    command_of,
    first_token,
    pattern_for_command,
    pattern_for_tool,
    tool_target,
)
from snowpea_core.permissions.allowlist import (
    Scope as AllowlistScope,
)
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import ApprovalRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.session.manager import EventHub
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.approvals")

#: Alias so the annotations below still mean the builtin ``list`` even though
#: :class:`ApprovalQueue` defines a method called ``list``.
ApprovalRequests = list[ApprovalRequest]

#: Scopes that cache an "allow" for the rest of the session.
CACHING_SCOPES: frozenset[str] = frozenset({"session", "project", "always"})

#: Scopes that also persist an allowlist entry, and the store each one uses.
PERSISTING_SCOPES: dict[str, str] = {"project": "project", "always": "global"}

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
    #: Session workdir, kept so a ``project`` answer knows where to write.
    workdir: Any = None
    #: False for ``config`` calls: a wide answer must not silence the next one.
    cacheable: bool = True



class ApprovalQueue:
    """Pending approvals plus the audit log."""

    def __init__(
        self,
        settings: Settings | None = None,
        paths: Paths | None = None,
        hub: EventHub | None = None,
        allowlist: Allowlist | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.paths = paths
        self.hub = hub
        self.allowlist = allowlist
        self._pending: dict[str, _Pending] = {}
        self._cache: set[tuple[str, str, str]] = set()

    def bind(
        self,
        settings: Settings,
        paths: Paths,
        hub: EventHub,
        allowlist: Allowlist | None = None,
    ) -> None:
        """Late wiring from ``app_server`` once ``Core`` exists."""
        self.settings = settings
        self.paths = paths
        self.hub = hub
        if allowlist is not None:
            self.allowlist = allowlist

    # -- queries -------------------------------------------------------
    def list(self, session_id: str | None = None, conn: Any = None) -> ApprovalRequests:
        """The shared queue: unattended requests, plus ``conn``'s own.

        An interactive request belongs to the surface that started the session
        and is asked over ``approval.request``; it never becomes visible to a
        second client (contract §4, AC-20).  Passing the asking connection back
        in is what lets that surface still see its own pending request.
        """
        return [
            entry.request
            for entry in self._pending.values()
            if (entry.unattended or (conn is not None and entry.origin_conn is conn))
            and (session_id is None or entry.request.sessionId == session_id)
        ]

    def unattended(self, session_id: str | None = None) -> ApprovalRequests:
        """Pending requests with no interactive surface to answer them."""
        return [
            entry.request
            for entry in self._pending.values()
            if entry.unattended and (session_id is None or entry.request.sessionId == session_id)
        ]

    def get(self, request_id: str) -> ApprovalRequest | None:
        entry = self._pending.get(request_id)
        return entry.request if entry else None

    @staticmethod
    def cache_key(
        session_id: str, tool: str, args: dict[str, Any] | None = None
    ) -> tuple[str, str, str]:
        """``(session, tool, first command token)``; the token is "" off-shell."""
        command = command_of(args or {})
        return (session_id, tool, first_token(command) if command else "")

    def cached(self, session_id: str, tool: str, args: dict[str, Any] | None = None) -> bool:
        """True when this session already approved this exact call shape."""
        return self.cache_key(session_id, tool, args) in self._cache

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
        note: str = "",
        cacheable: bool = True,
    ) -> Decision:
        """Ask for permission to run ``tool``; block until answered or denied.

        ``note`` is the extra warning the surface shows with the prompt.
        ``cacheable`` is false for ``config`` calls, so answering "allow for the
        session" on one settings write does not silently cover the next one.
        """
        if cacheable and self.cached(session.id, tool, args):
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
            note=note,
        )
        loop = asyncio.get_running_loop()
        origin = None if unattended else getattr(session, "origin_conn", None)
        entry = _Pending(
            request=request,
            future=loop.create_future(),
            unattended=unattended,
            origin_conn=origin,
            workdir=getattr(session, "workdir", None),
            cacheable=cacheable,
        )
        self._pending[request.requestId] = entry
        if origin is not None:
            entry.task = asyncio.ensure_future(self._ask_origin(entry))
        else:
            await self._broadcast_pending(request)
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
        await self._resolve(
            request,
            decision,
            notify_exclude=origin,
            workdir=entry.workdir,
            unattended=entry.unattended,
            cacheable=entry.cacheable,
        )
        return decision

    async def _broadcast_pending(self, request: ApprovalRequest) -> None:
        """Announce an unattended request to every authenticated surface.

        The gateway listens on the same hub, so a binding whose session raised
        the request also pushes it to the bound conversation with allow/deny
        buttons — one notification, two deliveries (contract §4).
        """
        if self.hub is None:
            return
        await self.hub.notify(
            "approval.pending", {"request": request.model_dump(mode="json")}
        )

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
        self,
        request: ApprovalRequest,
        decision: Decision,
        *,
        notify_exclude: Any = None,
        workdir: Any = None,
        unattended: bool = False,
        cacheable: bool = True,
    ) -> None:
        """Cache, persist, log and announce a finished request."""
        if cacheable and decision.allowed and decision.scope in CACHING_SCOPES:
            self._cache.add(self.cache_key(request.sessionId, request.tool, request.args))
        if cacheable and decision.allowed and decision.scope in PERSISTING_SCOPES:
            self._persist(request, decision.scope, workdir)
        self._append_log(request, decision, unattended=unattended)
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

    def _persist(self, request: ApprovalRequest, scope: str, workdir: Any) -> None:
        """Turn a ``project``/``always`` answer into an allowlist entry."""
        if self.allowlist is None:
            return
        store = cast(AllowlistScope, PERSISTING_SCOPES[scope])
        if store == "project" and workdir is None:
            log.warning("cannot store a project allowlist entry without a workdir")
            return
        command = command_of(request.args)
        if command:
            pattern, target = pattern_for_command(command), SHELL_TARGET
        else:
            pattern, target = pattern_for_tool(request.tool), tool_target(request.tool)
        try:
            self.allowlist.add(pattern, store, target, workdir=workdir)
        except (OSError, ValueError):  # pragma: no cover - a bad store never breaks a turn
            log.warning("could not store allowlist entry %r", pattern, exc_info=True)

    def _append_log(
        self, request: ApprovalRequest, decision: Decision, *, unattended: bool = False
    ) -> None:
        if self.paths is None:
            return
        record = {
            "ts": utc_now(),
            "requestId": request.requestId,
            "sessionId": request.sessionId,
            "tool": request.tool,
            "args": request.args,
            "risk": request.risk,
            "decision": decision.decision,
            "scope": decision.scope,
            "by": decision.by,
            "code": decision.code,
            "unattended": unattended,
        }
        try:
            self.paths.ensure()
            with self.paths.approvals_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:  # pragma: no cover - audit log must never break a turn
            log.warning("could not append to %s", self.paths.approvals_log, exc_info=True)


__all__ = [
    "CACHING_SCOPES",
    "ApprovalRequests",
    "GRACE_SECONDS",
    "PERSISTING_SCOPES",
    "ApprovalQueue",
    "Decision",
]
