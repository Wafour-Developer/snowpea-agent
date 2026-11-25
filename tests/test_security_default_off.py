"""Security checks off by default (1.66.0, capability ``securityDefaultOff``).

Owner decision 2026-11-19: the checks are off until a person turns them on; a
stored ``true`` nobody chose (the old default, written out) is migrated once;
while off, reads of tokens and logins follow the mode rules instead of being
blocked; with the checks on everything stays as it was.  The safety
guidelines are in every system prompt instead.

Every command string is DATA (Python literals) handed to snowpea's pure checker
or to tools whose ``run`` is a spy.  No real shell runs; every path is a tmp
dir or fictional, and no real credential is ever read.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.approval_queue import Decision
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.prompts import compose
from snowpea_core.providers.base import ToolCall
from snowpea_core.security import enforce
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import (
    CAPABILITIES,
    PROTOCOL_VERSION,
    SecurityGetParams,
    SecurityGetResult,
)
from snowpea_core.server.security_handlers import get_handler
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import Tool, ToolResult

pytestmark = pytest.mark.real_security_default

ON = {"security": {"enabled": True}}


def _on() -> Settings:
    return Settings.model_validate(ON)


def _judge(settings: Settings, tmp: Path, tool: str, args: dict[str, Any],
           tags: tuple[str, ...] = ()) -> Any:
    return enforce.judge(settings=settings, home=tmp / "home", workdir=tmp, tool=tool,
                         args=args, tags=tags)


# -- the default ----------------------------------------------------------------------------


def test_the_checks_are_off_by_default() -> None:
    assert Settings().security.enabled is False
    assert enforce.enabled(Settings()) is False
    assert enforce.enabled(None) is False
    assert Settings.model_validate({"security": {}}).security.enabled is False
    assert Settings.model_validate({"security": {"enabled": None}}).security.enabled is False
    # The stored lists stay as they are, ready for the switch.
    assert Settings().security.blockedCommands.presets == ["essential"]


def test_an_explicit_true_is_on() -> None:
    assert enforce.enabled(_on()) is True
    assert Settings.model_validate({"security": {"enabled": False}}).security.enabled is False


def test_a_degraded_security_section_fails_closed_to_on() -> None:
    settings = Settings().mark_degraded(["security"])
    assert enforce.enabled(settings) is True


def test_protocol_and_capability() -> None:
    assert PROTOCOL_VERSION == "1.79.0"
    assert "securityDefaultOff" in CAPABILITIES


# -- byDefault ------------------------------------------------------------------------------


def test_by_default_while_nobody_turned_them_off(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert enforce.switch_state(Settings(), home) == {
        "enabled": False, "byDefault": True, "disabledBy": None, "disabledAt": None}
    assert enforce.switch_state(_on(), home) == {
        "enabled": True, "byDefault": False, "disabledBy": None, "disabledAt": None}
    enforce.note_switch(home, True, False, by="browser")
    state = enforce.switch_state(Settings(), home)
    assert state["byDefault"] is False and state["disabledBy"] == "browser"
    assert state["disabledAt"]
    enforce.note_switch(home, False, True, by="tui")
    assert enforce.switch_state(_on(), home)["byDefault"] is False


def test_the_start_writes_no_record_for_the_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    enforce.reconcile_switch(Settings(), home)
    assert not (home / "security" / "switch.json").exists()
    assert not (home / "logs" / "security.jsonl").exists()
    enforce.reconcile_switch(_on(), home)  # turned on in the file while no daemon ran
    record = json.loads((home / "security" / "switch.json").read_text())
    assert record["enabled"] is True and record["by"] == "settings.json"


async def test_security_get_reports_the_default_neutrally(tmp_path: Path) -> None:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    got = await get_handler(None, SecurityGetParams(), core)
    assert isinstance(got, SecurityGetResult)
    assert got.enabled is False and got.byDefault is True
    assert got.disabledBy is None and got.disabledAt is None
    assert got.effective.commands == [] and got.effective.paths == []
    # Kept for clients of 1.65.0 and older, as a neutral note.
    assert got.problems[0].startswith("Security checks are off (the default)")
    first = got.problemItems[0]
    assert first.code == enforce.DISABLED_CODE and first.reason == enforce.DISABLED_NOTE
    for word in ("warning", "danger", "unsafe", "!"):
        assert word not in enforce.DISABLED_NOTE.lower()
    core.settings = _on()
    got = await get_handler(None, SecurityGetParams(), core)
    assert got.enabled is True and got.byDefault is False
    assert all(item.code != enforce.DISABLED_CODE for item in got.problemItems)


# -- the one-time migration ----------------------------------------------------------------


def _home_with(tmp_path: Path, stored: dict[str, Any], record: dict[str, Any] | None) -> Any:
    paths = Paths.create(tmp_path / "home")
    paths.ensure()
    Path(paths.settings_json).write_text(json.dumps(stored))
    if record is not None:
        target = Path(paths.home) / "security" / "switch.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record))
    settings = Settings.model_validate(stored)
    return paths, settings


def _stored(paths: Any) -> Any:
    return json.loads(Path(paths.settings_json).read_text())["security"]["enabled"]


def _marker(paths: Any) -> Path:
    return Path(paths.home).joinpath(*enforce.DEFAULT_OFF_MARKER)


def test_a_stored_true_nobody_chose_becomes_the_default(tmp_path: Path) -> None:
    paths, settings = _home_with(tmp_path, ON, None)
    assert enforce.migrate_default_off(settings, paths) is True
    assert settings.security.enabled is False and _stored(paths) is False
    marker = json.loads(_marker(paths).read_text())
    assert marker["from"] is True and marker["to"] is False and marker["chosen"] is False
    enforce.reconcile_switch(settings, paths.home)
    assert enforce.switch_state(settings, paths.home)["byDefault"] is True
    assert not (Path(paths.home) / "security" / "switch.json").exists()


def test_a_true_a_person_chose_stays_on(tmp_path: Path) -> None:
    record = {"enabled": True, "by": "tui", "at": "2026-11-18T10:00:00+00:00"}
    paths, settings = _home_with(tmp_path, ON, record)
    assert enforce.migrate_default_off(settings, paths) is False
    assert settings.security.enabled is True and _stored(paths) is True
    assert json.loads(_marker(paths).read_text())["chosen"] is True


def test_the_migration_runs_once(tmp_path: Path) -> None:
    paths, settings = _home_with(tmp_path, ON, None)
    assert enforce.migrate_default_off(settings, paths) is True
    # The person turns the checks on afterwards (by hand, no record): it stays.
    Path(paths.settings_json).write_text(json.dumps(ON))
    again = Settings.model_validate(ON)
    assert enforce.migrate_default_off(again, paths) is False
    assert again.security.enabled is True and _stored(paths) is True


def test_an_already_migrated_home_is_left_alone(tmp_path: Path) -> None:
    paths, settings = _home_with(tmp_path, ON, None)
    _marker(paths).parent.mkdir(parents=True, exist_ok=True)
    _marker(paths).write_text("{}\n")
    assert enforce.migrate_default_off(settings, paths) is False
    assert settings.security.enabled is True and _stored(paths) is True


def test_a_degraded_file_is_not_migrated(tmp_path: Path) -> None:
    paths, settings = _home_with(tmp_path, ON, None)
    settings.mark_degraded(["security"])
    assert enforce.migrate_default_off(settings, paths) is False
    assert not _marker(paths).exists() and _stored(paths) is True


def test_a_stored_false_or_no_key_only_writes_the_marker(tmp_path: Path) -> None:
    paths, settings = _home_with(tmp_path, {"security": {"enabled": False}}, None)
    assert enforce.migrate_default_off(settings, paths) is False
    assert _marker(paths).exists() and _stored(paths) is False


# -- token and login reads: the check layer ------------------------------------------------

# Test data only (Python literals, never run).
TOKEN_READ = "cat {home}/token"
CREDENTIALS_READ = "head -c 100 {home}/credentials.json"
TOKEN_MOVE = "mv {home}/token /tmp/elsewhere"
EXTERNAL_READ = "cat ~/.codex/auth.json"
CODE_MENTION = "print(open('{home}/token').read())"


@pytest.mark.parametrize("line", [TOKEN_READ, CREDENTIALS_READ, EXTERNAL_READ])
def test_on_keeps_the_secret_read_block(tmp_path: Path, line: str) -> None:
    command = line.format(home=tmp_path / "home")
    verdict = _judge(_on(), tmp_path, "shell", {"command": command}, ("exec",))
    assert verdict.blocked and verdict.secret_read and not verdict.person_may_allow


@pytest.mark.parametrize("line", [TOKEN_READ, CREDENTIALS_READ, EXTERNAL_READ])
def test_off_does_not_block_a_secret_read(tmp_path: Path, line: str) -> None:
    command = line.format(home=tmp_path / "home")
    for settings in (Settings(), Settings.model_validate({"security": {"enabled": False}})):
        verdict = _judge(settings, tmp_path, "shell", {"command": command}, ("exec",))
        assert not verdict.blocked and not verdict.secret_read, verdict


def test_off_does_not_block_a_file_tool_or_code_read(tmp_path: Path) -> None:
    home = tmp_path / "home"
    on = _judge(_on(), tmp_path, "read_file", {"path": str(home / "token")}, ("secret",))
    assert on.blocked and on.secret_read
    off = _judge(Settings(), tmp_path, "read_file", {"path": str(home / "token")}, ("secret",))
    assert not off.blocked, off
    code = CODE_MENTION.format(home=home)
    assert _judge(_on(), tmp_path, "python", {"code": code}, ("exec",)).secret_read
    assert not _judge(Settings(), tmp_path, "python", {"code": code}, ("exec",)).secret_read


def test_off_a_move_of_the_token_is_neither_a_secret_read_nor_a_control_ask(
        tmp_path: Path) -> None:
    command = TOKEN_MOVE.format(home=tmp_path / "home")
    on = _judge(_on(), tmp_path, "shell", {"command": command}, ("exec",))
    assert on.blocked and on.secret_read
    off = _judge(Settings(), tmp_path, "shell", {"command": command}, ("exec",))
    # m34 (owner decision 2026-11-20): with the checks off nothing of the
    # security layer asks, snowpea's own controls included.
    assert not off.secret_read
    assert not off.blocked and not off.opaque, off


# -- token and login reads: through the agent loop, by mode -------------------------------


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    ran: list[str] = []

    def spy(name: str) -> Any:
        async def run(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
            ran.append(name)
            return ToolResult(ok=True, output="done")

        return run

    for name, permission in (("run_cmd", "exec"), ("peek", "secret")):
        core.tools.register(Tool(name=name, category="test", description=name,
                                 input_schema={}, permission=permission, run=spy(name)))
    asked: list[dict[str, Any]] = []

    async def request(session: Any, tool: str, args: dict, **kwargs: Any) -> Decision:
        asked.append({"tool": tool, **kwargs})
        return Decision("allow", "once", "test")

    monkeypatch.setattr(core.approvals, "request", request)

    async def notify(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(core.hub, "notify", notify)
    (tmp_path / "proj").mkdir()
    session = Session(id="s-off", workdir=tmp_path / "proj", mode="auto")
    return SimpleNamespace(core=core, session=session, ran=ran, asked=asked)


async def _call(env: Any, name: str, args: dict[str, Any]) -> str:
    call = ToolCall(id=f"c-{len(env.session.history.snapshot())}", name=name, arguments=args)
    await _run_one_call(env.core, env.session, LocalBackend(env.session.workdir),
                        PermissionPolicy(), call, "t-1", False)
    messages = env.session.history.snapshot()
    return messages[-1].content if messages else ""


@pytest.mark.parametrize(("mode", "tool", "asks"), [
    ("auto", "peek", False), ("accept", "peek", True), ("plan", "peek", True),
    ("auto", "run_cmd", False), ("accept", "run_cmd", True), ("plan", "run_cmd", True),
])
async def test_off_a_secret_read_follows_the_mode_rules(loop: Any, mode: str, tool: str,
                                                        asks: bool) -> None:
    loop.session.mode = mode
    token = loop.core.paths.home / "token"
    args = {"path": str(token)} if tool == "peek" else {"command": f"cat {token}"}
    content = await _call(loop, tool, args)
    assert loop.ran == [tool], content
    assert [a["tool"] for a in loop.asked] == ([tool] if asks else [])
    assert not any(a.get("person_only") for a in loop.asked)
    assert "security" not in content.lower()


@pytest.mark.parametrize("mode", ["auto", "accept", "plan"])
async def test_on_a_secret_read_never_runs(loop: Any, mode: str) -> None:
    loop.core.settings = _on()
    loop.session.mode = mode
    token = loop.core.paths.home / "token"
    content = await _call(loop, "run_cmd", {"command": f"cat {token}"})
    assert loop.ran == [] and loop.asked == []
    assert content.startswith("blocked by the security list: security")


# -- the safety guidelines in every prompt -------------------------------------------------

SAFETY_MARKS = ("Safety.", "`rm -rf`", "`git push --force`", "`~/.codex/auth.json`",
                "write_file", "verified")


@pytest.mark.parametrize("mode", ["plan", "accept", "auto"])
@pytest.mark.parametrize("variant", [
    {}, {"subagent": True}, {"role": "executor"}, {"role": "explorer"},
    {"own_identity": True, "persona": "You are a named agent."},
    {"persona": "You are a named agent."},
])
def test_every_prompt_carries_the_safety_guidelines(mode: str, variant: dict[str, Any]) -> None:
    for vendor_class in compose.VENDOR_CLASSES:
        text = compose.build_system_prompt(mode=mode, vendor_class=vendor_class, **variant)
        for mark in SAFETY_MARKS:
            assert mark in text, (mode, variant, vendor_class, mark)
        assert text.count("\nSafety.\n") == 1
        assert "checks are off" not in text.lower()
