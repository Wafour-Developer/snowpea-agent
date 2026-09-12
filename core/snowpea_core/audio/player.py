"""Playing an audio file with whatever the machine already has.

There is no portable audio API in the standard library, so the daemon shells
out to the first player it can find.  Nothing here is required: when no player
exists, :func:`play` raises :class:`AudioError` with ``code="no_player"`` and
the caller hands the file to the client to play instead.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("snowpea.audio.player")

#: How long a single playback may take before it is killed.
DEFAULT_TIMEOUT = 300.0


class AudioError(RuntimeError):
    """An audio operation that could not be performed.

    ``code`` is one of ``no_player``, ``no_recorder``, ``no_stt``,
    ``no_tts``, ``not_recording``, ``playback_failed``, ``record_failed``,
    ``transcribe_failed`` or ``synthesis_failed``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Player:
    """One command that can play a file given its path."""

    name: str
    executable: str
    args: tuple[str, ...] = ()

    def argv(self, path: Path) -> list[str]:
        return [self.executable, *self.args, str(path)]


#: Candidates in preference order: macOS, PulseAudio, ALSA, then the two
#: media players that are on half the machines in the world anyway.
PLAYERS: tuple[Player, ...] = (
    Player("afplay", "afplay"),
    Player("paplay", "paplay"),
    Player("aplay", "aplay", ("-q",)),
    Player("ffplay", "ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")),
    Player("mpv", "mpv", ("--no-video", "--really-quiet")),
)

#: Windows has no CLI player, but every install has PowerShell.
WINDOWS_PLAYER = Player("powershell", "powershell")


def _windows_argv(path: Path) -> list[str]:
    script = f'(New-Object Media.SoundPlayer "{path}").PlaySync()'
    return [WINDOWS_PLAYER.executable, "-NoProfile", "-NonInteractive", "-Command", script]


def available_players() -> list[str]:
    """The names of every player found on this machine, in preference order."""
    found = [player.name for player in PLAYERS if shutil.which(player.executable)]
    if sys.platform == "win32" and shutil.which(WINDOWS_PLAYER.executable):
        found.append(WINDOWS_PLAYER.name)
    return found


def find_player(preferred: str | None = None) -> Player | None:
    """The player to use: ``preferred`` if it exists, else the first available.

    ``preferred`` may be a known name (``"mpv"``) or the path of any command
    that takes a file as its last argument.
    """
    if preferred:
        for player in (*PLAYERS, WINDOWS_PLAYER):
            if player.name == preferred and shutil.which(player.executable):
                return player
        if shutil.which(preferred):
            return Player(Path(preferred).name, preferred)
        log.debug("configured player %r not found; falling back", preferred)
    for player in PLAYERS:
        if shutil.which(player.executable):
            return player
    if sys.platform == "win32" and shutil.which(WINDOWS_PLAYER.executable):
        return WINDOWS_PLAYER
    return None


def can_play(preferred: str | None = None) -> bool:
    """True when something on this machine can play a file."""
    return find_player(preferred) is not None


async def play(
    path: Path | str,
    *,
    preferred: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Play ``path`` to completion; returns the player's name.

    Raises :class:`AudioError` — ``no_player`` when nothing is installed,
    ``playback_failed`` when the player exits non-zero.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise AudioError("playback_failed", f"no such audio file: {target}")
    player = find_player(preferred)
    if player is None:
        raise AudioError(
            "no_player",
            "no audio player found; install one of afplay, paplay, aplay, ffplay or mpv",
        )
    argv = _windows_argv(target) if player.name == WINDOWS_PLAYER.name else player.argv(target)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AudioError("playback_failed", f"{player.name}: {exc}") from exc
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        raise AudioError("playback_failed", f"{player.name} timed out") from None
    if process.returncode:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise AudioError("playback_failed", f"{player.name} exited {process.returncode}: {detail}")
    return player.name


__all__ = [
    "DEFAULT_TIMEOUT",
    "PLAYERS",
    "WINDOWS_PLAYER",
    "AudioError",
    "Player",
    "available_players",
    "can_play",
    "find_player",
    "play",
]
