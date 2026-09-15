"""Audio screens — voice in (speech to text) and voice out (text to speech).

Two screens in one section, because they are one decision in the user's head:
"can I talk to it, and does it talk back".  The screen itself asks the *input*
question; the wizard asks the output question, the voice and the auto-speak
toggle straight after, the way the providers screen asks for a key and a model.

**Action-first (v0.1.x).**  These used to be a plain list of every engine with
"Automatic" at the top, which put a *setting* in a race with the one thing a
user with no engines actually needed to do.  Someone with nothing installed
picked Automatic, got silence, and had no idea what to do next.

So the rows are actions now:

* ``Recommended: <engine> (CPU) — Install`` when nothing is installed, and
  pre-selected — the one useful move, offered first;
* ``Install <engine>…`` for anything else this machine could obtain;
* ``Choose a specific engine…``, a submenu for pinning one on purpose;
* ``Skip — keep defaults``.

**Automatic is still the setting and still the default.**  It is simply no
longer a row, because it is not an action: with nothing installed it is a
promise the machine cannot keep, and with something installed the screen can
say what it will actually use.  That is the status line —
``Automatic will use <engine>`` — which replaces the Recommended callout the
moment there is an engine to name.

What is installed decides what is offered: an engine that is not on ``PATH``
still appears in the submenu, marked, so the answer to "why can't I use piper"
is on screen instead of missing from it.
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

TITLE = "Audio — voice in"
HELP = "Who transcribes the microphone. Nothing here sends audio anywhere it does not have to."

#: Text to speech is asked right after, by the wizard.
TTS_TITLE = "Audio — voice out"
TTS_HELP = "Who says the reply out loud."

#: Titles of the two submenus, reached from ``Choose a specific engine…``.
CHOOSE_TITLE = "Audio — pick a transcription engine"
CHOOSE_TTS_TITLE = "Audio — pick a speech engine"
CHOOSE_HELP = (
    "Pins one engine instead of letting Automatic decide. A row marked "
    '"inactive" is not installed here. Esc goes back.'
)

#: Prefix marking a row that installs an engine instead of selecting one.
#: The wizard runs the install and shows the screen again, so the user lands
#: back where they were with the engine now active.
INSTALL_PREFIX = "install:"

#: The row that opens the submenu.  Not a provider id, so ``apply`` knows to
#: leave the setting alone and let the wizard show the other screen.
CHOOSE_ID = "choose"
CHOOSE_LABEL = "Choose a specific engine…"

#: The status line's three states.  There is no "Automatic" any more: a
#: direction of voice is pinned to one engine or it is off.
PINNED_LINE = "Pinned: {engine}."
UNSET_LINE = "Not set — this direction of voice is off until you pick an engine."
INSTALLED_LINE = "{engine} is installed, not selected — pick it to use it."


def install_target(choice: str) -> str | None:
    """The engine an ``install:`` row names, or ``None`` for a normal row."""
    text = str(choice or "")
    return text[len(INSTALL_PREFIX):] or None if text.startswith(INSTALL_PREFIX) else None


def is_choose(choice: str | set[str]) -> bool:
    """True when the answer was ``Choose a specific engine…``."""
    if isinstance(choice, set):
        choice = next(iter(choice), "")
    return str(choice or "") == CHOOSE_ID


#: What picking a row does.  Every row has one: a list where some entries do
#: nothing when you choose them is a list that lies about being a choice.
ACTION_INSTALL = "install"   # fetch it, then come back with it active
ACTION_SYSTEM = "system"     # show the platform command, offer to run it
ACTION_COMMAND = "command"   # ask for the template, validate, self-test, pin
ACTION_KEY = "key"           # ask for the API key, pin
ACTION_OFF = "off"           # unset: that direction of voice is off
ACTION_PIN = "pin"           # it works here; pin it


def row_action(item: CatalogItem) -> str:
    """What choosing ``item`` will do.

    The rule this enforces: **no dead rows**.  Picking an engine that is not
    installed used to change a setting and produce silence, which is the worst
    of both — the user thinks they chose something and the machine knows they
    did not.  Now every row leads to an install, a configuration step, or a
    pin, and the ones that could never lead anywhere here are not listed at all
    (``catalog.PLATFORM_ONLY``).
    """
    if item.id == AUDIO_OFF:
        return ACTION_OFF
    if item.active:
        return ACTION_PIN
    if item.id == "command":
        return ACTION_COMMAND
    if item.key == "key required":
        return ACTION_KEY
    if item.installable:
        return ACTION_INSTALL
    return ACTION_SYSTEM


def _installed(items: Sequence[CatalogItem]) -> list[CatalogItem]:
    """Engines that actually work here, Automatic and Off excluded."""
    return [item for item in items if item.active and item.id not in {"auto", AUDIO_OFF}]


def status_line(
    items: Sequence[CatalogItem], detected: Sequence[str], pinned: str | None = None
) -> str:
    """The line under the title: what is pinned, or that nothing is.

    Installing and pinning are separate steps, and the line says which one is
    outstanding.  An engine on disk that nobody selected is the state a user is
    most likely to be confused by — it looks done and does nothing — so it gets
    a sentence of its own.
    """
    labels = {item.id: item.label for item in items}
    engine = (pinned or "").strip()
    if engine and engine != AUDIO_OFF:
        return PINNED_LINE.format(engine=labels.get(engine, engine))
    usable = [name for name in detected if name != AUDIO_OFF]
    if usable:
        return INSTALLED_LINE.format(engine=labels.get(usable[0], usable[0]))
    return UNSET_LINE


def _install_rows(items: Sequence[CatalogItem]) -> list[ScreenItem]:
    """The actions: install the recommended engine, then any other obtainable one.

    Only for engines that are genuinely missing — an Install button next to
    something already working is noise — and never for a system package, whose
    hint is on its own row in the submenu because we will not run sudo for
    anyone.

    The recommended row appears **only while nothing is installed**.  Once
    there is an engine, the screen has something truer to say than a
    recommendation, and says it in the status line instead.
    """
    nothing_installed = not _installed(items)
    rows: list[ScreenItem] = []
    for item in items:
        if item.active or not item.installable:
            continue
        recommended = item.recommended and nothing_installed
        rows.append(
            ScreenItem(
                id=f"{INSTALL_PREFIX}{item.id}",
                label=(
                    f"Recommended: {item.label} — Install" if recommended
                    else f"Install {item.label}…"
                ),
                tags=("recommended", "installs now") if recommended else ("installs now",),
                selected=recommended,
                default=recommended,
                active=True,
            )
        )
    rows.sort(key=lambda row: "recommended" not in row.tags)
    return rows


def _choose_row(items: Sequence[CatalogItem], pinned: str | None) -> ScreenItem:
    """The row that opens the submenu, saying what is pinned when something is."""
    label = CHOOSE_LABEL
    if pinned and pinned not in {"auto", ""}:
        labels = {item.id: item.label for item in items}
        label = f"{CHOOSE_LABEL}  (now: {labels.get(pinned, pinned)})"
    return ScreenItem(
        id=CHOOSE_ID, label=label, tags=(), selected=False, default=False, active=True
    )


def _action_screen(
    title: str,
    help_text: str,
    items: Sequence[CatalogItem],
    detected: Sequence[str],
    pinned: str | None,
) -> Screen:
    """One action-first voice screen: install rows, the submenu, Skip."""
    installs = _install_rows(items)
    rows = (*installs, _choose_row(items, pinned), skip_item())
    status = status_line(items, detected, pinned)
    return Screen(title=title, items=rows, multi=False, help=f"{help_text}\n{status}")


def _choose_screen(
    title: str, items: Sequence[CatalogItem], pinned: str | None, default_id: str
) -> Screen:
    """The submenu: every engine, installed first, then the ways out.

    Order is what makes this readable — what works here, then Off, then the
    custom command, then the one that needs an account.  An engine that is not
    installed is still listed and still marked, because "why can't I use piper"
    deserves an answer on screen.
    """
    def rank(item: CatalogItem) -> tuple[int, int]:
        if item.id == AUDIO_OFF:
            return (1, 0)
        if item.id == "command":
            return (2, 0)
        if item.key != "no key":
            return (3, 0)
        return (0, 0 if item.active else 1)

    listed = [item for item in items if item.id != "auto"]
    rows = [
        ScreenItem(
            id=item.id,
            label=f"★ {item.label}" if item.id == pinned else item.label,
            tags=(*item.tags, "pinned") if item.id == pinned else item.tags,
            selected=item.id == pinned,
            default=item.id == default_id,
            # Every row is choosable: an inactive engine leads to its install
            # or its configuration step rather than to nothing (:func:`row_action`).
            active=True,
        )
        for item in sorted(listed, key=rank)
    ]
    return Screen(
        title=title, items=(*rows, skip_item()), multi=False, help=CHOOSE_HELP
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
    """The voice-in screen: what to do, not what to believe."""
    detected = detected_stt(state)
    items = list(catalog) if catalog is not None else stt_catalog(detected)
    return _action_screen(TITLE, HELP, items, detected, state.stt_provider)


def build_choose(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """The voice-in submenu, reached from ``Choose a specific engine…``."""
    detected = detected_stt(state)
    items = list(catalog) if catalog is not None else stt_catalog(detected)
    return _choose_screen(CHOOSE_TITLE, items, state.stt_provider, default_id="")


def build_tts(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """The voice-out screen, shown straight after :func:`build`."""
    detected = detected_tts(state)
    items = list(catalog) if catalog is not None else tts_catalog(detected)
    return _action_screen(TTS_TITLE, TTS_HELP, items, detected, state.tts_provider)


def build_choose_tts(state: WizardState, catalog: Sequence[CatalogItem] | None = None) -> Screen:
    """The voice-out submenu."""
    detected = detected_tts(state)
    items = list(catalog) if catalog is not None else tts_catalog(detected)
    return _choose_screen(CHOOSE_TTS_TITLE, items, state.tts_provider, default_id="")


def apply(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if install_target(str(choice)) is not None or is_choose(choice):
        # An Install row fetches an engine and the Choose row opens a submenu;
        # neither is an answer to "which engine", so the setting is left alone.
        return state
    if choice and choice != SKIP:
        state.stt_provider = choice
    return state


def apply_tts(state: WizardState, choice: str | set[str]) -> WizardState:
    if isinstance(choice, set):
        choice = next(iter(choice), SKIP)
    if install_target(str(choice)) is not None or is_choose(choice):
        return state
    if choice and choice != SKIP:
        state.tts_provider = choice
        if choice == AUDIO_OFF:
            # Off means off: no point keeping "read every reply aloud" on.
            state.auto_speak = False
    return state


#: Placeholders a custom command template has to contain to be usable.
COMMAND_REQUIRED: dict[str, tuple[str, ...]] = {
    "stt": ("{path}",),
    "tts": ("{text}", "{out}"),
}


def validate_command(template: str, direction: str) -> str | None:
    """Why ``template`` cannot work, or ``None`` when it can.

    Checked before it is saved, because a template missing ``{out}`` fails at
    the first spoken reply with an error about an empty file, and the thing
    that was actually wrong was typed three screens earlier.
    """
    text = (template or "").strip()
    if not text:
        return "a command is required"
    import shlex

    try:
        parts = shlex.split(text)
    except ValueError as exc:
        return f"the command does not parse: {exc}"
    if not parts:
        return "a command is required"
    missing = [name for name in COMMAND_REQUIRED.get(direction, ()) if name not in text]
    if missing:
        return f"the command must contain {' and '.join(missing)}"
    import shutil

    if shutil.which(parts[0]) is None:
        return f"{parts[0]} is not on PATH"
    return None


#: Row ids for the language step's fixed answers.
LANGUAGE_AUTO = "auto"
LANGUAGE_OTHER = "other"

#: Languages the voice and language steps offer before adding the reply one.
STEP_LANGUAGES: tuple[str, ...] = ("ko", "en", "ja", "zh")

VOICE_TITLE = "Audio — voice out: which voice"
VOICE_HELP = (
    "One engine can sound like a different person per language. A row that is not "
    "installed downloads first. Skip keeps the engine's own default."
)

LANGUAGE_TITLE = "Audio — voice in: which language"
LANGUAGE_HELP = (
    "Auto-detect lets the engine work it out, or falls back to your reply language. "
    "A fixed language forces it — and for a single-language engine it picks the model."
)


def build_language(
    state: WizardState, *, reply_language: str = "", catalog_rows: Any = None
) -> Screen:
    """Which language transcription should expect (``audio.stt.language``).

    For a single-language engine this is not a hint: a sherpa Zipformer has one
    model per language, so the answer decides which model is used and the rows
    say which one each language needs.
    """
    from snowpea_core.audio.stt import MODEL_BY_LANGUAGE
    from snowpea_core.audio.stt_models import MODELS

    current = (state.stt_language or LANGUAGE_AUTO).strip().lower()
    tags = [*STEP_LANGUAGES]
    reply = (reply_language or "").strip().lower().partition("-")[0]
    if reply and reply not in tags and reply != "auto":
        tags.append(reply)

    installed = set(detected_stt(state))
    rows = [
        ScreenItem(
            id=LANGUAGE_AUTO,
            label="Auto-detect",
            tags=("recommended",),
            selected=current in {"", LANGUAGE_AUTO},
            default=True,
            active=True,
        )
    ]
    for tag in tags:
        needed = MODEL_BY_LANGUAGE.get(tag)
        note: tuple[str, ...] = ()
        if needed and needed not in installed:
            model = MODELS.get(needed)
            note = (f"needs {model.id if model else needed}",)
        rows.append(
            ScreenItem(
                id=tag,
                label=tag,
                tags=note,
                selected=current == tag,
                default=False,
                active=True,
            )
        )
    rows.append(
        ScreenItem(
            id=LANGUAGE_OTHER,
            label="Other…  (a BCP-47 tag)",
            tags=(),
            selected=bool(current) and current not in {LANGUAGE_AUTO, *tags},
            default=False,
            active=True,
        )
    )
    return Screen(
        title=LANGUAGE_TITLE, items=(*rows, skip_item()), multi=False, help=LANGUAGE_HELP
    )


def build_voices(
    voices: Sequence[Any], language: str, pinned: str | None = None
) -> Screen:
    """One language's voices for the pinned engine, installed ones first."""
    rows = [
        ScreenItem(
            id=voice.id,
            label=f"★ {voice.label}" if voice.id == pinned else voice.label,
            tags=_voice_tags(voice, pinned),
            selected=voice.id == pinned,
            default=False,
            # Every row is choosable: an uninstalled voice downloads first.
            active=True,
        )
        for voice in voices
    ]
    title = f"{VOICE_TITLE} ({language})"
    return Screen(title=title, items=(*rows, skip_item()), multi=False, help=VOICE_HELP)


def _voice_tags(voice: Any, pinned: str | None) -> tuple[str, ...]:
    tags: list[str] = []
    if voice.id == pinned:
        tags.append("pinned")
    if getattr(voice, "gender", ""):
        tags.append(str(voice.gender))
    tags.append("installed" if voice.installed else "downloads first")
    return tuple(tags)


def run_install(choice: str, home: Any, out: Any = None) -> bool:
    """Install the engine an ``install:`` row names; returns whether it worked.

    Synchronous on purpose: the wizard is a terminal flow and has nothing
    else to do while pip runs.  The log is printed as it arrives so a
    three-minute download does not look like a hang.  ``run_sync`` copes with
    being called under a running loop (``/setup`` from the TUI).
    """
    from snowpea_core.setup.sync import run_sync

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
        result = run_sync(go())
    except Exception as exc:  # noqa: BLE001 - a failed install is not a failed wizard
        say(f"could not install {target}: {type(exc).__name__}: {exc}")
        return False
    if not result.ok and result.hint:
        say(result.hint)
    return result.ok


__all__ = [
    "INSTALLED_LINE",
    "CHOOSE_HELP",
    "CHOOSE_ID",
    "CHOOSE_LABEL",
    "CHOOSE_TITLE",
    "CHOOSE_TTS_TITLE",
    "HELP",
    "INSTALL_PREFIX",
    "PINNED_LINE",
    "UNSET_LINE",
    "TITLE",
    "TTS_HELP",
    "TTS_TITLE",
    "apply",
    "apply_tts",
    "build",
    "build_choose",
    "build_choose_tts",
    "build_tts",
    "detected_stt",
    "detected_tts",
    "ACTION_COMMAND",
    "ACTION_INSTALL",
    "ACTION_KEY",
    "ACTION_OFF",
    "ACTION_PIN",
    "ACTION_SYSTEM",
    "COMMAND_REQUIRED",
    "LANGUAGE_AUTO",
    "LANGUAGE_HELP",
    "LANGUAGE_OTHER",
    "LANGUAGE_TITLE",
    "STEP_LANGUAGES",
    "VOICE_HELP",
    "VOICE_TITLE",
    "build_language",
    "build_voices",
    "install_target",
    "is_choose",
    "row_action",
    "validate_command",
    "run_install",
    "status_line",
]
