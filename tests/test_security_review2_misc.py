"""Security review, part 2: the smaller findings, unit by unit.

M1 approval diffs, M2 exposure, M3 wide roots, M5/L7 tmux text and
adoption checks, M7 the core sanitiser, L4 preset snapshots, L8 launch cwd,
H2 implicit modes and legacy settings, M6 mail read tools, M8 the npx pin.
Escape sequences appear only as string data here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_external_manager import Conn, Env, env  # noqa: F401 - the fixture

from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings, take_legacy_values
from snowpea_core.external import proc
from snowpea_core.external.claude import preview_diff
from snowpea_core.external.proc import ExternalError
from snowpea_core.external.timeline import TimelineBuilder, clean_payload, clean_text

OSC52 = "\x1b]52;c;aGk=\x07"
OSC8 = "\x1b]8;;https://example.invalid\x1b\\link\x1b]8;;\x1b\\"
CSI = "\x1b[2J\x1b[1;1H"


# -- M7 ----------------------------------------------------------------------------------


def test_core_strips_escape_sequences_from_external_text() -> None:
    assert clean_text(f"a{OSC52}b") == "ab"
    assert clean_text(f"see {OSC8}") == "see link"
    assert clean_text(f"{CSI}x\r\ny\tz\x9b1m") == "x\ny\tz"
    assert clean_payload({"t": [f"{CSI}q"], f"k{CSI}": 1}) == {"t": ["q"], "k": 1}
    builder = TimelineBuilder("claude", "s")
    builder.assistant(f"hello{OSC52}", key="k1")
    builder.tool_call("c1", f"Bash{CSI}", {"command": f"ls{OSC8}"}, key="k2")
    for item in builder.drain():
        assert "\x1b" not in json.dumps(item.payload), item.payload


async def test_rows_and_states_are_sanitised(env: Env) -> None:  # noqa: F811
    from snowpea_core.external.manager import _line, _preview

    assert _line(f"title{OSC52}") == "title"
    assert _preview(f"first{CSI}\nsecond") == "first"


# -- M1 ----------------------------------------------------------------------------------


def test_write_preview_reads_only_project_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    inside = project / "a.txt"
    inside.write_text("old line\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    env_file = project / ".env"
    env_file.write_text("API_KEY=xyz\n", encoding="utf-8")
    shown = preview_diff("Write", {"file_path": str(inside), "content": "new"}, str(project))
    assert shown is not None and "-old line" in shown
    for target in (outside, env_file):
        diff = preview_diff("Write", {"file_path": str(target), "content": "new"}, str(project))
        assert diff is not None and "not shown" in diff
        assert "TOP SECRET" not in diff and "xyz" not in diff
    relative = preview_diff("Write", {"file_path": "../secret.txt", "content": "n"}, str(project))
    assert relative is not None and "TOP SECRET" not in relative


# -- M2 ----------------------------------------------------------------------------------


def test_a_broken_settings_file_hides_the_project(tmp_path: Path) -> None:
    assert ProjectSettings.exposed(tmp_path) is True
    folder = tmp_path / ".snowpea"
    folder.mkdir()
    (folder / "settings.json").write_text("{not json", encoding="utf-8")
    assert ProjectSettings.exposed(tmp_path) is False
    (folder / "settings.json").write_text(json.dumps({"jarvis": {"expose": "maybe"}}),
                                          encoding="utf-8")
    assert ProjectSettings.exposed(tmp_path) is False
    (folder / "settings.json").write_text(json.dumps({"jarvis": {"expose": True}}),
                                          encoding="utf-8")
    assert ProjectSettings.exposed(tmp_path) is True


async def test_a_subfolder_of_a_hidden_project_is_hidden(env: Env, tmp_path: Path) -> None:  # noqa: F811
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".snowpea").mkdir()
    (parent / ".snowpea" / "settings.json").write_text(
        json.dumps({"jarvis": {"expose": False}}), encoding="utf-8")

    async def roots() -> list[str]:
        return [str(parent), str(child), str(Path.home()), "/"]

    async def exposed(root: str) -> bool:
        return ProjectSettings.exposed(root)

    env.manager._roots = roots
    env.manager._exposed = exposed
    assert await env.manager._exposed_roots() == []  # child under hidden parent; wide roots out


# -- M3 ----------------------------------------------------------------------------------


def test_wide_roots_match_only_themselves() -> None:
    home = str(Path.home().resolve())
    assert proc.project_of(f"{home}/some/other/repo", [home, "/"]) is None
    assert proc.project_of(home, [home]) == home


# -- M5 / L7 -------------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_process_identity_checks(tmp_path: Path) -> None:
    import os

    assert proc.process_runs(os.getpid(), sys.executable)
    assert not proc.process_runs(os.getpid(), "/bin/true")
    argv = proc.process_argv(os.getpid())
    assert argv and not any(proc.is_dangerous(arg) for arg in argv[:1])


# -- H2: implicit modes, legacy values --------------------------------------------------------


def test_legacy_values_load_safe_and_are_reported() -> None:
    take_legacy_values()
    settings = Settings.model_validate({"jarvis": {
        "delegation": {"default": "full"},
        "external": {"clis": {"codex": {"defaultMode": "unrestricted"}}}}})
    assert settings.jarvis.delegation.default == "staged"
    assert settings.jarvis.external.clis["codex"].defaultMode is None
    found = take_legacy_values()
    assert "jarvis.delegation.default=full" in found
    assert any("defaultMode=unrestricted" in item for item in found)


async def test_a_continued_session_never_comes_back_unrestricted(env: Env) -> None:  # noqa: F811
    adapter = env.manager.registry.get("claude")
    record = SimpleNamespace(mode="unrestricted", adapter=adapter)
    assert env.manager._implicit_mode(record) != "unrestricted"  # type: ignore[arg-type]
    record.mode = "readonly"
    assert env.manager._implicit_mode(record) == "readonly"  # type: ignore[arg-type]
    env.settings.jarvis.external.clis["claude"].defaultMode = "unrestricted"  # forced in memory
    assert env.manager.default_mode(adapter) != "unrestricted"


async def test_only_a_person_sends_into_an_unrestricted_session(env: Env) -> None:  # noqa: F811
    person = Conn("ide")
    created = await env.manager.create("claude", str(env.project), mode="unrestricted",
                                       conn=person)
    ref = created["session"]["ref"]
    for other in (Conn("browser"), None):
        with pytest.raises(ExternalError) as refused:
            await env.manager.send(ref["cli"], ref["id"], "hi", conn=other)
        assert refused.value.code == "jarvis_person_only"
    await env.manager.close(ref["cli"], ref["id"], conn=person)


# -- L8 ------------------------------------------------------------------------------------


async def test_launch_cwd_is_the_validated_real_path(env: Env, tmp_path: Path) -> None:  # noqa: F811
    person = Conn("ide")
    created = await env.manager.create("claude", str(env.project), conn=person)
    ref = created["session"]["ref"]
    record = env.manager._records[(ref["cli"], ref["id"])]
    assert record.real_cwd == str(env.project.resolve())
    await env.manager.close(ref["cli"], ref["id"], conn=person)
    # The transcript's folder moved under it (a link now points elsewhere): refused.
    record.real_cwd = str(tmp_path / "somewhere-else")
    found = await env.manager.discover("claude")
    assert found is not None
    with pytest.raises(ExternalError):
        await env.manager._start_channel(record, "default", None, found, resume=True)


# -- M6 / M8 -----------------------------------------------------------------------------------


def test_mail_worker_tools_and_the_npx_pin() -> None:
    from snowpea_core.tools import mcp_catalog

    preset = mcp_catalog.preset("microsoft365")
    assert preset is not None
    server = preset["servers"][0]
    for name in ("get-mail-message-mime", "get-mail-attachment-content", "download-bytes"):
        assert name not in server["readTools"] and name in server["sendTools"]
    args = server["entry"]["args"]
    assert args[:2] == ["-y", "--ignore-scripts"] and "@" in args[2]
    assert "--ignore-scripts" in server["loginCommand"]
    assert "community" in preset["description"]


# -- L4 -----------------------------------------------------------------------------------------


def test_preset_snapshots_keep_references_not_values(tmp_path: Path) -> None:
    from snowpea_core.config.credentials import CredentialStore
    from snowpea_core.config.paths import Paths
    from snowpea_core.jarvis import presets

    paths = Paths.create(tmp_path / "home")
    paths.ensure()
    core: Any = SimpleNamespace(paths=paths)
    store = CredentialStore(paths)
    ref = presets._backup_key("google", "CLIENT_SECRET")
    store.save({"CLIENT_SECRET": "new-value", ref: "old-value"})
    presets._restore_credentials(core, {"CLIENT_SECRET": {"ref": ref}})
    data = store.load()
    assert data["CLIENT_SECRET"] == "old-value" and ref not in data
    presets._restore_credentials(core, {"CLIENT_SECRET": None})
    assert "CLIENT_SECRET" not in store.load()
