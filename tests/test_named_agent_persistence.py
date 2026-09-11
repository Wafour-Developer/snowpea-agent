"""US-021: named persistent agents (M7 contract §6, AC-17).

Two agents are created through the RPC surface a TUI would use, each bound to
its own fake chat channel, one of them carrying a scheduled job.  The daemon is
then restarted over the same ``SNOWPEA_HOME`` and everything is asserted again:
same session ids, same bindings, same jobs, and memories that never cross the
``agent:<name>`` namespace boundary.

The provider is the scripted fake and every "platform" is
:class:`~snowpea_core.gateway.fake.FakeAdapter`, so nothing here touches a
socket or a model.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect
from test_gateway import TIMEOUT

from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.server.app_server import Daemon

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "named_agents.json"

OPS_BRIEF = "운영 알림 담당"
BUILD_BRIEF = "빌드 리포트 담당"

OPS_FACT = "기억해: 운영 비밀번호는 alpaca 다"
BUILD_FACT = "기억해: 빌드 비밀번호는 bravo 다"
JOB_FACT = "기억해: 스케줄 채널은 opsonly 다"


@pytest_asyncio.fixture
async def named_env() -> AsyncIterator[None]:
    previous = {key: os.environ.get(key) for key in ("SNOWPEA_PROVIDER", "SNOWPEA_GATEWAY_FAKE")}
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    os.environ["SNOWPEA_GATEWAY_FAKE"] = "1"
    FakeAdapter.instances.clear()
    try:
        yield None
    finally:
        FakeAdapter.instances.clear()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def open_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


async def named_agents(client: RpcClient) -> dict[str, dict[str, Any]]:
    """``agent.list`` entries with ``kind == "named"``, keyed by name."""
    listed = (await client.ok("agent.list", {}))["agents"]
    return {entry["name"]: entry for entry in listed if entry["kind"] == "named"}


async def search(client: RpcClient, query: str, namespace: str) -> list[dict[str, Any]]:
    result = await client.ok("memory.search", {"query": query, "namespace": namespace})
    return list(result["hits"])


async def error(client: RpcClient, method: str, params: dict[str, Any]) -> str:
    """The message of the RPC error ``method`` answers with."""
    with pytest.raises(AssertionError) as caught:
        await client.ok(method, params)
    return str(caught.value)


async def say(adapter: FakeAdapter, text: str, channel_id: str, *, count: int = 1) -> str:
    """Push one inbound chat message and wait for the agent's reply."""
    await adapter.push(text, channel_id=channel_id, user_id="u1")
    return (await adapter.wait_for_send(TIMEOUT, count=count)).text


# ---------------------------------------------------------------------------
# (a) AC-17: two agents, two channels, a job, a restart
# ---------------------------------------------------------------------------


async def test_named_agents_survive_a_restart_with_isolated_memory(
    named_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    home = tmp_path / "home"

    first = Daemon(port=0, home=home)
    await first.start()
    try:
        client = await connect(http, first)
        await open_session(client, workdir)

        created_ops = await client.ok("agent.create", {"description": OPS_BRIEF, "named": True})
        created_build = await client.ok("agent.create", {"description": BUILD_BRIEF, "named": True})
        assert created_ops["name"] == "ops"
        assert created_build["name"] == "builder"

        await client.ok(
            "agent.bindChannel",
            {"name": "ops", "channel": "telegram:111", "credentialsRef": "tg_ops"},
        )
        await client.ok(
            "agent.bindChannel",
            {"name": "builder", "channel": "telegram:222", "credentialsRef": "tg_builder"},
        )
        scheduled = await client.ok(
            "job.schedule",
            {"spec": "in 3600s", "task": "무슨 일이 있었는지 요약해줘", "agent": "ops"},
        )

        before = await named_agents(client)
        assert sorted(before) == ["builder", "ops"]
        assert before["ops"]["namespace"] == "agent:ops"
        assert before["builder"]["namespace"] == "agent:builder"
        assert before["ops"]["channels"] == ["telegram:111"]
        assert before["builder"]["channels"] == ["telegram:222"]
        assert before["ops"]["jobs"] == [scheduled["jobId"]]
        assert before["builder"]["jobs"] == []
        assert before["ops"]["description"] == "운영 알림 담당 에이전트"

        # Each agent hears its own channel and remembers in its own namespace.
        ops_adapter = FakeAdapter.instances["tg_ops"]
        build_adapter = FakeAdapter.instances["tg_builder"]
        assert "기억해" in await say(ops_adapter, OPS_FACT, "111")
        assert "기억해" in await say(build_adapter, BUILD_FACT, "222")
        await asyncio.sleep(0.05)

        # The keepalive counts them (plan §2.6).
        info = await client.ok("system.info", {})
        assert info["counters"]["named_agents"] == 2
        assert "2 named agents" in info["lifecycle"]["summary"]

        await client.stop()
    finally:
        await first.stop()

    second = Daemon(port=0, home=home)
    await second.start()
    try:
        client = await connect(http, second)

        after = await named_agents(client)
        assert sorted(after) == ["builder", "ops"]
        for name in ("ops", "builder"):
            assert after[name]["sessionId"] == before[name]["sessionId"], name
            assert after[name]["channels"] == before[name]["channels"], name
            assert after[name]["bindings"] == before[name]["bindings"], name
        assert after["ops"]["jobs"] == [scheduled["jobId"]]

        bindings = (await client.ok("gateway.list", {}))["bindings"]
        assert sorted(b["target"] for b in bindings) == ["agent:builder", "agent:ops"]
        assert (await client.ok("system.info", {}))["counters"]["named_agents"] == 2

        # (b) memory is isolated by namespace, in both directions.
        assert await search(client, "alpaca", "agent:builder") == []
        assert await search(client, "bravo", "agent:ops") == []
        assert await search(client, "alpaca", "default") == []
        ops_hits = await search(client, "alpaca", "agent:ops")
        build_hits = await search(client, "bravo", "agent:builder")
        assert [hit["text"] for hit in ops_hits] == ["운영 비밀번호는 alpaca 다"]
        assert [hit["text"] for hit in build_hits] == ["빌드 비밀번호는 bravo 다"]

        # (c) the restored binding still reaches the agent's own session, and
        # does not open a gateway session beside it.
        ops_adapter = FakeAdapter.instances["tg_ops"]
        assert await say(ops_adapter, "상태 알려줘", "111")
        sessions = {s["sessionId"]: s for s in (await client.ok("session.list", {}))["sessions"]}
        assert after["ops"]["sessionId"] in sessions
        assert sessions[after["ops"]["sessionId"]]["originSurface"] == "agent:ops"
        assert not [s for s in sessions.values() if s["originSurface"] == "gateway:telegram:111"]

        # (d) delete takes the binding, the job and the session with it.
        await client.ok("agent.delete", {"name": "builder"})
        assert sorted(await named_agents(client)) == ["ops"]
        info = await client.ok("system.info", {})
        assert info["counters"]["named_agents"] == 1
        assert [b["target"] for b in (await client.ok("gateway.list", {}))["bindings"]] == [
            "agent:ops"
        ]
        assert after["builder"]["sessionId"] not in {
            s["sessionId"] for s in (await client.ok("session.list", {}))["sessions"]
        }

        await client.stop()
    finally:
        await second.stop()


# ---------------------------------------------------------------------------
# (e) a job that names an agent runs inside that agent's session
# ---------------------------------------------------------------------------


async def test_a_job_for_a_named_agent_runs_in_its_namespace(
    named_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        await open_session(client, workdir)
        await client.ok("agent.create", {"description": OPS_BRIEF, "named": True})
        agent = (await named_agents(client))["ops"]

        scheduled = await client.ok(
            "job.schedule", {"spec": "in 3600s", "task": JOB_FACT, "agent": "ops"}
        )
        await client.ok("job.runNow", {"jobId": scheduled["jobId"]})
        await asyncio.sleep(0.05)

        assert [hit["text"] for hit in await search(client, "opsonly", "agent:ops")] == [
            "스케줄 채널은 opsonly 다"
        ]
        assert await search(client, "opsonly", "default") == []

        # The run borrowed the agent's session; it did not close it.
        sessions = {s["sessionId"] for s in (await client.ok("session.list", {}))["sessions"]}
        assert agent["sessionId"] in sessions

        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (f) argument validation
# ---------------------------------------------------------------------------


async def test_bind_channel_and_delete_reject_unknown_input(
    named_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        await open_session(client, workdir)
        await client.ok("agent.create", {"description": OPS_BRIEF, "named": True})

        assert "no named agent" in await error(
            client, "agent.bindChannel", {"name": "nobody", "channel": "telegram:1"}
        )
        assert "platform" in await error(
            client, "agent.bindChannel", {"name": "ops", "channel": "telegram"}
        )
        assert "no named agent" in await error(client, "agent.delete", {"name": "nobody"})
        assert "already exists" in await error(
            client, "agent.create", {"description": OPS_BRIEF, "named": True}
        )

        await client.stop()
    finally:
        await daemon.stop()
