"""Tool registry (contract §6).

A tool is data plus one coroutine.  The registry is the only place that knows
which tools exist; the agent loop asks it for :class:`ToolSpec` objects to hand
to the provider and for the :class:`Tool` to run when the model calls one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.exec.backend import ExecutionBackend
from snowpea_core.providers.base import ToolSpec
from snowpea_core.server.protocol import PermissionTag, ToolInfo, ToolState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session


@dataclass
class ToolContext:
    """What a tool is allowed to reach: its session, the core, a backend."""

    session: Session
    core: Core
    backend: ExecutionBackend


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
        return ToolInfo(
            name=self.name,
            category=self.category,
            permissionTag=self.permission,
            state=self.state,
            source=self.source,
            description=self.description,
            reason=self.reason,
        )

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name, description=self.description, input_schema=self.input_schema
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
        audio_tools,
        browser,
        delegate,
        fs,
        git,
        glob,
        grep,
        media,
        process,
        settings_tools,
        shell,
        skills_tools,
        stubs,
        web,
    )

    for tool in (
        *fs.TOOLS,
        *glob.TOOLS,
        *grep.TOOLS,
        *shell.TOOLS,
        *process.TOOLS,
        *git.TOOLS,
        *web.TOOLS,
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
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolRun",
    "effective_permission",
    "register_builtin_tools",
]
