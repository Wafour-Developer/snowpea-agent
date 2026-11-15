"""A tiny MCP server over stdio: looks words up in the team glossary.

The extension starts it with the daemon's own Python, which already has the
``mcp`` package. Any MCP server works the same way: an npx package, a binary,
or a remote server given by ``url``.
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer

GLOSSARY = {
    "daemon": "The background process that owns sessions, tools and permissions.",
    "extension": "Python code in $SNOWPEA_HOME/extensions that adds to the daemon.",
    "skill": "A SKILL.md folder that teaches the model a procedure.",
}

server = MCPServer("glossary")


@server.tool(description="Define a term from the team glossary.")
def define(term: str) -> str:
    """Return the definition of ``term``, or say it is not in the glossary."""
    return GLOSSARY.get(term.strip().lower(), f"{term!r} is not in the glossary.")


if __name__ == "__main__":
    try:
        server.run("stdio")
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
