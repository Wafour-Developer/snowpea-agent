"""Headless parity for saved sessions, project teams and model profiles.

These four subcommands (GAP-14..17) were reachable only from the TUI or the
interactive wizard: a shell user could not list or purge saved sessions,
continue one, configure a project team, or see which model profile an agent
routes to.  Every case here goes through :func:`build_parser` so the argparse
wiring is covered alongside the behaviour.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, fake_provider, make_daemon

from snowpea_core.agent.definition import builtin_agent_definitions
from snowpea_core.cli import commands as cli_commands
from snowpea_core.cli.main import build_parser, run_headless
from snowpea_core.cli.main import main as cli_main
from snowpea_core.commands import agent_cmd, team_cmd, workers_cmd
from snowpea_core.commands.registry import CommandContext
from snowpea_core.config.project import ProjectSettings
from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()


async def run(daemon: Daemon, *argv: str) -> int:
    """Parse ``snowpea <argv>`` and dispatch it against this daemon."""
    args = build_parser().parse_args(list(argv))
    return await cli_commands.dispatch(args, daemon.paths.home)


# ---------------------------------------------------------------------------
# GAP-14 — session list / delete / clear
# ---------------------------------------------------------------------------


async def test_session_list_shows_saved_sessions_and_their_last_prompt(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        created = await client.ok("session.create", {"workdir": str(workdir)})
        session_id = str(created["sessionId"])
        await client.ok("session.close", {"sessionId": session_id})
        await client.stop()

    # Live-only is the default, so a closed session is invisible without the flag.
    assert await run(daemon, "session", "list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []

    assert await run(daemon, "session", "list", "--include-closed", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["sessionId"] for row in rows] == [session_id]

    # And scoped to a directory.
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert (
        await run(
            daemon, "session", "list", "--include-closed", "--workdir", str(other), "--json"
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == []


async def test_session_delete_and_clear(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        saved = []
        for _ in range(2):
            created = await client.ok("session.create", {"workdir": str(workdir)})
            saved.append(str(created["sessionId"]))
            await client.ok("session.close", {"sessionId": created["sessionId"]})
        await client.stop()

    assert await run(daemon, "session", "delete", saved[0], "--json") == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 1

    assert await run(daemon, "session", "clear", "--workdir", str(workdir), "--json") == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 1

    assert await run(daemon, "session", "list", "--include-closed", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


async def test_session_without_an_action_is_a_usage_error(daemon: Daemon) -> None:
    assert await run(daemon, "session") == cli_commands.EXIT_USAGE


# ---------------------------------------------------------------------------
# GAP-16 — project teams
# ---------------------------------------------------------------------------


async def test_team_create_use_list_and_delete(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    assert (
        await run(
            daemon, "team", "create", "delivery", "architect", "executor",
            "--workdir", str(workdir), "--json",
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"] == ["architect", "executor"]
    # The first team created becomes the active one; the daemon reads the same
    # file that ``/team create`` writes.
    assert payload["active"] == "delivery"
    project = ProjectSettings.load(workdir)
    assert project.agents.teams["delivery"] == ["architect", "executor"]

    assert (
        await run(
            daemon, "team", "create", "review", "critic", "--workdir", str(workdir), "--json"
        )
        == 0
    )
    capsys.readouterr()
    assert await run(daemon, "team", "use", "review", "--workdir", str(workdir), "--json") == 0
    assert json.loads(capsys.readouterr().out)["active"] == "review"

    assert await run(daemon, "team", "list", "--workdir", str(workdir), "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["active"] == "review"
    assert set(listed["project"]) == {"delivery", "review"}

    assert await run(daemon, "team", "delete", "review", "--workdir", str(workdir), "--json") == 0
    deleted = json.loads(capsys.readouterr().out)
    assert deleted["deleted"] == "review"
    # Deleting the active team leaves no active team rather than a dangling name.
    assert deleted["active"] is None
    assert "review" not in ProjectSettings.load(workdir).agents.teams


async def test_team_create_refuses_an_unknown_agent(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    code = await run(
        daemon, "team", "create", "bogus", "no-such-agent", "--workdir", str(workdir)
    )
    assert code == cli_commands.EXIT_USAGE
    assert "unknown agents: no-such-agent" in capsys.readouterr().err
    assert not ProjectSettings.load(workdir).agents.teams


async def test_team_use_refuses_an_unknown_team(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    assert (
        await run(daemon, "team", "use", "nope", "--workdir", str(workdir))
        == cli_commands.EXIT_USAGE
    )


async def test_team_count_forwards_to_workers_with_a_compatibility_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = SimpleNamespace()
    session = SimpleNamespace(id="s-1", job_id=None)
    seen: list[tuple[int, str]] = []
    said: list[str] = []

    class FakeManager:
        async def start(self, _session: Any, workers: int, task: str) -> str:
            seen.append((workers, task))
            return "tm-compat"

        async def status(self, _team_id: str | None = None) -> Any:
            class Status:
                tasks: list[Any] = []

            return Status()

        async def wait(self, _team_id: str) -> None:
            return None

    monkeypatch.setattr(workers_cmd, "get_manager_for", lambda _core: FakeManager())
    ctx = CommandContext(core=core, session=session, turn_id="turn-1")

    async def say(text: str) -> None:
        said.append(text)

    ctx.say = say  # type: ignore[method-assign]

    await team_cmd.cmd_team(ctx, '3 "add docstrings"')

    assert seen == [(3, "add docstrings")]
    assert said[0].startswith(f"{team_cmd.WORKERS_COMPAT_NOTE}\n")
    assert "tm-compat: 0 tasks across 3 worktrees." in said[0]
    assert said[1] == "tm-compat finished: 0 merged, 0 failed."


async def test_team_pipeline_report_failure_detection_tracks_evidence() -> None:
    assert not team_cmd._pipeline_report_failed(
        "stages: implement=executor, test=test-engineer, review=critic\n"
        "tasks: 1/1 finished\n"
        "tests: TESTS: PASS\n"
        "review: APPROVE\n\n"
        "Nothing was left unfinished."
    )
    assert team_cmd._pipeline_report_failed(
        "stages: implement=executor, test=test-engineer, review=critic\n"
        "tasks: 1/1 finished\n"
        "tests: NEEDS_MORE_EVIDENCE\n"
        "review: NO_VERDICT\n\n"
        "Nothing was left unfinished."
    )
    assert team_cmd._pipeline_report_failed(
        "stages: implement=executor\n"
        "tasks: 0/1 finished\n"
        "tests: not run\n"
        "review: not run\n\n"
        "Left unfinished:\n- implement failed"
    )


async def test_claude_agent_directories_keep_their_own_source_label(tmp_path: Path) -> None:
    """``~/.claude/agents`` is not this project (validation report §4.4)."""
    home = tmp_path / "snowpea-home"
    core = SimpleNamespace(paths=SimpleNamespace(home=home))
    global_agent = home / ".claude" / "agents" / "helper.md"
    project_agent = tmp_path / "project" / ".claude" / "agents" / "helper.md"
    own_agent = tmp_path / "project" / ".snowpea" / "agents" / "helper.md"
    for path in (global_agent, project_agent, own_agent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: helper\n---\nbody\n", encoding="utf-8")

    label = agent_cmd._agent_source_label
    assert label(global_agent, "project", core) == "claude-global"
    assert label(project_agent, "project", core) == "claude-project"
    assert label(own_agent, "project", core) == "project"


async def test_the_lookalike_builtins_say_how_they_differ() -> None:
    """`explore`/`explorer` and `reviewer`/`critic` are told apart in the list."""
    by_name = {defn.name: defn for defn in builtin_agent_definitions()}
    assert "explorer" in by_name["explore"].description
    assert "critic" in by_name["reviewer"].description
    for name in ("explore", "reviewer"):
        assert "built-in" in by_name[name].description.lower()


# ---------------------------------------------------------------------------
# GAP-17 — model profiles
# ---------------------------------------------------------------------------


async def test_model_profiles_and_assignment(
    daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        await client.ok(
            "settings.set",
            {
                "scope": "global",
                "patch": {
                    "models": {
                        "profiles": {
                            "fast": {"provider": "openai", "model": "gpt-5-mini"},
                            "deep": {"provider": "anthropic", "model": "claude-opus-5"},
                        }
                    }
                },
            },
        )
        await client.stop()

    assert await run(daemon, "model", "default", "fast", "--json") == 0
    capsys.readouterr()
    assert await run(daemon, "model", "assign", "executor", "deep", "--json") == 0
    capsys.readouterr()

    assert await run(daemon, "model", "profiles", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default"] == "fast"
    assert payload["agents"] == {"executor": "deep"}
    assert payload["profiles"]["deep"]["model"] == "claude-opus-5"


async def test_model_assign_rejects_an_unknown_profile(
    daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    """The settings validator is the gate; the CLI just reports its refusal."""
    assert await run(daemon, "model", "assign", "executor", "no-such-profile") == (
        cli_commands.EXIT_USAGE
    )
    assert "unknown model profile" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# GAP-15 — snowpea -c "…" --resume <sessionId>
# ---------------------------------------------------------------------------

FAKE_FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "session.json"


@pytest_asyncio.fixture
async def fake_daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FAKE_FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


async def test_headless_resume_continues_a_saved_session(
    fake_daemon: Daemon, tmp_path: Path
) -> None:
    """A headless run can continue a persisted session instead of opening one.

    Before this, ``run_headless`` always created a fresh session, so a shell
    user had no equivalent of the TUI's ``/resume``.
    """
    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, fake_daemon)
        created = await client.ok("session.create", {"workdir": str(workdir)})
        session_id = str(created["sessionId"])
        turn = await client.ok(
            "session.prompt", {"sessionId": session_id, "text": "first question"}
        )
        assert await client.wait_turn(turn["turnId"]) == "complete"
        await client.ok("session.close", {"sessionId": session_id})
        await client.stop()

    args = build_parser().parse_args(
        ["-c", "second question", "--resume", session_id, "--cwd", str(workdir)]
    )
    assert await run_headless(args, str(fake_daemon.paths.home)) == 0

    # The follow-up landed in the *same* session, after the first exchange.
    store = fake_daemon.core.store
    assert store is not None
    messages = await store.messages(session_id)
    prompts = [item for item in messages if item["role"] == "user"]
    assert len(prompts) >= 2


async def test_resume_needs_an_unknown_session_to_fail_cleanly(
    fake_daemon: Daemon, tmp_path: Path
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    args = build_parser().parse_args(
        ["-c", "hello", "--resume", "s-nope", "--cwd", str(workdir)]
    )
    assert await run_headless(args, str(fake_daemon.paths.home)) != 0


async def test_resume_without_a_prompt_is_a_usage_error() -> None:
    """``--resume`` alone would otherwise be silently dropped on the TUI path."""
    assert cli_main(["--resume", "s-1"]) == cli_commands.EXIT_USAGE


# ---------------------------------------------------------------------------
# CORE-model-assignment: session.setModel, project models, clearing
# ---------------------------------------------------------------------------


async def _profiles(client: Any) -> None:
    await client.ok(
        "settings.set",
        {
            "scope": "global",
            "patch": {
                "models": {
                    "default": "fast",
                    "profiles": {
                        "fast": {"provider": "openai", "model": "gpt-fast"},
                        "deep": {"provider": "anthropic", "model": "claude-deep"},
                    },
                }
            },
        },
    )


async def test_session_set_model_pins_and_clears(daemon: Daemon, tmp_path: Path) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            await _profiles(client)
            created = await client.ok("session.create", {"workdir": str(workdir)})
            session_id = str(created["sessionId"])

            pinned = await client.ok(
                "session.setModel", {"sessionId": session_id, "model": "deep"}
            )
            assert (pinned["provider"], pinned["model"]) == ("anthropic", "claude-deep")
            assert pinned["pinned"] is True
            # The surfaces are told, so a HUD fed once by session/ready updates.
            assert client.of_kind("model.changed")[-1]["payload"]["model"] == "claude-deep"

            cleared = await client.ok(
                "session.setModel", {"sessionId": session_id, "model": None}
            )
            assert cleared["model"] == "gpt-fast"  # back to models.default
            assert cleared["pinned"] is False

            frame = await client.call(
                "session.setModel", {"sessionId": session_id, "model": "nope"}
            )
            assert frame.get("error") is not None
            assert "unknown model" in str(frame["error"])
        finally:
            await client.stop()


async def test_model_profiles_shows_the_project_merged_view(
    daemon: Daemon, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        await _profiles(client)
        await client.stop()

    assert (
        await run(daemon, "model", "default", "deep", "--project", "--workdir", str(workdir),
                  "--json")
        == 0
    )
    capsys.readouterr()
    assert (
        await run(daemon, "model", "assign", "executor", "deep", "--project",
                  "--workdir", str(workdir), "--json")
        == 0
    )
    capsys.readouterr()

    assert await run(daemon, "model", "profiles", "--workdir", str(workdir), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["globalDefault"] == "fast"
    assert payload["projectDefault"] == "deep"
    assert payload["default"] == "deep"  # project wins
    assert payload["agents"] == {"executor": "deep"}
    assert payload["project"]["agents"] == {"executor": "deep"}


async def test_model_assign_can_clear_a_global_assignment(
    daemon: Daemon, capsys: pytest.CaptureFixture[str]
) -> None:
    """Clearing rides the null delete sentinel; it used to be impossible."""
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        await _profiles(client)
        await client.stop()

    assert await run(daemon, "model", "assign", "executor", "deep", "--json") == 0
    capsys.readouterr()
    assert await run(daemon, "model", "profiles", "--json") == 0
    assert json.loads(capsys.readouterr().out)["agents"] == {"executor": "deep"}

    assert await run(daemon, "model", "assign", "executor") == 0
    assert "cleared" in capsys.readouterr().out
    assert await run(daemon, "model", "profiles", "--json") == 0
    assert json.loads(capsys.readouterr().out)["agents"] == {}
