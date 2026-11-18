"""The daemon side of the security lists: one gate every caller uses.

:func:`judge` reads the effective lists for a folder (global settings plus the
project's additions), builds the :class:`~snowpea_core.security.check.Context`
(the registered projects, ``$SNOWPEA_HOME``, the user's home) and runs
:func:`~snowpea_core.security.check.check_call`.  :func:`announce` reports a
block: the ``security.blocked`` event to every person's surface, a line in
``$SNOWPEA_HOME/logs/security.jsonl`` and, while Jarvis mode keeps a ledger, a
ledger record.

Callers, all before anything else decides (mode, allowlist, session cache,
Jarvis routing): the agent loop's tool execution (``agent/loop.py``), the
approval queue (``permissions/approval_queue.py``), the external CLIs'
bridged approvals (``external/manager.py``).  Jarvis's own tools and scheduled
workers run through the agent loop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from snowpea_core.security import written
from snowpea_core.security.check import Context, Verdict, check_call
from snowpea_core.security.lists import Lists, effective

log = logging.getLogger("snowpea.security")

#: Seconds a listing of the registered projects (workspaces) is reused.
PROJECTS_TTL_SEC = 30.0


def _user_home() -> Path:
    try:
        return Path.home()
    except (OSError, RuntimeError):
        return Path("/nonexistent-home")


def live_projects(core: Any) -> list[str]:
    """Every project root known without waiting: Jarvis's, live sessions', the cached sidebar."""
    found: list[str] = []
    jarvis = getattr(core, "jarvis", None)
    projects = getattr(jarvis, "projects", None)
    if projects is not None:
        with contextlib.suppress(Exception):
            found.extend(projects.known_roots())
    sessions = getattr(core, "sessions", None)
    with contextlib.suppress(Exception):
        for session in sessions.all() if sessions is not None else ():
            workdir = getattr(session, "workdir", None)
            if workdir and not getattr(session, "parent_session_id", None):
                found.append(str(workdir))
    cached = getattr(core, "_security_projects", None)
    if cached:
        found.extend(cached[1])
    return list(dict.fromkeys(found))


async def refresh_projects(core: Any) -> None:
    """Re-read the sidebar's workspaces into the cache :func:`live_projects` uses."""
    cached = getattr(core, "_security_projects", None)
    if cached and time.monotonic() - cached[0] < PROJECTS_TTL_SEC:
        return
    store = getattr(core, "store", None)
    if store is None:
        return
    try:
        from snowpea_core.session import workspaces

        rows = await workspaces.list_all(store)
        paths = [str(row["path"]) for row in rows if row.get("path")]
    except Exception:  # noqa: BLE001 - the cache is a convenience
        log.debug("could not list workspaces for the security lists", exc_info=True)
        paths = list(cached[1]) if cached else []
    with contextlib.suppress(Exception):
        core._security_projects = (time.monotonic(), tuple(paths))


def context(
    *,
    settings: Any = None,
    home: Any = None,
    workdir: Any = None,
    projects: Iterable[str] = (),
    locale: str | None = None,
    host_tool: bool = False,
    dry_run: bool = False,
) -> Context:
    """``dry_run``: a judgement only (``security.check``), nothing is recorded."""
    work = None
    if workdir:
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            work = Path(str(workdir)).expanduser().resolve()
    return Context(
        workdir=work,
        user_home=_user_home(),
        home=Path(str(home)) if home else None,
        projects=tuple(str(p) for p in projects),
        locale=locale,
        unknown_code=effective_unknown_code(settings),
        host_tool=bool(host_tool),
        checks_off=not enabled(settings),
        dry_run=bool(dry_run),
    )


def unknown_code(settings: Any) -> str:
    """``security.unknownCode`` of the GLOBAL settings: ``ask``, ``auto`` or ``allow``
    (no settings at all: ``ask``; anything unrecognised: ``ask``).

    A project file cannot set it: it would take a question away.
    """
    value = getattr(getattr(settings, "security", None), "unknownCode", "ask")
    return value if value in ("auto", "allow") else "ask"


def enabled(settings: Any) -> bool:
    """``security.enabled`` of the GLOBAL settings (1.58.0); only ``False`` is off."""
    return getattr(getattr(settings, "security", None), "enabled", True) is not False


def effective_unknown_code(settings: Any) -> str:
    """What the check applies: ``allow`` while the switch is off, else :func:`unknown_code`."""
    return unknown_code(settings) if enabled(settings) else "allow"


#: Who switched the checks off and when (1.58.0): under ``$SNOWPEA_HOME/security/``,
#: a write-level control folder no session's tool may change (next to
#: ``home-projects.json``), so a session cannot rewrite who turned them off.
SWITCH_RECORD: tuple[str, ...] = ("security", "switch.json")
#: ``security.get`` problem code while the checks are off.
DISABLED_CODE = "security-disabled"
DISABLED_NOTE = (
    "security checks are off (security.enabled: false): blocked commands and protected "
    "paths are not applied and code snowpea cannot read ahead is not asked about; "
    "snowpea's own controls and the mode rules still apply")


def switch_state(settings: Any, home: Any) -> dict[str, Any]:
    """``{enabled, disabledBy, disabledAt}``: who/when only while off and recorded."""
    on = enabled(settings)
    state: dict[str, Any] = {"enabled": on, "disabledBy": None, "disabledAt": None}
    if on or not home:
        return state
    try:
        record = json.loads(Path(str(home)).joinpath(*SWITCH_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(record, dict) and record.get("enabled") is False:
        by, at = record.get("by"), record.get("at")
        state["disabledBy"] = by if isinstance(by, str) else None
        state["disabledAt"] = at if isinstance(at, str) else None
    return state


def reconcile_switch(settings: Any, home: Any) -> None:
    """At the daemon's start: when ``settings.json`` says otherwise than the record
    (an edit while no daemon ran), record it, by ``settings.json``; never raises.
    No record counts as on (what every home had before 1.58.0)."""
    if not home:
        return
    try:
        record = json.loads(Path(str(home)).joinpath(*SWITCH_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    recorded = not (isinstance(record, dict) and record.get("enabled") is False)
    now = enabled(settings)
    note_switch(home, recorded, now, by="settings.json", method="startup")


def note_switch(home: Any, before: bool, after: bool, *, by: str | None,
                method: str = "settings.set") -> None:
    """Record a change of the switch: the who/when file and one audit line
    (``$SNOWPEA_HOME/logs/security.jsonl``, kind ``security-switch``).  Nothing
    when ``before == after``; never raises."""
    if before == after or not home:
        return
    import datetime

    from snowpea_core.security.tamper import audit
    from snowpea_core.util.atomic import write_atomic

    stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    try:
        record = Path(str(home)).joinpath(*SWITCH_RECORD)
        record.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(record, json.dumps({"enabled": after, "by": by, "at": stamp}) + "\n")
    except Exception:  # noqa: BLE001 - the change itself stands
        log.warning("could not record the security switch", exc_info=True)
    audit(home, {"kind": "security-switch", "enabled": after, "by": by, "at": stamp,
                 "method": method})
    log.warning("security checks switched %s by %s", "on" if after else "off", by or "?")


#: Where the one-time ``security.unknownCode`` migration of 0.2.90 is recorded
#: (under ``$SNOWPEA_HOME``; ``security/`` is a write-level control folder, so no
#: tool removes the marker to run the migration again).
UNKNOWN_CODE_MARKER: tuple[str, ...] = ("security", "unknown-code-migrated.json")
#: What ``security.get`` says while this daemon run migrated the value.
UNKNOWN_CODE_MIGRATED_NOTE = (
    "snowpea 0.2.90 changed 'Commands and scripts snowpea cannot read ahead' from ask to "
    "auto (not asked in auto mode, asked in plan and accept); set it back to ask on this "
    "screen if you want every such command asked")


def migrate_unknown_code(settings: Any, paths: Any) -> bool:
    """Once per home, at the daemon's start (owner decision 2026-11-17): a GLOBAL
    ``settings.json`` that stores ``security.unknownCode: "ask"`` — what every
    0.2.88/0.2.89 file holds, since the old default was written out — becomes
    ``"auto"``, the default since 0.2.90.  Returns True when it changed the value.

    The marker makes it once: after it exists nothing is migrated again, so a
    person who sets ``ask`` later keeps it.  ``allow``, ``auto`` or no key: only
    the marker is written.  A degraded or unreadable settings file: nothing at
    all (the value stays ``ask``; the next start looks again).  Project files are
    never read here: the setting is global only.  Never raises.
    """
    import datetime

    from snowpea_core.util.atomic import write_atomic

    try:
        marker = Path(paths.home).joinpath(*UNKNOWN_CODE_MARKER)
        if os.path.lexists(marker):
            return False
        degraded = settings.degraded_sections()
        if "security" in degraded or "*" in degraded:
            return False
        try:
            raw = json.loads(Path(paths.settings_json).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        section = raw.get("security") if isinstance(raw, dict) else None
        stored = section.get("unknownCode") if isinstance(section, dict) else None
        changed = False
        if stored == "ask":
            settings.security.unknownCode = "auto"
            settings.set_section("security")
            settings.save(paths)
            changed = True
        marker.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        write_atomic(marker, json.dumps({"at": stamp, "from": stored,
                                         "to": "auto" if changed else stored}) + "\n")
        if changed:
            log.info("security.unknownCode migrated once from 'ask' to 'auto' (0.2.90 default)")
        return changed
    except Exception:  # noqa: BLE001 - a failed migration leaves the setting as it was
        log.warning("could not migrate security.unknownCode", exc_info=True)
        return False


def lists_for(settings: Any, workdir: Any) -> Lists:
    """The lists in force: none while ``security.enabled`` is false (1.58.0)."""
    if not enabled(settings):
        return Lists()
    try:
        return effective(settings, workdir)
    except Exception:  # noqa: BLE001 - unreadable lists fail closed to the defaults
        log.warning("could not read the security lists", exc_info=True)
        from snowpea_core.security.lists import from_raw

        return from_raw({})


def judge(
    *,
    settings: Any,
    home: Any,
    workdir: Any,
    tool: str,
    args: Mapping[str, Any] | None,
    tags: Iterable[str] = (),
    projects: Iterable[str] = (),
    locale: str | None = None,
    host_tool: bool = False,
) -> Verdict:
    """The verdict for one call; never raises.

    ``host_tool``: the call is dispatched to a tool the session's connected
    host registered and runs (:func:`~snowpea_core.tools.host_tools.runs_on_host`).
    """
    lists = lists_for(settings, workdir)
    ctx = context(settings=settings, home=home, workdir=workdir, projects=projects,
                  locale=locale, host_tool=host_tool)
    if home:
        # What the agent wrote is remembered under this home (review 6, M-8).
        written.set_home(home)
    return check_call(tool, args or {}, lists, ctx, tags=tags)


def note_written(core: Any, session: Any, paths: Iterable[str]) -> None:
    """Record files a session's call wrote or changed (a tool's paths, what the
    checkpoint scan found changed after a shell line); never raises."""
    try:
        home = getattr(getattr(core, "paths", None), "home", None)
        if home:
            written.set_home(home)
        workdir = getattr(session, "workdir", None)
        found: list[str] = []
        for raw in paths:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                if not workdir:
                    continue
                path = Path(str(workdir)) / path
            found.append(str(path))
        if found:
            written.note(found, getattr(session, "id", None))
    except Exception:  # noqa: BLE001 - a record that fails never breaks a tool call
        log.debug("could not record written paths", exc_info=True)


def judge_for_core(core: Any, session: Any, tool: str, args: Mapping[str, Any] | None,
                   tags: Iterable[str] = (), *, host_tool: bool = False) -> Verdict:
    paths = getattr(core, "paths", None)
    return judge(
        settings=getattr(core, "settings", None),
        home=getattr(paths, "home", None),
        workdir=getattr(session, "workdir", None),
        tool=tool,
        args=args,
        tags=tags,
        projects=live_projects(core),
        host_tool=host_tool,
    )


def _summary(tool: str, args: Mapping[str, Any] | None) -> str:
    from snowpea_core.jarvis.classify import _command_text

    args = args or {}
    command = _command_text(args)
    text = command if command else f"{tool} {json.dumps(dict(args), default=str)[:200]}"
    text = " ".join(str(text).split())
    return text[:240]


async def announce(
    *,
    hub: Any,
    paths: Any,
    session_id: str,
    tool: str,
    args: Mapping[str, Any] | None,
    verdict: Verdict,
    surface: str = "agent",
    core: Any = None,
) -> None:
    """``security.blocked`` to every surface, an audit line, a ledger record."""
    summary = _summary(tool, args)
    payload = {"sessionId": session_id, "tool": tool, "summary": summary,
               "by": verdict.by or "", "why": verdict.why or "", "surface": surface}
    if paths is not None:
        try:
            paths.ensure()
            record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **payload,
                      "match": verdict.match, "kind": verdict.kind}
            from snowpea_core.util.atomic import append_text

            append_text(Path(paths.logs_dir) / "security.jsonl", json.dumps(record) + "\n")
        except (OSError, AttributeError):  # pragma: no cover - the audit log never breaks a turn
            log.warning("could not write the security audit line", exc_info=True)
    log.warning("security list blocked %s in %s: %s (%s)", tool, session_id, summary, verdict.by)
    if core is not None and getattr(getattr(core, "settings", None), "jarvis", None) is not None:
        if getattr(core.settings.jarvis, "enabled", False):
            try:
                from snowpea_core.jarvis.delegation import engine_for

                engine_for(core).ledger.record_decision(
                    kind="escalate", decided_by="auto",
                    summary=f"blocked by the security list: {summary}"[:160],
                    session_id=session_id, tool=tool, decision="deny",
                    reason=f"security:{verdict.by}", importance="normal",
                )
            except Exception:  # noqa: BLE001 - the block itself stands
                log.debug("could not ledger a security block", exc_info=True)
    if hub is not None:
        try:
            await hub.notify("security.blocked", payload)
        except Exception:  # noqa: BLE001 - a dead socket never undoes a block
            log.debug("could not broadcast security.blocked", exc_info=True)


def note_person_answer(
    *,
    paths: Any,
    session_id: str,
    tool: str,
    args: Mapping[str, Any] | None,
    verdict: Verdict,
    decision: str,
    by: str,
    request_id: str,
    answer: Mapping[str, Any] | None = None,
) -> None:
    """A line in ``logs/security.jsonl`` for the person's answer to a blocked call
    put to them (0.2.94): an allow of an edit of snowpea's own security settings,
    trust store or switch record is recorded with what it was.  ``answer``: the
    answering connection (``connection`` kind, ``clientId``,
    ``personConfirmWindow``, ``remote``; security review L3).  Never raises."""
    if paths is None:
        return
    try:
        paths.ensure()
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "event": "person_allowed" if decision == "allow" else "person_denied",
                  "sessionId": session_id, "requestId": request_id, "tool": tool,
                  "summary": _summary(tool, args), "by": verdict.by or "",
                  "why": verdict.why or "", "match": verdict.match, "kind": verdict.kind,
                  "answeredBy": by,
                  "connection": (answer or {}).get("connection"),
                  "clientId": (answer or {}).get("clientId"),
                  "personConfirmWindow": bool((answer or {}).get("personConfirmWindow")),
                  "remote": bool((answer or {}).get("remote"))}
        from snowpea_core.util.atomic import append_text

        append_text(Path(paths.logs_dir) / "security.jsonl", json.dumps(record) + "\n")
    except (OSError, AttributeError):  # pragma: no cover - the audit log never breaks a turn
        log.warning("could not write the security audit line", exc_info=True)
    if decision == "allow":
        log.warning("a person allowed a blocked call once: %s in %s (%s)", tool, session_id,
                    verdict.by)


__all__ = [
    "DISABLED_CODE",
    "announce",
    "note_person_answer",
    "context",
    "effective_unknown_code",
    "enabled",
    "judge",
    "judge_for_core",
    "lists_for",
    "live_projects",
    "note_switch",
    "reconcile_switch",
    "refresh_projects",
    "switch_state",
]
