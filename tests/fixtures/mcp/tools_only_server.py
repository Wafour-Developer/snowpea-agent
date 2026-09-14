#!/usr/bin/env python3
"""A tools-only MCP server, spoken as raw JSON-RPC over stdio.

The ``mcp`` package's ``MCPServer`` advertises ``resources`` and ``prompts``
whether or not any are registered, so it cannot express the case the capability
gate exists for: a server that implements only ``tools/*`` and answers every
other family with ``-32601 Method not found``. This fixture is hand-rolled for
that reason — it advertises ``tools`` alone (M15 §E).
"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Return the text it was given, unchanged.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


def reply(request_id: object, result: dict[str, object]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def refuse(request_id: object, method: str) -> None:
    error = {"code": -32601, "message": f"Method not found: {method}"}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if request_id is None:  # a notification; nothing to answer
            continue
        if method == "initialize":
            reply(
                request_id,
                {
                    "protocolVersion": message.get("params", {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    # Tools and nothing else: this is the whole point.
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture-tools-only", "version": "1.0.0"},
                },
            )
        elif method == "tools/list":
            reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            text = message.get("params", {}).get("arguments", {}).get("text", "")
            reply(request_id, {"content": [{"type": "text", "text": str(text)}]})
        elif method == "ping":
            reply(request_id, {})
        else:
            refuse(request_id, method)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
