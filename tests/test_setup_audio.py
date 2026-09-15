"""The setup wizard's Audio section (CORE-multimodal)."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
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
    # There is no "auto" row any more: the first row is the recommended
    # engine, which is what a voice screen pre-selects.
    assert stt[0].id == catalog.RECOMMENDED_STT and stt[0].default
    assert tts[0].id == catalog.RECOMMENDED_TTS and tts[0].default
    assert catalog.AUDIO_OFF in {item.id for item in stt}
    assert catalog.AUDIO_OFF in {item.id for item in tts}


def test_only_installed_backends_are_active() -> None:
    tts = {item.id: item.active for item in catalog.tts_catalog(["piper"])}
    assert tts["piper"] is True
    assert tts["edge-tts"] is False
    # Off is always choosable; it needs nothing installed.
    assert tts[catalog.AUDIO_OFF] is True
    assert "auto" not in tts, "unset means off; there is nothing to fall back to"


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------


def test_the_screen_offers_actions_rather_than_a_list(only_path: Path) -> None:
    """Nothing installed: the one useful move is offered first and pre-selected."""
    state = WizardState()
    screen = audio_screen.build(state)
    ids = [item.id for item in screen.items]

    # Automatic is the setting, not a row: with nothing installed it is a
    # promise the machine cannot keep.
    assert "auto" not in ids
    assert ids[0] == f"{audio_screen.INSTALL_PREFIX}{catalog.RECOMMENDED_STT}"
    assert screen.items[0].label.startswith("Recommended: ")
    assert screen.items[0].default is True
    assert ids[-2:] == [audio_screen.CHOOSE_ID, SKIP]
    assert audio_screen.UNSET_LINE in screen.help


def test_the_recommendation_gives_way_to_a_status_line_once_something_is_there(
    only_path: Path,
) -> None:
    write_script(only_path, "whisper")
    state = WizardState()
    assert audio_screen.detected_stt(state) == ["local-whisper"]
    screen = audio_screen.build(state)
    ids = [item.id for item in screen.items]

    # There is something truer to say than a recommendation now, so it is said:
    # installed is not the same as chosen, and the line names the gap.
    assert audio_screen.INSTALLED_LINE.format(engine="Local whisper CLI") in screen.help
    assert audio_screen.UNSET_LINE not in screen.help
    assert not any(item.label.startswith("Recommended: ") for item in screen.items)
    # The installs are still offered, just not as the headline.
    assert f"{audio_screen.INSTALL_PREFIX}{catalog.RECOMMENDED_STT}" in ids
    assert not any(item.default for item in screen.items)


def test_the_submenu_lists_every_engine_installed_first(only_path: Path) -> None:
    write_script(only_path, "whisper")
    state = WizardState()
    screen = audio_screen.build_choose(state)
    rows = {item.id: item for item in screen.items}

    assert rows["local-whisper"].active is True
    # Every row is choosable: an inactive engine leads to its install or its
    # configuration step rather than to nothing.
    assert rows["openai"].active is True
    assert "inactive" in rows["openai"].tags
    # What works here comes first; the ways out come last, in that order.
    ids = [item.id for item in screen.items]
    assert ids.index("local-whisper") < ids.index("off") < ids.index("command")
    assert ids.index("command") < ids.index("openai")
    assert "auto" not in ids, "Automatic is not something you pin"
    assert ids[-1] == SKIP


def test_choosing_from_the_submenu_pins_the_engine(only_path: Path) -> None:
    state = WizardState()
    # The Choose row itself answers nothing; only the submenu does.
    audio_screen.apply(state, audio_screen.CHOOSE_ID)
    assert state.stt_provider == catalog.DEFAULT_STT_PROVIDER
    audio_screen.apply(state, "local-whisper")
    assert state.stt_provider == "local-whisper"
    # And the screen then says what is pinned, so it is not a hidden setting.
    row = next(
        item for item in audio_screen.build(state).items if item.id == audio_screen.CHOOSE_ID
    )
    assert "Local whisper CLI" in row.label


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


def test_tts_screen_offers_installs_and_names_what_is_installed(
    only_path: Path,
) -> None:
    write_script(only_path, "espeak-ng")
    state = WizardState()
    assert audio_screen.detected_tts(state) == ["espeak-ng"]
    screen = audio_screen.build_tts(state)
    ids = [item.id for item in screen.items]

    assert audio_screen.INSTALLED_LINE.format(engine="espeak-ng") in screen.help
    # A missing engine the daemon can fetch gets an Install row.
    assert f"{audio_screen.INSTALL_PREFIX}piper" in ids
    # A system package does not: we will not run sudo for anyone.
    assert f"{audio_screen.INSTALL_PREFIX}espeak-ng" not in ids
    # Nothing that is already installed is offered for installation either.
    assert f"{audio_screen.INSTALL_PREFIX}espeak-ng" not in ids


def test_the_tts_submenu_still_shows_every_engine_and_no_studio(only_path: Path) -> None:
    write_script(only_path, "espeak-ng")
    state = WizardState()
    rows = {item.id: item for item in audio_screen.build_choose_tts(state).items}

    assert rows["espeak-ng"].active is True
    assert "inactive" in rows["piper"].tags, "listed and marked, so the answer is on screen"
    # The studio row is gone from the voice catalog: it needs an MCP server
    # configured before it can say anything, so every machine without one saw
    # a choice that could never work.
    assert "studio" not in rows


def test_the_recommended_install_leads_the_tts_screen_when_nothing_is_there(
    only_path: Path,
) -> None:
    state = WizardState()
    screen = audio_screen.build_tts(state)
    assert screen.items[0].id == f"{audio_screen.INSTALL_PREFIX}{catalog.RECOMMENDED_TTS}"
    assert screen.items[0].default is True
    assert "auto" not in [item.id for item in screen.items]


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


def test_a_non_interactive_run_leaves_voice_unset(tmp_path: Path, only_path: Path) -> None:
    """Unset is the honest default: nothing is installed, so nothing is pinned."""
    audio = wizard.run("full", home=tmp_path / "home", interactive=False).settings.audio
    assert audio.stt.provider is None
    assert audio.stt.command is None
    assert (audio.tts.enabled, audio.tts.provider, audio.tts.autoSpeak) == (True, None, False)


def test_an_older_settings_file_reads_auto_as_unset(tmp_path: Path) -> None:
    """Migration: ``"auto"`` used to mean "try everything"; it now means off."""
    settings = Settings.model_validate(
        {"audio": {"stt": {"provider": "auto"}, "tts": {"provider": "auto"}}}
    )
    assert settings.audio.stt.provider is None
    assert settings.audio.tts.provider is None


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


# ---------------------------------------------------------------------------
# the submenu, driven through the wizard
# ---------------------------------------------------------------------------


def test_the_choose_row_opens_the_submenu_and_pins_what_it_answers(
    only_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_script(only_path, "whisper")
    seen: list[str] = []

    def ask(screen: Any, **_kw: Any) -> str:
        seen.append(screen.title)
        if screen.title == audio_screen.TITLE:
            # First time through, take the detour; afterwards, leave.
            return audio_screen.CHOOSE_ID if len(seen) == 1 else SKIP
        if screen.title == audio_screen.CHOOSE_TITLE:
            return "local-whisper"
        return SKIP

    result = wizard.run(
        "full", home=tmp_path / "home", interactive=True, ask=ask, section="audio"
    )

    assert audio_screen.CHOOSE_TITLE in seen, "the submenu was never shown"
    # The screen it came from is shown again, so Choose reads as a detour.
    assert seen.count(audio_screen.TITLE) >= 2
    assert result.state.stt_provider == "local-whisper"


def test_declining_the_submenu_pins_nothing_and_comes_back(
    only_path: Path, tmp_path: Path
) -> None:
    """Esc in the submenu means "back", not "never mind the whole question"."""
    seen: list[str] = []

    def ask(screen: Any, **_kw: Any) -> str:
        seen.append(screen.title)
        if screen.title == audio_screen.TITLE:
            return audio_screen.CHOOSE_ID if len(seen) == 1 else SKIP
        return SKIP

    before = WizardState().stt_provider
    result = wizard.run(
        "full", home=tmp_path / "home", interactive=True, ask=ask, section="audio"
    )

    assert audio_screen.CHOOSE_TITLE in seen
    assert seen.count(audio_screen.TITLE) >= 2, "it came back to the screen it left"
    assert result.state.stt_provider == before


def test_an_install_row_still_installs_and_shows_the_screen_again(
    only_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed: list[str] = []
    monkeypatch.setattr(
        audio_screen, "run_install", lambda choice, home, out=None: installed.append(choice) or True
    )
    seen: list[str] = []

    def ask(screen: Any, **_kw: Any) -> str:
        seen.append(screen.title)
        if screen.title == audio_screen.TITLE and len(seen) == 1:
            return f"{audio_screen.INSTALL_PREFIX}{catalog.RECOMMENDED_STT}"
        return SKIP

    wizard.run("full", home=tmp_path / "home", interactive=True, ask=ask, section="audio")

    assert installed == [f"{audio_screen.INSTALL_PREFIX}{catalog.RECOMMENDED_STT}"]
    assert seen.count(audio_screen.TITLE) >= 2


# ---------------------------------------------------------------------------
# every row leads somewhere
# ---------------------------------------------------------------------------


def test_every_row_has_something_it_does(only_path: Path) -> None:
    """The rule: no dead rows. Picking one always leads to an action."""
    for items in (catalog.stt_catalog([]), catalog.tts_catalog([])):
        for item in items:
            action = audio_screen.row_action(item)
            assert action in {
                audio_screen.ACTION_INSTALL,
                audio_screen.ACTION_SYSTEM,
                audio_screen.ACTION_COMMAND,
                audio_screen.ACTION_KEY,
                audio_screen.ACTION_OFF,
                audio_screen.ACTION_PIN,
            }, item.id


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("supertonic", "install"),
        ("piper", "install"),
        ("edge-tts", "install"),
        ("espeak-ng", "system"),
        ("command", "command"),
        ("openai", "key"),
        ("off", "off"),
    ],
)
def test_each_row_does_the_right_thing(only_path: Path, engine: str, expected: str) -> None:
    item = next(item for item in catalog.tts_catalog([], platform="linux") if item.id == engine)
    assert audio_screen.row_action(item) == expected


def test_an_installed_engine_is_pinned_by_picking_it(only_path: Path) -> None:
    write_script(only_path, "espeak-ng")
    item = next(
        item for item in catalog.tts_catalog(["espeak-ng"]) if item.id == "espeak-ng"
    )
    assert audio_screen.row_action(item) == audio_screen.ACTION_PIN


@pytest.mark.parametrize(
    ("platform", "present", "absent"),
    [
        ("linux", (), ("say", "powershell")),
        ("darwin", ("say",), ("powershell",)),
        ("win32", ("powershell",), ("say",)),
    ],
)
def test_an_engine_that_could_never_work_here_is_not_listed(
    platform: str, present: tuple[str, ...], absent: tuple[str, ...]
) -> None:
    """A row that leads nowhere is not a row; macOS `say` on Linux is not listed."""
    ids = {item.id for item in catalog.tts_catalog([], platform=platform)}
    for engine in present:
        assert engine in ids
    for engine in absent:
        assert engine not in ids


def test_a_pinned_engine_is_marked_in_the_submenu(only_path: Path) -> None:
    write_script(only_path, "espeak-ng")
    state = WizardState(tts_provider="espeak-ng")
    row = next(
        item for item in audio_screen.build_choose_tts(state).items if item.id == "espeak-ng"
    )
    assert row.label.startswith("★ ")
    assert "pinned" in row.tags


@pytest.mark.parametrize(
    ("template", "direction", "problem"),
    [
        ("", "tts", "a command is required"),
        ("piper --text {text}", "tts", "must contain {out}"),
        ("whisper", "stt", "must contain {path}"),
        ("no-such-binary {text} {out}", "tts", "not on PATH"),
        ("sh -c {text} {out}", "tts", None),
        ("sh -c {path}", "stt", None),
    ],
)
def test_a_custom_command_is_checked_before_it_is_saved(
    template: str, direction: str, problem: str | None
) -> None:
    """A template missing {out} fails at the first spoken reply, three screens later."""
    result = audio_screen.validate_command(template, direction)
    if problem is None:
        assert result is None
    else:
        assert result is not None and problem in result


def test_installing_does_not_pin(only_path: Path, tmp_path: Path, monkeypatch) -> None:
    """Install and configure are separate steps; the status line says so."""
    monkeypatch.setattr(audio_screen, "run_install", lambda choice, home, out=None: True)
    seen: list[str] = []
    said: list[str] = []

    def ask(screen, **_kw):
        seen.append(screen.title)
        first_visit = seen.count(screen.title) == 1
        if screen.title == audio_screen.TTS_TITLE and first_visit:
            return f"{audio_screen.INSTALL_PREFIX}{catalog.RECOMMENDED_TTS}"
        return SKIP

    result = wizard.run(
        "full",
        home=tmp_path / "home",
        interactive=True,
        ask=ask,
        section="audio",
        console=SimpleNamespace(print=lambda text="", **kw: said.append(str(text))),
    )

    assert result.state.tts_provider is None, "an install is not a choice"
    assert any("pick it to use it" in line for line in said), said
