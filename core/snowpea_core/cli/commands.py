"""Session-less ``snowpea`` subcommands (plan §3.6).

``tools list`` and ``commands list`` are pure RPC lookups — no session, no LLM —
so they double as the install smoke test.  ``daemon status|stop|start`` drives
the daemon process itself.  ``setup`` runs the M3 wizard (Quick/Full/Blank) and
``provider list|login`` inspect and authorise vendors.  What is left is an M4+
placeholder that exits 2.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from snowpea_core.cli.daemon_client import (
    CALL_TIMEOUT_SEC,
    DaemonClient,
    DaemonError,
    RpcCallError,
    ensure_daemon,
    pid_alive,
    read_daemon_json,
)
from snowpea_core.cli.render import (
    EXIT_AGENT_FAILED,
    EXIT_NO_DAEMON,
    EXIT_OK,
    EXIT_USAGE,
)
from snowpea_core.cli.service import service_command
from snowpea_core.config.paths import Paths, resolve_home

#: Subcommands whose implementation lands after M1.  ``team`` left this list
#: in US-020; nothing is a placeholder now.
PLACEHOLDER_SUBCOMMANDS: tuple[str, ...] = ()

#: Seconds to wait for ``daemon stop`` to see the process go away.
STOP_TIMEOUT_SEC = 10.0

#: ``job.runNow`` runs a whole turn before it answers, so it gets its own,
#: much longer, RPC deadline.
JOB_RUN_TIMEOUT_SEC = 900.0


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _fail(message: str, code: int) -> int:
    print(f"snowpea: {message}", file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# tool.list / command.list
# ---------------------------------------------------------------------------


async def _lookup(home: Path | str | None, method: str, key: str) -> list[dict[str, Any]]:
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        result = await client.call(method, {})
    finally:
        await client.close()
    items = result.get(key) or []
    return [item for item in items if isinstance(item, dict)]


async def _call(
    home: Path | str | None,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float | None = CALL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """One RPC round trip against the daemon, started if it is not running."""
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        return await client.call(method, params, timeout=timeout)
    finally:
        await client.close()


async def tools_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea tools list [--json]`` → ``tool.list``."""
    try:
        tools = await _lookup(home, "tool.list", "tools")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"tool.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(tools)
        return EXIT_OK
    if not tools:
        print("no tools registered")
        return EXIT_OK
    width = max(len(str(tool.get("name", ""))) for tool in tools)
    for tool in sorted(tools, key=lambda item: str(item.get("name", ""))):
        name = str(tool.get("name", ""))
        tag = str(tool.get("permissionTag", "?"))
        state = str(tool.get("state", "active"))
        suffix = "" if state == "active" else f" [{state}]"
        print(f"{name:<{width}}  {tag:<8}{suffix} {tool.get('description', '')}".rstrip())
    return EXIT_OK


async def commands_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea commands list [--json]`` → ``command.list``."""
    try:
        commands = await _lookup(home, "command.list", "commands")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"command.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(commands)
        return EXIT_OK
    if not commands:
        print("no commands registered")
        return EXIT_OK
    width = max(len(str(command.get("name", ""))) for command in commands)
    for command in sorted(commands, key=lambda item: str(item.get("name", ""))):
        print(f"/{str(command.get('name', '')):<{width}}  {command.get('summary', '')}".rstrip())
    return EXIT_OK


async def agents_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea agents [--json]`` → ``agent.list``.

    Poll this while ``/ralph`` runs to watch the subagents: each row has a
    ``kind`` (``definition``, ``named`` or ``subagent``) and, for a subagent, a
    ``status`` of queued, running, done or error (AC-04).
    """
    try:
        agents = await _lookup(home, "agent.list", "agents")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"agent.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(agents)
        return EXIT_OK
    if not agents:
        print("no agents defined and none running")
        return EXIT_OK
    width = max(len(str(agent.get("name", ""))) for agent in agents)
    for agent in agents:
        name = str(agent.get("name", ""))
        kind = str(agent.get("kind", "definition"))
        status = str(agent.get("status") or "")
        tail = str(agent.get("description") or agent.get("task") or "")
        marker = f"[{kind}{'/' + status if status else ''}]"
        print(f"{name:<{width}}  {marker:<22} {tail}".rstrip())
    return EXIT_OK


async def team_status(
    team_id: str | None = None, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea team status [teamId] [--json]`` → ``team.status`` (AC-16).

    Without a team id the daemon answers for the most recent run.  Each row
    carries the task's state, how many merge conflicts re-queued it and, when
    it ended in ``conflict`` or ``failed``, a one-line summary of the hunks
    that are still attached to it.
    """
    try:
        result = await _call(home, "team.status", {"teamId": team_id or ""})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"team.status failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    tasks = [row for row in (result.get("tasks") or []) if isinstance(row, dict)]
    print(
        f"team {result.get('teamId', '')} [{result.get('state', '')}] "
        f"{len(tasks)} tasks, {result.get('workers', 0)} workers"
    )
    if result.get("task"):
        print(f"  task: {result['task']}")
    for path in result.get("worktrees") or []:
        print(f"  worktree: {path}")
    for row in tasks:
        line = f"  {row.get('taskId', '')}  {str(row.get('status', '')):<9}"
        if row.get("agentN") is not None:
            line += f" agent {row['agentN']}"
        if row.get("retries"):
            line += f" retries {row['retries']}"
        line += f"  {row.get('title', '')}"
        print(line.rstrip())
        summary = row.get("conflictSummary") or ""
        if summary:
            print(f"      conflict: {summary}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# provider.* (M3 contract §1–§3)
# ---------------------------------------------------------------------------


async def provider_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea provider list [--json]`` → ``provider.list``."""
    try:
        providers = await _lookup(home, "provider.list", "providers")
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"provider.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(providers)
        return EXIT_OK
    width = max((len(str(item.get("vendor", ""))) for item in providers), default=8)
    for item in providers:
        vendor = str(item.get("vendor", ""))
        mark = "*" if item.get("default") else " "
        state = "configured" if item.get("configured") else "-"
        logins = ",".join(str(m) for m in item.get("authMethods") or [])
        print(
            f"{mark} {vendor:<{width}}  {state:<10} {logins:<22} "
            f"{item.get('defaultModel', '')}".rstrip()
        )
    return EXIT_OK


async def provider_models(
    vendor: str | None = None, home: Path | str | None = None, *, as_json: bool = False
) -> int:
    """``snowpea provider models [vendor] [--json]`` → ``provider.models``."""
    params: dict[str, Any] = {"vendor": vendor} if vendor else {}
    try:
        result = await _call(home, "provider.models", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"provider.models failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    listed = [str(item) for item in (result.get("models") or [])]
    name = str(result.get("vendor") or vendor or "")
    current = str(result.get("current") or "")
    if not listed:
        print(f"{name}: no models reported; set one with `snowpea setup provider`")
        return EXIT_OK
    for model in listed:
        print(f"{'*' if model == current else ' '} {model}")
    return EXIT_OK


async def provider_login(vendor: str, home: Path | str | None = None) -> int:
    """``snowpea provider login <vendor>`` → ``provider.loginWeb``.

    Only OpenAI (device code) and OpenRouter (OAuth PKCE) have a browser login;
    every other vendor answers ``login_unsupported`` with the API-key command.
    """
    if not vendor:
        return _fail("usage: snowpea provider login <vendor>", EXIT_USAGE)
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
        try:
            await client.call(
                "provider.loginWeb", {"vendor": vendor, "method": "web"}, timeout=900.0
            )
        finally:
            await client.close()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"{vendor} login failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"{vendor}: signed in; credentials saved to settings.json")
    return EXIT_OK


# ---------------------------------------------------------------------------
# setup (M3 contract §5)
# ---------------------------------------------------------------------------


def setup_command(args: argparse.Namespace, home: Path | str | None = None) -> int:
    """``snowpea setup [--quick|--full|--blank] [flags]`` (M3 contract §5).

    ``--login <vendor>`` is an alias for ``snowpea provider login <vendor>``
    and never runs the wizard.
    """
    from snowpea_core.setup import wizard

    login_vendor = getattr(args, "login", None)
    if login_vendor:
        return wizard.login(str(login_vendor), home)

    mode = "quick"
    if getattr(args, "full", False):
        mode = "full"
    elif getattr(args, "blank", False):
        mode = "blank"

    try:
        result = wizard.run(
            mode,  # type: ignore[arg-type]
            home=home,
            vendor=getattr(args, "vendor", None),
            key=getattr(args, "key", None),
            model=getattr(args, "model", None),
            base_url=getattr(args, "base_url", None),
            search_provider=getattr(args, "search_provider", None),
            browser_provider=getattr(args, "browser_provider", None),
            tools=getattr(args, "tools", None),
            gateway=getattr(args, "gateway", None),
            token=getattr(args, "token", None),
            user_id=getattr(args, "user_id", None),
            section=getattr(args, "section", None),
        )
    except wizard.SetupError as exc:
        return _fail(str(exc), EXIT_USAGE)

    if getattr(result, "cancelled", False):
        print("setup cancelled — nothing written")
        return 0
    print(f"settings written to {result.settings_path}")
    for line in result.summary():
        print(f"  {line}")
    _messenger_next_steps(result, home)
    return EXIT_OK


def _messenger_next_steps(result: Any, home: Path | str | None) -> None:
    """Say how an enabled messenger goes live, and start it if a daemon is up.

    The bindings live in the daemon, so the wizard's settings write is enough
    on its own only for the *next* start.  When one is already running we ask
    it to reconcile now (``gateway.sync``), so the user can talk to their bot
    without restarting anything.
    """
    enabled = result.state.enabled_gateways()
    if not enabled:
        return
    names = ", ".join(enabled)
    print()
    print(f"messenger {names} enabled — it starts with the daemon:")
    print("  run `snowpea` (or `snowpea daemon start`); send /start to your bot")
    if read_daemon_json(home) is None:
        return
    try:
        changed = _run_blocking(_gateway_sync(home))
    except (DaemonError, RpcCallError, OSError) as exc:
        print(f"  (the running daemon did not pick it up: {exc})")
        return
    live = sorted({*changed.get("added", []), *changed.get("kept", [])})
    for platform in live:
        print(f"  {platform}: listening")


def _run_blocking(coro: Any) -> Any:
    """``asyncio.run`` that also works inside ``dispatch``'s running loop.

    ``setup_command`` is synchronous (it prompts on stdin) but is awaited from
    the async ``dispatch``, so ``asyncio.run`` would raise.  A one-shot worker
    thread gets its own loop; the caller is already blocking either way.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _gateway_sync(home: Path | str | None) -> dict[str, Any]:
    """Ask the running daemon to reconcile its messenger bindings."""
    info = await ensure_daemon(home)
    client = DaemonClient(info)
    await client.connect()
    try:
        result = await client.call("gateway.sync", {})
    finally:
        await client.close()
    return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# daemon.*
# ---------------------------------------------------------------------------


async def daemon_status(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea daemon status`` — ``system.info`` plus lifecycle counters."""
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
        bindings: list[dict[str, Any]] = []
        try:
            result = await client.call("system.info", {})
            # Additive and optional: an older daemon has no gateway.list.
            with contextlib.suppress(RpcCallError):
                listed = await client.call("gateway.list", {})
                bindings = list((listed or {}).get("bindings") or [])
        finally:
            await client.close()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"system.info failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
        return EXIT_OK
    counters = result.get("counters") or {}
    lifecycle = result.get("lifecycle") or {}
    print(f"port         {result.get('port', info.port)}")
    print(f"pid          {result.get('pid', info.pid)}")
    print(f"startedAt    {result.get('startedAt', info.startedAt)}")
    print(f"version      {result.get('version', '')}")
    print(f"home         {result.get('home', '')}")
    for name in ("sessions", "jobs", "gateway_bindings", "named_agents"):
        print(f"{name:<12} {counters.get(name, 0)}")
    print(f"{'messengers':<12} {_messenger_line(bindings)}")
    if lifecycle:
        remaining = lifecycle.get("secondsUntilExit")
        remaining_text = "-" if remaining is None else f"{float(remaining):.0f}s"
        print(
            f"lifecycle    willExit={lifecycle.get('willExit', False)} "
            f"reason={lifecycle.get('reason', '?')} secondsUntilExit={remaining_text}"
        )
        # Plan §2.6: say in one line whether the daemon is going to exit, and
        # if not, what is keeping it up.
        print(lifecycle.get("summary") or _keepalive_summary(counters, remaining))
    return EXIT_OK


def _messenger_line(bindings: list[dict[str, Any]]) -> str:
    """``telegram (listening), slack (stopped)`` — one word per platform."""
    if not bindings:
        return "(none)"
    seen: dict[str, str] = {}
    for binding in bindings:
        platform = str(binding.get("platform") or "?")
        state = "listening" if binding.get("state") == "active" else "stopped"
        # Listening anywhere beats a stale row for the same platform.
        if seen.get(platform) != "listening":
            seen[platform] = state
    return ", ".join(f"{platform} ({seen[platform]})" for platform in sorted(seen))


#: Singular/plural wording mirrored from ``server/lifecycle.COUNTER_LABELS``,
#: used only when talking to a daemon too old to send ``lifecycle.summary``.
COUNTER_LABELS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "session", "sessions"),
    ("gateway_bindings", "gateway binding", "gateway bindings"),
    ("jobs", "job", "jobs"),
    ("named_agents", "named agent", "named agents"),
)


def _keepalive_summary(counters: dict[str, Any], remaining: Any) -> str:
    """``will exit in 120s`` / ``will not exit: 2 gateway bindings, 1 job``."""
    if remaining is not None:
        return f"will exit in {float(remaining):.0f}s"
    reasons = [
        f"{counters.get(name, 0)} {singular if counters.get(name, 0) == 1 else plural}"
        for name, singular, plural in COUNTER_LABELS
        if counters.get(name, 0) > 0
    ]
    return "will not exit: " + (", ".join(reasons) if reasons else "idle shutdown is off")


async def daemon_start(home: Path | str | None = None) -> int:
    """``snowpea daemon start`` — reuse or spawn, then report where it listens."""
    try:
        info = await ensure_daemon(home)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    print(f"snowpea daemon listening on 127.0.0.1:{info.port} (pid {info.pid})")
    return EXIT_OK


async def daemon_stop(home: Path | str | None = None) -> int:
    """``snowpea daemon stop`` — ``system.shutdown``, then wait for the process."""
    resolved = resolve_home(home)
    info = read_daemon_json(resolved)
    if info is None:
        print("no daemon is running")
        return EXIT_OK
    client = DaemonClient(info)
    try:
        await client.connect()
        try:
            await client.call("system.shutdown", {}, timeout=10.0)
        finally:
            await client.close()
    except DaemonError:
        # Already gone, or unreachable: fall through to the wait below.
        with contextlib.suppress(Exception):
            await client.close()
    except RpcCallError as exc:
        await client.close()
        return _fail(f"system.shutdown failed ({exc.code}): {exc.message}", EXIT_USAGE)

    daemon_json = Paths(home=resolved).daemon_json
    deadline = time.monotonic() + STOP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not pid_alive(info.pid) and not daemon_json.exists():
            break
        await asyncio.sleep(0.1)
    else:
        return _fail(f"the daemon (pid {info.pid}) did not stop in {STOP_TIMEOUT_SEC:.0f}s", 1)
    with contextlib.suppress(OSError):
        daemon_json.unlink()
    print(f"snowpea daemon stopped (pid {info.pid})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# gateway.* (M5 contract §3)
# ---------------------------------------------------------------------------


def parse_target(raw: str) -> dict[str, Any]:
    """``agent:<name>`` | ``session:<id>`` | ``new:<workdir>`` | raw JSON."""
    text = (raw or "").strip()
    if text.startswith("{"):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("target JSON must be an object")
        return value
    kind, _, rest = text.partition(":")
    if kind == "agent" and rest:
        return {"agent": rest}
    if kind == "session" and rest:
        return {"session": rest}
    if kind == "new":
        return {"new_session": {"workdir": rest or str(Path.cwd())}}
    raise ValueError("target must be agent:<name>, session:<id>, new:<workdir> or JSON")


async def gateway_bind(
    platform: str,
    credentials_ref: str,
    target: str,
    home: Path | str | None = None,
    *,
    channel_id: str | None = None,
    user_id: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea gateway bind <platform> <credentialsRef> <target>``."""
    try:
        parsed = parse_target(target)
    except (ValueError, json.JSONDecodeError) as exc:
        return _fail(str(exc), EXIT_USAGE)
    params: dict[str, Any] = {
        "platform": platform,
        "credentialsRef": credentials_ref,
        "target": parsed,
    }
    if channel_id:
        params["channelId"] = channel_id
    if user_id:
        params["userId"] = user_id
    try:
        result = await _call(home, "gateway.bind", params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"gateway.bind failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
    else:
        print(f"bound {platform} -> {target} as {result.get('bindingId')}")
    return EXIT_OK


async def gateway_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea gateway list [--json]``."""
    try:
        result = await _call(home, "gateway.list", {})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"gateway.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    bindings = [item for item in (result.get("bindings") or []) if isinstance(item, dict)]
    if as_json:
        _print_json(bindings)
        return EXIT_OK
    if not bindings:
        print("no gateway bindings")
        return EXIT_OK
    for binding in bindings:
        channel = binding.get("channelId") or "*"
        print(
            f"{binding.get('bindingId')}  {binding.get('platform')}  "
            f"{binding.get('target')}  channel={channel}  [{binding.get('state')}]"
        )
    return EXIT_OK


async def gateway_unbind(binding_id: str, home: Path | str | None = None) -> int:
    """``snowpea gateway unbind <bindingId>``."""
    try:
        await _call(home, "gateway.unbind", {"bindingId": binding_id})
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"gateway.unbind failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"unbound {binding_id}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# job.* (M5 contract §2)
# ---------------------------------------------------------------------------


def format_job_line(job: dict[str, Any]) -> str:
    """One line per job for ``snowpea job list``."""
    last = job.get("lastRunAt")
    last_text = f"{last}:{job.get('lastStatus') or '?'}" if last else "-"
    state = job.get("state", "scheduled")
    if not job.get("enabled", True) and state != "cancelled":
        state = f"{state},disabled"
    return (
        f"{job.get('jobId')}  {str(job.get('spec', '')):<20} "
        f"next={job.get('nextRunAt') or '-'}  last={last_text}  "
        f"mode={job.get('mode', '?')}  channel={job.get('channel') or '-'}  [{state}]"
    )


async def job_schedule(
    task: str,
    home: Path | str | None = None,
    *,
    spec: str | None = None,
    mode: str = "accept",
    channel: str | None = None,
    workdir: str | None = None,
    as_json: bool = False,
) -> int:
    """``snowpea job schedule --in 60s --task "…" [--channel telegram:<id>]``."""
    if not spec:
        return _fail("usage: snowpea job schedule --at <spec> --task <task>", EXIT_USAGE)
    if not task:
        return _fail("snowpea job schedule needs --task", EXIT_USAGE)
    params: dict[str, Any] = {"spec": spec, "task": task, "mode": mode}
    if channel:
        params["channel"] = channel
    params["workdir"] = workdir or str(Path.cwd())
    try:
        result = await _call(home, "job.schedule", params, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.schedule failed ({exc.code}): {exc.message}", EXIT_USAGE)
    if as_json:
        _print_json(result)
    else:
        print(f"scheduled {result.get('jobId')} next at {result.get('nextRunAt') or '-'}")
    return EXIT_OK


async def job_list(home: Path | str | None = None, *, as_json: bool = False) -> int:
    """``snowpea job list [--json]`` → ``job.list``."""
    try:
        result = await _call(home, "job.list", {}, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.list failed ({exc.code}): {exc.message}", EXIT_USAGE)
    jobs = [item for item in (result.get("jobs") or []) if isinstance(item, dict)]
    if as_json:
        _print_json(jobs)
        return EXIT_OK
    if not jobs:
        print("no jobs are scheduled")
        return EXIT_OK
    for job in jobs:
        print(format_job_line(job))
    return EXIT_OK


async def job_cancel(job_id: str, home: Path | str | None = None) -> int:
    """``snowpea job cancel <jobId>``."""
    if not job_id:
        return _fail("usage: snowpea job cancel <jobId>", EXIT_USAGE)
    try:
        await _call(home, "job.cancel", {"jobId": job_id}, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.cancel failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"cancelled {job_id}")
    return EXIT_OK


async def job_run(job_id: str, home: Path | str | None = None) -> int:
    """``snowpea job run <jobId>`` → ``job.runNow``, which runs in the daemon."""
    if not job_id:
        return _fail("usage: snowpea job run <jobId>", EXIT_USAGE)
    try:
        await _call(home, "job.runNow", {"jobId": job_id}, timeout=JOB_RUN_TIMEOUT_SEC)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"job.runNow failed ({exc.code}): {exc.message}", EXIT_USAGE)
    print(f"ran {job_id}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# skill.* (M6 contract §1)
# ---------------------------------------------------------------------------


def resolve_install_source(source: str) -> str:
    """Make a local install source absolute before it crosses the wire.

    The daemon runs in ``$SNOWPEA_HOME``, so a relative path typed in a project
    checkout — ``./tests/fixtures/plugins/sample-plugin`` (AC-10) — would be
    resolved against the wrong directory.  Anything that is not an existing
    local directory (a git URL, ``<marketplace>/<plugin>``, a shortcut) is
    passed through untouched.
    """
    text = source.strip()
    if not text or text.startswith(("git@", "ssh://", "git://", "http://", "https://")):
        return text
    candidate = Path(text).expanduser()
    if candidate.is_dir():
        return str(candidate.resolve())
    return text


async def skill_command(
    action: str,
    argument: str = "",
    home: Path | str | None = None,
    *,
    as_json: bool = False,
) -> int:
    """``snowpea skill list|search <query>|install <source>|remove <name>``."""
    if action == "install":
        argument = resolve_install_source(argument)
    methods = {
        "list": ("skill.list", {}),
        "search": ("skill.search", {"query": argument}),
        "install": ("skill.install", {"source": argument}),
        "remove": ("skill.remove", {"name": argument}),
    }
    if action not in methods:
        return _fail(
            "usage: snowpea skill list|search <query>|install <source>|remove <name>",
            EXIT_USAGE,
        )
    if action in ("search", "install", "remove") and not argument:
        return _fail(f"snowpea skill {action} needs an argument", EXIT_USAGE)
    method, params = methods[action]
    try:
        result = await _call(home, method, params)
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)
    except RpcCallError as exc:
        return _fail(f"{method} failed ({exc.code}): {exc.message}", EXIT_USAGE)

    if action in ("install", "remove"):
        if as_json:
            _print_json(result)
        else:
            print(f"{action}ed {argument}" if action == "remove" else f"installed {argument}")
        return EXIT_OK

    skills = [item for item in (result.get("skills") or []) if isinstance(item, dict)]
    unavailable = [str(item) for item in (result.get("unavailable") or [])]
    if as_json:
        _print_json({"skills": skills, "unavailable": unavailable} if unavailable else skills)
        return EXIT_OK
    for line in unavailable:
        print(f"snowpea: source unreachable — {line}", file=sys.stderr)
    if not skills:
        if unavailable:
            print(f"no skills found ({len(unavailable)} of the searched sources were unreachable)")
        else:
            print("no skills found")
        return EXIT_OK
    width = max(len(str(skill.get("name", ""))) for skill in skills)
    for skill in skills:
        name = str(skill.get("name", ""))
        kind = str(skill.get("kind", "skill"))
        source = str(skill.get("source", ""))
        summary = str(skill.get("summary", ""))
        print(f"{name:<{width}}  {kind:<8} {source:<22} {summary}".rstrip())
    return EXIT_OK


# ---------------------------------------------------------------------------
# system.checkUpdate / system.update (CORE-update)
# ---------------------------------------------------------------------------

#: An upgrade downloads and builds a package, so it gets a long deadline.
UPDATE_TIMEOUT_SEC = 900.0


def format_update_line(answer: dict[str, Any]) -> str:
    """One human line for a ``system.checkUpdate`` answer."""
    current = str(answer.get("current", ""))
    latest = str(answer.get("latest", ""))
    if answer.get("error"):
        return f"snowpea v{current} — could not check for updates: {answer['error']}"
    if not answer.get("available"):
        return f"snowpea v{current} is up to date."
    channel = str(answer.get("channel", ""))
    return f"snowpea v{current} — update available: v{latest} (from {channel})"


async def update_cli(
    home: Path | str | None = None, *, check_only: bool = False, as_json: bool = False
) -> int:
    """``snowpea update [--check]`` — report, and unless ``--check``, install."""
    try:
        info = await ensure_daemon(home)
        client = DaemonClient(info)
        await client.connect()
    except DaemonError as exc:
        return _fail(str(exc), EXIT_NO_DAEMON)

    try:
        try:
            answer = await client.call("system.checkUpdate", {"force": True})
        except RpcCallError as exc:
            return _fail(f"system.checkUpdate failed ({exc.code}): {exc.message}", EXIT_USAGE)

        if as_json and check_only:
            _print_json(answer)
        else:
            print(format_update_line(answer))
        if check_only or not answer.get("available"):
            return EXIT_OK

        try:
            started = await client.call("system.update", {}, timeout=UPDATE_TIMEOUT_SEC)
        except RpcCallError as exc:
            return _fail(f"system.update failed ({exc.code}): {exc.message}", EXIT_USAGE)
        if as_json:
            _print_json(started)
        if not started.get("started"):
            return _fail(str(started.get("error") or "the update did not start"), EXIT_USAGE)
        print(f"running {started.get('command')}")
        print(f"log: {started.get('log')}")

        phase, message = await _await_update(client)
        if phase != "done":
            return _fail(message or "the update failed", EXIT_AGENT_FAILED)
        print(message or "update finished")
    finally:
        await client.close()

    await daemon_stop(home)
    print("restart snowpea to run the new version")
    return EXIT_OK


async def _await_update(client: DaemonClient) -> tuple[str, str]:
    """Block until ``system.updateProgress`` reports ``done`` or ``failed``."""
    try:
        async for frame in client.notifications():
            if frame.get("method") != "system.updateProgress":
                continue
            params = frame.get("params") or {}
            phase = str(params.get("phase", ""))
            if phase in ("done", "failed"):
                return phase, str(params.get("message", ""))
    except DaemonError as exc:  # pragma: no cover - socket died mid-upgrade
        return "failed", str(exc)
    return "failed", "the daemon closed the connection before the update finished"


# ---------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------


def placeholder(name: str) -> int:
    """Print the M2+ notice and return the usage exit code."""
    print(f"snowpea {name}: not yet implemented", file=sys.stderr)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def add_subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any]:
    """Attach every ``snowpea <subcommand>`` to ``parser``."""
    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")

    tools = sub.add_parser("tools", help="inspect the tool registry")
    tools_sub = tools.add_subparsers(dest="action", metavar="<action>")
    tools_list_parser = tools_sub.add_parser("list", help="list registered tools")
    tools_list_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    commands = sub.add_parser("commands", help="inspect the slash-command registry")
    commands_sub = commands.add_subparsers(dest="action", metavar="<action>")
    commands_list_parser = commands_sub.add_parser("list", help="list registered commands")
    commands_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    provider = sub.add_parser("provider", help="inspect and log into chat providers")
    provider_sub = provider.add_subparsers(dest="action", metavar="<action>")
    provider_list_parser = provider_sub.add_parser("list", help="list known vendors")
    provider_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_models_parser = provider_sub.add_parser(
        "models", help="ask a vendor which models it serves"
    )
    provider_models_parser.add_argument(
        "vendor", nargs="?", default=None, help="vendor to query (default: the configured one)"
    )
    provider_models_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    provider_login_parser = provider_sub.add_parser(
        "login", help="browser login (openai, openrouter)"
    )
    provider_login_parser.add_argument("vendor", help="vendor to log into")

    setup_parser = sub.add_parser("setup", help="configure providers, search, tools")
    setup_parser.add_argument(
        "section",
        nargs="?",
        default=None,
        choices=["provider", "providers", "search", "browser", "tools", "gateway"],
        help="configure one section only, e.g. `snowpea setup search` (Hermes-style)",
    )
    setup_mode = setup_parser.add_mutually_exclusive_group()
    setup_mode.add_argument(
        "--quick", action="store_true", help="ask for the LLM provider only (default)"
    )
    setup_mode.add_argument(
        "--full", action="store_true", help="walk every screen (providers → … → done)"
    )
    setup_mode.add_argument("--blank", action="store_true", help="ask nothing, write the defaults")
    setup_parser.add_argument("--vendor", default=None, help="LLM vendor id, e.g. anthropic")
    setup_parser.add_argument("--key", default=None, help="API key for --vendor")
    setup_parser.add_argument("--model", default=None, help="default model for --vendor")
    setup_parser.add_argument("--base-url", dest="base_url", default=None, help="API base URL")
    setup_parser.add_argument(
        "--search-provider", dest="search_provider", default=None, help="web-search provider id"
    )
    setup_parser.add_argument(
        "--browser-provider", dest="browser_provider", default=None, help="browser provider id"
    )
    setup_parser.add_argument(
        "--tools", default=None, help="tool categories: 'vision,-git' enables and disables"
    )
    setup_parser.add_argument("--gateway", default=None, help="gateway to enable")
    setup_parser.add_argument("--token", default=None, help="bot token for --gateway")
    setup_parser.add_argument(
        "--user-id",
        dest="user_id",
        default=None,
        help="your user id on --gateway; only this account may approve from chat",
    )
    setup_parser.add_argument(
        "--login", default=None, metavar="VENDOR", help="browser login (alias of provider login)"
    )

    skill = sub.add_parser("skill", help="find, install and list skills and plugins")
    skill_sub = skill.add_subparsers(dest="action", metavar="<action>")
    for name, help_text, argument in (
        ("list", "list installed skills, agents, commands and plugins", None),
        ("search", "search the skill marketplaces", "query"),
        ("install", "install a path, git URL, <marketplace>/<plugin> or shortcut", "source"),
        ("remove", "delete an installed plugin", "name"),
    ):
        skill_action = skill_sub.add_parser(name, help=help_text)
        if argument:
            skill_action.add_argument(argument)
        skill_action.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    daemon = sub.add_parser("daemon", help="control the core daemon")
    daemon_sub = daemon.add_subparsers(dest="action", metavar="<action>")
    status_parser = daemon_sub.add_parser("status", help="show the running daemon")
    status_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    daemon_sub.add_parser("start", help="start the daemon if it is not running")
    daemon_sub.add_parser("stop", help="ask the daemon to shut down")

    gateway = sub.add_parser("gateway", help="attach chat platforms to agents or sessions")
    gateway_sub = gateway.add_subparsers(dest="action", metavar="<action>")
    bind_parser = gateway_sub.add_parser("bind", help="attach a platform account")
    bind_parser.add_argument("platform", help="telegram, discord or slack")
    bind_parser.add_argument("credentials_ref", help="credentials.json key or env var name")
    bind_parser.add_argument(
        "target", help="agent:<name>, session:<id>, new:<workdir> or a JSON object"
    )
    bind_parser.add_argument("--channel", dest="channel_id", help="restrict to one chat id")
    bind_parser.add_argument("--user", dest="user_id", help="platform user who may approve")
    bind_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    gateway_list_parser = gateway_sub.add_parser("list", help="list live bindings")
    gateway_list_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    unbind_parser = gateway_sub.add_parser("unbind", help="detach a binding")
    unbind_parser.add_argument("binding_id", help="binding id from `gateway list`")

    job = sub.add_parser("job", help="schedule prompts to run unattended")
    job_sub = job.add_subparsers(dest="action", metavar="<action>")
    job_schedule_parser = job_sub.add_parser("schedule", help="register a job")
    job_schedule_parser.add_argument(
        "--at",
        "--in",
        "--every",
        "--cron",
        "--spec",
        dest="spec",
        help='when to run: "0 9 * * *", "60s", "10m", "매일 09:00"',
    )
    job_schedule_parser.add_argument("--task", default="", help="prompt to run on each firing")
    job_schedule_parser.add_argument(
        "--mode", default="accept", choices=["plan", "accept", "auto"], help="mode for the run"
    )
    job_schedule_parser.add_argument("--channel", default=None, help="e.g. telegram:12345 or log")
    job_schedule_parser.add_argument("--workdir", default=None, help="directory the job runs in")
    job_schedule_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )
    job_list_parser = job_sub.add_parser("list", help="list scheduled jobs")
    job_list_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")
    job_cancel_parser = job_sub.add_parser("cancel", help="cancel a job")
    job_cancel_parser.add_argument("job_id", help="job id from `job list`")
    job_run_parser = job_sub.add_parser("run", help="fire a job now, in the daemon")
    job_run_parser.add_argument("job_id", help="job id from `job list`")

    service = sub.add_parser("service", help="run the daemon as a login service")
    service_sub = service.add_subparsers(dest="action", metavar="<action>")
    service_sub.add_parser("install", help="register the daemon with systemd/launchd/schtasks")
    service_sub.add_parser("uninstall", help="deregister it again")
    service_sub.add_parser("status", help="is it registered, is it running")

    update_parser = sub.add_parser("update", help="check for a newer snowpea and install it")
    update_parser.add_argument(
        "--check", dest="check_only", action="store_true", help="report only, install nothing"
    )
    update_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    agents_parser = sub.add_parser("agents", help="list agent definitions and running subagents")
    agents_parser.add_argument("--json", dest="sub_json", action="store_true", help="emit JSON")

    team = sub.add_parser("team", help="inspect a parallel worktree team run")
    team_sub = team.add_subparsers(dest="action", metavar="<action>")
    team_status_parser = team_sub.add_parser("status", help="show a team's task board")
    team_status_parser.add_argument(
        "team_id", nargs="?", default=None, help="team id; defaults to the most recent run"
    )
    team_status_parser.add_argument(
        "--json", dest="sub_json", action="store_true", help="emit JSON"
    )

    for name in PLACEHOLDER_SUBCOMMANDS:
        placeholder_parser = sub.add_parser(name, help=f"{name} (not yet implemented)")
        placeholder_parser.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return sub


def _job_spec(args: argparse.Namespace) -> str | None:
    """The spec as typed. ``--in 60s`` and ``--every 10m`` keep their own wording."""
    raw = getattr(args, "spec", None)
    if not raw:
        return None
    text = str(raw).strip()
    # argparse folds --in/--every/--cron/--at into one dest, so a bare duration
    # like "60s" is read as "in 60s" and "10m" after --every stays an interval
    # only if the user spelled it that way.  Bare durations mean "from now".
    if re.fullmatch(r"\d+\s*[a-z가-힣]+", text) and not text.startswith(("in ", "every ")):
        return f"in {text}"
    return text


async def dispatch(args: argparse.Namespace, home: Path | str | None = None) -> int:
    """Run the parsed subcommand; returns its exit code."""
    subcommand = args.subcommand
    as_json = bool(getattr(args, "sub_json", False) or getattr(args, "json", False))
    action = getattr(args, "action", None)

    if subcommand in PLACEHOLDER_SUBCOMMANDS:
        return placeholder(subcommand)
    if subcommand == "setup":
        return setup_command(args, home)
    if subcommand == "service":
        return service_command(str(action or ""), home)
    if subcommand == "tools":
        if action != "list":
            return _fail("usage: snowpea tools list [--json]", EXIT_USAGE)
        return await tools_list(home, as_json=as_json)
    if subcommand == "commands":
        if action != "list":
            return _fail("usage: snowpea commands list [--json]", EXIT_USAGE)
        return await commands_list(home, as_json=as_json)
    if subcommand == "agents":
        return await agents_list(home, as_json=as_json)
    if subcommand == "update":
        return await update_cli(
            home, check_only=bool(getattr(args, "check_only", False)), as_json=as_json
        )
    if subcommand == "team":
        if action != "status":
            return _fail("usage: snowpea team status [teamId] [--json]", EXIT_USAGE)
        return await team_status(getattr(args, "team_id", None), home, as_json=as_json)
    if subcommand == "provider":
        if action == "list":
            return await provider_list(home, as_json=as_json)
        if action == "models":
            return await provider_models(
                getattr(args, "vendor", None) or None, home, as_json=as_json
            )
        if action == "login":
            return await provider_login(str(getattr(args, "vendor", "") or ""), home)
        return _fail("usage: snowpea provider list|models|login <vendor>", EXIT_USAGE)
    if subcommand == "gateway":
        if action == "bind":
            return await gateway_bind(
                str(getattr(args, "platform", "") or ""),
                str(getattr(args, "credentials_ref", "") or ""),
                str(getattr(args, "target", "") or ""),
                home,
                channel_id=getattr(args, "channel_id", None),
                user_id=getattr(args, "user_id", None),
                as_json=as_json,
            )
        if action == "list":
            return await gateway_list(home, as_json=as_json)
        if action == "unbind":
            return await gateway_unbind(str(getattr(args, "binding_id", "") or ""), home)
        return _fail(
            "usage: snowpea gateway bind <platform> <credentialsRef> <target>"
            " | list | unbind <bindingId>",
            EXIT_USAGE,
        )
    if subcommand == "job":
        if action == "schedule":
            return await job_schedule(
                str(getattr(args, "task", "") or ""),
                home,
                spec=_job_spec(args),
                mode=str(getattr(args, "mode", "accept") or "accept"),
                channel=getattr(args, "channel", None),
                workdir=getattr(args, "workdir", None),
                as_json=as_json,
            )
        if action == "list":
            return await job_list(home, as_json=as_json)
        if action == "cancel":
            return await job_cancel(str(getattr(args, "job_id", "") or ""), home)
        if action == "run":
            return await job_run(str(getattr(args, "job_id", "") or ""), home)
        return _fail(
            'usage: snowpea job schedule --in 60s --task "…" | list | cancel <id> | run <id>',
            EXIT_USAGE,
        )
    if subcommand == "skill":
        argument = (
            getattr(args, "query", None)
            or getattr(args, "source", None)
            or getattr(args, "name", None)
            or ""
        )
        return await skill_command(str(action or ""), str(argument), home, as_json=as_json)
    if subcommand == "daemon":
        if action == "status":
            return await daemon_status(home, as_json=as_json)
        if action == "start":
            return await daemon_start(home)
        if action == "stop":
            return await daemon_stop(home)
        return _fail("usage: snowpea daemon status|start|stop", EXIT_USAGE)
    return _fail(f"unknown subcommand: {subcommand}", EXIT_USAGE)


__all__ = [
    "COUNTER_LABELS",
    "JOB_RUN_TIMEOUT_SEC",
    "PLACEHOLDER_SUBCOMMANDS",
    "STOP_TIMEOUT_SEC",
    "UPDATE_TIMEOUT_SEC",
    "add_subparsers",
    "agents_list",
    "commands_list",
    "daemon_start",
    "daemon_status",
    "daemon_stop",
    "dispatch",
    "gateway_bind",
    "gateway_list",
    "gateway_unbind",
    "format_job_line",
    "format_update_line",
    "job_cancel",
    "job_list",
    "job_run",
    "job_schedule",
    "parse_target",
    "placeholder",
    "provider_list",
    "service_command",
    "provider_login",
    "provider_models",
    "resolve_install_source",
    "setup_command",
    "skill_command",
    "team_status",
    "tools_list",
    "update_cli",
]
