"""The ``execute_code`` tool: run a short, temporary Python program."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi

DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0


def _format(exit_code: int, stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(f"--- stderr ---\n{stderr.rstrip(chr(10))}")
    parts.append(f"[exit {exit_code}]")
    return "\n".join(parts)


async def execute_code(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    code = args.get("code")
    if not isinstance(code, str) or not code:
        return ToolResult(ok=False, error="code is required")

    raw_timeout = args.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = min(MAX_TIMEOUT, max(1.0, float(raw_timeout)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    workdir = Path(ctx.session.workdir)
    temp_dir = workdir / ".snowpea" / "tmp"
    path: Path | None = None
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.chmod(0o700)
        fd, raw_path = tempfile.mkstemp(prefix="execute-code-", suffix=".py", dir=temp_dir)
        path = Path(raw_path)
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code)

        project_python = workdir / ".venv" / "bin" / "python"
        python = project_python if project_python.exists() else Path(sys.executable)
        command = f"{shlex.quote(str(python))} {shlex.quote(str(path))}"
        cwd = args.get("cwd")
        result = await ctx.backend.run(
            command,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env={"PYTHONUNBUFFERED": "1"},
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    output = _format(result.exit_code, strip_ansi(result.stdout), strip_ansi(result.stderr))
    if result.timed_out:
        return ToolResult(ok=False, output=output, error=result.stderr or "command timed out")
    if result.exit_code != 0:
        return ToolResult(ok=False, output=output, error=f"command exited with {result.exit_code}")
    return ToolResult(ok=True, output=output)


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="execute_code",
        category="terminal",
        description=descriptions.EXECUTE_CODE,
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python program to run."},
                "timeout": {
                    "type": "number",
                    "description": (
                        "Seconds before the program is killed (default 60, maximum 600)."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "Directory to run in, relative to the workdir.",
                },
            },
            "required": ["code"],
        },
        permission="exec",
        run=execute_code,
    ),
)


__all__ = ["DEFAULT_TIMEOUT", "MAX_TIMEOUT", "TOOLS", "execute_code"]
