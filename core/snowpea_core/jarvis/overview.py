"""The overview of supervised sessions: ``jarvis.overview`` and Jarvis's per-turn table.

Rows come from the intake's digests, completed with what is live right now
(pending approvals and questions, title, status) and the delegation engine's
view (level, pause).  The order is "most relevant first": sessions waiting on
someone, then failing ones, then running ones, then the rest by recent
activity.

Each Jarvis turn gets a compact table of at most :data:`CONTEXT_ROWS` rows
through the runtime-context mechanism (``agent/runtime_context.py``); anything
else Jarvis reads with its tools.  The table avoids anything that changes while
a session merely streams (no partial text, minute-precision times), so the
snapshot is not re-sent on every model call.
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Any

from snowpea_core.agent import runtime_context
from snowpea_core.jarvis.clock import parse_iso
from snowpea_core.server.protocol import JarvisOverviewRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.jarvis.service import JarvisService

#: Rows in the table each Jarvis turn sees.
CONTEXT_ROWS = 40
#: Runtime-context provider name and its place in the snapshot.
CONTEXT_NAME = "jarvis-overview"
CONTEXT_ORDER = 200

#: Fences the table: titles and digests are the sessions' text, not instructions.
_NONCE = secrets.token_hex(4)


def _group(row: JarvisOverviewRow) -> int:
    if row.pendingApprovals or row.pendingQuestions:
        return 0
    if row.status == "error":
        return 1
    if row.status == "running":
        return 2
    return 3


def order(rows: list[JarvisOverviewRow]) -> list[JarvisOverviewRow]:
    """Waiting on someone, then erroring, then running, then the rest; newest first in each."""

    def key(row: JarvisOverviewRow) -> tuple[int, float]:
        moment = parse_iso(row.lastActivityAt)
        return (_group(row), -(moment.timestamp() if moment else 0.0))

    return sorted(rows, key=key)


def build_rows(
    service: JarvisService,
    *,
    project: str | None = None,
    include_idle: bool = True,
    include_external: bool = True,
    limit: int | None = None,
) -> list[JarvisOverviewRow]:
    """Every supervised session in an exposed project, most relevant first."""
    core = service.core
    intake = service.intake
    projects = service.projects
    engine = service.engine
    wanted = None
    if project:
        from snowpea_core.session.workspaces import canonical

        wanted = canonical(project)
    rows: list[JarvisOverviewRow] = []
    for digest in list(intake.digests.values()) if intake is not None else []:
        if digest.session_id == service.session_id:
            continue
        if digest.session_id.startswith("ext:"):
            continue  # external sessions come from their own source (hooks.overview_sources)
        if not projects.exposed(digest.project):
            continue
        if not service.sees_digest(digest):
            continue  # 1.56.0: its Jarvis switch is off
        if wanted is not None and digest.project != wanted:
            continue
        session = core.sessions.get(digest.session_id)
        approvals = core.approvals.count(digest.session_id) if session is not None else 0
        questions = core.questions.count(digest.session_id) if session is not None else 0
        status = digest.status
        if session is not None:
            status = "running" if session.turn_live else (
                "error" if digest.status == "error" else "idle"
            )
        if approvals:
            status = "awaiting_approval"
        elif questions:
            status = "awaiting_question"
        state = engine.session_state(
            digest.session_id, project=digest.project, exposed=True, enabled=True
        )
        readable = service.readable(digest)
        rows.append(
            JarvisOverviewRow(
                sessionId=digest.session_id,
                project=digest.project,
                title=(session.title if session is not None else None) or digest.title,
                status=status,  # type: ignore[arg-type]
                pendingApprovals=approvals,
                pendingQuestions=questions,
                waitingOnPerson=intake.waiting_on_person(digest, approvals, questions),
                level=state.level,
                exposed=readable,
                pausedUntil=state.pausedUntil,
                takenOver=state.takenOver,
                levelScope=state.levelScope,
                grantId=state.grantId,
                lastActivityAt=digest.last_activity,
                # A browser-host session hidden by jarvis.exposeBrowser shows its status only.
                digest=digest.line() if readable else "",
                hostToolsFrom=getattr(session, "host_tools_from", None)
                if session is not None else None,
                originSurface=getattr(session, "origin_surface", None)
                if session is not None else None,
            )
        )
    if include_external:
        for source in list(service.hooks.overview_sources):
            try:
                extra = list(source())
            except Exception:  # noqa: BLE001 - one broken source must not cost the overview
                continue
            for row in extra:
                if not projects.exposed(row.project):
                    continue
                if wanted is not None and row.project != wanted:
                    continue
                rows.append(row)
    if not include_idle:
        rows = [
            row
            for row in rows
            if row.status != "idle" or row.pendingApprovals or row.pendingQuestions
        ]
    rows = order(rows)
    return rows[:limit] if limit is not None else rows


_FENCE_TAG = re.compile(r"<(\s*/?\s*)(untrusted)", re.IGNORECASE)


def _cell(text: Any, limit: int) -> str:
    # A title or digest may not close (or open) the table's fence.
    value = " ".join(str(text or "").replace(_NONCE, "").split()).replace("|", "/")
    value = _FENCE_TAG.sub(r"&lt;\1\2", value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _when(stamp: str | None) -> str:
    moment = parse_iso(stamp)
    return moment.strftime("%m-%d %H:%M") if moment else "-"


def render_table(service: JarvisService, rows: list[JarvisOverviewRow], total: int) -> str:
    """The table Jarvis sees at the top of a turn."""
    head = (
        f"Jarvis overview: {total} supervised session(s)"
        + (f", the {len(rows)} most relevant shown" if total > len(rows) else "")
        + ". Times are UTC. Use your tools for anything more."
    )
    if not rows:
        return head.replace(": 0 supervised session(s)", ": no supervised sessions")
    lines = [
        head,
        f'<untrusted source="session-overview" nonce="{_NONCE}">',
        "session | project | title | status | waiting (you) | level | last | digest",
    ]
    for row in rows:
        waiting = row.pendingApprovals + row.pendingQuestions
        level = row.level or "-"
        if row.takenOver:
            level += ",taken over"
        elif row.pausedUntil:
            level += f",paused to {_when(row.pausedUntil)}"
        ident = row.sessionId or (
            f"ext:{row.external.cli}:{row.external.id}" if row.external
            else f"{row.terminal.terminalId} (terminal {row.terminal.profile})" if row.terminal
            else "?"
        )
        if not row.exposed:
            level += ",browser: status only"
        level += external_flags(row)
        lines.append(
            " | ".join(
                [
                    ident,
                    _cell(service.projects.name_of(row.project) if row.project else "-", 30),
                    _cell(row.title or "-", 40),
                    row.status,
                    f"{waiting} ({row.waitingOnPerson})" if waiting else "-",
                    level,
                    _when(row.lastActivityAt),
                    _cell(row.digest or "-", 160),
                ]
            )
        )
    lines.append(f'</untrusted nonce="{_NONCE}">')
    return "\n".join(lines)


def external_flags(row: JarvisOverviewRow) -> str:
    """What an external row's level cell adds: where it runs and whether snowpea gates it."""
    if row.external is None:
        return ""
    flags = [f"{row.external.cli} in {row.host or 'none'}"]
    if row.mode:
        flags.append(f"mode {row.mode}")
    if row.controlled is False:
        flags.append("UNCONTROLLED: snowpea cannot gate what it runs; read only, never send it "
                     "work")
    if row.approvals == "person":
        flags.append("its prompts are answered by the person in the terminal")
    elif row.host == "terminal":
        flags.append("view only: open in the person's terminal")
    return "," + ",".join(flags)


def _render_context(core: Any, session: Any) -> str:
    """Runtime-context provider: the table, for the Jarvis session only."""
    service = getattr(core, "jarvis", None) if core is not None else None
    if service is None or not service.running or session is None:
        return ""
    if getattr(session, "id", None) != service.session_id:
        return ""
    rows = build_rows(service)
    return render_table(service, rows[:CONTEXT_ROWS], len(rows))


def register_context_provider() -> None:
    """Install the provider in the daemon's runtime-context registry (idempotent)."""
    runtime_context.REGISTRY.register(
        runtime_context.ContextProvider(
            CONTEXT_NAME, CONTEXT_ORDER, _render_context, source="jarvis"
        )
    )


__all__ = [
    "CONTEXT_NAME",
    "CONTEXT_ROWS",
    "build_rows",
    "external_flags",
    "order",
    "register_context_provider",
    "render_table",
]
