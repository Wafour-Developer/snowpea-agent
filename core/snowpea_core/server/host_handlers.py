"""RPC handlers added in protocol 1.6.0 for clients that host tools (snowpea-browser).

``tool.register`` / ``tool.unregister`` / ``tool.progress``, ``approval.ask``,
``session.attach`` / ``session.steer``, ``setup.status`` /
``setup.applyDefaults`` and ``provider.test``.  The design and the rules they
follow are in ``docs/design/m-browser-host-tools.md``; the host tool registry
itself is :mod:`snowpea_core.tools.host_tools`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from snowpea_core.permissions.allowlist import site_of
from snowpea_core.permissions.policy import MODE_MATRIX, RISK_BY_TAG
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    ApprovalAskParams,
    ApprovalAskResult,
    MemoryIngestParams,
    MemoryIngestResult,
    Ok,
    ProviderTestParams,
    ProviderTestResult,
    SessionArtifact,
    SessionArtifactsResult,
    SessionAttachParams,
    SessionAttachResult,
    SessionIdParams,
    SessionNoticeParams,
    SessionRenameParams,
    SessionSteerParams,
    SessionSteerResult,
    SessionToolContentParams,
    SessionToolContentResult,
    SettingsSetParams,
    SetupApplyDefaultsParams,
    SetupApplyDefaultsResult,
    SetupItem,
    SetupStatusParams,
    SetupStatusResult,
    ToolContentBlock,
    ToolProgressParams,
    ToolRegisterParams,
    ToolRegisterResult,
    ToolUnregisterParams,
    ToolUnregisterResult,
    UsageRow,
    UsageSummaryParams,
    UsageSummaryResult,
)
from snowpea_core.session.workspace import ensure_workspace, list_artifacts
from snowpea_core.tools.host_tools import HOST_TOOLS, HostToolError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.server.rpc import RpcConnection, RpcDispatcher
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.host")

#: Where a passed ``provider.test`` is remembered, under ``$SNOWPEA_HOME``.
SETUP_STATE_FILE = "setup-state.json"

#: Search providers that work without a key.
KEYLESS_SEARCH = frozenset({"ddgs"})


def _session(core: Core, session_id: str) -> Session:
    session = core.sessions.get(session_id)
    if session is None:
        raise RpcError(errors.NOT_FOUND, f"no such session: {session_id}")
    return session


# ---------------------------------------------------------------------------
# system.hello additions: identity, re-attach, keep-alive
# ---------------------------------------------------------------------------


def adopt_hello(core: Core, conn: RpcConnection, params: Any) -> None:
    """Record a hello's client identity; re-bind that client's sessions; keep alive.

    A browser that restarts comes back on a new connection with the same
    ``clientId``: every session it started (and their subagents) makes the new
    connection its origin again, so approvals and ``tool.invoke`` reach it.
    """
    conn.client_kind = params.clientKind
    conn.client_id = params.clientId
    conn.instance_id = params.instanceId
    if params.clientId:
        rebound = 0
        for session in core.sessions.all():
            if getattr(session, "origin_client_id", None) != params.clientId:
                continue
            origin = getattr(session, "origin_conn", None)
            if origin is None or origin is conn or getattr(origin, "closed", False):
                session.origin_conn = conn
                core.hub.subscribe(conn, session.id)
                rebound += 1
        if rebound:
            log.info("client %s re-attached to %d session(s)", params.clientId, rebound)
    if params.keepAlive and not conn.keep_alive:
        conn.keep_alive = True
        lifecycle = getattr(core, "lifecycle", None)
        if lifecycle is not None:
            lifecycle.increment("keepalive_clients")

            def _release(_conn: Any) -> None:
                lifecycle.increment("keepalive_clients", -1)

            conn.on_close.append(_release)


# ---------------------------------------------------------------------------
# tool.*
# ---------------------------------------------------------------------------


async def tool_register_handler(
    conn: RpcConnection, params: ToolRegisterParams, core: Core
) -> ToolRegisterResult:
    """``tool.register`` — add or replace this connection's host tools."""
    specs = [spec.model_dump(mode="json") for spec in params.tools]
    try:
        names = HOST_TOOLS.register(core, conn, specs)
    except HostToolError as exc:
        raise RpcError(errors.INVALID_PARAMS, str(exc)) from exc
    return ToolRegisterResult(registered=names)


async def tool_unregister_handler(
    conn: RpcConnection, params: ToolUnregisterParams, core: Core
) -> ToolUnregisterResult:
    """``tool.unregister`` — remove some of this connection's host tools."""
    return ToolUnregisterResult(removed=HOST_TOOLS.unregister(conn, list(params.names)))


async def tool_progress_handler(
    conn: RpcConnection, params: ToolProgressParams, core: Core
) -> Ok:
    """``tool.progress`` (notification) — re-emit as ``session.event tool.progress``."""
    return Ok(ok=await HOST_TOOLS.progress(conn, params.callId, params.message))


# ---------------------------------------------------------------------------
# approval.ask
# ---------------------------------------------------------------------------


async def approval_ask_handler(
    conn: RpcConnection, params: ApprovalAskParams, core: Core
) -> ApprovalAskResult:
    """``approval.ask`` — a host escalates one action through the normal pipeline.

    Order: the mode matrix for the escalated tag (``deny`` stays deny), the
    allowlist for this tool on this site, then the session's approver. Only the
    connection whose host tools the session uses may ask.

    ``forceAsk`` (1.7.0): the mode matrix may only deny, never auto-allow, so a
    person is asked even in ``auto``. ``risk: "payment"`` skips the allowlist
    and is never remembered: the answer's scope is ``once`` whatever the
    approver picked, and no allowlist entry or session cache is written.
    """
    session = _session(core, params.sessionId)
    if HOST_TOOLS.owner_of(session) != conn.surface_id:
        raise RpcError(
            errors.UNAUTHORIZED, "only the host whose tools this session uses may ask"
        )
    tool = core.tools.get(params.tool, session)
    if tool is None:
        raise RpcError(errors.NOT_FOUND, f"{params.tool} is not a tool of this session")
    detail = dict(params.detail or {})
    args = {**params.args, **({"detail": detail} if detail else {})}
    site = site_of(args, params.site or (str(detail["origin"]) if detail.get("origin") else None))
    payment = (params.risk or "").strip().lower() == PAYMENT_RISK
    force = params.forceAsk or payment
    matrix = MODE_MATRIX.get(session.mode, {}).get(params.permission, "ask")
    if force:
        # Only a deny may come from the mode; allow never does.
        if matrix == "deny":
            return ApprovalAskResult(decision="deny", by="mode")
    else:
        verdict = core.policy.decide(session.mode, params.permission, tool, args, session)
        if verdict == "deny":
            return ApprovalAskResult(decision="deny", by="mode")
        if verdict == "allow":
            return ApprovalAskResult(
                decision="allow", by="mode" if matrix == "allow" else "allowlist"
            )
    if not payment and core.allowlist.matches(tool, args, workdir=session.workdir, site=site):
        return ApprovalAskResult(decision="allow", by="allowlist")
    decision = await core.approvals.request(
        session,
        params.tool,
        {**args, **({"site": site} if site else {})},
        risk=params.risk or RISK_BY_TAG.get(params.permission, "medium"),
        unattended=session.origin_conn is None,
        cancel_event=session.interrupt,
        note=params.reason,
        scope_hint="site" if site and not payment else "once",
        # A payment answer is never remembered: no cache, no allowlist entry.
        cacheable=not payment,
        site=site,
    )
    scope = decision.scope if decision.scope in _SCOPES else "once"
    return ApprovalAskResult(
        decision="allow" if decision.allowed else "deny",
        scope="once" if payment else scope,  # type: ignore[arg-type]
        by=_decided_by(decision.by),
    )


#: ``approval.ask {risk}`` value whose answers are never remembered.
PAYMENT_RISK = "payment"
_SCOPES = ("once", "session", "project", "always", "site")


def _decided_by(by: str) -> str:
    """``user`` for any person's answer (origin surface, a client, a chat)."""
    if by in ("timeout", "interrupted", "error", "origin-unreachable"):
        return by
    return "user"


# ---------------------------------------------------------------------------
# session.attach / session.steer
# ---------------------------------------------------------------------------


async def session_attach_handler(
    conn: RpcConnection, params: SessionAttachParams, core: Core
) -> SessionAttachResult:
    """``session.attach`` — this connection becomes the session's origin."""
    session = _session(core, params.sessionId)
    session.origin_conn = conn
    if getattr(conn, "client_id", None):
        session.origin_client_id = conn.client_id
    core.hub.subscribe(conn, session.id)
    return SessionAttachResult(sessionId=session.id, hostTools=HOST_TOOLS.names_for(session))


#: Longest ``session.notice`` text kept; notices beyond this many are dropped.
NOTICE_CHARS = 2000
MAX_NOTICES = 20


async def usage_summary_handler(
    _conn: RpcConnection, params: UsageSummaryParams, core: Core
) -> UsageSummaryResult:
    """``usage.summary`` — token totals from stored usage events.

    Events stored before 1.7.0 carry no provider/model; they are counted under
    the session's current one.
    """
    if core.store is None:
        return UsageSummaryResult(groupBy=params.groupBy)
    totals: dict[str, list[int]] = {}
    for row in await core.store.usage_events(params.since, params.until):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        provider = payload.get("provider") or row.get("provider") or "unknown"
        model = payload.get("model") or row.get("model") or "unknown"
        key = {
            "provider": provider,
            "model": f"{provider}:{model}",
            "session": str(row["session_id"]),
            "day": str(row["ts"])[:10],
        }[params.groupBy]
        bucket = totals.setdefault(key, [0, 0, 0])
        bucket[0] += int(payload.get("inputTokens") or 0)
        bucket[1] += int(payload.get("outputTokens") or 0)
        bucket[2] += 1
    rows = sorted(
        (
            UsageRow(key=key, inputTokens=i, outputTokens=o, calls=c)
            for key, (i, o, c) in totals.items()
        ),
        key=lambda r: r.inputTokens + r.outputTokens,
        reverse=True,
    )
    return UsageSummaryResult(
        groupBy=params.groupBy,
        rows=rows,
        inputTokens=sum(r.inputTokens for r in rows),
        outputTokens=sum(r.outputTokens for r in rows),
    )


async def memory_ingest_handler(
    _conn: RpcConnection, params: MemoryIngestParams, core: Core
) -> MemoryIngestResult:
    """``memory.ingest`` — remember visited pages (browser namespace)."""
    from snowpea_core.memory import services as memory_services
    from snowpea_core.memory.browser import ingest

    counts = await ingest(
        memory_services(core).store,
        [item.model_dump() for item in params.items],
        params.sessionId,
    )
    return MemoryIngestResult(**counts)


async def session_notice_handler(
    _conn: RpcConnection, params: SessionNoticeParams, core: Core
) -> Ok:
    """``session.notice`` — queue a [system] line for the next model call."""
    session = _session(core, params.sessionId)
    text = " ".join(params.text.split())[:NOTICE_CHARS]
    if not text:
        raise RpcError(errors.INVALID_PARAMS, "text is empty")
    if len(session.pending_notices) < MAX_NOTICES:
        session.pending_notices.append(text)
    return Ok(ok=True)


async def session_rename_handler(
    _conn: RpcConnection, params: SessionRenameParams, core: Core
) -> Ok:
    """``session.rename`` — set (or clear) the title, persist it, tell every client."""
    session = _session(core, params.sessionId)
    title = params.title.strip()[:200] or None
    session.title = title
    if core.store is not None:
        await core.store.update_title(session.id, title)
    await core.sessions.announce_sessions_changed("renamed", session.id)
    return Ok(ok=True)


async def session_artifacts_handler(
    _conn: RpcConnection, params: SessionIdParams, core: Core
) -> SessionArtifactsResult:
    """``session.artifacts`` — what the session saved for the user."""
    session = _session(core, params.sessionId)
    root = ensure_workspace(core.paths.home, session)
    return SessionArtifactsResult(
        workspaceDir=str(root),
        artifacts=[SessionArtifact(**row) for row in list_artifacts(root)],
    )


async def session_tool_content_handler(
    _conn: RpcConnection, params: SessionToolContentParams, core: Core
) -> SessionToolContentResult:
    """``session.toolContent`` — a recent host result's blocks, images included."""
    _session(core, params.sessionId)
    blocks = HOST_TOOLS.content(params.sessionId, params.callId)
    if blocks is None:
        raise RpcError(errors.NOT_FOUND, f"no content kept for {params.callId}")
    return SessionToolContentResult(
        callId=params.callId, content=[ToolContentBlock(**block) for block in blocks]
    )


async def session_steer_handler(
    conn: RpcConnection, params: SessionSteerParams, core: Core
) -> SessionSteerResult:
    """``session.steer`` — inject into the running turn, or start one."""
    session = _session(core, params.sessionId)
    text = params.text.strip()
    if not text:
        raise RpcError(errors.INVALID_PARAMS, "text is empty")
    task = getattr(session, "turn_task", None)
    running = session.current_turn is not None or (task is not None and not task.done())
    if running:
        session.rpc_steers.append(text)
        return SessionSteerResult(ok=True, started=False)
    from snowpea_core.server.protocol import SessionPromptParams
    from snowpea_core.server.session_handlers import session_prompt_handler

    await session_prompt_handler(
        conn, SessionPromptParams(sessionId=session.id, text=text), core
    )
    return SessionSteerResult(ok=True, started=True)


# ---------------------------------------------------------------------------
# setup.status / setup.applyDefaults / provider.test
# ---------------------------------------------------------------------------


def _setup_state(core: Core) -> dict[str, Any]:
    path = core.paths.home / SETUP_STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_setup_state(core: Core, state: dict[str, Any]) -> None:
    path = core.paths.home / SETUP_STATE_FILE
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _configured_providers(core: Core) -> list[str]:
    return [info.vendor for info in core.providers.list() if info.configured]


def _search_keyless_ok(core: Core) -> bool:
    search = core.settings.search
    return search.provider in KEYLESS_SEARCH or bool(search.credentials.get(search.provider))


def _gateways_enabled(core: Core) -> list[str]:
    return [
        name
        for name, value in (core.settings.gateway or {}).items()
        if isinstance(value, dict) and value.get("enabled")
    ]


def setup_status(core: Core, profile: str) -> SetupStatusResult:
    """What setup still needs, for ``profile``."""
    configured = _configured_providers(core)
    tested = _setup_state(core).get("providerTested") or {}
    tested_ok = bool(tested.get("provider")) and tested.get("provider") in configured
    if profile == "browser":
        required = [
            SetupItem(
                id="provider",
                title="An LLM provider that answered a test call",
                done=tested_ok,
            )
        ]
    else:
        required = [
            SetupItem(id="provider", title="An LLM provider", done=bool(configured))
        ]
    settings = core.settings
    optional = [
        SetupItem(
            id="search",
            title="Web search (keyless default)",
            done=_search_keyless_ok(core),
            defaultApplied=settings.search.provider in KEYLESS_SEARCH,
        ),
        SetupItem(
            id="memory",
            title="Long-term memory",
            done=True,
            defaultApplied=bool(settings.memory.enabled),
        ),
        SetupItem(
            id="scheduler",
            title="Scheduled routines",
            done=True,
            defaultApplied=bool(settings.scheduler.enabled),
        ),
        SetupItem(
            id="gateways",
            title="Chat gateways (off by default)",
            done=True,
            defaultApplied=not _gateways_enabled(core),
        ),
        SetupItem(id="mode", title="Default mode: accept", done=True, defaultApplied=True),
    ]
    if profile == "browser":
        optional.append(
            SetupItem(
                id="browser",
                title="Browser actions run in the connected browser",
                done=True,
                defaultApplied=settings.browser.provider == "host",
            )
        )
    return SetupStatusResult(
        required=required,
        optional=optional,
        existingInstall=bool(configured) and bool(core.settings.providers),
        configuredProviders=configured,
    )


async def setup_status_handler(
    _conn: RpcConnection, params: SetupStatusParams, core: Core
) -> SetupStatusResult:
    return setup_status(core, params.profile)


async def setup_apply_defaults_handler(
    conn: RpcConnection, params: SetupApplyDefaultsParams, core: Core
) -> SetupApplyDefaultsResult:
    """``setup.applyDefaults`` — fill in defaults; what is already fine is left alone.

    Gateways are never turned off here: one the person enabled is their choice,
    and none is enabled by default. The default mode is already ``accept``.
    """
    settings = core.settings
    patch: dict[str, Any] = {}
    applied: list[str] = []
    if not _search_keyless_ok(core):
        patch.setdefault("search", {})["provider"] = "ddgs"
        applied.append("search.provider")
    if not settings.memory.enabled:
        patch.setdefault("memory", {})["enabled"] = True
        applied.append("memory.enabled")
    if not settings.scheduler.enabled:
        patch.setdefault("scheduler", {})["enabled"] = True
        applied.append("scheduler.enabled")
    if params.profile == "browser" and settings.browser.provider != "host":
        patch.setdefault("browser", {})["provider"] = "host"
        applied.append("browser.provider")
    if patch:
        from snowpea_core.server.settings_handlers import settings_set_handler

        await settings_set_handler(conn, SettingsSetParams(scope="global", patch=patch), core)
    return SetupApplyDefaultsResult(applied=applied, status=setup_status(core, params.profile))


async def provider_test_handler(
    _conn: RpcConnection, params: ProviderTestParams, core: Core
) -> ProviderTestResult:
    """``provider.test`` — one short completion; remembered for ``setup.status``."""
    started = time.monotonic()
    model = params.model or ""
    try:
        provider = core.providers.get(params.provider, params.model)
        model = str(getattr(provider, "model", "") or model)
        chunks: list[str] = []
        echo: str | None = None
        stream = provider.stream(
            [ChatMessage(role="user", content="Reply with the single word: ok")],
            [],
            max_tokens=256,
        )
        async for event in stream:
            if event.kind == "text_delta" and event.text:
                chunks.append(event.text)
            reported = getattr(event, "model", None)
            if isinstance(reported, str) and reported:
                echo = reported
        reply = "".join(chunks).strip()
    except Exception as exc:  # noqa: BLE001 - the answer is the diagnosis
        return ProviderTestResult(
            ok=False,
            provider=params.provider,
            model=model,
            latencyMs=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency = int((time.monotonic() - started) * 1000)
    state = _setup_state(core)
    state["providerTested"] = {
        "provider": params.provider,
        "model": model,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _save_setup_state(core, state)
    except OSError:  # pragma: no cover - a read-only home still answers the test
        log.warning("could not record the provider test", exc_info=True)
    return ProviderTestResult(
        ok=True,
        provider=params.provider,
        model=model,
        modelEcho=echo,
        latencyMs=latency,
        reply=reply[:200],
    )


def register_host_handlers(dispatcher: RpcDispatcher) -> None:
    from snowpea_core.tools.browser_providers.host import HOST_BROWSER

    HOST_BROWSER.bind(dispatcher.core)
    dispatcher.register("tool.register", tool_register_handler)
    dispatcher.register("tool.unregister", tool_unregister_handler)
    dispatcher.register("tool.progress", tool_progress_handler)
    dispatcher.register("approval.ask", approval_ask_handler)
    dispatcher.register("session.attach", session_attach_handler)
    dispatcher.register("session.steer", session_steer_handler)
    dispatcher.register("session.toolContent", session_tool_content_handler)
    dispatcher.register("session.artifacts", session_artifacts_handler)
    dispatcher.register("session.rename", session_rename_handler)
    dispatcher.register("session.notice", session_notice_handler)
    dispatcher.register("memory.ingest", memory_ingest_handler)
    dispatcher.register("usage.summary", usage_summary_handler)
    dispatcher.register("setup.status", setup_status_handler)
    dispatcher.register("setup.applyDefaults", setup_apply_defaults_handler)
    dispatcher.register("provider.test", provider_test_handler)


__all__ = ["adopt_hello", "register_host_handlers", "setup_status"]
