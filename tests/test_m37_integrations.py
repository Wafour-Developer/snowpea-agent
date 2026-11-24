"""M37 §2.7: CLI hook integrations install / uninstall / status.

Every test works in a throwaway HOME / SNOWPEA_HOME under ``tmp_path`` via
:class:`manager.Env`; nothing reads or writes the real ``~/.claude`` etc.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from snowpea_core.terminal.integrations import jsonedit, manager, tomledit
from snowpea_core.terminal.integrations.fileio import ConfigFileError

CLAUDE_SETTINGS = """{
    "model": "opus",
    "permissions": {
        "allow": ["Bash(ls:*)"]
    },
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "notify-send done"
                    }
                ]
            }
        ]
    }
}
"""

CODEX_TOML = """model = "gpt-5"

# my features
[features]
web_search = true

[profiles.fast]
model = "gpt-5-mini"
"""

CURSOR_HOOKS = """{
  "version": 1,
  "hooks": {
    "afterFileEdit": [{ "command": "./fmt.sh" }]
  }
}
"""

AGY_HOOKS = """{
  "mine": {
    "PreInvocation": [
      {
        "type": "command",
        "command": "echo hi"
      }
    ]
  }
}
"""


def make_env(tmp_path, *, windows=False, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environ = {"HOME": str(home), "USERPROFILE": str(home), "PATH": ""}
    environ.update(extra)
    return manager.Env(environ=environ, snowpea_home=tmp_path / "sp", windows=windows)


def cfg(env, cli):
    path = env.config_dir(cli)
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- claude ------------------------------------------------------------------------


def test_claude_install_idempotent_and_uninstall_restores_bytes(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    settings.write_text(CLAUDE_SETTINGS)
    os.chmod(settings, 0o640)

    result = manager.install("claude", env)
    first = settings.read_text()
    assert str(settings) in result.changed
    value = json.loads(first)
    stop_commands = [h["command"] for g in value["hooks"]["Stop"] for h in g["hooks"]]
    assert "notify-send done" in stop_commands
    assert manager.hook_command(env, "claude", "done") in stop_commands
    session = value["hooks"]["SessionStart"][0]
    assert session["matcher"] == manager.CLAUDE_SESSION_MATCHER
    assert value["hooks"]["Notification"][0]["matcher"] == manager.CLAUDE_BLOCKED_MATCHER
    # Formatting kept: four-space indent, the person's lines untouched.
    assert first.startswith('{\n    "model": "opus",\n    "permissions": {\n')
    assert '\n        "UserPromptSubmit": [\n            {\n' in first
    assert stat.S_IMODE(os.stat(settings).st_mode) == 0o640
    assert result.backups and open(result.backups[0]).read() == CLAUDE_SETTINGS
    assert manager.status("claude", env).status == manager.STATUS_CURRENT

    again = manager.install("claude", env)
    assert settings.read_text() == first
    assert str(settings) not in again.changed

    manager.uninstall("claude", env)
    assert settings.read_text() == CLAUDE_SETTINGS
    assert stat.S_IMODE(os.stat(settings).st_mode) == 0o640
    assert manager.status("claude", env).status == manager.STATUS_NOT_INSTALLED
    assert not env.script_path().exists()


def test_claude_missing_settings_is_created_then_removed(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    manager.install("claude", env)
    assert settings.exists()
    assert json.loads(settings.read_text())["hooks"]["Stop"]
    script = env.script_path()
    assert script.exists() and os.access(script, os.X_OK)
    assert manager.script_version(script.read_text()) == manager.INTEGRATION_VERSION
    manager.uninstall("claude", env)
    assert not settings.exists()


def test_claude_config_dir_env_is_honoured(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = make_env(tmp_path, CLAUDE_CONFIG_DIR=str(other))
    manager.install("claude", env)
    assert (other / "settings.json").exists()
    assert not (tmp_path / "home" / ".claude").exists()
    row = manager.status("claude", env)
    assert row.configDir == str(other) and row.status == manager.STATUS_CURRENT


def test_claude_missing_dir_is_an_error(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(manager.IntegrationError, match="not found"):
        manager.install("claude", env)


def test_status_modified_and_outdated(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    manager.install("claude", env)
    text = settings.read_text()
    edited = text.replace('"timeout": 10', '"timeout": 99', 1)
    settings.write_text(edited)
    assert manager.status("claude", env).status == manager.STATUS_MODIFIED
    # Reinstall repairs it.
    manager.install("claude", env)
    assert manager.status("claude", env).status == manager.STATUS_CURRENT
    # A missing event = outdated.
    settings.write_text(jsonedit.remove_member(settings.read_text(), ("hooks",), "Notification"))
    assert manager.status("claude", env).status == manager.STATUS_OUTDATED
    manager.install("claude", env)
    # An older script = outdated; an edited one = modified.
    script = env.script_path()
    script.write_text(script.read_text().replace("VERSION=1", "VERSION=0"))
    assert manager.status("claude", env).status == manager.STATUS_OUTDATED
    script.write_text(manager._asset(False) + "# mine\n")
    assert manager.status("claude", env).status == manager.STATUS_MODIFIED


def test_uninstall_keeps_the_persons_later_edits(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    settings.write_text(CLAUDE_SETTINGS)
    manager.install("claude", env)
    text = settings.read_text().replace('"model": "opus"', '"model": "sonnet"')
    settings.write_text(text)
    manager.uninstall("claude", env)
    assert settings.read_text() == CLAUDE_SETTINGS.replace('"opus"', '"sonnet"')


def test_compact_config_stays_compact(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    original = ('{"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", '
                '"command": "x"}]}]}}')
    settings.write_text(original)
    manager.install("claude", env)
    out = settings.read_text()
    assert "\n" not in out
    manager.uninstall("claude", env)
    assert settings.read_text() == original


def test_hard_link_is_refused(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    settings.write_text(CLAUDE_SETTINGS)
    os.link(settings, tmp_path / "second-name.json")
    with pytest.raises(ConfigFileError, match="hard links"):
        manager.install("claude", env)
    assert settings.read_text() == CLAUDE_SETTINGS


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_config_writes_the_target(tmp_path):
    env = make_env(tmp_path)
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(CLAUDE_SETTINGS)
    link = cfg(env, "claude") / "settings.json"
    link.symlink_to(real)
    manager.install("claude", env)
    assert link.is_symlink()
    assert "UserPromptSubmit" in real.read_text()
    manager.uninstall("claude", env)
    assert link.is_symlink() and real.read_text() == CLAUDE_SETTINGS


def test_invalid_json_is_refused(tmp_path):
    env = make_env(tmp_path)
    settings = cfg(env, "claude") / "settings.json"
    settings.write_text('{"a": 1, // comment\n}')
    with pytest.raises(manager.IntegrationError):
        manager.install("claude", env)
    settings.write_text('{"a": 1, "a": 2}')
    with pytest.raises(manager.IntegrationError, match="duplicate"):
        manager.install("claude", env)


# -- codex -------------------------------------------------------------------------


def test_codex_install_uninstall_restores_both_files(tmp_path):
    env = make_env(tmp_path)
    base = cfg(env, "codex")
    (base / "config.toml").write_text(CODEX_TOML)
    manager.install("codex", env)
    toml = (base / "config.toml").read_text()
    assert "[features]\nhooks = true\nweb_search = true\n" in toml
    assert tomledit.hooks_enabled(toml)
    hooks = json.loads((base / "hooks.json").read_text())
    assert [g["hooks"][0]["command"] for g in hooks["hooks"]["SessionStart"]] == [
        manager.hook_command(env, "codex", "session")]
    assert manager.status("codex", env).status == manager.STATUS_CURRENT
    snapshot = ((base / "hooks.json").read_text(), toml)
    manager.install("codex", env)
    assert ((base / "hooks.json").read_text(), (base / "config.toml").read_text()) == snapshot

    manager.uninstall("codex", env)
    assert (base / "config.toml").read_text() == CODEX_TOML
    assert not (base / "hooks.json").exists()


def test_codex_feature_flag_variants(tmp_path):
    env = make_env(tmp_path)
    base = cfg(env, "codex")
    for original in ("", 'model = "x"', 'model = "x"\n', "[features]\nhooks = false\n",
                     'features.hooks = false\nmodel = "x"\n'):
        (base / "config.toml").write_text(original)
        manager.install("codex", env)
        assert tomledit.hooks_enabled((base / "config.toml").read_text())
        manager.uninstall("codex", env)
        assert (base / "config.toml").read_text() == original, original


def test_codex_flag_the_person_set_is_left_on(tmp_path):
    env = make_env(tmp_path)
    base = cfg(env, "codex")
    original = "[features]\nhooks = true\n"
    (base / "config.toml").write_text(original)
    manager.install("codex", env)
    manager.uninstall("codex", env)
    assert (base / "config.toml").read_text() == original


def test_codex_home_env_and_flag_off_is_modified(tmp_path):
    other = tmp_path / "codex-home"
    other.mkdir()
    env = make_env(tmp_path, CODEX_HOME=str(other))
    manager.install("codex", env)
    assert (other / "hooks.json").exists() and (other / "config.toml").exists()
    (other / "config.toml").write_text("[features]\nhooks = false\n")
    assert manager.status("codex", env).status == manager.STATUS_MODIFIED


def test_codex_nested_features_table_is_not_ours(tmp_path):
    text = "[profiles.a.features]\nhooks = false\n"
    out, change = tomledit.enable_hooks(text)
    assert out.startswith(text) and out.endswith("[features]\nhooks = true\n")
    assert tomledit.undo(out, change) == text


# -- cursor ------------------------------------------------------------------------


def test_cursor_roundtrip_and_shape(tmp_path):
    env = make_env(tmp_path)
    hooks = cfg(env, "cursor") / "hooks.json"
    hooks.write_text(CURSOR_HOOKS)
    manager.install("cursor", env)
    value = json.loads(hooks.read_text())
    session = manager.hook_command(env, "cursor", "session")
    assert value["hooks"]["sessionStart"] == [{"command": session}]
    assert value["hooks"]["stop"] == [{"command": manager.hook_command(env, "cursor", "done")}]
    assert value["hooks"]["afterFileEdit"] == [{"command": "./fmt.sh"}]
    assert manager.status("cursor", env).status == manager.STATUS_CURRENT
    first = hooks.read_text()
    manager.install("cursor", env)
    assert hooks.read_text() == first
    manager.uninstall("cursor", env)
    assert hooks.read_text() == CURSOR_HOOKS


def test_cursor_version_key_added_and_removed(tmp_path):
    env = make_env(tmp_path, CURSOR_CONFIG_DIR=str(tmp_path / "cur"))
    (tmp_path / "cur").mkdir()
    hooks = tmp_path / "cur" / "hooks.json"
    hooks.write_text('{\n  "hooks": {}\n}\n')
    manager.install("cursor", env)
    assert json.loads(hooks.read_text())["version"] == 1
    manager.uninstall("cursor", env)
    assert hooks.read_text() == '{\n  "hooks": {}\n}\n'


# -- agy ---------------------------------------------------------------------------


def test_agy_block_roundtrip(tmp_path):
    env = make_env(tmp_path)
    hooks = cfg(env, "agy") / "hooks.json"
    hooks.write_text(AGY_HOOKS)
    manager.install("agy", env)
    value = json.loads(hooks.read_text())
    assert value["mine"] == json.loads(AGY_HOOKS)["mine"]
    handler = value[manager.AGY_BLOCK]["PreInvocation"][0]
    assert handler == {"type": "command", "command": manager.hook_command(env, "agy", "session"),
                       "timeout": 10}
    assert manager.status("agy", env).status == manager.STATUS_CURRENT
    first = hooks.read_text()
    manager.install("agy", env)
    assert hooks.read_text() == first
    hooks.write_text(first.replace('"timeout": 10', '"timeout": 5'))
    assert manager.status("agy", env).status == manager.STATUS_MODIFIED
    manager.uninstall("agy", env)
    assert hooks.read_text() == AGY_HOOKS
    assert env.config_dir("agy") == tmp_path / "home" / ".gemini" / "config"


def test_windows_command_strings(tmp_path):
    env = make_env(tmp_path, windows=True)
    claude = manager.hook_command(env, "claude", "done")
    assert claude.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File \"")
    assert claude.endswith('snowpea-agent-state.ps1" claude done')
    agy = manager.hook_command(env, "agy", "session")
    encoded = agy.rsplit(" ", 1)[1]
    import base64

    script = base64.b64decode(encoded).decode("utf-16-le")
    assert script.startswith("& '") and script.endswith("snowpea-agent-state.ps1' agy session")


# -- shared script / state ------------------------------------------------------------


def test_shared_script_stays_until_last_uninstall(tmp_path):
    env = make_env(tmp_path)
    cfg(env, "claude")
    cfg(env, "cursor")
    manager.install("claude", env)
    manager.install("cursor", env)
    state = json.loads(env.state_path().read_text())
    assert set(state["clis"]) == {"claude", "cursor"}
    manager.uninstall("claude", env)
    assert env.script_path().exists()
    manager.uninstall("cursor", env)
    assert not env.script_path().exists()
    assert json.loads(env.state_path().read_text())["clis"] == {}


def test_status_all_and_unknown_cli(tmp_path):
    env = make_env(tmp_path)
    rows = manager.status_all(env)
    assert [r.cli for r in rows] == list(manager.CLIS)
    assert all(r.status == manager.STATUS_NOT_INSTALLED for r in rows)
    with pytest.raises(manager.IntegrationError):
        manager.install("vim", env)
    assert manager.resolve_targets("all") == list(manager.CLIS)


def test_script_assets_carry_markers():
    for windows in (False, True):
        text = manager._asset(windows)
        assert "SNOWPEA_INTEGRATION_ID=snowpea-agent-state" in text
        assert manager.script_version(text) == manager.INTEGRATION_VERSION
        assert "SNOWPEA_TERMINAL_ID" in text
        assert "Apache-2.0" in text
