"""The built-in voice tools: ``transcribe_audio`` and ``text_to_speech``."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.tools import audio_tools
from snowpea_core.tools.registry import ToolContext, effective_permission


class FakeTools:
    """Just enough registry for ``refresh_state``."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}

    def set_state(self, name: str, state: str) -> None:
        self.states[name] = state


def make_core(tmp_path: Path, settings: dict[str, Any] | None = None) -> Any:
    resolved = Settings.model_validate(settings or {})
    return SimpleNamespace(
        paths=Paths(home=tmp_path / "home"),
        settings=resolved,
        providers=ProviderRegistry(resolved),
        tools=FakeTools(),
    )


def make_ctx(core: Any, workdir: Path) -> ToolContext:
    session = SimpleNamespace(id="s-1", workdir=str(workdir))
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


def write_script(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def only_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    # Nothing configured anywhere: no vendor keys leaking in from the developer's
    # own environment, or "auto" would resolve to the hosted providers.
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return bin_dir


#: espeak-ng stand-in: writes whatever the -w flag names.
ESPEAK = """
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  if [ "$a" = "-w" ]; then next=1; fi
done
printf 'RIFF....WAVE' > "$out"
exit 0
"""


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def test_tools_are_inactive_without_backends(only_path: Path, tmp_path: Path) -> None:
    core = make_core(tmp_path)
    assert audio_tools.refresh_state(core) == {
        "transcribe_audio": "inactive",
        "text_to_speech": "inactive",
    }
    assert core.tools.states == {"transcribe_audio": "inactive", "text_to_speech": "inactive"}


def test_tools_go_active_when_a_backend_appears(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "espeak-ng", "exit 0\n")
    write_script(only_path, "whisper", "exit 0\n")
    states = audio_tools.refresh_state(make_core(tmp_path))
    assert states == {"transcribe_audio": "active", "text_to_speech": "active"}


def test_both_tools_are_declared() -> None:
    names = {tool.name for tool in audio_tools.TOOLS}
    assert names == {"transcribe_audio", "text_to_speech"}
    for tool in audio_tools.TOOLS:
        # They start inactive and are switched on by refresh_state, the way the
        # media tools are.
        assert tool.state == "inactive"
        assert tool.category == "audio"
        assert tool.description.strip()


# ---------------------------------------------------------------------------
# transcribe_audio
# ---------------------------------------------------------------------------


async def test_transcribe_without_a_backend_explains_itself(
    only_path: Path, tmp_path: Path
) -> None:
    core = make_core(tmp_path)
    result = await audio_tools.run_transcribe_audio(
        make_ctx(core, tmp_path), {"path": "clip.wav"}
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("tool_inactive:")
    assert "whisper" in result.error


async def test_transcribe_runs_the_configured_command(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "my-stt", 'printf "a voice memo"\n')
    core = make_core(
        tmp_path, {"audio": {"stt": {"provider": "command", "command": "my-stt {path}"}}}
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "memo.wav").write_bytes(b"RIFF")

    # A relative path resolves against the session's workdir.
    result = await audio_tools.run_transcribe_audio(
        make_ctx(core, workdir), {"path": "memo.wav"}
    )
    assert result.ok
    assert result.output == "a voice memo"
    assert result.meta == {"provider": "command", "path": str(workdir / "memo.wav")}


async def test_transcribe_needs_an_existing_file(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "my-stt", "exit 0\n")
    core = make_core(
        tmp_path, {"audio": {"stt": {"provider": "command", "command": "my-stt {path}"}}}
    )
    result = await audio_tools.run_transcribe_audio(
        make_ctx(core, tmp_path), {"path": "nope.wav"}
    )
    assert not result.ok
    assert "no such audio file" in str(result.error)

    missing_arg = await audio_tools.run_transcribe_audio(make_ctx(core, tmp_path), {})
    assert not missing_arg.ok
    assert "needs a path" in str(missing_arg.error)


# ---------------------------------------------------------------------------
# text_to_speech
# ---------------------------------------------------------------------------


async def test_text_to_speech_uses_a_local_voice(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "espeak-ng", ESPEAK)
    core = make_core(tmp_path)
    result = await audio_tools.run_text_to_speech(
        make_ctx(core, tmp_path), {"text": "snowpea ready"}
    )
    assert result.ok
    assert result.path is not None
    assert Path(result.path).read_bytes().startswith(b"RIFF")
    assert result.meta is not None
    assert result.meta["provider"] == "espeak-ng"
    assert result.meta["played"] is False
    # Written under the session's own audio directory.
    assert Path(result.path).parent.name == "s-1"


async def test_text_to_speech_can_play_it(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "espeak-ng", ESPEAK)
    played = tmp_path / "played.txt"
    write_script(only_path, "mpv", f'echo "$@" > "{played}"\nexit 0\n')
    core = make_core(tmp_path)
    result = await audio_tools.run_text_to_speech(
        make_ctx(core, tmp_path), {"text": "hello", "play": True}
    )
    assert result.ok
    assert result.meta is not None and result.meta["played"] is True
    assert "played it" in result.output
    assert str(result.path) in played.read_text()


async def test_text_to_speech_survives_a_broken_player(only_path: Path, tmp_path: Path) -> None:
    """The audio exists; failing to play it locally is not failing the call."""
    write_script(only_path, "espeak-ng", ESPEAK)
    write_script(only_path, "mpv", "exit 7\n")
    core = make_core(tmp_path)
    result = await audio_tools.run_text_to_speech(
        make_ctx(core, tmp_path), {"text": "hello", "play": True}
    )
    assert result.ok
    assert result.meta is not None and result.meta["played"] is False


async def test_text_to_speech_without_a_backend(only_path: Path, tmp_path: Path) -> None:
    core = make_core(tmp_path)
    result = await audio_tools.run_text_to_speech(make_ctx(core, tmp_path), {"text": "hi"})
    assert not result.ok
    assert str(result.error).startswith("tool_inactive:")


async def test_text_to_speech_needs_text(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "espeak-ng", ESPEAK)
    core = make_core(tmp_path)
    result = await audio_tools.run_text_to_speech(make_ctx(core, tmp_path), {"text": "  "})
    assert not result.ok
    assert "needs text" in str(result.error)


# ---------------------------------------------------------------------------
# permission tags
# ---------------------------------------------------------------------------


def tool(name: str) -> Any:
    return next(t for t in audio_tools.TOOLS if t.name == name)


def test_a_local_backend_keeps_the_read_tag(only_path: Path, tmp_path: Path) -> None:
    """No egress: a local whisper reads a file you already have."""
    write_script(only_path, "whisper", "exit 0\n")
    core = make_core(tmp_path)
    assert effective_permission(tool("transcribe_audio"), {}, None, core) == "read"


def test_a_hosted_backend_raises_it_to_network(only_path: Path, tmp_path: Path) -> None:
    core = make_core(tmp_path, {"providers": {"openai": {"api_key": "sk-x"}}})
    assert effective_permission(tool("transcribe_audio"), {}, None, core) == "network"


def test_speech_is_network_when_hosted_and_read_when_local(
    only_path: Path, tmp_path: Path
) -> None:
    write_script(only_path, "espeak-ng", "exit 0\n")
    local = make_core(tmp_path)
    assert effective_permission(tool("text_to_speech"), {}, None, local) == "read"

    hosted = make_core(tmp_path, {"providers": {"openai": {"api_key": "sk-x"}}})
    assert effective_permission(tool("text_to_speech"), {}, None, hosted) == "network"


def test_the_declared_tag_survives_a_broken_lookup(only_path: Path) -> None:
    """``effective_permission`` never widens a tag when the hook explodes."""
    assert effective_permission(tool("text_to_speech"), {}, None, None) == "network"
    assert effective_permission(tool("transcribe_audio"), {}, None, None) == "read"
