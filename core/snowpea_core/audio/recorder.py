"""Recording the microphone to a wav file.

Same shape as :mod:`snowpea_core.audio.player`: find the first capture tool
that exists, run it as a child process, and stop it on demand.  A recording is
started by ``audio.record.start`` and finished by ``audio.record.stop``, which
returns the path the wav was written to.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from snowpea_core.audio.player import AudioError

log = logging.getLogger("snowpea.audio.recorder")

# ffmpeg lists capture devices as ``…] [0] Built-in Microphone``.
_FFMPEG_DEVICE_LINE = re.compile(r"\] \[(\d+)\] .+")

#: Recording format: 16 kHz mono PCM, which every STT backend accepts.
SAMPLE_RATE = 16_000
CHANNELS = 1

#: How long ``stop`` waits for the tool to flush its wav header before killing it.
STOP_GRACE = 5.0


@dataclass(frozen=True)
class RecorderBackend:
    """One capture command, parameterised by the output path."""

    name: str
    executable: str

    def argv(self, path: Path) -> list[str]:
        rate = str(SAMPLE_RATE)
        channels = str(CHANNELS)
        if self.name == "sox":
            return [self.executable, "-q", "-r", rate, "-c", channels, str(path)]
        if self.name == "arecord":
            return [self.executable, "-q", "-f", "S16_LE", "-r", rate, "-c", channels, str(path)]
        # ffmpeg needs to be told which capture API to use, per platform.
        if sys.platform == "darwin":
            source = ["-f", "avfoundation", "-i", ":0"]
        elif sys.platform == "win32":
            source = ["-f", "dshow", "-i", "audio=default"]
        else:
            source = ["-f", "pulse", "-i", "default"]
        return [
            self.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            *source,
            "-ar",
            rate,
            "-ac",
            channels,
            "-y",
            str(path),
        ]


#: ``rec`` is sox's recorder; it is the only one that needs no format flags.
BACKENDS: tuple[RecorderBackend, ...] = (
    RecorderBackend("sox", "rec"),
    RecorderBackend("arecord", "arecord"),
    RecorderBackend("ffmpeg", "ffmpeg"),
)


def available_recorders() -> list[str]:
    """Names of the capture tools installed here, in preference order."""
    names: list[str] = []
    for backend in BACKENDS:
        if not shutil.which(backend.executable):
            continue
        if backend.name == "ffmpeg" and not _ffmpeg_has_audio_device():
            continue
        names.append(backend.name)
    return names


def _ffmpeg_has_audio_device() -> bool:
    """True when ffmpeg's avfoundation backend can see at least one microphone.

    On macOS ffmpeg is often installed without any capture devices listed — only
    screen capture — and then it happily writes a zero-byte wav. Treat that as
    "no recorder" so clients fall back to their own microphone.
    """
    executable = shutil.which("ffmpeg")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-f",
                "avfoundation",
                "-list_devices",
                "true",
                "-i",
                "",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        text = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    except (OSError, subprocess.TimeoutExpired):
        # A probe we could not run should not hide a backend that might work.
        return True
    in_audio = False
    for line in text.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if not in_audio:
            continue
        if "AVFoundation video devices:" in line:
            break
        if _FFMPEG_DEVICE_LINE.search(line):
            return True
    return False if in_audio else True


def find_recorder(preferred: str | None = None) -> RecorderBackend | None:
    """The backend to record with, or ``None`` when none is installed."""
    if preferred:
        for backend in BACKENDS:
            if backend.name == preferred and shutil.which(backend.executable):
                if backend.name == "ffmpeg" and not _ffmpeg_has_audio_device():
                    continue
                return backend
        log.debug("configured recorder %r not found; falling back", preferred)
    for backend in BACKENDS:
        if not shutil.which(backend.executable):
            continue
        if backend.name == "ffmpeg" and not _ffmpeg_has_audio_device():
            log.debug("ffmpeg is installed but lists no microphone; skipping")
            continue
        return backend
    return None


def can_record(preferred: str | None = None) -> bool:
    """True when this machine has something that can capture the microphone."""
    return find_recorder(preferred) is not None


class Recorder:
    """A single microphone recording, started and stopped by the RPC surface.

    One instance holds at most one child process; :meth:`start` on an already
    running recorder is refused so two clients cannot fight over the mic.
    """

    def __init__(self, directory: Path | str, *, preferred: str | None = None) -> None:
        self.directory = Path(directory).expanduser()
        self.preferred = preferred
        self._process: asyncio.subprocess.Process | None = None
        self._path: Path | None = None
        self._backend: RecorderBackend | None = None
        self._started_at: float = 0.0

    @property
    def recording(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def path(self) -> Path | None:
        """Where the current (or most recent) recording is being written."""
        return self._path

    async def start(self, path: Path | str | None = None) -> Path:
        """Begin recording; returns the wav path being written to."""
        if self.recording:
            raise AudioError("record_failed", "a recording is already running")
        backend = find_recorder(self.preferred)
        if backend is None:
            raise AudioError(
                "no_recorder",
                "no recorder found; install sox (rec), alsa-utils (arecord) or ffmpeg",
            )
        target = Path(path).expanduser() if path else self.directory / f"rec-{int(time.time())}.wav"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AudioError("record_failed", f"cannot write to {target.parent}: {exc}") from exc
        try:
            process = await asyncio.create_subprocess_exec(
                *backend.argv(target),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AudioError("record_failed", f"{backend.name}: {exc}") from exc
        self._process = process
        self._path = target
        self._backend = backend
        self._started_at = time.monotonic()
        log.debug("recording with %s to %s", backend.name, target)
        return target

    async def stop(self) -> Path:
        """Stop the recording and return the finished wav.

        The tool is interrupted rather than killed so it can write the wav
        header; a tool that ignores that is killed after :data:`STOP_GRACE`.
        """
        process = self._process
        path = self._path
        if process is None or path is None:
            raise AudioError("not_recording", "no recording is running")
        self._process = None
        if process.returncode is None:
            if self._backend is not None and self._backend.name == "ffmpeg":
                # ffmpeg finalises the file when it reads a "q" on stdin.
                with_stdin = process.stdin
                if with_stdin is not None:
                    with_stdin.write(b"q")
                    try:
                        await with_stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                        pass
            else:
                try:
                    process.send_signal(signal.SIGINT)
                except ProcessLookupError:  # pragma: no cover - it already exited
                    pass
            try:
                await asyncio.wait_for(_drain(process), timeout=STOP_GRACE)
            except TimeoutError:
                process.kill()
                await _drain(process)
        duration = time.monotonic() - self._started_at
        if not path.is_file() or path.stat().st_size == 0:
            raise AudioError("record_failed", f"recording produced no audio ({path})")
        log.debug("recorded %.1fs to %s", duration, path)
        return path

    async def cancel(self) -> None:
        """Stop and discard the current recording, ignoring every failure."""
        process, path = self._process, self._path
        self._process = None
        if process is not None and process.returncode is None:
            process.kill()
            await _drain(process)
        if path is not None:
            path.unlink(missing_ok=True)



async def _drain(process: asyncio.subprocess.Process) -> None:
    """Wait for ``process``, reading its pipes as it goes.

    A bare ``wait()`` deadlocks when the recorder is chatty: asyncio only
    resolves the exit future once every pipe has closed, and an undrained
    stderr stops being read at the stream's high-water mark.  ``communicate``
    keeps reading, so a tool that complains on every frame still exits.
    """
    try:
        await process.communicate()
    except (BrokenPipeError, ConnectionResetError, ValueError):  # pragma: no cover
        await process.wait()


__all__ = [
    "BACKENDS",
    "CHANNELS",
    "SAMPLE_RATE",
    "STOP_GRACE",
    "Recorder",
    "RecorderBackend",
    "available_recorders",
    "can_record",
    "find_recorder",
    "_ffmpeg_has_audio_device",
]
