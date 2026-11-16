"""Protocol 1.55.0 follow-ups, group 1.

* ``hook_failed`` and ``context_window_too_small`` are registered error codes
  and their ``error`` events carry ``details``.
* A started tool call always ends with a ``tool.result``: cancelled, or broken
  after the tool (a hook, the cleanup).
* Windows hooks: a real PATH Python that cannot start or import the hook is
  retried once with core's interpreter, and the session remembers it.
* A ``personOnly`` question / approval answered by the origin without a
  person's marker is never silently ignored: the origin gets the request again
  with ``refused``, and a person can still answer it.

No real shell, CLI or model runs here: hooks spawn fakes, tools are spies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import loop
from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec import winproc
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import (
    ApprovalPendingNotification,
    ErrorEvent,
    QuestionItem,
    QuestionOption,
    QuestionPendingNotification,
    dump_schema,
)
from snowpea_core.session import events
from snowpea_core.session.questions import QuestionQueue
from snowpea_core.session.session import Session
from snowpea_core.skills import hooks
from snowpea_core.tools.registry import Tool, ToolResult

# -- (1) error codes ---------------------------------------------------------------------


def test_new_error_codes_are_registered() -> None:
    assert errors.HOOK_FAILED == hooks.HOOK_FAILED_CODE == "hook_failed"
    assert errors.CONTEXT_WINDOW_TOO_SMALL == loop.CONTEXT_WINDOW_TOO_SMALL
    codes = dump_schema()["errorCodes"]
    assert "hook_failed" in codes and "context_window_too_small" in codes


def test_error_event_details_are_optional() -> None:
    kind, plain = events.error("internal", "boom")
    assert kind == "error" and plain == {"code": "internal", "message": "boom"}
    _, rich = events.error("context_window_too_small", "small",
                           {"windowTokens": 4096, "neededTokens": 9800})
    assert rich["details"] == {"windowTokens": 4096, "neededTokens": 9800}
    ErrorEvent(**rich)


def test_window_too_small_error_carries_the_numbers() -> None:
    exc = loop.window_too_small(4096, 9800)
    assert exc is not None and exc.code == "context_window_too_small"
    assert exc.details == {"windowTokens": 4096, "neededTokens": 9800}  # type: ignore[attr-defined]


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def emit_event(self, session_id: str, event: Any) -> None:
        self.events.append((session_id, event))


async def test_hook_failed_event_has_details(tmp_path: Path) -> None:
    hooks._WARNED.clear()
    hub = _Hub()
    core = SimpleNamespace(hub=hub)
    session = SimpleNamespace(id="s-h")
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="guard", plugin="acme")
    await hooks.note_failure(core, session, hook, 9009, "not recognized")
    (_sid, (kind, payload)), = hub.events
    assert kind == "error" and payload["code"] == "hook_failed"
    assert payload["details"] == {
        "plugin": "acme", "hook": "PreToolUse",
        "reason": "command not found (exit 9009): not recognized", "exitCode": 9009,
    }


# -- (2) a started call always gets a result ---------------------------------------------


class _Allow(PermissionPolicy):
    def decide(self, *_a: Any, **_k: Any) -> Any:
        return "allow"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    started = asyncio.Event()

    async def slow(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        return ToolResult(ok=True)

    async def quick(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=True, output="fine")

    core.tools.register(Tool(name="slow", category="test", description="slow",
                             input_schema={}, permission="read", run=slow))
    core.tools.register(Tool(name="quick", category="test", description="quick",
                             input_schema={}, permission="read", run=quick))
    seen: list[tuple[str, dict[str, Any]]] = []

    async def emit(_sid: str, event: Any) -> None:
        seen.append(event)

    monkeypatch.setattr(core.hub, "emit_event", emit)
    (tmp_path / "proj").mkdir()
    session = Session(id="s-c", workdir=tmp_path / "proj", mode="auto")
    return SimpleNamespace(core=core, session=session, seen=seen, started=started)


def _results(seen: list[Any]) -> list[dict[str, Any]]:
    return [payload for kind, payload in seen if kind == "tool.result"]


async def test_a_cancelled_call_ends_with_a_cancelled_result(env: Any) -> None:
    call = ToolCall(id="c-1", name="slow", arguments={})
    task = asyncio.ensure_future(_run_one_call(
        env.core, env.session, LocalBackend(env.session.workdir), _Allow(), call, "t", False))
    await asyncio.wait_for(env.started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kinds = [kind for kind, _ in env.seen]
    assert kinds.count("tool.call") == 1
    (result,) = _results(env.seen)
    assert result["callId"] == "c-1" and result["ok"] is False and result["error"] == "cancelled"
    last = env.session.history.snapshot()[-1]
    assert last.role == "tool" and last.tool_call_id == "c-1" and last.content == "cancelled"


async def test_parallel_calls_all_settle_when_the_batch_is_cancelled(env: Any) -> None:
    backend = LocalBackend(env.session.workdir)
    calls = [ToolCall(id=f"c-{i}", name="slow", arguments={"i": i}) for i in range(3)]
    task = asyncio.ensure_future(asyncio.gather(*[
        _run_one_call(env.core, env.session, backend, _Allow(), call, "t", False)
        for call in calls
    ]))
    await asyncio.wait_for(env.started.wait(), 5)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(r["callId"] for r in _results(env.seen)) == ["c-0", "c-1", "c-2"]
    assert {r["error"] for r in _results(env.seen)} == {"cancelled"}


async def test_an_error_after_the_tool_still_settles_the_call(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("post hook machinery broke")

    monkeypatch.setattr(loop.plugin_hooks, "post_tool_use", broken)
    call = ToolCall(id="c-9", name="quick", arguments={})
    outcome = await _run_one_call(env.core, env.session, LocalBackend(env.session.workdir),
                                  _Allow(), call, "t", False)
    assert outcome is None  # the turn goes on; the model reads the failure
    (result,) = _results(env.seen)
    assert result["ok"] is False and "post hook machinery broke" in result["error"]


async def test_an_error_in_the_pre_hook_still_settles_the_call(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("pre hook broke")

    monkeypatch.setattr(loop.plugin_hooks, "pre_tool_use", broken)
    call = ToolCall(id="c-8", name="quick", arguments={})
    await _run_one_call(env.core, env.session, LocalBackend(env.session.workdir), _Allow(),
                        call, "t", False)
    (result,) = _results(env.seen)
    assert result["callId"] == "c-8" and "pre hook broke" in result["error"]


async def test_a_normal_call_gets_exactly_one_result(env: Any) -> None:
    call = ToolCall(id="c-7", name="quick", arguments={})
    await _run_one_call(env.core, env.session, LocalBackend(env.session.workdir), _Allow(),
                        call, "t", False)
    (result,) = _results(env.seen)
    assert result["ok"] is True and result["output"].startswith("fine")


# -- (3) Windows hook interpreter fallback -----------------------------------------------


class _Done:
    def __init__(self, code: int, err: bytes = b"") -> None:
        self.pid = 1
        self.returncode = code
        self._err = err

    async def communicate(self, _body: bytes) -> tuple[bytes, bytes]:
        return b"", self._err


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A Windows host whose PATH has a real ``python3``; records every spawned command."""
    hooks._CORE_PYTHON_FOR.clear()
    monkeypatch.setattr(hooks.sys, "platform", "win32")
    monkeypatch.setattr(hooks.sys, "executable", r"C:\snowpea\python.exe")
    monkeypatch.setattr(winproc, "python_substitute", lambda *_a, **_k: False)
    monkeypatch.setattr(winproc, "spawn_kwargs", lambda: {})
    monkeypatch.setattr(winproc, "attach", lambda _p: None)
    monkeypatch.setattr(winproc, "release", lambda _p: None)
    monkeypatch.setattr(winproc, "decode_output", lambda b: b.decode())
    return []


def _spawner(monkeypatch: pytest.MonkeyPatch, spawned: list[str],
             answers: list[tuple[int, bytes]]) -> None:
    async def create(command: str, **_kwargs: Any) -> _Done:
        spawned.append(command)
        code, err = answers.pop(0)
        return _Done(code, err)

    monkeypatch.setattr(hooks.asyncio, "create_subprocess_shell", create)


CORE_PY = r'"C:\snowpea\python.exe"'


@pytest.mark.parametrize(
    "first",
    [
        (1, b"Traceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'"),
        (1, b"ImportError: cannot import name 'x'"),
        (9009, b""),
    ],
)
async def test_a_failing_path_python_is_retried_with_cores_once_and_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: list[str],
    first: tuple[int, bytes],
) -> None:
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="python3 hooks/guard.py")
    _spawner(monkeypatch, windows, [first, (0, b""), (0, b"")])
    code, _out, _err = await hooks.run_hook_output(hook, {"session_id": "s-1"}, home=tmp_path)
    assert code == 0
    assert windows[0] == "python3 hooks/guard.py"
    assert windows[1].endswith(" hooks/guard.py") and "snowpea" in windows[1]
    # The session remembers: the next run goes straight to core's interpreter.
    await hooks.run_hook_output(hook, {"session_id": "s-1"}, home=tmp_path)
    assert len(windows) == 3 and "snowpea" in windows[2]


async def test_no_retry_for_an_ordinary_failure_or_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: list[str]
) -> None:
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="python3 hooks/guard.py")
    _spawner(monkeypatch, windows, [(1, b"ValueError: bad input"), (0, b""), (2, b"blocked")])
    assert (await hooks.run_hook_output(hook, {"session_id": "s"}, home=tmp_path))[0] == 1
    assert (await hooks.run_hook_output(hook, {"session_id": "s"}, home=tmp_path))[0] == 0
    assert (await hooks.run_hook_output(hook, {"session_id": "s"}, home=tmp_path))[0] == 2
    assert windows == ["python3 hooks/guard.py"] * 3


async def test_the_retry_failing_too_keeps_the_first_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: list[str]
) -> None:
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="python3 hooks/guard.py")
    _spawner(monkeypatch, windows, [(1, b"ModuleNotFoundError: a"), (1, b"ModuleNotFoundError: a"),
                                    (1, b"ModuleNotFoundError: a"), (0, b"")])
    code, _out, err = await hooks.run_hook_output(hook, {"session_id": "s"}, home=tmp_path)
    assert code == 1 and "ModuleNotFoundError" in err
    assert not hooks._CORE_PYTHON_FOR
    # Not remembered: the next run tries the PATH Python first again.
    await hooks.run_hook_output(hook, {"session_id": "s"}, home=tmp_path)
    assert windows[2] == "python3 hooks/guard.py"


async def test_a_non_python_hook_is_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: list[str]
) -> None:
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="node guard.js")
    _spawner(monkeypatch, windows, [(9009, b"")])
    assert (await hooks.run_hook_output(hook, {}, home=tmp_path))[0] == 9009
    assert windows == ["node guard.js"]


async def test_posix_hooks_are_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks._CORE_PYTHON_FOR.clear()
    monkeypatch.setattr(hooks.sys, "platform", "linux")
    spawned: list[str] = []
    _spawner(monkeypatch, spawned, [(1, b"ModuleNotFoundError: x")])
    hook = hooks.Hook(event="PreToolUse", matcher="*", command="python3 guard.py")
    assert (await hooks.run_hook_output(hook, {}, home=tmp_path))[0] == 1
    assert spawned == ["python3 guard.py"]


# -- (6) a refused origin reply is never a silent ignore -----------------------------------


class _Origin:
    """The asking connection: answers once without a marker, records notifications."""

    def __init__(self, kind: str, reply: dict[str, Any], client_id: str = "b-1") -> None:
        self.authenticated, self.closed = True, False
        self.client_kind, self.client_id = kind, client_id
        self.remote_login, self.client_version = False, "0.3.0"
        self.surface_id = f"{kind}-s"
        self.reply = reply
        self.notes: list[tuple[str, dict[str, Any]]] = []

    async def call(self, _method: str, _params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return self.reply

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notes.append((method, params))


async def _wait_for(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def test_an_unmarked_reply_to_a_person_only_question_comes_back(tmp_path: Path) -> None:
    queue = QuestionQueue(Settings())
    origin = _Origin("browser", {"answers": [{"selected": ["auto"]}]})
    session = SimpleNamespace(id="s-1", origin_conn=origin, owned_by=None, host_tools_from="b-1",
                              workdir=tmp_path)
    item = QuestionItem(header="Mode", question="?", options=[QuestionOption(label="auto")])
    task = asyncio.ensure_future(queue.ask(session, [item], timeout_sec=30, person_only=True,
                                           kind="mode"))
    await _wait_for(lambda: origin.notes)
    (method, params), = origin.notes
    assert method == "question.pending"
    QuestionPendingNotification(**params)
    assert params["refused"]["code"] == "jarvis_person_only"
    assert params["refused"]["reason"] == "mode_raise"
    request_id = params["request"]["requestId"]
    assert [r.requestId for r in queue.list()] == [request_id]  # still open
    assert not task.done()
    # The person's click (same browser, marked) answers it.
    assert await queue.respond(request_id, [{"selected": ["auto"]}], conn=origin,
                               actor="person")
    assert (await task)[0].person is True


async def test_an_unmarked_allow_of_a_person_only_approval_comes_back(tmp_path: Path) -> None:
    queue = ApprovalQueue(Settings(), Paths.create(tmp_path / "home"))
    origin = _Origin("browser", {"decision": "allow", "scope": "once"})
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=origin,
                              host_tools_from="b-1", owned_by=None)
    task = asyncio.ensure_future(queue.request(
        session, "write_file", {"path": "x"}, timeout_sec=30, person_only=True,
        tags=("write",)))
    await _wait_for(lambda: origin.notes)
    (method, params), = origin.notes
    assert method == "approval.pending"
    ApprovalPendingNotification(**params)
    assert params["refused"]["code"] == "jarvis_person_only"
    assert params["refused"]["reason"] == "person_only"
    request_id = params["request"]["requestId"]
    assert not task.done() and queue.get(request_id) is not None
    # A browser is never the person for a person-only approval, marker or not; the
    # request moved to the shared queue so the person's own surface answers it.
    with pytest.raises(errors.RpcError):
        await queue.respond(request_id, "allow", conn=origin, actor="person", by="b")
    person = _Origin("ide", {}, client_id="ide-1")
    applied = await queue.respond(request_id, "allow", conn=person, by="ide")
    assert applied is not None and applied.allowed
    decision = await task
    assert decision.allowed and decision.person is not None


async def test_a_deny_from_the_origin_is_still_a_deny(tmp_path: Path) -> None:
    queue = ApprovalQueue(Settings(), Paths.create(tmp_path / "home"))
    origin = _Origin("browser", {"decision": "deny"})
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=origin,
                              host_tools_from="b-1", owned_by=None)
    decision = await queue.request(session, "write_file", {"path": "x"}, timeout_sec=30,
                                   person_only=True, tags=("write",))
    assert not decision.allowed and decision.by == "origin" and origin.notes == []
