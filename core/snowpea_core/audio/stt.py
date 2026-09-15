"""Speech to text, from whichever backend the machine has.

Three implementations, tried in this order when the provider is ``"auto"``:

``local-whisper``
    The ``whisper`` or ``faster-whisper`` CLI, if one is on ``PATH``.  It goes
    first because nothing leaves the machine.
``openai``
    ``POST /audio/transcriptions`` against the OpenAI API (or any compatible
    server), using the key already configured for the ``openai`` provider.
``command``
    A user-configured command template containing ``{path}``; whatever it
    prints on stdout is the transcript.

Every backend answers the same :class:`STTProvider` protocol, and every
failure becomes an :class:`~snowpea_core.audio.player.AudioError`.  There is
deliberately nothing here about *who* is asking: the same
:func:`resolve_provider` serves the ``audio.transcribe`` RPC the TUI calls and
the ``transcribe_audio`` tool the agent calls.
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
        language: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.language = _real_language(language)
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
                    data=(
                        {"model": self.model, "language": self.language}
                        if self.language
                        else {"model": self.model}
                    ),
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
        self.language = _real_language(language)
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




# ---------------------------------------------------------------------------
# sherpa-onnx (local neural ASR, CPU)
# ---------------------------------------------------------------------------

#: Executables ``sherpa-onnx`` installs.  The offline one decodes a whole file
#: (SenseVoice), the online one decodes a stream (the zipformers); both accept
#: a wav path, which is all this backend needs.
SHERPA_OFFLINE_BIN = "sherpa-onnx-offline"
SHERPA_ONLINE_BIN = "sherpa-onnx"

#: How long a local decode may take.  Generous: the point of these models is
#: that they run on a CPU, and a CPU decoding a long recording is not stuck.
SHERPA_TIMEOUT = 600.0


class SherpaOnnxSTT:
    """Transcription through the ``sherpa-onnx`` CLIs and a downloaded model.

    One class covers the whole family because the difference between the models
    is which binary decodes them and which files they are handed, and both of
    those are rows in :mod:`snowpea_core.audio.stt_models`.  SenseVoice decodes
    offline and detects its own language; the zipformers are streaming models
    for one language each.

    The engine is *available* when the CLI is on PATH and the model directory
    is stamped complete, so a half-finished download never makes voice input
    look ready.
    """

    name = "sherpa-onnx"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        home: Path | str | None = None,
        language: str | None = None,
        timeout: float = SHERPA_TIMEOUT,
    ) -> None:
        from snowpea_core.audio import stt_models

        self.home = Path(home).expanduser() if home else None
        self.language = _real_language(language)
        self.timeout = timeout
        resolved = model_id
        if resolved in (None, "", "auto") and self.home is not None:
            resolved = stt_models.preferred(self.home, self.language)
        # A Zipformer speaks one language, so asking for a language it was not
        # trained on is asking for a different model.  Saying so beats decoding
        # Korean with an English model and reporting nonsense.
        self.wrong_language = _wrong_language(resolved, self.language)
        self.model_id = resolved
        self.model = stt_models.model_for(resolved) if resolved else None
        if self.model is not None:
            # The engine reports itself by the model it will actually use, so a
            # capabilities report names what is running rather than a family.
            self.name = self.model.id

    @property
    def executable(self) -> str:
        if self.model is not None and self.model.kind == "online":
            return SHERPA_ONLINE_BIN
        return SHERPA_OFFLINE_BIN

    def available(self) -> bool:
        if self.model is None or self.home is None or self.wrong_language:
            return False
        return bool(shutil.which(self.executable)) and self.model.installed(self.home)

    def missing_reason(self) -> str:
        """Why this engine cannot run, in words that name the fix."""
        if self.wrong_language and self.model is not None:
            needed = _model_for_language(self.language)
            return (
                f"{self.model.id} does not speak {self.language}; "
                + (f"install {needed}" if needed else "pick a language it speaks")
            )
        if self.model is None:
            return "no sherpa-onnx model is installed"
        if self.home is not None and not self.model.installed(self.home):
            return f"{self.model.id} is not downloaded; `snowpea audio install {self.model.id}`"
        return f"{self.executable} is not on PATH"

    def vad(self) -> Path | None:
        """The silero VAD that came with this model, when it has one."""
        from snowpea_core.audio import stt_models

        if self.model is None or self.home is None:
            return None
        candidate = self.model.directory(self.home) / stt_models.SILERO_VAD
        return candidate if candidate.is_file() else None

    def argv(self, audio: Path) -> list[str]:
        """The command line that decodes ``audio``.

        Flags are the ones the sherpa-onnx CLIs document for each model family;
        the model files come from the table rather than from guesswork, so a
        model whose archive layout changes fails detection instead of running
        with a wrong path.
        """
        if self.model is None or self.home is None:  # pragma: no cover - guarded by available()
            raise AudioError("no_stt", "sherpa-onnx has no model configured")
        root = self.model.root(self.home)
        argv = [self.executable, f"--tokens={root / 'tokens.txt'}"]
        if self.model.kind == "offline":
            argv.append(f"--sense-voice-model={root / 'model.int8.onnx'}")
            language = self.language or "auto"
            argv.append(f"--sense-voice-language={language}")
            vad = self.vad()
            if vad is not None:
                # With a VAD the decoder splits a long recording into
                # utterances instead of trying to swallow it whole.
                argv.append(f"--silero-vad-model={vad}")
        else:
            encoder, decoder, joiner = self._transducer(root)
            argv += [
                f"--encoder={encoder}",
                f"--decoder={decoder}",
                f"--joiner={joiner}",
            ]
        argv.append(str(audio))
        return argv

    def _transducer(self, root: Path) -> tuple[Path, Path, Path]:
        """The encoder / decoder / joiner this streaming model unpacked to."""
        needs = list(self.model.needs) if self.model is not None else []
        pick = {part: next((n for n in needs if n.startswith(part)), "") for part in
                ("encoder", "decoder", "joiner")}
        missing = [part for part, name in pick.items() if not name]
        if missing:  # pragma: no cover - the table always names all three
            raise AudioError("no_stt", f"model table has no {', '.join(missing)}")
        return (root / pick["encoder"], root / pick["decoder"], root / pick["joiner"])

    async def transcribe(self, path: Path, mime: str | None = None) -> Transcript:
        if not self.available():
            raise AudioError("no_stt", f"{self.name} is not installed")
        audio = Path(path)
        if not audio.is_file():
            raise AudioError("transcribe_failed", f"no such audio file: {audio}")
        out, err, code = await _run(self.argv(audio), self.timeout)
        if code:
            detail = (err or out).strip()[:300]
            raise AudioError("transcribe_failed", f"{self.name} exited {code}: {detail}")
        text = parse_sherpa_output(out)
        if not text:
            raise AudioError("transcribe_failed", f"{self.name} produced no transcript")
        return Transcript(text=text, provider=self.name)


def _real_language(language: str | None) -> str | None:
    """A tag to force, or ``None`` for "let the engine detect".

    ``"auto"`` is a *setting* value, not something any engine takes on its
    command line: whisper would look for a language called "auto".
    """
    tag = (language or "").strip()
    return None if not tag or tag.lower() == "auto" else tag


#: Which single-language model serves which language.  A multilingual model is
#: not here: it serves every language, so there is nothing to map.
MODEL_BY_LANGUAGE: dict[str, str] = {
    "ko": "sherpa-onnx-zipformer-ko",
    "en": "sherpa-onnx-zipformer-en",
}


def _model_for_language(language: str | None) -> str | None:
    tag = (language or "").strip().lower().partition("-")[0]
    return MODEL_BY_LANGUAGE.get(tag)


def _wrong_language(model_id: str | None, language: str | None) -> bool:
    """True when a single-language model was asked for a language it lacks."""
    from snowpea_core.audio import stt_models

    tag = (language or "").strip().lower().partition("-")[0]
    if not tag or tag == "auto" or not model_id:
        return False
    model = stt_models.model_for(model_id)
    if model is None or not model.languages:
        return False
    return tag not in {item.lower() for item in model.languages}


def parse_sherpa_output(out: str) -> str:
    """The transcript out of a sherpa-onnx CLI's report.

    The CLIs print a block per file — the path, then timing lines, then the
    text — and the text is what follows the last ``text:`` label.  Falling back
    to the last non-empty line keeps this working if the report is reworded,
    because a transcript that is one line off is better than none.
    """
    lines = [line.rstrip() for line in (out or "").splitlines()]
    for line in reversed(lines):
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("text:"):
            return stripped.split(":", 1)[1].strip()
        if lowered.startswith("{") and '"text"' in lowered:
            import json

            try:
                return str(json.loads(stripped).get("text", "")).strip()
            except ValueError:
                continue
    for line in reversed(lines):
        if line.strip() and ":" not in line:
            return line.strip()
    return ""


#: The order the wizard **recommends** engines in.  It is no longer a chain:
#: nothing resolves through it, because a direction of voice is either pinned
#: to one engine or off (``audio/__init__.pinned``).  What it still decides is
#: which engine the setup screens suggest installing first, and which one
#: detection lists first.
#:
#: SenseVoice leads because it is the recommended default (CPU, five languages,
#: its own VAD) and because it needs no language guess to be right.  The
#: zipformers follow, then the whisper CLI, then the hosted API — local before
#: hosted, because transcription is the one place audio of the user's room
#: would otherwise leave the machine.
RECOMMENDED_ORDER: tuple[str, ...] = (
    "sherpa-onnx-sensevoice",
    "sherpa-onnx-zipformer-ko",
    "sherpa-onnx-zipformer-en",
    "local-whisper",
    "openai",
    "command",
)

#: The sherpa-onnx rows, so callers can tell the family from the chain.
SHERPA_PROVIDERS: tuple[str, ...] = (
    "sherpa-onnx-sensevoice",
    "sherpa-onnx-zipformer-ko",
    "sherpa-onnx-zipformer-en",
)


def build_provider(
    name: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
    home: Path | str | None = None,
    language: str | None = None,
) -> STTProvider:
    """Construct one named backend, configured but not yet checked."""
    if name == "openai":
        return OpenAISTT(api_key, model=model, base_url=base_url, language=language)
    if name == "local-whisper":
        return LocalWhisperSTT(model=model or DEFAULT_WHISPER_MODEL, language=language)
    if name == "command":
        return CommandSTT(command)
    if name in SHERPA_PROVIDERS or name == "sherpa-onnx":
        return SherpaOnnxSTT(
            None if name == "sherpa-onnx" else name, home=home, language=language
        )
    raise AudioError("no_stt", f"unknown stt provider {name!r}")


def resolve_provider(
    name: str = "auto",
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
    home: Path | str | None = None,
    language: str | None = None,
) -> STTProvider | None:
    """The named backend, or ``None`` when it is not usable here.

    It is returned only when it is actually available, so the caller can report
    *why* speech input is off rather than failing later.  There is no fallback:
    an engine the user pinned and does not have is an engine that is missing,
    not a reason to quietly use a different one.
    """
    # One name, always: the caller pins an engine or asks for nothing.
    names = (name,) if name else ()
    for candidate in names:
        try:
            provider = build_provider(
                candidate,
                api_key=api_key,
                model=model,
                base_url=base_url,
                command=command,
                home=home,
                language=language,
            )
        except AudioError:
            continue
        if provider.available():
            return provider
    return None


def resolve_any(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    command: str | None = None,
    home: Path | str | None = None,
    language: str | None = None,
) -> STTProvider | None:
    """The first usable backend in :data:`RECOMMENDED_ORDER`, or ``None``.

    For the ``transcribe_audio`` **tool**, which the model calls on purpose and
    which should use whatever this machine has.  Voice *input* does not come
    through here: a user who turned the microphone on pinned an engine, and if
    it is missing the honest answer is that it is missing.
    """
    for candidate in RECOMMENDED_ORDER:
        try:
            provider = build_provider(
                candidate,
                api_key=api_key,
                model=model,
                base_url=base_url,
                command=command,
                home=home,
                language=language,
            )
        except AudioError:
            continue
        if provider.available():
            return provider
    return None


__all__ = [
    "resolve_any",
    "RECOMMENDED_ORDER",
    "SHERPA_OFFLINE_BIN",
    "SHERPA_ONLINE_BIN",
    "SHERPA_PROVIDERS",
    "MODEL_BY_LANGUAGE",
    "SherpaOnnxSTT",
    "parse_sherpa_output",
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
