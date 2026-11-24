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
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.tools.registry import ProgressEmitter, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from snowpea_core.server.app_server import Core
    from snowpea_core.tools.tool_content_store import ToolContentStore

log = logging.getLogger(__name__)

#: Host results whose content blocks ``session.toolContent`` can still return.
CONTENT_CACHE_SIZE = 200

#: How long a closed host's tool names are remembered (or until that client
#: registers again): a call to one in that window says the host is gone
#: instead of "unknown tool", which sent the model searching for other tools
#: and delegating for a dozen rounds after a browser crash (snowpea-browser).
GONE_GRACE_SEC = 30 * 60

#: What a call to a tool of a browser host that went away gets back.
BROWSER_GONE_MESSAGE = (
    "host_unavailable: Snowpea Browser closed or crashed, so {name} can't run now. "
    "It reconnects by itself when the browser restarts. Don't search for other tools "
    "or delegate: stop and tell the user to reopen Snowpea Browser."
)

#: The same for any other host (an IDE, a plugin surface).
HOST_GONE_MESSAGE = (
    "host_unavailable: the {kind} that provided {name} disconnected, so it can't run "
    "now. Don't search for other tools or delegate: stop and tell the user to "
    "reconnect it."
)

#: Host tools whose whole result is an image (the browser host's screenshot).
IMAGE_ONLY_TOOLS: frozenset[str] = frozenset({"browser_screenshot"})


def content_store(core: Any) -> ToolContentStore | None:
    """The daemon's on-disk store of host results with images, made on first use."""
    store = getattr(core, "tool_content", None)
    if store is None:
        paths = getattr(core, "paths", None)
        if paths is None:
            return None
        from snowpea_core.tools.tool_content_store import ToolContentStore

        store = ToolContentStore(paths.tool_content_dir)
        try:
            core.tool_content = store
        except AttributeError:  # a frozen stand-in core (tests)
            pass
    return store


def _can_see(ctx: ToolContext) -> bool:
    from snowpea_core.tools.view_image import session_can_use_images

    try:
        return session_can_use_images(ctx)
    except Exception:  # noqa: BLE001 - no provider registry (tests): let it through
        return True


#: Default wait for a ``tool.invoke`` answer.
DEFAULT_TIMEOUT_MS = 120_000

#: Permission tags a host tool may carry (the protocol's ``PermissionTag``).
PERMISSION_TAGS = frozenset(
    {"read", "write", "exec", "network", "send", "config", "delegate", "secret"}
)

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

#: Built-in tools a host tool may shadow.
SHADOWABLE_CATEGORY = "browser"


#: Permission tags a host tool may pair with ``parallel: "safe"``: a tool that
#: writes, sends or runs something must keep its order in a response.
PARALLEL_SAFE_PERMISSIONS: frozenset[str] = frozenset({"read", "network"})

class HostToolError(ValueError):
    """A registration the daemon refuses; the message names the tool."""


def _description_by(raw: Any) -> dict[str, str] | None:
    """``descriptionBy`` of a spec: non-empty ``agent -> text`` pairs, or ``None``."""
    if not isinstance(raw, dict):
        return None
    pairs = {
        str(agent): text
        for agent, text in raw.items()
        if isinstance(text, str) and text.strip() and str(agent).strip()
    }
    return pairs or None


#: The built-in that serves host tools' manuals (1.55.0); a session sees it
#: while one of its host tools has a short description or sections.
TOOL_HELP = "tool_help"
MAX_SECTIONS = 50
SECTION_NAME_MAX = 64
SECTION_MAX_CHARS = 20_000


def _help_fields(
    name: str, spec: dict[str, Any]
) -> tuple[tuple[str, ...], str | None, dict[str, str] | None]:
    """``(helpers, descriptionShort, sections)`` of a spec, checked (1.55.0)."""
    raw_helpers = spec.get("helpers") or []
    if not isinstance(raw_helpers, list):
        raise HostToolError(f"{name}: helpers must be a list of names")
    helpers = tuple(dict.fromkeys(str(item).strip() for item in raw_helpers if str(item).strip()))
    short = spec.get("descriptionShort")
    short = str(short).strip() if isinstance(short, str) and short.strip() else None
    raw_sections = spec.get("sections")
    sections: dict[str, str] | None = None
    if raw_sections is not None:
        if not isinstance(raw_sections, dict):
            raise HostToolError(f"{name}: sections must be an object of name -> text")
        if len(raw_sections) > MAX_SECTIONS:
            raise HostToolError(f"{name}: at most {MAX_SECTIONS} sections")
        sections = {}
        for key, text in raw_sections.items():
            label = str(key).strip()
            if not label or len(label) > SECTION_NAME_MAX:
                raise HostToolError(f"{name}: a section name must be 1-{SECTION_NAME_MAX} chars")
            if not isinstance(text, str):
                raise HostToolError(f"{name}: section {label!r} must be text")
            sections[label] = text[:SECTION_MAX_CHARS]
        sections = sections or None
    return helpers, short, sections


def _short_description(name: str, short: str, sections: dict[str, str] | None) -> str:
    """What the model reads for a tool registered with ``descriptionShort``."""
    if sections:
        listed = ", ".join(sections)
        return (
            f"{short}\n\nMore: {TOOL_HELP} {{\"tool\": \"{name}\", \"section\": …}} "
            f"returns one part of the manual; sections: {listed}."
        )
    return f"{short}\n\nThe full manual: {TOOL_HELP} {{\"tool\": \"{name}\"}}."


def _described_for(tool: Tool, agent: str | None) -> Tool:
    """``tool`` with the description written for ``agent``, when it has one."""
    text = tool.description_by.get(agent) if tool.description_by and agent else None
    return replace(tool, description=text) if text else tool


@dataclass
class _Host:
    """One connection's registered tools."""

    conn: Any
    tools: dict[str, Tool] = field(default_factory=dict)
    #: ``tool.register {capabilities}`` (1.31.0): ``turnContext`` and the like.
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GoneHost:
    """The tools of a host whose connection closed, kept for a while."""

    names: frozenset[str]
    client_kind: str | None
    at: float

    def message(self, name: str) -> str:
        """What a call to ``name`` gets back while the host is away."""
        if self.client_kind == "browser":
            return BROWSER_GONE_MESSAGE.format(name=name)
        return HOST_GONE_MESSAGE.format(name=name, kind=self.client_kind or "host")


@dataclass
class _InFlight:
    session_id: str
    name: str
    progress: ProgressEmitter
    #: Monotonic time of the host's last sign of life for this call: the
    #: invoke itself, then every ``tool.progress`` (addendum 15).
    last_seen: float = 0.0
    #: Surface id of the connection the call was sent to: only it may report
    #: progress (security review 6, L-3 — not any host with a tool of that name).
    owner: str = ""


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
        #: Disk writes of kept content still running.
        self._writes: set[asyncio.Future[bool]] = set()
        #: Closed hosts' tools, by surface id and by clientId (``GONE_GRACE_SEC``).
        self._gone: dict[str, GoneHost] = {}
        self._clock: Callable[[], float] = time.monotonic
        #: ``listener(conn)`` after a host's tools were dropped (its socket closed);
        #: the Jarvis service ends its browser session there (1.56.0).
        self.drop_listeners: list[Callable[[Any], None]] = []

    # -- registration --------------------------------------------------
    def register(
        self,
        core: Core,
        conn: Any,
        specs: list[dict[str, Any]],
        capabilities: list[str] | None = None,
    ) -> list[str]:
        """Add or replace ``conn``'s tools; returns the registered names.

        All-or-nothing: one bad spec refuses the whole call.  ``capabilities``
        replaces the connection's declared ones; ``None`` keeps them.
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
            parallel = str(spec.get("parallel") or "never")
            if parallel not in ("safe", "never"):
                raise HostToolError(f"{name}: parallel must be 'safe' or 'never'")
            if parallel == "safe" and permission not in PARALLEL_SAFE_PERMISSIONS:
                raise HostToolError(
                    f"{name}: parallel 'safe' is for read or network tools, not {permission}"
                )
            helpers, short, sections = _help_fields(name, spec)
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
                    description=(
                        _short_description(name, short, sections)
                        if short
                        else str(spec.get("description") or name)
                    ),
                    input_schema=dict(schema),
                    permission=permission,  # type: ignore[arg-type]
                    run=self._runner(core, conn.surface_id, name, timeout),
                    source=f"host:{conn.surface_id}",
                    parallel_safe=parallel == "safe",
                    deferred_hint=bool(spec.get("deferred")),
                    look=spec.get("look") is True,
                    kind=spec["kind"] if spec.get("kind") in ("act", "read") else None,
                    agents=(
                        tuple(str(item) for item in spec["agents"])
                        if isinstance(spec.get("agents"), list) and spec["agents"]
                        else None
                    ),
                    description_by=_description_by(spec.get("descriptionBy")),
                    helpers=helpers,
                    manual=str(spec.get("description") or name) if short else None,
                    sections=sections,
                )
            )
        host = self._hosts.get(conn.surface_id)
        # The client is back: its old tools are no longer "away".
        for key in (conn.surface_id, getattr(conn, "client_id", None)):
            if key:
                self._gone.pop(key, None)
        if host is None:
            host = self._hosts[conn.surface_id] = _Host(conn=conn)
            on_close = getattr(conn, "on_close", None)
            if isinstance(on_close, list):
                on_close.append(self.drop_connection)
        for tool in built:
            host.tools[tool.name] = tool
        if capabilities is not None:
            host.capabilities = frozenset(str(item) for item in capabilities)
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
        """Forget every tool ``conn`` registered (its socket closed).

        The names are remembered for :data:`GONE_GRACE_SEC`, so a session still
        pointing at this host is told it is away (:meth:`gone_for`).
        """
        host = self._hosts.pop(getattr(conn, "surface_id", ""), None)
        if host is None:
            return
        if host.tools:
            gone = GoneHost(
                frozenset(host.tools), getattr(conn, "client_kind", None), self._clock()
            )
            for key in (getattr(conn, "surface_id", None), getattr(conn, "client_id", None)):
                if key:
                    self._gone[key] = gone
        _invalidate_tool_prompt()
        for listener in list(self.drop_listeners):
            try:
                listener(conn)
            except Exception:  # noqa: BLE001 - a listener never fails a close
                log.debug("host drop listener failed", exc_info=True)

    def gone_for(self, session: Any, name: str | None = None) -> GoneHost | None:
        """The host that served ``session`` and has gone away, if it is recent.

        With ``name``, only one whose tools included it — and never while a
        live host serves that name to the session.
        """
        if session is None:
            return None
        if name is not None and name in self.for_session(session):
            return None
        now = self._clock()
        for key in [k for k, g in self._gone.items() if now - g.at > GONE_GRACE_SEC]:
            del self._gone[key]
        explicit = getattr(session, "host_tools_from", None)
        conn = getattr(session, "origin_conn", None)
        keys = [
            str(explicit) if explicit else None,
            getattr(conn, "surface_id", None),
            getattr(conn, "client_id", None),
        ]
        candidates = [self._gone[key] for key in keys if key and key in self._gone]
        if explicit and not candidates:
            # ``hostToolsFrom`` may name a client kind ("browser").
            candidates = [g for g in self._gone.values() if g.client_kind == str(explicit)]
        for gone in sorted(candidates, key=lambda g: g.at, reverse=True):
            if name is None or name in gone.names:
                return gone
        return None

    # -- lookup --------------------------------------------------------
    def is_host(self, conn: Any) -> bool:
        """True when ``conn`` has registered host tools (it is a host, not a plain client)."""
        return getattr(conn, "surface_id", None) in self._hosts

    def browser_hosts(self) -> list[Any]:
        """The open ``browser``-kind connections that are hosts now (review 7, Low 6)."""
        return [
            host.conn for host in self._hosts.values()
            if not getattr(host.conn, "closed", False)
            and getattr(host.conn, "client_kind", None) == "browser"
        ]

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
            # a clientKind ("browser"): such a host with tools. The newest
            # wins, since a reconnect's hello can land before the old socket's
            # close has been handled.
            for field_name in ("client_id", "client_kind"):
                for surface_id, host in reversed(self._hosts.items()):
                    if getattr(host.conn, "closed", False):
                        continue
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
        if host is None:
            return {}
        if not hasattr(session, "agent"):
            # A readiness probe (the scheduler's): any tool counts.
            return dict(host.tools)
        agent = getattr(session, "agent", None)
        # An agent-scoped tool (1.24.0) stays out of other agents' sessions.
        # Visibility only: it is no boundary for a delegation naming that agent.
        visible = {
            name: _described_for(tool, agent)
            for name, tool in host.tools.items()
            if tool.agents is None or (agent is not None and agent in tool.agents)
        }
        if TOOL_HELP not in visible and any(
            getattr(tool, "sections", None) or getattr(tool, "manual", None)
            for tool in visible.values()
        ):
            # The manuals' reader comes with the tools that have one (1.55.0).
            visible[TOOL_HELP] = TOOL_HELP_TOOL
        return visible

    def helper_owner(self, session: Any, name: str) -> str | None:
        """The host tool ``name`` is a helper inside, for ``session`` (1.55.0)."""
        for tool_name, tool in self.for_session(session).items():
            if name in (getattr(tool, "helpers", None) or ()):
                return tool_name
        return None

    def names_for(self, session: Any) -> list[str]:
        return list(self.for_session(session))

    def declares(self, surface_id: str | None, capability: str) -> bool:
        """Whether the live connection ``surface_id`` declared ``capability``."""
        host = self._hosts.get(surface_id) if surface_id else None
        return (
            host is not None
            and capability in host.capabilities
            and not getattr(host.conn, "closed", False)
        )

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
        if name in IMAGE_ONLY_TOOLS and not _can_see(ctx):
            # The host cannot know which model reads the result; core does.
            # Refuse before the browser takes a picture nobody can look at.
            return ToolResult(
                ok=False,
                error=(
                    f"{name}: this model cannot see images; use browser_snapshot for the "
                    "page text, and report a canvas or WebGL page as not seen in a browser"
                ),
            )
        conn = self.connection(surface_id)
        if conn is None or getattr(conn, "closed", False):
            return ToolResult(ok=False, error=f"{name}: the host that provides it disconnected")
        session = ctx.session
        call_id = ctx.call_id or f"host-{id(ctx)}"
        progress = ctx.progress or ProgressEmitter(core, session.id, call_id, name)
        self._inflight[call_id] = _InFlight(
            session.id, name, progress, asyncio.get_running_loop().time(),
            owner=surface_id,
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
            # resultsDir, downloadsDir, scratchDir, hasProject (1.71.0).
            **_session_folders(core, session),
            "parentSessionId": getattr(session, "parent_session_id", None),
            # userTurn, origin and job: the root's running turn (1.22.0, 1.23.0).
            **root_turn_context(core, session),
            "attachments": root_attachments(core, session),
            "guard": _guard_of(core, session),
            "model": model_facts(core, session),
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
        result = result_from_answer(name, answer)
        if not result.ok:
            host = self._hosts.get(surface_id)
            tool = host.tools.get(name) if host is not None else None
            result = with_help_section(session, tool, result)
        return result

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
        self,
        session_id: str,
        call_id: str,
        blocks: list[dict[str, Any]],
        store: ToolContentStore | None = None,
    ) -> list[dict[str, Any]]:
        """Remember a result's blocks; return them as a tool.result event carries them.

        With ``store``, a result that has an image is also written to disk in
        the background, so ``session.toolContent`` can still answer once it
        has left the in-memory cache or the daemon has restarted.
        """
        self._content[(session_id, call_id)] = blocks
        while len(self._content) > CONTENT_CACHE_SIZE:
            self._content.popitem(last=False)
        if store is not None and any(block.get("type") == "image" for block in blocks):
            self._persist(store, session_id, call_id, blocks)
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

    def _persist(
        self, store: ToolContentStore, session_id: str, call_id: str, blocks: list[dict[str, Any]]
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (a sync caller): write in place
            store.save(session_id, call_id, blocks)
            return
        future = loop.run_in_executor(None, store.save, session_id, call_id, list(blocks))
        self._writes.add(future)
        future.add_done_callback(self._writes.discard)

    def content(self, session_id: str, call_id: str) -> list[dict[str, Any]] | None:
        return self._content.get((session_id, call_id))

    async def content_or_disk(
        self, session_id: str, call_id: str, store: ToolContentStore | None
    ) -> list[dict[str, Any]] | None:
        """``content``, falling back to what ``store`` kept on disk."""
        blocks = self.content(session_id, call_id)
        if blocks is not None or store is None:
            return blocks
        return await asyncio.to_thread(store.load, session_id, call_id)

    async def progress(
        self,
        conn: Any,
        call_id: str,
        message: str,
        *,
        state: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Re-emit a client's ``tool.progress`` as a session event.

        Only the connection that owns the call may report on it.  A ``state``
        (1.55.0) goes out on the event too, so a card can show "waiting".
        """
        call = self._inflight.get(call_id)
        if call is None:
            return False
        surface = str(getattr(conn, "surface_id", "") or "")
        if not surface or surface != call.owner:
            return False
        # Any progress, an empty keepalive included, restarts the deadline.
        call.last_seen = asyncio.get_running_loop().time()
        if message or state:
            await call.progress.emit("stdout", message, state=state, note=note)
        return True


#: How often a waiting host call re-checks its deadline (seconds).
DEADLINE_POLL_SEC = 1.0


def model_facts(core: Any, session: Any) -> dict[str, Any]:
    """``tool.invoke``'s ``model`` (1.55.0): what the session's model can take."""
    providers = getattr(core, "providers", None)
    vendor = str(getattr(session, "provider", None) or "")
    local = False
    can_see = False
    try:
        if providers is not None:
            vendor = vendor or str(providers.default_vendor() or "")
            local = bool(providers.is_local_style(vendor))
            from snowpea_core.tools.view_image import session_can_see_images

            can_see = bool(session_can_see_images(core, session))
    except Exception:  # noqa: BLE001 - facts for the host; never fail the call
        log.debug("could not read the model facts for tool.invoke", exc_info=True)
    window = getattr(session, "context_window", None)
    return {
        "canSee": can_see,
        "contextWindow": window if isinstance(window, int) and window > 0 else None,
        "localStyle": local,
    }


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


def snapshot_tab(meta: dict[str, Any] | None) -> str | None:
    """The tab a host result is a snapshot of, or ``None`` (addendum 23).

    The host says so in its own meta: ``kind: "snapshot"`` and ``tab``.  Any
    other kind (``extract``, ``action``, ``other``) or no kind at all leaves
    the result to the ordinary round-based pruning.
    """
    host_meta = (meta or {}).get(HOST_META_KEY)
    if not isinstance(host_meta, dict) or host_meta.get("kind") != "snapshot":
        return None
    tab = host_meta.get("tab")
    if isinstance(tab, bool) or not isinstance(tab, (str, int)):
        return None
    text = str(tab).strip()
    return text[:200] or None


def root_session(core: Any, session: Any) -> Any | None:
    """The conversation ``session`` was delegated from, or ``None`` if lost.

    A scheduled run is a root of its own: the chat that scheduled it is not
    the conversation its turns belong to.
    """
    seen: set[str] = set()
    current = session
    sessions = getattr(core, "sessions", None)
    while (
        getattr(current, "parent_session_id", None)
        and getattr(current, "kind", None) != "scheduled"
        and sessions is not None
    ):
        parent_id = str(current.parent_session_id)
        if parent_id in seen:
            return None
        seen.add(parent_id)
        parent = sessions.get(parent_id)
        if parent is None:
            return None
        current = parent
    return current


def root_turn_context(core: Any, session: Any) -> dict[str, Any]:
    """``userTurn``, ``origin`` and ``job`` for a ``tool.invoke`` (1.22.0, 1.23.0).

    All three come from the root conversation's running turn, so a delegated
    child reports what its root does, never what its parent agent wrote.
    ``job`` is set only for a turn a schedule started.
    """
    root = root_session(core, session)
    if root is None:
        return {"userTurn": None, "origin": None, "job": None}
    turn_id = getattr(root, "current_turn", None)
    origin = (getattr(root, "turn_origins", None) or {}).get(turn_id) if turn_id else None
    job = (getattr(root, "turn_jobs", None) or {}).get(turn_id) if origin == "schedule" else None
    return {
        "userTurn": root_user_turn(core, session, root=root),
        "origin": origin,
        "job": dict(job) if isinstance(job, dict) else None,
    }


def _session_folders(core: Any, session: Any) -> dict[str, Any]:
    """``resultsDir``, ``downloadsDir``, ``scratchDir``, ``hasProject`` (1.71.0).

    Where the host should save what it makes or downloads for the person
    (``session/paths``); fields it cannot resolve are left out.
    """
    from snowpea_core.session.paths import resolve

    try:
        folders = resolve(session, core).wire()
    except Exception:  # noqa: BLE001 - optional context; the call goes on without it
        log.warning("session folders unresolved for %s", getattr(session, "id", "?"), exc_info=True)
        return {}
    return {
        key: folders[key]
        for key in ("resultsDir", "downloadsDir", "scratchDir", "hasProject")
        if folders[key] is not None
    }


def _guard_of(core: Any, session: Any) -> str | None:
    """``"agent-switch"`` while an agent-switch guard covers ``session`` (1.25.0)."""
    from snowpea_core.agent.loop import AGENT_SWITCH_GUARD
    from snowpea_core.agent.subagent import agent_guarded

    return AGENT_SWITCH_GUARD if agent_guarded(core, session) else None


#: How long a turn waits for a connected host's ``tool.register``.
REGISTER_WAIT_SEC = 5.0


def _awaited_host(core: Any, session: Any) -> float | None:
    """When a just-connected host of the session stops being waited for.

    ``None`` unless the host the session names is connected, registered no
    tools yet, and connected less than :data:`REGISTER_WAIT_SEC` ago — a
    client that never registers costs a turn nothing once that has passed.
    """
    target = getattr(session, "host_tools_from", None)
    if not target or HOST_TOOLS.for_session(session):
        return None
    hub = getattr(core, "hub", None)
    clients = hub.clients() if hub is not None and hasattr(hub, "clients") else []
    target = str(target)
    now = time.monotonic()
    ends = [
        float(getattr(conn, "connected_at", 0.0)) + REGISTER_WAIT_SEC
        for conn in clients
        if not getattr(conn, "closed", False)
        and target
        in (
            getattr(conn, "client_id", None),
            getattr(conn, "surface_id", None),
            getattr(conn, "client_kind", None),
        )
    ]
    end = max(ends, default=0.0)
    return end if end > now else None


async def wait_for_registration(
    core: Any, session: Any, timeout: float = REGISTER_WAIT_SEC
) -> bool:
    """Wait up to ``timeout`` for a connected host's tools; True once they are there.

    Only when the host the session names is connected and has not registered
    yet — a host that is away costs nothing, as the scheduler's own wait does.
    """
    end = _awaited_host(core, session)
    if end is None:
        return bool(HOST_TOOLS.for_session(session))
    deadline = min(end, time.monotonic() + timeout)
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if HOST_TOOLS.for_session(session):
            return True
    return bool(HOST_TOOLS.for_session(session))


def root_attachments(core: Any, session: Any) -> list[dict[str, Any]]:
    """The root conversation's user attachments whose files still exist (1.23.0)."""
    from snowpea_core.attachments.store import AttachmentStore

    root = root_session(core, session)
    paths = getattr(core, "paths", None)
    if root is None or paths is None:
        return []
    rows = AttachmentStore(paths.attachments_dir).listing(str(root.id))
    return [
        {
            "name": str(row.get("name") or ""),
            "path": str(row["path"]),
            "mime": str(row.get("mime") or "application/octet-stream"),
            "size": int(row.get("size") or 0),
            "messageId": row.get("messageId"),
            # Rows written before byPath existed: a copy has a content hash.
            "byPath": bool(row.get("byPath", not row.get("sha256"))),
        }
        for row in rows
        if row.get("path") and Path(str(row["path"])).exists()
    ]


def owned_run_owner(core: Any, session: Any) -> str | None:
    """The browser whose owned job ``session`` (or a session above it) runs.

    Such a run takes no outside input — no prompt, steer, notice, command,
    setting change or attach — so nothing but the job's own task speaks under
    its ``job`` (1.23.0).  Read from ``owned_by``, which is fixed when the
    run's session is made, never from what the run is doing.
    """
    seen: set[str] = set()
    current = session
    sessions = getattr(core, "sessions", None)
    while current is not None:
        owner = getattr(current, "owned_by", None)
        if owner:
            return str(owner)
        parent_id = getattr(current, "parent_session_id", None)
        if not parent_id or parent_id in seen or sessions is None:
            return None
        seen.add(str(parent_id))
        current = sessions.get(str(parent_id))
    return None


def runs_owned_job(core: Any, session: Any) -> bool:
    return owned_run_owner(core, session) is not None


def runs_on_host(session: Any, tool: Any) -> bool:
    """True when ``tool`` is the one a host registered and ``session`` dispatches to.

    The engine's own state decides — the tool object came from the registry
    (a host tool wins its name for a session, ``ToolRegistry.get``) and the
    session's host serves that very tool now — never the tool's name or its
    arguments: a core or MCP tool of the same name is not a host tool.
    """
    if session is None or tool is None:
        return False
    source = str(getattr(tool, "source", "") or "")
    if not source.startswith("host:"):
        return False
    served = HOST_TOOLS.for_session(session).get(str(getattr(tool, "name", "")))
    return served is not None and getattr(served, "source", None) == source


def drop_owned_context(session: Any, origin: str) -> None:
    """Fail closed: a turn nobody scheduled ends an owned run's authority.

    Anything other than the run itself starting a turn in it (its own
    ``queue_command``, a gateway message, an announce) clears the job behind
    it, so later host calls carry no ``job`` (1.23.0).
    """
    if origin != "schedule" and getattr(session, "owned_by", None):
        jobs = getattr(session, "turn_jobs", None)
        if jobs:
            jobs.clear()


def root_user_turn(core: Any, session: Any, *, root: Any = None) -> dict[str, Any] | None:
    """The root conversation's current ``user_turn``, copied, or ``None``.

    A delegated child reports its root's user message, never the task text its
    parent agent wrote: that is the agent's wording, not the person's intent.
    """
    current = root if root is not None else root_session(core, session)
    if current is None:
        return None
    turn = getattr(current, "user_turn", None)
    if not isinstance(turn, dict):
        return None
    message_id = str(turn.get("messageId") or "")
    return {
        "messageId": message_id,
        "text": str(turn.get("text") or ""),
        "steers": [str(item) for item in turn.get("steers") or []],
        # False when the agent, a schedule, a group member or the system
        # started the running turn: this is the person's last message, not
        # a request made in this turn.
        "current": bool(message_id) and message_id == getattr(current, "current_turn", None),
    }


def _host_image_problem(data: Any, mime: str) -> str | None:
    """Why a host's base64 image cannot be decoded, or ``None`` (see ``validate_image``)."""
    import base64
    import binascii

    from snowpea_core.attachments.model import validate_image

    try:
        raw = base64.b64decode(str(data), validate=False)
    except (binascii.Error, ValueError):
        return "not valid base64"
    return validate_image(raw, mime, decode=True)


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
            problem = _host_image_problem(block["data"], mime)
            if problem:
                texts.append(f"[image {name}-{index} not used: {problem}]")
                continue
            image: dict[str, Any] = {
                "bytes_b64": str(block["data"]),
                "mime": mime,
                "name": f"{name}-{index}.{mime.rsplit('/', 1)[-1]}",
            }
            if block.get("ephemeral") is True:
                image["ephemeral"] = True
            images.append(image)
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


def with_help_section(session: Any, tool: Tool | None, result: ToolResult) -> ToolResult:
    """A failed host result with the manual section its meta names, once per session.

    The host puts ``helpSection: "<name>"`` in a failed result's meta (1.55.0)
    when the model misused the tool in a way that part of the manual covers;
    core appends that section the first time only, so a model that keeps
    failing does not pay for the same text again.
    """
    meta = result.meta or {}
    host_meta = meta.get(HOST_META_KEY) if isinstance(meta.get(HOST_META_KEY), dict) else meta
    wanted = host_meta.get("helpSection") if isinstance(host_meta, dict) else None
    sections = getattr(tool, "sections", None)
    if not isinstance(wanted, str) or not isinstance(sections, dict):
        return result
    text = sections.get(wanted)
    if not text:
        return result
    shown = getattr(session, "help_sections_shown", None)
    if not isinstance(shown, set):
        shown = set()
        try:
            session.help_sections_shown = shown
        except AttributeError:  # pragma: no cover - a frozen stand-in
            return result
    name = str(getattr(tool, "name", "") or "")
    key = (name, wanted)
    if key in shown:
        return result
    shown.add(key)
    help_text = f"[{name} manual, section {wanted!r}]\n{text}"
    result.error = f"{result.error or 'failed'}\n\n{help_text}"
    return result


async def tool_help(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """``tool_help {tool, section?}``: one section of a tool's manual, or their list."""
    name = str(args.get("tool") or "").strip()
    section = str(args.get("section") or "").strip()
    if not name:
        return ToolResult(ok=False, error="tool_help needs a tool name")
    registry = getattr(ctx.core, "tools", None)
    tool = registry.get(name, ctx.session) if registry is not None else None
    if tool is None or name == TOOL_HELP:
        return ToolResult(ok=False, error=f"{name} is not one of your tools")
    sections = tool.sections or {}
    if section:
        text = sections.get(section)
        if text is None:
            listed = ", ".join(sections) or "none"
            return ToolResult(
                ok=False, error=f"{name} has no section {section!r}; its sections: {listed}"
            )
        return ToolResult(ok=True, output=f"[{name} manual, section {section!r}]\n{text}")
    if sections:
        lines = [f"Sections of the {name} manual (tool_help {{tool, section}} returns one):"]
        lines.extend(
            f"- {label}: {(text.strip().splitlines() or [''])[0][:120]}"
            for label, text in sections.items()
        )
        return ToolResult(ok=True, output="\n".join(lines))
    return ToolResult(ok=True, output=tool.manual or tool.description)


TOOL_HELP_TOOL = Tool(
    name=TOOL_HELP,
    category="tools",
    description=(
        "Read the manual of one of your tools: tool_help {tool} lists its sections (or "
        "returns its whole manual), tool_help {tool, section} returns one section."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "The tool's name."},
            "section": {"type": "string", "description": "A section name, optional."},
        },
        "required": ["tool"],
    },
    permission="read",
    run=tool_help,
    parallel_safe=True,
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
    "runs_on_host",
]
