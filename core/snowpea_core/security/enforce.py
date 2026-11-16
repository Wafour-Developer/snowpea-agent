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
) -> Context:
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
        unknown_code=unknown_code(settings),
    )


def unknown_code(settings: Any) -> str:
    """``security.unknownCode`` of the GLOBAL settings (``ask`` unless it says ``allow``).

    A project file cannot set it: it would take a question away.
    """
    value = getattr(getattr(settings, "security", None), "unknownCode", "ask")
    return "allow" if value == "allow" else "ask"


def lists_for(settings: Any, workdir: Any) -> Lists:
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
) -> Verdict:
    """The verdict for one call; never raises."""
    lists = lists_for(settings, workdir)
    ctx = context(settings=settings, home=home, workdir=workdir, projects=projects,
                  locale=locale)
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
                   tags: Iterable[str] = ()) -> Verdict:
    paths = getattr(core, "paths", None)
    return judge(
        settings=getattr(core, "settings", None),
        home=getattr(paths, "home", None),
        workdir=getattr(session, "workdir", None),
        tool=tool,
        args=args,
        tags=tags,
        projects=live_projects(core),
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
            with (Path(paths.logs_dir) / "security.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
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


__all__ = [
    "announce",
    "context",
    "judge",
    "judge_for_core",
    "lists_for",
    "live_projects",
    "refresh_projects",
]
