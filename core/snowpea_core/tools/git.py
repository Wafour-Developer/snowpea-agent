"""Git tools: thin, quoted wrappers over ``git`` on the session backend.

Nothing here shells out directly.  Every command goes through
``ctx.backend.run``, so ``/backend docker`` moves the repo view with it
(AC-18).  ANSI colour is stripped with the vendored Hermes helper because a
user's ``color.ui = always`` would otherwise land escape codes in the
transcript.
"""

from __future__ import annotations

import shlex
from typing import Any

from snowpea_core.tools.registry import Tool, ToolContext, ToolResult
from snowpea_core.vendor.hermes.tools.ansi_strip import strip_ansi

#: Prefix every invocation so config and locale cannot reshape the output.
GIT = "git --no-pager -c color.ui=false"

DEFAULT_LOG_COUNT = 20
MAX_LOG_COUNT = 500


async def _git(ctx: ToolContext, argv: str, *, timeout: float = 60.0) -> ToolResult:
    result = await ctx.backend.run(f"{GIT} {argv}", cwd=None, timeout=timeout)
    stdout = strip_ansi(result.stdout or "")
    stderr = strip_ansi(result.stderr or "")
    if result.timed_out:
        return ToolResult(ok=False, output=stdout, error=f"git timed out: {argv}")
    if result.exit_code != 0:
        return ToolResult(
            ok=False,
            output=stdout,
            error=stderr.strip() or f"git exited with {result.exit_code}",
        )
    return ToolResult(ok=True, output=stdout.rstrip("\n") or "(no output)")


async def git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await _git(ctx, "status --porcelain=v1 --branch")


async def git_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    parts = ["diff"]
    if bool(args.get("staged", False)):
        parts.append("--cached")
    path = str(args.get("path", "")).strip()
    if path:
        parts.extend(["--", shlex.quote(path)])
    result = await _git(ctx, " ".join(parts))
    if result.ok:
        return ToolResult(ok=True, output=result.output, diff=result.output or None)
    return result


async def git_log(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        count = int(args.get("count", DEFAULT_LOG_COUNT))
    except (TypeError, ValueError):
        count = DEFAULT_LOG_COUNT
    count = max(1, min(MAX_LOG_COUNT, count))
    pretty = shlex.quote("--pretty=format:%h %ad %an %s")
    parts = ["log", f"-n {count}", pretty, "--date=short"]
    path = str(args.get("path", "")).strip()
    if path:
        parts.extend(["--", shlex.quote(path)])
    return await _git(ctx, " ".join(parts))


async def git_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    message = str(args.get("message", "")).strip()
    if not message:
        return ToolResult(ok=False, error="message is required")
    paths = args.get("paths")
    if isinstance(paths, list) and paths:
        quoted = " ".join(shlex.quote(str(p)) for p in paths)
        staged = await _git(ctx, f"add -- {quoted}")
    elif bool(args.get("all", True)):
        staged = await _git(ctx, "add -A")
    else:
        staged = ToolResult(ok=True)
    if not staged.ok:
        return staged
    commit = await _git(ctx, f"commit -m {shlex.quote(message)}")
    if not commit.ok:
        return commit
    head = await _git(ctx, f"log -n 1 {shlex.quote('--pretty=format:%H %s')}")
    return ToolResult(ok=True, output=f"{commit.output}\n{head.output}".strip())


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="git_status",
        category="git",
        description="Show the working tree status of the repository in the session workdir.",
        input_schema={"type": "object", "properties": {}},
        permission="read",
        run=git_status,
    ),
    Tool(
        name="git_diff",
        category="git",
        description="Show the unified diff of unstaged changes, or of staged ones.",
        input_schema={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Diff the index instead."},
                "path": {"type": "string", "description": "Limit the diff to this path."},
            },
        },
        permission="read",
        run=git_diff,
    ),
    Tool(
        name="git_log",
        category="git",
        description="List recent commits as 'hash date author subject'.",
        input_schema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": f"Commits to list (default {DEFAULT_LOG_COUNT}).",
                },
                "path": {"type": "string", "description": "Only commits touching this path."},
            },
        },
        permission="read",
        run=git_log,
    ),
    Tool(
        name="git_commit",
        category="git",
        description=(
            "Stage changes and commit them. Stages everything unless paths are given."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only stage these paths.",
                },
                "all": {
                    "type": "boolean",
                    "description": "Stage every change when no paths are given (default true).",
                },
            },
            "required": ["message"],
        },
        permission="write",
        run=git_commit,
    ),
)


__all__ = [
    "DEFAULT_LOG_COUNT",
    "GIT",
    "MAX_LOG_COUNT",
    "TOOLS",
    "git_commit",
    "git_diff",
    "git_log",
    "git_status",
]
