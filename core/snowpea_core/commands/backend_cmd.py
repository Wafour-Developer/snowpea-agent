"""``/backend`` — show or change where this session's tools run (US-010).

``/backend`` prints the current backend, ``/backend local`` moves back to the
daemon's own machine, and ``/backend docker {"image":"python:3.11-slim"}`` or
``/backend ssh {"host":"box","user":"me","key":"~/.ssh/id_ed25519"}`` take a
JSON object of backend settings.  It goes through the same code path as the
``backend.set`` RPC, so both emit ``backend.changed``.
"""

from __future__ import annotations

import json
from typing import Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.exec.factory import BACKEND_KINDS, build_backend
from snowpea_core.session import events

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": list(BACKEND_KINDS),
            "description": "Where tools execute.",
        },
        "config": {
            "type": "object",
            "description": 'Backend settings as a JSON object, e.g. {"image": "..."}.',
        },
    },
}

USAGE = (
    "Usage: /backend [local|docker|ssh] [json-config]\n"
    '  /backend docker {"image": "python:3.11-slim"}\n'
    '  /backend ssh {"host": "box", "user": "me", "key": "~/.ssh/id_ed25519"}'
)


def parse_args(args: str) -> tuple[str, dict[str, Any]]:
    """``'docker {"image": "x"}'`` -> ``("docker", {"image": "x"})``."""
    kind, _, rest = args.strip().partition(" ")
    kind = kind.strip().lower()
    rest = rest.strip()
    if not rest:
        return kind, {}
    try:
        config = json.loads(rest)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config must be a JSON object: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    return kind, config


async def cmd_backend(ctx: CommandContext, args: str) -> None:
    """Show or change the session's execution backend."""
    if not args.strip():
        backend = ctx.session.backend
        await ctx.say(f"Backend: {backend.kind} (cwd {backend.cwd})\n{USAGE}")
        return
    try:
        kind, config = parse_args(args)
    except ValueError as exc:
        await ctx.say(f"{exc}\n{USAGE}")
        return
    if kind not in BACKEND_KINDS:
        await ctx.say(f"Unknown backend '{kind}'. Use one of: {', '.join(BACKEND_KINDS)}.")
        return
    try:
        backend = build_backend(
            kind, config, workdir=ctx.session.workdir, session_id=ctx.session.id
        )
    except ValueError as exc:
        await ctx.say(str(exc))
        return
    await ctx.session.set_backend(backend)
    await ctx.emit(events.backend_changed(kind))
    await ctx.say(f"Backend: {kind} (cwd {backend.cwd})")


BACKEND_COMMAND = Command(
    name="backend",
    summary="Show or change where tools run: /backend [local|docker|ssh] [json].",
    run=cmd_backend,
    args_schema=ARGS_SCHEMA,
)


COMMANDS: tuple[Command, ...] = (BACKEND_COMMAND,)


__all__ = [
    "ARGS_SCHEMA",
    "BACKEND_COMMAND",
    "COMMANDS",
    "USAGE",
    "cmd_backend",
    "parse_args",
]
