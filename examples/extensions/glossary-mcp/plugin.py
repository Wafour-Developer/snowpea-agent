"""glossary-mcp: an MCP server that lives as long as the extension.

The entry has the shape of one .mcp.json entry. The server starts in the
background when the extension loads; its tools appear as
mcp__glossary__define once it is up, carry the MCP permission (network unless
settings.mcp says otherwise), and go away when the extension is disabled,
reloaded or removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from snowpea_core.extensions import PluginContext


def apply(ctx: PluginContext) -> None:
    server = Path(__file__).with_name("server.py")
    ctx.mcp.register(
        "glossary",
        {
            "command": sys.executable,  # the daemon's Python
            "args": [str(server)],
            "timeoutSec": 20,
        },
    )
