"""The ``execute_code`` tool: run a short, temporary Python program."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi

DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
#: Attempts and pause between them when deleting the script.  Windows refuses
#: to delete a file another process still has open (WinError 32): a child the
#: program left running, a virus scanner or the indexer.
UNLINK_ATTEMPTS = 5
UNLINK_PAUSE = 0.2
#: A script left behind that long ago is swept on the next call.
STALE_AFTER = 3600.0


def _format(exit_code: int, stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(f"--- stderr ---\n{stderr.rstrip(chr(10))}")
    parts.append(f"[exit {exit_code}]")
    return "\n".join(parts)


def _quote(value: str) -> str:
    """Quote one argument for the shell the backend runs (cmd.exe on Windows)."""
    if sys.platform == "win32":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def interpreter(workdir: Path) -> str:
    """The command that runs a ``.py`` file, shell-quoted.

    The project's ``.venv`` first.  A frozen (PyInstaller) desktop core's
    ``sys.executable`` is the core itself, which took the script for an
    unknown argument: it uses the ``python3`` on PATH, and only without one
    its own bundled interpreter through ``--run-hook``.
    """
    for project_python in (
        workdir / ".venv" / "bin" / "python",
        workdir / ".venv" / "Scripts" / "python.exe",
    ):
        if project_python.exists():
            return _quote(str(project_python))
    if not getattr(sys, "frozen", False):
        return _quote(sys.executable)
    system = shutil.which("python3") or (
        shutil.which("python") if sys.platform == "win32" else None
    )
    if system:
        return _quote(system)
    return f"{_quote(sys.executable)} --run-hook"


async def _delete(path: Path) -> None:
    """Delete the script once the child is done, tolerating a Windows lock.

    A file still held open elsewhere cannot be deleted on Windows; a few
    retries usually outlast a scanner, and what remains is swept later
    (:func:`_sweep_stale`) instead of failing a call whose program ran.
    """
    for attempt in range(UNLINK_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            if attempt + 1 < UNLINK_ATTEMPTS:
                await asyncio.sleep(UNLINK_PAUSE)


def _sweep_stale(temp_dir: Path) -> None:
    """Delete scripts earlier calls could not, once they are an hour old."""
    cutoff = time.time() - STALE_AFTER
    for stale in temp_dir.glob("execute-code-*.py"):
        with contextlib.suppress(OSError):
            if stale.stat().st_mtime < cutoff:
                stale.unlink()


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
        _sweep_stale(temp_dir)
        fd, raw_path = tempfile.mkstemp(prefix="execute-code-", suffix=".py", dir=temp_dir)
        path = Path(raw_path)
        with contextlib.suppress(AttributeError, OSError):
            os.fchmod(fd, 0o700)  # no os.fchmod on Windows before Python 3.13
        # Written and closed before the child starts: Windows will not let
        # another process open a file this one still holds.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code)

        command = f"{interpreter(workdir)} {_quote(str(path))}"
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
            await _delete(path)

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
