"""Shared helpers for the Claude Code adapter tests (M19 WP6a)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from _external_adapter import FAKE_CLAUDE

from snowpea_core.external import proc
from snowpea_core.external.base import ApprovalAnswer, ApprovalAsk
from snowpea_core.external.claude import encode_cwd
from snowpea_core.external.proc import PinnedExecutable, ProcessSupervisor
from snowpea_core.external.timeline import TimelineBuilder, TimelineItem

OWNER_EMAIL = "owner@example.com"


def claude_home() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"])


def pinned() -> PinnedExecutable:
    return PinnedExecutable(path=str(FAKE_CLAUDE.resolve()), version="2.1.325 (Claude Code, fake)")


async def until(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def write_transcript(cwd: str, records: Sequence[dict[str, Any]], *,
                     session_id: str | None = None, mtime: float | None = None) -> Path:
    """A transcript under ``$CLAUDE_CONFIG_DIR/projects`` the way Claude lays it out."""
    sid = session_id or str(uuid.uuid4())
    path = claude_home() / "projects" / encode_cwd(cwd) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps({"sessionId": sid, **record}) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def user(text: Any, cwd: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "uuid": str(uuid.uuid4()), "cwd": cwd,
            "timestamp": "2026-11-15T01:02:03.000Z",
            "message": {"role": "user", "content": text}, **extra}


def assistant(blocks: list[dict[str, Any]], cwd: str, **extra: Any) -> dict[str, Any]:
    return {"type": "assistant", "uuid": str(uuid.uuid4()), "cwd": cwd,
            "timestamp": "2026-11-15T01:02:04.000Z",
            "message": {"id": "msg_1", "role": "assistant", "model": "claude-haiku-5-5",
                        "content": blocks}, **extra}


def private(cwd: str) -> list[dict[str, Any]]:
    return [
        {"type": "attachment", "cwd": cwd, "attachment": {"type": "system",
                                                          "userEmail": OWNER_EMAIL,
                                                          "organizationUuid": "org-1234"},
         "message": {"role": "user", "content": f"you are talking to {OWNER_EMAIL}"}},
        {"type": "session-owner", "accountEmail": OWNER_EMAIL, "accountUuid": "acct-5678"},
    ]


def start_terminal(session_id: str, cwd: Path, **env: str) -> subprocess.Popen[bytes]:
    """The person's interactive claude holding ``session_id`` (``--fake-terminal``)."""
    return subprocess.Popen(
        [sys.executable, str(FAKE_CLAUDE), "--fake-terminal", "--resume", session_id],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(cwd), env={**proc.scrubbed_env(), **env},
    )


def stop(child: subprocess.Popen[bytes]) -> None:
    if child.stdin is not None:
        child.stdin.close()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


class StubHost:
    """A :class:`ChannelHost` that records what a channel reports."""

    def __init__(self, home: Path, session_id: str = "new",
                 answer: ApprovalAnswer | None = None) -> None:
        self.supervisor = ProcessSupervisor(home)
        self.env = proc.scrubbed_env()
        self._builder = TimelineBuilder("claude", session_id, salt="stub")
        self.items: list[TimelineItem] = []
        self.asks: list[ApprovalAsk] = []
        self.answer = answer or ApprovalAnswer(allow=True)
        self.finished: list[tuple[str, str | None]] = []
        self.died: list[str] = []
        self.ids: list[str] = []
        self.titles: list[str] = []

    @property
    def builder(self) -> TimelineBuilder:
        return self._builder

    async def emit(self, items: Sequence[TimelineItem]) -> None:
        self.items.extend(items)

    async def ask_approval(self, ask: ApprovalAsk) -> ApprovalAnswer:
        self.asks.append(ask)
        return self.answer

    async def turn_finished(self, reason: str = "complete", error: str | None = None) -> None:
        self.finished.append((reason, error))

    async def channel_died(self, error: str) -> None:
        self.died.append(error)

    async def id_known(self, session_id: str) -> None:
        self.ids.append(session_id)

    async def title_known(self, title: str) -> None:
        self.titles.append(title)

    def kinds(self) -> list[str]:
        return [item.kind for item in self.items]

    def texts(self) -> list[str]:
        return [str(item.payload.get("text", "")) for item in self.items
                if item.kind == "message.done"]


def fake_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def starts(path: Path) -> list[dict[str, Any]]:
    """CLI starts (sessions), not the ``auth status`` probes of discovery."""
    return [entry["data"] for entry in fake_log(path)
            if entry["kind"] == "start" and entry["data"]["argv"][:1] != ["auth"]]
