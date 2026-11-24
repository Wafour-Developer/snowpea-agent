#!/usr/bin/env python3
"""An MCP server whose tool list changes while the client stays connected.

Starts with ``echo``, ``grow``, ``grow_quietly`` and ``old_tool``.  ``grow``
adds ``write_cell`` and ``get_table``, removes ``old_tool`` and sends
``notifications/tools/list_changed``; ``grow_quietly`` makes the same change
without the notification, the way a redeploy behind a proxy can look.  The
M36 re-list tests drive it.
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

server = MCPServer("fixture-changing")


def write_cell(table: int, row: int, col: int, text: str) -> str:
    """Write ``text`` into one table cell."""
    return f"wrote {text!r} at {table}/{row}/{col}"


def get_table(table: int) -> str:
    """Describe one table."""
    return f"table {table}: 2x2, empty cells (0,1) (1,1)"


def old_tool() -> str:
    """A tool that goes away on ``grow``."""
    return "old"


def _grow() -> None:
    names = {tool.name for tool in server._tool_manager.list_tools()}
    if "write_cell" not in names:
        server.add_tool(write_cell, description="Write text into an HWP table cell.")
        server.add_tool(get_table, description="Read an HWP table's cells.")
    if "old_tool" in names:
        server.remove_tool("old_tool")


@server.tool(description="Return the text it was given, unchanged.")
def echo(text: str) -> str:
    return text


@server.tool(description="Add the table tools and announce the change.")
async def grow(ctx: Context) -> str:
    _grow()
    await ctx.session.send_tool_list_changed()
    return "grown"


@server.tool(description="Add the table tools without announcing it.")
def grow_quietly() -> str:
    _grow()
    return "grown quietly"


server.add_tool(old_tool, description="A tool that goes away.")


if __name__ == "__main__":
    try:
        server.run("stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
