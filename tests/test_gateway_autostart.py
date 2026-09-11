"""CORE-gateway-autostart: a messenger enabled in the wizard just works.

``snowpea setup`` writes ``settings.gateway.<platform>``; nothing else should be
needed.  These tests pin the whole path: the daemon turns that block into a
*catch-all* binding at start (``channel_id`` is ``None``, so any chat that
messages the bot gets its own session), approvals stay fail-closed to the one
configured account, disabling the platform takes the binding away again, and
the wizard's flags write the block in the first place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from _support import connect

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.gateway.base import InboundMessage
from snowpea_core.gateway.fake import FakeAdapter
from snowpea_core.gateway.router import SOURCE_SETTINGS, desired_gateways
from snowpea_core.server.app_server import Daemon

TIMEOUT = 10.0


def write_settings(home: Path, gateway: dict[str, Any]) -> Paths:
    """Seed ``$SNOWPEA_HOME/settings.json`` the way the wizard would."""
    paths = Paths.create(home)
    settings = Settings.load(paths)
    settings.gateway = gateway
    settings.save(paths)
    return paths


TELEGRAM_ON = {
    "telegram": {"enabled": True, "token": "123:ABC", "allowed_user_id": "12345"}
}


# ---------------------------------------------------------------------------
# (a) enabled in settings -> listening after the daemon starts
# ---------------------------------------------------------------------------


async def test_enabled_messenger_binds_itself_when_the_daemon_starts(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    write_settings(
        home,
        {
            "telegram": {
                "enabled": True,
                "token": "123:ABC",
                "allowed_user_id": "12345",
                "workdir": str(workdir),
            }
        },
    )

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        bindings = (await client.ok("gateway.list", {}))["bindings"]
        assert len(bindings) == 1
        binding = bindings[0]
        assert binding["platform"] == "telegram"
        assert binding["source"] == SOURCE_SETTINGS
        # The catch-all: no channel, so any chat is served.
        assert binding["channelId"] is None
        assert binding["userId"] == "12345"
        assert binding["state"] == "active"
        assert binding["credentialsRef"] == "telegram"

        # The token left settings.json for credentials.json, mode 0600.
        credentials = home / "credentials.json"
        assert json.loads(credentials.read_text(encoding="utf-8"))["telegram"] == "123:ABC"
        assert credentials.stat().st_mode & 0o777 == 0o600

        await client.stop()
    finally:
        await daemon.stop()


async def test_a_new_chat_gets_its_own_session_through_the_catch_all_binding(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    write_settings(
        home,
        {
            "telegram": {
                "enabled": True,
                "token": "123:ABC",
                "allowed_user_id": "12345",
                "workdir": str(workdir),
            }
        },
    )

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        adapter = FakeAdapter.instances["telegram"]

        # Nobody bound chat "chat-a"; the catch-all serves it anyway.
        await adapter.push("지금 작업 중인 레포 이름", channel_id="chat-a", user_id="12345")
        assert "snowpea" in (await adapter.wait_for_send(TIMEOUT)).text

        # A second, unrelated chat gets a session of its own.
        await adapter.push("안녕", channel_id="chat-b", user_id="999")
        await adapter.wait_for_send(TIMEOUT, count=2)

        sessions = (await client.ok("session.list", {}))["sessions"]
        surfaces = {s["originSurface"] for s in sessions}
        assert surfaces == {"gateway:telegram:chat-a", "gateway:telegram:chat-b"}
        assert {s["workdir"] for s in sessions} == {str(workdir)}
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (b) risk 4: only the configured account may approve
# ---------------------------------------------------------------------------


async def test_only_the_configured_user_id_may_answer_an_approval(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    write_settings(home, TELEGRAM_ON)

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        core = daemon.core
        router = core.gateway
        (binding,) = router.list()

        answered: list[tuple[str, str, str]] = []

        async def record(request_id: str, decision: str, scope: str, *, by: str = "") -> None:
            answered.append((request_id, decision, by))

        core.approvals.respond = record  # type: ignore[method-assign]

        def press(user_id: str) -> InboundMessage:
            return InboundMessage(
                platform="telegram",
                channel_id="chat-a",
                user_id=user_id,
                callback_data="apr:ap-1:allow",
                callback_id="cb-1",
            )

        await router.handle(binding, press("12345"))
        assert [(r, d) for r, d, _ in answered] == [("ap-1", "allow")]
        assert answered[0][2] == "gateway:telegram:12345"

        # Someone else in the same chat cannot authorise anything.
        await router.handle(binding, press("99999"))
        assert len(answered) == 1
    finally:
        await daemon.stop()


async def test_a_messenger_without_a_user_id_can_approve_nothing(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    write_settings(home, {"telegram": {"enabled": True, "token": "123:ABC"}})

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        core = daemon.core
        (binding,) = core.gateway.list()
        assert binding.user_id is None

        answered: list[str] = []

        async def record(request_id: str, decision: str, scope: str, *, by: str = "") -> None:
            answered.append(request_id)

        core.approvals.respond = record  # type: ignore[method-assign]

        await core.gateway.handle(
            binding,
            InboundMessage(
                platform="telegram",
                channel_id="chat-a",
                user_id="12345",
                callback_data="apr:ap-1:allow",
                callback_id="cb-1",
            ),
        )
        assert answered == []
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (c) disabling it takes the binding away; a manual binding survives
# ---------------------------------------------------------------------------


async def test_disabling_the_messenger_removes_the_auto_binding(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    write_settings(home, TELEGRAM_ON)

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        assert len((await client.ok("gateway.list", {}))["bindings"]) == 1

        await client.ok(
            "settings.set",
            {"scope": "global", "patch": {"gateway": {"telegram": {"enabled": False}}}},
        )
        assert (await client.ok("gateway.list", {}))["bindings"] == []
        assert (await client.ok("system.info", {}))["counters"]["gateway_bindings"] == 0
        await client.stop()
    finally:
        await daemon.stop()


async def test_sync_is_idempotent_and_leaves_manual_bindings_alone(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    workdir = tmp_path / "project"
    workdir.mkdir()
    write_settings(home, TELEGRAM_ON)

    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        manual = await client.ok(
            "gateway.bind",
            {
                "platform": "telegram",
                "credentialsRef": "tg_manual",
                "target": {"new_session": {"workdir": str(workdir), "mode": "accept"}},
                "channelId": "c1",
                "userId": "u1",
            },
        )

        changed = await client.ok("gateway.sync", {})
        assert changed == {"added": [], "removed": [], "kept": ["telegram"]}

        listed = (await client.ok("gateway.list", {}))["bindings"]
        by_source = {b["source"]: b for b in listed}
        assert set(by_source) == {"manual", SOURCE_SETTINGS}
        assert by_source["manual"]["bindingId"] == manual["bindingId"]
        assert by_source["manual"]["channelId"] == "c1"
        await client.stop()
    finally:
        await daemon.stop()


async def test_the_auto_binding_survives_a_restart_without_duplicating(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    write_settings(home, TELEGRAM_ON)

    first = Daemon(port=0, home=home)
    await first.start()
    try:
        client = await connect(http, first)
        created = (await client.ok("gateway.list", {}))["bindings"][0]["bindingId"]
        await client.stop()
    finally:
        await first.stop()

    second = Daemon(port=0, home=home)
    await second.start()
    try:
        client = await connect(http, second)
        listed = (await client.ok("gateway.list", {}))["bindings"]
        assert [b["bindingId"] for b in listed] == [created]
        assert listed[0]["source"] == SOURCE_SETTINGS
        await client.stop()
    finally:
        await second.stop()


# ---------------------------------------------------------------------------
# (d) an enabled platform with no token is skipped, not fatal
# ---------------------------------------------------------------------------


def test_an_enabled_platform_without_a_token_is_skipped() -> None:
    settings = Settings()
    settings.gateway = {
        "telegram": {"enabled": True},
        "discord": {"enabled": False, "token": "d"},
        "slack": {"enabled": True, "token": "s"},
    }
    assert sorted(desired_gateways(settings)) == ["slack"]


async def test_a_tokenless_messenger_does_not_stop_the_daemon(
    gateway_env: None, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    write_settings(home, {"telegram": {"enabled": True}})
    daemon = Daemon(port=0, home=home)
    await daemon.start()
    try:
        client = await connect(http, daemon)
        assert (await client.ok("gateway.list", {}))["bindings"] == []
        await client.stop()
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# (e) the wizard writes the block the daemon reads
# ---------------------------------------------------------------------------


def test_setup_flags_write_the_token_and_the_user_id(tmp_path: Path) -> None:
    from snowpea_core.setup import wizard

    result = wizard.run(
        "blank",
        home=tmp_path / "home",
        gateway="telegram",
        token="123:ABC",
        user_id="12345",
        interactive=False,
    )

    assert result.settings.gateway["telegram"] == {
        "enabled": True,
        "token": "123:ABC",
        "allowed_user_id": "12345",
    }
    written = json.loads(result.settings_path.read_text(encoding="utf-8"))
    assert written["gateway"]["telegram"]["allowed_user_id"] == "12345"
    assert "messenger  telegram (user 12345)" in result.summary()


def test_user_id_without_a_gateway_is_a_usage_error(tmp_path: Path) -> None:
    from snowpea_core.setup import wizard

    with pytest.raises(wizard.SetupError, match="--user-id needs --gateway"):
        wizard.run("blank", home=tmp_path / "home", user_id="12345", interactive=False)


def test_the_wizard_asks_for_the_token_and_the_user_id(monkeypatch: Any, tmp_path: Path) -> None:
    """Picking telegram on screen ⑤ is followed by two prompts, in that order."""
    from snowpea_core.setup import ui, wizard
    from snowpea_core.setup.screens import gateway as gateway_screen

    asked: list[str] = []

    def fake_ask_text(prompt: str, *, interactive: Any = None, secret: bool = False) -> str:
        asked.append(prompt)
        return "123:ABC" if secret else "12345"

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)

    def ask(screen: Any, *, console: Any = None, interactive: bool = True) -> Any:
        return {"telegram"} if screen.title == gateway_screen.TITLE else set()

    result = wizard.run(
        "full",
        home=tmp_path / "home",
        vendor="anthropic",
        key="k",
        interactive=True,
        ask=ask,
    )

    assert len(asked) == 2
    assert "token" in asked[0]
    assert "user id" in asked[1] and "@userinfobot" in asked[1]
    assert result.settings.gateway["telegram"]["token"] == "123:ABC"
    assert result.settings.gateway["telegram"]["allowed_user_id"] == "12345"
