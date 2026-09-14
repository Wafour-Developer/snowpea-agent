#!/usr/bin/env python3
"""An MCP server that advertises resources and prompts as well as one tool.

The sibling ``echo_server.py`` is deliberately tools-only: between the two, the
capability gate in ``mcp_client.register_config`` is asserted in both
directions — a tools-only server gets no ``list_resources``/``get_prompt``
stubs, and this one does (M15 §E).
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer

server = MCPServer("fixture-capable")


@server.tool(description="Return the text it was given, unchanged.")
def echo(text: str) -> str:
    """Echo ``text`` back to the caller."""
    return text


@server.resource("memo://note", description="One fixture note.")
def note() -> str:
    """A resource, so the server advertises the ``resources`` capability."""
    return "a fixture note"


@server.prompt(description="A fixture prompt template.")
def greet(name: str) -> str:
    """A prompt, so the server advertises the ``prompts`` capability."""
    return f"Say hello to {name}."


if __name__ == "__main__":
    try:
        server.run("stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
