"""IDE-PROGRESS: ``turn.started``, ``tool.progress`` and ``compaction.started``.

Three additive events a surface needs to show honest progress: when a turn
really began, what a long-running tool is printing while it runs, and that a
compaction is under way rather than merely finished.  Every assertion here is
about the wire, driven by the scripted fake provider.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import Recorder, RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import CHUNK_LIMIT, LocalBackend
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.session import compaction
from snowpea_core.session.history import ChatMessage
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ProgressEmitter, ToolContext
from snowpea_core.tools.shell import MAX_STREAMED_BYTES, shell

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "fake"
TIMEOUT = 30.0

#: The slow command in ``progress.json`` needs a ``python3`` on PATH.
needs_python3 = pytest.mark.skipif(
    shutil.which("python3") is None, reason="no python3 on PATH for the slow-command fixture"
)


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURES / "progress.json"):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def start_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


def payloads(client: RpcClient, kind: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in client.of_kind(kind)]


# ---------------------------------------------------------------------------
# turn.started
# ---------------------------------------------------------------------------


async def test_turn_started_precedes_the_first_delta(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The clock can start from the event, not from the first thing overheard."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        result = await client.ok(
            "session.prompt", {"sessionId": session_id, "text": "say something"}
        )
        turn_id = str(result["turnId"])
        await client.wait_turn(turn_id, TIMEOUT)

        kinds = client.kinds()
        assert "turn.started" in kinds
        assert kinds.index("turn.started") < kinds.index("message.delta")
        assert kinds.index("turn.started") < kinds.index("turn.done")

        started = payloads(client, "turn.started")
        assert len(started) == 1
        assert started[0]["turnId"] == turn_id
        assert started[0]["prompt"] == "say something"
        # It never waited, so it is not flagged as having been queued.
        assert started[0]["queued"] is False
    finally:
        await client.stop()


@needs_python3
async def test_turn_started_marks_a_turn_that_waited_in_the_queue(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """``queued`` is true only for the turn that actually sat in the FIFO."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        first = await client.ok(
            "session.prompt", {"sessionId": session_id, "text": "run the slow command"}
        )
        # Sent while the first turn is still inside the slow shell call.
        second = await client.ok(
            "session.prompt", {"sessionId": session_id, "text": "the follow-up"}
        )
        await client.wait_turn(str(first["turnId"]), TIMEOUT)
        await client.wait_turn(str(second["turnId"]), TIMEOUT)

        started = {payload["turnId"]: payload for payload in payloads(client, "turn.started")}
        assert started[str(first["turnId"])]["queued"] is False
        assert started[str(second["turnId"])]["queued"] is True

        # The queued turn is announced as started only once it is running:
        # the dequeue comes first, and its own start after it.
        kinds = client.kinds()
        assert kinds.index("turn.dequeued") < len(kinds) - 1
        starts = [index for index, kind in enumerate(kinds) if kind == "turn.started"]
        assert len(starts) == 2
        assert kinds.index("turn.dequeued") < starts[1]
    finally:
        await client.stop()


# ---------------------------------------------------------------------------
# tool.progress — shell
# ---------------------------------------------------------------------------


@needs_python3
async def test_shell_streams_progress_before_its_result(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The tail arrives while the command runs and says the same thing it does."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        result = await client.ok(
            "session.prompt", {"sessionId": session_id, "text": "run the slow command"}
        )
        await client.wait_turn(str(result["turnId"]), TIMEOUT)

        progress = payloads(client, "tool.progress")
        assert progress, f"no tool.progress; saw {client.kinds()}"
        assert all(item["name"] == "shell" for item in progress)
        # One call, so one strictly increasing seq run, and one callId.
        assert [item["seq"] for item in progress] == list(range(len(progress)))
        assert len({item["callId"] for item in progress}) == 1
        assert not any(item["truncated"] for item in progress)

        kinds = client.kinds()
        assert kinds.index("tool.call") < kinds.index("tool.progress")
        assert kinds.index("tool.progress") < kinds.index("tool.result")

        # The chunks reassemble to exactly the stdout the authoritative result
        # reports — nothing dropped, nothing reordered, nothing invented.
        streamed = "".join(item["chunk"] for item in progress if item["stream"] == "stdout")
        assert streamed == "".join(f"line {i}\n" for i in range(6))
        tool_result = payloads(client, "tool.result")[0]
        assert streamed.rstrip("\n") in tool_result["output"]
        assert progress[0]["callId"] == tool_result["callId"]
    finally:
        await client.stop()


def _ctx(tmp_path: Path, recorder: Recorder, core: Core) -> ToolContext:
    session = Session(id="s-progress", workdir=tmp_path)
    core.hub.subscribe(recorder, session.id)
    return ToolContext(
        session=session,
        core=core,
        backend=LocalBackend(tmp_path),
        call_id="call-1",
        progress=ProgressEmitter(core, session.id, "call-1", "shell"),
    )


async def test_shell_stops_streaming_at_the_byte_cap(tmp_path: Path) -> None:
    """Past the cap the tail stops once, loudly, and the result is unaffected."""
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    recorder = Recorder()
    ctx = _ctx(tmp_path, recorder, core)
    # Comfortably more than MAX_STREAMED_BYTES of ASCII.
    count = (MAX_STREAMED_BYTES // CHUNK_LIMIT) + 8
    command = f"{sys.executable} -c \"import sys; sys.stdout.write('x' * {CHUNK_LIMIT} * {count})\""
    result = await shell(ctx, {"command": command, "timeout": 60})
    assert result.ok

    progress = [event["payload"] for event in recorder.of_kind("tool.progress")]
    assert progress
    assert progress[-1]["truncated"] is True
    assert progress[-1]["chunk"] == ""
    # Only the final one is flagged, and only it is empty.
    assert not any(item["truncated"] for item in progress[:-1])
    streamed = sum(len(item["chunk"].encode()) for item in progress)
    assert streamed <= MAX_STREAMED_BYTES
    # The result is unaffected by the tail having stopped: it still reports the
    # exit status, with its own (separate, smaller) capture limit applied.
    assert result.output.endswith("[exit 0]")


async def test_shell_without_a_listener_does_not_stream(tmp_path: Path) -> None:
    """No sink, no streaming path: the plain ``run`` result is unchanged."""
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    recorder = Recorder()
    session = Session(id="s-quiet", workdir=tmp_path)
    core.hub.subscribe(recorder, session.id)
    ctx = ToolContext(session=session, core=core, backend=LocalBackend(tmp_path))
    result = await shell(ctx, {"command": "echo hello"})
    assert result.ok
    assert "hello" in result.output
    assert recorder.of_kind("tool.progress") == []


# ---------------------------------------------------------------------------
# tool.progress — delegate_task
# ---------------------------------------------------------------------------


async def test_delegation_forwards_the_child_last_text(tmp_path: Path) -> None:
    """A delegation that runs for minutes has the child's last line to show."""
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    recorder = Recorder()
    session = Session(id="s-delegate", workdir=tmp_path)
    core.hub.subscribe(recorder, session.id)

    from snowpea_core.agent.subagent import get_manager

    manager = get_manager(core)
    record = manager.new_record(session, "do a thing", None, "")
    record.progress = ProgressEmitter(core, session.id, "call-9", "delegate_task")
    await manager.emit_update(record, last_text="calling shell")

    progress = [event["payload"] for event in recorder.of_kind("tool.progress")]
    assert [item["chunk"] for item in progress] == ["calling shell"]
    assert progress[0]["name"] == "delegate_task"
    assert progress[0]["callId"] == "call-9"
    # The subagent event itself is untouched.
    assert recorder.of_kind("subagent.update")[0]["payload"]["lastText"] == "calling shell"


# ---------------------------------------------------------------------------
# compaction.started
# ---------------------------------------------------------------------------


async def test_compaction_started_precedes_the_completion(tmp_path: Path) -> None:
    """The announcement comes first; ``compaction`` still reports the outcome."""
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    recorder = Recorder()
    session = Session(id="s-compact", workdir=tmp_path)
    core.hub.subscribe(recorder, session.id)
    for index in range(40):
        session.history.append(ChatMessage(role="user", content=f"message {index} " * 40))
        session.history.append(ChatMessage(role="assistant", content=f"reply {index} " * 40))

    result = await compaction.compact_session(core, session)
    assert result.compacted

    kinds = recorder.kinds()
    assert kinds.index("compaction.started") < kinds.index("compaction")
    started = [event["payload"] for event in recorder.of_kind("compaction.started")]
    assert len(started) == 1
    assert started[0]["reason"] == "manual"
    assert started[0]["before"] == recorder.of_kind("compaction")[0]["payload"]["before"]


async def test_compaction_that_does_nothing_announces_nothing(tmp_path: Path) -> None:
    """A history too short to summarise must not raise a false 'Compacting…'."""
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    recorder = Recorder()
    session = Session(id="s-short", workdir=tmp_path)
    core.hub.subscribe(recorder, session.id)
    session.history.append(ChatMessage(role="user", content="hi"))

    result = await compaction.compact_session(core, session)
    assert not result.compacted
    assert recorder.of_kind("compaction.started") == []
    await asyncio.sleep(0)
