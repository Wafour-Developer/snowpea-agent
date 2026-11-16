"""Security review 9 (release gate): F1 endpoint-shaped fields wait on a disk edit,
F2 a ``settings.json`` that links to the user's own file, F3 degraded settings,
F4 the last writers that followed links.

Links and files are made ONLY inside pytest's ``tmp_path`` (a throwaway home and
a fictional user home there); command lines are DATA handed to the pure checker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config import hot_reload
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings, SettingsUnreadable
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.settings_leaves import ENDPOINT_SHAPED

pytestmark = [pytest.mark.real_project_trust]

PERSON = SimpleNamespace(authenticated=True, closed=False, client_kind="ide",
                         remote_login=False, client_version="0.3.0", client_id="ide-1",
                         surface_id="ide-s")


def _core(paths: Paths) -> Core:
    made = Core(settings=Settings.load(paths), paths=paths, token="t")
    sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(method: str, payload: dict[str, Any], **_: Any) -> None:
        sent.append((method, payload))

    made.hub = SimpleNamespace(notify=notify)
    made.mark_settings_saved()
    return made


@pytest.fixture
def core(tmp_path: Path) -> Core:
    paths = Paths.create(tmp_path / "home")
    Settings().save(paths)
    return _core(paths)


def _edit(core: Core, change: Any) -> None:
    data = core.settings.model_dump(mode="json")
    change(data)
    core.paths.settings_json.write_text(json.dumps(data), encoding="utf-8")
    os.utime(core.paths.settings_json, None)


# -- F1: endpoint-shaped fields of the free-form blocks ------------------------------------


def test_every_endpoint_shaped_block_is_gated() -> None:
    patterns = set(hot_reload.gated_patterns())
    for block in ENDPOINT_SHAPED:
        assert (*block.split("."), hot_reload.ENDPOINT_FIELD) in patterns, block


@pytest.mark.parametrize(("change", "waits"), [
    (lambda d: d["search"]["credentials"].update(searxng={"url": "https://attacker.example"}),
     ["search.credentials.searxng.url"]),
    (lambda d: d["search"]["credentials"].update(tavily={"base_url": "https://a.example",
                                                         "baseUrl": "https://b.example"}),
     ["search.credentials.tavily.baseUrl", "search.credentials.tavily.base_url"]),
    (lambda d: d["browser"]["credentials"].update(firecrawl={"FIRECRAWL_URL": "https://a.example"}),
     ["browser.credentials.firecrawl.FIRECRAWL_URL"]),
    (lambda d: d["providers"].update(openai={"endpoint": "https://attacker.example/v1"}),
     ["providers.openai.endpoint"]),
    (lambda d: d["gateway"].update(telegram={"proxy": "http://attacker.example:3128"}),
     ["gateway.telegram.proxy"]),
])
async def test_an_endpoint_shaped_field_edited_on_disk_waits(core: Core, change: Any,
                                                              waits: list[str]) -> None:
    before = core.settings.model_dump(mode="json")
    _edit(core, change)
    await core.reload_settings()
    assert sorted(core.settings_waiting) == sorted(waits)
    assert core.settings.model_dump(mode="json") == before
    applied, _ = await core.accept_disk_settings(True)
    assert sorted(applied) == sorted(waits)


async def test_the_rest_of_an_endpoint_block_still_reloads_at_once(core: Core) -> None:
    _edit(core, lambda d: d["search"]["credentials"].update(brave={"api_key": "k-new"}))
    reloaded, keys = await core.reload_settings()
    assert reloaded and keys == ["search"] and core.settings_waiting == {}
    assert core.settings.search.credentials["brave"]["api_key"] == "k-new"


# -- F2: a settings.json that is a link to the user's own file ------------------------------


def _linked(tmp_path: Path, where: Path, data: dict[str, Any] | str) -> Paths:
    paths = Paths.create(tmp_path / "home")
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    paths.settings_json.symlink_to(where)
    return paths


def test_a_link_to_your_own_file_is_followed_and_written_in_place(tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "snowpea" / "settings.json"
    paths = _linked(tmp_path, target, {"security": {"unknownCode": "allow"},
                                       "agent": {"busy": "queue"}})
    os.chmod(target, 0o640)
    loaded = Settings.load(paths)
    assert loaded.degraded_sections() == [] and loaded.security.unknownCode == "allow"
    loaded.agent.busy = "steer"
    loaded.save(paths)
    assert paths.settings_json.is_symlink()
    assert os.path.realpath(paths.settings_json) == str(target)
    assert json.loads(target.read_text(encoding="utf-8"))["agent"]["busy"] == "steer"
    assert oct(target.stat().st_mode & 0o777) == "0o640"  # the target keeps its mode
    assert sorted(p.name for p in target.parent.iterdir()) == ["settings.json"]  # no temp left


def test_an_invalid_linked_file_is_copied_beside_the_link(tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "settings.json"
    paths = _linked(tmp_path, target, "{not json")
    loaded = Settings.load(paths)
    assert loaded.degraded_sections() == ["*"]
    assert (paths.home / "settings.json.invalid").read_text("utf-8") == "{not json"
    assert not (target.parent / "settings.json.invalid").exists()


@pytest.mark.parametrize("kind", ["missing", "folder", "loop"])
def test_a_link_to_anything_else_is_refused(tmp_path: Path, kind: str) -> None:
    paths = Paths.create(tmp_path / "home")
    if kind == "missing":
        paths.settings_json.symlink_to(tmp_path / "nothing.json")
    elif kind == "folder":
        (tmp_path / "folder").mkdir()
        paths.settings_json.symlink_to(tmp_path / "folder")
    else:
        paths.settings_json.symlink_to(paths.home / "loop.json")
        (paths.home / "loop.json").symlink_to(paths.settings_json)
    assert Settings.load(paths).degraded_sections() == ["*"]
    with pytest.raises(SettingsUnreadable):
        Settings().save(paths)
    assert paths.settings_json.is_symlink()


async def test_repointing_the_link_is_a_disk_edit_that_waits(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    first = tmp_path / "a" / "settings.json"
    first.parent.mkdir()
    first.write_text(json.dumps(Settings().model_dump(mode="json")), encoding="utf-8")
    paths.settings_json.symlink_to(first)
    core = _core(paths)
    second = tmp_path / "b" / "settings.json"
    second.parent.mkdir()
    data = Settings().model_dump(mode="json")
    data["security"]["unknownCode"] = "allow"
    second.write_text(json.dumps(data), encoding="utf-8")
    paths.settings_json.unlink()
    paths.settings_json.symlink_to(second)
    assert core.settings_file_changed()
    await core.reload_settings()
    assert sorted(core.settings_waiting) == ["security"]
    assert core.settings.security.unknownCode == "ask"


def _home_with_link(tmp_path: Path, target: Path) -> tuple[Path, Path]:
    user = tmp_path / "user"
    home = user / ".snowpea"
    home.mkdir(parents=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    (home / "settings.json").symlink_to(target)
    return user, home


@pytest.mark.parametrize("where", ["user/proj/snowpea-settings.json",
                                   "user/dotfiles/snowpea/settings.json"])
def test_the_linked_target_is_protected_like_settings_json(tmp_path: Path, where: str) -> None:
    from snowpea_core.security import written
    from snowpea_core.security.check import Context, check_command, check_path
    from snowpea_core.security.control_plane import control_plane_effect
    from snowpea_core.security.lists import Lists
    from snowpea_core.server.fs_handlers import Roots
    from snowpea_core.tools.config_guard import is_snowpea_config_path

    written.reset()
    target = tmp_path / where
    user, home = _home_with_link(tmp_path, target)
    work = user / "proj"
    work.mkdir(exist_ok=True)
    ctx = Context(workdir=work, user_home=user, home=home, projects=(str(work),), mounts=(),
                  env_venv=False)
    effect = control_plane_effect(target, "write", home=home, user_home=user)
    assert effect is not None and effect.verdict == "blocked"
    assert effect.shown == "$SNOWPEA_HOME/settings.json"
    assert control_plane_effect(target, "read", home=home, user_home=user) is None
    assert check_command(f"echo x > {target}", Lists(), ctx, cwd=work).blocked
    assert check_command(f"python3 -c \"open('{target}', 'w').write('x')\"", Lists(), ctx,
                         cwd=work).blocked
    assert check_path(str(target), Lists(), ctx).blocked                 # fs.write
    assert is_snowpea_config_path(str(target), home=home)                # file tools ask
    roots = Roots(workdir=work, roots=(work,), home=home, home_roots=())
    assert roots.protected(target)
    # A plain neighbour is ordinary work.
    neighbour = target.with_name("other.json")
    assert control_plane_effect(neighbour, "write", home=home, user_home=user) is None
    assert not is_snowpea_config_path(str(neighbour), home=home)
    assert not roots.protected(neighbour)


def test_jarvis_does_not_read_the_linked_target(tmp_path: Path) -> None:
    from snowpea_core.jarvis.tools import _secret_listing

    target = tmp_path / "user" / "dotfiles" / "settings.json"
    _user, home = _home_with_link(tmp_path, target)
    service = SimpleNamespace(core=SimpleNamespace(paths=SimpleNamespace(home=home)))
    assert _secret_listing(service, target)  # type: ignore[arg-type]
    assert not _secret_listing(service, target.with_name("notes.md"))  # type: ignore[arg-type]


def test_a_repointed_link_is_judged_where_it_points_now(tmp_path: Path) -> None:
    from snowpea_core.security.control_plane import control_plane_effect

    first = tmp_path / "user" / "dotfiles" / "one.json"
    user, home = _home_with_link(tmp_path, first)
    second = tmp_path / "user" / "dotfiles" / "two.json"
    second.write_text("{}", encoding="utf-8")
    (home / "settings.json").unlink()
    (home / "settings.json").symlink_to(second)
    assert control_plane_effect(second, "write", home=home, user_home=user) is not None
    assert control_plane_effect(first, "write", home=home, user_home=user) is None


async def test_a_linked_credentials_file_is_named_in_security_problems(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SecurityGetParams
    from snowpea_core.server.rpc import RpcConnection
    from snowpea_core.server.security_handlers import get_handler

    async def nothing(_frame: dict[str, Any]) -> None:
        return None

    paths = Paths.create(tmp_path / "home")
    elsewhere = tmp_path / "creds.json"
    elsewhere.write_text("{}", encoding="utf-8")
    paths.credentials_json.symlink_to(elsewhere)
    core = Core(settings=Settings(), paths=paths, token="t")
    got = await get_handler(RpcConnection(nothing), SecurityGetParams(), core)
    assert any(str(paths.credentials_json) in p and "replace the link with the file itself" in p
               for p in got.problems)


# -- F3: degraded settings ------------------------------------------------------------------


def test_an_unreadable_file_runs_on_the_default_lists(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text("{not json", "utf-8")
    loaded = Settings.load(paths)
    default = Settings().security
    assert loaded.security.blockedCommands.presets == default.blockedCommands.presets
    assert loaded.security.protectedPaths.presets == default.protectedPaths.presets
    assert loaded.security.unknownCode == "ask" and loaded.web.remote.enabled is False


async def test_an_unreadable_file_is_not_written_over_until_fixed(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import reload_settings_handler
    from snowpea_core.server.protocol import Empty, SettingsSetParams
    from snowpea_core.server.settings_handlers import settings_set_handler

    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text("{not json", "utf-8")
    core = _core(paths)
    assert core.settings.degraded_sections() == ["*"]
    with pytest.raises(RpcError) as caught:
        await settings_set_handler(PERSON, SettingsSetParams(
            patch={"agent": {"busy": "queue"}}), core)
    assert caught.value.code == errors.INVALID_PARAMS
    assert caught.value.data["reason"] == "settings_unreadable"
    assert str(paths.settings_json) in caught.value.message
    assert "settings.json.invalid" in caught.value.message
    with pytest.raises(SettingsUnreadable):
        core.settings.save(paths)
    with pytest.raises(SettingsUnreadable):
        Settings().save(paths)          # a fresh document too: the file on disk decides
    assert paths.settings_json.read_text("utf-8") == "{not json"
    # The person fixes the file; a reload clears the state and settings.set works.
    paths.settings_json.write_text(json.dumps({"agent": {"busy": "steer"}}), "utf-8")
    os.utime(paths.settings_json, None)
    await reload_settings_handler(PERSON, Empty(), core)
    assert core.settings.degraded_sections() == []
    await settings_set_handler(PERSON, SettingsSetParams(patch={"agent": {"busy": "queue"}}),
                               core)
    assert json.loads(paths.settings_json.read_text("utf-8"))["agent"]["busy"] == "queue"


def test_a_section_level_degrade_still_saves(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text(json.dumps({"web": {"remote": {"port": "x"}},
                                               "agent": {"busy": "queue"}}), "utf-8")
    loaded = Settings.load(paths)
    assert loaded.degraded_sections() == ["web"]
    loaded.save(paths)
    assert json.loads(paths.settings_json.read_text("utf-8"))["agent"]["busy"] == "queue"


def _broken_web(tmp_path: Path) -> tuple[Paths, dict[str, Any]]:
    paths = Paths.create(tmp_path / "home")
    data = Settings().model_dump(mode="json")
    data["web"] = {"remote": {"port": "x"}, "note": [1, {"a": None}]}
    paths.settings_json.write_text(json.dumps(data), "utf-8")
    return paths, data


async def test_an_unrelated_save_keeps_the_invalid_section_and_its_mark(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SettingsSetParams
    from snowpea_core.server.settings_handlers import settings_set_handler

    paths, data = _broken_web(tmp_path)
    core = _core(paths)
    assert core.settings.degraded_sections() == ["web"]
    await settings_set_handler(PERSON, SettingsSetParams(patch={"agent": {"busy": "queue"}}),
                               core)
    on_disk = json.loads(paths.settings_json.read_text("utf-8"))
    assert on_disk["web"] == data["web"] and on_disk["agent"]["busy"] == "queue"
    assert core.settings.degraded_sections() == ["web"]
    assert core.settings.web.remote.enabled is False         # in memory: still the defaults
    core.settings.save(paths)                                # an internal save too
    assert json.loads(paths.settings_json.read_text("utf-8"))["web"] == data["web"]


async def test_setting_the_section_clears_the_mark(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SettingsSetParams
    from snowpea_core.server.settings_handlers import settings_set_handler

    paths, _data = _broken_web(tmp_path)
    core = _core(paths)
    await settings_set_handler(PERSON, SettingsSetParams(patch={"web": {"remote": {
        "port": 7788}}}), core)
    assert core.settings.degraded_sections() == []
    on_disk = json.loads(paths.settings_json.read_text("utf-8"))
    assert on_disk["web"]["remote"]["port"] == 7788 and "note" not in on_disk["web"]
    assert Settings.load(paths).degraded_sections() == []


async def test_a_reload_with_no_changed_key_still_follows_the_mark(tmp_path: Path) -> None:
    from snowpea_core.server.app_server import info_handler, reload_settings_handler
    from snowpea_core.server.protocol import Empty
    from snowpea_core.server.web_remote_handlers import _status as web_status

    paths, data = _broken_web(tmp_path)
    core = _core(paths)
    core.hub.clients = list
    assert core.settings.degraded_sections() == ["web"]
    # Fixed on disk to exactly the defaults already in use: no key changes.
    data["web"] = Settings().model_dump(mode="json")["web"]
    paths.settings_json.write_text(json.dumps(data), "utf-8")
    os.utime(paths.settings_json, (1, 1))
    result = await reload_settings_handler(PERSON, Empty(), core)
    assert result.changedKeys == []
    assert core.settings.degraded_sections() == []
    assert (await info_handler(PERSON, Empty(), core)).settingsDegraded == []
    assert (await web_status(core)).settingsDegraded == []
    # Broken again into a value that loads as those same defaults: no key changes.
    data["web"] = {"remote": {"port": "y"}}
    paths.settings_json.write_text(json.dumps(data), "utf-8")
    os.utime(paths.settings_json, (2, 2))
    result = await reload_settings_handler(PERSON, Empty(), core)
    assert result.changedKeys == []
    assert core.settings.degraded_sections() == ["web"]
    assert (await info_handler(PERSON, Empty(), core)).settingsDegraded == ["web"]
    assert (await web_status(core)).settingsDegraded == ["web"]
    core.settings.save(paths)
    assert json.loads(paths.settings_json.read_text("utf-8"))["web"] == {"remote": {"port": "y"}}


def _degraded(tmp_path: Path, section: str) -> tuple[Paths, Settings]:
    """A file whose ``section`` and ``web`` do not validate, loaded as the daemon would."""
    paths = Paths.create(tmp_path / "home")
    data = Settings().model_dump(mode="json")
    data[section] = 5
    data["web"] = {"remote": {"port": "x"}}
    paths.settings_json.write_text(json.dumps(data), "utf-8")
    settings = Settings.load(paths)
    assert settings.degraded_sections() == sorted([section, "web"])
    return paths, settings


def _after_save(paths: Paths, settings: Settings, section: str) -> Any:
    """The section as written; the in-place change cleared only its own mark."""
    assert settings.degraded_sections() == ["web"]
    on_disk = json.loads(paths.settings_json.read_text("utf-8"))
    assert on_disk["web"] == {"remote": {"port": "x"}}             # still raw
    assert on_disk[section] == settings.model_dump(mode="json")[section]
    return on_disk[section]


def test_an_in_place_provider_change_is_written(tmp_path: Path) -> None:
    from snowpea_core.providers.registry import ProviderRegistry

    paths, settings = _degraded(tmp_path, "providers")
    registry = ProviderRegistry(settings, paths)
    registry.configure("openai", {"api_key": "sk-test"})
    registry.save()
    assert _after_save(paths, settings, "providers")["openai"]["api_key"] == "sk-test"


def test_an_in_place_allowlist_change_is_written(tmp_path: Path) -> None:
    from snowpea_core.permissions.allowlist import Allowlist

    paths, settings = _degraded(tmp_path, "allowlist")
    Allowlist(paths, settings).add("^ls$", scope="global")
    assert [e["pattern"] for e in _after_save(paths, settings, "allowlist")] == ["^ls$"]


async def test_an_in_place_handler_change_is_written(tmp_path: Path) -> None:
    from snowpea_core.server.extension_handlers import _save

    paths, settings = _degraded(tmp_path, "extensions")
    settings.extensions["demo"] = {"enabled": False}
    await _save(SimpleNamespace(settings=settings, paths=paths))  # type: ignore[arg-type]
    assert _after_save(paths, settings, "extensions") == {"demo": {"enabled": False}}


async def test_an_in_place_jarvis_change_is_written(tmp_path: Path) -> None:
    from snowpea_core.jarvis.service import JarvisService

    paths, settings = _degraded(tmp_path, "jarvis")
    settings.jarvis.enabled = True
    await JarvisService._save_settings(  # type: ignore[arg-type]
        SimpleNamespace(core=SimpleNamespace(settings=settings, paths=paths)))
    assert _after_save(paths, settings, "jarvis")["enabled"] is True


async def test_an_in_place_slash_command_change_is_written(tmp_path: Path) -> None:
    from snowpea_core.commands.mode_cmd import cmd_busy

    paths, settings = _degraded(tmp_path, "agent")
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    ctx = SimpleNamespace(core=SimpleNamespace(settings=settings, paths=paths,
                                               mark_settings_saved=lambda: None), say=say)
    await cmd_busy(ctx, "queue")  # type: ignore[arg-type]
    assert _after_save(paths, settings, "agent")["busy"] == "queue"


def test_the_setup_wizard_writes_what_it_answered(tmp_path: Path) -> None:
    from snowpea_core.setup.state import WizardState

    paths, settings = _degraded(tmp_path, "search")
    WizardState.from_settings(settings).write(paths, settings)
    _after_save(paths, settings, "search")


def test_the_new_reason_is_enumerated() -> None:
    assert errors.DETAIL_REASONS["settings_unreadable"][0] == errors.INVALID_PARAMS


# -- F4: the last writers that followed links ----------------------------------------------


def _bait(tmp_path: Path) -> Path:
    bait = tmp_path / "bait"
    bait.write_text("untouched", encoding="utf-8")
    return bait


def test_the_saved_by_marker_never_follows_a_link(tmp_path: Path) -> None:
    from snowpea_core.server.skill_gate import SAVED_BY_FILE, mark_saved_by

    skill = tmp_path / "skill"
    skill.mkdir()
    bait = _bait(tmp_path)
    (skill / SAVED_BY_FILE).symlink_to(bait)
    mark_saved_by(SimpleNamespace(remote_login=True, authenticated=True, closed=False,
                                  client_kind="web"), skill)
    assert bait.read_text(encoding="utf-8") == "untouched"


def test_jarvis_files_never_follow_a_link(tmp_path: Path) -> None:
    from snowpea_core.jarvis.tools import TodoBook, _write_text

    bait = _bait(tmp_path)
    folder = tmp_path / "jarvis"
    folder.mkdir()
    (folder / "todos.json.tmp").symlink_to(bait)
    TodoBook(folder).save([{"id": "1", "text": "x"}])
    assert bait.read_text(encoding="utf-8") == "untouched"
    assert TodoBook(folder).load() == [{"id": "1", "text": "x"}]
    report = folder / "r.md"
    report.symlink_to(bait)
    for append in (True, False):
        with pytest.raises(OSError):
            _write_text(report, "x\n", append=append)
    assert bait.read_text(encoding="utf-8") == "untouched"


def test_the_jobs_log_never_follows_a_link(tmp_path: Path) -> None:
    from snowpea_core.scheduler.scheduler import Scheduler

    paths = Paths.create(tmp_path / "home")
    bait = _bait(tmp_path)
    paths.jobs_log.parent.mkdir(parents=True, exist_ok=True)
    paths.jobs_log.symlink_to(bait)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.core = SimpleNamespace(paths=paths)  # type: ignore[attr-defined]
    scheduler._append_log(SimpleNamespace(id="j1"), "log", "hello")  # type: ignore[arg-type]
    assert bait.read_text(encoding="utf-8") == "untouched"
