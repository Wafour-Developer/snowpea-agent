"""file-stats: a tool in depth.

Shows an argument schema with required fields, a permission tag, errors the
model can act on, the session's backend for file access (so the tool works in
SSH and Docker sessions too), progress for a long call, work moved off the
event loop, a per-call timeout, and facts for surfaces in ``meta``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import posixpath
from typing import Any

from snowpea_core.extensions import PluginContext, Tool, ToolContext, ToolResult

MAX_FILES = 500

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "A file or folder, relative to the project root.",
        },
        "pattern": {
            "type": "string",
            "description": "For a folder: a glob such as '*.py'. Default: every file.",
            "default": "*",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _count(blobs: list[bytes]) -> tuple[int, int]:
    """Lines and bytes; runs in a worker thread."""
    return sum(blob.count(b"\n") for blob in blobs), sum(len(blob) for blob in blobs)


async def file_stats(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    # The session's files: on this machine, in a Docker container or over SSH.
    backend = tool_ctx.backend
    raw = str(args.get("path") or "").strip()
    if not raw:
        return ToolResult(ok=False, error="path is required")
    root = posixpath.normpath(str(backend.cwd).replace("\\", "/"))
    target = posixpath.normpath(str(backend.resolve(raw)).replace("\\", "/"))
    # A read tool still stays inside the project: say so instead of reading.
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        return ToolResult(ok=False, error=f"{raw} is outside the project; give a path inside it")
    if not await backend.exists(target):
        return ToolResult(ok=False, error=f"{raw} does not exist")
    parent, name = posixpath.split(target)
    if f"{name}/" in await backend.list_dir(parent):  # list_dir marks folders with "/"
        pattern = str(args.get("pattern") or "*")
        names = await backend.list_dir(target)
        files = [
            posixpath.join(target, entry)
            for entry in names
            if not entry.endswith("/") and fnmatch.fnmatch(entry, pattern)
        ]
    else:
        files = [target]
    if len(files) > MAX_FILES:
        return ToolResult(ok=False, error=f"{len(files)} files match; narrow the pattern")
    if tool_ctx.progress is not None:  # None when nobody is watching (tests, headless)
        await tool_ctx.progress.emit("stdout", f"reading {len(files)} files\n")
    blobs = [await backend.read_bytes(file) for file in files]
    # Counting is CPU work: keep it off the daemon's event loop.
    lines, size = await asyncio.to_thread(_count, blobs)
    return ToolResult(
        ok=True,
        output=f"{len(files)} files, {lines} lines, {size} bytes",
        meta={"files": len(files), "lines": lines, "bytes": size},  # for surfaces, not the model
    )


def apply(ctx: PluginContext) -> None:
    ctx.tools.register(
        Tool(
            name="file_stats",
            category="files",
            description=(
                "Count lines and bytes of a file, or of the files directly in a folder. "
                "Use it to size up files before reading them."
            ),
            input_schema=SCHEMA,
            permission="read",  # reads only: no approval needed in any mode
            run=file_stats,
            parallel_safe=True,  # two calls in one response may run at once
        ),
        timeout=30,  # a call that takes longer fails with a timeout error
    )
