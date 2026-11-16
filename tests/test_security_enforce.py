"""Security lists (1.54.0): enforcement at every gate, the RPCs and the settings.

Every command string is DATA handed to snowpea's pure checker or to tools whose
``run`` is a spy: the tests prove that nothing blocked reaches the spy.  No
real shell runs here; paths are tmp dirs or fictional.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.approval_queue import ApprovalQueue, Decision
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CAPABILITIES,
    EVENTS,
    SecurityCheckParams,
    SecurityGetParams,
    SecurityPresetsListParams,
    SettingsSetParams,
)
from snowpea_core.server.security_handlers import check_handler, get_handler, presets_list_handler
from snowpea_core.server.settings_handlers import settings_set_handler
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import Tool, ToolResult


class AlwaysAllow(PermissionPolicy):
    """A saved "always" rule for everything (the allowlist promotes every call)."""

    def promote(self, verdict: Any, *_args: Any, **_kwargs: Any) -> Any:
        return "allow"


class Hub:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.sessions = SimpleNamespace(get=lambda _i: None, all=lambda: [])

    async def notify(self, method: str, params: dict[str, Any], **_kw: Any) -> None:
        self.notes.append((method, params))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.notes if m == method]


def _conn(kind: str = "ide", *, remote: bool = False) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version="0.3.0", client_id=f"{kind}-1")


# -- (a) snowpea's own tool execution -------------------------------------------------


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    ran: list[tuple[str, dict[str, Any]]] = []

    def spy(name: str) -> Any:
        async def run(_ctx: Any, args: dict[str, Any]) -> ToolResult:
            ran.append((name, dict(args)))
            return ToolResult(ok=True, output="done")

        return run

    for name, permission in (("run_cmd", "exec"), ("put_file", "write"), ("look", "read")):
        core.tools.register(Tool(name=name, category="test", description=name,
                                 input_schema={}, permission=permission, run=spy(name)))
    asked: list[dict[str, Any]] = []

    async def request(session: Any, tool: str, args: dict, **kwargs: Any) -> Decision:
        asked.append({"tool": tool, "args": args, **kwargs})
        return Decision("allow", "once", "test")

    monkeypatch.setattr(core.approvals, "request", request)
    hub = Hub()
    monkeypatch.setattr(core.hub, "notify", hub.notify)
    session = Session(id="s-sec", workdir=tmp_path / "proj", mode="auto")
    (tmp_path / "proj").mkdir()
    return SimpleNamespace(core=core, session=session, ran=ran, asked=asked, hub=hub,
                           tmp=tmp_path)


async def _call(env: Any, name: str, args: dict[str, Any], *, session: Any = None) -> Any:
    session = session or env.session
    call = ToolCall(id=f"c-{len(session.history.snapshot())}", name=name, arguments=args)
    result = await _run_one_call(env.core, session, LocalBackend(session.workdir),
                                 AlwaysAllow(), call, "t-1", False)
    messages = session.history.snapshot()
    return result, messages[-1].content if messages else ""


@pytest.mark.parametrize("mode", ["auto", "accept", "plan"])
async def test_a_blocked_command_never_runs_in_any_mode(setup: Any, mode: str) -> None:
    setup.session.mode = mode
    # Default lists (essential) on a fresh install: nothing in settings.json.
    outcome, content = await _call(setup, "run_cmd", {"command": "sudo rm -rf /"})
    assert outcome is None  # the turn continues; the model reads the refusal
    assert setup.ran == [] and setup.asked == []
    assert content.startswith("blocked by the security list: commands:essential")
    blocked = setup.hub.of("security.blocked")
    assert blocked and blocked[0]["sessionId"] == "s-sec" and blocked[0]["by"] == \
        "commands:essential" and blocked[0]["surface"] == "agent"
    audit = (setup.core.paths.logs_dir / "security.jsonl").read_text().splitlines()
    assert json.loads(audit[-1])["by"] == "commands:essential"


async def test_a_protected_path_is_never_written(setup: Any) -> None:
    keep = setup.tmp / "keep"
    setup.core.settings.security.protectedPaths.custom = [  # type: ignore[assignment]
        {"path": str(keep), "level": "write"}  # type: ignore[list-item]
    ]
    setup.core.settings = Settings.model_validate(setup.core.settings.model_dump(mode="json"))
    _, content = await _call(setup, "put_file", {"path": str(keep / "a.txt"), "content": "x"})
    assert setup.ran == [] and content.startswith("blocked by the security list: custom")
    await _call(setup, "put_file", {"path": str(setup.tmp / "free.txt"), "content": "x"})
    assert [name for name, _ in setup.ran] == ["put_file"]


async def test_allowlist_and_auto_cannot_override_a_block_and_unchecked_asks(setup: Any) -> None:
    # AlwaysAllow is a saved rule for everything, the session is in auto.
    await _call(setup, "run_cmd", {"command": "git push --force"})  # not in essential
    assert [name for name, _ in setup.ran] == ["run_cmd"]
    setup.ran.clear()
    # A line that cannot be checked is asked, even in auto with an always rule.
    await _call(setup, "run_cmd", {"command": "$PAYLOAD"})
    assert [a["tool"] for a in setup.asked] == ["run_cmd"]
    assert setup.asked[0]["cacheable"] is False
    assert setup.ran == [("run_cmd", {"command": "$PAYLOAD"})]  # the person allowed it


async def test_a_scheduled_worker_and_the_jarvis_session_are_blocked_too(setup: Any) -> None:
    worker = Session(id="s-worker", workdir=setup.session.workdir, mode="auto")
    worker.origin_surface = "jarvis-task"
    _, content = await _call(setup, "run_cmd", {"command": "rm -rf ~"}, session=worker)
    assert content.startswith("blocked by the security list")
    scheduled = Session(id="s-job", workdir=setup.session.workdir, mode="auto")
    scheduled.origin_surface = "scheduler"
    _, content = await _call(setup, "run_cmd", {"command": "mkfs.ext4 /dev/sdb1"},
                             session=scheduled)
    assert content.startswith("blocked by the security list")
    assert setup.ran == []


async def test_editing_the_lists_through_a_tool_is_blocked_even_with_empty_lists(
    setup: Any,
) -> None:
    setup.core.settings = Settings.model_validate(
        {"security": {"blockedCommands": {"presets": []}, "protectedPaths": {"presets": []}}}
    )
    setup.core.settings.save(setup.core.paths)
    target = setup.core.paths.home / "settings.json"
    # "{}" would bring the defaults back in place of the person's empty lists.
    _, content = await _call(setup, "put_file", {"path": str(target), "content": "{}"})
    assert content.startswith("blocked by the security list: security")
    assert setup.ran == []


# -- (b) the approval queue -----------------------------------------------------------


def _queue(tmp: Path, settings: Settings | None = None) -> tuple[ApprovalQueue, Hub]:
    hub = Hub()
    queue = ApprovalQueue(settings or Settings(), Paths.create(tmp / "home"), hub)  # type: ignore[arg-type]
    return queue, hub


async def test_the_queue_refuses_a_blocked_call_before_cache_and_routing(tmp_path: Path) -> None:
    queue, hub = _queue(tmp_path)
    routed: list[Any] = []
    queue.router = lambda request, *_a: routed.append(request) or "jarvis"
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=None,
                              host_tools_from=None, owned_by=None)
    # "for this session" was answered on the same first word earlier.
    queue._cache.add(queue.cache_key("s-1", "shell", {"command": "rm x"}))
    decision = await queue.request(session, "shell", {"command": "rm -rf ~"},
                                   unattended=True, timeout_sec=5, tags=("exec",))
    assert not decision.allowed and decision.by == "security"
    assert decision.code == errors.SECURITY_BLOCKED
    assert decision.reason.startswith("blocked by the security list: commands:essential")
    assert queue.list() == [] and routed == []
    assert hub.of("approval.pending") == []
    assert hub.of("security.blocked")[0]["surface"] == "approval"
    log = (tmp_path / "home" / "logs" / "approvals.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["by"] == "security"


async def test_an_unchecked_call_is_the_persons_alone(tmp_path: Path) -> None:
    queue, hub = _queue(tmp_path)
    queue.router = lambda *_a: "auto"
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=None,
                              host_tools_from=None, owned_by=None)
    task = asyncio.ensure_future(queue.request(session, "shell", {"command": "eval \"$X\""},
                                               unattended=True, timeout_sec=30,
                                               tags=("exec",)))
    for _ in range(100):
        if queue.list():
            break
        await asyncio.sleep(0.01)
    pending = queue.list()[0]
    assert pending.reason == "cannot be checked against your block list"
    assert pending.personOnly is True and pending.neverDelegable is True
    assert pending.route == "person"  # never Jarvis, never auto
    assert pending.remember is None
    # Jarvis (or any in-process caller) cannot allow it.
    with pytest.raises(RpcError) as caught:
        await queue.respond(pending.requestId, "allow", "once", by="jarvis")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    await queue.respond(pending.requestId, "allow", "once", conn=_conn("ide"))
    assert (await task).allowed


async def test_never_delegable_is_marked_on_ordinary_requests(tmp_path: Path) -> None:
    queue, _hub = _queue(tmp_path)
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=None,
                              host_tools_from=None, owned_by=None)
    for command, never in (("snowpea config show", True), ("npm test", False)):
        task = asyncio.ensure_future(queue.request(session, "shell", {"command": command},
                                                   unattended=True, timeout_sec=30,
                                                   tags=("exec",)))
        for _ in range(100):
            if queue.list():
                break
            await asyncio.sleep(0.01)
        pending = queue.list()[0]
        assert bool(pending.neverDelegable) is never, command
        await queue.respond(pending.requestId, "deny")
        await task


# -- (c) bridged external-CLI approvals --------------------------------------------------


async def test_a_bridged_request_is_denied_with_the_reason(tmp_path: Path) -> None:
    from snowpea_core.external.base import ApprovalAsk
    from snowpea_core.external.manager import ExternalManager

    queue, hub = _queue(tmp_path)
    manager = ExternalManager.__new__(ExternalManager)
    manager.approvals = queue  # type: ignore[attr-defined]
    manager.home = tmp_path / "home"  # type: ignore[attr-defined]
    record = SimpleNamespace(cli="claude", id="abc", real_cwd=str(tmp_path),
                             info=SimpleNamespace(cwd=str(tmp_path), title=None),
                             project=str(tmp_path), approval_cache={"x"}, turn_cancel=None)
    ask = ApprovalAsk(tool="Bash", args={"command": "bash -c 'rm -rf /'"}, risk="high")
    answer = await manager._ask_approval(record, ask)  # type: ignore[arg-type]
    assert not answer.allow and answer.by == "security"
    assert answer.reason.startswith("blocked by the security list: commands:essential")
    assert queue.list() == []
    assert hub.of("security.blocked")[0]["sessionId"] == "ext:claude:abc"
    assert hub.of("security.blocked")[0]["surface"] == "external"


# -- RPCs -----------------------------------------------------------------------------------


@pytest.fixture
def rpc_core(tmp_path: Path) -> Any:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    hub = Hub()
    core.hub.notify = hub.notify  # type: ignore[method-assign]
    core.hub_spy = hub  # type: ignore[attr-defined]
    return core


def test_contract_is_declared() -> None:
    assert "securityLists" in CAPABILITIES
    assert "security.blocked" in EVENTS
    assert errors.SECURITY_BLOCKED in errors.ERROR_CODES


async def test_presets_list_is_localized(rpc_core: Any) -> None:
    ko = await presets_list_handler(None, SecurityPresetsListParams(locale="ko"), rpc_core)
    assert ko.commands[0].id == "essential" and ko.commands[0].title == "필수"
    assert ko.commands[0].default is True and all(not c.default for c in ko.commands[1:])
    assert {p.id for p in ko.paths} >= {"essential", "credentials", "snowpea-home",
                                        "other-projects", "mounts"}
    credentials = next(p for p in ko.paths if p.id == "credentials")
    assert credentials.level == "all" and "~/.ssh" in credentials.paths
    assert ko.locale == "ko"


async def test_get_reports_defaults_effective_and_writable(rpc_core: Any, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    ProjectSettings.model_validate(
        {"security": {"blockedCommands": {"custom": ["make deploy"]}}}
    ).save(project)
    result = await get_handler(_conn("ide"), SecurityGetParams(workdir=str(project)), rpc_core)
    assert result.writable is True
    assert result.blockedCommands.presets == ["essential"]
    assert result.protectedPaths.presets == ["essential"]
    assert result.project is not None and result.project.blockedCommands.custom == ["make deploy"]
    assert result.effective.blockedCommands.custom == ["make deploy"]
    assert any(p.path == "/" and p.scope == "self" for p in result.effective.paths)
    commands = {(c.pattern, c.by) for c in result.effective.commands}
    assert ("mkfs", "commands:essential") in commands
    assert ("rm -rf /", "commands:essential") in commands  # a predicate rule's example
    assert ("make deploy", "custom") in commands
    remote = await get_handler(_conn("web", remote=True), SecurityGetParams(), rpc_core)
    assert remote.writable is False


async def test_check_with_and_without_draft(rpc_core: Any) -> None:
    saved = await check_handler(None, SecurityCheckParams(command="git push -f"), rpc_core)
    assert not saved.blocked
    draft = {"blockedCommands": {"presets": [], "custom": ["git push --force"]},
             "protectedPaths": {"presets": [], "custom": [{"path": "/srv/keep", "level": "all"}]}}
    hit = await check_handler(None, SecurityCheckParams(command="git push -f", draft=draft,
                                                        locale="ko"), rpc_core)
    assert hit.blocked and hit.by == "custom" and "차단 패턴" in (hit.why or "")
    read = await check_handler(None, SecurityCheckParams(path="/srv/keep/x", access="read",
                                                         draft=draft), rpc_core)
    assert read.blocked and read.match == "/srv/keep" and read.level == "all"
    opaque = await check_handler(None, SecurityCheckParams(command="$X -rf /"), rpc_core)
    assert opaque.opaque and opaque.reason == "cannot be checked against your block list"
    with pytest.raises(RpcError) as caught:
        await check_handler(None, SecurityCheckParams(
            command="ls", draft={"blockedCommands": {"custom": ["*"]}}), rpc_core)
    assert caught.value.code == errors.INVALID_PARAMS
    with pytest.raises(RpcError):
        await check_handler(None, SecurityCheckParams(), rpc_core)


# -- settings ---------------------------------------------------------------------------------


async def _set(core: Any, conn: Any, patch: dict[str, Any], **extra: Any) -> Any:
    return await settings_set_handler(conn, SettingsSetParams(patch=patch, **extra), core)


async def test_only_a_local_person_changes_the_lists(rpc_core: Any) -> None:
    patch = {"security": {"blockedCommands": {"presets": ["essential", "disk"],
                                              "custom": ["git push --force"]}}}
    for conn in (_conn("web", remote=True), _conn("browser"), _conn("connector")):
        with pytest.raises(RpcError) as caught:
            await _set(rpc_core, conn, patch)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    with pytest.raises(RpcError):
        await _set(rpc_core, None, patch)  # in-process: nobody
    await _set(rpc_core, _conn("ide"), patch)
    assert rpc_core.settings.security.blockedCommands.presets == ["essential", "disk"]
    changed = rpc_core.hub_spy.of("settings.changed")
    assert changed and any(k.startswith("security") for k in changed[-1]["keys"])
    # Arrays replace; an explicit empty list stays empty after a reload.
    await _set(rpc_core, _conn("ide"), {"security": {"protectedPaths": {"presets": []}}})
    reloaded = Settings.load(rpc_core.paths)
    assert reloaded.security.protectedPaths.presets == []
    assert reloaded.security.blockedCommands.presets == ["essential", "disk"]


async def test_invalid_entries_are_invalid_params(rpc_core: Any) -> None:
    for patch in (
        {"security": {"blockedCommands": {"custom": ["re:("]}}},
        {"security": {"blockedCommands": {"presets": ["nope"]}}},
        {"security": {"protectedPaths": {"custom": [{"path": "relative/x"}]}}},
        {"security": {"protectedPaths": {"custom": [{"path": "/x", "level": "some"}]}}},
        {"security": {"protectedPaths": {"custom": [{"path": "/"}]}}},
        {"security": {"other": {}}},
    ):
        with pytest.raises(RpcError) as caught:
            await _set(rpc_core, _conn("ide"), patch)
        assert caught.value.code == errors.INVALID_PARAMS, patch


async def test_the_project_scope_may_only_add(rpc_core: Any, tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    add = {"security": {"blockedCommands": {"custom": ["make deploy"]}}}
    await _set(rpc_core, _conn("ide"), add, scope="project", workdir=str(project))
    assert ProjectSettings.load(project).security["blockedCommands"]["custom"] == ["make deploy"]
    with pytest.raises(RpcError) as caught:
        await _set(rpc_core, _conn("ide"), {"security": {"blockedCommands": {"custom": []}}},
                   scope="project", workdir=str(project))
    assert caught.value.code == errors.INVALID_PARAMS
    assert caught.value.data["reason"] == "project_add_only"
    with pytest.raises(RpcError) as caught:
        await _set(rpc_core, _conn("web", remote=True), add, scope="project",
                   workdir=str(project))
    assert caught.value.code == errors.JARVIS_PERSON_ONLY


def test_migration_defaults(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "home")
    paths.ensure()
    paths.settings_json.write_text(json.dumps({"agent": {}}))  # an install from before 1.54
    loaded = Settings.load(paths)
    assert loaded.security.blockedCommands.presets == ["essential"]
    assert loaded.security.protectedPaths.presets == ["essential"]
    paths.settings_json.write_text(json.dumps(
        {"security": {"blockedCommands": {"presets": []}, "protectedPaths": {"custom": ["/srv"]}}}
    ))
    loaded = Settings.load(paths)
    assert loaded.security.blockedCommands.presets == []
    assert loaded.security.protectedPaths.presets == ["essential"]
    assert loaded.security.protectedPaths.custom[0].path == "/srv"
    # A hand-edited file with junk still loads.
    paths.settings_json.write_text(json.dumps(
        {"security": {"blockedCommands": {"presets": "x", "custom": [1, "rm -rf"]},
                      "protectedPaths": {"custom": [{"path": "/a", "level": "bogus"}, 3]}}}
    ))
    loaded = Settings.load(paths)
    assert loaded.security.blockedCommands.custom == ["rm -rf"]
    assert loaded.security.protectedPaths.custom[0].level == "all"  # fails closed


def test_external_rows_say_when_the_lists_do_not_apply(tmp_path: Path) -> None:
    from snowpea_core.external.manager import ExternalManager

    manager = ExternalManager.__new__(ExternalManager)
    manager.approvals = SimpleNamespace(settings=Settings())  # type: ignore[attr-defined]
    assert manager._lists_apply(None, "snowpea", "bridged", True) is True
    assert manager._lists_apply(None, "snowpea", "bridged", False) is False
    assert manager._lists_apply(None, "tmux", "person", True) is False
    assert manager._lists_apply(None, "terminal", "none", True) is False
    empty = Settings.model_validate({"security": {"blockedCommands": {"presets": []},
                                                  "protectedPaths": {"presets": []}}})
    manager.approvals = SimpleNamespace(settings=empty)  # type: ignore[attr-defined]
    assert manager._lists_apply(None, "snowpea", "bridged", False) is None
