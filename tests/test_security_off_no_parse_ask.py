"""With the security checks off, a line snowpea cannot read never asks (m32).

Owner decision 2026-11-19: while ``security.enabled`` is off nothing of the
security layer asks — no blocked lists, no protected paths, no unknown-code or
unreadable-code asks.  Only the mode rules apply (auto mode asks nothing), and
snowpea's own control files still do.  A long multi-statement line (Korean in
its quoted strings) asked "parse:syntax" in auto mode with the checks off: the
reader failed on ``( … ) 2>&1 | grep`` and on ``2>&1>/dev/null``, and a parse
failure was kept as an ask whatever the switch said.

Every command string is DATA (Python literals) handed to snowpea's pure
checker or to a tool whose ``run`` is a spy.  No shell runs any of them.
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
from snowpea_core.jarvis.classify import parse_line
from snowpea_core.permissions.approval_queue import Decision
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.security import enforce
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import Tool, ToolResult

pytestmark = pytest.mark.real_security_default

OFF = {"security": {"enabled": False}}
ON = {"security": {"enabled": True}}

# Test data only (Python literals, never run): the shape of the owner's line.
OWNER_LINE = (
    'cd bench; echo "=== 빌드 (nvcc) ==="; '
    '(cd kern && nvcc -O3 hot.cu -o hot) 2>&1|grep -vE "warning #|경고|^$"; '
    'echo "rc=$?" echo "=== 문법 검사 ==="; '
    'for s in a.sh b.sh; do bash -n bench/$s && echo "OK $s"; done; '
    "tr '\\0' ' ' < /proc/$(cat bench/hotsweep.pid)/cmdline 2>&1; echo; "
    'grep -n "temp = -1" -A 2 sweep.py 2>&1>/dev/null | head -5'
)
#: A line the reader still cannot parse (an unclosed quote): ``parse:…``.
UNPARSED = 'echo "결과 rc=$?; ls'
#: The same, spelling snowpea's home: what was not read may write its controls.
UNPARSED_SNOWPEA = 'tar xf b.tar -C ~/.snowpea; echo "결과'
#: The browser's case: inline Python whose callee is computed
#: (``code:computed-callee``), importing a person's module named snowpea_….
PYTHON_C = (
    "python -m py_compile a.py b.py && node --check app.js && "
    "python -c \"import json,os,snowpea_search as s; t={'n': s.normalize}; "
    "trace=json.load(open('trace.json')); out=t['n']('서울 날씨', s.final_text(trace)); "
    "assert '서울' in out, out; print('정규화 PASS')\""
)


def _judge(settings: dict[str, Any], tmp: Path, command: str) -> Any:
    return enforce.judge(settings=Settings.model_validate(settings), home=tmp / "home",
                         workdir=tmp, tool="shell", args={"command": command}, tags=("exec",))


# -- the reader --------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "(cd a && make) 2>&1 | tail -3",
    "{ echo a; } 2>&1 | tail",
    "(make) 2>&1 |& tee log",
    "echo a 2>&1>/dev/null",
    "echo a 2>&1>>log",
    "echo a 1>&2<in",
    OWNER_LINE,
])
def test_the_reader_parses_a_duplicated_descriptor_before_a_pipe_or_redirect(line: str) -> None:
    parse_line(line)


def test_a_bare_pipe_is_still_a_syntax_error() -> None:
    from snowpea_core.jarvis.classify import _Opaque

    for line in ("| x", "a | | b", "a 2>&1 >"):
        with pytest.raises(_Opaque):
            parse_line(line)


def test_the_subshell_redirect_keeps_its_commands() -> None:
    parsed = parse_line("(cd a && make) 2>&1 | tail -3")
    assert [cmd.words for cmd in parsed.commands] == [["cd", "a"], ["make"], ["tail", "-3"]]
    parsed = parse_line("echo a 2>&1>/dev/null")
    assert parsed.commands[0].redirects == [(">", "/dev/null", False)]


# -- the check ---------------------------------------------------------------------


@pytest.mark.parametrize("line", [OWNER_LINE, UNPARSED, PYTHON_C])
def test_off_an_unreadable_line_does_not_ask(tmp_path: Path, line: str) -> None:
    verdict = _judge(OFF, tmp_path, line)
    assert not verdict.blocked and not verdict.opaque, verdict
    # Not opaque: never a person-only ask in any mode (Verdict.asks_in).
    assert not any(verdict.asks_in(mode) for mode in ("auto", "accept", "plan", "ask"))


def test_on_the_python_c_line_asks_as_before(tmp_path: Path) -> None:
    verdict = _judge(ON, tmp_path, PYTHON_C)
    assert verdict.opaque and "code:computed-callee" in verdict.unseen, verdict
    assert verdict.reason == "cannot tell what this code changes"


def test_on_an_unparsed_line_still_asks(tmp_path: Path) -> None:
    verdict = _judge(ON, tmp_path, UNPARSED)
    assert verdict.opaque and not verdict.relaxable, verdict
    assert any(why.startswith("parse:") for why in verdict.unseen)


def test_on_the_owner_line_asks_as_before(tmp_path: Path) -> None:
    # ``bash -n bench/$s`` runs a script only the shell names: still asked.
    verdict = _judge(ON, tmp_path, OWNER_LINE)
    assert verdict.opaque and "script-dynamic" in verdict.unseen, verdict
    assert not any(why.startswith("parse:") for why in verdict.unseen)


def test_off_an_unreadable_line_naming_snowpea_does_not_ask(tmp_path: Path) -> None:
    # m34 (owner decision 2026-11-20): no security ask while off, in any case.
    verdict = _judge(OFF, tmp_path, UNPARSED_SNOWPEA)
    assert not verdict.opaque and not verdict.blocked, verdict
    on = _judge(ON, tmp_path, UNPARSED_SNOWPEA)
    assert on.opaque and on.asks_in("auto"), on


def test_off_a_write_to_snowpea_controls_does_not_ask(tmp_path: Path) -> None:
    home = tmp_path / "home"
    verdict = _judge(OFF, tmp_path, f'echo "{{}}" > {home}/settings.json; echo "끝')
    assert not verdict.blocked and not verdict.opaque, verdict
    verdict = _judge(ON, tmp_path, f'echo "{{}}" > {home}/settings.json; echo "끝')
    assert verdict.blocked or verdict.opaque, verdict


# -- through the agent loop ----------------------------------------------------------


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    core = Core(settings=Settings.model_validate(OFF), paths=Paths.create(tmp_path / "home"),
                token="t")
    ran: list[str] = []

    async def run(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        ran.append("run_cmd")
        return ToolResult(ok=True, output="done")

    core.tools.register(Tool(name="run_cmd", category="test", description="run_cmd",
                             input_schema={}, permission="exec", run=run))
    asked: list[dict[str, Any]] = []

    async def request(session: Any, tool: str, args: dict, **kwargs: Any) -> Decision:
        asked.append({"tool": tool, **kwargs})
        return Decision("allow", "once", "test")

    monkeypatch.setattr(core.approvals, "request", request)

    async def notify(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(core.hub, "notify", notify)
    (tmp_path / "proj").mkdir()
    session = Session(id="s-offask", workdir=tmp_path / "proj", mode="auto")
    return SimpleNamespace(core=core, session=session, ran=ran, asked=asked)


async def _call(env: Any, command: str) -> None:
    call = ToolCall(id=f"c-{len(env.session.history.snapshot())}", name="run_cmd",
                    arguments={"command": command})
    await _run_one_call(env.core, env.session, LocalBackend(env.session.workdir),
                        PermissionPolicy(), call, "t-1", False)


@pytest.mark.parametrize("line", [OWNER_LINE, UNPARSED, PYTHON_C])
async def test_off_in_auto_mode_the_line_runs_unasked(loop: Any, line: str) -> None:
    await _call(loop, line)
    assert loop.ran == ["run_cmd"]
    assert loop.asked == []


@pytest.mark.parametrize("line", [UNPARSED, PYTHON_C])
async def test_off_in_accept_mode_only_the_mode_rule_asks(loop: Any, line: str) -> None:
    # accept mode asks for exec by its own rule; the queue makes it person-only
    # only for an opaque or blocked verdict, which the checks off never give here.
    loop.session.mode = "accept"
    await _call(loop, line)
    assert loop.ran == ["run_cmd"]
    assert len(loop.asked) == 1


async def test_on_in_auto_mode_the_unparsed_line_asks(loop: Any) -> None:
    loop.core.settings = Settings.model_validate(ON)
    await _call(loop, UNPARSED)
    assert len(loop.asked) == 1
