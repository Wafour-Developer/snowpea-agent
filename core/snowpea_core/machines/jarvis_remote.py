"""Jarvis in-process calls and notification relay for remote machines (M42, 1.77.0).

Jarvis sees three kinds of things on a remote machine, all only when the person
turned the item's Jarvis switch on there:

* terminals (``terminal.list``),
* snowpea sessions (``session.list``), named ``<machineId>:<sessionId>``,
* external CLI sessions (``external.list``), named ``<machineId>:ext:<cli>:<id>``.

The cache below is what the remote daemon last told us; the relay keeps it fresh
from the daemon-wide notifications Jarvis's own proxy client hears
(``sessions.changed``, ``jarvis.session.changed``, ``external.state``) and from a
periodic re-list, and turns status changes into intake wakes that name the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

log = logging.getLogger("snowpea.machines.jarvis")

#: Seconds between two full re-lists of one machine (sessions, externals, terminals).
REFRESH_INTERVAL_SEC = 60.0
#: A notification about an unknown item re-lists at most this often.
_REFRESH_MIN_GAP_SEC = 5.0
#: Statuses a ``JarvisOverviewRow`` accepts.
_ROW_STATUSES = frozenset({"idle", "running", "awaiting_approval", "awaiting_question", "error"})
_BLOCKED_STATUSES = frozenset({"awaiting_approval", "awaiting_question"})


def split_qualified_id(text: str) -> tuple[str | None, str]:
    """``<machineId>:<localId>`` or a local id alone."""
    raw = str(text or "").strip()
    if ":" not in raw:
        return None, raw
    mid, rest = raw.split(":", 1)
    if _UUID.fullmatch(mid):
        return mid, rest
    return None, raw


def qualify(machine_id: str, local_id: str) -> str:
    return f"{machine_id}:{local_id}"


def delegation_key(machine_id: str, local_key: str) -> str:
    """Machine-qualified delegation/intake key for a remote-owned session or terminal."""
    return qualify(machine_id, local_key)


def external_local_key(cli: str, ext_id: str) -> str:
    """``ext:<cli>:<id>``: an external session's key on its own machine."""
    return f"ext:{cli}:{ext_id}"


def parse_external_local(text: str) -> tuple[str, str] | None:
    """``(cli, id)`` of an ``ext:<cli>:<id>`` key, or ``None``."""
    parts = str(text or "").split(":", 2)
    if len(parts) == 3 and parts[0] == "ext" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _ref_parts(ref: Any) -> tuple[str, str] | None:
    """``(cli, id)`` of an ExternalRef, a ref dict or an ``ext:`` key."""
    if isinstance(ref, str):
        return parse_external_local(ref)
    if isinstance(ref, dict):
        cli, ext_id = ref.get("cli"), ref.get("id")
    else:
        cli, ext_id = getattr(ref, "cli", None), getattr(ref, "id", None)
    if not cli or not ext_id:
        return None
    return str(cli), str(ext_id)


def machine_label(manager: Any, machine_id: str) -> str:
    for row in manager.rows():
        if row.id == machine_id:
            return str(row.label or machine_id[:8])
    rec = manager.registry.get(machine_id)
    if rec is not None:
        return str(rec.label or machine_id[:8])
    return machine_id[:8]


def _row_status(value: Any) -> str:
    text = str(value or "idle")
    return text if text in _ROW_STATUSES else "idle"


@dataclass
class RemoteTerminalRow:
    machine_id: str
    terminal_id: str
    profile: str
    title: str
    jarvis_on: bool
    status: dict[str, Any] = field(default_factory=dict)
    parent: str | None = None
    dangerous: bool = False
    jarvis_paused_until: str | None = None


@dataclass
class RemoteSnowpeaSessionRow:
    """One snowpea session of a remote machine, as its ``session.list`` shows it."""

    machine_id: str
    session_id: str
    title: str
    workdir: str
    status: str
    jarvis_on: bool
    kind: str = "chat"
    mode: str | None = None
    auto_after_plan: bool = False
    parent: str | None = None
    last_activity: str | None = None
    paused_until: str | None = None
    pending_approvals: int = 0
    pending_questions: int = 0


@dataclass
class RemoteExternalSessionRow:
    """One external CLI session of a remote machine, as its ``external.list`` shows it."""

    machine_id: str
    cli: str
    ext_id: str
    title: str
    cwd: str
    status: str
    jarvis_on: bool
    host: str = "none"
    mode: str | None = None
    native_mode: str | None = None
    controlled: bool = True
    approvals: str | None = None
    can_send: bool = True
    updated_at: str | None = None
    preview: str = ""
    security_lists_apply: bool | None = None
    paused_until: str | None = None
    jarvis_paused_until: str | None = None

    @property
    def local_key(self) -> str:
        return external_local_key(self.cli, self.ext_id)

    def as_row_dict(self) -> dict[str, Any]:
        """The fields the external tools read, shaped like ``ExternalSessionRow``."""
        return {
            "ref": {"cli": self.cli, "id": self.ext_id},
            "title": self.title,
            "cwd": self.cwd,
            "status": self.status,
            "host": self.host,
            "mode": self.mode,
            "nativeMode": self.native_mode,
            "controlled": self.controlled,
            "approvals": self.approvals,
            "canSend": self.can_send,
            "updatedAt": self.updated_at,
            "preview": self.preview,
            "securityListsApply": self.security_lists_apply,
        }


class JarvisRemoteCache:
    """Jarvis-visible remote terminals, sessions and externals (keyed by qualified id)."""

    def __init__(self) -> None:
        self._rows: dict[str, RemoteTerminalRow] = {}
        self._sessions: dict[str, RemoteSnowpeaSessionRow] = {}
        self._externals: dict[str, RemoteExternalSessionRow] = {}

    def qualified(self, machine_id: str, terminal_id: str) -> str:
        return qualify(machine_id, terminal_id)

    # -- terminals ---------------------------------------------------------
    def upsert(self, machine_id: str, item: dict[str, Any]) -> None:
        tid = str(item.get("terminalId") or "")
        if not tid:
            return
        key = self.qualified(machine_id, tid)
        self._rows[key] = RemoteTerminalRow(
            machine_id=machine_id,
            terminal_id=tid,
            profile=str(item.get("profile") or "-"),
            title=str(item.get("title") or ""),
            jarvis_on=bool(item.get("jarvisOn")),
            status=dict(item.get("status") or {}),
            parent=item.get("parent"),
            dangerous=bool(item.get("dangerous")),
            jarvis_paused_until=item.get("jarvisPausedUntil"),
        )

    def jarvis_on(self, machine_id: str, terminal_id: str) -> bool:
        row = self._rows.get(self.qualified(machine_id, terminal_id))
        return bool(row and row.jarvis_on)

    def visible_rows(self) -> list[RemoteTerminalRow]:
        return [r for r in self._rows.values() if r.jarvis_on]

    def get(self, qualified_id: str) -> RemoteTerminalRow | None:
        row = self._rows.get(qualified_id)
        if row is None or not row.jarvis_on:
            return None
        return row

    # -- snowpea sessions --------------------------------------------------
    def upsert_session(self, machine_id: str, item: dict[str, Any]) -> None:
        """A ``session.list`` row (``SessionSummary``); Jarvis's own and sub-agents are skipped."""
        sid = str(item.get("sessionId") or "")
        if not sid:
            return
        key = qualify(machine_id, sid)
        if item.get("role") == "jarvis" or str(item.get("kind") or "chat") == "subagent":
            self._sessions.pop(key, None)
            return
        # A locked switch (project hidden there, the Jarvis family) is not Jarvis's to see.
        on = bool(item.get("jarvisOn")) and not item.get("jarvisLocked")
        self._sessions[key] = RemoteSnowpeaSessionRow(
            machine_id=machine_id,
            session_id=sid,
            title=str(item.get("title") or ""),
            workdir=str(item.get("workdir") or ""),
            status=_row_status(item.get("status")),
            jarvis_on=on,
            kind=str(item.get("kind") or "chat"),
            mode=item.get("mode"),
            auto_after_plan=bool(item.get("autoAfterPlan")),
            parent=item.get("parentSessionId"),
            paused_until=(item.get("jarvis") or {}).get("pausedUntil")
            if isinstance(item.get("jarvis"), dict)
            else item.get("pausedUntil"),
            last_activity=item.get("lastActivityAt"),
            pending_approvals=int(item.get("pendingApprovals") or 0),
            pending_questions=int(item.get("pendingQuestions") or 0),
        )

    def jarvis_on_session(self, machine_id: str, session_id: str) -> bool:
        row = self._sessions.get(qualify(machine_id, session_id))
        return bool(row and row.jarvis_on)

    def visible_session_rows(self) -> list[RemoteSnowpeaSessionRow]:
        return [r for r in self._sessions.values() if r.jarvis_on]

    def get_session(self, qualified_id: str) -> RemoteSnowpeaSessionRow | None:
        row = self._sessions.get(qualified_id)
        if row is None or not row.jarvis_on:
            return None
        return row

    def session_any(self, qualified_id: str) -> RemoteSnowpeaSessionRow | None:
        """The row whatever its switch says (the relay tracks switched-off ones too)."""
        return self._sessions.get(qualified_id)

    def drop_session(self, machine_id: str, session_id: str) -> None:
        self._sessions.pop(qualify(machine_id, session_id), None)

    def prune_terminals(self, machine_id: str, keep: set[str]) -> None:
        for key in [
            k for k, r in self._rows.items()
            if r.machine_id == machine_id and r.terminal_id not in keep
        ]:
            self._rows.pop(key, None)

    def prune_sessions(self, machine_id: str, keep: set[str]) -> None:
        """Forget this machine's sessions a full ``session.list`` no longer shows."""
        for key in [
            k for k, r in self._sessions.items()
            if r.machine_id == machine_id and r.session_id not in keep
        ]:
            self._sessions.pop(key, None)

    # -- external sessions -------------------------------------------------
    def upsert_external(self, machine_id: str, item: dict[str, Any]) -> None:
        """An ``external.list`` row (``ExternalSessionRow``)."""
        parts = _ref_parts(item.get("ref"))
        if parts is None:
            return
        cli, ext_id = parts
        on = bool(item.get("jarvisOn")) and not item.get("jarvisLocked")
        jarvis = item.get("jarvis") or {}
        paused = None
        if isinstance(jarvis, dict):
            paused = jarvis.get("pausedUntil")
        self._externals[qualify(machine_id, external_local_key(cli, ext_id))] = (
            RemoteExternalSessionRow(
                machine_id=machine_id,
                cli=cli,
                ext_id=ext_id,
                title=str(item.get("title") or ""),
                cwd=str(item.get("cwd") or ""),
                status=_row_status(item.get("status")),
                jarvis_on=on,
                host=str(item.get("host") or "none"),
                mode=item.get("mode"),
                native_mode=item.get("nativeMode"),
                controlled=bool(item.get("controlled", True)),
                approvals=item.get("approvals"),
                can_send=bool(item.get("canSend", True)),
                updated_at=item.get("updatedAt"),
                preview=str(item.get("preview") or ""),
                security_lists_apply=item.get("securityListsApply"),
                paused_until=paused,
                jarvis_paused_until=item.get("jarvisPausedUntil"),
            )
        )

    def jarvis_on_external(self, machine_id: str, ref: Any) -> bool:
        parts = _ref_parts(ref)
        if parts is None:
            return False
        row = self._externals.get(qualify(machine_id, external_local_key(*parts)))
        return bool(row and row.jarvis_on)

    def visible_external_rows(self) -> list[RemoteExternalSessionRow]:
        return [r for r in self._externals.values() if r.jarvis_on]

    def get_external(self, qualified_id: str) -> RemoteExternalSessionRow | None:
        row = self._externals.get(qualified_id)
        if row is None or not row.jarvis_on:
            return None
        return row

    def external_any(self, qualified_id: str) -> RemoteExternalSessionRow | None:
        return self._externals.get(qualified_id)

    def prune_externals(self, machine_id: str, keep: set[str]) -> None:
        for key in [
            k for k, r in self._externals.items()
            if r.machine_id == machine_id and r.local_key not in keep
        ]:
            self._externals.pop(key, None)

    # -- any kind ------------------------------------------------------------
    def jarvis_on_key(self, qualified_id: str) -> bool:
        """Whether ``<machineId>:<terminal|session|ext:...>`` is switched on for Jarvis."""
        return bool(
            self.get(qualified_id) is not None
            or self.get_session(qualified_id) is not None
            or self.get_external(qualified_id) is not None
        )

    def visible_keys(self, machine_id: str) -> set[str]:
        prefix = f"{machine_id}:"
        keys = {k for k, r in self._rows.items() if r.jarvis_on}
        keys |= {k for k, r in self._sessions.items() if r.jarvis_on}
        keys |= {k for k, r in self._externals.items() if r.jarvis_on}
        return {k for k in keys if k.startswith(prefix)}

    def drop_machine(self, machine_id: str) -> None:
        prefix = f"{machine_id}:"
        for table in (self._rows, self._sessions, self._externals):
            for key in [k for k in table if k.startswith(prefix)]:
                table.pop(key, None)


#: The cache's earlier name (terminals only); kept for importers.
RemoteTerminalCache = JarvisRemoteCache


def _cache(core: Any) -> JarvisRemoteCache | None:
    manager = getattr(core, "machines", None)
    return getattr(manager, "_jarvis_cache", None) if manager is not None else None


def remote_visible(core: Any, qualified_id: str) -> bool:
    """A remote item Jarvis may see: its switch is on there and not turned off here."""
    cache = _cache(core)
    if cache is None or not cache.jarvis_on_key(qualified_id):
        return False
    engine = getattr(getattr(core, "jarvis", None), "engine", None)
    return not (engine is not None and engine.switch(qualified_id) is False)


def remote_seed(core: Any, qualified_id: str) -> dict[str, Any] | None:
    """What the intake needs to start a digest of a remote session or external session."""
    mid, local = split_qualified_id(qualified_id)
    cache = _cache(core)
    if mid is None or cache is None or not remote_visible(core, qualified_id):
        return None
    manager = getattr(core, "machines", None)
    label = machine_label(manager, mid) if manager is not None else mid[:8]
    session = cache.get_session(qualified_id)
    if session is not None:
        return {
            "title": f"{label}: {session.title or 'untitled'}",
            "kind": "chat",
            "status": session.status if session.status in ("running", "error") else "idle",
            "last_activity": session.last_activity,
        }
    external = cache.get_external(qualified_id)
    if external is not None:
        return {
            "title": f"{label}: {external.title or 'untitled'}",
            "kind": "external",
            "status": external.status if external.status in ("running", "error") else "idle",
            "last_activity": external.updated_at,
        }
    _ = local
    return None


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def overview_rows(service: Any) -> list[Any]:
    """One row per Jarvis-visible remote snowpea session and external session."""
    from snowpea_core.jarvis.terminals import safe_title
    from snowpea_core.server.protocol import ExternalRef, JarvisOverviewRow

    machines = getattr(service.core, "machines", None)
    cache = getattr(machines, "_jarvis_cache", None) if machines is not None else None
    if cache is None:
        return []
    intake = service.intake
    rows: list[JarvisOverviewRow] = []
    for item in cache.visible_session_rows():
        qid = qualify(item.machine_id, item.session_id)
        label = machine_label(machines, item.machine_id)
        state = service.engine.session_state(qid, project=None, exposed=True, enabled=True)
        digest = intake.digests.get(qid) if intake is not None else None
        line = digest.line() if digest is not None else ""
        waiting = item.pending_approvals + item.pending_questions
        rows.append(
            JarvisOverviewRow(
                sessionId=qid,
                external=None,
                project=None,
                title=f"{label}: {safe_title(item.title)}",
                status=item.status,  # type: ignore[arg-type]
                pendingApprovals=item.pending_approvals,
                pendingQuestions=item.pending_questions,
                waitingOnPerson=waiting,
                level=state.level,
                exposed=True,
                pausedUntil=state.pausedUntil,
                takenOver=state.takenOver,
                levelScope=state.levelScope,
                grantId=state.grantId,
                lastActivityAt=(digest.last_activity if digest is not None else None)
                or item.last_activity,
                digest=f"[{label}] {line or item.status}"[:200],
                machine=item.machine_id,
                machineLabel=label,
            )
        )
    for ext in cache.visible_external_rows():
        qid = qualify(ext.machine_id, ext.local_key)
        label = machine_label(machines, ext.machine_id)
        state = service.engine.session_state(qid, project=None, exposed=True, enabled=True)
        digest = intake.digests.get(qid) if intake is not None else None
        line = digest.line() if digest is not None else ""
        row_fields: dict[str, Any] = {}
        if ext.host in ("none", "terminal", "tmux", "snowpea"):
            row_fields["host"] = ext.host
        if ext.approvals in ("none", "person", "snowpea", "jarvis"):
            row_fields["approvals"] = ext.approvals
        with contextlib.suppress(Exception):
            rows.append(
                JarvisOverviewRow(
                    sessionId=None,
                    external=ExternalRef(cli=ext.cli, id=ext.ext_id),  # type: ignore[arg-type]
                    project=None,
                    title=f"{label}: {safe_title(ext.title)}",
                    status=ext.status,  # type: ignore[arg-type]
                    mode=ext.mode,  # type: ignore[arg-type]
                    nativeMode=ext.native_mode,
                    controlled=ext.controlled,
                    level=state.level,
                    exposed=True,
                    pausedUntil=state.pausedUntil,
                    takenOver=state.takenOver,
                    levelScope=state.levelScope,
                    grantId=state.grantId,
                    lastActivityAt=(digest.last_activity if digest is not None else None)
                    or ext.updated_at,
                    digest=f"[{label}] {line or ext.preview or ext.status}"[:200],
                    securityListsApply=ext.security_lists_apply,
                    machine=ext.machine_id,
                    machineLabel=label,
                    **row_fields,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------


@dataclass
class RemoteJarvisRelay:
    """Feeds remote terminal, session and external events into local Jarvis intake."""

    machine_id: str
    label: str
    cache: JarvisRemoteCache
    states: dict[str, str] = field(default_factory=dict)
    #: Last raw status seen per qualified session / external key.
    raw: dict[str, str] = field(default_factory=dict)
    _last_refresh: float = field(default=float("-inf"), repr=False)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _intake(core: Any) -> Any:
        jarvis = getattr(core, "jarvis", None)
        if jarvis is None or not jarvis.running:
            return None
        return jarvis.intake

    @staticmethod
    def _forget(core: Any, key: str) -> None:
        """Drop the digest and unsent wakes of a remote item that went off or away."""
        jarvis = getattr(core, "jarvis", None)
        intake = getattr(jarvis, "intake", None)
        if intake is not None:
            with contextlib.suppress(Exception):
                intake.forget_session(key)

    def _queue_wake(self, core: Any, key: str, text: str, event: str) -> None:
        intake = self._intake(core)
        if intake is None:
            return
        from snowpea_core.jarvis.intake import WakeItem, _one_line
        from snowpea_core.jarvis.terminals import safe_title

        if intake.budget.exhausted():
            return
        intake.budget.count_wake()
        importance = "high" if event in ("blocked", "error") else "normal"
        item = WakeItem(
            _one_line(safe_title(text), 400), session_id=key, reason=event, importance=importance
        )
        jarvis = getattr(core, "jarvis", None)
        routed = False
        if jarvis is not None:
            with contextlib.suppress(Exception):
                routed = jarvis.engine.effective(key, None).level == "full"
        if routed:
            intake._queue_routed(item)  # noqa: SLF001
        else:
            intake._queue_notice(item)  # noqa: SLF001

    async def _maybe_refresh(self, core: Any) -> None:
        now = time.monotonic()
        if now - self._last_refresh < _REFRESH_MIN_GAP_SEC:
            return
        await self.refresh(core)

    # -- listing ------------------------------------------------------------
    async def refresh(self, core: Any) -> None:
        manager = getattr(core, "machines", None)
        if manager is None:
            return
        self._last_refresh = time.monotonic()
        before = self.cache.visible_keys(self.machine_id)
        term_listed: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            term_listed = await manager.jarvis_proxy_call(self.machine_id, "terminal.list", {})
        if term_listed is not None:
            keep_t = {str(t.get("terminalId")) for t in term_listed.get("terminals") or []
                      if isinstance(t, dict)}
            self.cache.prune_terminals(self.machine_id, keep_t)
        try:
            listed = await manager.jarvis_proxy_call(self.machine_id, "session.list", {})
        except Exception:  # noqa: BLE001
            listed = None
        if listed is not None:
            keep = {str(s.get("sessionId")) for s in listed.get("sessions") or []
                    if isinstance(s, dict)}
            self.cache.prune_sessions(self.machine_id, keep)
        try:
            ext_listed = await manager.jarvis_proxy_call(
                self.machine_id, "external.list", {"allProjects": True, "limit": 200}
            )
        except Exception:  # noqa: BLE001
            ext_listed = None
        if ext_listed is not None:
            keep_ext: set[str] = set()
            for item in ext_listed.get("sessions") or []:
                parts = _ref_parts(item.get("ref")) if isinstance(item, dict) else None
                if parts is not None:
                    keep_ext.add(external_local_key(*parts))
            self.cache.prune_externals(self.machine_id, keep_ext)
        # Seed the raw statuses so the first notification is judged against what was listed.
        for key in self.cache.visible_keys(self.machine_id):
            session = self.cache.session_any(key)
            external = self.cache.external_any(key)
            status = session.status if session is not None else (
                external.status if external is not None else None)
            if status is not None:
                self.raw.setdefault(key, status)
        for key in before - self.cache.visible_keys(self.machine_id):
            self._forget(core, key)

    # -- notifications ------------------------------------------------------
    async def handle(self, core: Any, method: str, params: dict[str, Any]) -> None:
        if method in ("terminal.status", "terminal.exit"):
            await self._handle_terminal(core, method, params)
        elif method == "sessions.changed":
            await self._handle_sessions_changed(core, params)
        elif method == "session.event":
            await self._handle_session_event(core, params)
        elif method == "jarvis.session.changed":
            await self._handle_switch_changed(core, params)
        elif method == "external.state":
            await self._handle_external_state(core, params)
        elif method == "external.event":
            await self._handle_external_event(core, params)

    async def _handle_terminal(self, core: Any, method: str, params: dict[str, Any]) -> None:
        intake = self._intake(core)
        if intake is None:
            return
        terminal_id = str(params.get("terminalId") or "")
        if not terminal_id:
            return
        item = {
            "terminalId": terminal_id,
            "profile": params.get("profile") or "-",
            "title": params.get("title") or "",
            "jarvisOn": self.cache.jarvis_on(self.machine_id, terminal_id),
            "status": params.get("status") or {},
        }
        if not item["jarvisOn"]:
            listed_on = False
            with contextlib.suppress(Exception):
                await self.refresh(core)
                listed_on = self.cache.jarvis_on(self.machine_id, terminal_id)
            if not listed_on:
                return
        key = delegation_key(self.machine_id, terminal_id)
        if method == "terminal.exit":
            event = "exit"
        else:
            event = str((params.get("status") or {}).get("state") or "")
        previous = self.states.get(key)
        self.states[key] = event
        if event == previous or event not in ("blocked", "error", "done", "exit"):
            return
        status = params.get("status") or {}
        kind = status.get("kind")
        what = {
            "blocked": f"is blocked ({kind or 'waiting'})",
            "error": "reports an error",
            "done": "finished its turn",
            "exit": "exited",
        }[event]
        self._queue_wake(
            core,
            key,
            f'[{self.label}] remote terminal {terminal_id} ({item.get("profile")}) {what}',
            event,
        )

    def _touch_digest(self, core: Any, key: str, status: str, title: str | None) -> None:
        intake = self._intake(core)
        if intake is None:
            return
        digest = intake._digest_for(key)  # noqa: SLF001
        if digest is None:
            return
        digest.status = status if status in ("running", "error") else "idle"
        if title:
            from snowpea_core.jarvis.terminals import safe_title

            digest.title = f"{self.label}: {safe_title(title)}"

    def _on_status(
        self, core: Any, key: str, noun: str, ident: str, title: str, status: str
    ) -> None:
        """Judge a status change of a remote session or external session; wake on the news."""
        previous = self.raw.get(key)
        self.raw[key] = status
        self._touch_digest(core, key, status, title)
        if status == previous:
            return
        if status == "error":
            event, what = "error", "reports an error"
        elif status in _BLOCKED_STATUSES:
            kind = "approval" if status == "awaiting_approval" else "question"
            event, what = "blocked", f"is blocked (a {kind} waits)"
        elif status == "idle" and previous == "running":
            event, what = "done", "finished its turn"
        else:
            return
        name = f'"{title}" ' if title else ""
        self._queue_wake(
            core, key, f"[{self.label}] {noun} {name}({ident}) {what}", event
        )

    async def _handle_sessions_changed(self, core: Any, params: dict[str, Any]) -> None:
        if self._intake(core) is None:
            return
        sid = str(params.get("sessionId") or "")
        reason = str(params.get("reason") or "")
        if not sid:
            if reason == "deleted":
                await self._maybe_refresh(core)
            return
        key = delegation_key(self.machine_id, sid)
        if reason in ("closed", "deleted"):
            self.cache.drop_session(self.machine_id, sid)
            self.raw.pop(key, None)
            self._forget(core, key)
            return
        row = self.cache.session_any(key)
        if row is None:
            await self._maybe_refresh(core)
            row = self.cache.session_any(key)
            if row is None:
                return
        was_on = row.jarvis_on
        if params.get("jarvisOn") is not None:
            row.jarvis_on = bool(params.get("jarvisOn")) and not params.get("jarvisLocked")
        if params.get("title"):
            row.title = str(params["title"])
        status = params.get("status")
        if status:
            row.status = _row_status(status)
        if not row.jarvis_on:
            if was_on:
                self._forget(core, key)
            return
        if status:
            self._on_status(core, key, "remote session", sid, row.title, row.status)

    async def _handle_session_event(self, core: Any, params: dict[str, Any]) -> None:
        intake = self._intake(core)
        sid = str(params.get("sessionId") or "")
        if intake is None or not sid or not self.cache.jarvis_on_session(self.machine_id, sid):
            return
        key = delegation_key(self.machine_id, sid)
        row = self.cache.session_any(key)
        title = row.title if row is not None else ""
        await self._forward_event(
            core, key, "remote session", sid, title,
            str(params.get("kind") or ""), params,
        )

    async def _forward_event(
        self,
        core: Any,
        key: str,
        noun: str,
        ident: str,
        title: str,
        kind: str,
        params: dict[str, Any],
    ) -> None:
        """Hand one event of a remote session to the intake under its qualified key.

        ``turn.done`` is judged here (so the wake names the machine and is not doubled
        by the ``sessions.changed`` the same turn end causes); everything else updates
        the intake's digest, and the person's own message pauses Jarvis there.
        """
        intake = self._intake(core)
        if intake is None:
            return
        payload = params.get("payload") or {}
        if kind == "turn.done":
            reason = str(payload.get("reason") or "complete")
            if reason == "interrupted":
                self.raw[key] = "idle"
                return
            target = "error" if reason == "error" else "idle"
            if self.raw.get(key) == target:
                return
            if self.raw.get(key) != "running":
                self.raw[key] = "running"
            self._on_status(core, key, noun, ident, title, target)
            return
        if kind == "turn.started":
            self.raw[key] = "running"
        await intake._on_event(  # noqa: SLF001
            {
                "sessionId": key,
                "kind": kind,
                "payload": payload,
                "ts": params.get("ts"),
            }
        )

    async def _handle_switch_changed(self, core: Any, params: dict[str, Any]) -> None:
        """``jarvis.session.changed``: a switch flipped on the remote machine."""
        tid = str(params.get("terminalId") or "")
        if tid:
            key = delegation_key(self.machine_id, tid)
            enabled = bool(params.get("enabled"))
            row = self.cache.get(key) or self.cache._rows.get(key)  # noqa: SLF001
            if row is None:
                await self._maybe_refresh(core)
                row = self.cache._rows.get(key)  # noqa: SLF001
            if row is not None:
                row.jarvis_on = enabled
            if not enabled:
                self._forget(core, key)
            return
        sid = str(params.get("sessionId") or "")
        ref = params.get("external")
        enabled = bool(params.get("enabled"))
        parts = _ref_parts(ref) if ref else None
        if parts is not None:
            key = delegation_key(self.machine_id, external_local_key(*parts))
            row_ext = self.cache.external_any(key)
            if row_ext is None:
                await self._maybe_refresh(core)
                return
            row_ext.jarvis_on = enabled
        elif sid:
            key = delegation_key(self.machine_id, sid)
            row_ses = self.cache.session_any(key)
            if row_ses is None:
                await self._maybe_refresh(core)
                return
            row_ses.jarvis_on = enabled
        else:
            return
        if not enabled:
            self._forget(core, key)
            self.raw.pop(key, None)
        else:
            await self._maybe_refresh(core)

    async def _handle_external_state(self, core: Any, params: dict[str, Any]) -> None:
        if self._intake(core) is None:
            return
        parts = _ref_parts(params.get("ref"))
        if parts is None:
            return
        local = external_local_key(*parts)
        key = delegation_key(self.machine_id, local)
        row = self.cache.external_any(key)
        if row is None:
            await self._maybe_refresh(core)
            row = self.cache.external_any(key)
            if row is None:
                return
        row.status = _row_status(params.get("status"))
        if params.get("host"):
            row.host = str(params["host"])
        if not row.jarvis_on:
            return
        self._on_status(core, key, "remote external session", local, row.title, row.status)

    async def _handle_external_event(self, core: Any, params: dict[str, Any]) -> None:
        intake = self._intake(core)
        parts = _ref_parts(params.get("ref"))
        if intake is None or parts is None:
            return
        local = external_local_key(*parts)
        key = delegation_key(self.machine_id, local)
        row = self.cache.get_external(key)
        if row is None:
            return
        await self._forward_event(
            core, key, "remote external session", local, row.title,
            str(params.get("kind") or ""), params,
        )


async def jarvis_proxy_call(core: Any, machine_id: str, method: str, params: dict[str, Any]) -> Any:
    from snowpea_core.server.machine_handlers import ensure_manager

    return await ensure_manager(core).jarvis_proxy_call(machine_id, method, params)


#: Notifications the relay hands to :meth:`RemoteJarvisRelay.handle`.
_RELAYED = frozenset({
    "terminal.status",
    "terminal.exit",
    "sessions.changed",
    "session.event",
    "jarvis.session.changed",
    "external.state",
    "external.event",
})


async def _periodic_refresh(relay: RemoteJarvisRelay, core: Any) -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SEC)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await relay.refresh(core)


async def _relay_jarvis(machine_id: str, manager: Any, client: Any) -> None:
    label = machine_label(manager, machine_id)
    relay = RemoteJarvisRelay(machine_id=machine_id, label=label, cache=manager._jarvis_cache)  # noqa: SLF001
    with contextlib.suppress(Exception):
        await relay.refresh(manager.core)
    periodic = asyncio.create_task(_periodic_refresh(relay, manager.core))
    try:
        async for frame in client.notifications():
            method = str(frame.get("method", ""))
            params = dict(frame.get("params") or {})
            if method == "terminal.list":
                for item in params.get("terminals") or []:
                    if isinstance(item, dict):
                        manager._jarvis_cache.upsert(machine_id, item)  # noqa: SLF001
                continue
            if method == "terminal.created":
                term = params.get("terminal") or {}
                if isinstance(term, dict):
                    manager._jarvis_cache.upsert(machine_id, term)  # noqa: SLF001
                continue
            if method in _RELAYED:
                with contextlib.suppress(Exception):
                    await relay.handle(manager.core, method, params)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.debug("jarvis relay for machine %s ended", machine_id, exc_info=True)
    finally:
        periodic.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await periodic


def attach_jarvis_remote(service: Any) -> None:
    """Hook MachineManager reconnects into Jarvis remote relays (idempotent)."""
    core = service.core
    manager = getattr(core, "machines", None)
    if manager is None:
        return
    hooks = getattr(service, "hooks", None)
    if hooks is not None and not any(
        getattr(source, "__jarvis_remote_service__", None) is service
        for source in hooks.overview_sources
    ):
        def source() -> list[Any]:
            return overview_rows(service)

        source.__jarvis_remote_service__ = service  # type: ignore[attr-defined]
        hooks.overview_sources.append(source)
    if getattr(manager, "_jarvis_remote_attached", False):
        return
    manager._jarvis_remote_attached = True  # noqa: SLF001
    manager._jarvis_cache = JarvisRemoteCache()  # noqa: SLF001
    manager._jarvis_relays: dict[str, asyncio.Task[None]] = {}  # noqa: SLF001

    async def ensure_relay(machine_id: str) -> None:
        if not service.running:
            return
        live = manager._live.get(machine_id)  # noqa: SLF001
        if live is None or live.status != "connected":
            return
        old = manager._jarvis_relays.pop(machine_id, None)  # noqa: SLF001
        if old is not None:
            old.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await old
        await manager.close_jarvis_client(machine_id)
        try:
            client = await manager.jarvis_client(machine_id)
        except Exception:  # noqa: BLE001
            return
        manager._jarvis_relays[machine_id] = asyncio.create_task(  # noqa: SLF001
            _relay_jarvis(machine_id, manager, client)
        )

    manager.ensure_jarvis_relay = ensure_relay  # type: ignore[attr-defined]

    original_disconnect = manager.disconnect

    def disconnect(machine_id: str) -> None:
        task = manager._jarvis_relays.pop(machine_id, None)  # noqa: SLF001
        if task is not None and manager._loop is not None:
            manager._loop.call_soon_threadsafe(task.cancel)
        manager._jarvis_cache.drop_machine(machine_id)  # noqa: SLF001
        original_disconnect(machine_id)

    manager.disconnect = disconnect  # type: ignore[method-assign]

    if manager._loop is not None:
        for mid, live in list(manager._live.items()):  # noqa: SLF001
            if live.status == "connected":
                asyncio.run_coroutine_threadsafe(ensure_relay(mid), manager._loop)


__all__ = [
    "JarvisRemoteCache",
    "RemoteExternalSessionRow",
    "RemoteJarvisRelay",
    "RemoteSnowpeaSessionRow",
    "RemoteTerminalCache",
    "attach_jarvis_remote",
    "delegation_key",
    "external_local_key",
    "jarvis_proxy_call",
    "machine_label",
    "overview_rows",
    "parse_external_local",
    "qualify",
    "remote_seed",
    "remote_visible",
    "split_qualified_id",
]
