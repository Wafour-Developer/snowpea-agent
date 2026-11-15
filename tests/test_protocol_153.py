"""Protocol 1.53.0 (M19 Jarvis mode, WP1): the wire contract and its small pieces.

The jarvis.*, external.* and mcp.preset.* contract is declared but answers
``not_implemented`` and stays out of ``system.hello`` until its work package
lands; ``message.user`` carries an origin; ``lastPrompt`` and ``lastUserAt``
only follow the person's own prompts; settings gain the ``jarvis`` block.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _support import Recorder, make_daemon
from pydantic import ValidationError

from snowpea_core.agent import loop as agent_loop
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.providers.base import ChatMessage, StreamEvent, Usage
from snowpea_core.server import protocol
from snowpea_core.server.errors import ERROR_CODES
from snowpea_core.server.protocol import (
    ApprovalRequest,
    MessageUser,
    SessionListParams,
    SessionSummary,
    SessionTrajectoryParams,
)
from snowpea_core.server.session_handlers import session_list_handler
from snowpea_core.server.trajectory_handlers import session_trajectory_handler
from snowpea_core.session.history import message_text

M19_METHODS = {
    "jarvis.status",
    "jarvis.enable",
    "jarvis.disable",
    "jarvis.overview",
    "jarvis.ledger",
    "jarvis.delegation.list",
    "jarvis.delegation.set",
    "jarvis.delegation.revoke",
    "jarvis.takeover",
    "jarvis.handback",
    "jarvis.briefing.get",
    "jarvis.briefing.set",
    "jarvis.project.list",
    "jarvis.project.rename",
    "jarvis.project.alias",
    "external.clis",
    "external.list",
    "external.open",
    "external.read",
    "external.send",
    "external.interrupt",
    "external.setMode",
    "external.create",
    "external.close",
    "external.get",
    "external.unwatch",
    "mcp.preset.list",
    "mcp.preset.apply",
}
WP2_METHODS = {
    "jarvis.ledger",
    "jarvis.delegation.list",
    "jarvis.delegation.set",
    "jarvis.delegation.revoke",
    "jarvis.takeover",
    "jarvis.handback",
}
WP3_METHODS = {
    "jarvis.status",
    "jarvis.enable",
    "jarvis.disable",
    "jarvis.overview",
    "jarvis.project.list",
    "jarvis.project.rename",
    "jarvis.project.alias",
}
M19_EVENTS = {
    "jarvis.state",
    "jarvis.decision",
    "jarvis.escalation",
    "jarvis.delegation.changed",
    "external.event",
    "external.state",
    "external.clis.changed",
}


class _Echo:
    vendor = "fake"
    model = "fake-1"

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        last = (message_text(messages[-1]).splitlines() or [""])[0]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=10, output_tokens=2))
        yield StreamEvent(kind="text_delta", text=f"echo {last}")
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _turn(core: Any, session: Any, text: str, **kwargs: Any) -> str:
    turn_id = agent_loop.start_turn(core, session, text, **kwargs)
    await asyncio.wait_for(session.turn_task, timeout=60)
    return turn_id


async def _row(core: Any, session_id: str, **params: Any) -> SessionSummary:
    result = await session_list_handler(None, SessionListParams(**params), core)
    return next(r for r in result.sessions if r.sessionId == session_id)


# -- the contract ----------------------------------------------------------


def test_version_and_declared_contract() -> None:
    assert protocol.PROTOCOL_VERSION == "1.53.0"
    assert M19_METHODS <= set(protocol.METHODS)
    # Declared only until their work package lands: the dispatcher's
    # not_implemented stub answers the rest; the external.* family is
    # answered by the common layer (WP5).
    external = {name for name in M19_METHODS if name.startswith("external.")}
    assert external <= protocol.IMPLEMENTED_METHODS
    assert M19_METHODS & protocol.IMPLEMENTED_METHODS == WP2_METHODS | WP3_METHODS | external
    assert M19_EVENTS <= set(protocol.EVENTS)
    assert {"jarvis_disabled", "jarvis_person_only", "external_view_only"} <= set(ERROR_CODES)
    assert "external_unavailable" in ERROR_CODES


def test_pending_capabilities_are_not_advertised() -> None:
    assert set(protocol.PENDING_CAPABILITIES) == {
        "mcpPresets",
        "jarvisBriefing",
    }
    assert not (set(protocol.PENDING_CAPABILITIES) & set(protocol.CAPABILITIES))
    # Implemented in WP1, WP2 and WP3, so advertised now.
    assert "messageOrigin" in protocol.CAPABILITIES
    assert "jarvisDelegation" in protocol.CAPABILITIES
    assert "jarvis" in protocol.CAPABILITIES
    assert "externalSessions" in protocol.CAPABILITIES  # WP5
    assert protocol.dump_schema()["capabilities"] == list(protocol.CAPABILITIES)


def test_additive_fields_default_to_absent() -> None:
    assert MessageUser(text="hi").origin is None
    row = SessionSummary(sessionId="s", workdir="/w", mode="auto", createdAt="t")
    assert row.role is None and row.jarvis is None
    request = ApprovalRequest(requestId="r", sessionId="s", tool="shell")
    assert request.external is None and request.route is None
    with pytest.raises(ValidationError):
        MessageUser(text="hi", origin={"kind": "robot"})


def test_external_rows_say_where_the_process_lives() -> None:
    row = protocol.ExternalSessionRow(
        ref={"cli": "codex", "id": "t-1"},
        cwd="/w",
        status="running",
        host="tmux",
        canSend=False,
        approvals="person",
        tmux={"session": "work", "pane": "%3"},
    )
    assert row.host == "tmux" and row.tmux is not None and row.tmux.session == "work"
    with pytest.raises(ValidationError):
        protocol.ExternalSessionRow(
            ref={"cli": "codex", "id": "t"}, cwd="/", status="open_elsewhere"
        )
    assert row.mode is None and row.controlled is True
    create = protocol.ExternalCreateParams(cli="claude", project="/w")
    assert create.transport == "structured" and create.mode is None
    yolo = protocol.ExternalSetModeParams(ref={"cli": "cursor", "id": "c"}, mode="unrestricted")
    assert yolo.mode == "unrestricted"
    with pytest.raises(ValidationError):
        protocol.ExternalSetModeParams(ref={"cli": "cursor", "id": "c"}, mode="yolo")
    with pytest.raises(ValidationError):
        protocol.ExternalRef(cli="gemini", id="x")


def test_jarvis_settings_defaults_and_validation() -> None:
    jarvis = Settings().jarvis
    assert jarvis.enabled is False
    assert jarvis.folder == "~/Snowpea/Jarvis"
    assert jarvis.quietMinutes == 10
    assert jarvis.delegation.default == "staged"
    # The owner's override of the design: grants from a messenger are allowed.
    assert jarvis.messenger.allowGrants is True
    assert jarvis.external.maxProcesses == 4
    assert (jarvis.notify.inApp, jarvis.notify.os, jarvis.notify.messenger) == (True, True, True)
    assert ProjectSettings().jarvis.expose is True
    assert ProjectSettings.model_validate({"jarvis": {"expose": False}}).jarvis.expose is False
    for bad in (
        {"external": {"clis": {"gemini": {}}}},
        {"quietMinutes": 0},
        {"delegation": {"default": "everything"}},
        {"folder": "  "},
        {"external": {"clis": {"agy": {"defaultMode": "readonly"}}}},
        {"external": {"clis": {"codex": {"defaultMode": "yolo"}}}},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"jarvis": bad})
    ok = Settings.model_validate({"jarvis": {"external": {"clis": {"agy": {"path": "/x/agy"}}}}})
    assert ok.jarvis.external.clis["agy"].path == "/x/agy"


# -- message.user origin -----------------------------------------------------


async def test_message_user_carries_whose_words_they_are(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        session = await core.sessions.create(tmp_path, mode="auto")
        recorder = Recorder()
        core.hub.subscribe(recorder, session.id)
        await _turn(core, session, "typed in the ide", surface="ide")
        await _turn(core, session, "scheduled task", origin="schedule")
        await _turn(core, session, "from a group member", origin="member", surface="telegram")
        await _turn(core, session, "/ralplan queued", origin="agent")
        await _turn(core, session, "child finished", origin="agent", origin_kind="subagent")
        await _turn(core, session, "", model_text="[system] (handback) x", initiator="system")
        origins = [e["payload"]["origin"] for e in recorder.of_kind("message.user")]
        assert origins == [
            {"kind": "human", "surface": "ide"},
            {"kind": "schedule", "surface": None},
            {"kind": "gateway", "surface": "telegram"},
            {"kind": "system", "surface": None},
            {"kind": "subagent", "surface": None},
        ]
        trajectory = await session_trajectory_handler(
            None, SessionTrajectoryParams(sessionId=session.id), core
        )
        users = [r for r in trajectory.rows if r.kind == "user"]
        assert [(r.data or {}).get("origin") for r in users] == origins

        child = await core.sessions.create(
            tmp_path, mode="auto", parent_session_id=session.id, kind="subagent"
        )
        child.is_subagent = True
        child_rec = Recorder()
        core.hub.subscribe(child_rec, child.id)
        await _turn(core, child, "the lead's brief")
        assert child_rec.of_kind("message.user")[0]["payload"]["origin"]["kind"] == "subagent"
    finally:
        await daemon.stop()


async def test_last_prompt_is_only_ever_the_persons(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        session = await core.sessions.create(tmp_path, mode="auto")
        await _turn(core, session, "fix the login bug")
        row = await _row(core, session.id)
        assert row.lastPrompt == "fix the login bug"
        typed_at = row.lastUserAt
        assert typed_at
        await asyncio.sleep(0.01)
        await _turn(core, session, "nightly report", origin="schedule")
        await _turn(core, session, "⟲ worker finished", origin="agent", origin_kind="subagent")
        await _turn(core, session, "/next queued by the model", origin="agent")
        await _turn(
            core,
            session,
            "",
            model_text="[system] (handback) keep going",
            initiator="system",
            reason="handback",
        )
        row = await _row(core, session.id)
        assert row.lastPrompt == "fix the login bug"
        assert row.lastUserAt == typed_at
        await core.sessions.close(session.id)
        closed = await _row(core, session.id, includeClosed=True)
        assert closed.lastPrompt == "fix the login bug"
        assert closed.lastUserAt == typed_at
    finally:
        await daemon.stop()


async def test_a_session_with_no_human_prompt_has_none(tmp_path: Any) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        session = await core.sessions.create(tmp_path, mode="auto")
        await _turn(core, session, "only a scheduled task", origin="schedule")
        row = await _row(core, session.id)
        assert row.lastPrompt is None and row.lastUserAt is None
    finally:
        await daemon.stop()


async def test_an_auto_injected_skill_note_is_not_the_last_prompt(tmp_path: Any) -> None:
    from types import SimpleNamespace

    daemon = await make_daemon(tmp_path / "home")
    try:
        core = daemon.core
        core.providers.get = lambda _p, _m: _Echo()
        core.skills.skills["release-notes"] = SimpleNamespace(
            name="release-notes",
            plugin=None,
            doc=SimpleNamespace(
                name="release-notes", auto_inject=["release"], body="Write the notes first."
            ),
        )
        session = await core.sessions.create(tmp_path, mode="auto")
        await _turn(core, session, "prepare the release")
        history = session.history.snapshot()
        notes = [m for m in history if (m.source or {}).get("producer") == "skills"]
        assert notes, "the skill was not auto-injected"
        assert (await _row(core, session.id)).lastPrompt == "prepare the release"
        await _turn(core, session, "", model_text="[system] (handback) more", initiator="system")
        # A session from before message.user existed reads its history, by source.
        await asyncio.to_thread(
            core.store._execute,
            "DELETE FROM events WHERE session_id = ? AND kind = 'message.user'",
            (session.id,),
        )
        assert (await _row(core, session.id)).lastPrompt == "prepare the release"
    finally:
        await daemon.stop()
