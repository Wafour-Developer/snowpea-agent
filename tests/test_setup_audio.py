"""The setup wizard's Audio section (CORE-multimodal)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.setup import catalog, wizard
from snowpea_core.setup.screens import audio as audio_screen
from snowpea_core.setup.state import SKIP, WizardState


@pytest.fixture
def only_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return bin_dir


def write_script(directory: Path, name: str) -> Path:
    script = directory / name
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def test_catalogs_are_free_first_and_offer_off() -> None:
    stt = catalog.stt_catalog()
    tts = catalog.tts_catalog()
    # assert_free_first already ran inside; this is the contract it enforces.
    assert [item.rank() for item in stt] == sorted(item.rank() for item in stt)
    assert [item.rank() for item in tts] == sorted(item.rank() for item in tts)
    assert stt[0].id == "auto" and stt[0].default
    assert tts[0].id == "auto" and tts[0].default
    assert catalog.AUDIO_OFF in {item.id for item in stt}
    assert catalog.AUDIO_OFF in {item.id for item in tts}


def test_only_installed_backends_are_active() -> None:
    tts = {item.id: item.active for item in catalog.tts_catalog(["piper"])}
    assert tts["piper"] is True
    assert tts["edge-tts"] is False
    # auto and off are always choosable; they need nothing installed.
    assert tts["auto"] is True and tts[catalog.AUDIO_OFF] is True


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------


def test_the_screen_shows_what_is_installed(only_path: Path) -> None:
    write_script(only_path, "whisper")
    state = WizardState()
    assert audio_screen.detected_stt(state) == ["local-whisper"]
    screen = audio_screen.build(state)
    rows = {item.id: item for item in screen.items}
    assert rows["local-whisper"].active is True
    assert rows["openai"].active is False
    assert rows["auto"].selected is True


def test_apply_records_the_choice_and_skip_does_not(only_path: Path) -> None:
    state = WizardState()
    audio_screen.apply(state, "local-whisper")
    assert state.stt_provider == "local-whisper"
    audio_screen.apply(state, SKIP)
    assert state.stt_provider == "local-whisper"


def test_choosing_off_clears_auto_speak(only_path: Path) -> None:
    state = WizardState(auto_speak=True)
    audio_screen.apply_tts(state, catalog.AUDIO_OFF)
    assert state.tts_provider == catalog.AUDIO_OFF
    assert state.auto_speak is False


def test_tts_screen_lists_detected_backends(only_path: Path) -> None:
    write_script(only_path, "espeak-ng")
    state = WizardState()
    assert audio_screen.detected_tts(state) == ["espeak-ng"]
    rows = {item.id: item for item in audio_screen.build_tts(state).items}
    assert rows["espeak-ng"].active is True
    assert rows["studio"].active is False


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def test_audio_block_is_what_gets_written() -> None:
    state = WizardState(
        stt_provider="command",
        stt_command="my-stt {path}",
        tts_provider="piper",
        tts_voice="ko-KR",
        auto_speak=True,
    )
    assert state.audio_block() == {
        "stt": {"provider": "command", "command": "my-stt {path}"},
        "tts": {
            "enabled": True,
            "provider": "piper",
            "autoSpeak": True,
            "voice": "ko-KR",
        },
    }


def test_off_writes_tts_disabled() -> None:
    block = WizardState(tts_provider=catalog.AUDIO_OFF).audio_block()
    assert block["tts"]["enabled"] is False


def test_state_round_trips_through_settings() -> None:
    settings = Settings.model_validate(
        {
            "audio": {
                "stt": {"provider": "local-whisper"},
                "tts": {"provider": "piper", "voice": "ko", "autoSpeak": True},
            }
        }
    )
    state = WizardState.from_settings(settings)
    assert state.stt_provider == "local-whisper"
    assert state.tts_provider == "piper"
    assert state.tts_voice == "ko"
    assert state.auto_speak is True


def test_disabled_tts_reads_back_as_off() -> None:
    settings = Settings.model_validate({"audio": {"tts": {"enabled": False}}})
    assert WizardState.from_settings(settings).tts_provider == catalog.AUDIO_OFF


def test_the_summary_names_the_audio_answers() -> None:
    state = WizardState(stt_provider="local-whisper", tts_provider="piper", auto_speak=True)
    line = next(row for row in state.summary() if row.startswith("audio"))
    assert "in local-whisper" in line
    assert "out piper" in line
    assert "auto-speak" in line


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------


def test_audio_is_its_own_section() -> None:
    assert wizard.SECTIONS["audio"][0] == "audio"
    assert wizard.SECTIONS["voice"][0] == "audio"


def test_a_non_interactive_run_keeps_auto(tmp_path: Path, only_path: Path) -> None:
    audio = wizard.run("full", home=tmp_path / "home", interactive=False).settings.audio
    assert audio.stt.provider == "auto"
    assert audio.stt.command is None
    assert (audio.tts.enabled, audio.tts.provider, audio.tts.autoSpeak) == (True, "auto", False)


def test_picking_a_voice_asks_for_the_details(
    tmp_path: Path, only_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choosing a backend is followed by voice, auto-speak and the test offer."""
    from snowpea_core.setup import ui

    write_script(only_path, "espeak-ng")
    asked: list[str] = []
    answers = iter(["ko-KR", "y", "n"])

    def fake_ask_text(prompt: str, *, interactive: Any = None, secret: bool = False) -> str:
        asked.append(prompt)
        return next(answers, "")

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)

    def ask(screen: Any, *, console: Any = None, interactive: bool = True) -> Any:
        if screen.title == audio_screen.TITLE:
            return "local-whisper"
        if screen.title == audio_screen.TTS_TITLE:
            return "espeak-ng"
        return SKIP

    result = wizard.run(
        "full", home=tmp_path / "home", section="audio", interactive=True, ask=ask
    )
    assert [prompt.split()[0] for prompt in asked] == ["voice", "read", "test"]
    audio = result.settings.audio
    assert audio.stt.provider == "local-whisper"
    assert audio.tts.enabled is True
    assert audio.tts.provider == "espeak-ng"
    assert audio.tts.autoSpeak is True
    assert audio.tts.voice == "ko-KR"


def test_skipping_the_audio_screens_asks_nothing(
    tmp_path: Path, only_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving it on Automatic must not cost the user three more questions."""
    from snowpea_core.setup import ui

    asked: list[str] = []

    def fake_ask_text(prompt: str, *, interactive: Any = None, secret: bool = False) -> str:
        asked.append(prompt)
        return ""

    monkeypatch.setattr(ui, "ask_text", fake_ask_text)

    def ask(screen: Any, *, console: Any = None, interactive: bool = True) -> Any:
        return SKIP

    wizard.run("full", home=tmp_path / "home", section="audio", interactive=True, ask=ask)
    assert asked == []


def test_the_voice_test_reports_a_failure_without_stopping_setup(
    tmp_path: Path, only_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No player installed: the test says so and the answers are still written."""
    from snowpea_core.setup import ui

    write_script(only_path, "espeak-ng")
    printed: list[str] = []
    answers = iter(["", "n", "y"])
    monkeypatch.setattr(
        ui,
        "ask_text",
        lambda prompt, interactive=None, secret=False: next(answers, ""),
    )

    def ask(screen: Any, *, console: Any = None, interactive: bool = True) -> Any:
        return "espeak-ng" if screen.title == audio_screen.TTS_TITLE else SKIP

    state = WizardState()
    wizard._test_voice(state, printed.append)
    assert printed  # it said something rather than raising
    result = wizard.run(
        "full", home=tmp_path / "home", section="audio", interactive=True, ask=ask
    )
    assert result.settings.audio.tts.provider == "espeak-ng"
