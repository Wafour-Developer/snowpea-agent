"""A generic test adapter on the external base layer, driving ``fake_claude.py``.

It is deliberately small and uses only what a real adapter (WP6) uses: the
pinned executable from discovery, :func:`proc.build_argv` (with
:func:`proc.bypass_args` behind a grant), ``host.supervisor.spawn`` with the
scrubbed ``host.env``, a :class:`TimelineBuilder` with the
:data:`USER_ASSISTANT_ONLY` privacy filter, and the channel host callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from snowpea_core.external import proc
from snowpea_core.external.base import (
    ApprovalAsk,
    ChannelHost,
    CliCapabilities,
    ExternalAdapter,
    ExternalChannel,
    LaunchSpec,
    Liveness,
    SessionInfo,
)
from snowpea_core.external.proc import ExternalError, ExternalProcess
from snowpea_core.external.timeline import (
    USER_ASSISTANT_ONLY,
    TimelineBuilder,
    TimelineItem,
    iso_from_text,
    iso_from_unix,
)
from snowpea_core.server import errors

FAKE_CLIS = Path(__file__).parent / "fixtures" / "fake_clis"
FAKE_CLAUDE = FAKE_CLIS / "fake_claude.py"


def config_dir() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"])


def encoded(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    # Privacy first: nothing but conversation records goes any further.
    return list(USER_ASSISTANT_ONLY.apply(out, lambda record: record.get("type")))


def convert(records: list[dict[str, Any]], builder: TimelineBuilder) -> list[TimelineItem]:
    for record in records:
        ts = iso_from_text(record.get("timestamp"))
        message = record.get("message") or {}
        content = message.get("content")
        if record.get("type") == "user":
            if isinstance(content, str):
                builder.user(content, key=record.get("uuid"), ts=ts)
                continue
            for block in content or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content")
                    builder.tool_result(
                        block.get("tool_use_id"),
                        ok=not block.get("is_error"),
                        output=text if isinstance(text, str) and not block.get("is_error") else "",
                        error=text if block.get("is_error") else None,
                        ts=ts,
                    )
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                builder.assistant(block.get("text", ""), key=record.get("uuid"), ts=ts)
            elif kind == "thinking":
                builder.reasoning("", key=f"{record.get('uuid')}:think", ts=ts)
            elif kind == "tool_use":
                builder.tool_call(block.get("id"), block.get("name", "tool"),
                                  block.get("input") or {}, ts=ts)
    builder.flush()
    return builder.drain()


class FakeClaudeChannel(ExternalChannel):
    def __init__(self, spec: LaunchSpec, host: ChannelHost) -> None:
        super().__init__(spec, host)
        self.process: ExternalProcess | None = None
        self.reader: asyncio.Task[None] | None = None
        self.closing = False
        self.started_mode: str | None = None
        self.initialized: asyncio.Future[None] | None = None
        self.approval_tasks: set[asyncio.Task[None]] = set()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.alive

    def pids(self) -> set[int]:
        return {self.process.pid} if self.process is not None and self.process.alive else set()

    async def start(self) -> None:
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
            await self.host_api.id_known(self.session_id)
        await self._spawn()

    async def _spawn(self) -> None:
        assert self.session_id is not None
        args = [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "plan" if self.mode == "readonly" else "default",
            "--permission-prompts", "host", "--permission-prompt-tool", "stdio",
            "--resume" if self.spec.resume else "--session-id", self.session_id,
        ]
        if self.spec.model:
            args += ["--model", self.spec.model]
        bypass = None
        if self.mode == "unrestricted":
            if self.grant is None:
                raise ExternalError(errors.JARVIS_PERSON_ONLY, "unrestricted needs a grant")
            bypass = proc.bypass_args(self.grant, ["--dangerously-skip-permissions"])
        argv = proc.build_argv(self.spec.executable, args, bypass=bypass)
        self.process = await self.host_api.supervisor.spawn(
            argv, cwd=self.spec.cwd, env=self.host_api.env, cli="claude", ref=self.session_id
        )
        self.started_mode = self.mode
        self.closing = False
        self.initialized = asyncio.get_running_loop().create_future()
        self.reader = asyncio.ensure_future(self._read(self.process))
        await self.process.write_line(
            {"type": "control_request", "request_id": "init_1",
             "request": {"subtype": "initialize"}}
        )
        try:
            await asyncio.wait_for(asyncio.shield(self.initialized), 10)
        except TimeoutError as exc:
            await self.close()
            raise ExternalError(errors.EXTERNAL_UNAVAILABLE, "claude did not initialize") from exc

    async def _read(self, process: ExternalProcess) -> None:
        builder = self.host_api.builder
        async for frame in process.json_lines():
            kind = frame.get("type")
            if kind == "control_response":
                response = frame.get("response") or {}
                if response.get("request_id") == "init_1" and self.initialized is not None:
                    if not self.initialized.done():
                        self.initialized.set_result(None)
                continue
            if kind == "control_request":
                request = frame.get("request") or {}
                if request.get("subtype") == "can_use_tool":
                    task = asyncio.ensure_future(self._approve(process, frame))
                    self.approval_tasks.add(task)
                    task.add_done_callback(self.approval_tasks.discard)
                continue
            if kind == "assistant":
                for block in (frame.get("message") or {}).get("content") or []:
                    if block.get("type") == "text":
                        builder.assistant(block.get("text", ""), key=frame.get("uuid"))
                    elif block.get("type") == "thinking":
                        builder.reasoning("", key=f"{frame.get('uuid')}:think")
                    elif block.get("type") == "tool_use":
                        builder.tool_call(block.get("id"), block.get("name", "tool"),
                                          block.get("input") or {})
            elif kind == "user" and not frame.get("isReplay"):
                for block in (frame.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        error = bool(block.get("is_error"))
                        builder.tool_result(block.get("tool_use_id"), ok=not error,
                                            output="" if error else str(block.get("content")),
                                            error=str(block.get("content")) if error else None)
            elif kind == "result":
                await self.host_api.emit(builder.drain())
                interrupted = frame.get("terminal_reason") == "interrupted"
                await self.host_api.turn_finished("interrupted" if interrupted else "complete")
                continue
            await self.host_api.emit(builder.drain())
        code = await process.wait()
        if not self.closing and process is self.process:
            await self.host_api.channel_died(f"claude exited with status {code}")

    async def _approve(self, process: ExternalProcess, frame: dict[str, Any]) -> None:
        request = frame.get("request") or {}
        answer = await self.host_api.ask_approval(
            ApprovalAsk(tool=str(request.get("tool_name")), args=dict(request.get("input") or {}),
                        note=str(request.get("description") or ""))
        )
        if answer.allow:
            body = {"behavior": "allow", "updatedInput": request.get("input") or {}}
        else:
            body = {"behavior": "deny", "message": answer.reason or "Denied", "interrupt": False}
        with contextlib.suppress(ExternalError):
            await process.write_line({"type": "control_response", "response": {
                "subtype": "success", "request_id": frame.get("request_id"), "response": body}})

    async def send(self, text: str, turn_id: str) -> None:
        if self.started_mode != self.mode and self.process is not None:
            # A mode switch takes effect from the next turn: restart with new flags.
            await self.close()
            self.spec.resume = True
            await self._spawn()
        assert self.process is not None
        await self.process.write_line(
            {"type": "user", "message": {"role": "user", "content": text},
             "parent_tool_use_id": None, "session_id": ""}
        )

    async def interrupt(self) -> None:
        if self.process is not None:
            await self.process.write_line(
                {"type": "control_request", "request_id": f"int_{uuid.uuid4().hex[:6]}",
                 "request": {"subtype": "interrupt"}}
            )

    async def close(self) -> None:
        self.closing = True
        for task in list(self.approval_tasks):
            task.cancel()
        if self.process is not None:
            await self.process.terminate(grace=1.0)
        if self.reader is not None and self.reader is not asyncio.current_task():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


class FakeClaudeAdapter(ExternalAdapter):
    id = "claude"
    title = "Claude Code (fake)"
    binary = "claude-fake-not-on-path"
    capabilities = CliCapabilities(streaming=True, approvals="bridged", live_view=True)

    async def list_sessions(
        self, roots: Sequence[str], *, limit: int, before: str | None = None
    ) -> list[SessionInfo]:
        rows: list[SessionInfo] = []
        projects = config_dir() / "projects"
        for path in projects.glob("*/*.jsonl"):
            info = self._info(path)
            if info is None:
                continue
            if before is not None and (info.updated_at or "") > before:
                continue
            if any(info.cwd == root or info.cwd.startswith(root.rstrip("/") + "/")
                   for root in roots):
                rows.append(info)
        rows.sort(key=lambda info: info.updated_at or "", reverse=True)
        return rows[:limit]

    def _info(self, path: Path) -> SessionInfo | None:
        records = _records(path)
        if not records:
            return None
        cwd = next((r.get("cwd") for r in records if r.get("cwd")), None)
        if not cwd:
            return None
        first = next((r for r in records if r.get("type") == "user"
                      and isinstance((r.get("message") or {}).get("content"), str)), None)
        preview = (first.get("message") or {}).get("content", "") if first else ""
        return SessionInfo(id=path.stem, cwd=cwd, title=preview[:60] or None,
                           updated_at=iso_from_unix(path.stat().st_mtime), preview=preview,
                           extra={"path": str(path)})

    async def get_session(self, session_id: str) -> SessionInfo | None:
        for path in (config_dir() / "projects").glob(f"*/{session_id}.jsonl"):
            return self._info(path)
        return None

    async def read(self, info: SessionInfo) -> list[TimelineItem]:
        path = Path(info.extra.get("path") or "")
        if not path.is_file():
            found = await self.get_session(info.id)
            path = Path(found.extra["path"]) if found else path
        return convert(_records(path), TimelineBuilder(self.id, info.id))

    async def liveness(self, info: SessionInfo, *, own_pids: set[int]) -> Liveness:
        for path in (config_dir() / "sessions").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pid = int(data["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if data.get("sessionId") != info.id or pid in own_pids:
                continue
            if not proc.pid_alive(pid) or proc.process_start_time(pid) != data.get("procStart"):
                continue
            return Liveness(host="terminal", busy=data.get("status") == "busy")
        return Liveness()

    def channel(self, spec: LaunchSpec, host: ChannelHost) -> ExternalChannel:
        return FakeClaudeChannel(spec, host)


__all__ = ["FAKE_CLAUDE", "FAKE_CLIS", "FakeClaudeAdapter", "config_dir", "encoded"]
