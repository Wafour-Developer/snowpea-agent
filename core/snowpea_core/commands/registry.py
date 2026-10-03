"""Slash-command registry (contract §9).

A command is a turn that does not go to the model.  It gets the same turn id
and the same ``turn.done`` event as a prompt, so every surface can treat the
two identically: send text, watch the event stream.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from snowpea_core.providers.base import ChatMessage
from snowpea_core.server import errors
from snowpea_core.server.protocol import CommandInfo, CommandSource
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

#: Commands that write a project instruction file, so the cached environment
#: block is stale the moment they finish (CORE-context-files).
CONTEXT_WRITING_COMMANDS: frozenset[str] = frozenset({"init", "deepinit", "skill"})

#: Keep command turns useful for future model context without letting verbose
#: command output consume the next prompt.
MAX_COMMAND_HISTORY_CHARS = 6000
MAX_COMMAND_HISTORY_HEAD_CHARS = 3000
MAX_COMMAND_HISTORY_TAIL_CHARS = 2600
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"oauth[_-]?token|password|passwd|secret|client[_-]?secret|authorization)"
    r"(\s*(?:=|:)\s*|\s+)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer)\s+([a-z0-9._~+/=-]{8,})")
_AUTHORIZATION_HEADER = re.compile(r"(?im)^(\s*authorization\s*:\s*).+$")
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|password|'
    r'passwd|secret|client[_-]?secret|authorization)"\s*:\s*)"(?:\\.|[^"\\])*"'
)

log = logging.getLogger("snowpea.commands")


def _redact_command_text(text: str) -> str:
    """Mask secret-looking command args/output before persisting history."""
    masked = _JSON_SECRET.sub(lambda m: f'{m.group(1)}"***"', text)
    masked = _AUTHORIZATION_HEADER.sub(lambda m: f"{m.group(1)}***", masked)
    masked = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}***", masked)
    return _BEARER_TOKEN.sub(lambda m: f"{m.group(1)} ***", masked)


def _clip_command_history(text: str) -> str:
    """Keep both the beginning and final status when a command is verbose."""
    if len(text) <= MAX_COMMAND_HISTORY_CHARS:
        return text
    head = text[:MAX_COMMAND_HISTORY_HEAD_CHARS].rstrip()
    tail = text[-MAX_COMMAND_HISTORY_TAIL_CHARS:].lstrip()
    return f"{head}\n…[command output truncated]\n{tail}"


@dataclass
class CommandContext:
    """What a command may reach."""

    core: Core
    session: Session
    turn_id: str
    conn: Any = None
    command_name: str = ""
    command_args: str = ""
    #: Set by a command that ran a full agent turn itself (a skill command);
    #: the registry then leaves ``turn.done`` to the turn it started.
    handled_turn: bool = False
    #: Assistant texts emitted by plain commands. Skill commands run the normal
    #: agent loop, so their history is owned by that loop instead.
    output_texts: list[str] = field(default_factory=list)
    #: Optional concise handoff summary a command can provide for the next model
    #: turn. This is especially important for workflow commands such as
    #: ``/ralph`` whose detailed event stream is not otherwise in history.
    history_summary: str | None = None
    _history_recorded: bool = False
    _history_started: bool = False

    async def emit(self, event: events.Event) -> None:
        await self.core.hub.emit_event(self.session.id, event)

    async def say(self, text: str) -> None:
        """Answer the user with a completed assistant message."""
        self.output_texts.append(text)
        if self.command_name in {"ralph", "team", "workers"}:
            from snowpea_core.agent.loop import _flush_notices
            from snowpea_core.session.manager import persist_history

            self._start_history()
            await _flush_notices(self.session)
            self.session.history.append(
                ChatMessage(
                    role="assistant", content=_clip_command_history(_redact_command_text(text))
                )
            )
            await persist_history(self.core.store, self.session)
        await self.emit(events.message_done(text))

    def _start_history(self) -> None:
        if self._history_started:
            return
        line = _redact_command_text(f"/{self.command_name} {self.command_args}".strip())
        self.session.history.append(ChatMessage(role="user", content=line))
        self._history_started = True

    def set_history_summary(self, text: str | None) -> None:
        self.history_summary = text.strip() if text and text.strip() else None

    async def record_history(self, *, reason: str = "complete") -> None:
        """Persist a plain command turn into the conversation history.

        Slash commands are real turns for surfaces, but commands that do not run
        the agent loop used to leave no user/assistant messages behind. The next
        ordinary prompt then had no main-context knowledge of a completed (or
        failed) workflow such as ``/ralph``.
        """
        if self._history_recorded:
            return
        self._history_recorded = True
        line = _redact_command_text(f"/{self.command_name} {self.command_args}".strip())
        if line == "/":
            line = "/" + (self.command_name or "command")
        text = self.history_summary or "\n\n".join(
            part for part in self.output_texts if part.strip()
        )
        text = _redact_command_text(text)
        if not text.strip():
            text = f"Command {line} finished with reason: {reason}."
        text = _clip_command_history(text)
        if reason != "complete" and not text.lower().startswith("command failed"):
            text = f"Command finished with reason '{reason}'.\n\n{text}"
        self._start_history()
        self.session.history.append(ChatMessage(role="assistant", content=text))
        self.session.history.compact()
        try:
            from snowpea_core.session.manager import persist_history

            await persist_history(self.core.store, self.session)
        except Exception:  # noqa: BLE001 - command history is best-effort
            log.debug("could not persist command history for %s", self.session.id, exc_info=True)


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
        #: Names of the commands the core itself ships (``/ralph``, ``/team``,
        #: ``/plan`` …). A skill may never take one of these: a user who has a
        #: Claude Code plugin with a ``ralph`` skill must still get snowpea's
        #: ``/ralph``. Filled in by :func:`register_builtin_commands`.
        self.protected: frozenset[str] = frozenset()

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
        ctx = CommandContext(
            core=core,
            session=session,
            turn_id=turn_id,
            conn=conn,
            command_name=name,
            command_args=args,
        )
        # A command is a turn: it ends with ``turn.done`` below, so it opens
        # with ``turn.started`` too, carrying the line that was typed. That
        # line is what ``session.list`` shows for a session used only through
        # commands — one initialised with ``/deepinit`` and nothing else had
        # no prompt at all and the pickers dropped it as unprompted.
        await core.hub.emit_event(
            session.id, events.turn_started(turn_id, f"/{name} {args}".strip())
        )
        try:
            await command.run(ctx, args)
        except asyncio.CancelledError:
            ctx.set_history_summary(
                "Command was interrupted before it completed."
                + (
                    "\n\nPartial output:\n" + "\n\n".join(ctx.output_texts)
                    if ctx.output_texts
                    else ""
                )
            )
            await ctx.record_history(reason="interrupted")
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "interrupted"))
            raise
        except Exception as exc:  # noqa: BLE001 - a broken command ends its own turn
            log.exception("command /%s failed", name)
            ctx.set_history_summary(f"Command failed: {type(exc).__name__}: {exc}")
            await ctx.record_history(reason="error")
            await core.hub.emit_event(
                session.id, events.error(errors.INTERNAL, f"{type(exc).__name__}: {exc}")
            )
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "error"))
            return turn_id
        finally:
            session.current_turn = None
            # ``/init``, ``/deepinit`` and ``/skill create`` all leave a new
            # instruction file on disk.  Without this the very next turn still
            # runs on the environment block cached before the command
            # (CORE-context-files).
            if name in CONTEXT_WRITING_COMMANDS:
                from snowpea_core.agent.agent import invalidate_environment

                invalidate_environment()
        if not ctx.handled_turn:
            await ctx.record_history(reason="complete")
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
        delegation_cmd,
        effort_cmd,
        init_cmd,
        mcp_cmd,
        memory_cmd,
        mode_cmd,
        model_cmd,
        ralph,
        review_cmd,
        schedule_cmd,
        setup_cmd,
        skill_cmd,
        team_cmd,
        ultrawork,
        workers_cmd,
    )
    from snowpea_core.commands.builtin import COMMANDS

    for command in (
        *COMMANDS,
        *mcp_cmd.COMMANDS,
        *memory_cmd.COMMANDS,
        *mode_cmd.COMMANDS,
        *model_cmd.COMMANDS,
        *effort_cmd.COMMANDS,
        *delegation_cmd.COMMANDS,
        *backend_cmd.COMMANDS,
        *schedule_cmd.COMMANDS,
        *agent_cmd.COMMANDS,
        *delegate_cmd.COMMANDS,
        *skill_cmd.COMMANDS,
        # M7 workflows (contract §4): loops and fan-out live in Python.
        *ralph.COMMANDS,
        *review_cmd.COMMANDS,
        *ultrawork.COMMANDS,
        *deepinit.COMMANDS,
        *init_cmd.COMMANDS,
        *setup_cmd.COMMANDS,
        *team_cmd.COMMANDS,
        *workers_cmd.COMMANDS,
    ):
        registry.register(command)
    registry.protected = frozenset(command.name for command in registry.commands())
    return registry


__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "CommandRun",
    "register_builtin_commands",
]
