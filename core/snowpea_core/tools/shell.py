"""The ``shell`` tool: run a command on the session's execution backend."""

from __future__ import annotations

from typing import Any

from snowpea_core.exec.backend import DEFAULT_TIMEOUT
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools import process as process_tools
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi

MAX_TIMEOUT = 3600.0


def _format(exit_code: int, stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(f"[stderr]\n{stderr.rstrip(chr(10))}")
    parts.append(f"[exit {exit_code}]")
    return "\n".join(parts)


async def shell(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    command = str(args.get("command", "")).strip()
    if not command:
        return ToolResult(ok=False, error="command is required")
    if bool(args.get("background", False)):
        return await process_tools.launch(ctx, command)
    cwd = args.get("cwd")
    raw_timeout = args.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = min(MAX_TIMEOUT, max(1.0, float(raw_timeout)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    try:
        result = await ctx.backend.run(command, cwd=str(cwd) if cwd else None, timeout=timeout)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    output = _format(result.exit_code, strip_ansi(result.stdout), strip_ansi(result.stderr))
    if result.timed_out:
        return ToolResult(ok=False, output=output, error=result.stderr or "command timed out")
    if result.exit_code != 0:
        return ToolResult(ok=False, output=output, error=f"command exited with {result.exit_code}")
    return ToolResult(ok=True, output=output)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="shell",
        category="terminal",
        description=descriptions.SHELL,
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to run."},
                "cwd": {
                    "type": "string",
                    "description": "Directory to run in, relative to the workdir.",
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Detach the command and return immediately; track it with "
                        "process_list and stop it with process_kill."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Seconds before the command is killed "
                        f"(default {DEFAULT_TIMEOUT:g})."
                    ),
                },
            },
            "required": ["command"],
        },
        permission="exec",
        run=shell,
    ),
)


__all__ = ["MAX_TIMEOUT", "TOOLS", "shell"]
