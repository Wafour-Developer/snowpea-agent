"""M5 US-015: spec parsing, the job table, unattended runs and keepalive.

The parsing tests are plain unit tests.  Everything else drives a real
in-process daemon over its WebSocket with the scripted fake provider, so a job
really does open a session, call the "model", run tools and deliver its answer
— the same path AC-08 and AC-20 exercise by hand against Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from snowpea_core.cli import commands as cli_commands
from snowpea_core.scheduler import services
from snowpea_core.scheduler.jobs import utc_now
from snowpea_core.scheduler.nl_parse import first_run, parse_spec
from snowpea_core.server.app_server import Daemon
from snowpea_core.server.lifecycle import Lifecycle
from snowpea_core.server.protocol import PROTOCOL_VERSION

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "scheduler.json"
TIMEOUT = 20.0

#: A fixed "now" so the parsing expectations never depend on the wall clock.
NOW = datetime(2026, 9, 11, 14, 30, tzinfo=UTC).astimezone()


# ---------------------------------------------------------------------------
# (1) spec parsing — cron, relative, interval, ko + en natural language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "kind"),
    [
        ("0 9 * * *", "cron"),
        ("*/15 * * * *", "cron"),
        ("in 60s", "once"),
        ("in 2h", "once"),
        ("10분 뒤", "once"),
        ("30분 후에", "once"),
        ("every 10m", "interval"),
        ("매 30분", "interval"),
        ("10분마다", "interval"),
        ("매일 09:00", "cron"),
        ("매일 아침 9시", "cron"),
        ("every day at 9am", "cron"),
        ("daily at 21:30", "cron"),
        ("평일 08:00", "cron"),
        ("every monday at 9am", "cron"),
        ("매주 금요일 18:30", "cron"),
        ("tomorrow 10:00", "once"),
        ("내일 10시", "once"),
    ],
)
async def test_parse_spec_kinds(spec: str, kind: str) -> None:
    parsed = parse_spec(spec, NOW)
    assert parsed.kind == kind
    assert first_run(parsed, NOW) is not None


async def test_parse_spec_clock_values() -> None:
    """The Korean and English spellings of nine in the morning agree."""
    for spec in ("매일 09:00", "매일 아침 9시", "every day at 9am", "매일 오전 9시"):
        assert parse_spec(spec, NOW).cron == "0 9 * * *"
    assert parse_spec("매일 오후 3시", NOW).cron == "0 15 * * *"
    assert parse_spec("every day at 9:30pm", NOW).cron == "30 21 * * *"


async def test_parse_spec_relative_and_interval_seconds() -> None:
    assert parse_spec("in 60s", NOW).at == NOW.astimezone(UTC) + timedelta(seconds=60)
    assert parse_spec("10분 뒤", NOW).at == NOW.astimezone(UTC) + timedelta(minutes=10)
    assert parse_spec("every 10m", NOW).interval_sec == 600
    assert parse_spec("매 30분", NOW).interval_sec == 1800


async def test_parse_spec_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        parse_spec("whenever you feel like it", NOW)
    with pytest.raises(ValueError):
        parse_spec("", NOW)


# ---------------------------------------------------------------------------
# (2) lifecycle keepalive reasons (plan §2.6)
# ---------------------------------------------------------------------------


async def test_lifecycle_reason_line_for_one_job() -> None:
    lifecycle = Lifecycle(idle_timeout_sec=1800)
    lifecycle.set_counter("jobs", 1)
    assert lifecycle.summary() == "will not exit: 1 job"
    assert lifecycle.status()["reasons"] == ["1 job"]

    lifecycle.set_counter("gateway_bindings", 2)
    assert lifecycle.summary() == "will not exit: 2 gateway bindings, 1 job"

    lifecycle.set_counter("jobs", 0)
    lifecycle.set_counter("gateway_bindings", 0)
    assert lifecycle.summary().startswith("will exit in ")


# ---------------------------------------------------------------------------
# daemon harness
# ---------------------------------------------------------------------------


class Client:
    """Minimal JSON-RPC client with an event log and an approval policy."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.events: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.approval_requests: list[dict[str, Any]] = []
        #: "allow", "deny" or "ignore" (never answer, so the daemon times out).
        self.approval_mode = "allow"

    def start(self) -> None:
        self._reader = asyncio.ensure_future(self._read())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        await self._ws.close()

    async def _read(self) -> None:
        async for message in self._ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if "method" not in frame:
                future = self._pending.pop(int(frame["id"]), None)
                if future is not None and not future.done():
                    future.set_result(frame)
                continue
            if frame.get("id") is None:
                self.notifications.append(frame)
                if frame["method"] == "session.event":
                    self.events.append(frame["params"])
                continue
            self.approval_requests.append(frame["params"])
            if self.approval_mode == "ignore":
                continue
            await self._ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": {"decision": self.approval_mode, "scope": "once"},
                }
            )

    async def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = TIMEOUT
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return await asyncio.wait_for(future, timeout)

    async def ok(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = TIMEOUT
    ) -> dict[str, Any]:
        frame = await self.call(method, params, timeout)
        assert "error" not in frame, frame["error"]
        return frame["result"]

    def of_method(self, method: str) -> list[dict[str, Any]]:
        return [frame["params"] for frame in self.notifications if frame["method"] == method]

    async def wait(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = TIMEOUT
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for event in self.events:
                if predicate(event):
                    return event
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out; saw {[e['kind'] for e in self.events]}")
            await asyncio.sleep(0.02)


async def make_daemon(home: Path, settings: dict[str, Any] | None = None) -> Daemon:
    home.mkdir(parents=True, exist_ok=True)
    if settings:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    return daemon


@pytest_asyncio.fixture
async def provider_env() -> AsyncIterator[None]:
    previous = os.environ.get("SNOWPEA_PROVIDER")
    os.environ["SNOWPEA_PROVIDER"] = f"fake:{FIXTURE}"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SNOWPEA_PROVIDER", None)
        else:
            os.environ["SNOWPEA_PROVIDER"] = previous


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, provider_env: None) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


@pytest_asyncio.fixture
async def http() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


async def connect(http: aiohttp.ClientSession, daemon: Daemon) -> Client:
    ws = await http.ws_connect(f"http://127.0.0.1:{daemon.port}/ws")
    client = Client(ws)
    client.start()
    await client.ok(
        "system.hello",
        {
            "token": daemon.token,
            "clientVersion": "test-us015",
            "protocolVersion": PROTOCOL_VERSION,
        },
    )
    return client


def jobs_log(daemon: Daemon) -> str:
    path = daemon.paths.jobs_log
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# (3) job.schedule / job.list over RPC
# ---------------------------------------------------------------------------


async def test_schedule_over_rpc_shows_up_in_job_list(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon)
    result = await client.ok(
        "job.schedule",
        {
            "spec": "매일 09:00",
            "task": "summarise the repo",
            "mode": "accept",
            "channel": "log",
            "workdir": str(tmp_path),
        },
    )
    job_id = result["jobId"]
    assert result["nextRunAt"]

    listing = await client.ok("job.list", {})
    jobs = {job["jobId"]: job for job in listing["jobs"]}
    assert job_id in jobs
    job = jobs[job_id]
    assert job["kind"] == "cron"
    assert job["spec"] == "매일 09:00"
    assert job["nextRunAt"] == result["nextRunAt"]
    assert job["lastRunAt"] is None
    assert job["lastStatus"] is None
    assert daemon.core is not None
    assert daemon.core.lifecycle.counters["jobs"] == 1
    await client.stop()


async def test_schedule_rejects_an_unparseable_spec(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon)
    frame = await client.call(
        "job.schedule", {"spec": "sometime soonish", "task": "x", "workdir": str(tmp_path)}
    )
    assert frame["error"]["data"]["code"] == "invalid_params"
    await client.stop()


# ---------------------------------------------------------------------------
# (4) job.runNow runs in the daemon and delivers to the log channel (AC-08)
# ---------------------------------------------------------------------------


async def test_run_now_executes_in_process_and_records_the_run(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon)
    schedule = await client.ok(
        "job.schedule",
        {
            "spec": "0 9 * * *",
            "task": "summarise the repo",
            "channel": "log",
            "workdir": str(tmp_path),
        },
    )
    job_id = schedule["jobId"]

    await client.ok("job.runNow", {"jobId": job_id})

    # The run happened in this process: the session it opened is in the store
    # of the very daemon we are talking to, tagged as coming from the scheduler.
    assert daemon.core is not None
    sessions = await daemon.core.store.list_sessions(include_closed=True)
    scheduler_sessions = [s for s in sessions if s["origin_surface"] == "scheduler"]
    assert len(scheduler_sessions) == 1
    assert scheduler_sessions[0]["workdir"] == str(tmp_path)

    # The provider was called and its answer reached the log channel.
    log_text = jobs_log(daemon)
    assert "repo summary: nothing changed" in log_text
    assert job_id in log_text

    listing = await client.ok("job.list", {})
    job = next(item for item in listing["jobs"] if item["jobId"] == job_id)
    assert job["lastStatus"] == "ok"
    assert job["lastRunAt"] is not None
    # runNow leaves the schedule alone.
    assert job["nextRunAt"] == schedule["nextRunAt"]

    events = client.of_method("job.event")
    kinds = [event["kind"] for event in events if event["jobId"] == job_id]
    assert kinds == ["started", "finished"]
    await client.stop()


# ---------------------------------------------------------------------------
# (5) the double-fire guard
# ---------------------------------------------------------------------------


async def test_the_same_occurrence_only_runs_once(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon)
    schedule = await client.ok(
        "job.schedule",
        {"spec": "every 10m", "task": "summarise the repo", "workdir": str(tmp_path)},
    )
    assert daemon.core is not None
    scheduler = services(daemon.core)
    job = await scheduler.get(schedule["jobId"])
    assert job is not None

    occurrence = utc_now()
    await scheduler._fire(job, occurrence)
    await scheduler._fire(job, occurrence)

    runs = await scheduler.store.runs(job.id)
    assert len(runs) == 1
    assert jobs_log(daemon).count(job.id) == 1
    await client.stop()


async def test_a_tick_fires_a_due_job_and_moves_it_on(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon)
    schedule = await client.ok(
        "job.schedule",
        {"spec": "in 1s", "task": "summarise the repo", "workdir": str(tmp_path)},
    )
    assert daemon.core is not None
    scheduler = services(daemon.core)

    fired = await scheduler.tick(utc_now() + timedelta(seconds=5))
    assert fired == 1

    job = await scheduler.get(schedule["jobId"])
    assert job is not None
    # A one-shot has nothing left to do, so it stops holding the daemon open.
    assert job.next_run is None
    assert job.enabled is False
    assert job.last_status == "ok"
    assert daemon.core.lifecycle.counters["jobs"] == 0
    await client.stop()


# ---------------------------------------------------------------------------
# (6) an unattended approval that nobody answers (AC-20 b)
# ---------------------------------------------------------------------------


async def test_unanswered_approval_marks_the_run_denied_by_timeout(
    tmp_path: Path, http: aiohttp.ClientSession, provider_env: None
) -> None:
    daemon = await make_daemon(
        tmp_path / "home",
        {"approvals": {"timeoutSec": 1}, "scheduler": {"tickSec": 1}},
    )
    try:
        client = await connect(http, daemon)
        client.approval_mode = "ignore"
        schedule = await client.ok(
            "job.schedule",
            {
                "spec": "0 9 * * *",
                "task": "list the files",
                "mode": "accept",
                "workdir": str(tmp_path),
            },
        )
        job_id = schedule["jobId"]

        await client.ok("job.runNow", {"jobId": job_id}, timeout=30.0)

        listing = await client.ok("job.list", {})
        job = next(item for item in listing["jobs"] if item["jobId"] == job_id)
        assert job["lastStatus"] == "denied_by_timeout"

        kinds = [event["kind"] for event in client.of_method("job.event")]
        assert kinds == ["started", "denied"]
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (7) the /schedule command
# ---------------------------------------------------------------------------


async def test_slash_schedule_registers_a_job(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    session = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})

    turn = await client.ok(
        "session.prompt",
        {"sessionId": session["sessionId"], "text": '/schedule "in 60s" "echo hi"'},
    )
    await client.wait(
        lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn["turnId"]
    )
    said = [e for e in client.events if e["kind"] == "message.done"]
    assert said and said[-1]["payload"]["text"].startswith("scheduled j-")

    listing = await client.ok("job.list", {})
    assert [job["task"] for job in listing["jobs"]] == ["echo hi"]
    assert listing["jobs"][0]["spec"] == "in 60s"

    # ...and /schedule list can read it back.
    turn = await client.ok(
        "session.prompt", {"sessionId": session["sessionId"], "text": "/schedule list"}
    )
    await client.wait(
        lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn["turnId"]
    )
    assert listing["jobs"][0]["jobId"] in client.events[-2]["payload"]["text"]
    await client.stop()


# ---------------------------------------------------------------------------
# (8) the CLI
# ---------------------------------------------------------------------------


async def test_cli_job_list_json(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = await connect(http, daemon)
    schedule = await client.ok(
        "job.schedule",
        {"spec": "every 10m", "task": "summarise the repo", "workdir": str(tmp_path)},
    )
    await client.stop()

    home = daemon.paths.home
    args = argparse.Namespace(subcommand="job", action="list", sub_json=True)
    assert await cli_commands.dispatch(args, home) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [job["jobId"] for job in payload] == [schedule["jobId"]]
    assert payload[0]["spec"] == "every 10m"


async def test_cli_job_schedule_and_cancel(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = daemon.paths.home
    args = argparse.Namespace(
        subcommand="job",
        action="schedule",
        spec="60s",
        task="summarise the repo",
        mode="accept",
        channel="log",
        workdir=str(tmp_path),
        sub_json=True,
    )
    assert await cli_commands.dispatch(args, home) == 0
    created = json.loads(capsys.readouterr().out)
    job_id = created["jobId"]
    assert created["nextRunAt"]

    args = argparse.Namespace(subcommand="job", action="cancel", job_id=job_id)
    assert await cli_commands.dispatch(args, home) == 0
    assert f"cancelled {job_id}" in capsys.readouterr().out

    assert daemon.core is not None
    job = await services(daemon.core).get(job_id)
    assert job is not None
    assert job.enabled is False
    assert job.state == "cancelled"
    assert daemon.core.lifecycle.counters["jobs"] == 0


async def test_cli_daemon_status_prints_the_keepalive_reason(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = daemon.paths.home
    args = argparse.Namespace(
        subcommand="job",
        action="schedule",
        spec="0 9 * * *",
        task="summarise the repo",
        mode="accept",
        channel="log",
        workdir=str(tmp_path),
        sub_json=False,
    )
    assert await cli_commands.dispatch(args, home) == 0
    capsys.readouterr()

    args = argparse.Namespace(subcommand="daemon", action="status", sub_json=False)
    assert await cli_commands.dispatch(args, home) == 0
    out = capsys.readouterr().out
    assert "jobs         1" in out
    assert "will not exit: 1 job" in out


# ---------------------------------------------------------------------------
# (9) the schedule_create tool asks before registering an auto-mode job
# ---------------------------------------------------------------------------


async def test_schedule_tools_replace_the_m5_stubs(
    daemon: Daemon, http: aiohttp.ClientSession
) -> None:
    client = await connect(http, daemon)
    tools = {tool["name"]: tool for tool in (await client.ok("tool.list", {}))["tools"]}
    for name in ("schedule_create", "schedule_list", "schedule_cancel"):
        assert tools[name]["state"] == "active"
        assert tools[name]["permissionTag"] == "send"
    await client.stop()


async def test_model_registering_an_auto_job_needs_approval(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """Even in an auto session, a job that will itself run in auto mode asks."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    client = await connect(http, daemon)
    client.approval_mode = "deny"
    session = await client.ok("session.create", {"workdir": str(workdir), "mode": "auto"})

    turn = await client.ok(
        "session.prompt",
        {"sessionId": session["sessionId"], "text": "schedule a daily summary"},
    )
    await client.wait(
        lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn["turnId"]
    )
    assert [request["tool"] for request in client.approval_requests] == ["schedule_create"]
    assert (await client.ok("job.list", {}))["jobs"] == []

    client.approval_mode = "allow"
    turn = await client.ok(
        "session.prompt",
        {"sessionId": session["sessionId"], "text": "schedule a daily summary"},
    )
    await client.wait(
        lambda e: e["kind"] == "turn.done" and e["payload"]["turnId"] == turn["turnId"]
    )
    jobs = (await client.ok("job.list", {}))["jobs"]
    assert [job["spec"] for job in jobs] == ["매일 09:00"]
    assert jobs[0]["mode"] == "auto"
    await client.stop()
