"""`/busy` command: show and set the busy-turn follow-up policy."""

from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import pytest
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.config.paths import Paths

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "session.json"
TIMEOUT = 10.0


async def _start_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


async def _prompt(client: RpcClient, session_id: str, text: str) -> str:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    return str(result["turnId"])


async def test_busy_command_shows_sets_and_persists(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()

    with fake_provider(FIXTURE):
        daemon = await make_daemon(home)
        try:
            client = await connect(http, daemon, timeout=TIMEOUT)
            session_id = await _start_session(client, workdir)

            show = await _prompt(client, session_id, "/busy")
            assert await client.wait_turn(show, timeout=TIMEOUT) == "complete"
            assert client.of_kind("message.done")[-1]["payload"]["text"].startswith("Busy: steer")

            set_queue = await _prompt(client, session_id, "/busy queue")
            assert await client.wait_turn(set_queue, timeout=TIMEOUT) == "complete"
            assert client.of_kind("message.done")[-1]["payload"]["text"] == "Busy: queue"

            shown_again = await _prompt(client, session_id, "/busy")
            assert await client.wait_turn(shown_again, timeout=TIMEOUT) == "complete"
            assert client.of_kind("message.done")[-1]["payload"]["text"].startswith("Busy: queue")

            invalid = await _prompt(client, session_id, "/busy nope")
            assert await client.wait_turn(invalid, timeout=TIMEOUT) == "complete"
            assert "Unknown busy policy" in client.of_kind("message.done")[-1]["payload"]["text"]

            settings = await client.ok("settings.get", {"scope": "global"})
            assert settings["settings"]["agent"]["busy"] == "queue"

            await client.stop()
        finally:
            await daemon.stop()

    # Persisted to the same daemon settings file path as other settings writes.
    saved = json.loads(Paths.create(home).settings_json.read_text(encoding="utf-8"))
    assert saved["agent"]["busy"] == "queue"
