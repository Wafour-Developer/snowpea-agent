"""CORE-gateway-chat: chat commands, typing and the progress line.

A messenger is a surface in its own right, so the things a terminal answers
with its own window — which conversation am I in, what else is open, put me in
another one, stop — are answered by ``/sessions``, ``/resume``, ``/new``,
``/projects``, ``/status`` and ``/stop``.  While a turn runs the chat shows a
typing hint and one progress message that is edited rather than repeated.

Same shape as ``test_gateway.py``: a real in-process daemon with the scripted
provider and ``SNOWPEA_GATEWAY_FAKE=1``, so a "platform" is an in-memory queue.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import httpx
import pytest
import pytest_asyncio
from _support import PROVIDER_FIXTURES, connect, env_vars

from snowpea_core.config.settings import Settings
from snowpea_core.gateway.activity import elapsed_text, tool_label
from snowpea_core.gateway.base import (
    parse_project_callback,
    parse_session_callback,
    project_callback,
    session_callback,
)
from snowpea_core.gateway.chat import MENU_COMMANDS, NOT_YOUR_CHAT, ChatSessionMemory, read_projects
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.gateway.telegram import TelegramAdapter
from snowpea_core.server.app_server import Daemon

TIMEOUT = 10.0
FIXTURE = PROVIDER_FIXTURES / "gateway_chat.json"


@pytest_asyncio.fixture
async def chat_env() -> AsyncIterator[None]:
    """The chat-command provider script plus the in-process fake adapter."""
    with env_vars(SNOWPEA_PROVIDER=f"fake:{FIXTURE}", SNOWPEA_GATEWAY_FAKE="1"):
        FakeAdapter.instances.clear()
        try:
            yield None
        finally:
            FakeAdapter.instances.clear()


def write_settings(home: Path, data: dict[str, Any]) -> None:
    """Seed ``$SNOWPEA_HOME/settings.json`` with a raw document.

    Written as JSON rather than through :class:`Settings` because ``ide`` is
    the IDE's own block and the core model does not declare it.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(data), encoding="utf-8")


async def bind(
    client: Any, workdir: Path, *, mode: str = "auto", user_id: str | None = "u1"
) -> FakeAdapter:
    """Bind one telegram chat to a fresh session in ``workdir``."""
    params: dict[str, Any] = {
        "platform": "telegram",
        "credentialsRef": "tg_test",
        "target": {"new_session": {"workdir": str(workdir), "mode": mode}},
        "channelId": "c1",
    }
    if user_id:
        params["userId"] = user_id
    await client.ok("gateway.bind", params)
    return FakeAdapter.instances["tg_test"]


async def say_msg(adapter: FakeAdapter, text: str, *, user_id: str = "u1") -> Any:
    """Send one message and return the outbound message it produced."""
    before = len(adapter.sent)
    await adapter.push(text, channel_id="c1", user_id=user_id)
    return await adapter.wait_for_send(TIMEOUT, count=before + 1)


async def say(adapter: FakeAdapter, text: str, *, user_id: str = "u1") -> str:
    """Send one message and return the text of the reply it produced."""
    return (await say_msg(adapter, text, user_id=user_id)).text


# ---------------------------------------------------------------------------
# (a) /sessions, /resume
# ---------------------------------------------------------------------------


async def test_sessions_lists_with_a_star_and_buttons(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir)
        await say(adapter, "첫 번째 질문")

        current = (await client.ok("session.list", {}))["sessions"][0]["sessionId"]
        listed = await say_msg(adapter, "/sessions")

        assert listed.text.startswith("★ 1. ")
        assert current[:8] in listed.text
        assert "project" in listed.text  # the workdir basename
        assert "첫 번째 질문" in listed.text
        assert [parse_session_callback(data) for data in listed.button_data()] == [current]
        await client.stop()
    finally:
        await daemon.stop()


async def test_resume_switches_the_chat_by_number_and_by_prefix(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, first)
        await say(adapter, "in the first one")

        opened = await say(adapter, f"/new {second}")
        assert str(second) in opened
        new_id = opened.split()[1]

        # A prompt now lands in the session /new opened, not the original.
        await say(adapter, "in the second one")
        rows = {s["sessionId"]: s for s in (await client.ok("session.list", {}))["sessions"]}
        assert rows[new_id]["lastPrompt"] == "in the second one"
        old_id = next(sid for sid in rows if sid != new_id)
        assert rows[old_id]["lastPrompt"] == "in the first one"

        # Back by id prefix, then a prompt proves the switch took.
        back = await say(adapter, f"/resume {old_id[:5]}")
        assert back.startswith(f"Now in {old_id}")
        await say(adapter, "back in the first")
        rows = {s["sessionId"]: s for s in (await client.ok("session.list", {}))["sessions"]}
        assert rows[old_id]["lastPrompt"] == "back in the first"

        # And by the row number of the list the chat just saw.
        listed = await say_msg(adapter, "/sessions")
        wanted = [line for line in listed.text.splitlines() if new_id[:8] in line][0]
        number = wanted.split(".")[0].replace("★", "").strip()
        assert (await say(adapter, f"/resume {number}")).startswith(f"Now in {new_id}")
        await client.stop()
    finally:
        await daemon.stop()


async def test_a_bare_number_after_the_list_picks_that_row(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir)
        await say(adapter, "open one")
        await say_msg(adapter, "/sessions")

        assert (await say(adapter, "1")).startswith("Now in s-")
        # The pick is spent: the next number is an ordinary prompt again.
        assert await say(adapter, "1") == "fake chat reply"
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (b) /new and /projects
# ---------------------------------------------------------------------------


async def test_new_opens_a_session_in_a_path_and_in_a_named_project(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    other = tmp_path / "api"
    workdir.mkdir()
    other.mkdir()
    write_settings(home, {"ide": {"projects": [{"workdir": str(other), "pinned": True}]}})
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir)

        opened = await say(adapter, f"/new {workdir}")
        assert opened.startswith("New s-") and str(workdir) in opened

        # By the project's name, from ide.projects.
        by_name = await say(adapter, "/new api")
        assert str(other) in by_name

        assert "no such path or project" in await say(adapter, "/new nowhere-at-all")
        workdirs = {s["workdir"] for s in (await client.ok("session.list", {}))["sessions"]}
        assert workdirs == {str(workdir), str(other)}
        await client.stop()
    finally:
        await daemon.stop()


async def test_projects_merges_settings_and_recent_workdirs_pinned_first(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    recent = tmp_path / "recent"
    old = tmp_path / "old"
    pinned = tmp_path / "pinned"
    for path in (recent, old, pinned):
        path.mkdir()
    write_settings(
        home,
        {
            "ide": {
                "projects": [
                    {"workdir": str(old), "lastOpenedAt": "2026-01-01T00:00:00Z"},
                    {"workdir": str(pinned), "pinned": True},
                ]
            }
        },
    )
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, recent)
        await say(adapter, "open a session so the workdir is remembered")

        listed = await say_msg(adapter, "/projects")
        lines = listed.text.splitlines()
        assert lines[0].startswith("1. pinned")
        assert lines[1].startswith("2. old")
        assert any(line.startswith("3. recent") for line in lines)
        assert [parse_project_callback(data) for data in listed.button_data()] == [1, 2, 3]

        # Pressing a row starts a session there.
        before = len(adapter.sent)
        await adapter.press(project_callback(1), channel_id="c1", user_id="u1")
        opened = await adapter.wait_for_send(TIMEOUT, count=before + 1)
        assert str(pinned) in opened.text
        await client.stop()
    finally:
        await daemon.stop()


def test_read_projects_is_permissive_about_the_ide_block() -> None:
    settings = Settings()
    settings.ide = {  # type: ignore[attr-defined]
        "projects": [
            "~/plain-string",
            {"workdir": "/a", "lastOpenedAt": "2026-02-02"},
            {"workdir": "/b", "pinned": True},
            {"nothing": "useful"},
            17,
        ]
    }
    projects = read_projects(settings, ["/a", "/c"])
    assert [project.path for project in projects] == [
        "/b",
        "/a",
        str(Path("~/plain-string").expanduser()),
        "/c",
    ]
    assert read_projects(Settings(), ["/only"])[0].name == "only"


# ---------------------------------------------------------------------------
# (c) /status, /stop, /help
# ---------------------------------------------------------------------------


async def test_status_names_the_session_workdir_and_mode(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir, mode="accept")
        await say(adapter, "open the session")
        session_id = (await client.ok("session.list", {}))["sessions"][0]["sessionId"]
        await wait_running(client, False)

        status = await say(adapter, "/status")
        assert session_id in status
        assert str(workdir) in status
        assert "mode accept" in status
        assert "idle" in status
        await client.stop()
    finally:
        await daemon.stop()


async def test_stop_reports_an_idle_session_and_interrupts_a_running_turn(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir)
        await say(adapter, "open the session")
        await wait_running(client, False)
        assert await say(adapter, "/stop") == "nothing running"

        # A turn that will sit in the provider for half a minute.
        await adapter.push("wait forever please", channel_id="c1", user_id="u1")
        await wait_running(client, True)
        assert await say(adapter, "/stop") == "stopped"
        await client.stop()
    finally:
        await daemon.stop()


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.05)


async def wait_running(client: Any, wanted: bool) -> None:
    """Block until the daemon's only session is (not) running a turn."""
    for _ in range(200):
        rows = (await client.ok("session.list", {}))["sessions"]
        if rows and bool(rows[0].get("running")) is wanted:
            return
        await _tick()
    raise AssertionError(f"the session never reported running={wanted}")


async def test_help_lists_the_chat_commands_and_the_session_ones(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir)
        text = await say(adapter, "/help")
        for name in ("/sessions", "/resume", "/new", "/projects", "/status", "/stop"):
            assert name in text
        assert "/mode" in text and "/model" in text
        assert "/setup" not in text and "/login" not in text
        # One message, and inside Telegram's limit.
        assert len(text) <= 3500
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (d) gating and persistence
# ---------------------------------------------------------------------------


async def test_only_the_bound_user_may_change_the_chats_session(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir, user_id="u1")
        await say(adapter, "open the session")

        assert await say(adapter, f"/new {workdir}", user_id="stranger") == NOT_YOUR_CHAT
        assert await say(adapter, "/stop", user_id="stranger") == NOT_YOUR_CHAT
        # Reading is open to everyone in the conversation.
        assert "mode" in await say(adapter, "/status", user_id="stranger")
        assert (await client.ok("session.list", {}))["sessions"].__len__() == 1
        await client.stop()
    finally:
        await daemon.stop()


async def test_the_chats_session_choice_survives_a_restart(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, first_dir)
        await say(adapter, "in the first one")
        opened = await say(adapter, f"/new {second_dir}")
        chosen = opened.split()[1]
        await client.stop()
    finally:
        await daemon.stop()

    stored = json.loads((home / "gateway-chats.json").read_text(encoding="utf-8"))
    assert list(stored.values()) == [chosen]
    assert list(stored)[0].endswith("|c1")

    second = Daemon(port=0, home=home)
    await second.start()
    try:
        client = await connect(http, second)
        adapter = FakeAdapter.instances["tg_test"]
        await say(adapter, "after the restart")
        rows = {s["sessionId"]: s for s in (await client.ok("session.list", {}))["sessions"]}
        assert rows[chosen]["lastPrompt"] == "after the restart"
        await client.stop()
    finally:
        await second.stop()


def test_chat_session_memory_survives_a_broken_file(tmp_path: Path) -> None:
    memory = ChatSessionMemory(tmp_path / "gateway-chats.json")
    assert memory.get("gw-1", "c1") is None
    memory.remember("gw-1", "c1", "s-1")
    assert memory.get("gw-1", "c1") == "s-1"
    memory.forget("gw-1", "c1")
    assert memory.get("gw-1", "c1") is None
    (tmp_path / "gateway-chats.json").write_text("not json at all", encoding="utf-8")
    assert memory.get("gw-1", "c1") is None


# ---------------------------------------------------------------------------
# (e) typing and the progress message
# ---------------------------------------------------------------------------


async def test_typing_runs_while_a_turn_does_and_stops_at_the_end(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir)
        assert await say(adapter, "answer me slowly") == "took my time"

        assert adapter.typing_calls, "the chat should have been shown a typing hint"
        assert set(adapter.typing_calls) == {"c1"}
        # Nothing keeps typing once the turn is done.
        seen = len(adapter.typing_calls)
        await _tick()
        await _tick()
        assert len(adapter.typing_calls) == seen
        await client.stop()
    finally:
        await daemon.stop()


async def test_progress_is_one_message_edited_in_place(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir, mode="auto")
        await adapter.push("please use tools now", channel_id="c1", user_id="u1")
        # The progress message goes out on the first tool call, the answer
        # after the second: two messages for a two-tool turn, not three.
        await adapter.wait_for_send(TIMEOUT, count=2)
        await wait_running(client, False)

        assert [m.text for m in adapter.sent][-1] == "ran both"
        edited = {edit[1] for edit in adapter.edits}
        assert len(edited) == 1, "two tool calls must not cost two messages"
        assert len(adapter.sent) == 2
        assert adapter.sent[0].message_id in edited
        assert adapter.edits[-1][2].startswith("✓ 2 tool calls · ")
        await client.stop()
    finally:
        await daemon.stop()


async def test_progress_and_typing_can_both_be_turned_off(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    write_settings(home, {"gateway": {"typing": False, "progress": False}})
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, workdir, mode="auto")
        await say(adapter, "please use tools now")

        assert adapter.typing_calls == []
        assert adapter.edits == []
        assert not [m for m in adapter.sent if m.text.startswith("⏳")]
        await client.stop()
    finally:
        await daemon.stop()


def test_tool_labels_and_elapsed_are_short() -> None:
    assert tool_label("shell", {"command": "echo  hi"}) == "shell echo hi"
    assert tool_label("read_file", {"path": "/tmp/a.txt"}) == "read_file /tmp/a.txt"
    assert tool_label("think", {}) == "think"
    assert tool_label("shell", {"command": "x" * 200}).endswith("…")
    assert elapsed_text(9.4) == "9s"
    assert elapsed_text(187) == "3m 07s"


# ---------------------------------------------------------------------------
# (f) the Telegram wire calls
# ---------------------------------------------------------------------------


def _telegram(seen: list[httpx.Request]) -> TelegramAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramAdapter("123:ABC", client=client)


async def test_telegram_publishes_its_command_menu_on_start() -> None:
    seen: list[httpx.Request] = []
    adapter = _telegram(seen)
    await adapter.register_commands()
    await adapter.stop()

    assert seen[0].url.path == "/bot123:ABC/setMyCommands"
    commands = json.loads(seen[0].content)["commands"]
    assert [entry["command"] for entry in commands] == [name for name, _ in MENU_COMMANDS]
    assert all(0 < len(entry["description"]) <= 256 for entry in commands)


async def test_telegram_typing_and_edit_payload_shapes() -> None:
    seen: list[httpx.Request] = []
    adapter = _telegram(seen)
    await adapter.typing("555")
    await adapter.edit("555", "77", "⏳ shell echo hi")
    await adapter.stop()

    assert seen[0].url.path == "/bot123:ABC/sendChatAction"
    assert json.loads(seen[0].content) == {"chat_id": "555", "action": "typing"}
    assert seen[1].url.path == "/bot123:ABC/editMessageText"
    assert json.loads(seen[1].content) == {
        "chat_id": "555",
        "message_id": "77",
        "text": "⏳ shell echo hi",
    }


async def test_a_failed_command_menu_never_carries_the_bot_token() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401, json={"ok": False}))
    adapter = TelegramAdapter("123:SECRET", client=httpx.AsyncClient(transport=transport))
    # Never raises: a bot whose menu could not be published still works.
    await adapter.register_commands()
    await adapter.stop()


def test_session_and_project_callbacks_round_trip() -> None:
    assert session_callback("s-7c68c2") == "ses:s-7c68c2"
    assert parse_session_callback("ses:s-7c68c2") == "s-7c68c2"
    assert parse_session_callback("ses:") is None
    assert parse_session_callback("apr:ap-1:allow") is None
    assert parse_session_callback(None) is None
    assert project_callback(3) == "prj:3"
    assert parse_project_callback("prj:3") == 3
    assert parse_project_callback("prj:x") is None
    assert parse_project_callback(None) is None


@pytest.mark.parametrize("command", [name for name, _ in MENU_COMMANDS])
def test_every_menu_command_has_a_description(command: str) -> None:
    description = dict(MENU_COMMANDS)[command]
    assert description and len(description) <= 256
