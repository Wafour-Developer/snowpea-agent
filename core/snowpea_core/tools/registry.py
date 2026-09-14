"""Tool registry (contract §6).

A tool is data plus one coroutine.  The registry is the only place that knows
which tools exist; the agent loop asks it for :class:`ToolSpec` objects to hand
to the provider and for the :class:`Tool` to run when the model calls one.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.providers.base import ToolSpec
from snowpea_core.server.protocol import PermissionTag, ToolInfo, ToolState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger(__name__)


class ProgressEmitter:
    """Publishes ``tool.progress`` for one in-flight call (IDE-PROGRESS D2).

    Owns the ``seq`` counter so every chunk of one call is ordered, and
    swallows emit failures: a surface that went away must never fail the tool
    whose output it was watching.  ``tool.result`` remains authoritative.
    """

    def __init__(self, core: Core, session_id: str, call_id: str, name: str) -> None:
        self._core = core
        self._session_id = session_id
        self._call_id = call_id
        self._name = name
        self._seq = 0

    async def emit(self, stream: str, chunk: str, *, truncated: bool = False) -> None:
        from snowpea_core.session import events

        seq, self._seq = self._seq, self._seq + 1
        event = events.tool_progress(
            self._call_id,
            self._name,
            stream=stream if stream in ("stdout", "stderr") else "stdout",
            chunk=chunk,
            seq=seq,
            truncated=truncated,
        )
        try:
            await self._core.hub.emit_event(self._session_id, event)
        except Exception:  # noqa: BLE001 - a dead listener must not fail the tool
            log.debug("could not emit tool.progress for %s", self._call_id, exc_info=True)


@dataclass
class ToolContext:
    """What a tool is allowed to reach: its session, the core, a backend."""

    session: Session
    core: Core
    backend: ExecutionBackend
    #: Id of the ``tool.call`` being served, when the loop is running one.
    #: Empty for a tool invoked outside the loop (tests, internal callers).
    call_id: str = ""
    #: Sink for ``tool.progress``; ``None`` when nothing is listening, which
    #: is the signal to skip the streaming path entirely.
    progress: ProgressEmitter | None = None


@dataclass
class ToolResult:
    """What a tool returns; ``diff`` triggers an extra ``diff`` event."""

    ok: bool
    output: str = ""
    error: str | None = None
    diff: str | None = None
    path: str | None = None
    #: Structured facts about the call that the text output only hints at —
    #: ``web_search`` puts ``provider`` / ``fallback_from`` / ``reason`` here.
    meta: dict[str, Any] | None = None


ToolRun = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class Tool:
    """One callable capability offered to the model."""

    name: str
    category: str
    description: str
    input_schema: dict[str, Any]
    permission: PermissionTag
    run: ToolRun
    state: ToolState = "active"
    source: str = "builtin"
    #: Why an inactive tool is inactive, shown by ``tool.list``; empty while it
    #: is active.  ``lsp_*`` sets it when ``lsp.enabled`` is false (M13 §4).
    reason: str = ""
    #: Optional per-call override of :attr:`permission`.  A write that lands on
    #: a snowpea configuration file is re-tagged ``config``, which the mode
    #: matrix never resolves to a silent ``allow`` (see ``tools/config_guard``).
    permission_for: Callable[[dict[str, Any], Any, Any], PermissionTag] | None = None

    def info(self) -> ToolInfo:
        # ``mcp:<server>`` is the source an MCP tool is registered with, so the
        # server name a client groups by is derivable here rather than being
        # parsed out of the tool name by every surface (M14 §3).
        server = self.source[4:] if self.source.startswith("mcp:") else ""
        return ToolInfo(
            name=self.name,
            category=self.category,
            permissionTag=self.permission,
            state=self.state,
            source=self.source,
            server=server,
            description=self.description,
            reason=self.reason,
        )

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            source=self.source,
            permission=self.permission,
        )


#: Aliases so the annotations below still mean the builtin ``list`` even
#: though the registries define a method called ``list``.
ToolInfos = list[ToolInfo]
ToolSpecs = list[ToolSpec]
Tools = list[Tool]


@dataclass
class ToolRegistry:
    """Name -> :class:`Tool` lookup, insertion-ordered."""

    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def unregister(self, name: str) -> Tool | None:
        """Drop a tool; used when an MCP server goes away."""
        return self._tools.pop(name, None)

    def set_state(self, name: str, state: ToolState) -> Tool | None:
        """Flip one tool between ``active`` and ``inactive`` in place.

        Media tools use this to become callable the moment credentials arrive,
        without a daemon restart (contract §2).
        """
        tool = self._tools.get(name)
        if tool is not None:
            tool.state = state
        return tool

    def list(self, session: Any | None = None) -> ToolInfos:
        """Every registered tool as protocol ``ToolInfo`` (``tool.list``)."""
        return [tool.info() for tool in self._tools.values()]

    def active(self, session: Any | None = None) -> Tools:
        """Active tools, narrowed by a skill's ``allowed-tools`` when one is set."""
        allowed = getattr(session, "allowed_tools", None) if session is not None else None
        return [
            tool
            for tool in self._tools.values()
            if tool.state == "active" and (allowed is None or tool.name in allowed)
        ]

    def specs(self, session: Any | None = None) -> ToolSpecs:
        """Tool descriptions for the provider (active tools only)."""
        return [tool.spec() for tool in self.active(session)]

    def __len__(self) -> int:
        return len(self._tools)


def effective_permission(
    tool: Tool,
    args: dict[str, Any] | None = None,
    session: Any = None,
    core: Any = None,
) -> PermissionTag:
    """The tag this particular call is judged by.

    Almost always :attr:`Tool.permission`; ``write_file`` and ``edit_file``
    raise it to ``config`` when the path is a settings or credentials file.
    ``core`` is passed so the hook can read the daemon's real home rather than
    re-deriving it from the environment.
    """
    hook = tool.permission_for
    if hook is None:
        return tool.permission
    try:
        return hook(args or {}, session, core)
    except Exception:  # noqa: BLE001 - a broken hook must not widen permission
        return tool.permission


def register_builtin_tools(registry: ToolRegistry) -> ToolRegistry:
    """Register the whole builtin catalog (M2 contract §2).

    MCP tools are not here: they are discovered per workdir and registered by
    :func:`snowpea_core.tools.mcp_client.sync_tools`.
    """
    from snowpea_core.lsp import tools as lsp_tools
    from snowpea_core.tools import (
        ask_user,
        audio_tools,
        browser,
        delegate,
        fs,
        git,
        glob,
        grep,
        media,
        process,
        set_mode,
        settings_tools,
        shell,
        skills_tools,
        stubs,
        web,
    )

    for tool in (
        *fs.TOOLS,
        *ask_user.TOOLS,
        *glob.TOOLS,
        *grep.TOOLS,
        *shell.TOOLS,
        *process.TOOLS,
        *git.TOOLS,
        *web.TOOLS,
        *set_mode.TOOLS,
        *settings_tools.TOOLS,
        *skills_tools.TOOLS,
        *browser.TOOLS,
        *stubs.TOOLS,
        *media.TOOLS,
        *audio_tools.TOOLS,
        *delegate.TOOLS,
        *lsp_tools.TOOLS,
    ):
        registry.register(tool)
    return registry


__all__ = [
    "ProgressEmitter",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolRun",
    "effective_permission",
    "register_builtin_tools",
]
