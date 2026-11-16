"""Security review 6, H-1 (+ M-7): snowpea's own control plane, always on.

Every command line and every piece of code below is DATA.  It is handed to the
pure checker (``check_command`` / ``check_call`` / ``control_plane_effect``),
which parses it and never runs anything.  ``/home/u`` is fictional.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.security import check as check_mod
from snowpea_core.security import control_plane as cp
from snowpea_core.security import written
from snowpea_core.security.check import (
    CONTROL_REASON,
    Context,
    check_call,
    check_command,
    check_path,
)
from snowpea_core.security.lists import Lists

USER = "/home/u"
HOME = f"{USER}/.snowpea"
WORK = f"{USER}/proj"
ESSENTIAL = Lists(command_presets=("essential",), path_presets=("essential",))
#: No preset, no entry: only the always-on rule is left.
NOTHING = Lists()


@pytest.fixture(autouse=True)
def _fresh() -> None:
    written.reset()


def _ctx(**changes: object) -> Context:
    values: dict[str, object] = {
        "workdir": Path(WORK), "user_home": Path(USER), "home": Path(HOME),
        "projects": (WORK,), "mounts": (), "env_venv": False,
    }
    values.update(changes)
    return Context(**values)  # type: ignore[arg-type]


def _state(line: str, lists: Lists = NOTHING) -> str:
    verdict = check_command(line, lists, _ctx(), cwd=Path(WORK))
    return "blocked" if verdict.blocked else "opaque" if verdict.opaque else "allowed"


# (line, verdict) — judged with EMPTY lists: nothing but the control plane.
SHELL = [
    # (a) the CLI: asks a person in every mode.
    ("snowpea project trust .", "opaque"),
    ("snowpea project trust /home/u/proj --yes", "opaque"),
    ("/home/u/.local/bin/snowpea project trust .", "opaque"),
    ("uv run snowpea project trust .", "opaque"),
    ("uvx snowpea project trust .", "opaque"),
    ("npx snowpea project trust .", "opaque"),
    ("python -m snowpea_core.cli project trust .", "opaque"),
    ("python3 -m snowpea_core project trust .", "opaque"),
    ("env FOO=1 snowpea daemon status", "opaque"),
    ("bash -c 'snowpea project trust .'", "opaque"),
    ("""python3 -c "import subprocess; subprocess.run(['snowpea', 'project', 'trust', '.'])\"""",
     "opaque"),
    ("""python3 -c "from snowpea_core.cli import main; main()\"""", "opaque"),
    # (b) the trust store and the other control files: never written.
    ("printf x > /home/u/.snowpea/trust/projects.json", "blocked"),
    ("echo '{}' > ~/.snowpea/trust/projects.json", "blocked"),
    ("echo '{}' > $HOME/.snowpea/trust/migrated.json", "blocked"),
    ("rm ~/.snowpea/trust/migrated.json", "blocked"),
    ("rm -rf ~/.snowpea/trust", "blocked"),
    ("mv ~/.snowpea/trust /tmp/t", "blocked"),
    ("chmod 777 ~/.snowpea/token", "blocked"),
    ("cp evil.json ~/.snowpea/credentials.json", "blocked"),
    ("tee ~/.snowpea/daemon.json < x", "blocked"),
    ("sqlite3 x.db > ~/.snowpea/state.db", "blocked"),
    ("truncate -s 0 ~/.snowpea/jarvis/delegation.db", "blocked"),
    ("echo x >> ~/.snowpea/jarvis/ledger.jsonl", "blocked"),
    ("rm ~/.snowpea/logs/security.jsonl", "blocked"),
    ("rm -rf ~/.snowpea/logs", "blocked"),
    ("echo x > ~/.snowpea/security/written.log", "blocked"),
    ("rm -rf ~/.snowpea", "blocked"),
    ("mv ~/.snowpea ~/.snowpea.bak", "blocked"),
    ("ln -sf /tmp/x ~/.snowpea/token", "blocked"),
    ("""python3 -c "open('/home/u/.snowpea/trust/projects.json', 'w').write('{}')\"""",
     "blocked"),
    ("""python3 -c "from pathlib import Path; (Path.home() / '.snowpea' / 'trust' / """
     """'projects.json').write_text('{}')\"""", "blocked"),
    ("""python3 -c "import os; os.remove(os.path.expanduser('~/.snowpea/trust/migrated.json'))\"""",
     "blocked"),
    ("""node -e "require('fs').writeFileSync('/home/u/.snowpea/trust/projects.json', '{}')\"""",
     "blocked"),
    # (c) the secrets: never read.
    ("cat /home/u/.snowpea/daemon.json", "blocked"),
    ("cat ~/.snowpea/token", "blocked"),
    ("head -c 64 ~/.snowpea/credentials.json", "blocked"),
    ("cat ~/.snowpea/tok*", "blocked"),
    ("cat ~/.snowpea/mcp-oauth.json", "blocked"),
    ("jq .token ~/.snowpea/daemon.json", "blocked"),
    ("cp ~/.snowpea/daemon.json /tmp/d", "blocked"),
    ("base64 < ~/.snowpea/token", "blocked"),
    ("grep -r token ~/.snowpea", "blocked"),
    ("tar czf /tmp/s.tgz ~/.snowpea", "blocked"),
    ("""python3 -c "print(open('/home/u/.snowpea/daemon.json').read())\"""", "blocked"),
    ("""python3 -c "import json, os; print(json.load(open(os.path.expanduser("""
     """'~/.snowpea/daemon.json')))['token'])\"""", "blocked"),
    ("""python3 -c "from pathlib import Path; print((Path.home() / '.snowpea' / 'token')"""
     """.read_text())\"""", "blocked"),
    ("""node -e "console.log(require('fs').readFileSync('/home/u/.snowpea/token', 'utf8'))\"""",
     "blocked"),
    # snowpea's own executable content: a person decides.
    ("echo x >> ~/.snowpea/skills/a/SKILL.md", "opaque"),
    ("cp -r evil ~/.snowpea/plugins/evil", "opaque"),
    ("echo '{}' > ~/.snowpea/hooks.json", "opaque"),
    ("mkdir -p ~/.snowpea/extensions/x", "opaque"),
    # Code that names a control file without plainly only reading it.
    ("""python3 -c "import numpy; numpy.save('/home/u/.snowpea/settings.json', 1)\"""",
     "opaque"),
    # Working state and ordinary reads stay as they were.
    ("echo plan > ~/.snowpea/plans/p.md", "allowed"),
    ("ls ~/.snowpea/plans", "allowed"),
    ("cat ~/.snowpea/settings.json", "allowed"),
    ("tail -n 50 ~/.snowpea/logs/daemon.log", "allowed"),
    ("cat ~/.snowpea/sessions/2026-11-16_s1/tmp/out.txt", "allowed"),
    ("echo x > ~/.snowpea/sessions/2026-11-16_s1/tmp/out.txt", "allowed"),
    ("""python3 -c "print(open('/home/u/.snowpea/settings.json').read())\"""", "allowed"),
    ("ls", "allowed"),
    ("rm -rf build", "allowed"),
    ("cat token", "allowed"),  # a file of that name in the project is not the daemon's
    ("echo x > credentials.json", "allowed"),
]


@pytest.mark.parametrize(("line", "expected"), SHELL)
def test_control_plane_shell_pairs(line: str, expected: str) -> None:
    assert _state(line) == expected, line


def test_shell_write_to_the_settings_file_stays_blocked() -> None:
    assert _state("echo '{}' > ~/.snowpea/settings.json") == "blocked"
    assert _state("sed -i s/ask/allow/ ~/.snowpea/settings.json") == "blocked"


def test_the_rule_is_on_whatever_the_lists_say() -> None:
    line = "printf x > /home/u/.snowpea/trust/projects.json"
    for lists in (NOTHING, ESSENTIAL, Lists(path_presets=("snowpea-home",))):
        assert check_command(line, lists, _ctx()).blocked
    verdict = check_command("cat ~/.snowpea/token", NOTHING, _ctx(locale="ko"))
    assert verdict.blocked and verdict.by == "security" and "snowpea" in (verdict.why or "")
    assert verdict.kind == "control"


def test_the_cli_question_cannot_be_switched_off() -> None:
    for code in ("ask", "allow"):
        verdict = check_command("snowpea project trust .", ESSENTIAL, _ctx(unknown_code=code))
        assert verdict.opaque and not verdict.blocked
        assert verdict.reason == CONTROL_REASON
        assert "snowpea-cli" in verdict.unseen
        assert "CLI" in (verdict.why or "")


def test_the_users_default_home_is_protected_beside_the_daemons() -> None:
    ctx = _ctx(home=Path("/srv/other-home"))
    assert check_command("cat /srv/other-home/token", NOTHING, ctx).blocked
    assert check_command("cat ~/.snowpea/token", NOTHING, ctx).blocked


def test_a_symlink_into_the_home_is_judged_where_it_lands(tmp_path: Path) -> None:
    home = tmp_path / "home" / ".snowpea"
    (home / "trust").mkdir(parents=True)
    (home / "token").write_text("t", encoding="utf-8")
    work = tmp_path / "proj"
    work.mkdir()
    (work / "t").symlink_to(home / "token")
    (work / "store").symlink_to(home / "trust")
    ctx = Context(workdir=work, user_home=tmp_path / "home", home=home, projects=(str(work),),
                  mounts=(), env_venv=False)
    assert check_command("cat t", NOTHING, ctx, cwd=work).blocked
    assert check_command("echo x > store/projects.json", NOTHING, ctx, cwd=work).blocked


# -- the file tools go through the same table ---------------------------------------

TOOLS = [
    ("write_file", {"path": f"{HOME}/trust/projects.json", "content": "{}"}, ("config",), True),
    ("write_file", {"path": f"{HOME}/daemon.json", "content": "{}"}, ("config",), True),
    ("write_file", {"path": f"{HOME}/token", "content": "x"}, ("config",), True),
    ("edit_file", {"path": f"{HOME}/jarvis/presets.json", "old_string": "a",
                   "new_string": "b"}, ("config",), True),
    ("delete_file", {"path": f"{HOME}/trust/migrated.json"}, ("config",), True),
    ("read_file", {"path": f"{HOME}/daemon.json"}, ("read",), True),
    ("read_file", {"path": f"{HOME}/token"}, ("secret",), True),
    ("read_file", {"path": f"{HOME}/credentials.json"}, ("secret",), True),
    ("read_file", {"path": "~/.snowpea/mcp-oauth.json"}, ("secret",), True),
    ("grep", {"pattern": "token", "path": HOME}, ("read",), True),
    # Not blocked: the file tools already ask for these (``config``), or they are reads.
    ("read_file", {"path": f"{HOME}/settings.json"}, ("read",), False),
    ("read_file", {"path": f"{HOME}/logs/daemon.log"}, ("read",), False),
    ("write_file", {"path": f"{HOME}/skills/a/SKILL.md", "content": "x"}, ("config",), False),
    ("write_file", {"path": f"{HOME}/plans/p.md", "content": "x"}, ("write",), False),
    ("write_file", {"path": "src/a.py", "content": "x"}, ("write",), False),
    ("read_file", {"path": "token"}, ("read",), False),
]


@pytest.mark.parametrize(("tool", "args", "tags", "blocked"), TOOLS)
def test_control_plane_tool_pairs(tool: str, args: dict[str, object], tags: tuple[str, ...],
                                  blocked: bool) -> None:
    verdict = check_call(tool, args, NOTHING, _ctx(), tags=tags)
    assert verdict.blocked is blocked, (tool, args, verdict)
    if blocked:
        assert verdict.by == "security"


def test_a_settings_edit_by_a_file_tool_is_still_the_persons_to_approve(tmp_path: Path) -> None:
    """``settings.json`` stays editable behind the ``config`` question; its ``security``
    section (the lists and ``unknownCode``) stays out of every tool's reach."""
    home = tmp_path / ".snowpea"
    home.mkdir()
    settings = home / "settings.json"
    settings.write_text('{"audio": {"voice": "a"}, "security": {"unknownCode": "ask"}}',
                        encoding="utf-8")
    ctx = Context(workdir=tmp_path / "proj", user_home=tmp_path, home=home, projects=(),
                  mounts=(), env_venv=False)
    harmless = check_call("write_file", {
        "path": str(settings),
        "content": '{"audio": {"voice": "b"}, "security": {"unknownCode": "ask"}}',
    }, NOTHING, ctx, tags=("config",))
    assert not harmless.blocked
    widened = check_call("write_file", {
        "path": str(settings),
        "content": '{"audio": {"voice": "a"}, "security": {"unknownCode": "allow"}}',
    }, NOTHING, ctx, tags=("config",))
    assert widened.blocked and widened.by == "security"


def test_the_try_it_box_knows_the_control_plane() -> None:
    assert check_path(f"{HOME}/trust/projects.json", NOTHING, _ctx(), write=True).blocked
    assert check_path(f"{HOME}/token", NOTHING, _ctx(), write=False).blocked
    assert not check_path(f"{HOME}/settings.json", NOTHING, _ctx(), write=False).blocked
    assert not check_path(f"{WORK}/src/a.py", NOTHING, _ctx(), write=True).blocked


# -- the one classifier -------------------------------------------------------------

EFFECTS = [
    ("trust/projects.json", "write", "blocked"),
    ("trust/projects.json", "read", None),
    ("trust/migrated.json", "delete", "blocked"),
    ("trust", "delete", "blocked"),
    ("daemon.json", "read", "blocked"),
    ("daemon.json", "write", "blocked"),
    ("token", "read", "blocked"),
    ("token.bak", "read", "blocked"),
    ("credentials.json", "read", "blocked"),
    ("credentials-old.json", "write", "blocked"),
    ("mcp-oauth.json", "read", "blocked"),
    ("settings.json", "write", "blocked"),
    ("settings.json", "read", None),
    ("jarvis/delegation.db", "write", "blocked"),
    ("jarvis/ledger.jsonl", "write", "blocked"),
    ("state.db", "write", "blocked"),
    ("state.db-wal", "write", "blocked"),
    ("security/written.log", "write", "blocked"),
    ("install.json", "write", "blocked"),
    ("gateway-chats.json", "write", "blocked"),
    ("logs/approvals.jsonl", "write", "blocked"),
    ("logs/security.jsonl", "delete", "blocked"),
    ("logs", "delete", "blocked"),
    ("logs/daemon.log", "write", None),
    ("hooks.json", "write", "ask"),
    ("skills/a/SKILL.md", "write", "ask"),
    ("plugins/x/plugin.py", "write", "ask"),
    ("extensions/x/main.py", "write", "ask"),
    ("skills/a/SKILL.md", "read", None),
    ("plans/p.md", "write", None),
    ("sessions/2026-11-16_s1/tmp/x.py", "write", None),
    ("cache/models.json", "write", None),
    ("", "delete", "blocked"),
    ("", "write", None),
]


@pytest.mark.parametrize(("rel", "access", "expected"), EFFECTS)
def test_control_plane_effect_table(rel: str, access: str, expected: str | None) -> None:
    hit = cp.control_plane_effect(f"{HOME}/{rel}".rstrip("/"), access, home=HOME, user_home=USER)
    assert (hit.verdict if hit is not None else None) == expected, (rel, access, hit)


def test_control_plane_effect_takes_relative_and_outside_paths() -> None:
    assert cp.control_plane_effect("trust/projects.json", "write", home=HOME, cwd=HOME,
                                   user_home=USER) is not None
    assert cp.control_plane_effect("trust/projects.json", "write", home=HOME,
                                   user_home=USER) is None  # no cwd: nothing to resolve
    assert cp.control_plane_effect(f"{WORK}/trust/projects.json", "write", home=HOME,
                                   user_home=USER) is None
    assert cp.control_plane_effect("", "write", home=HOME) is None
    hit = cp.control_plane_effect(f"{HOME}/token", "read", home=HOME, user_home=USER)
    assert hit is not None and hit.shown == "$SNOWPEA_HOME/token" and hit.entry.level == "all"


def test_every_path_the_paths_module_knows_is_classified(tmp_path: Path) -> None:
    """A new ``Paths`` property that is neither in the control table nor in the
    working list fails here: a control file cannot be added unprotected."""
    paths = Paths(home=tmp_path)
    unclassified: list[str] = []
    seen = 0
    for name in dir(Paths):
        if name.startswith("_") or not isinstance(getattr(Paths, name), property):
            continue
        value = getattr(paths, name)
        if not isinstance(value, Path) or value == tmp_path:
            continue
        seen += 1
        parts = value.relative_to(tmp_path).parts
        if cp.entry_for(parts) is None and parts[0] not in cp.WORKING:
            unclassified.append(name)
    assert seen >= 15
    assert unclassified == []
    # What the table must hold, by name (the decision's list).
    for name in ("daemon_json", "token_file", "credentials_json"):
        entry = cp.entry_for(getattr(paths, name).relative_to(tmp_path).parts)
        assert entry is not None and entry.level == "all", name
    for name in ("settings_json", "state_db", "install_json", "approvals_log"):
        entry = cp.entry_for(getattr(paths, name).relative_to(tmp_path).parts)
        assert entry is not None and entry.level == "write", name
    for rel in ("trust/projects.json", "trust/migrated.json", "jarvis/delegation.db",
                "security/written.log", "mcp-oauth.json"):
        assert cp.entry_for(tuple(rel.split("/"))) is not None, rel
    assert {entry.level for entry in cp.CONTROL} <= set(cp.LEVELS)
    assert not {entry.parts[0] for entry in cp.CONTROL} & set(cp.WORKING) - {"logs"}


def test_the_trust_store_files_are_in_the_table() -> None:
    from snowpea_core.trust import projects

    for name in (projects.STORE_NAME, projects.MARKER_NAME):
        assert cp.entry_for((projects.STORE_DIR, name)) is not None
    assert cp.entry_for((written.DIR_NAME, written.FILE_NAME)) is not None


def test_the_config_guard_reads_its_secrets_from_the_table(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from snowpea_core.tools import config_guard

    home = tmp_path / ".snowpea"
    home.mkdir()
    for name in ("token", "daemon.json", "credentials.json", "mcp-oauth.json", "settings.json"):
        (home / name).write_text("{}", encoding="utf-8")
    core = SimpleNamespace(paths=SimpleNamespace(home=home, attachments_dir=home / "attachments"))
    for name in ("token", "daemon.json", "credentials.json", "mcp-oauth.json"):
        assert config_guard.permission_for_read({"path": str(home / name)}, None, core) == \
            "secret", name
    assert config_guard.permission_for_read({"path": str(home / "settings.json")}, None,
                                            core) == "read"


# -- M-7: one way to compare names where the file system ignores case ---------------


def test_case_is_folded_in_every_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.tools import config_guard
    from snowpea_core.trust import projects
    from snowpea_core.util import pathcase

    upper_store = "printf x > /home/u/.SNOWPEA/Trust/Projects.json"
    upper_secret = "cat /home/u/.Snowpea/TOKEN"
    project = tmp_path / "proj"
    project.mkdir()

    monkeypatch.setattr(check_mod, "_FOLD_CASE", False)
    monkeypatch.setattr(pathcase, "FOLD", False)
    assert _state(upper_store) == "allowed"
    assert _state(upper_secret) == "allowed"
    assert not projects.control_path(project / ".Snowpea" / "Hooks.json")
    assert not config_guard.is_config_path(".Snowpea/Settings.json", workdir=project,
                                           home=tmp_path / "home")

    monkeypatch.setattr(check_mod, "_FOLD_CASE", True)
    monkeypatch.setattr(pathcase, "FOLD", True)
    assert _state(upper_store) == "blocked"
    assert _state(upper_secret) == "blocked"
    assert projects.control_path(project / ".Snowpea" / "Hooks.json")
    assert projects.control_path(project / ".CLAUDE" / "settings.local.json")
    assert projects.control_path(project / ".MCP.JSON")
    assert config_guard.is_config_path(".Snowpea/Settings.json", workdir=project,
                                       home=tmp_path / "home")
    assert config_guard.is_config_path(str(tmp_path / "HOME" / "Trust" / "x"), workdir=project,
                                       home=tmp_path / "home")
    # The settings-file rule of the lists themselves.
    ctx = _ctx()
    assert check_command("echo '{}' > /home/u/.snowpea/SETTINGS.JSON", NOTHING, ctx).blocked
    assert check_command("echo '{}' > .Snowpea/Settings.json", NOTHING, ctx,
                         cwd=Path(WORK)).blocked
    # NFD and NFC spellings are one name.
    assert pathcase.fold_text("café") == pathcase.fold_text("café")
