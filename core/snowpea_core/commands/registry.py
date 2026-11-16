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

from snowpea_core.providers.base import ChatMessage
from snowpea_core.server import errors
from snowpea_core.server.protocol import CommandInfo, CommandSource, ItemFormat
from snowpea_core.session import events
from snowpea_core.util.redaction import redact_text

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
log = logging.getLogger("snowpea.commands")


def _redact_command_text(text: str) -> str:
    """Mask secret-looking command args/output before persisting history."""
    return redact_text(text)


def _clip_command_history(text: str) -> str:
    """Keep both the beginning and final status when a command is verbose."""
    if len(text) <= MAX_COMMAND_HISTORY_CHARS:
        return text
    head = text[:MAX_COMMAND_HISTORY_HEAD_CHARS].rstrip()
    tail = text[-MAX_COMMAND_HISTORY_TAIL_CHARS:].lstrip()
    return f"{head}\n…[command output truncated]\n{tail}"


def _refuse_local(conn: Any, core: Any, name: str, args: str, source: str, level: str) -> None:
    """A command stands for the RPC it performs (security review 6, M-2).

    Refused to a connection that is not a local person's: a ``local`` command
    (``/update``, ``/desktop``, ``/allow``), and any command whose RPC twin
    (``persists.COMMAND_TWINS``) the dispatcher's tables refuse to this kind of
    connection — so ``/update`` reaches ``system.update`` for nobody
    ``system.update`` itself would refuse.
    """
    from snowpea_core.commands.persists import twins
    from snowpea_core.server import settings_gate
    from snowpea_core.server.errors import RpcError
    from snowpea_core.server.person_gate import method_refusal

    refused = level == "local"
    for method in () if refused else twins(name, args, source):
        try:
            method_refusal(method, conn)
            settings_gate.kind_method_refusal(method, {}, conn, None)
        except RpcError:
            refused = True
            break
    if refused:
        settings_gate.ledger_settings(core, conn, [f"/{name}"], refused=True, method="command.run")
        settings_gate.refuse(conn, f"run /{name}", reason=settings_gate.LOCAL_ONLY,
                             extra={"what": "command"})


def gate_command(
    core: Any,
    session: Any,
    name: str,
    args: str,
    *,
    conn: Any = None,
    actor: str | None = None,
    caller: str | None = None,
    source: str = "builtin",
) -> None:
    """Refuse a command its caller may not start (``jarvis_person_only``).

    * a browser: the per-command rule of ``command.run`` (``settings_gate.
      command_refusal``: a persisting command needs ``actor: "person"``);
    * a connector: nothing that persists;
    * every connection but a local person's (a remote web login, a browser with
      or without the marker, a connector): no ``local`` command and no command
      whose RPC twin that connection may not call (:func:`_refuse_local`,
      ``details.reason: "local_only"``);
    * anyone: a command that raises the session's mode needs a person
      (``session/mode_guard.py``, ``details.reason: "mode_raise"``);
    * no connection: ``caller="messenger"`` (the chat's bound person) and
      ``caller="internal"`` (a handler that judged its own caller) are allowed;
      an unknown in-process caller may run only what persists nothing and
      raises no mode.
    """
    from snowpea_core.commands.mode_cmd import mode_target
    from snowpea_core.commands.persists import persists
    from snowpea_core.server import errors
    from snowpea_core.server.errors import RpcError
    from snowpea_core.server.person_gate import connection_kind, snapshot
    from snowpea_core.server.settings_gate import command_refusal, refuse_closed
    from snowpea_core.session import mode_guard

    what = persists(name, args, source)
    target = mode_target(name, args)
    current = "auto" if getattr(session, "mode", None) == "plan" and getattr(
        session, "auto_after_plan", False) else getattr(session, "mode", "accept")
    if conn is not None:
        # Security review 7, H-1: ``conn`` may be the snapshot taken when the
        # command was accepted (a queued command is gated again when it runs,
        # by that snapshot); a connection that has closed is nobody.
        who = snapshot(conn, actor, caller=caller)
        conn, actor = who, who.actor
        kind = connection_kind(conn)
        refuse_closed(conn, f"run /{name}")
        if kind != "person":
            _refuse_local(conn, core, name, args, source, what.level)
        command_refusal(conn, core, name, args, source=source, actor=actor)
        if kind == "connector" and what.level != "session":
            raise RpcError(errors.JARVIS_PERSON_ONLY, f"a connector cannot run /{name}",
                           {"reason": "person_only", "kind": kind})
        mode_caller = mode_guard.ModeCaller.from_conn(conn, actor)
    elif caller in ("messenger", "internal"):
        mode_caller = mode_guard.ModeCaller.internal(f"command-{caller}")
    else:
        if what.level != "session":
            raise RpcError(errors.JARVIS_PERSON_ONLY,
                           f"/{name} changes settings; only a person may run it",
                           {"reason": "person_only", "kind": "none"})
        mode_caller = mode_guard.NOBODY
    settings = getattr(core, "settings", None)
    if target is not None:
        mode_guard.check(mode_caller, session, current, target, settings=settings)
    else:
        # web.remote.maxMode (security review 7): a remote login starts no work
        # (a ``turn`` command) in a session above it; a read or a setting
        # (``instant``) and a mode command that lowers it stay open.
        from snowpea_core.commands.kinds import TURN, command_kind

        if command_kind(name, args, source) == TURN:
            mode_guard.check_remote_max(mode_caller.kind, current, settings, f"running /{name}")
    if name == "mode" and (args or "").strip().lower() == "save":
        sessions = getattr(core, "sessions", None)
        if sessions is not None:
            mode_guard.check(mode_caller, None, sessions.default_mode(session.workdir),
                             session.mode, settings=settings)


@dataclass
class CommandContext:
    """What a command may reach."""

    core: Core
    session: Session
    turn_id: str
    conn: Any = None
    command_name: str = ""
    command_args: str = ""
    #: A browser's ``actor: "person"`` marker on ``command.run`` (1.54.0).
    actor: str | None = None
    #: With no connection: who started it (``"messenger"``, ``"internal"``).
    caller: str | None = None
    #: Who started it, as it was when the command was accepted
    #: (``person_gate.CallerSnapshot``, security review 7 H-1): what a command
    #: judges its caller by — never ``conn``, which may have closed since.
    who: Any = None
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
    #: An instant command running beside a model turn (1.55.0): its replies
    #: are collected for one ``command.result`` and never enter the history.
    beside_turn: bool = False
    _history_recorded: bool = False
    _history_started: bool = False

    def __post_init__(self) -> None:
        if self.who is None:
            from snowpea_core.server.person_gate import snapshot

            self.who = snapshot(self.conn, self.actor, caller=self.caller)

    async def emit(self, event: events.Event) -> None:
        await self.core.hub.emit_event(self.session.id, event)

    async def say(self, text: str) -> None:
        """Answer the user with a completed assistant message."""
        self.output_texts.append(text)
        if self.beside_turn:
            return
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
        if self._history_recorded or self.beside_turn:
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
    #: ``snowpea`` | ``claude`` | ``codex`` (1.52.0): what a skill command was written for.
    format: ItemFormat = "snowpea"

    def info(self) -> CommandInfo:
        return CommandInfo(
            name=self.name,
            summary=self.summary,
            argsSchema=self.args_schema,
            source=self.source,
            format=self.format,
        )


#: Aliases so annotations below still mean the builtin ``list``.
CommandInfos = list[CommandInfo]
Commands = list["Command"]


class CommandRegistry:
    """Name -> :class:`Command` lookup plus ``/foo bar`` parsing."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        #: Instant commands running beside a turn, held so they are not collected.
        self._beside: set[asyncio.Task[None]] = set()
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
        self,
        core: Core,
        session: Session,
        name: str,
        args: str = "",
        conn: Any = None,
        *,
        origin: str | None = None,
        actor: str | None = None,
        caller: str | None = None,
        when_busy: str | None = None,
    ) -> str:
        """Schedule a command in the background and return its turn id.

        While a model turn runs (1.55.0, ``commands/kinds.py``): an ``instant``
        command runs beside it without touching the turn's bookkeeping and
        reports with ``command.result`` (the id returned is the command's);
        a ``turn`` command is queued behind it like a prompt (``turn.queued``;
        ``when_busy`` ``"steer"`` queues too — a command cannot be folded into
        a running request).  With no turn running, every command is a turn.

        ``origin`` says whose words the command line is (``agent.loop.ORIGINS``);
        a turn the command runs inherits it.  ``actor`` is a browser's
        ``actor: "person"`` marker (default: the current RPC call's).

        The ONE gate for every way a command starts (``command.run``, slash text
        in ``session.prompt``, a messenger message, a skill draft): who asks is
        ``conn`` (with ``actor``), or, with no connection, ``caller`` — an
        in-process identity (``"messenger"``: the bound person of a chat;
        ``"internal"``: a handler that already judged its own caller).  A
        refusal raises before anything runs, so the text never reaches the
        model either.  See :func:`gate_command`.
        """
        if actor is None:
            from snowpea_core.server.rpc import current_actor

            actor = current_actor()
        source = getattr(self._commands.get(name), "source", "builtin")
        from snowpea_core.server.person_gate import snapshot

        # Security review 7, H-1: who asks is fixed NOW; the command — run at
        # once, beside a turn or queued behind one — is judged by this snapshot.
        who = snapshot(conn, actor, caller=caller, origin=origin or "human")
        gate_command(core, session, name, args, conn=who if conn is not None else None,
                     actor=actor, caller=caller, source=source)
        from snowpea_core.agent import loop as agent_loop
        from snowpea_core.commands.kinds import INSTANT, command_kind

        running = session.turn_task
        busy = running is not None and not running.done()
        if busy and command_kind(name, args, source) == INSTANT:
            command_id = f"c-{uuid.uuid4().hex[:12]}"
            side = asyncio.ensure_future(
                self.run_beside(core, session, name, args, conn, command_id,
                                actor=actor, caller=caller, who=who)
            )
            self._beside.add(side)
            side.add_done_callback(self._beside.discard)
            return command_id
        turn_id = f"t-{uuid.uuid4().hex[:12]}"
        if origin:
            agent_loop.note_origin(session, turn_id, origin)
        queued = agent_loop.QueuedTurn(
            turn_id=turn_id,
            text=f"/{name} {args}".strip(),
            unattended=session.origin_conn is None,
            attachments=[],
            origin=origin or "human",
            command=agent_loop.QueuedCommand(name, args, conn, actor, caller, who=who),
        )
        agent_loop.note_caller(session, turn_id, who)
        if busy:
            # A command that starts work waits for the running turn, like a
            # prompt; it is never folded into it, whatever ``when_busy`` says.
            session.queued_turns.append(queued)
            waiting = len(session.queued_turns)
            agent_loop._emit_soon(core, session, events.turn_queued(turn_id, waiting, waiting))
            return turn_id
        session.current_turn = turn_id
        session.turn_task = asyncio.ensure_future(agent_loop.drain_turns(core, session, queued))
        return turn_id

    async def run_beside(
        self,
        core: Core,
        session: Session,
        name: str,
        args: str,
        conn: Any,
        command_id: str,
        *,
        actor: str | None = None,
        caller: str | None = None,
        who: Any = None,
    ) -> None:
        """Run an instant command beside a running turn; one ``command.result`` (1.55.0).

        No ``turn.started``/``turn.done``/``message.done``, no history and no
        change to ``current_turn``/``turn_task``: the running turn stays the
        session's turn, interruptible and ``running`` in ``session.list``.
        """
        from snowpea_core.commands.kinds import applies_from

        line = f"/{name} {args}".strip()
        running_turn = session.current_turn
        command = self._commands.get(name)
        if command is None:
            await core.hub.emit_event(
                session.id,
                events.command_result(command_id, line, False, f"unknown command: /{name}",
                                      running_turn_id=running_turn),
            )
            return
        ctx = CommandContext(
            core=core, session=session, turn_id=command_id, conn=conn, command_name=name,
            command_args=args, actor=actor, caller=caller, beside_turn=True, who=who,
        )
        ok = True
        try:
            await command.run(ctx, args)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken command reports, never ends the turn
            # Security review 7, Low 5: the event reaches every subscriber; the
            # exception's text stays in the log under the id.
            error_id = errors.new_error_id()
            log.exception("command /%s failed beside turn %s (error id %s)", name,
                          running_turn, error_id)
            ok = False
            ctx.output_texts.append(f"Command failed: {errors.internal_text(error_id)}")
        await core.hub.emit_event(
            session.id,
            events.command_result(
                command_id,
                line,
                ok,
                "\n\n".join(text for text in ctx.output_texts if text.strip()),
                applies_from=applies_from(name, args) if ok else None,
                running_turn_id=running_turn,
            ),
        )

    async def run(
        self,
        core: Core,
        session: Session,
        name: str,
        args: str = "",
        conn: Any = None,
        turn_id: str | None = None,
        *,
        actor: str | None = None,
        caller: str | None = None,
        who: Any = None,
    ) -> str:
        """Run a command to completion, emitting its ``turn.done``.

        ``who`` is the caller snapshot :meth:`start` took: with it the command
        is gated again here, as it starts (security review 7, H-1) — what was
        allowed when it was queued may not be when its turn comes (Jarvis mode
        turned on, the session's mode, a limit set meanwhile).
        """
        turn_id = turn_id or f"t-{uuid.uuid4().hex[:12]}"
        command = self._commands.get(name)
        if who is not None and command is not None:
            from snowpea_core.server.errors import RpcError

            try:
                gate_command(core, session, name, args,
                             conn=who if who.kind != "none" else None, actor=who.actor,
                             caller=who.caller, source=command.source)
            except RpcError as exc:
                line = f"/{name} {args}".strip()
                await core.hub.emit_event(session.id, events.turn_started(turn_id, line))
                await core.hub.emit_event(
                    session.id, events.error(exc.code, str(exc.message), exc.data))
                await core.hub.emit_event(session.id, events.turn_done(turn_id, "error"))
                if session.current_turn == turn_id:
                    session.current_turn = None
                return turn_id
        if command is None:
            # Open and close the turn like any other: ``start`` already made it
            # the session's current turn, and a client that saw ``turn.done``
            # without a ``turn.started`` (an IDE, a browser) never closes the
            # turn it opened locally, so it keeps spinning for ever while the
            # TUI shows "Stopped".  Clearing ``current_turn`` matters just as
            # much: ``session.list`` reads it as "running".
            await core.hub.emit_event(
                session.id, events.turn_started(turn_id, f"/{name} {args}".strip())
            )
            await core.hub.emit_event(
                session.id, events.error(errors.NOT_FOUND, f"unknown command: /{name}")
            )
            await core.hub.emit_event(session.id, events.turn_done(turn_id, "error"))
            if session.current_turn == turn_id:
                session.current_turn = None
            return turn_id
        ctx = CommandContext(
            core=core,
            session=session,
            turn_id=turn_id,
            conn=conn,
            command_name=name,
            command_args=args,
            actor=actor,
            caller=caller,
            who=who,
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
        except Exception:  # noqa: BLE001 - a broken command ends its own turn
            # Security review 7, Low 5: an id for everyone, the text in the log.
            error_id = errors.new_error_id()
            log.exception("command /%s failed (error id %s)", name, error_id)
            ctx.set_history_summary(f"Command failed: {errors.internal_text(error_id)}")
            await ctx.record_history(reason="error")
            await core.hub.emit_event(
                session.id,
                events.error(errors.INTERNAL, errors.internal_text(error_id),
                             {"errorId": error_id}),
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
    """Register the built-ins: ``help``/``tools``, modes, ``/agent``,
    ``/mcp`` and ``/skill``."""
    from snowpea_core.commands import (
        agent_cmd,
        deepinit,
        delegate_cmd,
        delegation_cmd,
        effort_cmd,
        goal_cmd,
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
        vision_cmd,
        workers_cmd,
    )
    from snowpea_core.commands.builtin import COMMANDS

    for command in (
        *COMMANDS,
        *mcp_cmd.COMMANDS,
        *memory_cmd.COMMANDS,
        *mode_cmd.COMMANDS,
        *model_cmd.COMMANDS,
        *vision_cmd.COMMANDS,
        *goal_cmd.COMMANDS,
        *effort_cmd.COMMANDS,
        *delegation_cmd.COMMANDS,
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
