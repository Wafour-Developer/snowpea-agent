"""Checkpoint RPC integration through a real daemon and scripted tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from _support import PROVIDER_FIXTURES, connect, fake_provider, make_daemon

FIXTURE = PROVIDER_FIXTURES / "checkpoints.json"


async def _create(client: object, workdir: Path) -> str:
    result = await client.ok(  # type: ignore[attr-defined]
        "session.create", {"workdir": str(workdir), "mode": "accept"}
    )
    return str(result["sessionId"])


async def test_checkpoint_rpc_turn_diff_restore_and_event(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("original\n", encoding="utf-8")
    (workdir / "b.txt").write_text("before\n", encoding="utf-8")
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home", {"tools": {"readBeforeWrite": False}})
        try:
            client = await connect(http, daemon)
            session_id = await _create(client, workdir)
            turn = await client.ok(
                "session.prompt",
                {"sessionId": session_id, "text": "make checkpoint changes"},
            )
            assert await client.wait_turn(str(turn["turnId"])) == "complete"

            listed = await client.ok("checkpoint.list", {"sessionId": session_id})
            assert len(listed["checkpoints"]) == 1
            checkpoint = listed["checkpoints"][0]
            assert {row["path"] for row in checkpoint["files"]} == {"a.txt", "b.txt"}
            assert {row["path"]: row["status"] for row in checkpoint["files"]} == {
                "a.txt": "modified",
                "b.txt": "modified",
            }

            diff = await client.ok(
                "checkpoint.diff",
                {"sessionId": session_id, "id": checkpoint["id"]},
            )
            a_patch = next(row["patch"] for row in diff["files"] if row["path"] == "a.txt")
            assert "-after" in a_patch and "+original" in a_patch

            restored = await client.ok(
                "checkpoint.restore",
                {"sessionId": session_id, "id": checkpoint["id"]},
            )
            assert set(restored["restored"]) == {"a.txt", "b.txt"}
            assert (workdir / "a.txt").read_text(encoding="utf-8") == "original\n"
            event = await client.wait(lambda row: row["kind"] == "checkpoint.restored")
            assert event["payload"]["checkpointId"] == restored["checkpointId"]
            listed = await client.ok("checkpoint.list", {"sessionId": session_id})
            assert listed["checkpoints"][0]["kind"] == "restore"
            await client.stop()
        finally:
            await daemon.stop()


async def test_checkpoint_restore_refuses_while_turn_runs(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "a.txt").write_text("before\n", encoding="utf-8")
    with fake_provider(FIXTURE):
        daemon = await make_daemon(tmp_path / "home")
        try:
            client = await connect(http, daemon)
            session_id = await _create(client, workdir)
            manifest, _ = await daemon.core.checkpoints.before_write(
                daemon.core.sessions.get(session_id), "seed", "a.txt"
            )
            assert manifest is not None
            (workdir / "a.txt").write_text("after\n", encoding="utf-8")
            await daemon.core.checkpoints.note_after(
                daemon.core.sessions.get(session_id), "seed", "a.txt"
            )
            await daemon.core.checkpoints.finalize_turn(
                daemon.core.sessions.get(session_id), "seed"
            )
            turn = await client.ok(
                "session.prompt", {"sessionId": session_id, "text": "slow checkpoint"}
            )
            await asyncio.sleep(0.05)
            response = await client.call(
                "checkpoint.restore", {"sessionId": session_id, "id": "seed"}
            )
            assert response["error"]["data"]["code"] == "session_busy"
            await client.wait_turn(str(turn["turnId"]))
            await client.stop()
        finally:
            await daemon.stop()


async def test_disabled_checkpoints_record_nothing(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    with fake_provider(FIXTURE):
        daemon = await make_daemon(
            tmp_path / "home",
            {"checkpoints": {"enabled": False}, "tools": {"readBeforeWrite": False}},
        )
        try:
            client = await connect(http, daemon)
            session_id = await _create(client, workdir)
            turn = await client.ok(
                "session.prompt",
                {"sessionId": session_id, "text": "make checkpoint changes"},
            )
            await client.wait_turn(str(turn["turnId"]))
            listed = await client.ok("checkpoint.list", {"sessionId": session_id})
            assert listed == {"checkpoints": []}
            await client.stop()
        finally:
            await daemon.stop()
