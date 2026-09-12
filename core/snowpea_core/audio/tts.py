"""Text to speech, on top of the existing ``text_to_speech`` media tool.

The core already knows how to synthesise speech: ``text_to_speech`` in
:mod:`snowpea_core.tools.media` forwards to the snowpea-studio MCP server's
``generate_speech``.  This module reuses that rather than adding a second
vendor integration — it calls the same MCP tool, finds the audio in whatever
the server answered (a url, a path, or inline base64), and lands it in a local
file that :mod:`snowpea_core.audio.player` can play or the client can fetch.

TTS is therefore *available* exactly when media generation is configured.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from snowpea_core.audio.player import AudioError

log = logging.getLogger("snowpea.audio.tts")

#: The MCP tool ``tools/media.py`` exposes for speech.
TOOL_NAME = "text_to_speech"

DEFAULT_TIMEOUT = 120.0

#: Audio extensions, longest first, used to guess the type of a url or path.
_AUDIO_MIMES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
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

#: ``(text, arguments) -> whatever the MCP tool returned``.
SpeechCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Speech:
    """A synthesised utterance sitting in a file."""

    path: Path
    mime: str
    voice: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """The JSON-safe shape ``audio.speak`` answers with."""
        payload: dict[str, Any] = {"path": str(self.path), "mime": self.mime}
        if self.voice:
            payload["voice"] = self.voice
        return payload


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
    if payload.startswith("data:"):
        header, _, payload = payload.partition(",")
        mime = header[5:].split(";", 1)[0] or "audio/mpeg"
    else:
        mime = "audio/mpeg"
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise AudioError("synthesis_failed", f"speech payload is not base64: {exc}") from exc
    target = out_dir / f"{stem}{extension_for(mime)}"
    target.write_bytes(raw)
    return Speech(path=target, mime=mime)


async def synthesize(
    text: str,
    *,
    caller: SpeechCaller,
    out_dir: Path | str,
    voice: str | None = None,
    language: str | None = None,
    stem: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    client_factory: Any = None,
) -> Speech:
    """Speak ``text`` and return the audio file it landed in.

    ``caller`` runs the MCP tool — in the daemon that is a thin wrapper around
    :mod:`snowpea_core.tools.media`; in the tests it is a stub.
    """
    body = (text or "").strip()
    if not body:
        raise AudioError("synthesis_failed", "nothing to speak")
    args: dict[str, Any] = {"text": body}
    if voice:
        args["voice"] = voice
    if language:
        args["language"] = language
    try:
        result = await caller(TOOL_NAME, args)
    except AudioError:
        raise
    except Exception as exc:  # noqa: BLE001 - the studio server can raise anything
        raise AudioError("synthesis_failed", f"{type(exc).__name__}: {exc}") from exc
    kind, value = parse_result(result)
    speech = await materialise(
        kind,
        value,
        Path(out_dir).expanduser(),
        stem=stem or f"speech-{abs(hash(body)) % 10**10}",
        timeout=timeout,
        client_factory=client_factory,
    )
    return Speech(path=speech.path, mime=speech.mime, voice=voice)


__all__ = [
    "DEFAULT_TIMEOUT",
    "TOOL_NAME",
    "Speech",
    "SpeechCaller",
    "extension_for",
    "materialise",
    "mime_for",
    "parse_result",
    "synthesize",
]
