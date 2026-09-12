"""Text to speech, from whichever backend the machine has.

Speech used to mean one thing: forward ``text_to_speech`` to the snowpea-studio
MCP server.  That leaves the feature dead on every machine without studio
configured, so this module is a chain instead, tried in this order when the
provider is ``"auto"``:

``studio``
    The existing ``text_to_speech`` media tool (snowpea-studio's
    ``generate_speech``).  First when it is configured, because it is the one
    backend with real voices behind it.
``openai``
    ``POST /audio/speech`` (``tts-1``, ``gpt-4o-mini-tts``), using the key
    already configured for the ``openai`` provider.
``edge-tts`` / ``piper`` / ``say`` / ``espeak-ng`` / ``powershell``
    Local CLIs, in that order — the first one installed wins.  Nothing leaves
    the machine.
``command``
    A user-configured template with ``{text}`` and ``{out}`` placeholders.

Every backend answers the same :class:`TTSProvider` protocol and returns a
:class:`Speech` — a file on disk, which the caller either plays through
:mod:`snowpea_core.audio.player` or hands to the client.  The same
:func:`resolve_provider` serves the ``audio.speak`` RPC and the
``text_to_speech`` tool.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import shlex
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from snowpea_core.audio.player import AudioError

log = logging.getLogger("snowpea.audio.tts")

#: The MCP tool ``tools/media.py`` exposes for speech.
TOOL_NAME = "text_to_speech"

DEFAULT_TIMEOUT = 120.0

#: OpenAI's cheap speech model; ``gpt-4o-mini-tts`` is the quality option.
DEFAULT_OPENAI_MODEL = "tts-1"
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

#: Audio extensions, used to guess the type of a url, a path or an output file.
_AUDIO_MIMES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aiff": "audio/aiff",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)

#: Keys a studio response might carry the audio under.
_URL_KEYS = ("url", "audio_url", "asset_url", "download_url", "uri", "href")
_PATH_KEYS = ("path", "file", "filepath", "file_path", "local_path")
_DATA_KEYS = ("audio", "audio_base64", "b64", "base64", "data", "content")

#: ``(tool_name, arguments) -> whatever the MCP tool returned``.
SpeechCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Speech:
    """A synthesised utterance sitting in a file."""

    path: Path
    mime: str
    voice: str | None = None
    provider: str = ""

    def to_payload(self) -> dict[str, Any]:
        """The JSON-safe shape ``audio.speak`` answers with."""
        payload: dict[str, Any] = {"path": str(self.path), "mime": self.mime}
        if self.voice:
            payload["voice"] = self.voice
        if self.provider:
            payload["provider"] = self.provider
        return payload


@runtime_checkable
class TTSProvider(Protocol):
    """A backend that turns text into an audio file."""

    name: str

    def available(self) -> bool:
        """True when this backend can actually run here."""
        ...

    async def synthesize(
        self,
        text: str,
        *,
        out_dir: Path,
        voice: str | None = None,
        language: str | None = None,
        stem: str | None = None,
    ) -> Speech: ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def mime_for(name: str) -> str:
    """The audio MIME type implied by a filename or url."""
    lowered = name.lower().split("?", 1)[0]
    for suffix, mime in _AUDIO_MIMES.items():
        if lowered.endswith(suffix):
            return mime
    return "audio/mpeg"


def extension_for(mime: str) -> str:
    """The file extension for an audio MIME type."""
    for suffix, known in _AUDIO_MIMES.items():
        if known == mime:
            return suffix
    return ".mp3"


def _stem_for(text: str, stem: str | None) -> str:
    """A stable, filesystem-safe basename for one utterance."""
    if stem:
        return stem
    import hashlib

    return "speech-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _prepare(text: str, out_dir: Path) -> str:
    """Validate the text and make sure the output directory exists."""
    body = (text or "").strip()
    if not body:
        raise AudioError("synthesis_failed", "nothing to speak")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AudioError("synthesis_failed", f"cannot write to {out_dir}: {exc}") from exc
    return body


async def _run(argv: list[str], timeout: float, stdin: bytes | None = None) -> tuple[str, int]:
    """Run a synthesis command; returns ``(stderr, returncode)``."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AudioError("synthesis_failed", f"{argv[0]}: {exc}") from exc
    try:
        _, err = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
    except TimeoutError:
        process.kill()
        raise AudioError("synthesis_failed", f"{argv[0]} timed out") from None
    return err.decode("utf-8", "replace"), int(process.returncode or 0)


def _check_output(path: Path, provider: str, stderr: str, code: int) -> None:
    """Turn a failed command or an empty file into an :class:`AudioError`."""
    if code:
        raise AudioError("synthesis_failed", f"{provider} exited {code}: {stderr.strip()[:300]}")
    if not path.is_file() or path.stat().st_size == 0:
        detail = stderr.strip()[:300]
        suffix = f": {detail}" if detail else ""
        raise AudioError("synthesis_failed", f"{provider} produced no audio{suffix}")


# ---------------------------------------------------------------------------
# studio (the existing MCP forward)
# ---------------------------------------------------------------------------


def _walk(value: Any, keys: tuple[str, ...]) -> str | None:
    """The first string found under any of ``keys``, depth-first."""
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        for nested in value.values():
            hit = _walk(nested, keys)
            if hit:
                return hit
    elif isinstance(value, list):
        for item in value:
            hit = _walk(item, keys)
            if hit:
                return hit
    return None


def parse_result(result: Any) -> tuple[str, str]:
    """Find the audio in an MCP answer: ``(kind, value)``.

    ``kind`` is ``"url"``, ``"path"`` or ``"base64"``.  The studio server
    renders tool output as text, so a JSON body, a bare url and a plain path
    all have to be understood.
    """
    payload: Any = result
    if isinstance(payload, str):
        stripped = payload.strip()
        try:
            payload = json.loads(stripped)
        except ValueError:
            match = _URL_RE.search(stripped)
            if match:
                return "url", match.group(0)
            if stripped and "\n" not in stripped and Path(stripped).exists():
                return "path", stripped
            raise AudioError(
                "synthesis_failed", f"no audio in the speech result: {stripped[:200]}"
            ) from None
    url = _walk(payload, _URL_KEYS)
    if url and url.lower().startswith(("http://", "https://")):
        return "url", url
    path = _walk(payload, _PATH_KEYS)
    if path:
        return "path", path
    data = _walk(payload, _DATA_KEYS)
    if data and len(data) > 64:
        return "base64", data
    rendered = json.dumps(payload, ensure_ascii=False)[:200]
    raise AudioError("synthesis_failed", f"no audio in the speech result: {rendered}")


async def materialise(
    kind: str,
    value: str,
    out_dir: Path,
    *,
    stem: str,
    timeout: float = DEFAULT_TIMEOUT,
    client_factory: Any = None,
) -> Speech:
    """Turn a url / path / base64 blob into a file under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if kind == "url":
        mime = mime_for(value)
        target = out_dir / f"{stem}{extension_for(mime)}"
        client = (
            client_factory()
            if client_factory is not None
            else httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        )
        try:
            async with client as session:
                response = await session.get(value)
                if response.status_code >= 400:
                    raise AudioError(
                        "synthesis_failed",
                        f"speech download failed: HTTP {response.status_code}",
                    )
                target.write_bytes(response.content)
        except httpx.HTTPError as exc:
            raise AudioError("synthesis_failed", f"speech download failed: {exc}") from exc
        return Speech(path=target, mime=mime)
    if kind == "path":
        source = Path(value).expanduser()
        if not source.is_file():
            raise AudioError("synthesis_failed", f"speech file is missing: {source}")
        mime = mime_for(source.name)
        target = out_dir / f"{stem}{source.suffix or extension_for(mime)}"
        if source != target:
            shutil.copyfile(source, target)
        return Speech(path=target, mime=mime)
    payload = value
    mime = "audio/mpeg"
    if payload.startswith("data:"):
        header, _, payload = payload.partition(",")
        mime = header[5:].split(";", 1)[0] or "audio/mpeg"
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AudioError("synthesis_failed", f"speech payload is not base64: {exc}") from exc
    target = out_dir / f"{stem}{extension_for(mime)}"
    target.write_bytes(raw)
    return Speech(path=target, mime=mime)


class StudioTTS:
    """Speech through the snowpea-studio MCP server's ``generate_speech``."""

    name = "studio"

    def __init__(
        self,
        caller: SpeechCaller | None,
        *,
        configured: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        client_factory: Any = None,
    ) -> None:
        self.caller = caller
        self.configured = configured
        self.timeout = timeout
        self._client_factory = client_factory

    def available(self) -> bool:
        """Studio is *usable* when it is configured; the caller is plumbing.

        Reporting availability without a caller is what lets
        ``audio.capabilities`` answer from settings alone.
        """
        return self.configured

    async def synthesize(
        self,
        text: str,
        *,
        out_dir: Path,
        voice: str | None = None,
        language: str | None = None,
        stem: str | None = None,
    ) -> Speech:
        if self.caller is None:
            raise AudioError("no_tts", "the snowpea-studio MCP server is not configured")
        body = _prepare(text, out_dir)
        args: dict[str, Any] = {"text": body}
        if voice:
            args["voice"] = voice
        if language:
            args["language"] = language
        try:
            result = await self.caller(TOOL_NAME, args)
        except AudioError:
            raise
        except Exception as exc:  # noqa: BLE001 - the studio server can raise anything
            raise AudioError("synthesis_failed", f"{type(exc).__name__}: {exc}") from exc
        kind, value = parse_result(result)
        speech = await materialise(
            kind,
            value,
            out_dir,
            stem=_stem_for(body, stem),
            timeout=self.timeout,
            client_factory=self._client_factory,
        )
        return Speech(path=speech.path, mime=speech.mime, voice=voice, provider=self.name)


# ---------------------------------------------------------------------------
# openai
# ---------------------------------------------------------------------------


class OpenAITTS:
    """Speech through ``POST /audio/speech``."""

    name = "openai"

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client_factory: Any = None,
    ) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_OPENAI_MODEL
        self.base_url = (base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._client_factory = client_factory

    def available(self) -> bool:
        return bool(self.api_key)

    def _client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            client: httpx.AsyncClient = self._client_factory()
            return client
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def synthesize(
        self,
        text: str,
        *,
        out_dir: Path,
        voice: str | None = None,
        language: str | None = None,
        stem: str | None = None,
    ) -> Speech:
        if not self.api_key:
            raise AudioError("no_tts", "openai speech needs an API key")
        body = _prepare(text, out_dir)
        payload = {
            "model": self.model,
            "input": body,
            "voice": voice or DEFAULT_OPENAI_VOICE,
            "response_format": "mp3",
        }
        try:
            async with self._client() as client:
                response = await client.post(
                    "/audio/speech",
                    json=payload,
                    headers={"authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise AudioError("synthesis_failed", f"openai: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise AudioError(
                "synthesis_failed", f"openai: HTTP {response.status_code}: {response.text[:300]}"
            )
        target = out_dir / f"{_stem_for(body, stem)}.mp3"
        target.write_bytes(response.content)
        if target.stat().st_size == 0:
            raise AudioError("synthesis_failed", "openai returned an empty audio body")
        return Speech(
            path=target, mime="audio/mpeg", voice=voice or DEFAULT_OPENAI_VOICE, provider=self.name
        )


# ---------------------------------------------------------------------------
# local CLIs
# ---------------------------------------------------------------------------


class _CliTTS:
    """Shared plumbing for a CLI that writes its speech to a file."""

    name = "cli"
    executable = ""
    #: Extension the tool writes; also decides the reported MIME type.
    suffix = ".wav"

    def __init__(self, executable: str | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.executable = executable or type(self).executable
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.executable) and shutil.which(self.executable) is not None

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        raise NotImplementedError

    def stdin_for(self, text: str) -> bytes | None:
        """Text handed on stdin instead of on the command line, if any."""
        return None

    async def synthesize(
        self,
        text: str,
        *,
        out_dir: Path,
        voice: str | None = None,
        language: str | None = None,
        stem: str | None = None,
    ) -> Speech:
        if not self.available():
            raise AudioError("no_tts", f"{self.name} is not installed")
        body = _prepare(text, out_dir)
        target = out_dir / f"{_stem_for(body, stem)}{self.suffix}"
        stderr, code = await _run(
            self.argv(body, target, voice), self.timeout, self.stdin_for(body)
        )
        _check_output(target, self.name, stderr, code)
        return Speech(path=target, mime=mime_for(self.suffix), voice=voice, provider=self.name)


class EdgeTTS(_CliTTS):
    """Microsoft Edge's neural voices through the ``edge-tts`` CLI."""

    name = "edge-tts"
    executable = "edge-tts"
    suffix = ".mp3"

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        argv = [self.executable, "--text", text, "--write-media", str(out)]
        if voice:
            argv += ["--voice", voice]
        return argv


class PiperTTS(_CliTTS):
    """Piper: local neural voices, text on stdin."""

    name = "piper"
    executable = "piper"
    suffix = ".wav"

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        argv = [self.executable, "--output_file", str(out)]
        if voice:
            # Piper calls the voice a model; it is a path or a downloaded name.
            argv += ["--model", voice]
        return argv

    def stdin_for(self, text: str) -> bytes | None:
        return text.encode("utf-8")


class SayTTS(_CliTTS):
    """macOS ``say``, writing a wav rather than speaking directly."""

    name = "say"
    executable = "say"
    suffix = ".wav"

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        argv = [self.executable, "-o", str(out), "--data-format=LEI16@22050"]
        if voice:
            argv += ["-v", voice]
        return [*argv, text]


class EspeakTTS(_CliTTS):
    """``espeak-ng``: robotic, tiny, and on most Linux boxes already."""

    name = "espeak-ng"
    executable = "espeak-ng"
    suffix = ".wav"

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        argv = [self.executable, "-w", str(out)]
        if voice:
            argv += ["-v", voice]
        return [*argv, text]


class PowershellTTS(_CliTTS):
    """Windows SAPI through PowerShell, for machines with no CLI at all."""

    name = "powershell"
    executable = "powershell"
    suffix = ".wav"

    def available(self) -> bool:
        return sys.platform == "win32" and shutil.which(self.executable) is not None

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        escaped = text.replace("'", "''")
        select = f"$s.SelectVoice('{voice}'); " if voice else ""
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"{select}$s.SetOutputToWaveFile('{out}'); $s.Speak('{escaped}'); $s.Dispose()"
        )
        return [self.executable, "-NoProfile", "-NonInteractive", "-Command", script]


class CommandTTS(_CliTTS):
    """A user-configured template with ``{text}`` and ``{out}`` placeholders."""

    name = "command"

    def __init__(
        self, template: str | None, *, suffix: str = ".wav", timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        super().__init__(executable=None, timeout=timeout)
        self.template = (template or "").strip()
        self.suffix = suffix

    def available(self) -> bool:
        if not self.template:
            return False
        try:
            parts = shlex.split(self.template)
        except ValueError:
            return False
        return bool(parts) and shutil.which(parts[0]) is not None

    def argv(self, text: str, out: Path, voice: str | None) -> list[str]:
        try:
            parts = shlex.split(self.template)
        except ValueError as exc:
            raise AudioError("no_tts", f"tts command is not parsable: {exc}") from exc
        if not parts:
            raise AudioError("no_tts", "tts command is empty")
        argv = [
            part.replace("{text}", text).replace("{out}", str(out)).replace("{voice}", voice or "")
            for part in parts
        ]
        if "{out}" not in self.template:
            argv.append(str(out))
        return argv

    def stdin_for(self, text: str) -> bytes | None:
        return None if "{text}" in self.template else text.encode("utf-8")


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

#: The order ``"auto"`` tries backends in.
AUTO_ORDER: tuple[str, ...] = (
    "studio",
    "openai",
    "edge-tts",
    "piper",
    "say",
    "espeak-ng",
    "powershell",
    "command",
)

#: Every backend name, for settings validation and the setup wizard.
PROVIDER_NAMES: tuple[str, ...] = AUTO_ORDER

#: The local CLIs, in the order the wizard should report them.
LOCAL_PROVIDERS: tuple[str, ...] = ("edge-tts", "piper", "say", "espeak-ng", "powershell")


def build_provider(
    name: str,
    *,
    caller: SpeechCaller | None = None,
    studio_configured: bool = False,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
    client_factory: Any = None,
) -> TTSProvider:
    """Construct one named backend, configured but not yet checked."""
    if name == "studio":
        return StudioTTS(caller, configured=studio_configured, client_factory=client_factory)
    if name == "openai":
        return OpenAITTS(api_key, model=model, base_url=base_url, client_factory=client_factory)
    if name == "edge-tts":
        return EdgeTTS()
    if name == "piper":
        return PiperTTS()
    if name == "say":
        return SayTTS()
    if name == "espeak-ng":
        return EspeakTTS()
    if name == "powershell":
        return PowershellTTS()
    if name == "command":
        return CommandTTS(command)
    raise AudioError("no_tts", f"unknown tts provider {name!r}")


def resolve_provider(
    name: str = "auto",
    *,
    caller: SpeechCaller | None = None,
    studio_configured: bool = False,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
    client_factory: Any = None,
) -> TTSProvider | None:
    """The backend to speak with, or ``None`` when none is usable."""
    names = AUTO_ORDER if name in {"auto", ""} else (name,)
    for candidate in names:
        try:
            provider = build_provider(
                candidate,
                caller=caller,
                studio_configured=studio_configured,
                api_key=api_key,
                model=model,
                base_url=base_url,
                command=command,
                client_factory=client_factory,
            )
        except AudioError:
            continue
        if provider.available():
            return provider
    return None


def available_providers(
    *,
    caller: SpeechCaller | None = None,
    studio_configured: bool = False,
    api_key: str | None = None,
    command: str | None = None,
) -> list[str]:
    """Every backend that would work here, in preference order."""
    found: list[str] = []
    for candidate in AUTO_ORDER:
        provider = build_provider(
            candidate,
            caller=caller,
            studio_configured=studio_configured,
            api_key=api_key,
            command=command,
        )
        if provider.available():
            found.append(provider.name)
    return found


async def synthesize(
    text: str,
    *,
    out_dir: Path | str,
    provider: TTSProvider | None = None,
    caller: SpeechCaller | None = None,
    studio_configured: bool | None = None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
    voice: str | None = None,
    language: str | None = None,
    stem: str | None = None,
    client_factory: Any = None,
) -> Speech:
    """Speak ``text`` and return the audio file it landed in.

    Either hand in a resolved ``provider``, or the credentials for
    :func:`resolve_provider` to pick one.
    """
    chosen = provider or resolve_provider(
        "auto",
        caller=caller,
        studio_configured=caller is not None if studio_configured is None else studio_configured,
        api_key=api_key,
        model=model,
        base_url=base_url,
        command=command,
        client_factory=client_factory,
    )
    if chosen is None:
        raise AudioError(
            "no_tts",
            "no speech backend: configure the snowpea-studio MCP server, set an OpenAI API key, "
            "or install edge-tts, piper, say or espeak-ng",
        )
    return await chosen.synthesize(
        text, out_dir=Path(out_dir).expanduser(), voice=voice, language=language, stem=stem
    )


__all__ = [
    "AUTO_ORDER",
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_OPENAI_VOICE",
    "DEFAULT_TIMEOUT",
    "LOCAL_PROVIDERS",
    "PROVIDER_NAMES",
    "TOOL_NAME",
    "CommandTTS",
    "EdgeTTS",
    "EspeakTTS",
    "OpenAITTS",
    "PiperTTS",
    "PowershellTTS",
    "SayTTS",
    "Speech",
    "SpeechCaller",
    "StudioTTS",
    "TTSProvider",
    "available_providers",
    "build_provider",
    "extension_for",
    "materialise",
    "mime_for",
    "parse_result",
    "resolve_provider",
    "synthesize",
]
