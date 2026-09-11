"""Tool registry — M1 stub (US-005 fills it in)."""

from __future__ import annotations

from typing import Any

from snowpea_core.server.protocol import ToolInfo


class ToolRegistry:
    """Name -> tool lookup. Stub: empty."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        self._tools[getattr(tool, "name", str(tool))] = tool

    def get(self, name: str) -> Any | None:
        return self._tools.get(name)

    def list(self, session: Any | None = None) -> list[ToolInfo]:
        return []


__all__ = ["ToolRegistry"]
