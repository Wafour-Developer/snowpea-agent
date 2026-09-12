"""Speech to text, from whichever backend the machine has.

Three implementations, tried in this order when the provider is ``"auto"``:

``openai``
    ``POST /audio/transcriptions`` against the OpenAI API (or any compatible
    server), using the key already configured for the ``openai`` provider.
``local-whisper``
    The ``whisper`` or ``faster-whisper`` CLI, if one is on ``PATH``.  Nothing
    leaves the machine.
``command``
    A user-configured command template containing ``{path}``; whatever it
    prints on stdout is the transcript.

Every backend answers the same :class:`STTProvider` protocol, and every
failure becomes an :class:`~snowpea_core.audio.player.AudioError`.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from snowpea_core.audio.player import AudioError

log = logging.getLogger("snowpea.audio.stt")

#: OpenAI's default transcription model; ``gpt-4o-transcribe`` also works.
DEFAULT_OPENAI_MODEL = "whisper-1"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT = 120.0

#: Whisper CLIs, in preference order.
WHISPER_EXECUTABLES: tuple[str, ...] = ("faster-whisper", "whisper")

#: Local whisper model size; small enough to run on a laptop CPU.
DEFAULT_WHISPER_MODEL = "base"

#: MIME type -> the extension the OpenAI endpoint wants to see on the upload.
_UPLOAD_EXTENSIONS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


@dataclass(frozen=True)
class Transcript:
    """What a backend heard."""

    text: str
    provider: str


@runtime_checkable
class STTProvider(Protocol):
    """A backend that turns an audio file into text."""

    name: str

    def available(self) -> bool:
        """True when this backend can actually run here."""
        ...

    async def transcribe(self, path: Path, mime: str | None = None) -> Transcript: ...


class OpenAISTT:
    """Transcription via ``POST /audio/transcriptions``."""

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
        #: Injected by the tests; ``None`` means a real :class:`httpx.AsyncClient`.
        self._client_factory = client_factory

    def available(self) -> bool:
        return bool(self.api_key)

    def _client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            client: httpx.AsyncClient = self._client_factory()
            return client
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def transcribe(self, path: Path, mime: str | None = None) -> Transcript:
        if not self.api_key:
            raise AudioError("no_stt", "openai transcription needs an API key")
        audio = Path(path)
        if not audio.is_file():
            raise AudioError("transcribe_failed", f"no such audio file: {audio}")
        extension = _UPLOAD_EXTENSIONS.get(mime or "", audio.suffix.lstrip(".") or "wav")
        files = {
            "file": (f"{audio.stem}.{extension}", audio.read_bytes(), mime or "audio/wav"),
        }
        headers = {"authorization": f"Bearer {self.api_key}"}
        try:
            async with self._client() as client:
                response = await client.post(
                    "/audio/transcriptions",
                    data={"model": self.model},
                    files=files,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise AudioError("transcribe_failed", f"openai: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise AudioError("transcribe_failed", f"openai: HTTP {response.status_code}: {detail}")
        return Transcript(text=_text_from_response(response), provider=self.name)


def _text_from_response(response: httpx.Response) -> str:
    """The transcript out of a JSON body, or the raw text for ``response_format=text``."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(payload, dict):
        return str(payload.get("text") or "").strip()
    return str(payload).strip()


class LocalWhisperSTT:
    """Transcription through a local ``whisper`` / ``faster-whisper`` CLI."""

    name = "local-whisper"

    def __init__(
        self,
        executable: str | None = None,
        *,
        model: str = DEFAULT_WHISPER_MODEL,
        language: str | None = None,
        timeout: float = 600.0,
    ) -> None:
        self.executable = executable or self._discover()
        self.model = model
        self.language = language
        self.timeout = timeout

    @staticmethod
    def _discover() -> str | None:
        for candidate in WHISPER_EXECUTABLES:
            if shutil.which(candidate):
                return candidate
        return None

    def available(self) -> bool:
        return bool(self.executable and shutil.which(self.executable))

    async def transcribe(self, path: Path, mime: str | None = None) -> Transcript:
        if not self.available():
            raise AudioError("no_stt", "no local whisper CLI found on PATH")
        audio = Path(path)
        if not audio.is_file():
            raise AudioError("transcribe_failed", f"no such audio file: {audio}")
        with tempfile.TemporaryDirectory(prefix="snowpea-stt-") as tmp:
            argv = [
                str(self.executable),
                str(audio),
                "--model",
                self.model,
                "--output_format",
                "txt",
                "--output_dir",
                tmp,
            ]
            if self.language:
                argv += ["--language", self.language]
            stdout, stderr, code = await _run(argv, self.timeout)
            if code:
                detail = stderr.strip()[:300] or stdout.strip()[:300]
                raise AudioError("transcribe_failed", f"{self.executable} exited {code}: {detail}")
            written = sorted(Path(tmp).glob("*.txt"))
            if written:
                return Transcript(
                    text=written[0].read_text(encoding="utf-8", errors="replace").strip(),
                    provider=self.name,
                )
        # Some builds only print to stdout; that is a transcript too.
        return Transcript(text=stdout.strip(), provider=self.name)


class CommandSTT:
    """Transcription through a user-configured command.

    The template must contain ``{path}``, which is replaced with the audio
    file; stdout is taken as the transcript.
    """

    name = "command"

    def __init__(self, template: str | None, *, timeout: float = 600.0) -> None:
        self.template = (template or "").strip()
        self.timeout = timeout

    def available(self) -> bool:
        if not self.template:
            return False
        try:
            argv = shlex.split(self.template)
        except ValueError:
            return False
        return bool(argv) and shutil.which(argv[0]) is not None

    def argv(self, path: Path) -> list[str]:
        """The template split into an argv, with ``{path}`` substituted."""
        try:
            parts = shlex.split(self.template)
        except ValueError as exc:
            raise AudioError("no_stt", f"stt command is not parsable: {exc}") from exc
        if not parts:
            raise AudioError("no_stt", "stt command is empty")
        target = str(path)
        argv = [part.replace("{path}", target) for part in parts]
        if "{path}" not in self.template:
            argv.append(target)
        return argv

    async def transcribe(self, path: Path, mime: str | None = None) -> Transcript:
        if not self.template:
            raise AudioError("no_stt", "no stt command configured")
        audio = Path(path)
        if not audio.is_file():
            raise AudioError("transcribe_failed", f"no such audio file: {audio}")
        stdout, stderr, code = await _run(self.argv(audio), self.timeout)
        if code:
            detail = stderr.strip()[:300] or stdout.strip()[:300]
            raise AudioError("transcribe_failed", f"stt command exited {code}: {detail}")
        return Transcript(text=stdout.strip(), provider=self.name)


async def _run(argv: list[str], timeout: float) -> tuple[str, str, int]:
    """Run a command, returning ``(stdout, stderr, returncode)``."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AudioError("transcribe_failed", f"{argv[0]}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        raise AudioError("transcribe_failed", f"{argv[0]} timed out") from None
    return (
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        int(process.returncode or 0),
    )


#: The order ``"auto"`` tries backends in.
AUTO_ORDER: tuple[str, ...] = ("openai", "local-whisper", "command")


def build_provider(
    name: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
) -> STTProvider:
    """Construct one named backend, configured but not yet checked."""
    if name == "openai":
        return OpenAISTT(api_key, model=model, base_url=base_url)
    if name == "local-whisper":
        return LocalWhisperSTT(model=model or DEFAULT_WHISPER_MODEL)
    if name == "command":
        return CommandSTT(command)
    raise AudioError("no_stt", f"unknown stt provider {name!r}")


def resolve_provider(
    name: str = "auto",
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
) -> STTProvider | None:
    """The backend to transcribe with, or ``None`` when none is usable.

    A named provider is returned only when it is actually available, so the
    caller can report *why* speech input is off rather than failing later.
    """
    names = AUTO_ORDER if name in {"auto", ""} else (name,)
    for candidate in names:
        try:
            provider = build_provider(
                candidate, api_key=api_key, model=model, base_url=base_url, command=command
            )
        except AudioError:
            continue
        if provider.available():
            return provider
    return None


__all__ = [
    "AUTO_ORDER",
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_WHISPER_MODEL",
    "WHISPER_EXECUTABLES",
    "CommandSTT",
    "LocalWhisperSTT",
    "OpenAISTT",
    "STTProvider",
    "Transcript",
    "build_provider",
    "resolve_provider",
]
