"""Which voices each speech engine has, and which of them are on this machine.

Picking an engine is half a decision: "Supertonic" does not say whether the
Korean replies come out male or female, and "piper" without a voice file cannot
say anything at all.  So an engine is asked what it can do, and the answer is
the same shape whoever asked — the wizard, ``/setup audio``, or the
``audio.voices`` RPC.

Two kinds of engine, and the difference matters to the user:

*Multilingual* — one model, many languages, voices that work in all of them.
    Supertonic is this: 31 languages plus an ``na`` fallback out of a single
    ~400MB model, and its ten preset styles (M1-M5, F1-F5) are not per-language.
    Choosing a voice per language is still useful — a different speaker for
    Korean than for English — but nothing extra is downloaded for it.

*Per-voice model* — one file per voice, each tied to a language.
    Piper is this: a voice *is* a download, and a language with no voice file
    is a language it cannot speak.  Those rows are ``installed: False`` and
    ``audio.install {engine, voice}`` fetches them.

Everything else is discovered at run time by asking the CLI, because the answer
depends on what the machine has and a table here would be a guess.

Verified 2026-09-15: Supertonic's per-language *models* do not exist — upstream
ships one multilingual model.  The lead's note assumed otherwise; see
``docs/design/deviations/CORE-audio-install.md``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.audio.voices")

#: How long an engine gets to list its own voices.  Listing is a menu the user
#: is waiting in front of; a slow one is a bug, not something to wait out.
LIST_TIMEOUT = 5.0

#: The language key meaning "whatever the reply is in".  It is what a single
#: chosen voice is stored under, and the fallback when a language has none.
ANY = "*"

#: Languages the voice step offers tabs for before adding the reply language.
DEFAULT_LANGUAGES: tuple[str, ...] = ("ko", "en")


@dataclass(frozen=True)
class Voice:
    """One voice an engine can speak with."""

    id: str
    label: str
    #: BCP-47 tag, or :data:`ANY` for a voice that works in every language.
    language: str = ANY
    gender: str = ""
    #: False when choosing it downloads something first.
    installed: bool = True
    #: Download size, when it is a download and the size is known.
    size_bytes: int | None = None
    #: True when the engine can synthesise a preview of it here and now.
    sample: bool = True

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "language": self.language,
            "installed": self.installed,
            "sample": self.sample,
        }
        if self.gender:
            payload["gender"] = self.gender
        if self.size_bytes is not None:
            payload["sizeBytes"] = self.size_bytes
        return payload


# ---------------------------------------------------------------------------
# supertonic — one multilingual model, ten preset styles
# ---------------------------------------------------------------------------

#: The preset styles upstream ships, in the order its own docs list them.
SUPERTONIC_STYLES: tuple[tuple[str, str], ...] = (
    ("M1", "male"),
    ("M2", "male"),
    ("M3", "male"),
    ("M4", "male"),
    ("M5", "male"),
    ("F1", "female"),
    ("F2", "female"),
    ("F3", "female"),
    ("F4", "female"),
    ("F5", "female"),
)

#: Languages the model speaks.  Not a download list — there is one model — so
#: this is only what the voice step offers tabs for.
SUPERTONIC_LANGUAGES: tuple[str, ...] = (
    "en", "ko", "ja", "zh", "es", "fr", "de", "pt", "ru", "ar",
)


def supertonic_voices(installed: bool) -> list[Voice]:
    """The ten preset styles; every one works in every language it supports."""
    return [
        Voice(
            id=name,
            label=f"{name} ({gender})",
            language=ANY,
            gender=gender,
            installed=installed,
        )
        for name, gender in SUPERTONIC_STYLES
    ]


# ---------------------------------------------------------------------------
# piper — one file per voice, each tied to a language
# ---------------------------------------------------------------------------

#: Curated piper voices: the ones this project can name a URL for.  Piper has
#: hundreds; listing them all would be a catalogue nobody reads, and the
#: interesting question at setup time is "can it speak my language".
PIPER_VOICES: tuple[tuple[str, str, str, str], ...] = (
    # (voice id, language, path under the voices repo, label)
    (
        "en_US-lessac-medium",
        "en",
        "en/en_US/lessac/medium",
        "Lessac (US English, medium)",
    ),
    (
        "ko_KR-kss-medium",
        "ko",
        "ko/ko_KR/kss/medium",
        "KSS (Korean, medium)",
    ),
)

#: Roughly what one medium voice weighs, for the row's size hint.  Upstream
#: publishes no manifest of sizes, so this is the order of magnitude rather
#: than a promise.
PIPER_VOICE_BYTES = 64 * 1024 * 1024


def piper_voice_path(voice_id: str) -> str | None:
    """The path under the piper-voices repo for ``voice_id``."""
    for name, _language, path, _label in PIPER_VOICES:
        if name == voice_id:
            return path
    return None


def piper_voices(home: Path | str | None) -> list[Voice]:
    """The curated voices, each marked by whether its file is on disk."""
    from snowpea_core.audio.install import VOICES_DIRNAME

    root = Path(home).expanduser() / VOICES_DIRNAME if home else None
    out: list[Voice] = []
    for name, language, _path, label in PIPER_VOICES:
        on_disk = bool(root and (root / f"{name}.onnx").is_file())
        out.append(
            Voice(
                id=name,
                label=label,
                language=language,
                installed=on_disk,
                size_bytes=None if on_disk else PIPER_VOICE_BYTES,
                sample=on_disk,
            )
        )
    return out


# ---------------------------------------------------------------------------
# openai — a fixed set, no download
# ---------------------------------------------------------------------------

OPENAI_VOICES: tuple[str, ...] = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")


def openai_voices(configured: bool) -> list[Voice]:
    return [
        Voice(id=name, label=name, language=ANY, installed=configured, sample=configured)
        for name in OPENAI_VOICES
    ]


# ---------------------------------------------------------------------------
# the CLIs — ask them, because the answer is whatever the machine has
# ---------------------------------------------------------------------------


async def _run(argv: list[str]) -> str:
    """Run a listing command and return its stdout, or ``""``."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.debug("could not list voices with %s: %s", argv[0], exc)
        return ""
    try:
        out, _err = await asyncio.wait_for(process.communicate(), LIST_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        log.info("%s took too long to list its voices", argv[0])
        return ""
    return out.decode("utf-8", "replace")


#: ``edge-tts --list-voices`` prints ``Name: ko-KR-SunHiNeural`` blocks.
_EDGE_NAME = re.compile(r"^Name:\s*(\S+)", re.MULTILINE)
_EDGE_GENDER = re.compile(r"^Gender:\s*(\S+)", re.MULTILINE)


async def edge_voices(languages: tuple[str, ...] = DEFAULT_LANGUAGES) -> list[Voice]:
    """Ask ``edge-tts`` what it has, filtered to the languages that matter here.

    Microsoft ships hundreds; a picker listing all of them is a picker nobody
    finishes reading.
    """
    if shutil.which("edge-tts") is None:
        return []
    text = await _run(["edge-tts", "--list-voices"])
    names = _EDGE_NAME.findall(text)
    genders = _EDGE_GENDER.findall(text)
    wanted = {tag.lower() for tag in languages}
    out: list[Voice] = []
    for index, name in enumerate(names):
        language = name.split("-", 1)[0].lower()
        if language not in wanted:
            continue
        gender = genders[index].lower() if index < len(genders) else ""
        out.append(Voice(id=name, label=name, language=language, gender=gender))
    return out


#: ``espeak-ng --voices`` prints a fixed-width table; column 2 is the language.
_ESPEAK_ROW = re.compile(r"^\s*\d+\s+(\S+)\s+\S+\s+(\S+)\s+(\S.*?)\s*$", re.MULTILINE)


async def espeak_voices(languages: tuple[str, ...] = DEFAULT_LANGUAGES) -> list[Voice]:
    """Ask ``espeak-ng`` for its voice table."""
    if shutil.which("espeak-ng") is None:
        return []
    text = await _run(["espeak-ng", "--voices"])
    wanted = {tag.lower() for tag in languages}
    out: list[Voice] = []
    for language, name, label in _ESPEAK_ROW.findall(text):
        base = language.split("-", 1)[0].lower()
        if base not in wanted:
            continue
        out.append(Voice(id=name, label=f"{label} ({language})", language=base))
    return out


#: ``say -v ?`` prints ``Yuna                ko_KR    # 안녕하세요``.
_SAY_ROW = re.compile(r"^(\S[^#]*?)\s{2,}([a-z]{2}_[A-Z]{2})\s+#", re.MULTILINE)


async def say_voices(languages: tuple[str, ...] = DEFAULT_LANGUAGES) -> list[Voice]:
    """Ask macOS ``say`` for its installed voices."""
    if shutil.which("say") is None:
        return []
    text = await _run(["say", "-v", "?"])
    wanted = {tag.lower() for tag in languages}
    out: list[Voice] = []
    for name, tag in _SAY_ROW.findall(text):
        base = tag.split("_", 1)[0].lower()
        if base not in wanted:
            continue
        clean = name.strip()
        out.append(Voice(id=clean, label=f"{clean} ({tag})", language=base))
    return out


#: The one-liner that asks Windows SAPI what it has.
_SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; "
    "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() "
    "| ForEach-Object { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name }"
)


async def sapi_voices(languages: tuple[str, ...] = DEFAULT_LANGUAGES) -> list[Voice]:
    """Ask Windows SAPI through PowerShell."""
    if shutil.which("powershell") is None:
        return []
    text = await _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT])
    wanted = {tag.lower() for tag in languages}
    out: list[Voice] = []
    for line in text.splitlines():
        name, _, tag = line.strip().partition("|")
        if not name or not tag:
            continue
        base = tag.split("-", 1)[0].lower()
        if base not in wanted:
            continue
        out.append(Voice(id=name, label=f"{name} ({tag})", language=base))
    return out


# ---------------------------------------------------------------------------
# the one entry point
# ---------------------------------------------------------------------------


async def voices_for(
    engine: str,
    *,
    home: Path | str | None = None,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
    installed_engines: tuple[str, ...] = (),
    openai_key: bool = False,
) -> list[Voice]:
    """Every voice ``engine`` offers here, installed ones first.

    An engine that is not installed still answers: the wizard wants to show
    what it *would* offer, so choosing it is an informed decision rather than a
    leap.  What it cannot do is preview, which is what ``sample`` says.
    """
    name = (engine or "").strip()
    here = set(installed_engines)
    if name == "supertonic":
        found = supertonic_voices("supertonic" in here)
    elif name == "piper":
        found = piper_voices(home)
    elif name == "openai":
        found = openai_voices(openai_key)
    elif name == "edge-tts":
        found = await edge_voices(languages)
    elif name == "espeak-ng":
        found = await espeak_voices(languages)
    elif name == "say":
        found = await say_voices(languages)
    elif name == "powershell":
        found = await sapi_voices(languages)
    else:
        # ``command`` and anything unknown: the template decides, and a list of
        # voices for it would be a list of guesses.
        found = []
    return sorted(found, key=lambda voice: (not voice.installed, voice.language, voice.id))


def by_language(found: list[Voice]) -> dict[str, list[Voice]]:
    """Group voices by language, with :data:`ANY` voices offered under each."""
    languages = sorted({voice.language for voice in found if voice.language != ANY})
    grouped: dict[str, list[Voice]] = {}
    universal = [voice for voice in found if voice.language == ANY]
    for tag in languages:
        grouped[tag] = [voice for voice in found if voice.language == tag]
    if universal and not grouped:
        grouped[ANY] = universal
    elif universal:
        for tag in grouped:
            grouped[tag] = [*grouped[tag], *universal]
        grouped[ANY] = universal
    return grouped


@dataclass
class VoiceChoice:
    """What ``audio.tts.voices`` resolves to for one reply."""

    voice: str | None = None
    #: ``language`` | ``any`` | ``engine`` — which rung answered.
    source: str = "engine"
    #: Set when the engine has nothing for the language that was asked for.
    note: str = ""


def pick(voices: Any, language: str | None, known: tuple[str, ...] = ()) -> VoiceChoice:
    """The voice to speak ``language`` with, from the per-language setting.

    Order: the language's own entry, then ``*``, then nothing — which leaves
    the engine's own default, and is a perfectly good answer.  ``known`` is the
    languages the engine actually speaks; a language outside it is worth one
    line in the log rather than a silent substitution.
    """
    table = voices if isinstance(voices, dict) else {}
    tag = (language or "").strip().lower().partition("-")[0]
    if tag and table.get(tag):
        return VoiceChoice(voice=str(table[tag]), source="language")
    note = ""
    if tag and known and tag not in {item.lower() for item in known}:
        note = f"no voice for {tag}; using the default"
    if table.get(ANY):
        return VoiceChoice(voice=str(table[ANY]), source="any", note=note)
    return VoiceChoice(voice=None, source="engine", note=note)


def normalise(raw: Any, legacy: str | None = None) -> dict[str, str]:
    """``audio.tts.voices`` as a clean mapping, folding in the legacy field.

    A single ``voice: "M1"`` from before this existed means "that voice,
    whatever the language", which is exactly what :data:`ANY` means.
    """
    table: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            tag = str(key).strip().lower()
            name = str(value or "").strip()
            if tag and name:
                table[tag] = name
    if legacy and ANY not in table:
        table[ANY] = str(legacy).strip()
    return table


__all__ = [
    "ANY",
    "DEFAULT_LANGUAGES",
    "LIST_TIMEOUT",
    "OPENAI_VOICES",
    "PIPER_VOICES",
    "PIPER_VOICE_BYTES",
    "SUPERTONIC_LANGUAGES",
    "SUPERTONIC_STYLES",
    "Voice",
    "VoiceChoice",
    "by_language",
    "edge_voices",
    "espeak_voices",
    "normalise",
    "openai_voices",
    "pick",
    "piper_voice_path",
    "piper_voices",
    "sapi_voices",
    "say_voices",
    "supertonic_voices",
    "voices_for",
]
