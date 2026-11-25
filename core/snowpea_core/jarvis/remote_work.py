"""Jarvis's tools on a remote machine's snowpea and external sessions (M42, 1.77.0).

A remote session is named ``<machineId>:<sessionId>``, a remote external session
``<machineId>:ext:<cli>:<id>``.  Jarvis sees one only while its Jarvis switch is on
on its own machine (``session.list`` / ``external.list`` say so, and the relay keeps
the answer fresh), and every call goes through ``jarvis_proxy_call`` and its gates:
no trust, no dangerous options, nothing for a session whose switch is off.

The checks the local tools make (the person's request or delegated work, the
person's pause) are made by the tools before they call in here.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from snowpea_core.jarvis.terminals import is_terminal_id
from snowpea_core.machines.jarvis_remote import (
    jarvis_proxy_call,
    machine_label,
    remote_visible,
    split_qualified_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.jarvis.service import JarvisService
    from snowpea_core.jarvis.tools import Target
    from snowpea_core.tools.registry import ToolResult


def _cache(service: JarvisService) -> Any:
    machines = getattr(service.core, "machines", None)
    return getattr(machines, "_jarvis_cache", None) if machines is not None else None


def _label(service: JarvisService, machine_id: str) -> str:
    machines = getattr(service.core, "machines", None)
    return machine_label(machines, machine_id) if machines is not None else machine_id[:8]


async def _call(
    service: JarvisService, machine_id: str, method: str, params: dict[str, Any]
) -> Any:
    """``jarvis_proxy_call`` with a refusal or a remote error read as a tool refusal."""
    from snowpea_core.jarvis.tools import Refused
    from snowpea_core.server.errors import RpcError

    try:
        return await jarvis_proxy_call(service.core, machine_id, method, params)
    except RpcError as exc:
        raise Refused(f"{exc.message} ({exc.code})") from exc


def _host(service: JarvisService, machine_id: str) -> str:
    return f"machine:{_label(service, machine_id)}"


async def remote_target(service: JarvisService, qualified_id: str) -> Target:
    """The target of ``<machineId>:<sessionId>`` (a snowpea session) or ``<machineId>:ext:…``."""
    from snowpea_core.jarvis.tools import Refused, Target

    machine_id, local_id = split_qualified_id(qualified_id)
    cache = _cache(service)
    unknown = Refused(f"no session {qualified_id!r} that Jarvis supervises")
    if machine_id is None or cache is None:
        raise unknown
    if is_terminal_id(local_id):
        raise Refused("that is a terminal on a remote machine; use the terminal tools")
    if local_id.startswith("ext:"):
        found, _row = await external_target(service, qualified_id)
        return found
    with contextlib.suppress(Exception):
        await jarvis_proxy_call(service.core, machine_id, "session.list", {})
    row = cache.get_session(qualified_id)
    if row is None or not remote_visible(service.core, qualified_id):
        raise unknown
    return Target(
        qualified_id,
        None,
        None,
        False,
        row.title,
        _host(service, machine_id),
        machine_id=machine_id,
        remote_id=local_id,
    )


async def external_target(
    service: JarvisService, qualified_id: str
) -> tuple[Target, dict[str, Any]]:
    """The target of a remote external session and its row (shaped like ``ExternalSessionRow``)."""
    from snowpea_core.jarvis.tools import Refused, Target
    from snowpea_core.machines.jarvis_remote import parse_external_local

    machine_id, local_id = split_qualified_id(qualified_id)
    cache = _cache(service)
    unknown = Refused(f"no external session {qualified_id!r} that Jarvis supervises")
    parts = parse_external_local(local_id)
    if machine_id is None or cache is None or parts is None:
        raise unknown
    ref = {"cli": parts[0], "id": parts[1]}
    known = cache.external_any(qualified_id)
    if known is not None and known.jarvis_on:
        # One fresh row: its status, mode and switch.
        with contextlib.suppress(Exception):
            got = await jarvis_proxy_call(service.core, machine_id, "external.get", {"ref": ref})
            cache.upsert_external(machine_id, dict(got.get("session") or {}))
    else:
        with contextlib.suppress(Exception):
            await jarvis_proxy_call(
                service.core, machine_id, "external.list", {"allProjects": True, "limit": 200}
            )
    row = cache.get_external(qualified_id)
    if row is None or not remote_visible(service.core, qualified_id):
        raise unknown
    found = Target(
        qualified_id,
        None,
        None,
        False,
        row.title,
        _host(service, machine_id),
        machine_id=machine_id,
        remote_id=row.local_key,
    )
    return found, row.as_row_dict()


# ---------------------------------------------------------------------------
# snowpea sessions
# ---------------------------------------------------------------------------


async def remote_session_read(
    service: JarvisService,
    target: Target,
    *,
    view: str,
    limit: int,
    before_seq: int | None,
) -> str:
    """The digest the intake keeps, or the remote session's trajectory rows."""
    from snowpea_core.jarvis.tools import _digest_text

    assert target.machine_id and target.remote_id
    if view == "digest":
        row = _cache(service).get_session(target.session_id)
        head = f"machine: {_label(service, target.machine_id)}"
        if row is not None:
            head += f"; remote status: {row.status}; mode: {row.mode or '-'}"
        return f"{head}\n{_digest_text(service, target)}"
    result = await _call(
        service,
        target.machine_id,
        "session.trajectory",
        {"sessionId": target.remote_id, "limit": 2000},
    )
    rows = [
        r for r in result.get("rows") or []
        if isinstance(r, dict) and (before_seq is None or int(r.get("seq") or 0) < before_seq)
    ]
    page = rows[-limit:]
    lines = [f"trajectory of {target.session_id}: {len(page)} of {len(rows)} rows"]
    if len(rows) > len(page) and page:
        lines.append(f"(older rows: call again with beforeSeq={page[0].get('seq')})")
    for row in page:
        head = f"[{row.get('seq')}] {row.get('kind')}"
        if row.get("title"):
            head += f" {row['title']}"
        if row.get("ok") is False:
            head += " (failed)"
        lines.append(head)
        if row.get("text"):
            lines.append("  " + str(row["text"]).replace("\n", "\n  "))
    return "\n".join(lines)


async def remote_session_prompt(
    service: JarvisService,
    ctx: Any,
    target: Target,
    text: str,
    when_busy: str | None,
) -> ToolResult:
    """``session.prompt`` on the remote machine, as Jarvis (marked with its prefix)."""
    from snowpea_core.jarvis.tools import PROMPT_PREFIX, _access, _cause_for, _result

    assert target.machine_id and target.remote_id
    row = _cache(service).get_session(target.session_id)
    _cause_for(ctx, service, (row.mode if row is not None else None) or "accept",
               "prompting a remote session")
    params: dict[str, Any] = {"sessionId": target.remote_id, "text": PROMPT_PREFIX + text}
    if when_busy:
        params["whenBusy"] = when_busy
    sent = await _call(service, target.machine_id, "session.prompt", params)
    lbl = _label(service, target.machine_id or "")
    service.record_decision(
        kind="prompt",
        decided_by="jarvis",
        summary=f"[{lbl}] asked {target.title or target.session_id}: {text}"[:300],
        session_id=target.session_id,
        project=None,
        importance="normal",
    )
    await _access(service, target, "prompt", text[:160])
    return _result(
        f"sent to {target.session_id} on {_label(service, target.machine_id)} "
        f"(turn {sent.get('turnId')})",
        turnId=sent.get("turnId"),
    )


async def remote_session_steer(
    service: JarvisService, ctx: Any, target: Target, text: str
) -> ToolResult:
    from snowpea_core.jarvis.tools import PROMPT_PREFIX, _access, _cause_for, _result

    assert target.machine_id and target.remote_id
    row = _cache(service).get_session(target.session_id)
    _cause_for(ctx, service, (row.mode if row is not None else None) or "accept",
               "steering a remote session")
    await _call(
        service,
        target.machine_id,
        "session.steer",
        {"sessionId": target.remote_id, "text": PROMPT_PREFIX + text},
    )
    lbl = _label(service, target.machine_id or "")
    service.record_decision(
        kind="prompt",
        decided_by="jarvis",
        summary=f"[{lbl}] steered {target.title or target.session_id}: {text}"[:300],
        session_id=target.session_id,
        project=None,
        importance="normal",
    )
    await _access(service, target, "steer", text[:160])
    return _result(f"steered {target.session_id} on {_label(service, target.machine_id)}")


async def remote_session_interrupt(
    service: JarvisService, target: Target, reason: str
) -> ToolResult:
    from snowpea_core.jarvis.tools import _access, _result

    assert target.machine_id and target.remote_id
    await _call(
        service, target.machine_id, "session.interrupt", {"sessionId": target.remote_id}
    )
    lbl = _label(service, target.machine_id or "")
    service.record_decision(
        kind="interrupt",
        decided_by="jarvis",
        summary=f"[{lbl}] stopped {target.title or target.session_id}"
        + (f": {reason}" if reason else ""),
        session_id=target.session_id,
        project=None,
        reason=reason,
        importance="high",
    )
    await _access(service, target, "interrupt", reason or "stopped the running turn")
    return _result(f"asked {target.session_id} to stop its running turn")


# ---------------------------------------------------------------------------
# external sessions
# ---------------------------------------------------------------------------


async def remote_external_read(
    service: JarvisService,
    found: Target,
    row: dict[str, Any],
    *,
    before_seq: int | None,
    limit: int,
) -> ToolResult:
    from snowpea_core.jarvis.external import _entry, _mode_text
    from snowpea_core.jarvis.tools import READ_MAX_CHARS, _result, fence

    assert found.machine_id and found.remote_id
    ref = _ref_of(found)
    params: dict[str, Any] = {"ref": ref, "limit": limit}
    if before_seq:
        params["beforeSeq"] = before_seq
    page = await _call(service, found.machine_id, "external.read", params)
    events = [e for e in page.get("events") or [] if isinstance(e, dict)]
    lines = [
        f"{found.session_id} — {found.title or 'untitled'} "
        f"({_label(service, found.machine_id)}; {row['status']}, host {row['host']}, "
        f"mode {_mode_text(row)}"
        + ("" if row.get("controlled", True) else "; UNCONTROLLED") + ")"
    ]
    if page.get("hasMore") and events:
        lines.append(f"(older entries: call again with beforeSeq={events[0].get('seq')})")
    lines.extend(line for line in (_entry(event) for event in events) if line)
    text = "\n".join(lines)
    if len(text) > READ_MAX_CHARS:
        text = text[: READ_MAX_CHARS - 40] + "\n… [cut; read an older page with beforeSeq]"
    await _external_access(service, found, "read", f"read {len(events)} entries")
    return _result(fence(f"external:{found.session_id}", text), count=len(events))


async def remote_external_send(
    service: JarvisService, found: Target, row: dict[str, Any], text: str
) -> ToolResult:
    from snowpea_core.jarvis.external import _mode_text
    from snowpea_core.jarvis.tools import PROMPT_PREFIX, _result

    assert found.machine_id and found.remote_id
    ref = _ref_of(found)
    sent = await _call(
        service,
        found.machine_id,
        "external.send",
        {"ref": ref, "text": PROMPT_PREFIX + text},
    )
    lbl = _label(service, found.machine_id or "")
    service.record_decision(
        kind="prompt",
        decided_by="jarvis",
        summary=f"[{lbl}] asked {found.title or found.session_id}: {text}"[:300],
        session_id=found.session_id,
        project=None,
        importance="normal",
    )
    await _external_access(service, found, "prompt", text[:160])
    return _result(
        f"sent to {found.session_id} (turn {sent.get('turnId')}); it runs in mode "
        f"{_mode_text(row)}" + ("" if row.get("controlled", True)
                                else ", which snowpea does not gate"),
        turnId=sent.get("turnId"),
    )


async def remote_external_interrupt(
    service: JarvisService, found: Target, reason: str
) -> ToolResult:
    from snowpea_core.jarvis.tools import _result

    assert found.machine_id and found.remote_id
    await _call(service, found.machine_id, "external.interrupt", {"ref": _ref_of(found)})
    lbl = _label(service, found.machine_id or "")
    service.record_decision(
        kind="interrupt",
        decided_by="jarvis",
        summary=f"[{lbl}] stopped {found.title or found.session_id}"
        + (f": {reason}" if reason else ""),
        session_id=found.session_id,
        project=None,
        reason=reason,
        importance="high",
    )
    await _external_access(service, found, "interrupt", reason or "stopped the running turn")
    return _result(f"asked {found.session_id} to stop its running turn")


def _ref_of(found: Target) -> dict[str, str]:
    from snowpea_core.machines.jarvis_remote import parse_external_local

    parts = parse_external_local(found.remote_id or "")
    assert parts is not None
    return {"cli": parts[0], "id": parts[1]}


async def _external_access(
    service: JarvisService, found: Target, action: str, summary: str
) -> None:
    await service.record_access(
        session_id=found.session_id,
        project=None,
        host=found.host,
        action=action,
        summary=summary,
        external=None,
    )


def remote_external_lines(service: JarvisService, *, cli: str | None) -> list[str]:
    """``external_list`` lines for the remote external sessions Jarvis may see."""
    from snowpea_core.jarvis.external import _mode_text
    from snowpea_core.machines.jarvis_remote import qualify

    cache = _cache(service)
    if cache is None:
        return []
    lines: list[str] = []
    for row in cache.visible_external_rows():
        if cli is not None and row.cli != cli:
            continue
        if not remote_visible(service.core, qualify(row.machine_id, row.local_key)):
            continue
        key = qualify(row.machine_id, row.local_key)
        data = row.as_row_dict()
        lines.append(" | ".join([
            key,
            _label(service, row.machine_id),
            str(row.title or "-")[:60],
            row.status,
            row.host + ("" if row.can_send else " (view only)"),
            _mode_text(data),
            "yes" if row.controlled else "NO (snowpea cannot gate it)",
            str(row.updated_at or "-"),
            str(row.preview or "")[:120],
        ]))
    return lines


__all__ = [
    "external_target",
    "remote_external_interrupt",
    "remote_external_lines",
    "remote_external_read",
    "remote_external_send",
    "remote_session_interrupt",
    "remote_session_prompt",
    "remote_session_read",
    "remote_session_steer",
    "remote_target",
]
