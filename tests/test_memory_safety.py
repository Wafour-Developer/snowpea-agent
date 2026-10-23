"""Addendum 27, C6: user memory refuses secrets and personal data.

A remembered note is repeated in every later prompt through the digest, so
``save_memory``, the auto-remember nudge and ``memory.write`` refuse a text
that looks like a credential, a card or long number, a phone number, an email
address or a 주민등록번호.  A refusal names the kind of value, never the value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon
from test_memory_recall import _StubCore, _StubSession
from test_memory_scope import _Core, ctx_for, git_repo

import snowpea_core.memory as memory_mod
from snowpea_core.config.paths import Paths
from snowpea_core.memory import MemoryServices
from snowpea_core.memory.safety import sensitive_kind
from snowpea_core.memory.tools import save_memory

pytestmark = pytest.mark.asyncio

#: (text, the value that must never be echoed, the kind named in the refusal)
REFUSED = [
    ("내 비밀번호는 password=hunter2xyz", "hunter2xyz", "a password or key"),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop", "an access token"),
    (
        "deploy key 9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc",
        "9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc",
        "a key or token",
    ),
    ("카드는 4111 1111 1111 1111", "4111 1111 1111 1111", "a card number"),
    ("주문번호 123456789", "123456789", "a long number"),
    ("내 번호 010-1234-5678", "010-1234-5678", "a phone number"),
    ("연락처 01012345678", "01012345678", "a phone number"),
    ("메일은 me@example.co.kr", "me@example.co.kr", "an email address"),
    ("주민번호 900101-1234567", "900101-1234567", "a resident registration number"),
]
KEPT = ["나는 로켓배송만 써", "dark mode, 2026-10-23 meeting at 10:30", "버전 0.2.63을 쓴다"]


@pytest_asyncio.fixture
async def services(tmp_path: Path) -> Any:
    memory = MemoryServices.open(Paths.create(tmp_path / "home"))
    try:
        yield memory
    finally:
        await memory.close()


@pytest.mark.parametrize(("text", "value", "kind"), REFUSED)
async def test_kinds(text: str, value: str, kind: str) -> None:
    assert sensitive_kind(text) == kind


@pytest.mark.parametrize("text", KEPT)
async def test_ordinary_preferences_pass(text: str) -> None:
    assert sensitive_kind(text) is None


@pytest.mark.parametrize(("text", "value", "kind"), REFUSED)
async def test_save_memory_refuses_without_echoing(
    services: MemoryServices, tmp_path: Path, text: str, value: str, kind: str
) -> None:
    ctx = ctx_for(_Core(services), git_repo(tmp_path / "repo"))
    result = await save_memory(ctx, {"text": text, "scope": "global"})
    assert result.ok is False
    assert result.error is not None and kind in result.error
    assert value not in result.error
    assert await services.store.list() == []


async def test_save_memory_still_keeps_a_preference(
    services: MemoryServices, tmp_path: Path
) -> None:
    ctx = ctx_for(_Core(services), git_repo(tmp_path / "repo"))
    result = await save_memory(ctx, {"text": "나는 로켓배송만 써", "scope": "global"})
    assert result.ok is True
    assert [row.text for row in await services.store.list()] == ["나는 로켓배송만 써"]


@pytest.mark.parametrize(("text", "value", "kind"), REFUSED)
async def test_the_nudge_skips_silently(
    services: MemoryServices, text: str, value: str, kind: str, caplog: Any
) -> None:
    with caplog.at_level("DEBUG", logger="snowpea.memory"):
        wrote = await memory_mod.nudge_after_turn(
            _StubCore(services), _StubSession(), f"기억해: {text}"
        )
    assert wrote is None
    assert await services.store.list() == []
    assert all(value not in record.getMessage() for record in caplog.records)


async def test_the_nudge_still_remembers_a_preference(services: MemoryServices) -> None:
    wrote = await memory_mod.nudge_after_turn(
        _StubCore(services), _StubSession(), "기억해: 나는 로켓배송만 써"
    )
    assert wrote is not None and "로켓배송" in wrote.text


async def test_memory_write_refuses_and_list_flags_old_rows(tmp_path: Path) -> None:
    daemon = await make_daemon(tmp_path / "home")
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            for text, value, kind in REFUSED:
                frame = await client.call("memory.write", {"text": text})
                error = frame["error"]
                assert error["data"]["code"] == "invalid_params"
                assert kind in error["message"] and value not in error["message"]

            kept = await client.ok("memory.write", {"text": "나는 로켓배송만 써"})
            # A row saved before the check is left alone but flagged.
            old = await daemon.core.memory.store.write("옛 메모: 010-9876-5432")
            listed = {row["id"]: row for row in (await client.ok("memory.list", {}))["entries"]}
            assert listed[kept["id"]]["sensitive"] is False
            assert listed[old.id]["sensitive"] is True
            await client.stop()
    finally:
        await daemon.stop()
