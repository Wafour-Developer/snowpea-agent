"""Slash-command registry (contract §9).

A command is a turn that does not go to the model.  It gets the same turn id
and the same ``turn.done`` event as a prompt, so every surface can treat the
two identically: send text, watch the event stream.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.server import errors
from snowpea_core.server.protocol import CommandInfo, CommandSource
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.commands")


@dataclass
class CommandContext:
    """What a command may reach."""

    core: Core
    session: Session
    turn_id: str
    conn: Any = None
    #: Set by a command that ran a full agent turn itself (a skill command);
    #: the registry then leaves ``turn.done`` to the turn it started.
    handled_turn: bool = False

    async def emit(self, event: events.Event) -> None:
        await self.core.hub.emit_event(self.session.id, event)

    async def say(self, text: str) -> None:
        """Answer the user with a completed assistant message."""
        await self.emit(events.message_done(text))


CommandRun = Callable[[CommandContext, str], Awaitable[None]]


@dataclass
class Command:
    """One slash command."""

    name: str
    summary: str
    run: CommandRun
    args_schema: dict[str, Any] = field(default_factory=dict)
    source: CommandSource = "builtin"

    def info(self) -> CommandInfo:
        return CommandInfo(
            name=self.name,
            summary=self.summary,
            argsSchema=self.args_schema,
            source=self.source,
        )


#: Aliases so annotations below still mean the builtin ``list``.
CommandInfos = list[CommandInfo]
Commands = list["Command"]


class CommandRegistry:
    """Name -> :class:`Command` lookup plus ``/foo bar`` parsing."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> Command:
        self._commands[command.name] = command
        return command

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def unregister(self, name: str) -> Command | None:
        """Drop a command; the skill loader does this before every re-scan."""
        return self._commands.pop(name, None)

    def list(self, session: Any | None = None) -> CommandInfos:
        return [command.info() for command in self._commands.values()]

    def commands(self) -> Commands:
        return list(self._commands.values())

    def parse(self, text: str) -> tuple[str, str] | None:
        """``"/mode auto"`` -> ``("mode", "auto")``; non-slash text -> ``None``."""
        if not text.startswith("/"):
            return None
        name, _, args = text[1:].partition(" ")
        if not name:
            return None
        return name, args.strip()

    # -- execution -----------------------------------------------------
    def start(
        self, core: Core, session: Session, name: str, args: str = "", conn: Any = None
    ) -> str:
        """Schedule a command in the background and return its turn id."""
        turn_id = f"t-{uuid.uuid4().hex[:12]}"
        session.current_turn = turn_id
        task = asyncio.ensure_future(self.run(core, session, name, args, conn, turn_id))
        session.turn_task = task
        return turn_id

    async def run(
        self,
        core: Core,
        session: Session,
        name: str,
        args: str = "",
        conn: Any = None,
        turn_id: str | None = None,
    ) -> str:
        """Run a command to completion, emitting its ``turn.done``."""
        turn_id = turn_id or f"t-{uuid.uuid4().hex[:12]}"
        command = self._commands.get(name)
        if command is None:
            await core.hub.emit_event(
                session.id, events.error(errors.NOT_FOUND, f"unknown command: /{name}")
            )
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "error"))
            return turn_id
        ctx = CommandContext(core=core, session=session, turn_id=turn_id, conn=conn)
        try:
            await command.run(ctx, args)
        except asyncio.CancelledError:
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "interrupted"))
            raise
        except Exception as exc:  # noqa: BLE001 - a broken command ends its own turn
            log.exception("command /%s failed", name)
            await core.hub.emit_event(
                session.id, events.error(errors.INTERNAL, f"{type(exc).__name__}: {exc}")
            )
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "error"))
            return turn_id
        finally:
            session.current_turn = None
        if not ctx.handled_turn:
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "complete"))
        return turn_id

    def __len__(self) -> int:
        return len(self._commands)


def register_builtin_commands(registry: CommandRegistry) -> CommandRegistry:
    """Register the built-ins: ``help``/``tools``, modes, ``/backend``, ``/agent``,
    ``/mcp`` and ``/skill``."""
    from snowpea_core.commands import (
        agent_cmd,
        backend_cmd,
        deepinit,
        delegate_cmd,
        init_cmd,
        mcp_cmd,
        mode_cmd,
        model_cmd,
        ralph,
        schedule_cmd,
        skill_cmd,
        team_cmd,
        ultrawork,
    )
    from snowpea_core.commands.builtin import COMMANDS

    for command in (
        *COMMANDS,
        *mcp_cmd.COMMANDS,
        *mode_cmd.COMMANDS,
        *model_cmd.COMMANDS,
        *backend_cmd.COMMANDS,
        *schedule_cmd.COMMANDS,
        *agent_cmd.COMMANDS,
        *delegate_cmd.COMMANDS,
        *skill_cmd.COMMANDS,
        # M7 workflows (contract §4): loops and fan-out live in Python.
        *ralph.COMMANDS,
        *ultrawork.COMMANDS,
        *deepinit.COMMANDS,
        *init_cmd.COMMANDS,
        *team_cmd.COMMANDS,
    ):
        registry.register(command)
    return registry


__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "CommandRun",
    "register_builtin_commands",
]
