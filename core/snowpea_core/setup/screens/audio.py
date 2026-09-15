"""Audio screen — voice in (speech to text) and voice out (text to speech).

Two lists in one section, because they are one decision in the user's head:
"can I talk to it, and does it talk back".  The screen itself asks the *input*
question; the wizard asks the output question, the voice and the auto-speak
toggle straight after, the way the providers screen asks for a key and a model.

What is installed decides what is offered: a row for a CLI that is not on
``PATH`` is shown inactive rather than hidden, so the answer to "why can't I
use piper" is on screen instead of missing from it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from snowpea_core.audio import AudioConfig, stt_providers
from snowpea_core.audio import install as audio_install
from snowpea_core.audio import tts as tts_backends
from snowpea_core.setup.catalog import AUDIO_OFF, CatalogItem, stt_catalog, tts_catalog
from snowpea_core.setup.screens import Screen, ScreenItem, skip_item
from snowpea_core.setup.state import SKIP, WizardState

TITLE = "Audio — voice in and out"
HELP = (
    "Automatic picks a local backend first and never sends audio anywhere it does not "
    'have to. A row marked "inactive" is not installed here; Off turns the feature off.'
)

#: Text to speech is asked right after, by the wizard.
TTS_TITLE = "Audio — speak replies"
TTS_HELP = (
    "Which backend says things out loud. Automatic prefers whatever local voice is "
    "installed, then OpenAI. Pick an Install row to fetch one now."
)

#: Prefix marking a row that installs an engine instead of selecting one.
#: The wizard runs the install and shows the list again, so the user lands
#: back where they were with the engine now active.
INSTALL_PREFIX = "install:"


def install_target(choice: str) -> str | None:
    """The engine an ``install:`` row names, or ``None`` for a normal row."""
    text = str(choice or "")
    return text[len(INSTALL_PREFIX):] or None if text.startswith(INSTALL_PREFIX) else None


def _install_rows(items: Sequence[CatalogItem]) -> list[ScreenItem]:
    """One Install row per engine this machine could obtain but does not have.

    Only for engines that are genuinely missing: an Install button next to
    something already working is noise. A system package gets no row — its
    hint is already on its own row — because we will not run sudo for anyone.

    The recommended engine's row comes first and is the default when nothing
    is installed at all, so the wizard leads with "Recommended: … — Install"
    rather than with a list of things that do not work yet.  It is a *default*,
    not a requirement: Skip and Automatic both still degrade to whatever the
    machine turns out to have.
    """
    nothing_installed = not any(
        item.active and item.id not in {"auto", AUDIO_OFF} for item in items
    )
    rows: list[ScreenItem] = []
    for item in items:
        if item.active or not item.installable:
            continue
        recommended = item.recommended
        rows.append(
            ScreenItem(
                id=f"{INSTALL_PREFIX}{item.id}",
                label=(
                    f"Recommended: {item.label} — Install" if recommended
                    else f"Install {item.label}"
                ),
                tags=("recommended", "installs now") if recommended else ("installs now",),
                selected=recommended and nothing_installed,
                default=recommended and nothing_installed,
                active=True,
            )
        )
    rows.sort(key=lambda row: "recommended" not in row.tags)
    return rows


def _config(state: WizardState) -> AudioConfig:
    """What the state says, as the audio package's own config object."""
    return AudioConfig(
        stt_provider=state.stt_provider,
        stt_command=state.stt_command,
        tts_provider=state.tts_provider,
        tts_command=state.tts_command,
        voice=state.tts_voice,
    )


def detected_stt(state: WizardState) -> list[str]:
    """Transcription backends that would work on this machine."""
    return stt_providers(_config(state))


def detected_tts(state: WizardState) -> list[str]:
    """Speech backends that would work on this machine."""
    return tts_backends.available_providers(command=state.tts_command)


def build(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """The speech-to-text list."""
    items = list(catalog) if catalog is not None else stt_catalog(detected_stt(state))
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags,
            selected=item.id == state.stt_provider,
            default=item.default,
            active=item.active,
        )
        for item in items
    ]
    return Screen(
        title=TITLE, items=(*rows, *_install_rows(items), skip_item()), multi=False, help=HELP
    )


def build_tts(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """The text-to-speech list, shown straight after :func:`build`."""
    items = list(catalog) if catalog is not None else tts_catalog(detected_tts(state))
    rows = [
        ScreenItem(
            id=item.id,
            label=item.label,
            tags=item.tags,
            selected=item.id == state.tts_provider,
            default=item.default,
            active=item.active,
        )
        for item in items
    ]
    return Screen(
        title=TTS_TITLE,
        items=(*rows, *_install_rows(items), skip_item()),
        multi=False,
        help=TTS_HELP,
    )


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if install_target(str(choice)) is not None:
        # An Install row fetches an engine; it does not answer the question.
        return state
    if choice and choice != SKIP:
        state.stt_provider = choice
    return state


def apply_tts(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if install_target(str(choice)) is not None:
        return state
    if choice and choice != SKIP:
        state.tts_provider = choice
        if choice == AUDIO_OFF:
            # Off means off: no point keeping "read every reply aloud" on.
            state.auto_speak = False
    return state


def run_install(choice: str, home: Any, out: Any = None) -> bool:
    """Install the engine an ``install:`` row names; returns whether it worked.

    Synchronous on purpose: the wizard is a terminal flow, not an event loop,
    and it has nothing else to do while pip runs.  The log is printed as it
    arrives so a three-minute download does not look like a hang.
    """
    import asyncio

    target = install_target(choice)
    if target is None:
        return False
    say = out or print

    async def progress(line: str) -> None:
        say(f"  {line}")

    async def go() -> audio_install.InstallResult:
        return await audio_install.install(
            target, home=Path(home), progress=progress
        )

    try:
        result = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - a failed install is not a failed wizard
        say(f"could not install {target}: {type(exc).__name__}: {exc}")
        return False
    if not result.ok and result.hint:
        say(result.hint)
    return result.ok


__all__ = [
    "HELP",
    "INSTALL_PREFIX",
    "TITLE",
    "TTS_HELP",
    "TTS_TITLE",
    "apply",
    "apply_tts",
    "build",
    "build_tts",
    "detected_stt",
    "detected_tts",
    "install_target",
    "run_install",
]
