"""CORE-gateway-chat: ``/sessions`` by last use, in pages, with search.

A phone list of ten sessions in creation order buried the old conversation
still in use.  The list is now ordered by last activity with the chat's own
session pinned on top, pages through ten at a time with numbers that run
across pages (so ``/resume 13`` works), and ``/sessions <text>`` searches.

Same shape as ``test_gateway_chat.py``: a real in-process daemon with the
scripted provider and the in-memory fake adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest_asyncio
from _support import PROVIDER_FIXTURES, connect, env_vars

from snowpea_core.gateway.base import (
    CALLBACK_DATA_BYTES,
    Button,
    parse_session_callback,
    parse_session_page_callback,
    session_page_callback,
)
from snowpea_core.gateway.chat import (
    MENU_COMMANDS,
    SESSION_ROWS,
    activity_of,
    ago,
    row_matches,
    sessions_args,
)
from snowpea_core.gateway.discord import MENU_DESCRIPTION_CHARS, action_row
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.gateway.slack import action_blocks
from snowpea_core.server.app_server import Daemon

TIMEOUT = 10.0
FIXTURE = PROVIDER_FIXTURES / "gateway_chat.json"


@pytest_asyncio.fixture
async def chat_env() -> AsyncIterator[None]:
    with env_vars(SNOWPEA_PROVIDER=f"fake:{FIXTURE}", SNOWPEA_GATEWAY_FAKE="1"):
        FakeAdapter.instances.clear()
        try:
            yield None
        finally:
            FakeAdapter.instances.clear()


async def bind(client: Any, workdir: Path) -> FakeAdapter:
    await client.ok(
        "gateway.bind",
        {
            "platform": "telegram",
            "credentialsRef": "tg_test",
            "target": {"new_session": {"workdir": str(workdir), "mode": "auto"}},
            "channelId": "c1",
            "userId": "u1",
        },
    )
    return FakeAdapter.instances["tg_test"]


async def say_msg(adapter: FakeAdapter, text: str) -> Any:
    before = len(adapter.sent)
    await adapter.push(text, channel_id="c1", user_id="u1")
    return await adapter.wait_for_send(TIMEOUT, count=before + 1)


async def say(adapter: FakeAdapter, text: str) -> str:
    return (await say_msg(adapter, text)).text


async def press_msg(adapter: FakeAdapter, data: str) -> Any:
    before = len(adapter.sent)
    await adapter.press(data)
    return await adapter.wait_for_send(TIMEOUT, count=before + 1)


async def open_session(adapter: FakeAdapter, workdir: Path, prompt: str) -> str:
    """``/new <workdir>`` then one prompt; the new session's id."""
    workdir.mkdir(exist_ok=True)
    opened = await say(adapter, f"/new {workdir}")
    assert opened.startswith("New s-"), opened
    await say(adapter, prompt)
    return opened.split()[1]


def row_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip("★ ")[:1].isdigit()]


def number_of(text: str, session_id: str) -> int:
    line = next(line for line in row_lines(text) if session_id[:8] in line)
    return int(line.replace("★", "").strip().split(".")[0])


# ---------------------------------------------------------------------------
# order
# ---------------------------------------------------------------------------


async def test_last_use_beats_creation_and_the_current_session_is_pinned(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, tmp_path / "base")
        first = await open_session(adapter, tmp_path / "alpha", "alpha work")
        second = await open_session(adapter, tmp_path / "beta", "beta work")
        # Back to the older one and use it: it is now the more recent.
        assert (await say(adapter, f"/resume {first[:8]}")).startswith(f"Now in {first}")
        await say(adapter, "alpha again")
        third = await open_session(adapter, tmp_path / "gamma", "gamma work")

        listed = await say(adapter, "/sessions")
        lines = row_lines(listed)
        assert lines[0].startswith("★ 1. gamma") and third[:8] in lines[0]
        assert first[:8] in lines[1] and lines[1].startswith("2. alpha")
        assert second[:8] in lines[2] and lines[2].startswith("3. beta")
        assert "방금" in lines[0]

        # The chat's own session stays on top even when it is not the latest.
        await say(adapter, f"/resume {second[:8]}")
        lines = row_lines(await say(adapter, "/sessions"))
        assert lines[0].startswith("★ 1. beta")
        assert [third[:8] in lines[1], first[:8] in lines[2]] == [True, True]

        # session.list for clients keeps its own (creation) order.
        listed_ids = [s["sessionId"] for s in (await client.ok("session.list", {}))["sessions"]]
        assert listed_ids.index(third) < listed_ids.index(second) < listed_ids.index(first)
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# paging and search
# ---------------------------------------------------------------------------


async def test_pages_number_across_pages_and_resume_13_works(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, tmp_path / "base")
        ids = [
            await open_session(adapter, tmp_path / f"proj{n:02d}", f"task number {n}")
            for n in range(13)
        ]

        first = await say_msg(adapter, "/sessions")
        assert len(row_lines(first.text)) == SESSION_ROWS
        assert "— 1/2쪽 · 다음: /sessions 2" in first.text
        # Ten rows plus one "next" button, and nothing before page 1.
        assert len(first.buttons) == SESSION_ROWS + 1
        assert first.buttons[-1].text == "다음 ▶"
        assert parse_session_page_callback(first.buttons[-1].data) == (2, "")

        page2 = await press_msg(adapter, first.buttons[-1].data)
        lines = row_lines(page2.text)
        assert [line.split(".")[0] for line in lines] == ["11", "12", "13"]
        assert ids[0][:8] in lines[-1]  # the least recently used is last
        assert [button.text for button in page2.buttons][-1] == "◀ 이전"
        assert (await say(adapter, "/sessions 2")) == page2.text

        # A bare number after page 2 is that global row; so is /resume 13.
        assert (await say(adapter, "12")).startswith(f"Now in {ids[1]}")
        assert (await say(adapter, "/resume 13")).startswith(f"Now in {ids[0]}")
        assert "no session 14" in await say(adapter, "/resume 14")
        # A page past the end shows the last page.
        assert "2/2쪽" in await say(adapter, "/sessions 9")
        await client.stop()
    finally:
        await daemon.stop()


async def test_search_filters_keeps_global_numbers_and_resume_takes_a_unique_match(
    chat_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = Daemon(port=0, home=tmp_path / "home")
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = await bind(client, tmp_path / "base")
        api = await open_session(adapter, tmp_path / "Billing-API", "fix the login bug")
        web = await open_session(adapter, tmp_path / "webapp", "로그인 화면 수정")
        await open_session(adapter, tmp_path / "docs", "write the readme")

        everything = await say(adapter, "/sessions")
        found = await say_msg(adapter, "/sessions billing")
        assert found.text.splitlines()[0] == '"billing": 3개 중 일치 1개'
        assert len(row_lines(found.text)) == 1
        assert number_of(found.text, api) == number_of(everything, api)
        assert [parse_session_callback(data) for data in found.button_data()] == [api]

        # Prompt text, Korean included, and an id prefix.
        assert web[:8] in await say(adapter, "/sessions 로그인")
        assert "일치 1개" in await say(adapter, f"/sessions {web[:8].upper()}")
        assert "3개 중 일치 0개" in await say(adapter, "/sessions nothing-like-this")

        assert (await say(adapter, "/resume 로그인 화면")).startswith(f"Now in {web}")
        assert "일치 2개" in await say(adapter, "/resume the")
        await client.stop()
    finally:
        await daemon.stop()


async def test_help_explains_paging_and_search() -> None:
    menu = dict(MENU_COMMANDS)
    assert "/sessions 2" in menu["sessions"] and "/resume 13" in menu["resume"]
    assert all(len(text) <= MENU_DESCRIPTION_CHARS for text in menu.values())


# ---------------------------------------------------------------------------
# pure helpers and platform limits
# ---------------------------------------------------------------------------


def test_page_callbacks_fit_in_64_bytes() -> None:
    assert session_page_callback(2) == "sesp:2"
    assert parse_session_page_callback("sesp:12:billing api") == (12, "billing api")
    assert parse_session_page_callback("ses:s-1234") is None
    assert parse_session_callback("sesp:2") is None
    korean = session_page_callback(3, "로그인 화면")
    assert korean is not None and len(korean.encode("utf-8")) <= CALLBACK_DATA_BYTES
    assert parse_session_page_callback(korean) == (3, "로그인 화면")
    # Too long for Telegram: no button rather than a refused message.
    assert session_page_callback(2, "가" * 20) is None
    assert session_page_callback(999, "x" * (CALLBACK_DATA_BYTES - len("sesp:999:"))) is not None


def test_sessions_args() -> None:
    assert sessions_args("") == (1, "")
    assert sessions_args("2") == (2, "")
    assert sessions_args("api") == (1, "api")
    assert sessions_args("billing api 3") == (3, "billing api")


def test_ago_and_activity() -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    assert ago(now - timedelta(seconds=5), now) == "방금"
    assert ago(now - timedelta(minutes=3), now) == "3분 전"
    assert ago(now - timedelta(hours=2), now) == "2시간 전"
    assert ago(now - timedelta(hours=30), now) == "어제"
    assert ago(now - timedelta(days=4), now) == "4일 전"
    assert ago(None, now) == ""
    row = SimpleNamespace(
        createdAt="2026-11-01T00:00:00.000Z",
        lastActivityAt="2026-11-18T09:00:00.000Z",
        lastUserAt="2026-11-18T08:59:00Z",
    )
    assert activity_of(row) == datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
    bare = SimpleNamespace(createdAt="2026-11-01T00:00:00Z", lastActivityAt=None, lastUserAt=None)
    assert activity_of(bare) == datetime(2026, 11, 1, tzinfo=UTC)


def test_row_matches_folder_title_prompt_and_id() -> None:
    row = SimpleNamespace(
        sessionId="s-7c68c2aa", workdir="/src/Billing-API", title="Release prep", lastPrompt="hi"
    )
    assert row_matches(row, "billing") and row_matches(row, "RELEASE")
    assert row_matches(row, "s-7c6") and row_matches(row, "HI")
    assert not row_matches(row, "7c68")  # an id matches by prefix only
    assert not row_matches(row, "nothing")


def test_a_full_page_of_buttons_reaches_every_platform() -> None:
    buttons = [Button(f"{n}. row", f"ses:s-{n:06d}") for n in range(1, 11)]
    buttons += [Button("◀ 이전", "sesp:1"), Button("다음 ▶", "sesp:3")]
    rows = action_row(buttons)  # Discord: five per row, five rows at most
    assert [len(row["components"]) for row in rows] == [5, 5, 2]
    assert [c["custom_id"] for row in rows for c in row["components"]] == [b.data for b in buttons]
    elements = action_blocks("list", buttons)[1]["elements"]  # Slack: up to 25
    assert len(elements) == 12 and len({e["action_id"] for e in elements}) == 12
    assert all(len(b.data.encode("utf-8")) <= CALLBACK_DATA_BYTES for b in buttons)
