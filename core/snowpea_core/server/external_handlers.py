"""``external.*`` (1.53.0, M19 WP5): external agent CLI sessions over RPC.

Thin wrappers around :class:`snowpea_core.external.manager.ExternalManager`:
validation, person checks and process handling live there.  With no adapter
registered, ``external.clis`` lists all four CLIs as not available and every
other method answers ``external_unavailable``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from snowpea_core.config.project import ProjectSettings
from snowpea_core.external.manager import ExternalManager
from snowpea_core.external.proc import ExternalError
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import is_person, require_person
from snowpea_core.server.protocol import (
    Empty,
    ExternalClisConfirmPathParams,
    ExternalClisResult,
    ExternalCreateParams,
    ExternalCreateResult,
    ExternalGetResult,
    ExternalListParams,
    ExternalListResult,
    ExternalOpenParams,
    ExternalOpenResult,
    ExternalReadParams,
    ExternalReadResult,
    ExternalRefParams,
    ExternalSendParams,
    ExternalSendResult,
    ExternalSetModeParams,
    Ok,
)
from snowpea_core.session import workspaces as ws

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher

log = logging.getLogger("snowpea.external")

HANDLED_METHODS: tuple[str, ...] = (
    "external.clis",
    "external.clis.confirmPath",
    "external.list",
    "external.open",
    "external.read",
    "external.send",
    "external.setMode",
    "external.interrupt",
    "external.create",
    "external.close",
    "external.get",
    "external.unwatch",
)


async def _roots(core: Core) -> list[str]:
    """Registered projects: workspace folders and every top-level session's workdir."""
    found: set[str] = set()
    store = getattr(core, "store", None)
    if store is not None:
        with contextlib.suppress(Exception):
            for item in await ws.list_all(store):
                found.add(str(item["path"]))
            found.update(await ws.session_counts(store))
    sessions = getattr(core, "sessions", None)
    for session in getattr(sessions, "all", list)():
        workdir = getattr(session, "workdir", None)
        if workdir and getattr(session, "kind", "chat") != "subagent":
            found.add(ws.canonical(workdir))
    return sorted(found)


async def _exposed(root: str) -> bool:
    """``jarvis.expose`` of the project at ``root``; a broken settings file hides it (M2)."""
    try:
        return await asyncio.to_thread(ProjectSettings.exposed, root)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _workdir_fn(core: Core) -> Callable[[str], Awaitable[str | None]]:
    async def workdir_of(session_id: str) -> str | None:
        sessions = getattr(core, "sessions", None)
        session = sessions.get(session_id) if sessions is not None else None
        if session is not None:
            return str(session.workdir)
        store = getattr(core, "store", None)
        if store is None:
            return None
        for row in await store.list_sessions(include_closed=True):
            if str(row.get("id")) == session_id and row.get("workdir"):
                return str(row["workdir"])
        return None

    return workdir_of


def _ledger_fn(core: Core) -> Callable[[dict[str, Any]], str]:
    """Unrestricted grants go into the delegation engine's ledger (one ledger per daemon).

    The engine announces each entry as ``jarvis.decision``.  A failed write
    raises, so no grant is made without its entry.
    """

    def write(entry: dict[str, Any]) -> str:
        from snowpea_core.jarvis.delegation import engine_for

        decision = engine_for(core).ledger.record_decision(
            kind="unrestricted",
            decided_by="person",
            summary=str(entry.get("summary") or ""),
            external=entry.get("external"),
            project=entry.get("project"),
            decision=entry.get("decision"),
            reason=str(entry.get("reason") or ""),
            importance=str(entry.get("importance") or "high"),
        )
        return decision.id

    return write


def ensure_manager(core: Core) -> ExternalManager:
    """The daemon's :class:`ExternalManager`, created on first use."""
    manager = getattr(core, "external", None)
    if manager is None:
        manager = ExternalManager(
            home=core.paths.home,
            settings=lambda: core.settings,
            hub=core.hub,
            approvals=core.approvals,
            roots=lambda: _roots(core),
            exposed=_exposed,
            workdir_of=_workdir_fn(core),
            ledger=_ledger_fn(core),
        )
        core.external = manager
    return manager


def _rpc(exc: ExternalError) -> RpcError:
    return RpcError(exc.code, exc.message, exc.details)


async def _call(coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    except ExternalError as exc:
        raise _rpc(exc) from exc


def _own_only(conn: RpcConnection | None, core: Core, cli: str, session_id: str) -> None:
    """A browser host or the connector reaches only sessions it watches (security review L6).

    A person (local or remote web login) reaches every session in a
    registered project, as before.  Anyone else must have opened, created or
    sent to the session first; otherwise it does not exist for them.
    """
    if is_person(conn):
        return
    if not ensure_manager(core).watched_by(cli, session_id, conn):
        raise RpcError(errors.NOT_FOUND, f"no {cli} session {session_id} for this connection")


async def clis_handler(_conn: RpcConnection, _params: Empty, core: Core) -> ExternalClisResult:
    rows = await _call(ensure_manager(core).clis())
    return ExternalClisResult.model_validate({"clis": rows})


async def confirm_path_handler(
    conn: RpcConnection, params: ExternalClisConfirmPathParams, core: Core
) -> ExternalClisResult:
    """``external.clis.confirmPath``: the person allows snowpea to run a CLI path override."""
    from snowpea_core.config.settings import Settings
    from snowpea_core.external.manager import resolved_override

    require_person(conn, "confirm an external CLI path", local=True)
    cli_settings = core.settings.jarvis.external.clis.get(params.cli)
    override = getattr(cli_settings, "path", None)
    real = resolved_override(override) if override else None
    if real is None or real != params.path:
        raise RpcError(
            errors.INVALID_PARAMS,
            "that is not the path jarvis.external.clis."
            f"{params.cli}.path resolves to now; read external.clis again",
        )
    merged = core.settings.model_dump(mode="json")
    merged["jarvis"]["external"]["clis"][params.cli]["confirmedPath"] = real
    updated = Settings.model_validate(merged)
    updated.save(core.paths)
    await core.adopt_settings(updated, ["jarvis"])
    with contextlib.suppress(Exception):
        from snowpea_core.jarvis.delegation import engine_for

        engine_for(core).ledger.record_decision(
            kind="escalate",
            decided_by="person",
            summary=f"confirmed {params.cli} executable {real}"[:300],
            reason="settings",
            importance="normal",
        )
    rows = await _call(ensure_manager(core).clis(refresh=True))
    return ExternalClisResult.model_validate({"clis": rows})


async def list_handler(
    conn: RpcConnection, params: ExternalListParams, core: Core
) -> ExternalListResult:
    manager = ensure_manager(core)
    result = await _call(
        manager.list_sessions(
            clis=params.clis,
            cwd=params.cwd,
            session_id=params.sessionId,
            all_projects=params.allProjects,
            limit=params.limit,
            before=params.before,
        )
    )
    if not is_person(conn):
        own = manager.watched_refs(conn)
        result["sessions"] = [
            row for row in result["sessions"]
            if (row["ref"]["cli"], row["ref"]["id"]) in own
        ]
    return ExternalListResult.model_validate(result)


async def open_handler(
    conn: RpcConnection, params: ExternalOpenParams, core: Core
) -> ExternalOpenResult:
    result = await _call(
        ensure_manager(core).open(
            params.ref.cli, params.ref.id, limit=params.limit, mode=params.mode, conn=conn,
            transport=params.transport, trust=params.trust,
        )
    )
    return ExternalOpenResult.model_validate(result)


async def read_handler(
    conn: RpcConnection, params: ExternalReadParams, core: Core
) -> ExternalReadResult:
    _own_only(conn, core, params.ref.cli, params.ref.id)
    result = await _call(
        ensure_manager(core).read(
            params.ref.cli, params.ref.id, before_seq=params.beforeSeq, limit=params.limit
        )
    )
    return ExternalReadResult.model_validate(result)


async def send_handler(
    conn: RpcConnection, params: ExternalSendParams, core: Core
) -> ExternalSendResult:
    _own_only(conn, core, params.ref.cli, params.ref.id)
    result = await _call(
        ensure_manager(core).send(params.ref.cli, params.ref.id, params.text, conn=conn,
                                  trust=params.trust)
    )
    return ExternalSendResult.model_validate(result)


async def set_mode_handler(
    conn: RpcConnection, params: ExternalSetModeParams, core: Core
) -> ExternalCreateResult:
    _own_only(conn, core, params.ref.cli, params.ref.id)
    result = await _call(
        ensure_manager(core).set_mode(params.ref.cli, params.ref.id, params.mode, conn=conn)
    )
    return ExternalCreateResult.model_validate(result)


async def interrupt_handler(
    conn: RpcConnection, params: ExternalRefParams, core: Core
) -> Ok:
    _own_only(conn, core, params.ref.cli, params.ref.id)
    await _call(ensure_manager(core).interrupt(params.ref.cli, params.ref.id))
    return Ok(ok=True)


async def create_handler(
    conn: RpcConnection, params: ExternalCreateParams, core: Core
) -> ExternalCreateResult:
    # Security review 7, H-2: no external CLI is started inside snowpea's own home.
    from snowpea_core.server.project_gate import require_project

    require_project(core, params.project, dot=False, field="project")
    result = await _call(
        ensure_manager(core).create(
            params.cli,
            params.project,
            model=params.model,
            mode=params.mode,
            transport=params.transport,
            conn=conn,
            trust=params.trust,
        )
    )
    return ExternalCreateResult.model_validate(result)


async def close_handler(conn: RpcConnection, params: ExternalRefParams, core: Core) -> Ok:
    _own_only(conn, core, params.ref.cli, params.ref.id)
    await _call(ensure_manager(core).close(params.ref.cli, params.ref.id, conn=conn))
    return Ok(ok=True)


async def get_handler(
    conn: RpcConnection, params: ExternalRefParams, core: Core
) -> ExternalGetResult:
    _own_only(conn, core, params.ref.cli, params.ref.id)
    result = await _call(ensure_manager(core).get(params.ref.cli, params.ref.id))
    return ExternalGetResult.model_validate(result)


async def unwatch_handler(conn: RpcConnection, params: ExternalRefParams, core: Core) -> Ok:
    await _call(ensure_manager(core).unwatch(params.ref.cli, params.ref.id, conn=conn))
    return Ok(ok=True)


def register_external_handlers(dispatcher: RpcDispatcher) -> RpcDispatcher:
    dispatcher.register("external.clis", clis_handler)
    dispatcher.register("external.clis.confirmPath", confirm_path_handler)
    dispatcher.register("external.list", list_handler)
    dispatcher.register("external.open", open_handler)
    dispatcher.register("external.read", read_handler)
    dispatcher.register("external.send", send_handler)
    dispatcher.register("external.setMode", set_mode_handler)
    dispatcher.register("external.interrupt", interrupt_handler)
    dispatcher.register("external.create", create_handler)
    dispatcher.register("external.close", close_handler)
    dispatcher.register("external.get", get_handler)
    dispatcher.register("external.unwatch", unwatch_handler)
    return dispatcher


__all__ = ["HANDLED_METHODS", "ensure_manager", "register_external_handlers"]
