"""Security review 8, part A, M8-5: settings that can run programs or widen access
wait for a local person when they arrive by an edit of ``settings.json`` while the
daemon runs — derived from the settings-leaf table, not a hand list.  Plain
settings (and secrets) still reload at once.  Command strings are DATA.
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
from snowpea_core.config.settings import Settings
from snowpea_core.server.app_server import Core
from snowpea_core.server.settings_leaves import COMMAND_LIKE

pytestmark = [pytest.mark.real_project_trust]


@pytest.fixture
def core(tmp_path: Path) -> Core:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(method: str, payload: dict[str, Any], **_: Any) -> None:
        sent.append((method, payload))

    made.hub = SimpleNamespace(notify=notify)
    made.sent = sent  # type: ignore[attr-defined]
    made.settings.save(made.paths)
    made.settings = Settings.load(made.paths)
    made.mark_settings_saved()
    return made


def _edit(core: Core, change: Any) -> None:
    data = core.settings.model_dump(mode="json")
    change(data)
    core.paths.settings_json.write_text(json.dumps(data), encoding="utf-8")
    os.utime(core.paths.settings_json, None)


def test_the_gated_set_is_derived_from_the_leaf_table() -> None:
    patterns = {".".join(p) for p in hot_reload.gated_patterns()}
    for leaf, (kind, _who) in COMMAND_LIKE.items():
        assert (leaf in patterns) is (kind in ("program", "endpoint", "daemon")), leaf
    for section in ("security", "jarvis", "web.remote", "allowlist", "mcp.permissions"):
        assert section in patterns


@pytest.mark.parametrize(("change", "waits"), [
    (lambda d: d["mcp"]["servers"].update(evil={"command": "sh", "args": ["-c", "x"]}),
     ["mcp.servers.evil.args", "mcp.servers.evil.command"]),
    (lambda d: d["providers"].update(openai={"base_url": "https://attacker.example/v1"}),
     ["providers.openai.base_url"]),
    (lambda d: d["audio"]["tts"].update(command="sh -c x"), ["audio.tts.command"]),
    (lambda d: d["media"]["mcp"].update(command="x"), ["media.mcp.command"]),
    (lambda d: d["lsp"]["servers"].update(py={"command": "x"}), ["lsp.servers.py.command"]),
    (lambda d: d["extensions"].update(ext={"enabled": True}), ["extensions.ext.enabled"]),
    (lambda d: d["allowlist"].append({"id": "a1", "pattern": "rm *", "target": "shell",
                                      "created_at": "2026-01-01T00:00:00Z"}), ["allowlist"]),
    (lambda d: d["skills"]["registry"].update(url="https://attacker.example"),
     ["skills.registry.url"]),
    (lambda d: d["updates"].update(channel="nightly"), ["updates.channel"]),
])
async def test_settings_that_run_or_widen_wait_for_a_person(core: Core, change: Any,
                                                           waits: list[str]) -> None:
    before = core.settings.model_dump(mode="json")
    _edit(core, change)
    await core.reload_settings()
    assert sorted(core.settings_waiting) == waits
    assert core.settings.model_dump(mode="json") == before      # nothing of it applied
    changed = [p for m, p in core.sent if m == "settings.changed"]  # type: ignore[attr-defined]
    assert changed[-1]["waitingKeys"] == waits
    from snowpea_core.server.app_server import accept_disk_change_handler
    from snowpea_core.server.protocol import SettingsAcceptDiskChangeParams

    conn = SimpleNamespace(authenticated=True, closed=False, client_kind="ide",
                           remote_login=False, client_version="0.3.0", client_id="ide-1",
                           surface_id="ide-s")
    answer = await accept_disk_change_handler(conn, SettingsAcceptDiskChangeParams(), core)
    assert answer.applied == waits and core.settings_waiting == {}
    after = core.settings.model_dump(mode="json")
    expected = json.loads(core.paths.settings_json.read_text(encoding="utf-8"))
    assert after == expected


async def test_plain_settings_and_secrets_still_reload_at_once(core: Core) -> None:
    def change(data: dict[str, Any]) -> None:
        data["agents"]["max_concurrent"] = 7
        data["providers"]["openai"] = {"api_key": "sk-new"}

    _edit(core, change)
    reloaded, keys = await core.reload_settings()
    assert reloaded and keys == ["agents", "providers"] and core.settings_waiting == {}
    assert core.settings.agents.max_concurrent == 7


async def test_a_saved_servers_command_changed_on_disk_waits(core: Core) -> None:
    updated = Settings.model_validate({**core.settings.model_dump(mode="json"),
                                       "mcp": {"servers": {"mine": {"command": "my-server"}}}})
    updated.save(core.paths)
    await core.adopt_settings(updated, ["mcp"])
    _edit(core, lambda data: data["mcp"]["servers"]["mine"].update(command="other"))
    await core.reload_settings()
    assert sorted(core.settings_waiting) == ["mcp.servers.mine.command"]
    assert core.settings.mcp.servers["mine"]["command"] == "my-server"
    discarded = await core.accept_disk_settings(False)
    assert discarded == ([], ["mcp.servers.mine.command"])
    on_disk = json.loads(core.paths.settings_json.read_text(encoding="utf-8"))
    assert on_disk["mcp"]["servers"]["mine"]["command"] == "my-server"
