"""m21: ``security.unknownCode`` is migrated once from ``ask`` to ``auto`` (0.2.90).

Every settings.json written by 0.2.88/0.2.89 stores ``"unknownCode": "ask"``
(the old default was written out).  Owner decision: migrate once — at the first
daemon start of 0.2.90, a stored ``ask`` becomes ``auto`` and a marker under
``$SNOWPEA_HOME/security/`` makes sure it never happens again.  Everything here
runs in pytest's temporary folders.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config import hot_reload
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.security import enforce
from snowpea_core.server.app_server import Core

pytestmark = [pytest.mark.real_project_trust]


def _home(tmp_path: Path, security: Any) -> Paths:
    """A home whose settings.json looks like one an older snowpea wrote."""
    paths = Paths.create(tmp_path / "home")
    data = Settings().model_dump(mode="json")
    if security is None:
        data["security"].pop("unknownCode")
    else:
        data["security"] = security if isinstance(security, dict) else {
            **data["security"], "unknownCode": security}
    paths.settings_json.write_text(json.dumps(data), encoding="utf-8")
    return paths


def _stored(paths: Paths) -> Any:
    return json.loads(paths.settings_json.read_text(encoding="utf-8"))["security"].get(
        "unknownCode")


def _marker(paths: Paths) -> Path:
    return paths.home.joinpath(*enforce.UNKNOWN_CODE_MARKER)


def _start(paths: Paths) -> tuple[Settings, bool]:
    """What Daemon.start does with the file: load, migrate."""
    settings = Settings.load(paths)
    return settings, enforce.migrate_unknown_code(settings, paths)


def test_a_stored_ask_becomes_auto_once(tmp_path: Path) -> None:
    paths = _home(tmp_path, "ask")
    settings, changed = _start(paths)
    assert changed and settings.security.unknownCode == "auto"
    assert _stored(paths) == "auto" and _marker(paths).is_file()
    record = json.loads(_marker(paths).read_text(encoding="utf-8"))
    assert record["from"] == "ask" and record["to"] == "auto"
    # The second start changes nothing.
    settings, changed = _start(paths)
    assert not changed and settings.security.unknownCode == "auto"


def test_a_person_who_sets_ask_later_keeps_it(tmp_path: Path) -> None:
    paths = _home(tmp_path, "ask")
    _start(paths)
    settings = Settings.load(paths)
    settings.security.unknownCode = "ask"
    settings.save(paths)
    settings, changed = _start(paths)
    assert not changed and settings.security.unknownCode == "ask" and _stored(paths) == "ask"


@pytest.mark.parametrize("value", ["allow", "auto", None])
def test_other_values_only_write_the_marker(tmp_path: Path, value: Any) -> None:
    paths = _home(tmp_path, value)
    before = paths.settings_json.read_bytes()
    _settings, changed = _start(paths)
    assert not changed and paths.settings_json.read_bytes() == before
    assert _marker(paths).is_file()


def test_a_degraded_security_section_is_left_alone(tmp_path: Path) -> None:
    paths = _home(tmp_path, {"blockedCommands": "not an object", "unknownCode": "ask"})
    before = paths.settings_json.read_bytes()
    settings, changed = _start(paths)
    assert "security" in settings.degraded_sections()
    assert not changed and settings.security.unknownCode == "ask"
    assert paths.settings_json.read_bytes() == before and not _marker(paths).exists()


def test_an_unreadable_file_is_left_alone(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    paths.settings_json.write_text("{ not json", encoding="utf-8")
    settings, changed = _start(paths)
    assert not changed and settings.security.unknownCode == "ask"
    assert paths.settings_json.read_text(encoding="utf-8") == "{ not json"
    assert not _marker(paths).exists()


def test_a_project_file_is_never_touched(tmp_path: Path) -> None:
    paths = _home(tmp_path, "ask")
    project = tmp_path / "proj" / ".snowpea"
    project.mkdir(parents=True)
    (project / "settings.json").write_text('{"security": {"unknownCode": "ask"}}',
                                           encoding="utf-8")
    _start(paths)
    assert json.loads((project / "settings.json").read_text())["security"]["unknownCode"] \
        == "ask"


async def test_no_disk_wait_and_no_tamper_line(tmp_path: Path) -> None:
    paths = _home(tmp_path, "ask")
    settings, changed = _start(paths)
    assert changed
    # As Daemon.start: the stamp is taken after the migration's own save.
    core = Core(settings=settings, paths=paths, token="t")
    core.unknown_code_migrated = changed
    core.settings_stamp = hot_reload.stamp(paths.settings_json)
    core.hub = SimpleNamespace(notify=_ignore)
    assert not core.settings_file_changed()
    assert await core.reload_settings() == (False, [])
    assert core.settings_waiting == {}
    security_log = paths.logs_dir / "security.jsonl"
    assert not security_log.exists() or "tamper" not in security_log.read_text()


async def test_security_get_says_it_once(tmp_path: Path) -> None:
    from snowpea_core.server.protocol import SecurityGetParams
    from snowpea_core.server.security_handlers import get_handler

    paths = _home(tmp_path, "ask")
    settings, changed = _start(paths)
    core = Core(settings=settings, paths=paths, token="t")
    core.unknown_code_migrated = changed
    conn = SimpleNamespace(authenticated=True, closed=False, client_kind="ide",
                           remote_login=False, client_version="0.3.0", client_id="ide-1",
                           surface_id="ide-s")
    result = await get_handler(conn, SecurityGetParams(), core)
    assert result.unknownCode == "auto"
    assert enforce.UNKNOWN_CODE_MIGRATED_NOTE in result.problems
    # Once the person chose a value, it is not said any more.
    core.settings.security.unknownCode = "ask"
    result = await get_handler(conn, SecurityGetParams(), core)
    assert enforce.UNKNOWN_CODE_MIGRATED_NOTE not in result.problems


@pytest.mark.parametrize("line", [
    "rm ~/.snowpea/security/unknown-code-migrated.json",
    "echo '{}' > ~/.snowpea/security/unknown-code-migrated.json",
    "mv ~/.snowpea/security /tmp/s",
])
def test_no_tool_removes_the_marker(line: str, tmp_path: Path) -> None:
    from snowpea_core.security.check import Context, check_command
    from snowpea_core.security.lists import from_raw

    user = tmp_path / "u"
    ctx = Context(workdir=tmp_path, user_home=user, home=user / ".snowpea",
                  projects=(), mounts=(), env_venv=False, unknown_code="allow")
    verdict = check_command(line, from_raw({}), ctx, cwd=tmp_path)
    assert verdict.blocked, line


async def _ignore(*_args: Any, **_kwargs: Any) -> None:
    return None
