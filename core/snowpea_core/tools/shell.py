"""The ``shell`` tool: run a command on the session's execution backend."""

from __future__ import annotations

from typing import Any

from snowpea_core.exec.backend import DEFAULT_TIMEOUT, ExecResult, StreamingBackend
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools import process as process_tools
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi

MAX_TIMEOUT = 3600.0

#: Ceiling on what one command may stream as ``tool.progress``, in bytes.
#: A live tail is a courtesy, not a transport: past this the surface is told
#: once that the tail stopped and reads the rest from ``tool.result``.
MAX_STREAMED_BYTES = 256 * 1024


def _format(exit_code: int, stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(f"[stderr]\n{stderr.rstrip(chr(10))}")
    parts.append(f"[exit {exit_code}]")
    return "\n".join(parts)


async def _run(ctx: ToolContext, command: str, *, cwd: str | None, timeout: float) -> ExecResult:
    """Run the command, tailing its output as ``tool.progress`` when we can.

    Streaming is best-effort: it needs a listener (``ctx.progress``) and a
    backend that can stream (only ``local`` today).  Everything else falls
    back to the one-shot ``run``, and either way the returned result — not the
    chunks — is what the model and ``tool.result`` see (IDE-PROGRESS D2).
    """
    sink = ctx.progress
    backend = ctx.backend
    if sink is None or not isinstance(backend, StreamingBackend):
        return await backend.run(command, cwd=cwd, timeout=timeout)

    streamed = 0
    stopped = False

    async def on_chunk(stream: str, chunk: str) -> None:
        nonlocal streamed, stopped
        if stopped:
            return
        size = len(chunk.encode("utf-8", "replace"))
        if streamed + size > MAX_STREAMED_BYTES:
            stopped = True
            await sink.emit(stream, "", truncated=True)
            return
        streamed += size
        await sink.emit(stream, chunk)

    return await backend.run_stream(command, cwd=cwd, timeout=timeout, on_chunk=on_chunk)


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
        result = await _run(ctx, command, cwd=str(cwd) if cwd else None, timeout=timeout)
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
                        f"Seconds before the command is killed (default {DEFAULT_TIMEOUT:g})."
                    ),
                },
            },
            "required": ["command"],
        },
        permission="exec",
        run=shell,
    ),
)


__all__ = ["MAX_STREAMED_BYTES", "MAX_TIMEOUT", "TOOLS", "shell"]
