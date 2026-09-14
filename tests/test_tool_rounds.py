"""The tool-round budget: a checkpoint that reports, then asks (CORE-subagent-budget)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, make_daemon
from test_session_loop import prompt

from snowpea_core.agent import loop as agent_loop

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "tool_rounds.json"
CONTINUE = agent_loop._CONTINUE_ROWS["ko"][2].format(n=2)
STOP = agent_loop._CONTINUE_ROWS["ko"][3]


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SNOWPEA_PROVIDER", f"fake:{FIXTURE}")
    monkeypatch.setenv("SNOWPEA_TEST", "1")
    instance = await make_daemon(tmp_path / "home", settings={"agent": {"max_tool_rounds": 2}})
    try:
        yield instance
    finally:
        await instance.stop()


@pytest_asyncio.fixture
async def http() -> Any:
    async with aiohttp.ClientSession() as s:
        yield s


async def open_session(client: RpcClient, workdir: Path, mode: str) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


async def test_continue_lets_the_turn_run_past_the_budget(
    daemon: Any, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    client = await connect(http, daemon, question_answer=[{"selected": [CONTINUE], "text": None}])
    session_id = await open_session(client, workdir, "auto")

    turn_id = await prompt(client, session_id, "파일 세 번 써줘")
    assert await client.wait_turn(turn_id) == "complete"
    # Three writes need three rounds; the budget was two, so the checkpoint fired once.
    assert [q["questions"][0]["header"] for q in client.questions] == ["도구 호출 한도"]
    assert sorted(p.name for p in workdir.glob("*.txt")) == ["a.txt", "b.txt", "c.txt"]
    assert client.of_kind("error") == []
    # The report is written *before* the question, so the person deciding can
    # see what the turn has done so far; the turn then finishes normally.
    said = [e["payload"]["text"] for e in client.of_kind("message.done")]
    assert said[0] == "a.txt와 b.txt를 썼습니다. c.txt가 남았습니다."
    assert said[-1] == "다 썼습니다"
    await client.stop()


async def test_stop_ends_the_turn_at_the_budget(
    daemon: Any, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    client = await connect(http, daemon, question_answer=[{"selected": [STOP], "text": None}])
    session_id = await open_session(client, workdir, "auto")

    turn_id = await prompt(client, session_id, "파일 세 번 써줘")
    # The budget is its own reason now: the turn ended having said something,
    # so it is not an error and the surface has a report to show.
    assert await client.wait_turn(turn_id) == "budget"
    assert len(client.questions) == 1
    assert sorted(p.name for p in workdir.glob("*.txt")) == ["a.txt", "b.txt"]
    assert client.of_kind("error") == []
    assert [e["payload"]["text"] for e in client.of_kind("message.done")] == [
        "a.txt와 b.txt를 썼습니다. c.txt가 남았습니다."
    ]
    await client.stop()
