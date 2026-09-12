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

from snowpea_core.audio import AudioConfig, stt_providers
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
    "Which backend says things out loud. Automatic prefers snowpea-studio, then OpenAI, "
    "then whatever local voice is installed."
)


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
    return Screen(title=TITLE, items=(*rows, skip_item()), multi=False, help=HELP)


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
    return Screen(title=TTS_TITLE, items=(*rows, skip_item()), multi=False, help=TTS_HELP)


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if choice and choice != SKIP:
        state.stt_provider = choice
    return state


def apply_tts(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if choice and choice != SKIP:
        state.tts_provider = choice
        if choice == AUDIO_OFF:
            # Off means off: no point keeping "read every reply aloud" on.
            state.auto_speak = False
    return state


__all__ = [
    "HELP",
    "TITLE",
    "TTS_HELP",
    "TTS_TITLE",
    "apply",
    "apply_tts",
    "build",
    "build_tts",
    "detected_stt",
    "detected_tts",
]
