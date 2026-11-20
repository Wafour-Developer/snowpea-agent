"""With the security checks off, nothing of the security layer asks (m34).

Owner decision 2026-11-20 (confirmed twice): while ``security.enabled`` is off
no security ask or block is left, in any case — not for code snowpea cannot
read ahead, not for a line naming its home, not even for a write to its own
control files (settings.json, token, credentials, ``security/``).  In auto mode
such a call simply runs; plan and accept keep their mode rules, never as a
person-only security ask.  With the checks on nothing changes.

The owner's card ("snowpea-unread", "parse:syntax") came from an adb line whose
``PID=$(… | tr -d '\\r') && …`` the reader could not parse: a ``$(…)`` followed
by ``&&`` read as an empty command before the operator.

Every command string is DATA (Python literals) handed to snowpea's pure checker
or to a tool whose ``run`` is a spy.  No shell runs any of them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.jarvis.classify import _Opaque, parse_line
from snowpea_core.permissions.approval_queue import Decision
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.security import enforce
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools.config_guard import permission_for_write
from snowpea_core.tools.registry import Tool, ToolResult

pytestmark = pytest.mark.real_security_default

OFF = {"security": {"enabled": False}}
ON = {"security": {"enabled": True}}

# Test data only (Python literals, never run): the owner's line, with its
# session-scratch script under ``{home}``.
OWNER_LINE = (
    "cd /Users/whitevil/work/aichar/lam && "
    'export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH" && '
    "PID=$(adb -s R3KYC03VDNA shell pidof dev.lam.demo | tr -d '\\r') && "
    'echo "pid=$PID" && '
    "adb -s R3KYC03VDNA forward tcp:9339 localabstract:webview_devtools_remote_$PID && "
    "timeout 40 node {home}/sessions/2026-11-20_s-3c5f7aa7fa33/tmp/pix.mjs; "
    'echo "exit=$?"'
)


def _line(tmp: Path) -> str:
    return OWNER_LINE.format(home=tmp / "home")


def _judge(settings: dict[str, Any], tmp: Path, tool: str, args: dict[str, Any],
           tags: tuple[str, ...]) -> Any:
    return enforce.judge(settings=Settings.model_validate(settings), home=tmp / "home",
                         workdir=tmp / "proj", tool=tool, args=args, tags=tags)


# -- the reader --------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "X=$(echo a) && echo b",
    "PID=$(adb shell pidof x | tr -d '\\r') && echo \"pid=$PID\"",
    "echo $(date) || true",
    "echo $(date) | cat",
    "(cd a && make) && echo ok",
    "(make) | tail -3",
    OWNER_LINE.format(home="/Users/whitevil/.snowpea"),
])
def test_a_closed_paren_before_an_operator_parses(line: str) -> None:
    parsed = parse_line(line)
    assert not parsed.unsure
    assert any(cmd.words[:1] == ["echo"] or cmd.words[:1] == ["tail"] or cmd.words[:1] == ["cat"]
               or cmd.words[:1] == ["true"] for cmd in parsed.commands), parsed.commands


@pytest.mark.parametrize("line", ["&& echo a", "echo a && && echo b", "echo a | | cat",
                                  "X=$(echo a) && && echo b"])
def test_an_empty_command_before_an_operator_still_fails(line: str) -> None:
    with pytest.raises(_Opaque):
        parse_line(line)


# -- the check ---------------------------------------------------------------------


def test_off_the_owner_line_gives_no_ask(tmp_path: Path) -> None:
    verdict = _judge(OFF, tmp_path, "shell", {"command": _line(tmp_path)}, ("exec",))
    assert not verdict.blocked and not verdict.opaque, verdict
    assert not any(verdict.asks_in(mode) for mode in ("auto", "accept", "plan"))


def test_on_the_owner_line_has_no_parse_failure_any_more(tmp_path: Path) -> None:
    verdict = _judge(ON, tmp_path, "shell", {"command": _line(tmp_path)}, ("exec",))
    assert not any(why.startswith("parse:") for why in verdict.unseen), verdict


@pytest.mark.parametrize("rel", ["token", "settings.json", "credentials.json",
                                 "security/switch.json", "daemon.json", "state.db"])
def test_off_a_shell_write_to_a_control_file_gives_no_ask(tmp_path: Path, rel: str) -> None:
    command = f"echo x > {tmp_path / 'home' / rel}"
    off = _judge(OFF, tmp_path, "shell", {"command": command}, ("exec",))
    assert not off.blocked and not off.opaque, off
    on = _judge(ON, tmp_path, "shell", {"command": command}, ("exec",))
    assert on.blocked or on.opaque, on


def test_off_an_unreadable_line_naming_the_home_gives_no_ask(tmp_path: Path) -> None:
    command = 'tar xf b.tar -C ~/.snowpea; echo "끝'
    off = _judge(OFF, tmp_path, "shell", {"command": command}, ("exec",))
    assert not off.blocked and not off.opaque, off
    on = _judge(ON, tmp_path, "shell", {"command": command}, ("exec",))
    assert on.blocked or on.opaque, on


def test_off_a_file_tool_rewriting_the_security_lists_is_not_blocked(tmp_path: Path) -> None:
    args = {"path": str(tmp_path / "home" / "settings.json"),
            "content": '{"security": {"enabled": true}}'}
    off = _judge(OFF, tmp_path, "write_file", args, ("config",))
    assert not off.blocked and not off.opaque, off
    on = _judge(ON, tmp_path, "write_file", args, ("config",))
    assert on.blocked, on


# -- through the agent loop ----------------------------------------------------------


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    core = Core(settings=Settings.model_validate(OFF), paths=Paths.create(tmp_path / "home"),
                token="t")
    ran: list[str] = []

    async def run(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        ran.append("ran")
        return ToolResult(ok=True, output="done")

    core.tools.register(Tool(name="run_cmd", category="test", description="run_cmd",
                             input_schema={}, permission="exec", run=run))
    core.tools.register(Tool(name="put_file", category="test", description="put_file",
                             input_schema={}, permission="write", run=run,
                             permission_for=permission_for_write))
    asked: list[dict[str, Any]] = []

    async def request(session: Any, tool: str, args: dict, **kwargs: Any) -> Decision:
        asked.append({"tool": tool, **kwargs})
        return Decision("allow", "once", "test")

    monkeypatch.setattr(core.approvals, "request", request)

    async def notify(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(core.hub, "notify", notify)
    (tmp_path / "proj").mkdir()
    session = Session(id="s-offnever", workdir=tmp_path / "proj", mode="auto")
    return SimpleNamespace(core=core, session=session, ran=ran, asked=asked,
                           home=core.paths.home, tmp=tmp_path)


async def _call(env: Any, name: str, args: dict[str, Any]) -> None:
    call = ToolCall(id=f"c-{len(env.session.history.snapshot())}", name=name, arguments=args)
    await _run_one_call(env.core, env.session, LocalBackend(env.session.workdir),
                        PermissionPolicy(), call, "t-1", False)


def _owner(env: Any) -> dict[str, Any]:
    return {"command": OWNER_LINE.format(home=env.home)}


async def test_off_auto_the_owner_line_runs_unasked(loop: Any) -> None:
    await _call(loop, "run_cmd", _owner(loop))
    assert loop.ran == ["ran"]
    assert loop.asked == []


async def test_off_accept_the_owner_line_gets_only_the_exec_ask(loop: Any) -> None:
    loop.session.mode = "accept"
    await _call(loop, "run_cmd", _owner(loop))
    assert loop.ran == ["ran"]
    assert len(loop.asked) == 1
    assert loop.asked[0].get("cacheable") is True  # not an unchecked (person-only) ask


async def test_off_auto_a_settings_write_runs_unasked(loop: Any) -> None:
    await _call(loop, "put_file", {"path": str(loop.home / "settings.json"), "content": "{}"})
    assert loop.ran == ["ran"]
    assert loop.asked == []


async def test_off_auto_a_shell_write_to_the_token_runs_unasked(loop: Any) -> None:
    await _call(loop, "run_cmd", {"command": f"echo x > {loop.home / 'token'}"})
    assert loop.ran == ["ran"]
    assert loop.asked == []


async def test_off_plan_a_settings_write_is_still_refused_by_the_mode(loop: Any) -> None:
    loop.session.mode = "plan"
    await _call(loop, "put_file", {"path": str(loop.home / "settings.json"), "content": "{}"})
    assert loop.ran == []
    assert loop.asked == []


async def test_off_accept_a_settings_write_keeps_the_config_ask(loop: Any) -> None:
    loop.session.mode = "accept"
    await _call(loop, "put_file", {"path": str(loop.home / "settings.json"), "content": "{}"})
    assert len(loop.asked) == 1


async def test_on_auto_a_settings_write_still_asks(loop: Any) -> None:
    loop.core.settings = Settings.model_validate(ON)
    await _call(loop, "put_file", {"path": str(loop.home / "settings.json"), "content": "{}"})
    assert len(loop.asked) == 1


async def test_on_auto_a_shell_write_to_the_token_still_stops(loop: Any) -> None:
    loop.core.settings = Settings.model_validate(ON)
    await _call(loop, "run_cmd", {"command": f"echo x > {loop.home / 'token'}"})
    assert loop.ran == [] or len(loop.asked) == 1
