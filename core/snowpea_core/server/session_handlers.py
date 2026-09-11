"""RPC handlers for sessions, commands, tools, approvals and providers (US-005).

These live beside ``app_server`` rather than inside it so the daemon module
stays about process lifecycle.  ``register_session_handlers`` is called from
``build_dispatcher`` before the ``not_implemented`` fallbacks, and
``wire_core`` connects the singletons that ``Core`` creates empty.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from snowpea_core.agent import loop as agent_loop
from snowpea_core.commands.registry import register_builtin_commands
from snowpea_core.exec.factory import build_backend
from snowpea_core.memory import services as memory_services
from snowpea_core.memory import wire_memory
from snowpea_core.scheduler import wire_scheduler
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    ApprovalListResult,
    ApprovalRespondParams,
    BackendSetParams,
    CommandListResult,
    CommandRunParams,
    Empty,
    MemoryHit,
    MemorySearchParams,
    MemorySearchResult,
    MemoryWriteParams,
    MemoryWriteResult,
    Ok,
    OptionalSessionParams,
    ProviderConfigureParams,
    ProviderListResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionEvent,
    SessionIdParams,
    SessionListResult,
    SessionPromptParams,
    SessionResumeParams,
    SessionResumeResult,
    SessionSetModeParams,
    SessionSetModeResult,
    ToolListResult,
    TurnResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.session import events
from snowpea_core.session.store import Store
from snowpea_core.skills.loader import SkillLoader
from snowpea_core.tools import browser_providers, mcp_client
from snowpea_core.tools import media as media_tools
from snowpea_core.tools.registry import register_builtin_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.server.session")

#: Methods this module implements; the rest stay ``not_implemented`` at M1.
HANDLED_METHODS: tuple[str, ...] = (
    "session.create",
    "session.resume",
    "session.list",
    "session.close",
    "session.prompt",
    "session.interrupt",
    "session.setMode",
    "command.list",
    "command.run",
    "tool.list",
    "approval.list",
    "approval.respond",
    "provider.list",
    "backend.set",
    "memory.search",
    "memory.write",
)


def wire_core(core: Core) -> Core:
    """Give ``Core``'s collaborators the store, settings and hub they need."""
    core.store = Store.open(core.paths)
    core.sessions.bind(core.store, core.settings, core.hub)
    core.hub.bind(core.store, core.sessions)
    core.approvals.bind(core.settings, core.paths, core.hub)
    core.providers.bind(core.settings)
    register_builtin_tools(core.tools)
    wire_memory(core)
    wire_scheduler(core)
    media_tools.refresh_state(core)
    core.sessions.on_close.append(browser_providers.close_all_sessions)
    register_builtin_commands(core.commands)
    core.skills = SkillLoader(core)
    core.skills.load_sync()
    return core


def _session(core: Core, session_id: str) -> Session:
    session = core.sessions.get(session_id)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
    return session


def _count_sessions(core: Core) -> None:
    core.lifecycle.set_counter("sessions", len(core.sessions))


# ---------------------------------------------------------------------------
# session.*
# ---------------------------------------------------------------------------


async def session_create_handler(
    conn: RpcConnection, params: SessionCreateParams, core: Core
) -> SessionCreateResult:
    """``session.create`` — also subscribes the caller to the session's events."""
    session = await core.sessions.create(
        workdir=params.workdir,
        mode=params.mode,
        provider=params.provider,
        model=params.model,
        agent=params.agent,
        max_concurrent=params.maxConcurrent,
        origin_surface=params.originSurface or conn.surface_id,
        origin_conn=conn,
    )
    core.hub.subscribe(conn, session.id)
    _count_sessions(core)
    await mcp_client.sync_tools(core, session.workdir)
    return SessionCreateResult(sessionId=session.id)


async def session_resume_handler(
    conn: RpcConnection, params: SessionResumeParams, core: Core
) -> SessionResumeResult:
    """``session.resume`` — re-subscribe and replay events after ``afterSeq``."""
    session = _session(core, params.sessionId)
    core.hub.subscribe(conn, session.id)
    if session.origin_conn is None or getattr(session.origin_conn, "closed", False):
        session.origin_conn = conn
        session.origin_surface = conn.surface_id
    stored = (
        await core.store.events_after(session.id, params.afterSeq or 0)
        if core.store is not None
        else []
    )
    return SessionResumeResult(
        sessionId=session.id, events=[SessionEvent.model_validate(e) for e in stored]
    )


async def session_list_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> SessionListResult:
    return SessionListResult(sessions=core.sessions.list())


async def session_close_handler(_conn: RpcConnection, params: SessionIdParams, core: Core) -> Ok:
    closed = await core.sessions.close(params.sessionId)
    if not closed:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    _count_sessions(core)
    return Ok(ok=True)


async def session_prompt_handler(
    conn: RpcConnection, params: SessionPromptParams, core: Core
) -> TurnResult:
    """``session.prompt`` — slash text becomes a command, anything else a turn."""
    session = _session(core, params.sessionId)
    core.hub.subscribe(conn, session.id)
    text = params.text
    if params.attachments:
        extra = "\n".join(
            part for part in (_attachment_text(a) for a in params.attachments) if part
        )
        if extra:
            text = f"{text}\n\n{extra}" if text else extra
    parsed = core.commands.parse(text)
    if parsed is not None:
        name, args = parsed
        return TurnResult(turnId=core.commands.start(core, session, name, args, conn))
    unattended = session.origin_conn is None
    return TurnResult(turnId=agent_loop.start_turn(core, session, text, unattended=unattended))


def _attachment_text(attachment: object) -> str:
    text = getattr(attachment, "text", None)
    if text:
        return str(text)
    path = getattr(attachment, "path", None)
    return f"[attachment: {path}]" if path else ""


async def session_interrupt_handler(
    _conn: RpcConnection, params: SessionIdParams, core: Core
) -> Ok:
    session = _session(core, params.sessionId)
    session.interrupt.set()
    return Ok(ok=True)


async def session_set_mode_handler(
    _conn: RpcConnection, params: SessionSetModeParams, core: Core
) -> SessionSetModeResult:
    from snowpea_core.session import events

    session = _session(core, params.sessionId)
    mode = await core.sessions.set_mode(session, params.mode)
    await core.hub.emit_event(session.id, events.mode_changed(mode))
    return SessionSetModeResult(mode=mode)


# ---------------------------------------------------------------------------
# command.* / tool.*
# ---------------------------------------------------------------------------


async def command_list_handler(
    _conn: RpcConnection, params: OptionalSessionParams, core: Core
) -> CommandListResult:
    session = core.sessions.get(params.sessionId) if params.sessionId else None
    return CommandListResult(commands=core.commands.list(session))


async def command_run_handler(
    conn: RpcConnection, params: CommandRunParams, core: Core
) -> TurnResult:
    session = _session(core, params.sessionId)
    core.hub.subscribe(conn, session.id)
    name = params.name[1:] if params.name.startswith("/") else params.name
    return TurnResult(turnId=core.commands.start(core, session, name, params.args, conn))


async def tool_list_handler(
    _conn: RpcConnection, params: OptionalSessionParams, core: Core
) -> ToolListResult:
    """``tool.list`` — MCP servers are synced first so their tools are listed.

    Without a session there is no workdir, so the daemon's own directory is
    used; that is what ``snowpea tools list`` means by "here".
    """
    session = core.sessions.get(params.sessionId) if params.sessionId else None
    await mcp_client.sync_tools(core, session.workdir if session else Path.cwd())
    return ToolListResult(tools=core.tools.list(session))


# ---------------------------------------------------------------------------
# approval.* / provider.*
# ---------------------------------------------------------------------------


async def approval_list_handler(
    _conn: RpcConnection, params: OptionalSessionParams, core: Core
) -> ApprovalListResult:
    return ApprovalListResult(requests=core.approvals.list(params.sessionId))


async def approval_respond_handler(
    conn: RpcConnection, params: ApprovalRespondParams, core: Core
) -> Ok:
    await core.approvals.respond(
        params.requestId,
        params.decision,
        params.scope,
        by=conn.surface_id,
        conn=conn,
    )
    return Ok(ok=True)


async def backend_set_handler(_conn: RpcConnection, params: BackendSetParams, core: Core) -> Ok:
    """``backend.set`` — swap where a session's tools run, closing the old backend."""
    session = _session(core, params.sessionId)
    try:
        backend = build_backend(
            params.kind,
            params.config,
            workdir=session.workdir,
            session_id=session.id,
        )
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    await session.set_backend(backend)
    await core.hub.emit_event(session.id, events.backend_changed(params.kind))
    log.info("session %s backend is now %s", session.id, params.kind)
    return Ok(ok=True)


async def provider_list_handler(
    _conn: RpcConnection, _params: Empty, core: Core
) -> ProviderListResult:
    return ProviderListResult(providers=core.providers.list())


async def provider_configure_handler(
    _conn: RpcConnection, params: ProviderConfigureParams, core: Core
) -> Ok:
    """``provider.configure`` — vendor ``media`` flips the media tools live."""
    if params.vendor == "media":
        state = await media_tools.configure(core, params.config)
        log.info("media tools are now %s", state)
        return Ok(ok=True)
    raise RpcError(
        errors.NOT_IMPLEMENTED,
        f"provider.configure does not handle vendor {params.vendor!r} yet",
    )


# ---------------------------------------------------------------------------
# memory.* (M5 contract §1)
# ---------------------------------------------------------------------------


async def memory_search_handler(
    _conn: RpcConnection, params: MemorySearchParams, core: Core
) -> MemorySearchResult:
    """``memory.search`` — FTS5 recall inside one namespace, best first."""
    entries = await memory_services(core).store.search(
        params.query,
        namespace=params.namespace or "default",
        limit=params.limit,
    )
    return MemorySearchResult(
        hits=[
            MemoryHit(id=entry.id, text=entry.text, tags=entry.tags, score=entry.score)
            for entry in entries
        ]
    )


async def memory_write_handler(
    _conn: RpcConnection, params: MemoryWriteParams, core: Core
) -> MemoryWriteResult:
    """``memory.write`` — store one memory and return its id."""
    text = params.text.strip()
    if not text:
        raise RpcError(errors.INVALID_PARAMS, "memory.write needs a non-empty text")
    entry = await memory_services(core).store.write(
        text, tags=params.tags, namespace=params.namespace or "default"
    )
    return MemoryWriteResult(id=entry.id)


def register_session_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("session.create", session_create_handler)
    dispatcher.register("session.resume", session_resume_handler)
    dispatcher.register("session.list", session_list_handler)
    dispatcher.register("session.close", session_close_handler)
    dispatcher.register("session.prompt", session_prompt_handler)
    dispatcher.register("session.interrupt", session_interrupt_handler)
    dispatcher.register("session.setMode", session_set_mode_handler)
    dispatcher.register("command.list", command_list_handler)
    dispatcher.register("command.run", command_run_handler)
    dispatcher.register("tool.list", tool_list_handler)
    dispatcher.register("approval.list", approval_list_handler)
    dispatcher.register("approval.respond", approval_respond_handler)
    dispatcher.register("provider.list", provider_list_handler)
    dispatcher.register("backend.set", backend_set_handler)
    dispatcher.register("memory.search", memory_search_handler)
    dispatcher.register("memory.write", memory_write_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_session_handlers", "wire_core"]
