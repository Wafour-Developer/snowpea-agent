"""Plugin hooks (M6 contract §1).

A plugin ships ``hooks/hooks.json`` in the Claude Code shape::

    {"hooks": {"PreToolUse": [{"matcher": "shell|write_file",
                               "hooks": [{"type": "command", "command": "…"}]}]}}

``matcher`` is a regular expression matched against the tool name (``"*"`` and
the empty string mean "every tool").  The command runs through the shell with
the hook payload on stdin::

    {"event", "tool_name", "tool_input", "session_id", "cwd"}

and ``SNOWPEA_HOME`` / ``SNOWPEA_TOOL_NAME`` in its environment, plus
``CLAUDE_PLUGIN_ROOT`` and ``SNOWPEA_PLUGIN_ROOT`` pointing at the plugin that
declared it.  For ``PreToolUse``, exit status 2 blocks the call and the hook's
stderr becomes the error the model sees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.skills.hooks")

#: The three events this milestone runs; others are parsed and ignored.
EVENTS: tuple[str, ...] = ("PreToolUse", "PostToolUse", "Stop")

#: Exit status a ``PreToolUse`` hook uses to refuse the call.
BLOCK_EXIT_CODE = 2

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
        self.hooks.setdefault(hook.event, []).append(hook)

    def for_tool(self, event: str, tool_name: str) -> list[Hook]:
        return [hook for hook in self.hooks.get(event, []) if hook.matches(tool_name)]

    def count(self) -> int:
        return sum(len(items) for items in self.hooks.values())

    def load_file(self, path: Path, *, plugin: str = "", root: Path | None = None) -> int:
        """Read one ``hooks.json``; returns how many hooks it contributed."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
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
                        )
                    )
                    added += 1
        return added


def expand(text: str, root: Path | None) -> str:
    """Substitute the plugin-root placeholders a Claude Code hook may use."""
    if root is None:
        return text
    value = str(root)
    for name in ("CLAUDE_PLUGIN_ROOT", "SNOWPEA_PLUGIN_ROOT"):
        text = text.replace(f"${{{name}}}", value).replace(f"${name}", value)
    return text


def _env(home: Path | str, tool_name: str, root: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env["SNOWPEA_HOME"] = str(home)
    env["SNOWPEA_TOOL_NAME"] = tool_name
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
    command = expand(hook.command, hook.root)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(home, str(payload.get("tool_name") or ""), hook.root),
            cwd=str(cwd) if cwd else None,
        )
    except OSError as exc:
        log.info("hook %s could not start: %s", hook.command, exc)
        return 0, ""
    try:
        _, err = await asyncio.wait_for(process.communicate(body), hook.timeout)
    except TimeoutError:
        process.kill()
        log.info("hook %s timed out after %.0fs", hook.command, hook.timeout)
        return 0, ""
    return int(process.returncode or 0), (err or b"").decode("utf-8", "replace").strip()


def registry_of(core: Core) -> HookRegistry | None:
    loader = getattr(core, "skills", None)
    registry = getattr(loader, "hooks", None)
    return registry if isinstance(registry, HookRegistry) else None


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
    hooks = registry.for_tool(event, tool_name)
    if not hooks:
        return HookOutcome()
    payload = {
        "event": event,
        "tool_name": tool_name,
        "tool_input": dict(tool_input or {}),
        "session_id": session.id,
        "cwd": str(session.workdir),
    }
    home = core.paths.home
    outcome = HookOutcome()
    for hook in hooks:
        code, err = await run_hook(hook, payload, home=home, cwd=session.workdir)
        outcome.ran += 1
        if event == "PreToolUse" and code == BLOCK_EXIT_CODE:
            outcome.blocked = True
            outcome.message = err or f"{tool_name} was blocked by a {hook.plugin or 'plugin'} hook"
            return outcome
    return outcome


async def pre_tool_use(
    core: Core, session: Session, tool_name: str, tool_input: dict[str, Any]
) -> str | None:
    """``None`` to continue; the error message when a hook blocked the call."""
    outcome = await run_event(core, session, "PreToolUse", tool_name, tool_input)
    return outcome.message if outcome.blocked else None


async def post_tool_use(
    core: Core, session: Session, tool_name: str, tool_input: dict[str, Any]
) -> None:
    await run_event(core, session, "PostToolUse", tool_name, tool_input)


async def stop(core: Core, session: Session) -> None:
    await run_event(core, session, "Stop")


__all__ = [
    "BLOCKED_PREFIX",
    "BLOCK_EXIT_CODE",
    "DEFAULT_TIMEOUT_SEC",
    "EVENTS",
    "Hook",
    "HookOutcome",
    "HookRegistry",
    "expand",
    "post_tool_use",
    "pre_tool_use",
    "run_event",
    "run_hook",
    "stop",
]
