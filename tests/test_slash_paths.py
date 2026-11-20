"""A pasted absolute path is a prompt, not a slash command (m32 feedback).

``/Users/me/notes/x.md summarise this`` failed twice with "unknown command";
a first word with another path separator in it that names no registered
command goes to the model.  A short unknown name (``/foo``) still fails, as
the TUI and desktop expect.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.commands.registry import CommandRegistry
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import session_handlers
from snowpea_core.server.app_server import Core
from snowpea_core.server.protocol import CommandRunParams, SessionPromptParams

PERSON = SimpleNamespace(authenticated=True, closed=False, client_kind="tui", remote_login=False,
                         client_version="0.3.0", client_id="tui-1", surface_id="tui-s")


@pytest.mark.parametrize(
    "text",
    [
        "/Users/me/notes/x.md summarise this",
        "/Users/me/notes/x.md",
        "/tmp/build.log what failed?",
        "/tmp/",
        "/home/me/a b.txt",
        "/C:\\Users\\me\\x.md",
        "C:\\Users\\me\\x.md read it",
        "plain words",
    ],
)
def test_a_path_is_not_parsed_as_a_command(text: str) -> None:
    assert CommandRegistry().parse(text) is None


def test_short_names_still_parse_known_or_not() -> None:
    registry = CommandRegistry()
    assert registry.parse("/foo") == ("foo", "")
    assert registry.parse("/mode auto") == ("mode", "auto")
    assert registry.parse("/plugin:skill go") == ("plugin:skill", "go")


def test_a_registered_name_with_a_slash_stays_a_command() -> None:
    registry = CommandRegistry()
    registry.register(SimpleNamespace(name="team/review"))  # type: ignore[arg-type]
    assert registry.parse("/team/review now") == ("team/review", "now")


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")


@pytest.fixture
def recorded(core: Core, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"prompts": [], "commands": []}

    def start_turn(_core: Any, _session: Any, text: str, **_kwargs: Any) -> str:
        calls["prompts"].append(text)
        return "t-prompt"

    def start_command(_core: Any, _session: Any, name: str, args: str = "", *_a: Any,
                      **_k: Any) -> str:
        calls["commands"].append((name, args))
        return "t-command"

    monkeypatch.setattr(session_handlers.agent_loop, "start_turn", start_turn)
    monkeypatch.setattr(core.commands, "start", start_command)
    core.hub.subscribe = lambda *_a, **_k: None  # type: ignore[method-assign]
    return calls


async def test_session_prompt_sends_a_path_to_the_model(
    core: Core, tmp_path: Path, recorded: dict[str, list[Any]]
) -> None:
    session = await core.sessions.create(tmp_path)
    for text in ("/Users/me/x.md explain", "/tmp/build.log"):
        result = await session_handlers.session_prompt_handler(
            PERSON, SessionPromptParams(sessionId=session.id, text=text), core
        )
        assert result.turnId == "t-prompt"
    assert recorded["prompts"] == ["/Users/me/x.md explain", "/tmp/build.log"]
    assert recorded["commands"] == []


async def test_session_prompt_keeps_an_unknown_short_command(
    core: Core, tmp_path: Path, recorded: dict[str, list[Any]]
) -> None:
    session = await core.sessions.create(tmp_path)
    await session_handlers.session_prompt_handler(
        PERSON, SessionPromptParams(sessionId=session.id, text="/foo bar"), core
    )
    assert recorded["commands"] == [("foo", "bar")]
    assert recorded["prompts"] == []


async def test_command_run_with_a_path_name_becomes_a_prompt(
    core: Core, tmp_path: Path, recorded: dict[str, list[Any]]
) -> None:
    """The TUI splits slash text itself and sends the path as ``command.run``."""
    session = await core.sessions.create(tmp_path)
    result = await session_handlers.command_run_handler(
        PERSON,
        CommandRunParams(sessionId=session.id, name="Users/me/x.md", args="summarise this"),
        core,
    )
    assert result.turnId == "t-prompt"
    assert recorded["prompts"] == ["/Users/me/x.md summarise this"]

    await session_handlers.command_run_handler(
        PERSON, CommandRunParams(sessionId=session.id, name="foo", args=""), core
    )
    assert recorded["commands"] == [("foo", "")]
