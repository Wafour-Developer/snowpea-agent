#!/usr/bin/env python3
"""A one-tool MCP server over stdio, used by the tools contract tests.

Exposes ``echo``, which returns whatever text it was given.  Declared the way a
real server is::

    {"mcpServers": {"fixture-echo": {"command": "python", "args": ["echo_server.py"]}}}
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer

server = MCPServer("fixture-echo")


@server.tool(description="Return the text it was given, unchanged.")
def echo(text: str) -> str:
    """Echo ``text`` back to the caller."""
    return text


if __name__ == "__main__":
    try:
        server.run("stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
