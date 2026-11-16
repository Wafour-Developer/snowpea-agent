"""Security review 6, M-2: a slash command stands for the RPC it performs.

``/update`` called ``system.update``'s handler directly, ``/desktop`` starts a
program and ``/mcp add`` ran the MCP handlers with no connection, so a remote
web login reached what the dispatcher refuses it.  ``commands/persists.py`` now
has the level ``local`` and a twin table (command -> RPC methods), and the one
command gate refuses a command to every connection its twin is refused to.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.commands.persists import COMMAND_PERSISTS, COMMAND_TWINS, persists, twins
from snowpea_core.commands.registry import gate_command
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors, settings_gate
from snowpea_core.server.app_server import Core, build_dispatcher
from snowpea_core.server.errors import RpcError
from snowpea_core.server.person_gate import PERSON_METHODS, method_refusal

pytestmark = pytest.mark.real_mode_guard


def _conn(kind: str = "ide", *, remote: bool = False, version: str = "0.3.0",
          client_id: str | None = None) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=remote, client_version=version,
                           client_id=client_id or f"{kind}-1", surface_id=f"{kind}-s")


PERSON = _conn("ide")
REMOTE = _conn("web", remote=True)
BROWSER = _conn("browser", client_id="b-1")
LEGACY = _conn("browser", version="0.2.0", client_id="b-legacy")
CONNECTOR = _conn("connector")

#: Argument strings every command is tried with (plus what its own table needs).
SAMPLE_ARGS: tuple[str, ...] = (
    "", "check", "--check", "list", "show", "get x", "catalog", "reload", "test x", "remove x",
    "enable x", "disable x", "configure x", "add x --url https://mcp.example.com",
    "add-json x {}", "providers", "openai", "save", "tool:shell",
)


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")


def test_the_local_commands() -> None:
    assert persists("update", "").level == "local"
    assert persists("update", "check").level == "person"  # only reads what is available
    assert persists("desktop", "").level == "local"
    allow = persists("allow", "tool:shell")
    assert allow.level == "local" and allow.jarvis_on  # still Jarvis's chain while it is on
    # Not local: their RPC twins are a remote login's or a browser's too.
    assert persists("mcp", "add x --url https://mcp.example.com").level == "person"
    assert persists("allowlist", "remove 3").level == "person"
    assert persists("compact", "").level == "session"


def test_a_twin_that_is_a_local_persons_makes_the_command_local() -> None:
    """Derived from the method table: no command is left behind when a method is added to it."""
    for name in COMMAND_PERSISTS:
        for args in SAMPLE_ARGS:
            local = any(PERSON_METHODS.get(method) is True for method in twins(name, args))
            if local:
                assert persists(name, args).level == "local", (name, args)


def test_twins_name_registered_methods(core: Core) -> None:
    registered = set(build_dispatcher(core).method_names())
    for name in COMMAND_TWINS:
        assert name in COMMAND_PERSISTS, name
        for args in SAMPLE_ARGS:
            assert set(twins(name, args)) <= registered, (name, args)
    assert twins("update", "", source="skill") == ()  # a command file only sends a prompt


def test_every_handler_a_command_uses_is_a_twin(core: Core) -> None:
    """A built-in command whose module calls an RPC handler must list that method as a
    twin, so the gate refuses the command wherever the dispatcher refuses the method."""
    dispatcher = build_dispatcher(core)
    method_of = {handler.__name__: method for method, handler in dispatcher._handlers.items()
                 if handler.__name__.endswith("_handler")}
    from snowpea_core.commands.registry import CommandRegistry, register_builtin_commands

    by_module: dict[str, set[str]] = {}
    for command in register_builtin_commands(CommandRegistry()).commands():
        by_module.setdefault(command.run.__module__, set()).add(command.name)
    assert "snowpea_core.commands.builtin" in by_module
    for module_name, names in by_module.items():
        source = inspect.getsource(importlib.import_module(module_name))
        used = {method_of[name] for name in set(re.findall(r"\b(\w+_handler)\(", source))
                if name in method_of}
        listed = {method for name in names for args in SAMPLE_ARGS
                  for method in twins(name, args)}
        assert used <= listed, f"{module_name}: add to COMMAND_TWINS: {sorted(used - listed)}"


#: command line -> who may start it: local person, remote login, connector,
#: legacy browser, browser without the marker, browser with it, the bound messenger.
GATE: dict[str, str] = {
    "/update": "ynnnnny",
    "/update check": "yynynyy",
    "/desktop": "ynnnnny",
    "/allow tool:shell": "ynnnnny",  # (the command itself still refuses the messenger)
    "/allowlist": "yynyyyy",  # a connector may not call permission.allowlist.list either
    "/allowlist remove 3": "ynnynyy",
    "/setup": "ynnynyy",
    "/setup providers": "ynnynyy",
    "/login openai": "yynynyy",
    "/mcp": "yynyyyy",
    "/mcp list": "yynyyyy",
    "/mcp add x --url https://mcp.example.com": "yynynyy",
    "/mcp remove x": "yynynyy",
    "/mcp test x": "yynynyy",
    "/compact": "yyyyyyy",
    "/model default x": "yynynyy",
}
WHO = ("person", "remote", "connector", "legacy", "unmarked", "marked", "messenger")


@pytest.mark.parametrize(("line", "expected"), sorted(GATE.items()))
async def test_the_gate_by_kind(core: Core, tmp_path: Path, line: str, expected: str) -> None:
    session = await core.sessions.create(tmp_path)
    name, _, args = line[1:].partition(" ")
    callers: dict[str, dict[str, Any]] = {
        "person": {"conn": PERSON}, "remote": {"conn": REMOTE}, "connector": {"conn": CONNECTOR},
        "legacy": {"conn": LEGACY}, "unmarked": {"conn": BROWSER},
        "marked": {"conn": BROWSER, "actor": "person"}, "messenger": {"caller": "messenger"},
    }
    got = ""
    for who in WHO:
        try:
            gate_command(core, session, name, args, **callers[who])
            got += "y"
        except RpcError as exc:
            assert exc.code == errors.JARVIS_PERSON_ONLY, (line, who)
            got += "n"
    assert got == expected, line


async def test_the_refusal_says_local_only(core: Core, tmp_path: Path) -> None:
    session = await core.sessions.create(tmp_path)
    for conn in (REMOTE, BROWSER, CONNECTOR):
        with pytest.raises(RpcError) as caught:
            gate_command(core, session, "update", "", conn=conn, actor="person")
        assert caught.value.data["reason"] == "local_only"
        assert caught.value.data["kind"] == settings_gate._kind(conn)


def test_a_command_is_refused_exactly_where_its_twin_is() -> None:
    """For a remote login and a connector: the command passes the twin rule iff the
    dispatcher's tables pass every twin."""
    from snowpea_core.commands.registry import _refuse_local

    for name in COMMAND_TWINS:
        for args in SAMPLE_ARGS:
            for conn in (REMOTE, CONNECTOR):
                table_ok = True
                for method in twins(name, args):
                    try:
                        method_refusal(method, conn)
                        settings_gate.kind_method_refusal(method, {}, conn, None)
                    except RpcError:
                        table_ok = False
                try:
                    _refuse_local(conn, None, name, args, "builtin",
                                  persists(name, args).level)
                    command_ok = True
                except RpcError:
                    command_ok = False
                assert command_ok is table_ok, (name, args, conn.client_kind)


async def test_update_never_reaches_the_handler_for_a_remote_login(
        core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real entry point: ``command.run`` and slash text in ``session.prompt``."""
    from snowpea_core.server import update_handlers
    from snowpea_core.server.protocol import SessionPromptParams
    from snowpea_core.server.session_handlers import session_prompt_handler

    called: list[str] = []

    async def update(*_a: Any, **_k: Any) -> Any:
        called.append("update")
        raise AssertionError("system.update must not run")

    monkeypatch.setattr(update_handlers, "update_handler", update)
    session = await core.sessions.create(tmp_path)
    core.hub.subscribe = lambda *_a, **_k: None  # type: ignore[method-assign]
    for text in ("/update", "/desktop", "/allow tool:shell"):
        with pytest.raises(RpcError) as caught:
            await session_prompt_handler(REMOTE, SessionPromptParams(
                sessionId=session.id, text=text), core)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
        assert caught.value.data["reason"] == "local_only"
        with pytest.raises(RpcError):
            core.commands.start(core, session, text[1:].split()[0],
                                " ".join(text.split()[1:]), REMOTE)
    assert called == [] and session.current_turn is None


async def test_mcp_command_from_a_remote_login_meets_the_rpc_rule(
        core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/mcp add`` with a command passes the gate (mcp.add is a remote login's) and is
    then refused by the handler like the RPC; nothing is written and nothing runs."""
    from snowpea_core.commands import mcp_cmd
    from snowpea_core.server import mcp_handlers
    from snowpea_core.tools import mcp_client, mcp_config

    started: list[Any] = []

    async def probe(config: Any) -> tuple[bool, list[Any], str | None, int]:
        started.append(config)
        return True, [], None, 1

    async def register(_core: Any, config: Any) -> None:
        started.append(config)

    monkeypatch.setattr(mcp_handlers, "probe", probe)
    monkeypatch.setattr(mcp_client, "register_config", register)
    session = await core.sessions.create(tmp_path)
    said: list[str] = []
    ctx = SimpleNamespace(core=core, session=session, conn=REMOTE, actor=None)

    async def say(text: str) -> None:
        said.append(text)

    ctx.say = say
    # Security review 7, H-1: the snapshot of the caller, not the live connection.
    judged = mcp_cmd._remote(ctx)
    assert judged.kind == "remote_login" and judged.conn is REMOTE
    with pytest.raises(RpcError) as caught:
        await mcp_cmd._add(ctx, {"name": "run", "command": "prog", "args": ["x"],
                                 "scope": "project", "workdir": str(tmp_path)})
    assert caught.value.data["reason"] == "local_only"
    path = mcp_config.config_path("project", core.paths.home, tmp_path)
    assert started == [] and "run" not in mcp_config.read_servers(path)
    # A local person's command is the same as before: no connection is passed.
    assert mcp_cmd._remote(SimpleNamespace(conn=PERSON)) is None
    assert mcp_cmd._remote(SimpleNamespace(conn=BROWSER)) is None
    assert mcp_cmd._remote(SimpleNamespace(conn=None)) is None
    await mcp_client.MANAGER.close_all()
