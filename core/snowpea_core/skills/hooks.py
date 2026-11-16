"""Plugin hooks (M6 contract §1).

A plugin ships ``hooks/hooks.json`` in the Claude Code shape::

    {"hooks": {"PreToolUse": [{"matcher": "shell|write_file",
                               "hooks": [{"type": "command", "command": "…"}]}]}}

``matcher`` is a regular expression matched against the tool name (``"*"`` and
the empty string mean "every tool").  The command runs through the shell with
the hook payload on stdin::

    {"event", "tool_name", "tool_input", "session_id", "cwd",
     "origin_surface", "host_tools_from", "agent", "parent_session_id"}

and ``SNOWPEA_HOME`` / ``SNOWPEA_TOOL_NAME`` in its environment, plus
``CLAUDE_PLUGIN_ROOT`` and ``SNOWPEA_PLUGIN_ROOT`` pointing at the plugin that
declared it.  For ``PreToolUse``, exit status 2 blocks the call and the hook's
stderr becomes the error the model sees.

``Stop`` runs when the model is about to end its turn, as in Claude Code: the
payload adds ``hook_event_name``, ``stop_hook_active`` (true when this turn
already continued because of a Stop hook) and ``last_assistant_message``. A
hook keeps the agent going with exit status 2 (stderr is the instruction) or
by printing ``{"decision": "block", "reason": "…"}``; the reason is handed
to the model and the turn continues. At most :data:`MAX_STOP_CONTINUATIONS`
times per turn, so a hook that always blocks cannot loop forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.util.atomic import read_owned

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.skills.hooks")

#: The three events this milestone runs; others are parsed and ignored.
EVENTS: tuple[str, ...] = ("PreToolUse", "PostToolUse", "Stop")

#: Exit status a ``PreToolUse`` hook uses to refuse the call, and a ``Stop``
#: hook uses to keep the agent going.
BLOCK_EXIT_CODE = 2

#: How many times Stop hooks may keep one turn going.
MAX_STOP_CONTINUATIONS = 8

#: What :func:`run_hook` reports for a hook that could not start or timed out.
HOOK_FAILED = -1

DEFAULT_TIMEOUT_SEC = 30.0

#: Error prefix the model (and the tests) see when a hook refuses a call.
BLOCKED_PREFIX = "hook_blocked"


@dataclass(frozen=True)
class Hook:
    """One command hook from one plugin."""

    event: str
    matcher: str
    command: str
    plugin: str = ""
    root: Path | None = None
    timeout: float = DEFAULT_TIMEOUT_SEC
    #: A ``PreToolUse`` hook with ``failClosed: true`` blocks the call when it
    #: cannot start, crashes or times out, instead of letting it through.
    fail_closed: bool = False
    #: The project (real path) a project's own hook belongs to: it runs only for
    #: sessions working in that project (security review 5, C1).  ``None``: a
    #: global or plugin hook, for every session.
    project: Path | None = None
    #: A project hook's file, relative to the project: trust is checked again
    #: before each run, with the scripts it runs (security review 6, M-3).
    rel: str = ""

    def applies_to(self, workdir: Any) -> bool:
        """This hook runs for a session in ``workdir``."""
        if self.project is None:
            return True
        if not workdir:
            return False
        try:
            real = Path(os.path.realpath(os.fspath(workdir)))
        except (OSError, TypeError, ValueError):
            return False
        return real == self.project or self.project in real.parents

    def matches(self, tool_name: str) -> bool:
        pattern = (self.matcher or "").strip()
        if pattern in ("", "*"):
            return True
        try:
            return re.search(pattern, tool_name) is not None
        except re.error:
            return pattern == tool_name


@dataclass
class HookOutcome:
    """What running the hooks for one event decided."""

    blocked: bool = False
    message: str = ""
    ran: int = 0


@dataclass
class HookRegistry:
    """Every hook the loader found, grouped by event."""

    hooks: dict[str, list[Hook]] = field(default_factory=dict)

    def clear(self) -> None:
        self.hooks = {}

    def add(self, hook: Hook) -> None:
        bucket = self.hooks.setdefault(hook.event, [])
        if hook not in bucket:  # a project scanned twice registers its hooks once
            bucket.append(hook)

    def for_tool(self, event: str, tool_name: str, workdir: Any = ...) -> list[Hook]:
        """Hooks of ``event`` that match ``tool_name``; with ``workdir``, only those
        that apply to a session there (a project's hooks stay in their project)."""
        return [hook for hook in self.hooks.get(event, []) if hook.matches(tool_name)
                and (workdir is ... or hook.applies_to(workdir))]

    def count(self) -> int:
        return sum(len(items) for items in self.hooks.values())

    def load_file(self, path: Path, *, plugin: str = "", root: Path | None = None,
                  project: Path | None = None, data: bytes | None = None,
                  rel: str = "") -> int:
        """Read one ``hooks.json``; returns how many hooks it contributed.

        ``project``: the hooks are that project's own (they run only there).
        ``data``: the file's bytes as project trust verified them — parsed
        instead of reading the file again (security review 6, M-2); ``rel``: the
        file's path in the project, kept on each hook for the check before a run.
        """
        try:
            text = (data if data is not None else read_owned(path)).decode("utf-8")
            raw = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.info("ignoring unreadable hooks file %s: %s", path, exc)
            return 0
        table = raw.get("hooks") if isinstance(raw, dict) else None
        if not isinstance(table, dict):
            return 0
        added = 0
        for event, entries in table.items():
            if event not in EVENTS or not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                matcher = str(entry.get("matcher") or "*")
                for spec in entry.get("hooks") or []:
                    if not isinstance(spec, dict):
                        continue
                    if str(spec.get("type") or "command") != "command":
                        continue
                    command = str(spec.get("command") or "").strip()
                    if not command:
                        continue
                    timeout = spec.get("timeout")
                    self.add(
                        Hook(
                            event=event,
                            matcher=matcher,
                            command=command,
                            plugin=plugin,
                            root=root,
                            timeout=float(timeout) if timeout else DEFAULT_TIMEOUT_SEC,
                            fail_closed=bool(spec.get("failClosed")),
                            project=project,
                            rel=rel,
                        )
                    )
                    added += 1
        return added


def python_command() -> str:
    """How a hook runs a ``.py`` file with the core's own interpreter.

    ``python "<exe>"`` from source; a frozen core runs it through its
    ``--run-hook`` entry, since it has no interpreter binary to hand out.
    """
    if sys.platform == "win32":
        exe = subprocess.list2cmdline([sys.executable])
    else:
        exe = shlex.quote(sys.executable)
    return f"{exe} --run-hook" if getattr(sys, "frozen", False) else exe


def expand(text: str, root: Path | None) -> str:
    """Substitute the placeholders a hook command may use.

    ``${CLAUDE_PLUGIN_ROOT}``/``${SNOWPEA_PLUGIN_ROOT}`` become the plugin's
    directory and ``${SNOWPEA_PYTHON}`` the core's interpreter
    (:func:`python_command`), so ``${SNOWPEA_PYTHON} hooks/x.py`` works
    without a ``python3`` on PATH.
    """
    python = python_command()
    text = text.replace("${SNOWPEA_PYTHON}", python).replace("$SNOWPEA_PYTHON", python)
    if root is None:
        return text
    value = str(root)
    for name in ("CLAUDE_PLUGIN_ROOT", "SNOWPEA_PLUGIN_ROOT"):
        text = text.replace(f"${{{name}}}", value).replace(f"${name}", value)
    return text


#: ``(program, substitute)`` pairs already logged, so each is logged once.
_SUBSTITUTED: set[tuple[str, str]] = set()

_FIRST_TOKEN = re.compile(r'\s*(?:"([^"]*)"|(\S+))')


def windows_python(
    command: str,
    *,
    platform: str | None = None,
    which: Any = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """``command`` with a bare ``python3``/``python`` program made runnable on Windows.

    Plugins are written on macOS and Linux and say ``python3 hooks/x.py``.  On
    Windows that name is usually missing or is the Microsoft Store "App
    Execution Alias" stub, which prints "Python was not found", exits 9009
    and opens a console of its own.  The rule (Windows only; anything else is
    returned unchanged), see :func:`winproc.python_substitute`: a first word
    that is exactly ``python3``/``python`` (``.exe`` allowed, any case, no
    path) and does not resolve on PATH to a real interpreter — missing, or
    only the Store alias — becomes the core's own interpreter, exactly what
    ``${SNOWPEA_PYTHON}`` expands to.  Logged once per program at INFO.
    """
    from snowpea_core.exec import winproc

    if (platform or sys.platform) != "win32":
        return command
    match = _FIRST_TOKEN.match(command)
    if match is None:
        return command
    program = match.group(1) or match.group(2) or ""
    if not winproc.python_substitute(program, which=which, environ=environ):
        return command
    replacement = python_command()
    key = (program, replacement)
    if key not in _SUBSTITUTED:
        _SUBSTITUTED.add(key)
        log.info("hook program %r is not a usable Python here; using %s", program, replacement)
    return replacement + command[match.end() :]


def _env(home: Path | str, tool_name: str, root: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env["SNOWPEA_HOME"] = str(home)
    env["SNOWPEA_TOOL_NAME"] = tool_name
    env["SNOWPEA_PYTHON"] = python_command()
    if root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(root)
        env["SNOWPEA_PLUGIN_ROOT"] = str(root)
    return env


async def run_hook(
    hook: Hook,
    payload: dict[str, Any],
    *,
    home: Path | str,
    cwd: Path | str | None = None,
) -> tuple[int, str]:
    """Run one hook; returns ``(exit_code, stderr)``.  Never raises."""
    code, _out, err = await run_hook_output(
        hook, payload, home=home, cwd=cwd, session_id=str(payload.get("session_id") or "")
    )
    return code, err


#: ``(session_id, hook command)`` whose bare ``python3`` failed to start or
#: import from PATH on Windows and worked with the core's interpreter: later
#: runs in that session go straight to the core's interpreter.
_CORE_PYTHON_FOR: set[tuple[str, str]] = set()

#: A line a Python that started but could not load the hook's packages prints.
_IMPORT_FAILURE = re.compile(r"^\s*(?:\w+\.)*(?:ImportError|ModuleNotFoundError)\b", re.MULTILINE)

#: cmd.exe's "is not recognized as an internal or external command".
_NOT_FOUND_EXIT = 9009


def _bare_python_on_path(command: str) -> re.Match[str] | None:
    """The first-word match when ``command`` starts with a bare ``python3``/``python``."""
    from snowpea_core.exec import winproc

    match = _FIRST_TOKEN.match(command)
    if match is None:
        return None
    program = match.group(1) or match.group(2) or ""
    return match if program.lower() in winproc.BARE_PYTHON_NAMES else None


def _start_or_import_failed(code: int, err: str) -> bool:
    """True when the PATH Python could not start the hook or load its imports."""
    if code == _NOT_FOUND_EXIT:
        return True
    return code not in (0, BLOCK_EXIT_CODE) and bool(_IMPORT_FAILURE.search(err or ""))


async def run_hook_output(
    hook: Hook,
    payload: dict[str, Any],
    *,
    home: Path | str,
    cwd: Path | str | None = None,
    session_id: str = "",
) -> tuple[int, str, str]:
    """Run one hook; returns ``(exit_code, stdout, stderr)``.  Never raises.

    The command runs through the shell (``/bin/sh -c`` or ``cmd.exe /c``) as
    hook authors expect.  On Windows it gets no console window, its own job,
    and a bare ``python3`` resolves to a usable interpreter
    (:func:`windows_python`); on a timeout the whole tree is ended, and once
    the hook has answered whatever it left behind ends too.

    The Windows interpreter rule prefers a real Python on PATH (the plugin's
    packages are usually installed there).  When that Python cannot start the
    hook (exit 9009) or stops on an ``ImportError``/``ModuleNotFoundError``,
    the hook runs once more with the core's interpreter; if that one does not
    fail the same way, the session keeps using it for this hook.
    """
    try:
        expanded = expand(hook.command, hook.root)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        session_id = session_id or str(payload.get("session_id") or "")
        key = (session_id, hook.command)
        bare = _bare_python_on_path(expanded) if sys.platform == "win32" else None
        if bare is not None and key in _CORE_PYTHON_FOR:
            return await _spawn(hook, python_command() + expanded[bare.end() :], body, payload,
                                home, cwd)
        command = windows_python(expanded)
        result = await _spawn(hook, command, body, payload, home, cwd)
        if bare is None or command != expanded or not _start_or_import_failed(result[0], result[2]):
            return result
        # The PATH Python is real but cannot run this hook: try the core's once.
        retry = await _spawn(hook, python_command() + expanded[bare.end() :], body, payload,
                             home, cwd)
        if _start_or_import_failed(retry[0], retry[2]) or retry[0] == HOOK_FAILED:
            return result
        _CORE_PYTHON_FOR.add(key)
        log.info(
            "hook %r failed with the PATH Python (%s); the core's interpreter works, "
            "using it for this session", hook.command, failure_reason(result[0], result[2]),
        )
        return retry
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken hook must never break the turn
        log.warning("hook %s failed to run: %s", hook.command, exc, exc_info=True)
        return HOOK_FAILED, "", f"could not run: {type(exc).__name__}: {exc}"


async def _spawn(
    hook: Hook,
    command: str,
    body: bytes,
    payload: dict[str, Any],
    home: Path | str,
    cwd: Path | str | None,
) -> tuple[int, str, str]:
    """Run ``command`` once for ``hook``; ``(exit_code, stdout, stderr)``.  Never raises."""
    from snowpea_core.exec import winproc

    try:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_env(home, str(payload.get("tool_name") or ""), hook.root),
                cwd=str(cwd) if cwd else None,
                **winproc.spawn_kwargs(),
            )
        except (OSError, ValueError) as exc:
            log.info("hook %s could not start: %s", hook.command, exc)
            return HOOK_FAILED, "", f"could not start: {exc}"
        winproc.attach(process)
        try:
            out, err = await asyncio.wait_for(process.communicate(body), hook.timeout)
        except TimeoutError:
            # The process may have exited a moment ago: kill() then raises
            # ProcessLookupError, which used to end the whole turn.
            await winproc.kill(process)
            log.info("hook %s timed out after %.0fs", hook.command, hook.timeout)
            return HOOK_FAILED, "", f"timed out after {hook.timeout:g}s"
        except asyncio.CancelledError:
            winproc.terminate_now(process)
            raise
        finally:
            winproc.release(process)
        return (
            int(process.returncode or 0),
            winproc.decode_output(out or b"").strip(),
            winproc.decode_output(err or b"").strip(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken hook must never break the turn
        log.warning("hook %s failed to run: %s", hook.command, exc, exc_info=True)
        return HOOK_FAILED, "", f"could not run: {type(exc).__name__}: {exc}"


#: Error code of the event a surface shows when a plugin hook is not running.
#: Stable; clients may localise it.  The message names the plugin and why.
HOOK_FAILED_CODE = "hook_failed"


@dataclass
class HookHealth:
    """A hook that failed the last time it ran (``/skill hooks`` lists these)."""

    plugin: str
    event: str
    command: str
    reason: str
    exit_code: int
    failures: int = 0
    last_at: float = 0.0


#: Failing hooks by ``(plugin, event, command)``; a successful run removes one.
HEALTH: dict[tuple[str, str, str], HookHealth] = {}

#: ``(session_id, plugin, event, command)`` already warned about and surfaced.
_WARNED: set[tuple[str, str, str, str]] = set()


def _key(hook: Hook) -> tuple[str, str, str]:
    return (hook.plugin, hook.event, hook.command)


def failure_reason(code: int, err: str) -> str:
    """One line on why a hook failed, from its exit status and first stderr line."""
    first = next((line.strip() for line in err.splitlines() if line.strip()), "")
    if code == HOOK_FAILED:
        return first or "could not run"
    if code in (9009, 127):
        # cmd.exe's and sh's "command not found".
        detail = f": {first}" if first else ""
        return f"command not found (exit {code}){detail}"
    return f"exit {code}: {first}" if first else f"exit {code}"


def note_ok(hook: Hook) -> None:
    HEALTH.pop(_key(hook), None)


async def note_failure(core: Any, session: Any, hook: Hook, code: int, err: str) -> None:
    """Record a failed hook; warn and tell the session's surfaces once per session.

    The call itself still proceeds (unless the hook is ``failClosed``) — this
    only makes the failure visible instead of a log line per call.
    """
    reason = failure_reason(code, err)
    health = HEALTH.get(_key(hook))
    if health is None:
        health = HEALTH[_key(hook)] = HookHealth(
            plugin=hook.plugin, event=hook.event, command=hook.command, reason=reason,
            exit_code=code,
        )
    health.reason, health.exit_code = reason, code
    health.failures += 1
    health.last_at = time.time()
    session_id = str(getattr(session, "id", ""))
    marker = (session_id, *_key(hook))
    if marker in _WARNED:
        log.debug("hook %s failed again: %s", hook.command, reason)
        return
    _WARNED.add(marker)
    name = hook.plugin or "plugin"
    log.warning(
        "%s hook of %s is not running (session %s): %s [%s]",
        hook.event, name, session_id, reason, hook.command,
    )
    hub = getattr(core, "hub", None)
    if hub is None or not session_id:
        return
    kind = "guard hook" if hook.event == "PreToolUse" else f"{hook.event} hook"
    message = f"a plugin {kind} is not running: {name} — {reason}"
    if hook.fail_closed and hook.event == "PreToolUse":
        message += " (it is fail-closed: the tools it guards are blocked)"
    try:
        from snowpea_core.session import events

        details: dict[str, Any] = {
            "plugin": name,
            "hook": hook.command[:500],
            "event": hook.event,
            "reason": reason,
        }
        if code != HOOK_FAILED:
            details["exitCode"] = code
        elif err.startswith("timed out"):
            details["timedOut"] = True
        await hub.emit_event(session_id, events.error(HOOK_FAILED_CODE, message, details))
    except Exception:  # noqa: BLE001 - telling the surface is best effort
        log.debug("could not surface the hook failure", exc_info=True)


def health_lines() -> list[str]:
    """Human lines for every hook that is failing now."""
    return [
        f"- {h.plugin or 'plugin'} {h.event}: {h.reason} "
        f"({h.failures} failure{'s' if h.failures != 1 else ''}) [{h.command}]"
        for h in HEALTH.values()
    ]


def registry_of(core: Core) -> HookRegistry | None:
    loader = getattr(core, "skills", None)
    registry = getattr(loader, "hooks", None)
    return registry if isinstance(registry, HookRegistry) else None


def session_fields(session: Session) -> dict[str, Any]:
    """Who the session belongs to, so a guard need not read state.db for it.

    ``browser_provider`` is ``"host"`` or ``"local"`` for a browser session
    (who serves its ``browser_*`` calls) and ``None`` for any other.
    """
    from snowpea_core.tools.browser_providers import browser_provider_of

    return {
        "origin_surface": getattr(session, "origin_surface", None),
        "host_tools_from": getattr(session, "host_tools_from", None),
        "agent": getattr(session, "agent", None),
        "parent_session_id": getattr(session, "parent_session_id", None),
        "browser_provider": browser_provider_of(session),
    }


def tool_provider(core: Core, session: Session, tool_name: str) -> str | None:
    """Who serves ``tool_name`` here: ``host``, ``mcp`` or ``builtin``.

    A host tool is one a connected surface registered (``tool.register``); an
    MCP tool comes from a configured server; everything else, LSP included,
    runs in the daemon.  ``None`` when no tool by that name is registered.
    """
    tools = getattr(core, "tools", None)
    try:
        tool = tools.get(tool_name, session) if tools is not None else None
    except Exception:  # noqa: BLE001 - a lookup failure only drops the field
        tool = None
    if tool is None:
        return None
    source = str(getattr(tool, "source", "") or "")
    if source.startswith("host:"):
        return "host"
    if source.startswith("mcp:"):
        return "mcp"
    return "builtin"


def security_refusal(core: Any, session: Any, hook: Hook) -> str | None:
    """Why the security lists refuse ``hook``'s command, or ``None`` (review 5, C1 f).

    A hook command is a shell line like any other: one the lists block does not
    run and counts as a failed hook (a fail-closed guard then blocks its call).
    A project's hook whose file — or a project script it runs — is not as the
    person trusted it any more does not run either (security review 6, M-3).
    """
    if hook.project is not None and hook.rel:
        try:
            from snowpea_core.trust.projects import store_for

            if not store_for(core.paths.home).active(hook.project, "hooks", hook.rel):
                log.warning("hook %s not run: %s (or a script it runs) changed since the "
                            "project was trusted", hook.command, hook.rel)
                return ("the project's hooks, or a script they run, changed since you trusted "
                        "the project")
        except Exception:  # noqa: BLE001 - trust fails closed
            log.warning("could not check project trust for hook %s", hook.command, exc_info=True)
            return "project trust could not be checked for this hook"
    try:
        from snowpea_core.security import enforce

        verdict = enforce.judge_for_core(core, session, "shell",
                                         {"command": expand(hook.command, hook.root)}, ("exec",))
    except Exception:  # noqa: BLE001 - a check that cannot run refuses
        log.warning("could not check hook %s against the security lists", hook.command,
                    exc_info=True)
        return "the security lists could not check this hook command"
    if verdict.blocked:
        log.warning("hook %s blocked by the security lists: %s", hook.command, verdict.by)
        return f"blocked by the security list: {verdict.by} — {verdict.why}"
    return None


async def run_event(
    core: Core,
    session: Session,
    event: str,
    tool_name: str = "",
    tool_input: dict[str, Any] | None = None,
) -> HookOutcome:
    """Run every hook registered for ``event`` whose matcher accepts the tool."""
    registry = registry_of(core)
    if registry is None:
        return HookOutcome()
    hooks = registry.for_tool(event, tool_name, session.workdir)
    if not hooks:
        return HookOutcome()
    payload = {
        "event": event,
        "tool_name": tool_name,
        "tool_input": dict(tool_input or {}),
        "session_id": session.id,
        "cwd": str(session.workdir),
        **session_fields(session),
    }
    if tool_name:
        payload["tool_provider"] = tool_provider(core, session, tool_name)
    home = core.paths.home
    outcome = HookOutcome()
    for hook in hooks:
        refused = security_refusal(core, session, hook)
        if refused is not None:
            code, err = HOOK_FAILED, refused  # never started: a failed hook
        else:
            code, err = await run_hook(hook, payload, home=home, cwd=session.workdir)
        outcome.ran += 1
        if event == "PreToolUse" and code == BLOCK_EXIT_CODE:
            outcome.blocked = True
            outcome.message = err or f"{tool_name} was blocked by a {hook.plugin or 'plugin'} hook"
            return outcome
        if code in (0, BLOCK_EXIT_CODE):
            note_ok(hook)
        else:
            await note_failure(core, session, hook, code, err)
            if event == "PreToolUse" and hook.fail_closed:
                # A guard that did not run must not let the call through.
                outcome.blocked = True
                outcome.message = (
                    f"{tool_name} was blocked: the {hook.plugin or 'plugin'} guard hook "
                    f"failed ({err[:200] or f'exit {code}'})"
                )
                return outcome
    return outcome


async def pre_tool_use(
    core: Core, session: Session, tool_name: str, tool_input: dict[str, Any]
) -> str | None:
    """``None`` to continue; the error message when a hook blocked the call.

    Never raises: a failure inside the hook machinery is logged and the call
    goes on — unless a matching hook is ``failClosed``, which then blocks it.
    """
    try:
        outcome = await run_event(core, session, "PreToolUse", tool_name, tool_input)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - hook trouble must not become the turn's error
        log.warning("PreToolUse hooks for %s failed: %s", tool_name, exc, exc_info=True)
        registry = registry_of(core)
        guards = (registry.for_tool("PreToolUse", tool_name, session.workdir)
                  if registry is not None else [])
        if any(hook.fail_closed for hook in guards):
            return f"{tool_name} was blocked: a fail-closed guard hook could not run ({exc})"
        return None
    return outcome.message if outcome.blocked else None


async def post_tool_use(
    core: Core, session: Session, tool_name: str, tool_input: dict[str, Any]
) -> None:
    try:
        await run_event(core, session, "PostToolUse", tool_name, tool_input)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - hook trouble must not become the turn's error
        log.warning("PostToolUse hooks for %s failed", tool_name, exc_info=True)


@dataclass(frozen=True)
class StopDecision:
    """What the Stop hooks said about ending the turn."""

    block: bool = False
    reason: str = ""


def _stop_block_reason(code: int, out: str, err: str) -> str | None:
    """The instruction when a Stop hook blocks, else ``None`` (Claude Code's contract)."""
    if code == BLOCK_EXIT_CODE:
        return err or out or "A Stop hook asked you to continue."
    if code != 0:
        return None
    text = out.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("continue") is False:
        return None
    if str(data.get("decision") or "").lower() != "block":
        return None
    return str(data.get("reason") or "A Stop hook asked you to continue.")


async def stop(
    core: Core,
    session: Session,
    *,
    last_message: str = "",
    active: bool = False,
) -> StopDecision:
    """Run the Stop hooks; the first that blocks decides, with its reason."""
    registry = registry_of(core)
    if registry is None:
        return StopDecision()
    hooks = registry.for_tool("Stop", "", session.workdir)
    if not hooks:
        return StopDecision()
    payload = {
        "event": "Stop",
        "hook_event_name": "Stop",
        "tool_name": "",
        "tool_input": {},
        "session_id": session.id,
        "cwd": str(session.workdir),
        **session_fields(session),
        "stop_hook_active": active,
        "last_assistant_message": last_message,
    }
    for hook in hooks:
        refused = security_refusal(core, session, hook)
        if refused is not None:
            await note_failure(core, session, hook, HOOK_FAILED, refused)
            continue
        code, out, err = await run_hook_output(
            hook, payload, home=core.paths.home, cwd=session.workdir
        )
        reason = _stop_block_reason(code, out, err)
        if reason is not None:
            return StopDecision(block=True, reason=reason.strip())
        if code not in (0, BLOCK_EXIT_CODE):
            await note_failure(core, session, hook, code, err)
        else:
            note_ok(hook)
    return StopDecision()


__all__ = [
    "BLOCKED_PREFIX",
    "BLOCK_EXIT_CODE",
    "MAX_STOP_CONTINUATIONS",
    "StopDecision",
    "DEFAULT_TIMEOUT_SEC",
    "EVENTS",
    "HEALTH",
    "HOOK_FAILED_CODE",
    "Hook",
    "HookHealth",
    "HookOutcome",
    "HookRegistry",
    "expand",
    "failure_reason",
    "health_lines",
    "note_failure",
    "note_ok",
    "windows_python",
    "post_tool_use",
    "pre_tool_use",
    "run_event",
    "run_hook",
    "run_hook_output",
    "stop",
]
