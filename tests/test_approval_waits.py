"""1.35.0: an approval waits for the person at a TUI or desktop session.

Only a run with nobody in front of it (a browser-driven run, a messenger, a
scheduled job) keeps ``approvals.timeoutSec``; and a timeout is "not answered
yet", never a refusal the model may route around.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server import errors


class _Origin:
    closed = False

    def __init__(self, kind: str | None) -> None:
        self.client_kind = kind
        self.timeouts: list[float | None] = []
        self.release = asyncio.Event()

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
        self.timeouts.append(timeout)
        self.requested = params
        await self.release.wait()
        return {"decision": "allow", "scope": "once"}


def _queue() -> ApprovalQueue:
    return ApprovalQueue(Settings.model_validate({"approvals": {"timeoutSec": 1}}))


def _session(origin: Any) -> Any:
    return SimpleNamespace(id="s-1", origin_conn=origin, workdir="/tmp", kind="chat")


@pytest.mark.asyncio
async def test_a_tui_approval_has_no_clock() -> None:
    queue, origin = _queue(), _Origin("tui")
    task = asyncio.ensure_future(queue.request(_session(origin), "shell", {"command": "ls"}))
    await asyncio.sleep(1.6)  # past the 1 s setting
    assert not task.done()
    assert origin.requested["timeoutSec"] == 0
    origin.release.set()
    decision = await asyncio.wait_for(task, 5.0)
    assert decision.allowed
    assert origin.timeouts == [None]


@pytest.mark.asyncio
async def test_a_browser_run_keeps_the_setting() -> None:
    queue, origin = _queue(), _Origin("browser")
    decision = await asyncio.wait_for(
        queue.request(_session(origin), "shell", {"command": "ls"}), 8.0
    )
    assert not decision.allowed
    assert decision.code == errors.APPROVAL_TIMEOUT
    assert origin.requested["timeoutSec"] == 1


@pytest.mark.asyncio
async def test_an_unattended_request_keeps_the_setting() -> None:
    queue = _queue()
    decision = await asyncio.wait_for(
        queue.request(_session(_Origin("tui")), "shell", {"command": "ls"}, unattended=True),
        8.0,
    )
    assert decision.code == errors.APPROVAL_TIMEOUT
