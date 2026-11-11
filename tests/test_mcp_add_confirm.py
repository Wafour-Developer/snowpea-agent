"""``/mcp add`` asks before adding a server the security check flagged."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.commands import mcp_cmd
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import McpAddResult

FINDING = "the url is plain http, so its headers travel in clear text"


class FakeQueue:
    def __init__(self, label: str | None) -> None:
        self.label = label
        self.asked: list[Any] = []

    async def ask(self, session: Any, items: list[Any], cancel_event: Any = None) -> list[Any]:
        self.asked.append(items)
        selected = [self.label] if self.label else []
        return [SimpleNamespace(selected=selected, text=None)]


class Ctx:
    def __init__(self, queue: FakeQueue | None, *, attended: bool = True) -> None:
        self.core = SimpleNamespace(questions=queue)
        self.session = SimpleNamespace(
            mode="default",
            unattended=not attended,
            origin_conn=object() if attended else None,
            origin_surface="",
        )
        self.said: list[str] = []

    async def say(self, text: str) -> None:
        self.said.append(text)


def install_handler(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def handler(_conn: Any, params: Any, core: Any) -> McpAddResult:
        calls.append({"accept": params.acceptFindings, "force": params.force})
        if not (params.acceptFindings or params.force):
            raise RpcError(errors.MCP_UNSAFE, FINDING, {"findings": [FINDING]})
        return McpAddResult(
            ok=True, path="/x/.mcp.json", state="ready", tools=[], warnings=[FINDING]
        )

    monkeypatch.setattr(mcp_cmd, "mcp_add_handler", handler)
    return calls


ARGS = {"name": "web", "url": "http://example.com/mcp", "workdir": "/w"}


@pytest.mark.asyncio
async def test_a_flagged_server_is_added_when_the_person_agrees(monkeypatch) -> None:
    calls = install_handler(monkeypatch)
    queue = FakeQueue("Add")
    ctx = Ctx(queue)
    await mcp_cmd._add(ctx, dict(ARGS))
    assert [c["accept"] for c in calls] == [False, True]
    assert FINDING in queue.asked[0][0].question
    assert "saved to /x/.mcp.json" in ctx.said[-1]


@pytest.mark.asyncio
async def test_declining_writes_nothing(monkeypatch) -> None:
    calls = install_handler(monkeypatch)
    ctx = Ctx(FakeQueue("Cancel"))
    await mcp_cmd._add(ctx, dict(ARGS))
    assert len(calls) == 1
    assert "Not added" in ctx.said[-1]


@pytest.mark.asyncio
async def test_silence_is_not_consent(monkeypatch) -> None:
    calls = install_handler(monkeypatch)
    ctx = Ctx(FakeQueue(None))
    await mcp_cmd._add(ctx, dict(ARGS))
    assert len(calls) == 1
    assert "Not added" in ctx.said[-1]


@pytest.mark.asyncio
async def test_an_unattended_session_keeps_the_refusal(monkeypatch) -> None:
    install_handler(monkeypatch)
    queue = FakeQueue("Add")
    ctx = Ctx(queue, attended=False)
    await mcp_cmd._add(ctx, dict(ARGS))
    assert queue.asked == []
    assert "Not added" in ctx.said[-1]


@pytest.mark.asyncio
async def test_force_still_skips_the_question(monkeypatch) -> None:
    calls = install_handler(monkeypatch)
    queue = FakeQueue("Cancel")
    ctx = Ctx(queue)
    await mcp_cmd._add(ctx, {**ARGS, "force": True})
    assert queue.asked == []
    assert calls == [{"accept": False, "force": True}]
