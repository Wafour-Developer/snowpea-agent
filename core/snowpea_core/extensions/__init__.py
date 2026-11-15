"""The public API for snowpea extensions — import everything from here.

    from snowpea_core.extensions import PluginContext, Tool, ToolResult

These names are the stable surface (see :data:`API_VERSION` and the policy in
:mod:`snowpea_core.extensions.api`); the modules they are defined in are not,
and may move between releases.

* :class:`PluginContext` — what ``apply(ctx)`` receives.
* Tools: :class:`Tool`, :class:`ToolResult`, :class:`ToolContext`,
  :data:`PermissionTag`.
* Prompt: :class:`PromptSection`, :class:`PromptInputs`, :data:`SECTION_ORDERS`.
* Runtime context: :class:`ContextProvider`, :data:`CONTEXT_ORDERS`.
* Hooks: :class:`PostDecision` (``post_execute``), :class:`StopDecision` and
  :class:`StopContext` (``turn_stopping``), :data:`STOP_ORDERS`.
* Commands: :class:`Command`, :class:`CommandContext`.
* :class:`ExtensionError` — raised by a registration that cannot be made.
"""

from __future__ import annotations

from snowpea_core.agent.runtime_context import CONTEXT_ORDERS, ContextProvider
from snowpea_core.agent.turn_stopping import StopContext, StopDecision
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.extensions.api import (
    API_VERSION,
    DISPOSE_TIMEOUT,
    TOOL_PERMISSIONS,
    Disposer,
    ExtensionError,
    PluginContext,
)
from snowpea_core.prompts.sections import SECTION_ORDERS, PromptInputs, PromptSection
from snowpea_core.server.protocol import PermissionTag
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.tools.result_hooks import PostDecision

#: Where the built-in ``turn_stopping`` listeners sit; the first decision wins.
STOP_ORDERS: dict[str, int] = {"verify-gate": 100, "unfinished-work": 150, "stop-hooks": 200}

__all__ = [
    "API_VERSION",
    "CONTEXT_ORDERS",
    "DISPOSE_TIMEOUT",
    "SECTION_ORDERS",
    "STOP_ORDERS",
    "TOOL_PERMISSIONS",
    "Command",
    "CommandContext",
    "ContextProvider",
    "Disposer",
    "ExtensionError",
    "PermissionTag",
    "PluginContext",
    "PostDecision",
    "PromptInputs",
    "PromptSection",
    "StopContext",
    "StopDecision",
    "Tool",
    "ToolContext",
    "ToolResult",
]
