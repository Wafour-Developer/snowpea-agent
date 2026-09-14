"""RPC handlers for sessions, commands, tools, approvals and providers (US-005).

These live beside ``app_server`` rather than inside it so the daemon module
stays about process lifecycle.  ``register_session_handlers`` is called from
``build_dispatcher`` before the ``not_implemented`` fallbacks, and
``wire_core`` connects the singletons that ``Core`` creates empty.
"""

from __future__ import annotations

import functools
import logging
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from snowpea_core.agent import loop as agent_loop
from snowpea_core.attachments import pending
from snowpea_core.attachments.model import Attachment as FileAttachment
from snowpea_core.attachments.model import AttachmentError
from snowpea_core.attachments.store import AttachmentStore
from snowpea_core.commands.registry import register_builtin_commands
from snowpea_core.exec.factory import build_backend
from snowpea_core.lsp import wire_lsp
from snowpea_core.memory import services as memory_services
from snowpea_core.memory import wire_memory
from snowpea_core.memory.retrieval import agent_namespace_of, project_namespace_of
from snowpea_core.memory.scopes import GLOBAL_NAMESPACE, project_namespace
from snowpea_core.providers.base import ProviderError
from snowpea_core.scheduler import wire_scheduler
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    ApprovalListResult,
    ApprovalRespondParams,
    Attachment,
    BackendSetParams,
    CommandListResult,
    CommandRunParams,
    Empty,
    MemoryDeleteParams,
    MemoryEntryInfo,
    MemoryHit,
    MemoryListParams,
    MemoryListResult,
    MemoryScope,
    MemorySearchParams,
    MemorySearchResult,
    MemoryWriteParams,
    MemoryWriteResult,
    Ok,
    OptionalSessionParams,
    ProviderConfigureParams,
    ProviderListResult,
    ProviderModelsParams,
    ProviderModelsResult,
    QuestionListResult,
    QuestionRespondParams,
    SessionCompactParams,
    SessionCompactResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionDeleteParams,
    SessionDeleteResult,
    SessionEvent,
    SessionIdParams,
    SessionListParams,
    SessionListResult,
    SessionPromptParams,
    SessionResumeParams,
    SessionResumeResult,
    SessionSetModelParams,
    SessionSetModelResult,
    SessionSetModeParams,
    SessionSetModeResult,
    SessionSummary,
    ToolListResult,
    TurnResult,
)
from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
from snowpea_core.session import events
from snowpea_core.session.history import message_from_json, message_text
from snowpea_core.session.store import Store
from snowpea_core.skills.loader import SkillLoader
from snowpea_core.tools import audio_tools, browser_providers, mcp_client, web
from snowpea_core.tools import media as media_tools
from snowpea_core.tools.registry import register_builtin_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.server.session")

#: Attachment failures onto wire error codes.  A file that is too large is a
#: bad request, not a server fault, so both map to ``invalid_params``; the
#: message names the limit.
_ATTACHMENT_CODES: dict[str, str] = {
    "invalid_params": errors.INVALID_PARAMS,
    "attachment_too_large": errors.INVALID_PARAMS,
    "internal": errors.INTERNAL,
}

#: ``$reviewer look at this diff`` — the short way to hand one prompt to an
#: agent (contract §9, CORE-us020).  Mirrors ``tui/src/state/delegation.ts``'s
#: ``DelegationHint`` regex so the shorthand a client highlights while it is
#: typed is exactly the shorthand the daemon rewrites once it is sent: the
#: name needs a space after it to count, so a bare ``$foo`` with nothing typed
#: yet does not misfire.
_DELEGATE_PREFIX = re.compile(r"^\$([A-Za-z0-9][\w.-]*)(?:\s+([\s\S]*))?$")

#: Methods this module implements; the rest stay ``not_implemented`` at M1.
HANDLED_METHODS: tuple[str, ...] = (
    "session.create",
    "session.resume",
    "session.list",
    "session.close",
    "session.prompt",
    "session.interrupt",
    "session.compact",
    "session.setMode",
    "session.setModel",
    "command.list",
    "command.run",
    "tool.list",
    "approval.list",
    "approval.respond",
    "question.list",
    "question.respond",
    "provider.list",
    "provider.models",
    "backend.set",
    "memory.search",
    "memory.write",
    "memory.list",
    "memory.delete",
)


def _definition_model(core: Core, agent: str, workdir: Path) -> str | None:
    """``model:`` from the agent definition named ``agent``, visible at ``workdir``."""
    from snowpea_core.commands.agent_cmd import definitions_for

    for definition in definitions_for(core, workdir):
        if definition.name == agent:
            return definition.model or None
    return None


def wire_core(core: Core) -> Core:
    """Give ``Core``'s collaborators the store, settings and hub they need."""
    core.store = Store.open(core.paths)
    core.sessions.bind(core.store, core.settings, core.hub)
    core.sessions.definition_model_for = functools.partial(_definition_model, core)
    core.hub.bind(core.store, core.sessions)
    core.approvals.bind(core.settings, core.paths, core.hub)
    core.questions.bind(core.settings, core.hub)
    core.providers.bind(core.settings, core.paths)
    # /model and the lazy model auto-pick persist through the registry; the
    # hook keeps that write from looking like an outside edit next turn.
    core.providers.on_saved = core.mark_settings_saved
    register_builtin_tools(core.tools)
    wire_lsp(core)
    wire_memory(core)
    wire_scheduler(core)
    media_tools.refresh_state(core)
    audio_tools.refresh_state(core)
    core.sessions.on_close.append(browser_providers.close_all_sessions)
    register_builtin_commands(core.commands)
    core.skills = SkillLoader(core)
    core.skills.load_sync()
    from snowpea_core.skills import registry_client

    registry_client.configure_client(core.settings)
    return core


async def _refresh_settings(core: Core) -> None:
    """Adopt an outside edit to ``settings.json`` before the turn reads it.

    ``snowpea setup`` writes the file from its own process, so without this a
    running daemon would keep sending the model it was started with
    (CORE-settings-reload).  The check is one ``os.stat``; the re-read and the
    rebind only happen when the file really moved.
    """
    reload_settings = getattr(core, "reload_settings", None)
    if reload_settings is None:  # pragma: no cover - a core without it is a double
        return
    try:
        await reload_settings()
    except Exception:  # noqa: BLE001 - a bad settings file must not cost a turn
        log.warning("could not reload settings.json", exc_info=True)


async def _load_project_skills(core: Core, workdir: Path) -> None:
    """Register this project's skills, commands and agents before the first turn.

    The daemon scans every workdir it knows at boot, but a project it has never
    opened a session in — a new checkout, or one the IDE just added — is not in
    that list, so its ``/commands`` would only appear after the next full
    reload.  One incremental scan here closes that gap (M15 §B5b).
    """
    skills = getattr(core, "skills", None)
    reload_workdir = getattr(skills, "reload_workdir", None)
    if reload_workdir is None:
        return
    try:
        await reload_workdir(workdir)
    except Exception:  # noqa: BLE001 - a bad skill file must not fail session.create
        log.warning("could not load project skills from %s", workdir, exc_info=True)


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
    await _refresh_settings(core)
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
    await _load_project_skills(core, session.workdir)
    await mcp_client.sync_tools(core, session.workdir)
    return SessionCreateResult(sessionId=session.id)


async def session_resume_handler(
    conn: RpcConnection, params: SessionResumeParams, core: Core
) -> SessionResumeResult:
    """``session.resume`` — re-subscribe and replay events after ``afterSeq``."""
    session = core.sessions.get(params.sessionId)
    if session is None:
        session = await core.sessions.restore(params.sessionId, origin_conn=conn)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {params.sessionId}")
    await _load_project_skills(core, session.workdir)
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
    _conn: RpcConnection, params: SessionListParams, core: Core
) -> SessionListResult:
    live = core.sessions.list()
    if not params.includeClosed or core.store is None:
        rows = live
    else:
        by_id = {row.sessionId: row for row in live}
        for stored in await core.store.list_sessions(include_closed=True):
            session_id = str(stored["id"])
            if session_id in by_id:
                continue
            by_id[session_id] = SessionSummary(
                sessionId=session_id,
                workdir=str(stored["workdir"]),
                mode=stored["mode"],
                provider=stored.get("provider"),
                model=stored.get("model"),
                originSurface=stored.get("origin_surface"),
                createdAt=str(stored["created_at"]),
                seq=await core.store.max_seq(session_id),
                kind=stored.get("kind") or "chat",
                parentSessionId=stored.get("parent_session_id"),
                jobId=stored.get("job_id"),
            )
        rows = list(by_id.values())
    if params.workdir:
        rows = [row for row in rows if row.workdir == params.workdir]
    if params.kinds is not None:
        wanted = set(params.kinds)
        rows = [row for row in rows if row.kind in wanted]
    if core.store is not None:
        enriched = []
        for row in rows:
            messages = await core.store.messages(row.sessionId)
            latest = next((item for item in reversed(messages) if item["role"] == "user"), None)
            prompt = (
                message_text(message_from_json("user", latest["content"]))
                if latest is not None
                else None
            )
            enriched.append(row.model_copy(update={"lastPrompt": prompt}))
        rows = enriched
    return SessionListResult(sessions=sorted(rows, key=lambda row: row.createdAt, reverse=True))


async def session_delete_saved_handler(
    _conn: RpcConnection, params: SessionDeleteParams, core: Core
) -> SessionDeleteResult:
    if core.store is None:
        return SessionDeleteResult()
    live_ids = {row.sessionId for row in core.sessions.list()}
    stored = await core.store.list_sessions(include_closed=True)
    ids = [
        str(row["id"])
        for row in stored
        if str(row["id"]) not in live_ids
        and (
            params.all
            or (params.sessionId and row["id"] == params.sessionId)
            or (params.workdir and row["workdir"] == params.workdir)
        )
    ]
    deleted = await core.store.delete_sessions(ids)
    for session_id in ids:
        _purge_session_files(core, session_id)
    return SessionDeleteResult(deleted=deleted)


def _purge_session_files(core: Core, session_id: str) -> None:
    """Remove the on-disk bytes a deleted session owned.

    Deleting a session used to leave ``<home>/attachments/<id>/`` and
    ``<home>/audio/<id>/`` behind forever: an unbounded disk leak, and a
    privacy surprise for a user who deleted a thread because of what they
    had pasted into it (CORE-fixes-v017 R4).  Best effort — the rows are
    already gone, and a file that will not delete must not fail the RPC.
    """
    try:
        AttachmentStore(core.paths.attachments_dir).purge(session_id)
    except OSError:
        log.warning("could not purge attachments for %s", session_id, exc_info=True)
    try:
        audio = core.paths.audio_dir / session_id
        if audio.is_dir():
            shutil.rmtree(audio, ignore_errors=True)
    except OSError:  # pragma: no cover - rmtree already ignores errors
        log.warning("could not purge audio for %s", session_id, exc_info=True)


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
    await _refresh_settings(core)
    session = _session(core, params.sessionId)
    core.hub.subscribe(conn, session.id)
    text = params.text
    _stored, inline_text = _accept_attachments(core, session.id, params.attachments)
    if inline_text:
        text = f"{text}\n\n{inline_text}" if text else inline_text
    # No marker is added to the text: the turn's content blocks carry one per
    # attachment (``[image: shot.png]``), so history, resume and a text-only
    # model all see it without the prompt being rewritten here.
    parsed = core.commands.parse(text)
    if parsed is not None:
        name, args = parsed
        # A slash command is not a model turn; nothing would consume the bytes.
        pending.clear(session.id)
        return TurnResult(turnId=core.commands.start(core, session, name, args, conn))
    delegate_match = _DELEGATE_PREFIX.match(text) if text else None
    if delegate_match is not None and delegate_match.group(2) is not None:
        # Same rewrite as ``/delegate <agent> <task>`` (CORE-us020): the daemon
        # parses the prefix, not any one client, so the TUI, the SDK and a
        # scheduled job all get the same reply-with-outcome behaviour.
        pending.clear(session.id)
        args = f"{delegate_match.group(1)} {delegate_match.group(2)}"
        return TurnResult(turnId=core.commands.start(core, session, "delegate", args, conn))
    unattended = session.origin_conn is None
    return TurnResult(turnId=agent_loop.start_turn(core, session, text, unattended=unattended))


def _accept_attachments(
    core: Core, session_id: str, attachments: Sequence[Attachment] | None
) -> tuple[list[FileAttachment], str]:
    """Validate, persist and stash a prompt's attachments.

    Returns the stored attachments and the text of any ``kind="text"`` ones,
    which are inlined into the prompt rather than stored.  Inline bytes are
    written under ``<home>/attachments/<session>/`` so a resumed session can
    still find them; a file the user pointed at is left where it is.
    """
    if not attachments:
        pending.clear(session_id)
        return [], ""
    store = AttachmentStore(core.paths.attachments_dir)
    stored: list[FileAttachment] = []
    inline: list[str] = []
    for entry in attachments:
        if entry.kind == "text" or (entry.text and not entry.path and not entry.data):
            if entry.text:
                label = f"[{entry.name}]\n" if entry.name else ""
                inline.append(f"{label}{entry.text}")
            continue
        try:
            built = FileAttachment.from_payload(
                {
                    "name": entry.name or (Path(entry.path).name if entry.path else "attachment"),
                    "mime": entry.mimeType,
                    "path": entry.path,
                    "data": entry.data,
                }
            )
            stored.append(store.save(session_id, built))
        except AttachmentError as exc:
            code = _ATTACHMENT_CODES.get(exc.code, errors.INVALID_PARAMS)
            raise RpcError(code, str(exc)) from exc
    pending.stash(session_id, stored)
    return stored, "\n\n".join(inline)


async def session_interrupt_handler(
    _conn: RpcConnection, params: SessionIdParams, core: Core
) -> Ok:
    """``session.interrupt`` — stop the running turn *and* the queue behind it.

    Flushing the queue is part of Stop: a user who queued three follow-ups and
    then interrupted used to watch all three run anyway (CORE-fixes-v017 R3).
    Each dropped prompt is announced as ``turn.dequeued`` + ``turn.done``.
    """
    session = _session(core, params.sessionId)
    session.interrupt.set()
    await agent_loop.flush_queued_turns(core, session)
    return Ok(ok=True)


async def session_compact_handler(
    conn: RpcConnection, params: SessionCompactParams, core: Core
) -> SessionCompactResult:
    """``session.compact`` — the RPC half of ``/compact`` (CORE-context).

    Same code path as the command, so the TUI, headless and IDE surfaces all
    get the same summary, the same events and the same numbers.
    """
    from snowpea_core.session import compaction

    session = _session(core, params.sessionId)
    # Compaction happens between turns, never inside one: replacing the history
    # under a running tool loop would strand a pending tool result (CORE-context).
    # The /compact command cannot hit this — it *is* the turn.
    turn_task = session.turn_task
    if turn_task is not None and not turn_task.done():
        raise RpcError(
            errors.INVALID_PARAMS,
            f"{params.sessionId} has a turn in flight; interrupt it or wait, then compact",
        )
    core.hub.subscribe(conn, session.id)
    result = await compaction.compact_session(core, session, params.instructions)
    return SessionCompactResult(
        before=result.before, after=result.after, summaryChars=result.summary_chars
    )


async def session_set_mode_handler(
    _conn: RpcConnection, params: SessionSetModeParams, core: Core
) -> SessionSetModeResult:
    from snowpea_core.session import events

    session = _session(core, params.sessionId)
    mode = await core.sessions.set_mode(session, params.mode)
    await core.hub.emit_event(session.id, events.mode_changed(mode))
    return SessionSetModeResult(mode=mode)


async def session_set_model_handler(
    _conn: RpcConnection, params: SessionSetModelParams, core: Core
) -> SessionSetModelResult:
    """``session.setModel`` — the RPC half of ``/model <ref>``.

    Same code path as the command, so the TUI, the IDE and headless all pin a
    session the same way and all get the pin persisted on the session row.
    """
    session = _session(core, params.sessionId)
    try:
        route = await core.sessions.set_model(session, params.model)
    except ValueError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    await core.hub.emit_event(session.id, events.model_changed(route.provider, route.model))
    return SessionSetModelResult(
        provider=route.provider,
        model=route.model,
        pinned=bool((params.model or "").strip() not in ("", "inherit")),
    )


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
    # ``web.annotate`` fills ToolInfo.provider, so a caller can see which search
    # provider would actually answer without running a search (CORE-search-fix).
    return ToolListResult(tools=web.annotate(core.tools.list(session), core.settings))


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
        reason=params.reason,
    )
    return Ok(ok=True)


# ---------------------------------------------------------------------------
# question.*  (the ``ask_user`` tool)
# ---------------------------------------------------------------------------


async def question_list_handler(
    _conn: RpcConnection, params: OptionalSessionParams, core: Core
) -> QuestionListResult:
    return QuestionListResult(requests=core.questions.list(params.sessionId))


async def question_respond_handler(
    conn: RpcConnection, params: QuestionRespondParams, core: Core
) -> Ok:
    """Answer a batch from any surface; a question carries no authority."""
    await core.questions.respond(
        params.requestId,
        [answer.model_dump(mode="json") for answer in params.answers],
        by=conn.surface_id,
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


async def provider_models_handler(
    _conn: RpcConnection, params: ProviderModelsParams, core: Core
) -> ProviderModelsResult:
    """``provider.models`` — ask the vendor's endpoint what it actually serves."""
    vendor = (params.vendor or "").strip() or core.providers.default_vendor()
    try:
        listing = await core.providers.model_listing(vendor)
    except ProviderError as exc:
        raise RpcError(exc.code, str(exc)) from exc
    return ProviderModelsResult(
        vendor=vendor,
        models=listing.models,
        current=core.providers.model_for(vendor),
        source=listing.source,
        detail=listing.detail,
    )


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
            MemoryHit(
                id=entry.id,
                text=entry.text,
                tags=entry.tags,
                score=entry.score,
                scope=cast(MemoryScope, entry.scope),
                project=entry.project,
            )
            for entry in entries
        ]
    )


def _scope_namespaces(core: Core, params: MemoryListParams) -> list[str]:
    """The namespaces ``memory.list`` should read, from its scope filter (§1b).

    A project scope needs a project root, which comes either from a session id
    or from an explicit ``project`` path — a CLI running in a checkout has the
    second and not the first.
    """
    scope = params.scope or "all"
    project = ""
    agent = ""
    if params.project:
        project = project_namespace(params.project)
    session = core.sessions.get(params.sessionId) if params.sessionId else None
    if session is not None:
        project = project or project_namespace_of(session)
        agent = agent_namespace_of(session)
    if scope == "project":
        return [project] if project else []
    if scope == "global":
        return [GLOBAL_NAMESPACE]
    if scope == "agent":
        return [agent] if agent else []
    return [name for name in (project, GLOBAL_NAMESPACE, agent) if name]


async def memory_list_handler(
    _conn: RpcConnection, params: MemoryListParams, core: Core
) -> MemoryListResult:
    """``memory.list`` — what is remembered, filtered by scope (M5 §1b)."""
    namespaces = _scope_namespaces(core, params)
    if not namespaces:
        return MemoryListResult(entries=[])
    store = memory_services(core).store
    limit = max(1, params.limit)
    query = (params.query or "").strip()
    entries = (
        await store.search_many(query, namespaces=namespaces, limit=limit)
        if query
        else await store.list_many(namespaces=namespaces, limit=limit)
    )
    return MemoryListResult(
        entries=[
            MemoryEntryInfo(
                id=entry.id,
                text=entry.text,
                tags=entry.tags,
                scope=cast(MemoryScope, entry.scope),
                project=entry.project,
                createdAt=entry.created_at,
            )
            for entry in entries
        ]
    )


async def memory_delete_handler(
    _conn: RpcConnection, params: MemoryDeleteParams, core: Core
) -> Ok:
    """``memory.delete`` — forget one memory by id."""
    memory_id = params.id.strip()
    if not memory_id:
        raise RpcError(errors.INVALID_PARAMS, "memory.delete needs an id")
    removed = await memory_services(core).store.delete(memory_id)
    if not removed:
        raise RpcError(errors.NOT_FOUND, f"no memory with id {memory_id}")
    return Ok(ok=True)


async def memory_write_handler(
    _conn: RpcConnection, params: MemoryWriteParams, core: Core
) -> MemoryWriteResult:
    """``memory.write`` — store one memory and return its id."""
    text = params.text.strip()
    if not text:
        raise RpcError(errors.INVALID_PARAMS, "memory.write needs a non-empty text")
    entry = await memory_services(core).store.write(
        text, tags=params.tags, namespace=params.namespace or GLOBAL_NAMESPACE
    )
    return MemoryWriteResult(id=entry.id)


def register_session_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    """Register every method in :data:`HANDLED_METHODS`."""
    dispatcher.register("session.create", session_create_handler)
    dispatcher.register("session.resume", session_resume_handler)
    dispatcher.register("session.list", session_list_handler)
    dispatcher.register("session.deleteSaved", session_delete_saved_handler)
    dispatcher.register("session.close", session_close_handler)
    dispatcher.register("session.prompt", session_prompt_handler)
    dispatcher.register("session.interrupt", session_interrupt_handler)
    dispatcher.register("session.compact", session_compact_handler)
    dispatcher.register("session.setMode", session_set_mode_handler)
    dispatcher.register("session.setModel", session_set_model_handler)
    dispatcher.register("command.list", command_list_handler)
    dispatcher.register("command.run", command_run_handler)
    dispatcher.register("tool.list", tool_list_handler)
    dispatcher.register("approval.list", approval_list_handler)
    dispatcher.register("approval.respond", approval_respond_handler)
    dispatcher.register("question.list", question_list_handler)
    dispatcher.register("question.respond", question_respond_handler)
    dispatcher.register("provider.list", provider_list_handler)
    dispatcher.register("provider.models", provider_models_handler)
    dispatcher.register("backend.set", backend_set_handler)
    dispatcher.register("memory.search", memory_search_handler)
    dispatcher.register("memory.write", memory_write_handler)
    dispatcher.register("memory.list", memory_list_handler)
    dispatcher.register("memory.delete", memory_delete_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "register_session_handlers", "wire_core"]
