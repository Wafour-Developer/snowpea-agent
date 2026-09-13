"""M4 US-013: the mode x permission-tag matrix, the allowlist and project modes.

Every case drives a real in-process daemon over its WebSocket with the scripted
fake provider, so the assertions are about the events a TUI would actually see:
``approval.request`` calls, ``error{mode_denied}`` and ``turn.done`` reasons.

The ``network`` and ``send`` rows of the matrix need tools carrying those tags.
M1 ships none, so two inert dummies are registered into the running daemon's
tool registry (see :func:`register_test_tools`) rather than into the product
tool set, which US-009 owns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, make_daemon
from test_session_loop import prompt

from snowpea_core.config.project import ProjectSettings
from snowpea_core.server.app_server import Daemon
from snowpea_core.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "permissions.json"

#: prompt text -> the tool the fake provider will call for it.
TOOL_FOR_TAG: dict[str, tuple[str, str]] = {
    "read": ("use read_file", "read_file"),
    "write": ("use write_file", "write_file"),
    "exec": ("use shell", "shell"),
    "network": ("use net_fetch", "net_fetch"),
    "send": ("use send_message", "send_message"),
}

#: Contract §7, restated here so a silent edit of ``MODE_MATRIX`` fails a test.
EXPECTED: dict[tuple[str, str], str] = {
    ("plan", "read"): "allow",
    ("plan", "write"): "deny",
    ("plan", "exec"): "ask",
    ("plan", "network"): "allow",
    ("plan", "send"): "deny",
    ("accept", "read"): "allow",
    ("accept", "write"): "allow",
    ("accept", "exec"): "ask",
    ("accept", "network"): "ask",
    ("accept", "send"): "ask",
    ("auto", "read"): "allow",
    ("auto", "write"): "allow",
    ("auto", "exec"): "allow",
    ("auto", "network"): "allow",
    ("auto", "send"): "allow",
}


# ---------------------------------------------------------------------------
# test-only tools for the network / send rows
# ---------------------------------------------------------------------------


async def _net_fetch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Inert stand-in for a network tool: echoes the url, touches nothing."""
    return ToolResult(ok=True, output=f"fetched {args.get('url', '')}")


async def _send_message(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Inert stand-in for an outbound-message tool."""
    return ToolResult(ok=True, output=f"sent {args.get('text', '')}")


TEST_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="net_fetch",
        category="web",
        description="Test-only network tool.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        permission="network",
        run=_net_fetch,
        source="test",
    ),
    Tool(
        name="send_message",
        category="messaging",
        description="Test-only outbound message tool.",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        permission="send",
        run=_send_message,
        source="test",
    ),
)


def register_test_tools(registry: ToolRegistry) -> ToolRegistry:
    for tool in TEST_TOOLS:
        registry.register(tool)
    return registry


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SNOWPEA_PROVIDER", f"fake:{FIXTURE}")
    monkeypatch.setenv("SNOWPEA_TEST", "1")
    instance = await make_daemon(tmp_path / "home")
    assert instance.core is not None
    register_test_tools(instance.core.tools)
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("x\n", encoding="utf-8")
    return project


async def open_session(client: RpcClient, workdir: Path, mode: str | None = None) -> str:
    params: dict[str, Any] = {"workdir": str(workdir)}
    if mode is not None:
        params["mode"] = mode
    result = await client.ok("session.create", params)
    return str(result["sessionId"])


def project_allowlist(workdir: Path) -> list[dict[str, Any]]:
    path = ProjectSettings.path_for(workdir)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return list(raw.get("allowlist", []))


# ---------------------------------------------------------------------------
# (a) the 15-cell matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["plan", "accept", "auto"])
@pytest.mark.parametrize("tag", ["read", "write", "exec", "network", "send"])
async def test_mode_matrix(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path, mode: str, tag: str
) -> None:
    expected = EXPECTED[(mode, tag)]
    text, tool_name = TOOL_FOR_TAG[tag]

    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, mode)

    turn_id = await prompt(client, session_id, text)
    reason = await client.wait_turn(turn_id)

    codes = [event["payload"]["code"] for event in client.of_kind("error")]
    called = [call["payload"]["name"] for call in client.of_kind("tool.call")]

    if expected == "deny":
        # The tool never runs and the model is told why, but one refusal no
        # longer ends the turn (CORE-prompts, gap 3), so the scripted model
        # answers instead and the turn completes.
        assert reason == "complete", f"{mode}/{tag} should be refused, not fatal"
        assert codes == ["mode_denied"]
        assert client.approval_requests == []
        assert called == []
        refused = client.of_kind("tool.result")
        assert [item["payload"]["ok"] for item in refused] == [False]
        assert "do not retry the same call" in refused[0]["payload"]["error"]
    elif expected == "allow":
        assert reason == "complete", f"{mode}/{tag} should run: saw {codes}"
        assert client.approval_requests == []
        assert called == [tool_name]
    else:
        assert reason == "complete", f"{mode}/{tag} should ask then run: saw {codes}"
        assert len(client.approval_requests) == 1
        assert client.approval_requests[0]["tool"] == tool_name
        assert called == [tool_name]

    await client.stop()


# ---------------------------------------------------------------------------
# (b) the allowlist promotes ask -> allow (AC-13)
# ---------------------------------------------------------------------------


async def test_allowlist_add_stops_the_second_ask(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "accept")

    first = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(first) == "complete"
    assert len(client.approval_requests) == 1

    added = await client.ok(
        "permission.allowlist.add", {"pattern": "^ls( .*)?$", "scope": "project"}
    )
    assert added["patternId"]

    listed = await client.ok("permission.allowlist.list", {"scope": "project"})
    assert len(listed["patterns"]) == 1
    assert listed["patterns"][0]["pattern"] == "^ls( .*)?$"
    assert listed["patterns"][0]["scope"] == "project"

    second = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(second) == "complete"
    assert len(client.approval_requests) == 1, "the allowlisted command must not ask again"

    await client.stop()


async def test_allowlist_does_not_cover_other_commands(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "accept")
    await client.ok("permission.allowlist.add", {"pattern": "^ls( .*)?$", "scope": "project"})

    turn_id = await prompt(client, session_id, "use grep_lines")
    assert await client.wait_turn(turn_id) == "complete"
    assert len(client.approval_requests) == 1

    await client.stop()


async def test_plan_mode_asks_before_a_command_and_honours_the_allowlist(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``plan`` asks for a command instead of refusing it, so a planner can
    check a version or run the tests; an allowlisted command runs unasked."""
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "plan")

    first = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(first) == "complete"
    assert len(client.approval_requests) == 1
    assert client.of_kind("error") == []

    await client.ok("permission.allowlist.add", {"pattern": "^ls( .*)?$", "scope": "project"})
    second = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(second) == "complete"
    assert len(client.approval_requests) == 1, "the allowlisted command must not ask"
    assert [item["payload"]["ok"] for item in client.of_kind("tool.result")] == [True, True]

    await client.stop()


async def test_allowlist_never_promotes_a_denial(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``plan`` still refuses a write; nothing in the allowlist can lift that."""
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "plan")
    await client.ok("permission.allowlist.add", {"pattern": ".*", "scope": "project"})

    turn_id = await prompt(client, session_id, "use write_file")
    assert await client.wait_turn(turn_id) == "complete"
    assert [e["payload"]["code"] for e in client.of_kind("error")] == ["mode_denied"]
    assert [item["payload"]["ok"] for item in client.of_kind("tool.result")] == [False]

    await client.stop()


async def test_allowlist_remove_restores_the_ask(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "accept")
    added = await client.ok(
        "permission.allowlist.add", {"pattern": "^ls( .*)?$", "scope": "project"}
    )

    assert (await client.ok("permission.allowlist.remove", {"patternId": added["patternId"]}))[
        "ok"
    ] is True
    assert (await client.ok("permission.allowlist.list", {}))["patterns"] == []

    turn_id = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(turn_id) == "complete"
    assert len(client.approval_requests) == 1

    await client.stop()


async def test_global_scope_lands_in_the_home_settings(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    await open_session(client, workdir, "accept")
    await client.ok("permission.allowlist.add", {"pattern": "^ls( .*)?$", "scope": "always"})

    stored = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
    assert [entry["pattern"] for entry in stored["allowlist"]] == ["^ls( .*)?$"]
    assert project_allowlist(workdir) == []

    listed = await client.ok("permission.allowlist.list", {"scope": "always"})
    assert [p["scope"] for p in listed["patterns"]] == ["always"]

    await client.stop()


# ---------------------------------------------------------------------------
# (c) answering an approval with a scope records the exception
# ---------------------------------------------------------------------------


async def test_respond_with_project_scope_adds_an_allowlist_entry(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    client.approval_scope = "project"
    session_id = await open_session(client, workdir, "accept")

    turn_id = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(turn_id) == "complete"

    entries = project_allowlist(workdir)
    assert [entry["pattern"] for entry in entries] == ["^ls( .*)?$"]
    assert entries[0]["target"] == "shell"

    await client.stop()


async def test_respond_with_session_scope_keys_on_the_command(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``session`` caches ``(tool, first token)``, so a different command re-asks."""
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    client.approval_scope = "session"
    session_id = await open_session(client, workdir, "accept")

    first = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(first) == "complete"
    repeat = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(repeat) == "complete"
    assert len(client.approval_requests) == 1

    other = await prompt(client, session_id, "use grep_lines")
    assert await client.wait_turn(other) == "complete"
    assert len(client.approval_requests) == 2
    assert project_allowlist(workdir) == [], "session scope must not persist anything"

    await client.stop()


# ---------------------------------------------------------------------------
# (d) modes: commands, /mode save and the project default
# ---------------------------------------------------------------------------


async def test_plan_then_accept_emit_mode_changed(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "auto")

    plan_turn = await prompt(client, session_id, "/plan")
    assert await client.wait_turn(plan_turn) == "complete"
    accept_turn = await prompt(client, session_id, "/accept")
    assert await client.wait_turn(accept_turn) == "complete"

    assert [e["payload"]["mode"] for e in client.of_kind("mode.changed")] == ["plan", "accept"]
    assert (await client.ok("session.list"))["sessions"][0]["mode"] == "accept"

    await client.stop()


async def test_mode_save_becomes_the_project_default(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")

    plan_turn = await prompt(client, session_id, "/plan")
    assert await client.wait_turn(plan_turn) == "complete"
    save_turn = await prompt(client, session_id, "/mode save")
    assert await client.wait_turn(save_turn) == "complete"

    stored = json.loads(ProjectSettings.path_for(workdir).read_text(encoding="utf-8"))
    assert stored["defaultMode"] == "plan"

    fresh = await open_session(client, workdir)
    rows = {row["sessionId"]: row["mode"] for row in (await client.ok("session.list"))["sessions"]}
    assert rows[fresh] == "plan", "a session without an explicit mode takes the project default"

    await client.stop()


async def test_mode_show_and_unknown_mode_are_reported(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")

    show = await prompt(client, session_id, "/mode show")
    assert await client.wait_turn(show) == "complete"
    assert "Mode: accept" in client.of_kind("message.done")[-1]["payload"]["text"]

    bad = await prompt(client, session_id, "/mode sideways")
    assert await client.wait_turn(bad) == "complete"
    assert "Unknown mode" in client.of_kind("message.done")[-1]["payload"]["text"]

    await client.stop()


# ---------------------------------------------------------------------------
# (e) the /allow, /allowlist and /approvals commands
# ---------------------------------------------------------------------------


async def test_allow_command_writes_the_project_allowlist(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "accept")

    allow_turn = await prompt(client, session_id, "/allow ^ls( .*)?$")
    assert await client.wait_turn(allow_turn) == "complete"
    assert [entry["pattern"] for entry in project_allowlist(workdir)] == ["^ls( .*)?$"]

    list_turn = await prompt(client, session_id, "/allowlist")
    assert await client.wait_turn(list_turn) == "complete"
    assert "^ls( .*)?$" in client.of_kind("message.done")[-1]["payload"]["text"]

    shell_turn = await prompt(client, session_id, "use shell")
    assert await client.wait_turn(shell_turn) == "complete"
    assert client.approval_requests == []

    await client.stop()


async def test_allow_command_with_global_flag_and_tool_target(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")

    turn_id = await prompt(client, session_id, "/allow tool:send_message --global")
    assert await client.wait_turn(turn_id) == "complete"

    stored = json.loads(daemon.paths.settings_json.read_text(encoding="utf-8"))
    assert stored["allowlist"][0]["target"] == "tool:send_message"
    assert project_allowlist(workdir) == []

    send_turn = await prompt(client, session_id, "use send_message")
    assert await client.wait_turn(send_turn) == "complete"
    assert client.approval_requests == [], "an allowlisted tool tag must not ask"

    await client.stop()


async def test_approvals_command_reports_an_empty_queue(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "accept")

    turn_id = await prompt(client, session_id, "/approvals")
    assert await client.wait_turn(turn_id) == "complete"
    assert "No unattended approvals" in client.of_kind("message.done")[-1]["payload"]["text"]

    await client.stop()


async def test_help_lists_the_permission_commands(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    listed = await client.ok("command.list", {})
    names = {command["name"] for command in listed["commands"]}
    assert {"plan", "accept", "auto", "mode", "approvals", "allow", "allowlist"} <= names
    await client.stop()


# ---------------------------------------------------------------------------
# (f) the config tag: writes that land on snowpea's own settings (CORE-search-fix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["accept", "auto"])
async def test_a_config_write_asks_in_every_mode_that_runs_it(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path, mode: str
) -> None:
    """``write_file`` on ``.snowpea/settings.json`` is ``config``, not ``write``.

    Auto mode is the interesting row: ``write`` is a silent allow there, which
    is how the agent came to rewrite settings.json when asked to show it.
    """
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, mode)

    turn_id = await prompt(client, session_id, "rewrite the settings")
    assert await client.wait_turn(turn_id) == "complete"

    assert len(client.approval_requests) == 1
    request = client.approval_requests[0]
    assert request["tool"] == "write_file"
    assert request["note"] == "modifies snowpea configuration"
    assert request["risk"] == "high"

    await client.stop()


async def test_a_plain_write_still_runs_silently_in_auto_mode(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """The guard must not turn every write into a prompt."""
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "auto")

    turn_id = await prompt(client, session_id, "use write_file")
    assert await client.wait_turn(turn_id) == "complete"
    assert client.approval_requests == []

    await client.stop()


async def test_a_config_write_is_denied_in_plan_mode(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "plan")

    turn_id = await prompt(client, session_id, "rewrite the settings")
    assert await client.wait_turn(turn_id) == "complete"
    assert [event["payload"]["code"] for event in client.of_kind("error")] == ["mode_denied"]
    assert client.approval_requests == []
    assert [item["payload"]["ok"] for item in client.of_kind("tool.result")] == [False]

    await client.stop()


async def test_settings_set_asks_even_in_auto_mode(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    session_id = await open_session(client, workdir, "auto")

    turn_id = await prompt(client, session_id, "use settings_set")
    assert await client.wait_turn(turn_id) == "complete"

    assert len(client.approval_requests) == 1
    assert client.approval_requests[0]["tool"] == "settings_set"
    assert client.approval_requests[0]["note"] == "modifies snowpea configuration"

    await client.stop()


async def test_settings_get_is_read_only(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """Showing configuration needs no approval, and works in plan mode."""
    client = await connect(http, daemon)
    session_id = await open_session(client, workdir, "plan")

    turn_id = await prompt(client, session_id, "use settings_get")
    assert await client.wait_turn(turn_id) == "complete"
    assert client.approval_requests == []

    results = [event["payload"] for event in client.of_kind("tool.result")]
    assert results[-1]["ok"] is True
    assert "search.provider" in results[-1]["output"]

    await client.stop()


async def test_the_allowlist_cannot_promote_a_config_call(
    daemon: Daemon, http: aiohttp.ClientSession, workdir: Path
) -> None:
    """``always`` on a config write must not silence the next one."""
    client = await connect(http, daemon)
    client.approval_mode = "allow"
    client.approval_scope = "always"
    session_id = await open_session(client, workdir, "accept")

    first = await prompt(client, session_id, "use settings_set")
    assert await client.wait_turn(first) == "complete"
    second = await prompt(client, session_id, "use settings_set")
    assert await client.wait_turn(second) == "complete"

    assert len(client.approval_requests) == 2, "the second config call must ask again"

    await client.stop()


async def test_the_config_guard_covers_the_whole_snowpea_home(tmp_path: Path) -> None:
    """Settings, credentials, the state db, the token and the logs are all config."""
    from snowpea_core.config.paths import Paths
    from snowpea_core.config.settings import Settings
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session
    from snowpea_core.tools.registry import effective_permission, register_builtin_tools

    home = Paths.create(tmp_path / "home")
    core = Core(settings=Settings(), paths=home, token="t")
    register_builtin_tools(core.tools)
    workdir = tmp_path / "work"
    workdir.mkdir()
    session = Session(id="s-guard", workdir=workdir)

    def tag(tool_name: str, path: str) -> str:
        tool = core.tools.get(tool_name)
        assert tool is not None
        return effective_permission(tool, {"path": path}, session, core)

    for name in ("write_file", "edit_file"):
        for path in (
            str(home.settings_json),
            str(home.credentials_json),
            str(home.state_db),
            str(home.token_file),
            str(home.daemon_log),
            ".snowpea/settings.json",
            "../../.snowpea/settings.json",
        ):
            assert tag(name, path) == "config", f"{name} {path}"
        for path in (
            "src/main.py",
            "README.md",
            str(workdir / "notes.txt"),
            # A team's worktrees live under .snowpea/ but hold ordinary source;
            # a worker editing them must not be stopped for approval.
            ".snowpea/worktrees/tm-1/src/main.py",
            ".snowpea/skills/demo/SKILL.md",
        ):
            assert tag(name, path) == "write", f"{name} {path}"
        assert tag(name, ".snowpea/worktrees/tm-1/.snowpea/settings.json") == "config"

    # read_file has no override at all: showing configuration stays a read.
    read_tool = core.tools.get("read_file")
    assert read_tool is not None
    settings_path = {"path": str(home.settings_json)}
    assert effective_permission(read_tool, settings_path, session, core) == "read"
