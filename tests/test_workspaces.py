"""Workspaces: the folders the sidebar groups conversations by (M17 2d)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _support import make_daemon

from snowpea_core.session import workspaces as ws


async def test_the_registry_is_filled_from_session_folders(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        a, b = tmp_path / "alpha", tmp_path / "beta"
        a.mkdir()
        b.mkdir()
        await core.sessions.create(a, mode="auto")
        await core.sessions.create(b, mode="auto")
        await core.sessions.create(b, mode="auto")
        items = await ws.list_all(core.store)
        by_path = {item["path"]: item for item in items}
        assert by_path[ws.canonical(b)]["sessions"] == 2
        assert by_path[ws.canonical(a)]["title"] == "alpha"
    finally:
        await daemon.stop()


async def test_add_rename_reorder_remove(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        store = daemon.core.store
        one = await ws.add(store, str(tmp_path / "one"))
        two = await ws.add(store, str(tmp_path / "two"), "Second")
        assert (await ws.add(store, str(tmp_path / "one")))["id"] == one["id"]  # same folder
        assert [i["id"] for i in await ws.list_all(store)][:2] == [two["id"], one["id"]]
        await ws.reorder(store, [one["id"]])
        assert [i["id"] for i in await ws.list_all(store)][:2] == [one["id"], two["id"]]
        renamed = await ws.rename(store, one["id"], "  Main   app ")
        assert renamed is not None and renamed["title"] == "Main app"
        with pytest.raises(ValueError):
            await ws.rename(store, one["id"], "  ")
        assert await ws.remove(store, two["id"]) is True
        assert await ws.get(store, two["id"]) is None
    finally:
        await daemon.stop()


async def test_a_symlinked_folder_is_the_same_workspace(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        first = await ws.add(daemon.core.store, str(real))
        second = await ws.add(daemon.core.store, str(link))
        assert first["id"] == second["id"] and first["path"] == str(Path(real).resolve())
    finally:
        await daemon.stop()


async def test_pin_and_archive_flags(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        session = await core.sessions.create(tmp_path, mode="auto")
        await ws.set_flag(core.store, session.id, "pinned", True)
        await ws.set_flag(core.store, session.id, "archived", True)
        flags = await ws.session_flags(core.store)
        assert flags[session.id] == {"pinned": True, "archived": True}
        counts = await ws.session_counts(core.store)
        assert counts.get(ws.canonical(tmp_path), 0) == 0  # archived sessions are not counted
    finally:
        await daemon.stop()


async def test_session_list_rows_carry_workspace_and_flags(tmp_path: Any) -> None:
    from snowpea_core.server.protocol import Empty, SessionFlagParams, SessionListParams
    from snowpea_core.server.session_handlers import session_list_handler
    from snowpea_core.server.workspace_handlers import (
        session_pin_handler,
        workspace_list_handler,
    )

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        folder = tmp_path / "app"
        folder.mkdir()
        session = await core.sessions.create(folder, mode="auto")
        await session_pin_handler(None, SessionFlagParams(sessionId=session.id, value=True), core)
        listing = await workspace_list_handler(None, Empty(), core)
        workspace = next(w for w in listing.workspaces if w.path == ws.canonical(folder))
        result = await session_list_handler(None, SessionListParams(), core)
        row = next(r for r in result.sessions if r.sessionId == session.id)
        assert row.workspaceId == workspace.id and row.pinned is True and row.archived is False
    finally:
        await daemon.stop()


async def test_archiving_a_running_conversation_needs_stop(tmp_path: Any) -> None:
    import asyncio

    from snowpea_core.server.errors import RpcError
    from snowpea_core.server.protocol import SessionFlagParams
    from snowpea_core.server.workspace_handlers import session_archive_handler

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        session = await core.sessions.create(tmp_path, mode="auto")
        session.turn_task = asyncio.ensure_future(asyncio.sleep(30))
        with pytest.raises(RpcError):
            await session_archive_handler(
                None, SessionFlagParams(sessionId=session.id, value=True), core
            )
        await session_archive_handler(
            None, SessionFlagParams(sessionId=session.id, value=True, stop=True), core
        )
        assert session.interrupt.is_set()
        assert (await ws.session_flags(core.store))[session.id]["archived"] is True
        session.turn_task.cancel()
    finally:
        await daemon.stop()
