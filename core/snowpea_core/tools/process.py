"""Background shell processes: launch, list, kill.

``shell`` with ``background: true`` detaches the command on the session backend
and records its pid here.  ``process_list`` and ``process_kill`` read and act
on that registry.  The registry is deliberately thin — one row per launch, a
liveness probe on read — because a long-running build or dev server is the only
case it exists for.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

#: Where a background command's combined output is written on the backend.
LOG_DIR = ".snowpea/logs"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class BackgroundProcess:
    """One detached command launched by ``shell``."""

    id: str
    session_id: str
    pid: int
    command: str
    log_path: str
    backend_kind: str
    started_at: str
    exit_code: int | None = None

    def row(self, running: bool) -> str:
        if running:
            state = "running"
        else:
            state = f"exited({self.exit_code if self.exit_code is not None else '?'})"
        return f"{self.id}\tpid={self.pid}\t{state}\t{self.started_at}\t{self.command}"


@dataclass
class ProcessRegistry:
    """Every background process the daemon has launched, newest last."""

    _processes: dict[str, BackgroundProcess] = field(default_factory=dict)
    _counter: int = 0

    def add(
        self,
        *,
        session_id: str,
        pid: int,
        command: str,
        log_path: str,
        backend_kind: str,
    ) -> BackgroundProcess:
        self._counter += 1
        entry = BackgroundProcess(
            id=f"bg-{self._counter}",
            session_id=session_id,
            pid=pid,
            command=command,
            log_path=log_path,
            backend_kind=backend_kind,
            started_at=_now(),
        )
        self._processes[entry.id] = entry
        return entry

    def get(self, process_id: str) -> BackgroundProcess | None:
        return self._processes.get(process_id)

    def list(self, session_id: str | None = None) -> list[BackgroundProcess]:
        return [
            p
            for p in self._processes.values()
            if session_id is None or p.session_id == session_id
        ]

    def drop(self, process_id: str) -> None:
        self._processes.pop(process_id, None)

    def clear(self) -> None:
        self._processes.clear()
        self._counter = 0


#: Process-wide registry; the daemon is one process.
REGISTRY = ProcessRegistry()


async def _alive(ctx: ToolContext, pid: int) -> bool:
    try:
        result = await ctx.backend.run(f"kill -0 {pid} 2>/dev/null", cwd=None, timeout=10.0)
    except OSError:
        return False
    return result.exit_code == 0


async def launch(ctx: ToolContext, command: str) -> ToolResult:
    """Detach ``command`` on the backend and register its pid."""
    log_path = f"{LOG_DIR}/bg-{int(datetime.now(UTC).timestamp() * 1000)}.log"
    script = (
        f"mkdir -p {shlex.quote(LOG_DIR)} && "
        f"nohup sh -c {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 & echo $!"
    )
    try:
        result = await ctx.backend.run(script, cwd=None, timeout=30.0)
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    pid_text = (result.stdout or "").strip().splitlines()
    if result.exit_code != 0 or not pid_text or not pid_text[-1].isdigit():
        return ToolResult(
            ok=False,
            output=result.stdout,
            error=(result.stderr or "could not start the background command").strip(),
        )
    entry = REGISTRY.add(
        session_id=ctx.session.id,
        pid=int(pid_text[-1]),
        command=command,
        log_path=log_path,
        backend_kind=str(getattr(ctx.backend, "kind", "local")),
    )
    return ToolResult(
        ok=True,
        output=(
            f"started {entry.id} (pid {entry.pid}) in the background\n"
            f"output: {entry.log_path}"
        ),
        path=entry.log_path,
    )


async def process_list(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    scope_all = bool(args.get("allSessions", False))
    entries = REGISTRY.list(None if scope_all else ctx.session.id)
    if not entries:
        return ToolResult(ok=True, output="no background processes")
    rows = []
    for entry in entries:
        running = await _alive(ctx, entry.pid)
        if not running and entry.exit_code is None:
            entry.exit_code = 0
        rows.append(entry.row(running))
    return ToolResult(ok=True, output="\n".join(rows))


async def process_kill(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    process_id = str(args.get("id", "")).strip()
    if not process_id:
        return ToolResult(ok=False, error="id is required (see process_list)")
    entry = REGISTRY.get(process_id)
    if entry is None:
        return ToolResult(ok=False, error=f"no such background process: {process_id}")
    signal_name = str(args.get("signal", "TERM")).strip().upper().removeprefix("SIG") or "TERM"
    if signal_name not in {"TERM", "KILL", "INT", "HUP", "QUIT"}:
        return ToolResult(ok=False, error=f"unsupported signal: {signal_name}")
    try:
        result = await ctx.backend.run(
            f"kill -{signal_name} {entry.pid}", cwd=None, timeout=15.0
        )
    except OSError as exc:
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if result.exit_code != 0:
        return ToolResult(
            ok=False,
            error=(result.stderr or f"kill exited with {result.exit_code}").strip(),
        )
    entry.exit_code = -1
    REGISTRY.drop(entry.id)
    return ToolResult(ok=True, output=f"sent SIG{signal_name} to {entry.id} (pid {entry.pid})")


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="process_list",
        category="terminal",
        description="List the background commands this session started and whether they run.",
        input_schema={
            "type": "object",
            "properties": {
                "allSessions": {
                    "type": "boolean",
                    "description": "Include processes started by other sessions.",
                }
            },
        },
        permission="read",
        run=process_list,
    ),
    Tool(
        name="process_kill",
        category="terminal",
        description="Signal a background command started by shell; default SIGTERM.",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Process id from process_list."},
                "signal": {
                    "type": "string",
                    "description": "TERM, KILL, INT, HUP or QUIT (default TERM).",
                },
            },
            "required": ["id"],
        },
        permission="exec",
        run=process_kill,
    ),
)


__all__ = [
    "LOG_DIR",
    "REGISTRY",
    "TOOLS",
    "BackgroundProcess",
    "ProcessRegistry",
    "launch",
    "process_kill",
    "process_list",
]
