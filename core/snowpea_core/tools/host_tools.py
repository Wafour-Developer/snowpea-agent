"""Host tools: tools a connected client registers and runs itself (protocol 1.6.0).

A client such as snowpea-browser executes page actions in its own process.  It
registers them with ``tool.register``; the agent loop then calls them like any
other tool, and the call travels back to that client as a server->client
``tool.invoke`` request, the way ``approval.request`` already does.

Rules (docs/design/m-browser-host-tools.md):

* **Ownership.** A host tool belongs to the connection that registered it and is
  dropped when that connection closes; a client re-registers after
  ``system.hello``.
* **Scope.** A session sees the host tools of exactly one connection: the one
  named by ``session.create {hostToolsFrom}``, else the session's origin
  connection.  Subagents inherit their parent's.
* **Names.** Plain tool names (``[A-Za-z][A-Za-z0-9_-]{0,63}``).  A name may not
  collide with a daemon tool, except the built-in ``browser_*`` tools, which a
  host tool of the same name *shadows* in the sessions that see it — that is how
  a browser host replaces the Playwright tools.  Names are per connection, so
  two hosts may both register ``browser_navigate``.
* **Permissions.** A host tool carries an ordinary permission tag, and its calls
  go through the same mode matrix, allowlist and hooks as any other tool.
* **Failure.** A timeout (default 120 s, ``timeoutMs`` per tool) or a closed
  connection is a tool error, never a hang.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.tools.registry import ProgressEmitter, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from snowpea_core.server.app_server import Core

log = logging.getLogger(__name__)

#: Host results whose content blocks ``session.toolContent`` can still return.
CONTENT_CACHE_SIZE = 200

#: Default wait for a ``tool.invoke`` answer.
DEFAULT_TIMEOUT_MS = 120_000

#: Permission tags a host tool may carry (the protocol's ``PermissionTag``).
PERMISSION_TAGS = frozenset(
    {"read", "write", "exec", "network", "send", "config", "delegate", "secret"}
)

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

#: Built-in tools a host tool may shadow.
SHADOWABLE_CATEGORY = "browser"


class HostToolError(ValueError):
    """A registration the daemon refuses; the message names the tool."""


@dataclass
class _Host:
    """One connection's registered tools."""

    conn: Any
    tools: dict[str, Tool] = field(default_factory=dict)


@dataclass
class _InFlight:
    session_id: str
    name: str
    progress: ProgressEmitter
    #: Monotonic time of the host's last sign of life for this call: the
    #: invoke itself, then every ``tool.progress`` (addendum 15).
    last_seen: float = 0.0


class HostTools:
    """Every connection's host tools, keyed by the connection's surface id."""

    def __init__(self) -> None:
        self._hosts: dict[str, _Host] = {}
        #: ``callId -> call`` for ``tool.progress`` routing.
        self._inflight: dict[str, _InFlight] = {}
        #: ``(session, callId) -> content blocks`` of recent host results, for
        #: ``session.toolContent``; images are kept here, not in stored events.
        self._content: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
        #: ``tool.cancel`` sends still in flight, held so they are not collected.
        self._cancels: set[asyncio.Task[None]] = set()

    # -- registration --------------------------------------------------
    def register(self, core: Core, conn: Any, specs: list[dict[str, Any]]) -> list[str]:
        """Add or replace ``conn``'s tools; returns the registered names.

        All-or-nothing: one bad spec refuses the whole call.
        """
        built: list[Tool] = []
        seen: set[str] = set()
        for spec in specs:
            name = str(spec.get("name") or "")
            if not NAME_RE.match(name):
                raise HostToolError(f"invalid tool name {name!r}")
            if name in seen:
                raise HostToolError(f"{name} is listed twice")
            seen.add(name)
            builtin = core.tools.get(name)
            if builtin is not None and builtin.category != SHADOWABLE_CATEGORY:
                raise HostToolError(
                    f"{name} is already a daemon tool; only browser_* tools may be shadowed"
                )
            permission = str(spec.get("permission") or "")
            if permission not in PERMISSION_TAGS:
                raise HostToolError(f"{name}: unknown permission tag {permission!r}")
            schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                raise HostToolError(f"{name}: inputSchema must be an object")
            timeout_ms = spec.get("timeoutMs")
            timeout = (
                float(timeout_ms) / 1000.0
                if isinstance(timeout_ms, int | float) and timeout_ms > 0
                else DEFAULT_TIMEOUT_MS / 1000.0
            )
            built.append(
                Tool(
                    name=name,
                    category=str(spec.get("category") or "host"),
                    description=str(spec.get("description") or name),
                    input_schema=dict(schema),
                    permission=permission,  # type: ignore[arg-type]
                    run=self._runner(core, conn.surface_id, name, timeout),
                    source=f"host:{conn.surface_id}",
                )
            )
        host = self._hosts.get(conn.surface_id)
        if host is None:
            host = self._hosts[conn.surface_id] = _Host(conn=conn)
            on_close = getattr(conn, "on_close", None)
            if isinstance(on_close, list):
                on_close.append(self.drop_connection)
        for tool in built:
            host.tools[tool.name] = tool
        _invalidate_tool_prompt()
        return [tool.name for tool in built]

    def unregister(self, conn: Any, names: list[str]) -> list[str]:
        """Drop some of ``conn``'s tools; returns the names actually removed."""
        host = self._hosts.get(conn.surface_id)
        if host is None:
            return []
        removed = [name for name in names if host.tools.pop(name, None) is not None]
        if removed:
            _invalidate_tool_prompt()
        return removed

    def drop_connection(self, conn: Any) -> None:
        """Forget every tool ``conn`` registered (its socket closed)."""
        if self._hosts.pop(getattr(conn, "surface_id", ""), None) is not None:
            _invalidate_tool_prompt()

    # -- lookup --------------------------------------------------------
    def owner_of(self, session: Any) -> str | None:
        """The surface id whose host tools ``session`` sees, if any."""
        if session is None:
            return None
        explicit = getattr(session, "host_tools_from", None)
        if explicit:
            explicit = str(explicit)
            if explicit in self._hosts:
                return explicit
            # A clientId: whichever live connection that client is on now; then
            # a clientKind ("browser"): the first such host with tools.
            for field_name in ("client_id", "client_kind"):
                for surface_id, host in self._hosts.items():
                    if host.tools and getattr(host.conn, field_name, None) == explicit:
                        return surface_id
            # The named host is gone (a browser relaunched under a new
            # clientId): a live browser that is the session's origin, and
            # provides host tools, takes over (addendum 7).
            fallback = self._live_browser_origin(session)
            return fallback if fallback is not None else explicit
        conn = getattr(session, "origin_conn", None)
        return getattr(conn, "surface_id", None) if conn is not None else None

    def _live_browser_origin(self, session: Any) -> str | None:
        conn = getattr(session, "origin_conn", None)
        if conn is None or getattr(conn, "closed", False):
            return None
        if getattr(conn, "client_kind", None) != "browser":
            return None
        surface_id = getattr(conn, "surface_id", None)
        host = self._hosts.get(surface_id) if surface_id else None
        return surface_id if host is not None and host.tools else None

    def live_browser(self, client_id: str, candidates: list[Any]) -> Any:
        """The open ``browser``-kind connection whose clientId (or surface id) is ``client_id``."""
        pool = [*candidates, *(host.conn for host in self._hosts.values())]
        for conn in pool:
            if conn is None or getattr(conn, "closed", False):
                continue
            if getattr(conn, "client_kind", None) != "browser":
                continue
            if client_id in (getattr(conn, "client_id", None), getattr(conn, "surface_id", None)):
                return conn
        return None

    def for_session(self, session: Any) -> dict[str, Tool]:
        """``name -> Tool`` for the host tools ``session`` may call."""
        owner = self.owner_of(session)
        host = self._hosts.get(owner) if owner else None
        return dict(host.tools) if host is not None else {}

    def names_for(self, session: Any) -> list[str]:
        return list(self.for_session(session))

    def connection(self, surface_id: str) -> Any:
        host = self._hosts.get(surface_id)
        return host.conn if host is not None else None

    # -- calls ---------------------------------------------------------
    def _runner(self, core: Core, surface_id: str, name: str, timeout: float) -> Any:
        async def run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            return await self.invoke(core, surface_id, name, timeout, ctx, args)

        return run

    async def invoke(
        self,
        core: Core,
        surface_id: str,
        name: str,
        timeout: float,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        """Send ``tool.invoke`` to the owning connection and map its answer."""
        conn = self.connection(surface_id)
        if conn is None or getattr(conn, "closed", False):
            return ToolResult(ok=False, error=f"{name}: the host that provides it disconnected")
        session = ctx.session
        call_id = ctx.call_id or f"host-{id(ctx)}"
        progress = ctx.progress or ProgressEmitter(core, session.id, call_id, name)
        self._inflight[call_id] = _InFlight(
            session.id, name, progress, asyncio.get_running_loop().time()
        )
        params = {
            "sessionId": session.id,
            "turnId": getattr(session, "current_turn", None) or "",
            "callId": call_id,
            "name": name,
            "args": args,
            # A code-running host tool (a REPL) registered as ``read`` enforces
            # the mode itself: no page mutations in plan (addendum 3 §S).
            "mode": getattr(session, "mode", "accept"),
            "workspaceDir": str(
                getattr(session, "workspace_dir", None) or getattr(session, "workdir", "") or ""
            ),
            # The session's project folder itself (Projects, 1.7.0).
            "workdir": str(Path(getattr(session, "workdir", "") or ".").resolve()),
            "parentSessionId": getattr(session, "parent_session_id", None),
        }
        try:
            answer = await self._call_or_cancel(
                conn, session, params, timeout, waiting=_waiting_on_person(core, session.id)
            )
            if answer is None:
                return ToolResult(ok=False, error=f"{name}: interrupted")
        except TimeoutError:
            return ToolResult(
                ok=False, error=f"{name}: the host did not answer within {timeout:g}s"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead host is a tool error
            return ToolResult(ok=False, error=f"{name}: {exc}")
        finally:
            self._inflight.pop(call_id, None)
        return result_from_answer(name, answer)

    async def _call_or_cancel(
        self,
        conn: Any,
        session: Any,
        params: dict[str, Any],
        timeout: float,
        waiting: Callable[[], bool] | None = None,
    ) -> dict[str, Any] | None:
        """The host's answer, or ``None`` once the session is interrupted.

        On an interrupt the host is told with a ``tool.cancel`` notification
        and the turn moves on without waiting for it (addendum 2 §L).

        The deadline is ``timeout`` of silence, not of wall time (addendum 15):
        a ``tool.progress`` from the host restarts it, and it does not run at
        all while the session waits on a person (an ``approval.ask`` the host
        raised for this call, or a question). When it does run out, the host
        gets ``tool.cancel`` too, so it frees its REPL, and ``TimeoutError``
        is raised.
        """
        loop = asyncio.get_running_loop()
        interrupt = getattr(session, "interrupt", None)
        call = asyncio.ensure_future(conn.call("tool.invoke", params, timeout=None))
        watcher = asyncio.ensure_future(interrupt.wait()) if interrupt is not None else None
        waiters = {call, watcher} if watcher is not None else {call}
        record = self._inflight.get(str(params.get("callId")))
        started = loop.time()
        try:
            while True:
                seen = record.last_seen if record is not None else started
                if waiting is not None and waiting():
                    if record is not None:
                        record.last_seen = loop.time()
                    seen = loop.time()
                remaining = seen + timeout - loop.time()
                if remaining <= 0:
                    call.cancel()
                    self._send_cancel(conn, params)
                    raise TimeoutError
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=min(remaining, DEADLINE_POLL_SEC),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
        except asyncio.CancelledError:
            # The loop cancels a running tool on an interrupt: still tell the host.
            call.cancel()
            self._send_cancel(conn, params)
            raise
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()
        if call.done():
            return call.result()
        call.cancel()
        self._send_cancel(conn, params)
        return None

    def _send_cancel(self, conn: Any, params: dict[str, Any]) -> None:
        """Fire ``tool.cancel`` without waiting on it (addendum 2 §L)."""

        async def send() -> None:
            try:
                await conn.notify(
                    "tool.cancel", {"sessionId": params["sessionId"], "callId": params["callId"]}
                )
            except Exception:  # noqa: BLE001 - a dead host needs no cancel
                log.debug("could not send tool.cancel", exc_info=True)

        task = asyncio.ensure_future(send())
        self._cancels.add(task)
        task.add_done_callback(self._cancels.discard)

    def keep_content(
        self, session_id: str, call_id: str, blocks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Remember a result's blocks; return them as a tool.result event carries them."""
        self._content[(session_id, call_id)] = blocks
        while len(self._content) > CONTENT_CACHE_SIZE:
            self._content.popitem(last=False)
        event_blocks: list[dict[str, Any]] = []
        for index, block in enumerate(blocks):
            if block.get("type") == "image":
                event_blocks.append(
                    {
                        "type": "image",
                        "mediaType": block.get("mediaType") or "image/png",
                        "contentRef": f"{call_id}:{index}",
                    }
                )
            elif block.get("type") == "text":
                event_blocks.append({"type": "text", "text": str(block.get("text") or "")})
        return event_blocks

    def content(self, session_id: str, call_id: str) -> list[dict[str, Any]] | None:
        return self._content.get((session_id, call_id))

    async def progress(self, conn: Any, call_id: str, message: str) -> bool:
        """Re-emit a client's ``tool.progress`` as a session event.

        Only the connection that owns the call may report on it.
        """
        call = self._inflight.get(call_id)
        if call is None:
            return False
        owner = self._hosts.get(getattr(conn, "surface_id", ""))
        if owner is None or call.name not in owner.tools:
            return False
        # Any progress, an empty keepalive included, restarts the deadline.
        call.last_seen = asyncio.get_running_loop().time()
        if message:
            await call.progress.emit("stdout", message)
        return True


#: How often a waiting host call re-checks its deadline (seconds).
DEADLINE_POLL_SEC = 1.0


def _waiting_on_person(core: Any, session_id: str) -> Callable[[], bool]:
    """True while ``session_id`` has an approval or a question pending."""

    def check() -> bool:
        for queue_name in ("approvals", "questions"):
            queue = getattr(core, queue_name, None)
            count = getattr(queue, "count", None)
            if callable(count):
                try:
                    if count(session_id):
                        return True
                except Exception:  # noqa: BLE001 - a broken queue never stalls a call
                    continue
        return False

    return check


#: Key under which a host result's own ``meta`` travels inside ``ToolResult.meta``.
HOST_META_KEY = "host_meta"

#: Largest host ``meta`` forwarded on a ``tool.result`` event (JSON bytes).
HOST_META_MAX_BYTES = 16 * 1024


def event_meta(meta: dict[str, Any] | None, *, tool: str = "") -> dict[str, Any] | None:
    """The host's ``meta`` for the ``tool.result`` event, or ``None`` when absent or too big.

    Surfaces use it (page preview cards: ``pages``, ``title``, ``elapsedMs``);
    the model never sees it. It is forwarded even for a sensitive result,
    whose output and content are redacted, because the host puts only
    non-secret facts there.
    """
    host_meta = (meta or {}).get(HOST_META_KEY)
    if not isinstance(host_meta, dict) or not host_meta:
        return None
    try:
        size = len(json.dumps(host_meta, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        log.warning("dropping %s result meta: not JSON", tool)
        return None
    if size > HOST_META_MAX_BYTES:
        log.warning(
            "dropping %s result meta: %d bytes exceeds %d", tool, size, HOST_META_MAX_BYTES
        )
        return None
    return host_meta


def result_from_answer(name: str, answer: dict[str, Any]) -> ToolResult:
    """A ``tool.invoke`` answer as a :class:`ToolResult`.

    ``content`` text blocks join ``output``; image blocks become the
    ``meta.images`` the loop hands to a vision model (a text-only model keeps
    just the text).  ``meta.sensitive`` passes through untouched.
    """
    output = str(answer.get("output") or "")
    meta = dict(answer["meta"]) if isinstance(answer.get("meta"), dict) else {}
    if meta:
        # The host's own facts, kept apart from what core adds below, so the
        # tool.result event can hand them to surfaces as given (addendum 13).
        meta[HOST_META_KEY] = dict(meta)
    images: list[dict[str, Any]] = []
    texts: list[str] = []
    for index, block in enumerate(answer.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif block.get("type") == "image" and block.get("data"):
            mime = str(block.get("mediaType") or "image/png")
            images.append(
                {
                    "bytes_b64": str(block["data"]),
                    "mime": mime,
                    "name": f"{name}-{index}.{mime.rsplit('/', 1)[-1]}",
                }
            )
    if texts:
        output = "\n\n".join([output, *texts]) if output else "\n\n".join(texts)
    blocks = [block for block in answer.get("content") or [] if isinstance(block, dict)]
    if blocks:
        meta["content"] = blocks
    if images:
        meta["images"] = images
        if not output:
            output = f"{len(images)} image(s) attached"
    return ToolResult(
        ok=bool(answer.get("ok")),
        output=output,
        error=(str(answer["error"]) if answer.get("error") else None),
        meta=meta or None,
    )


def _invalidate_tool_prompt() -> None:
    from snowpea_core.agent import agent as agent_mod

    agent_mod.invalidate_tools()


#: One registry per daemon process.
HOST_TOOLS = HostTools()


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "HOST_TOOLS",
    "HostToolError",
    "HostTools",
    "PERMISSION_TAGS",
    "result_from_answer",
]
