"""External CLI native modes (protocol 1.56.0).

Each CLI's own modes come from one table per adapter (argv / app-server
policy / ACP mode), offered as the installed CLI's ``--help`` says; the
legacy ``mode`` still works both ways; person-only modes keep the
``unrestricted`` gate.  Everything runs against the fake CLIs in
``tests/fixtures/fake_clis/``; no real CLI is started.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from _claude_support import StubHost, pinned, starts, until
from _external_adapter import FAKE_CLAUDE
from _support import env_vars
from test_external_claude import Conn, Env

from snowpea_core.external import agy as agy_mod
from snowpea_core.external import claude as claude_mod
from snowpea_core.external import codex as codex_mod
from snowpea_core.external import cursor as cursor_mod
from snowpea_core.external import modes, proc
from snowpea_core.external.base import LaunchSpec
from snowpea_core.external.claude import ClaudeAdapter
from snowpea_core.external.proc import ExternalError
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import ExternalCliInfo, ExternalSetModeParams

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake CLIs")

#: ``claude --help`` 2.1.326, the part discovery reads (recorded 2026-11-17).
CLAUDE_HELP_2_1_326 = """\
  --permission-mode <mode>              Permission mode to use for the session
                                        (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  --permission-prompts <target>         Who answers permission prompts with
"""
#: ``agy --help`` 1.5.9 (stderr), the ``--mode`` line.
AGY_HELP_1_5_9 = (
    "Usage of agy:\n"
    "  --mode                          Set the agent execution mode for this session "
    "(accept-edits, auto, plan)\n"
    "  --model                         Model for the current CLI session\n"
)
#: ``cursor-agent --help`` 2026.11.13-eacade1, the ``--mode`` option.
CURSOR_HELP = """\
  --mode <mode>                Start in the given execution mode. plan:
                               read-only/planning (analyze, propose plans, no
                               edits). ask: Q&A style for explanations and
                               questions (read-only). (choices: "plan", "ask")
  --plan                       Start in plan mode (shorthand for --mode=plan).
"""


def grant(cli: str, tmp_path: Path) -> proc.UnrestrictedGrant:
    """A person's grant, as the manager makes one (ledger entry included)."""
    return proc.grant_unrestricted(
        proc.confirm_person(Conn("ide")), cli=cli, session_id=None, project=None,
        action="create", home=tmp_path, ledger=lambda entry: "jl-test",
    )


def ids(rows: Any) -> list[str]:
    return [row.id for row in rows]


# -- discovery: modes come from the installed CLI ------------------------------------------


def test_help_choices_read_each_clis_usage() -> None:
    assert modes.help_choices(CLAUDE_HELP_2_1_326, "--permission-mode") == [
        "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]
    assert modes.help_choices(AGY_HELP_1_5_9, "--mode") == ["accept-edits", "auto", "plan"]
    assert modes.help_choices(CURSOR_HELP, "--mode") == ["plan", "ask"]
    assert modes.help_choices("  --other <x>   (choices: \"a\", \"b\")", "--mode") is None
    assert modes.help_choices("", "--mode") is None


def test_claude_offers_what_its_help_lists() -> None:
    rows = claude_mod.claude_native_modes(CLAUDE_HELP_2_1_326)
    assert ids(rows) == ["default", "acceptEdits", "plan", "auto", "dontAsk",
                         "bypassPermissions"]
    default = rows[0]
    assert default.label == "Manual" and default.launch["permissionMode"] == "manual"
    # A version without auto (or an unreadable help) does not offer it.
    older = claude_mod.claude_native_modes(
        CLAUDE_HELP_2_1_326.replace('"auto",', "").replace('"dontAsk",', ""))
    assert "auto" not in ids(older) and "dontAsk" not in ids(older)
    assert ids(claude_mod.claude_native_modes(None)) == ["default", "plan", "bypassPermissions"]


def test_agy_and_cursor_offer_what_their_help_lists() -> None:
    assert ids(agy_mod.agy_native_modes(AGY_HELP_1_5_9, "asks")) == [
        "default", "accept-edits", "auto", "plan", "dangerously-skip-permissions"]
    # agy's own setting runs every tool unasked: no mode asks or bridges anything.
    quiet = agy_mod.agy_native_modes(AGY_HELP_1_5_9, "auto")
    assert {(row.asks, row.approvals) for row in quiet} == {("never", "none")}
    assert ids(modes.offered(cursor_mod.NATIVE_MODES, ["ask"])) == ["ask", "agent", "yolo"]
    assert ids(modes.offered(cursor_mod.NATIVE_MODES,
                             modes.help_choices(CURSOR_HELP, "--mode"))) == [
        "ask", "plan", "agent", "yolo"]


def test_tables_say_the_truth() -> None:
    """Contract rules on every table: read-only wording only for writes none; bypass
    and auto-style modes person-only; every legacy mode maps to a row of its own bucket."""
    for adapter_cls in (claude_mod.ClaudeAdapter, codex_mod.CodexAdapter, agy_mod.AgyAdapter,
                        cursor_mod.CursorAdapter):
        table = adapter_cls.native_table
        for row in table:
            if row.asks == "never" and row.writes == "anywhere":
                assert row.person_only and row.legacy == "unrestricted", row
            if row.legacy == "readonly":
                assert row.writes in ("none", "plan"), row
            assert set(row.to_wire()) == {"id", "label", "asks", "writes", "approvals",
                                          "personOnly", "legacy"}
        for legacy, native in adapter_cls.legacy_native.items():
            row = modes.find(table, native)
            assert row is not None and row.legacy == legacy
            assert not row.person_only or legacy == "unrestricted"
    claude = {row.id: row for row in claude_mod.NATIVE_MODES}
    assert claude["plan"].writes == "plan" and claude["plan"].approvals == "bridged"
    assert claude["auto"].asks == "sometimes" and claude["auto"].person_only
    assert claude["auto"].approvals == "bridged"


async def test_fake_claude_discovery_lists_native_modes() -> None:
    found = await ClaudeAdapter().discover(str(FAKE_CLAUDE))
    assert found.native_modes is not None
    assert "auto" in ids(found.native_modes)
    with env_vars(FAKE_CLAUDE_MODES="default,plan,bypassPermissions"):
        older = await ClaudeAdapter().discover(str(FAKE_CLAUDE))
    assert older.native_modes is not None
    assert ids(older.native_modes) == ["default", "plan", "bypassPermissions"]


# -- argv / policy produced by the table ------------------------------------------------------


def test_claude_argv_per_mode(tmp_path: Path) -> None:
    adapter = ClaudeAdapter()
    spec = LaunchSpec(cwd=str(tmp_path), project=None, executable=pinned(), mode="default")
    session = ["--session-id", "x"]

    def argv(mode: str, native: str, granted: bool = False) -> list[str]:
        return adapter.argv(spec, mode, grant("claude", tmp_path) if granted else None,
                            session, structured=True, native_mode=native)

    plain = argv("default", "default")
    assert plain[plain.index("--permission-mode") + 1] == "default"
    assert "--permission-prompts" in plain and "host" in plain
    plan = argv("readonly", "plan")
    assert plan[plan.index("--permission-mode") + 1] == "plan"
    assert plan[plan.index("--permission-prompts") + 1] == "host"
    for native in ("auto", "acceptEdits", "dontAsk", "bypassPermissions"):
        legacy = "unrestricted" if native == "bypassPermissions" else "default"
        with pytest.raises(ExternalError) as caught:
            argv(legacy, native)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    auto = argv("default", "auto", granted=True)
    assert "--permission-mode=auto" in auto
    assert auto[auto.index("--permission-prompts") + 1] == "host"  # still bridged
    edits = argv("default", "acceptEdits", granted=True)
    assert "--permission-mode=acceptEdits" in edits
    dont = argv("default", "dontAsk", granted=True)
    assert "--permission-mode=dontAsk" in dont
    assert dont[dont.index("--permission-prompts") + 1] == "none"
    bypass = argv("unrestricted", "bypassPermissions", granted=True)
    assert "--dangerously-skip-permissions" in bypass and "--permission-prompts" not in bypass
    # The forbidden-flag guard still stands for anything not produced by the table.
    for pair in (["--permission-mode", "auto"], ["--permission-mode=bypassPermissions"],
                 ["--permission-mode", "dontAsk"], ["--allow-dangerously-skip-permissions"]):
        with pytest.raises(ExternalError):
            proc.build_argv(pinned(), pair, forbidden=adapter.forbidden_args,
                            forbidden_pairs=adapter.forbidden_pairs)
    with pytest.raises(ExternalError) as caught:
        argv("default", "yolo")
    assert caught.value.code == errors.INVALID_PARAMS


def test_codex_policy_per_mode(tmp_path: Path) -> None:
    adapter = codex_mod.CodexAdapter()
    host = StubHost(tmp_path / "home")

    def policy(mode: str, native: str, which: str, granted: bool = False) -> Any:
        spec = LaunchSpec(cwd=str(tmp_path), project=None, executable=pinned(), mode=mode,
                          native_mode=native, grant=grant("codex", tmp_path) if granted else None)
        return codex_mod.CodexChannel(adapter, spec, host)._policy(which)

    assert policy("default", "untrusted", "thread") == (
        {"approvalPolicy": "untrusted", "sandbox": "workspace-write"}, None)
    assert policy("readonly", "read-only", "thread") == (
        {"approvalPolicy": "never", "sandbox": "read-only"}, None)
    for native, legacy in (("default", "default"), ("full-access", "unrestricted")):
        with pytest.raises(ExternalError) as caught:
            policy(legacy, native, "thread")
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    scanned, trusted = policy("default", "default", "turn", granted=True)
    assert scanned == {} and trusted is not None
    assert trusted["approvalPolicy"] == "on-request"
    assert trusted["sandboxPolicy"]["type"] == "workspaceWrite"
    scanned, trusted = policy("unrestricted", "full-access", "thread", granted=True)
    assert trusted == {"approvalPolicy": "never", "sandbox": "danger-full-access"}
    # A bypass value outside the table is still refused on the wire.
    with pytest.raises(ExternalError):
        codex_mod.check_policy({"approvalPolicy": "never", "sandbox": "workspace-write"})


def test_cursor_argv_per_mode(tmp_path: Path) -> None:
    adapter = cursor_mod.CursorAdapter(cursor_home=tmp_path / "cursor")
    host = StubHost(tmp_path / "home")

    def argv(mode: str, native: str, granted: bool = False) -> list[str]:
        spec = LaunchSpec(cwd=str(tmp_path), project=None, executable=pinned(), mode=mode,
                          native_mode=native, session_id="chat-1", resume=True,
                          grant=grant("cursor", tmp_path) if granted else None)
        return cursor_mod.CursorChannel(spec, host, adapter).argv("hello")

    ask = argv("readonly", "ask")
    assert ask[ask.index("--mode") + 1] == "ask"
    plan = argv("readonly", "plan")
    assert plan[plan.index("--mode") + 1] == "plan"
    agent = argv("default", "agent")
    assert "--mode" not in agent and "--yolo" not in agent
    with pytest.raises(ExternalError) as caught:
        argv("unrestricted", "yolo")
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert "--yolo" in argv("unrestricted", "yolo", granted=True)


def test_agy_rows_launch() -> None:
    rows = {row.id: row for row in agy_mod.NATIVE_MODES}
    assert rows["plan"].launch["acpMode"] == "plan"
    assert rows["accept-edits"].person_only and rows["auto"].person_only
    assert rows["dangerously-skip-permissions"].launch["bypass"] == (
        "--dangerously-skip-permissions",)
    assert not rows["default"].person_only and not rows["plan"].person_only


# -- the manager: validation, legacy mapping, the person gate ------------------------------


class _Env(Env):
    async def native(self, conn: Any, native_mode: str, **kwargs: Any) -> dict[str, Any]:
        return (await self.manager.create("claude", str(self.project), conn=conn,
                                          native_mode=native_mode, **kwargs))["session"]


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    setup = _Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLI_SCENARIO=None,
                  FAKE_CLI_SCENARIO_FILE=None, FAKE_CLAUDE_LOGGED_IN=None,
                  FAKE_CLAUDE_MODES=None):
        await setup.manager.start()
        try:
            yield setup
        finally:
            await setup.manager.shutdown()


def _remote() -> Conn:
    conn = Conn("web")
    conn.remote_login = True  # type: ignore[attr-defined]
    return conn


async def test_clis_row_lists_native_modes(env: _Env) -> None:
    rows = {row["id"]: row for row in await env.manager.clis()}
    claude = ExternalCliInfo.model_validate(rows["claude"])
    assert [m.id for m in claude.nativeModes] == [
        "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]
    assert claude.defaultNativeMode == "default"
    auto = next(m for m in claude.nativeModes if m.id == "auto")
    assert (auto.asks, auto.writes, auto.approvals, auto.personOnly, auto.legacy) == (
        "sometimes", "workspace", "bridged", True, "default")
    # The CLIs that are not installed here still list what every version has.
    assert rows["cursor"]["defaultNativeMode"] == "ask"


async def test_unknown_native_mode_is_refused(env: _Env) -> None:
    conn = Conn("ide")
    for bad in ("yolo", "AUTO", "default --dangerously-skip-permissions", "x" * 200):
        with pytest.raises(ExternalError) as caught:
            await env.native(conn, bad)
        assert caught.value.code == errors.INVALID_PARAMS
    assert starts(env.log) == []
    ref = (await env.native(conn, "default"))["ref"]
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_mode("claude", ref["id"], native_mode="read-only", conn=conn)
    assert caught.value.code == errors.INVALID_PARAMS
    with pytest.raises(ExternalError) as caught:
        await env.manager.set_mode("claude", ref["id"], conn=conn)
    assert caught.value.code == errors.INVALID_PARAMS


async def test_a_mode_the_installed_version_lacks_is_refused(tmp_path: Path) -> None:
    setup = _Env(tmp_path)
    with env_vars(FAKE_CLI_LOG=str(setup.log), FAKE_CLAUDE_MODES="default,plan,bypassPermissions"):
        await setup.manager.start()
        try:
            with pytest.raises(ExternalError) as caught:
                await setup.native(Conn("ide"), "auto")
            assert caught.value.code == errors.INVALID_PARAMS
            assert "auto" in caught.value.message
        finally:
            await setup.manager.shutdown()


async def test_person_only_modes_need_a_person(env: _Env) -> None:
    """auto/acceptEdits/dontAsk/bypass: never Jarvis or another in-process caller, a
    browser host, the connector or a remote web login; a person with a ledger entry."""
    for native in ("auto", "acceptEdits", "dontAsk", "bypassPermissions"):
        for conn in (None, Conn("browser"), Conn("connector"), _remote()):
            with pytest.raises(ExternalError) as caught:
                await env.native(conn, native)
            assert caught.value.code == errors.JARVIS_PERSON_ONLY, (native, conn)
    assert starts(env.log) == []
    ledger = env.home / "jarvis" / "ledger.jsonl"
    assert not ledger.exists()
    person = Conn("tui")
    row = await env.native(person, "auto")
    assert (row["mode"], row["nativeMode"]) == ("default", "auto")
    assert row["approvals"] == "bridged" and row["controlled"] is True
    entries = ledger.read_text().splitlines()
    assert len(entries) == 1 and "Auto" in entries[0] and '"kind": "unrestricted"' in entries[0]
    # setMode into one is gated the same way; out of one is not.
    plain = (await env.native(person, "default"))["ref"]
    for conn in (None, Conn("connector"), _remote()):
        with pytest.raises(ExternalError) as caught:
            await env.manager.set_mode("claude", plain["id"], native_mode="acceptEdits",
                                       conn=conn)
        assert caught.value.code == errors.JARVIS_PERSON_ONLY
    moved = (await env.manager.set_mode("claude", row["ref"]["id"], native_mode="plan",
                                        conn=None))["session"]
    assert (moved["mode"], moved["nativeMode"]) == ("readonly", "plan")


async def test_nobody_but_the_person_sends_into_a_person_only_mode(env: _Env) -> None:
    person = Conn("ide")
    row = await env.native(person, "auto")
    with pytest.raises(ExternalError) as caught:
        await env.manager.send("claude", row["ref"]["id"], "go", conn=None)
    assert caught.value.code == errors.JARVIS_PERSON_ONLY
    assert "Auto" in caught.value.message


async def test_a_continued_session_never_comes_back_person_only(env: _Env) -> None:
    person = Conn("ide")
    ref = (await env.native(person, "auto"))["ref"]
    await env.turn(person, ref)
    await env.manager.close("claude", ref["id"], conn=person)
    # Jarvis (no connection) continues it: in the default mode, not auto.
    turn = (await env.manager.send("claude", ref["id"], "again", conn=None))["turnId"]
    assert turn
    row = (await env.manager.get("claude", ref["id"]))["session"]
    assert (row["mode"], row["nativeMode"]) == ("default", "default")
    argv = starts(env.log)[-1]["argv"]
    assert "--permission-mode=auto" not in argv


async def test_legacy_mode_still_works_both_ways(env: _Env) -> None:
    conn = Conn("ide")
    row = (await env.manager.create("claude", str(env.project), mode="readonly",
                                    conn=conn))["session"]
    assert (row["mode"], row["nativeMode"]) == ("readonly", "plan")
    ref = row["ref"]
    row = (await env.manager.set_mode("claude", ref["id"], "default", conn=conn))["session"]
    assert (row["mode"], row["nativeMode"]) == ("default", "default")
    # Old clients' setMode params (mode only) still validate; nativeMode is optional.
    assert ExternalSetModeParams.model_validate(
        {"ref": {"cli": "claude", "id": ref["id"]}, "mode": "readonly"}).nativeMode is None
    assert ExternalSetModeParams.model_validate(
        {"ref": {"cli": "claude", "id": ref["id"]}, "nativeMode": "plan"}).mode is None
    # nativeMode wins over mode.
    row = (await env.manager.set_mode("claude", ref["id"], "readonly", native_mode="default",
                                      conn=conn))["session"]
    assert (row["mode"], row["nativeMode"]) == ("default", "default")
    # Created without either: the CLI's default mode.
    row = (await env.manager.create("claude", str(env.project), conn=conn))["session"]
    assert (row["mode"], row["nativeMode"]) == ("default", "default")


# -- fake-CLI integration: what the CLI still asks is bridged -------------------------------


async def test_auto_mode_still_bridges_what_the_cli_asks(env: _Env) -> None:
    conn = Conn("ide")
    scenario = env.home.parent / "auto.json"
    scenario.write_text('{"turns": ["approval", "approval"], "tools": ["write", "bash"], '
                        '"command": "make test"}')
    with env_vars(FAKE_CLI_SCENARIO_FILE=str(scenario)):
        row = await env.native(conn, "auto")
        ref = row["ref"]
        # An edit goes through unasked in auto mode (the fake's "write" tool) ...
        done = await env.turn(conn, ref, "write it")
        assert done["payload"]["reason"] == "complete"
        assert env.hub.pending() == []
        argv = starts(env.log)[-1]["argv"]
        assert "--permission-mode=auto" in argv
        # ... a command still asks: it reaches the approval queue like default mode.
        turn = (await env.manager.send("claude", ref["id"], "run it", conn=conn))["turnId"]
        pending = await until(env.hub.pending)
        assert pending[0]["tool"] == "Bash"
        assert pending[0]["external"] == {"cli": "claude", "id": ref["id"]}
        await env.approvals.respond(pending[0]["requestId"], "deny", by="client", conn=conn,
                                    reason="not now")
        done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "complete"


async def test_plan_mode_bridges_its_plan_to_the_person(env: _Env) -> None:
    conn = Conn("ide")
    with env_vars(FAKE_CLI_SCENARIO="plan"):
        row = await env.native(conn, "plan")
        ref = row["ref"]
        assert (row["mode"], row["nativeMode"], row["approvals"]) == (
            "readonly", "plan", "bridged")
        turn = (await env.manager.send("claude", ref["id"], "plan it", conn=conn))["turnId"]
        pending = await until(env.hub.pending)
        request = pending[0]
        assert request["tool"] == "ExitPlanMode" and request.get("personOnly") is True
        assert request["args"]["plan"].startswith("1. write note.txt")
        # Neither Jarvis nor the engine may answer it: it is the person's.
        for by in ("jarvis", "jarvis:auto"):
            with pytest.raises(RpcError) as caught:
                await env.approvals.respond(request["requestId"], "allow", by=by)
            assert caught.value.code == errors.JARVIS_PERSON_ONLY
        await env.approvals.respond(request["requestId"], "allow", conn=conn)
        done = await until(lambda: conn.turn_done(turn))
    assert done["payload"]["reason"] == "complete"
    row = (await env.manager.get("claude", ref["id"]))["session"]
    assert (row["mode"], row["nativeMode"]) == ("default", "default")
    states = env.hub.states()
    assert states[-1]["nativeMode"] == "default"
    # The CLI already runs in its default mode: the next turn needs no restart.
    with env_vars(FAKE_CLI_SCENARIO="plan"):
        await env.turn(conn, ref, "go on")
    assert len(starts(env.log)) == 1
