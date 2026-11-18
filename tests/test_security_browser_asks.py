"""Browser team asks (1.64.0): ``security.get {allProjects}`` and ja/es/zh-CN texts.

The browser's global Settings › 보안 page shows each registered project's
additions read-only; ``security.presets.list`` and the check's sentences speak
Japanese, Spanish and Simplified Chinese as well as English and Korean.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.security import catalog, check
from snowpea_core.server import security_handlers
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import (
    CAPABILITIES,
    SecurityGetParams,
    SecurityPresetsListParams,
)
from snowpea_core.server.security_handlers import get_handler, presets_list_handler
from snowpea_core.session import workspaces
from snowpea_core.session.store import Store

ALL_LOCALES = ("en", "ko", "ja", "es", "zh-CN")


def _conn(kind: str = "browser") -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=False, client_version="0.4.0", client_id=f"{kind}-1")


@pytest.fixture
def core(tmp_path: Path) -> Any:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    made.store = Store(tmp_path / "home" / "sessions.db")
    return made


def _project(root: Path, security: dict[str, Any] | None) -> Path:
    root.mkdir(parents=True)
    if security is not None:
        (root / ".snowpea").mkdir()
        (root / ".snowpea" / "settings.json").write_text(json.dumps({"security": security}))
    return root


# -- security.get {allProjects} -------------------------------------------------------------


async def test_get_without_all_projects_has_no_projects(core: Any) -> None:
    result = await get_handler(_conn(), SecurityGetParams(), core)
    assert result.projects is None and result.truncated is False


async def test_get_lists_each_registered_projects_additions(core: Any, tmp_path: Path) -> None:
    adds = _project(tmp_path / "adds", {
        "blockedCommands": {"presets": ["power"], "custom": ["make deploy"]},
        "protectedPaths": {"custom": [{"path": "~/vault", "level": "all"}]},
    })
    plain = _project(tmp_path / "plain", None)
    broken = _project(tmp_path / "broken", {"blockedCommands": {"presets": ["no-such"]}})
    await workspaces.add(core.store, str(adds), "Adds")
    await workspaces.add(core.store, str(plain))
    await workspaces.add(core.store, str(broken), "Broken")

    result = await get_handler(_conn(), SecurityGetParams(allProjects=True), core)
    assert result.truncated is False
    by_root = {p.workdir: p for p in result.projects or []}
    assert set(by_root) >= {str(adds), str(plain), str(broken)}
    first = by_root[str(adds)]
    assert first.name == "Adds"
    assert first.blockedCommands.presets == ["power"]
    assert first.blockedCommands.custom == ["make deploy"]
    assert [(e.path, e.level) for e in first.protectedPaths.custom] == [("~/vault", "all")]
    assert first.problems is None
    empty = by_root[str(plain)]
    assert empty.blockedCommands.presets == [] and empty.protectedPaths.custom == []
    assert by_root[str(broken)].problems and "no-such" in by_root[str(broken)].problems[0]
    # The global lists are unchanged by the projects.
    assert result.blockedCommands.presets == ["essential"]
    assert empty.problems is None


async def test_get_all_projects_is_bounded(core: Any, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_handlers, "MAX_PROJECTS", 2)
    for name in ("a", "b", "c"):
        await workspaces.add(core.store, str(_project(tmp_path / name, None)))
    result = await get_handler(_conn(), SecurityGetParams(allProjects=True), core)
    assert len(result.projects or []) == 2 and result.truncated is True


def test_capability_advertised() -> None:
    assert "securityAllProjects" in CAPABILITIES
    assert security_handlers.MAX_PROJECTS == 200


# -- localisation ---------------------------------------------------------------------------


def test_every_preset_has_every_locale() -> None:
    for preset in (*catalog.COMMAND_PRESETS, *catalog.PATH_PRESETS):
        for field in (preset.title, preset.description):
            assert set(field) == set(ALL_LOCALES), preset.id
            assert all(text.strip() for text in field.values()), preset.id


def test_every_check_text_has_every_locale() -> None:
    for key, texts in check._MESSAGES.items():
        assert set(texts) == set(ALL_LOCALES), key
        fields = {name for t in texts.values() for name in _fields(t)}
        for lang, text in texts.items():
            # Every locale fills the same blanks.
            assert set(_fields(text)) == fields, (key, lang)
    for head, texts in check._WHY_TEXTS:
        assert set(texts) == set(ALL_LOCALES), head
        for text in texts.values():
            text.format(x="X")


def _fields(text: str) -> list[str]:
    import string

    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


def test_locale_fallback() -> None:
    texts = {"en": "E", "ko": "K", "ja": "J", "es": "S", "zh-CN": "Z"}
    assert catalog.pick(texts, "zh-CN") == "Z"
    assert catalog.pick(texts, "zh_cn") == "Z"
    assert catalog.pick(texts, "es-ES") == "S"
    assert catalog.pick(texts, "es-419") == "S"
    assert catalog.pick(texts, "ja-JP") == "J"
    assert catalog.pick(texts, "ko-KR") == "K"
    assert catalog.pick(texts, "fr") == "E"
    assert catalog.pick(texts, None) == "E"
    # zh-CN, then zh, then en.
    assert catalog.pick({"en": "E", "zh": "Z"}, "zh-CN") == "Z"
    assert catalog.pick({"en": "E", "zh-CN": "Z"}, "zh-TW") == "E"


@pytest.mark.parametrize(("asked", "served"), [
    ("ja", "ja"), ("es", "es"), ("zh-CN", "zh-CN"), ("es-ES", "es"), ("ja-JP", "ja"),
    ("fr", "en"),
])
async def test_presets_list_in_the_new_locales(core: Any, asked: str, served: str) -> None:
    result = await presets_list_handler(None, SecurityPresetsListParams(locale=asked), core)
    assert result.locale == served
    titles = {p.id: p.title for p in result.commands}
    assert titles["essential"] == catalog.COMMAND_PRESETS[0].title[served]
    for preset in (*result.commands, *result.paths):
        source = catalog.command_preset(preset.id) if preset in result.commands else next(
            p for p in catalog.PATH_PRESETS if p.id == preset.id)
        assert preset.title == source.title[served]
        assert preset.description == source.description[served]


def test_check_message_in_the_new_locales() -> None:
    assert check.message("custom_title", "zh-CN") == "你的列表"
    assert check.message("custom_title", "es-MX") == "tu lista"
    assert "{path}" not in check.message("custom_path_all", "ja", path="/x")
    assert check.why_text("eval", "", "es").endswith("(eval)")
