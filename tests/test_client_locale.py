"""``system.hello {locale}`` and localised validation texts (1.65.0, ``clientLocale``).

A client says its language once; ``settings.set``'s problems, ``security.get``'s
problems and the person-only refusals a browser shows come back in it, while
the machine codes (``details.reason``, ``problemItems[].reason``) stay English.
"""

from __future__ import annotations

import asyncio
import string
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.security import enforce
from snowpea_core.security.catalog import LOCALES
from snowpea_core.security.lists import path_problem, problems_in
from snowpea_core.server import errors, settings_gate
from snowpea_core.server.app_server import Core, build_dispatcher
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import CAPABILITIES, PROTOCOL_VERSION, HelloParams
from snowpea_core.server.rpc import RpcConnection
from snowpea_core.util import i18n


async def _nothing(_obj: dict[str, Any]) -> None:
    return None


def _conn(kind: str = "tui", locale: str | None = None) -> RpcConnection:
    conn = RpcConnection(_nothing)
    conn.authenticated = True
    conn.client_kind = kind
    conn.client_version = "0.4.0"
    conn.locale = locale
    return conn


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")


@pytest.fixture
def dispatcher(core: Core) -> Any:
    return build_dispatcher(core)


def _hello(**extra: Any) -> dict[str, Any]:
    return {"method": "system.hello",
            "params": {"token": "t", "clientVersion": "0.4.0", "protocolVersion": PROTOCOL_VERSION,
                       "clientKind": "tui", **extra}}


# -- the hello field --------------------------------------------------------------------------


def test_contract() -> None:
    assert PROTOCOL_VERSION == "1.79.0"
    assert "clientLocale" in CAPABILITIES
    assert "locale" in HelloParams.model_fields


@pytest.mark.parametrize(("raw", "kept"), [
    ("ko", "ko"), ("ko-KR", "ko-KR"), ("ko_kr", "ko-KR"), (" ja ", "ja"), ("ES-es", "es-ES"),
    ("zh-hans-cn", "zh-Hans-CN"), ("zh-CN", "zh-CN"), ("es-419", "es-419"),
])
def test_normalise_keeps_a_language_tag(raw: str, kept: str) -> None:
    assert i18n.normalise_locale(raw) == kept


@pytest.mark.parametrize("raw", [
    None, 42, "", "C", "ko_KR.UTF-8", "ko KR", "../etc", "k", "x" * 36, "ko-" + "a" * 33,
    "한국어", "en--US",
])
def test_normalise_ignores_what_is_not_a_tag(raw: Any) -> None:
    assert i18n.normalise_locale(raw) is None


async def test_hello_stores_the_locale(dispatcher: Any) -> None:
    conn = RpcConnection(_nothing)
    reply = await dispatcher._invoke(conn, _hello(locale="ko_kr"))
    assert reply["protocolVersion"] == PROTOCOL_VERSION
    assert conn.authenticated and conn.locale == "ko-KR"


@pytest.mark.parametrize("bad", ["not a tag!", 42, ["ko"], "x" * 80, "C"])
async def test_an_invalid_locale_is_ignored_not_refused(dispatcher: Any, bad: Any) -> None:
    conn = RpcConnection(_nothing)
    await dispatcher._invoke(conn, _hello(locale=bad))
    assert conn.authenticated and conn.locale is None
    # A later hello with an odd value keeps what the connection had.
    await dispatcher._invoke(conn, _hello(locale="ja"))
    await dispatcher._invoke(conn, _hello(locale=bad))
    assert conn.locale == "ja"


async def test_a_hello_without_locale_is_english(dispatcher: Any) -> None:
    conn = RpcConnection(_nothing)
    await dispatcher._invoke(conn, _hello())
    assert conn.locale is None


# -- the texts --------------------------------------------------------------------------------


def _fields(text: str) -> set[str]:
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


@pytest.mark.parametrize("key", sorted(i18n.MESSAGES))
def test_every_text_is_in_every_locale_with_the_same_placeholders(key: str) -> None:
    texts = i18n.MESSAGES[key]
    assert set(texts) == set(LOCALES), key
    want = _fields(texts["en"])
    for locale, text in texts.items():
        assert text.strip(), (key, locale)
        assert _fields(text) == want, (key, locale)
        if locale != "en":
            assert text != texts["en"], (key, locale)


def test_the_english_notes_are_the_constants_they_replace() -> None:
    assert i18n.MESSAGES["get.disabled"]["en"] == enforce.DISABLED_NOTE
    assert i18n.MESSAGES["get.unknown_code_migrated"]["en"] == enforce.UNKNOWN_CODE_MIGRATED_NOTE


@pytest.mark.parametrize("reason", [
    settings_gate.BROWSER_NOT_ALLOWED, settings_gate.BROWSER_REMOTE, settings_gate.BROWSER_LEGACY,
    settings_gate.BROWSER_UNCONFIRMED, settings_gate.BROWSER_NOT_OWNER, "messenger_blocked",
    "remote_blocked", "trust_changed",
])
def test_every_person_only_refusal_has_a_text(reason: str) -> None:
    assert reason in i18n.MESSAGES


def test_a_regional_tag_falls_back_to_its_language() -> None:
    assert i18n.text("path.empty", locale="ko-KR") == i18n.MESSAGES["path.empty"]["ko"]
    assert i18n.text("path.empty", locale="es-ES") == i18n.MESSAGES["path.empty"]["es"]
    assert i18n.text("path.empty", locale="zh-CN") == i18n.MESSAGES["path.empty"]["zh-CN"]
    assert i18n.text("path.empty", locale="fr") == i18n.MESSAGES["path.empty"]["en"]
    assert i18n.text("path.empty") == i18n.MESSAGES["path.empty"]["en"]


def test_the_machine_reason_stays_english_while_the_text_changes() -> None:
    raw = {"protectedPaths": {"custom": ["relative/x"]},
           "blockedCommands": {"presets": ["nope"]}}
    token = i18n.CURRENT_LOCALE.set((None, "ko"))
    try:
        found = problems_in(raw)
        assert path_problem("relative/x") == i18n.text("path.not_absolute", locale="ko",
                                                       path="'relative/x'")
    finally:
        i18n.CURRENT_LOCALE.reset(token)
    english = problems_in(raw)
    assert found.structured == english.structured
    assert {"list": "commands", "entry": "nope", "reason": "unknown preset"} in found.structured
    assert any("is not an absolute path" in item["reason"] for item in found.structured)
    assert all(a != b for a, b in zip(found, english, strict=True))
    assert any("알 수 없는 프리셋" in line for line in found)


# -- through the dispatcher -------------------------------------------------------------------


BAD_PRESET = {"security": {"blockedCommands": {"presets": ["essential", "nope"]}}}


async def _refused(dispatcher: Any, conn: Any, method: str, params: dict[str, Any]) -> RpcError:
    with pytest.raises(RpcError) as caught:
        await dispatcher._invoke(conn, {"method": method, "params": params})
    return caught.value


async def test_settings_set_answers_a_korean_connection_in_korean(dispatcher: Any) -> None:
    err = await _refused(dispatcher, _conn(locale="ko"), "settings.set",
                         {"scope": "global", "patch": BAD_PRESET})
    assert err.code == errors.INVALID_PARAMS
    assert "알 수 없는 프리셋 'nope'" in err.message
    assert err.data["problems"] == [err.message]
    assert err.data["problemItems"] == [
        {"list": "commands", "entry": "nope", "reason": "unknown preset"}]


async def test_settings_set_answers_a_connection_without_locale_in_english(
        dispatcher: Any) -> None:
    err = await _refused(dispatcher, _conn(), "settings.set",
                         {"scope": "global", "patch": BAD_PRESET})
    assert err.message.startswith("security.blockedCommands.presets: unknown preset 'nope'")
    assert err.data["problemItems"][0]["reason"] == "unknown preset"


async def test_global_only_is_localised_and_keeps_its_reason(dispatcher: Any,
                                                             tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    err = await _refused(dispatcher, _conn(locale="ja"), "settings.set",
                         {"scope": "project", "workdir": str(project),
                          "patch": {"security": {"unknownCode": "allow"}}})
    assert err.data == {"reason": "global_only", "key": "security.unknownCode"}
    assert "グローバル設定" in err.message


async def test_a_calls_own_locale_param_wins(dispatcher: Any) -> None:
    err = await _refused(dispatcher, _conn(locale="ko"), "security.check",
                         {"command": "ls", "locale": "es-ES",
                          "draft": {"blockedCommands": {"presets": ["nope"]}}})
    assert "preset desconocido 'nope'" in err.message


async def test_security_get_problems_follow_the_connection(core: Core, dispatcher: Any) -> None:
    core.settings = Settings.model_validate({"security": {"enabled": False}})
    ko = await dispatcher._invoke(_conn(locale="zh-CN"), {"method": "security.get",
                                                          "params": {}})
    en = await dispatcher._invoke(_conn(), {"method": "security.get", "params": {}})
    assert ko["problems"][0] == i18n.MESSAGES["get.disabled"]["zh-CN"]
    assert en["problems"][0] == enforce.DISABLED_NOTE
    # The structured twin keeps its stable code and English reason.
    assert ko["problemItems"][0] == en["problemItems"][0]
    assert ko["problemItems"][0]["reason"] == enforce.DISABLED_NOTE


def test_a_browser_refusal_is_in_the_browsers_language() -> None:
    browser = _conn("browser", locale="ko-KR")
    with pytest.raises(RpcError) as caught:
        settings_gate.refuse(browser, "trust a project", reason=settings_gate.BROWSER_NOT_OWNER)
    assert caught.value.data["reason"] == "browser_not_owner"
    assert caught.value.message == i18n.text(
        "refusal.browser", locale="ko", why=i18n.MESSAGES["browser_not_owner"]["ko"])


#: What an old browser (host < 0.4.0, no locale) shows as is: where to allow it instead.
WHERE_TO_ALLOW = {"en": "allow it in the snowpea app or the terminal on this computer",
                  "ko": "이 컴퓨터의 snowpea 앱이나 터미널에서 허용"}
UPDATE_BROWSER = {"en": "or update the browser", "ko": "브라우저를 업데이트"}


@pytest.mark.parametrize("reason", [settings_gate.BROWSER_LEGACY,
                                    settings_gate.BROWSER_UNCONFIRMED,
                                    "messenger_blocked", "remote_blocked"])
@pytest.mark.parametrize("locale", ["en", "ko"])
def test_a_refusal_an_old_client_cannot_satisfy_says_where_to_allow_it(
        reason: str, locale: str) -> None:
    said = i18n.text(reason, locale=locale)
    assert WHERE_TO_ALLOW[locale] in said.lower()
    if reason == settings_gate.BROWSER_LEGACY:
        assert UPDATE_BROWSER[locale] in said


def test_an_old_browser_without_locale_reads_the_english_hint() -> None:
    old = _conn("browser")  # sends no locale
    with pytest.raises(RpcError) as caught:
        settings_gate.refuse(old, "trust a project", reason=settings_gate.BROWSER_LEGACY)
    assert caught.value.data["reason"] == "browser_legacy"
    assert WHERE_TO_ALLOW["en"] in caught.value.message
    assert UPDATE_BROWSER["en"] in caught.value.message


def test_a_text_for_another_connection_uses_that_connections_locale() -> None:
    caller, other = _conn(locale="ko"), _conn(locale="ja")
    token = i18n.CURRENT_LOCALE.set((caller.surface_id, caller.locale))
    try:
        assert i18n.current_locale() == "ko"
        assert i18n.current_locale(caller) == "ko"
        assert i18n.current_locale(other) == "ja"
    finally:
        i18n.CURRENT_LOCALE.reset(token)
    assert i18n.current_locale() is None
    assert i18n.current_locale(other) == "ja"


# -- the contextvar does not leak -------------------------------------------------------------


async def test_the_locale_does_not_leak_between_connections_or_calls(dispatcher: Any) -> None:
    seen: dict[str, list[str | None]] = {}

    async def probe(conn: Any, _params: Any, _core: Any) -> dict[str, Any]:
        mine = seen.setdefault(conn.surface_id, [])
        mine.append(i18n.current_locale())
        await asyncio.sleep(0.01)  # the others run in between
        mine.append(i18n.current_locale())
        return {}

    dispatcher._handlers["system.info"] = probe
    conns = [_conn(locale="ko"), _conn(locale="ja"), _conn(), _conn(locale="es-ES")]
    await asyncio.gather(*(
        dispatcher._invoke(conn, {"method": "system.info", "params": {}})
        for conn in conns * 3
    ))
    for conn in conns:
        assert seen[conn.surface_id] == [conn.locale] * 6
    assert i18n.CURRENT_LOCALE.get() is None


async def test_a_refused_call_resets_the_locale(dispatcher: Any) -> None:
    await _refused(dispatcher, _conn(locale="ko"), "settings.set",
                   {"scope": "global", "patch": BAD_PRESET})
    assert i18n.CURRENT_LOCALE.get() is None
    assert i18n.text("path.empty") == i18n.MESSAGES["path.empty"]["en"]
