"""Security review 5: project trust gates ONLY what comes from the project folder.

User-level hooks — snowpea's global ``$SNOWPEA_HOME/hooks.json`` and every plugin
installed under ``$SNOWPEA_HOME/plugins`` (the bundled ``snowpea-browser`` guard
among them) — keep running in an UNTRUSTED project, a browser-hosted session
there included.  Hooks are never executed here: ``run_hook_output`` is a spy
that records the command and starts nothing.  Hook commands are harmless data.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server.app_server import Core
from snowpea_core.trust import projects as trust

pytestmark = pytest.mark.real_project_trust

GUARD = "guard-marker {where}"


def _hooks(path: Path, command: str, *, fail_closed: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    spec: dict[str, Any] = {"type": "command", "command": command}
    if fail_closed:
        spec["failClosed"] = True
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [spec]}]}}),
                    encoding="utf-8")


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Core, Path, Any]:
    from snowpea_core.skills.loader import SkillLoader

    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    trust.set_default_home(core.paths.home)
    home = core.paths.home
    plugin = home / "plugins" / "snowpea-plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "snowpea-browser", "version": "1.0.0"}), encoding="utf-8")
    _hooks(plugin / "hooks" / "hooks.json", GUARD.format(where="bundled-browser"),
           fail_closed=True)
    other = home / "plugins" / "other-plugin"
    (other / ".claude-plugin").mkdir(parents=True)
    (other / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "other-plugin"}), encoding="utf-8")
    _hooks(other / "hooks" / "hooks.json", GUARD.format(where="installed-plugin"))
    _hooks(home / "hooks.json", GUARD.format(where="user-global"))
    project = tmp_path / "untrusted"
    _hooks(project / ".snowpea" / "hooks.json", GUARD.format(where="project"))
    loader = SkillLoader(core, workdirs=[project])
    loader.scan()
    core.skills = loader
    return core, project, loader


def test_user_level_hooks_stay_active_in_an_untrusted_project(setup: Any) -> None:
    core, project, loader = setup
    assert not trust.store_for(core.paths.home).active(project, "hooks", ".snowpea/hooks.json")
    commands = sorted(h.command for h in loader.hooks.for_tool("PreToolUse", "shell", project))
    assert commands == [GUARD.format(where="bundled-browser"),
                        GUARD.format(where="installed-plugin"),
                        GUARD.format(where="user-global")]
    # ... and the project's own hook is not among them until the person trusts it.
    trust.store_for(core.paths.home).set(project, ["hooks"], by="ide")
    loader.scan()
    assert GUARD.format(where="project") in {
        h.command for h in loader.hooks.for_tool("PreToolUse", "shell", project)}


async def test_the_browser_plugin_guard_fires_for_a_browser_session_there(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.skills import hooks as hooks_mod

    core, project, _loader = setup
    ran: list[str] = []

    async def spy(hook: Any, *_a: Any, **_k: Any) -> tuple[int, str, str]:
        ran.append(hook.command)
        code = hooks_mod.BLOCK_EXIT_CODE if "bundled-browser" in hook.command else 0
        return code, "", "blocked by the browser guard"

    monkeypatch.setattr(hooks_mod, "run_hook_output", spy)
    monkeypatch.setattr(hooks_mod, "tool_provider", lambda *_a: "host")
    session = SimpleNamespace(id="s-b", workdir=project, origin_surface="browser",
                              host_tools_from="profile-1", agent=None, parent_session_id=None,
                              browser_provider="host")
    monkeypatch.setattr(hooks_mod, "session_fields", lambda _s: {
        "host_tools_from": "profile-1", "browser_provider": "host"})
    blocked = await hooks_mod.pre_tool_use(core, session, "browser_click", {})
    assert blocked == "blocked by the browser guard"
    assert GUARD.format(where="bundled-browser") in ran
    assert GUARD.format(where="project") not in ran
