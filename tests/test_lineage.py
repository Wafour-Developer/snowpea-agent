"""session.lineage (M17 2e, protocol 1.48.0): breadcrumbs and the delegation tree."""

from __future__ import annotations

from typing import Any

import pytest
from _support import make_daemon

from snowpea_core.server.errors import RpcError
from snowpea_core.server.lineage_handlers import session_lineage_handler
from snowpea_core.server.protocol import SessionLineageParams


async def test_ancestors_and_descendants(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        lead = await core.sessions.create(tmp_path, mode="auto")
        child = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=lead.id, kind="subagent"
        )
        grandchild = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=child.id, kind="subagent"
        )
        other = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=lead.id, kind="subagent"
        )
        top = await session_lineage_handler(None, SessionLineageParams(sessionId=lead.id), core)
        assert top.ancestors == []
        assert [n.sessionId for n in top.descendants] == [child.id, other.id]
        assert [n.sessionId for n in top.descendants[0].children] == [grandchild.id]
        deep = await session_lineage_handler(
            None, SessionLineageParams(sessionId=grandchild.id), core
        )
        assert [n.sessionId for n in deep.ancestors] == [lead.id, child.id]
        assert deep.descendants == []
        with pytest.raises(RpcError):
            await session_lineage_handler(None, SessionLineageParams(sessionId="s-x"), core)
    finally:
        await daemon.stop()
