"""The speech-model table: what to download, from where, and how to check it.

One table, in one place, because a model download is the part of voice setup
that is slow, large and impossible to eyeball.  Everything about a model — the
release asset it comes from, the archive layout it unpacks to, the files a
decoder needs out of it — is a row here rather than a string somewhere in the
backend.

Provenance, verified 2026-09-15 against the sherpa-onnx documentation:

* the ASR models are official **sherpa-onnx GitHub release assets** under the
  ``asr-models`` tag (k2-fsa/sherpa-onnx), the same URLs the upstream docs tell
  a user to ``wget``;
* ``silero_vad.onnx`` ships from the same release and is what lets the offline
  SenseVoice decoder cut a long recording into utterances.

**Checksums are not pinned yet.**  ``sha256`` is ``None`` on every row: upstream
publishes no digest alongside these assets, and inventing one would make
verification fail for everybody rather than protect anybody.  A row with a
digest *is* verified, so filling the table in later needs no code change —
:func:`verify` already refuses a mismatch.  See ``CORE-audio-install`` for the
open item.

Downloads are **resumable** (HTTP ``Range`` against the partial file) and
**cancellable** (the caller's ``asyncio`` task is cancelled and the partial
file is left where it is, ready to resume).
"""

from __future__ import annotations

import hashlib
import logging
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("snowpea.audio.models")

#: Where every downloaded speech model lands, under ``$SNOWPEA_HOME``.
MODELS_DIRNAME = "models"

#: Subdirectory per family, so ``models/sherpa-onnx/<id>/`` is one model.
SHERPA_DIRNAME = "sherpa-onnx"

#: Written beside an unpacked model once it is complete and verified.  Its
#: presence is what detection reads, so a half-finished download never makes an
#: engine look installed.
STAMP_NAME = ".snowpea-ok"

#: Bytes per read while streaming a download.
CHUNK = 1 << 16

#: Where the official assets live.  Pinned as a prefix so a row is a filename.
SHERPA_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

#: The VAD every offline decoder uses to cut a recording into utterances.
SILERO_VAD = "silero_vad.onnx"


@dataclass(frozen=True)
class SpeechModel:
    """One downloadable speech model."""

    #: Catalog id, matching the audio catalog row and ``audio.install``.
    id: str
    #: Human label, used in logs and the wizard.
    label: str
    #: Release asset filename, appended to :data:`SHERPA_RELEASE`.
    asset: str
    #: ``"offline"`` decodes a whole file; ``"online"`` decodes a stream.
    kind: str = "offline"
    #: Languages the model actually recognises, for the ``auto`` chain.
    languages: tuple[str, ...] = ()
    #: Expected digest of the archive.  ``None`` means "not pinned upstream";
    #: see the module docstring.
    sha256: str | None = None
    #: Extra assets fetched next to the model, e.g. the VAD.
    extras: tuple[str, ...] = ()
    #: Files the decoder needs, relative to the unpacked directory.  Detection
    #: checks these exist, so a truncated archive is not mistaken for a model.
    needs: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"{SHERPA_RELEASE}/{self.asset}"

    @property
    def unpacked_name(self) -> str:
        """The directory the archive unpacks to: the asset minus its suffixes."""
        name = self.asset
        for suffix in (".tar.bz2", ".tar.gz", ".tgz", ".tar"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return name

    def directory(self, home: Path | str) -> Path:
        return Path(home).expanduser() / MODELS_DIRNAME / SHERPA_DIRNAME / self.id

    def root(self, home: Path | str) -> Path:
        """Where the archive's own top-level directory ends up."""
        return self.directory(home) / self.unpacked_name

    def installed(self, home: Path | str) -> bool:
        """True when this model is fully downloaded, unpacked and stamped."""
        directory = self.directory(home)
        if not (directory / STAMP_NAME).is_file():
            return False
        root = self.root(home)
        return all((root / name).is_file() for name in self.needs)


#: Every speech model snowpea knows how to fetch.
#:
#: The int8 variants are deliberate: they are what makes these usable on a CPU,
#: which is the whole point of a local engine.  Real-time factors quoted in the
#: labels are upstream's own CPU figures, not measurements of ours.
MODELS: dict[str, SpeechModel] = {
    "sherpa-onnx-sensevoice": SpeechModel(
        id="sherpa-onnx-sensevoice",
        label="SenseVoiceSmall (zh/en/ja/ko/yue, ~17-20x RT, file/server transcription)",
        asset="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        kind="offline",
        languages=("zh", "en", "ja", "ko", "yue"),
        extras=(SILERO_VAD,),
        needs=("model.int8.onnx", "tokens.txt"),
    ),
    "sherpa-onnx-zipformer-ko": SpeechModel(
        id="sherpa-onnx-zipformer-ko",
        label="sherpa-onnx Korean Zipformer INT8 (streaming, ~10-38x RT on CPU)",
        asset="sherpa-onnx-streaming-zipformer-korean-2024-06-16.tar.bz2",
        kind="online",
        languages=("ko",),
        needs=(
            "encoder-epoch-99-avg-1.int8.onnx",
            "decoder-epoch-99-avg-1.onnx",
            "joiner-epoch-99-avg-1.int8.onnx",
            "tokens.txt",
        ),
    ),
    "sherpa-onnx-zipformer-en": SpeechModel(
        id="sherpa-onnx-zipformer-en",
        label="sherpa-onnx English Zipformer INT8 (streaming)",
        asset="sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2",
        kind="online",
        languages=("en",),
        needs=(
            "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "decoder-epoch-99-avg-1-chunk-16-left-128.onnx",
            "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
            "tokens.txt",
        ),
    ),
}

#: Model preferred for a language when the ``auto`` chain has a choice.  The
#: multilingual offline model leads because it needs no language guess to be
#: right; a streaming zipformer is picked only when it is the one installed.
BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "ko": ("sherpa-onnx-sensevoice", "sherpa-onnx-zipformer-ko"),
    "en": ("sherpa-onnx-sensevoice", "sherpa-onnx-zipformer-en"),
}

#: What a language nobody has a specific model for falls back to.
DEFAULT_ORDER: tuple[str, ...] = (
    "sherpa-onnx-sensevoice",
    "sherpa-onnx-zipformer-en",
    "sherpa-onnx-zipformer-ko",
)


def model_for(model_id: str) -> SpeechModel | None:
    return MODELS.get(model_id)


def installed_models(home: Path | str) -> list[str]:
    """Ids of every speech model fully present here, in table order."""
    return [name for name, model in MODELS.items() if model.installed(home)]


def preferred(home: Path | str, language: str | None = None) -> str | None:
    """The best installed model for ``language``, or ``None``.

    The order is the language's own list first (multilingual model leading),
    then everything else installed, so a user who installed only the Korean
    zipformer still gets it for English rather than getting silence.
    """
    tag = (language or "").strip().lower().partition("-")[0]
    order = [*BY_LANGUAGE.get(tag, ()), *DEFAULT_ORDER]
    seen: dict[str, None] = {}
    for name in order:
        seen.setdefault(name, None)
    for name in seen:
        model = MODELS.get(name)
        if model is not None and model.installed(home):
            return name
    return None


# ---------------------------------------------------------------------------
# downloading
# ---------------------------------------------------------------------------

#: ``(line) -> None``, the same progress callback the installer uses.
Progress = Callable[[str], Awaitable[None]]


def verify(path: Path, expected: str | None, *, name: str = "") -> bool:
    """True when ``path`` matches ``expected``, or when nothing is pinned.

    An unpinned row is reported once at INFO rather than silently accepted, so
    the gap is visible in a log instead of only in this docstring.
    """
    if not expected:
        log.info("no sha256 pinned for %s; skipping verification", name or path.name)
        return True
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    actual = digest.hexdigest()
    if actual == expected.lower():
        return True
    log.warning("%s: sha256 %s does not match the pinned %s", path.name, actual, expected)
    return False


async def download_file(
    url: str,
    target: Path,
    *,
    progress: Progress | None = None,
    fetch: Any = None,
    sha256: str | None = None,
    log: Any = None,
) -> bool:
    """Fetch ``url`` to ``target``, resuming a partial file when there is one.

    The bytes land in ``<target>.part`` and are only moved into place once the
    transfer finished and the digest (when one is pinned) matched, so an
    interrupted download can never be mistaken for a complete model.  Cancelling
    the surrounding task leaves the ``.part`` file for the next attempt.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    have = partial.stat().st_size if partial.is_file() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    if have and progress is not None:
        await progress(f"resuming {target.name} at {have} bytes")

    client_factory = fetch or _client
    try:
        async with client_factory() as client:
            async with client.stream("GET", url, headers=headers) as response:
                status = int(getattr(response, "status_code", 0))
                if status == 200 and have:
                    # The server ignored the range; start over rather than
                    # append a second copy of the file onto the first.
                    have = 0
                    partial.unlink(missing_ok=True)
                elif status not in (200, 206):
                    if progress is not None:
                        await progress(f"HTTP {status} for {url}")
                    return False
                total = _total(response, have)
                written = have
                with partial.open("ab" if have else "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                        written += len(chunk)
                        if _tick(written, len(chunk)):
                            line = _progress_line(target.name, written, total)
                            # Bytes as they arrive: a 400MB download and a
                            # checksum look identical in a log, and only one of
                            # them takes four minutes.
                            if log is not None:
                                await log.bytes(written, total, target.name)
                            elif progress is not None:
                                await progress(line)
    except Exception as exc:  # noqa: BLE001 - a failed download is a result
        if progress is not None:
            await progress(f"{target.name}: {type(exc).__name__}: {exc}")
        return False

    if log is not None:
        from snowpea_core.audio.install import STAGE_VERIFY

        await log.stage(STAGE_VERIFY, f"checking {target.name}")
    if not verify(partial, sha256, name=target.name):
        partial.unlink(missing_ok=True)
        if progress is not None:
            await progress(f"{target.name}: checksum mismatch, discarded")
        return False
    partial.replace(target)
    return True


def _client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=None, follow_redirects=True)


def _total(response: Any, have: int) -> int:
    """Total size from the response headers, or ``0`` when unknown."""
    headers = getattr(response, "headers", {}) or {}
    length: Any = headers.get("content-length") or headers.get("Content-Length")
    try:
        return int(length) + have  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


#: Progress is reported per this many bytes, so a 1GB model does not produce a
#: million notifications.
PROGRESS_STEP = 8 << 20


def _tick(written: int, chunk: int) -> bool:
    """True when this chunk crossed a progress boundary."""
    return written // PROGRESS_STEP != (written - chunk) // PROGRESS_STEP


def _progress_line(name: str, written: int, total: int) -> str:
    done = written / (1 << 20)
    if total:
        return f"{name}: {done:.0f} MiB / {total / (1 << 20):.0f} MiB"
    return f"{name}: {done:.0f} MiB"


def _safe_members(archive: tarfile.TarFile, root: Path) -> list[tarfile.TarInfo]:
    """Members that stay inside ``root``; anything else is a malicious archive."""
    safe: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        destination = (root / member.name).resolve()
        if destination == root.resolve() or root.resolve() in destination.parents:
            safe.append(member)
        else:
            log.warning("refusing archive member outside the target: %s", member.name)
    return safe


def unpack(archive: Path, into: Path) -> bool:
    """Extract a ``.tar.bz2`` model archive, refusing any path that escapes."""
    into.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as handle:
            members = _safe_members(handle, into)
            try:
                # `data` is the standard-library filter that refuses absolute
                # paths, traversal, links and device nodes; the member check
                # above is belt to its braces.
                handle.extractall(into, members=members, filter="data")  # noqa: S202
            except TypeError:  # pragma: no cover - Python without the filter argument
                handle.extractall(into, members=members)  # noqa: S202
    except (OSError, tarfile.TarError) as exc:
        log.warning("could not unpack %s: %s", archive.name, exc)
        return False
    return True


async def ensure_model(
    model_id: str,
    home: Path | str,
    *,
    progress: Progress | None = None,
    fetch: Any = None,
    keep_archive: bool = False,
    log: Any = None,
) -> bool:
    """Download, verify and unpack one model; ``True`` when it is ready.

    Already-installed models answer immediately: re-running an install is how a
    user retries a half-finished one, and it must not re-download a gigabyte to
    tell them it was fine.
    """
    model = MODELS.get(model_id)
    if model is None:
        if progress is not None:
            await progress(f"unknown speech model {model_id!r}")
        return False
    directory = model.directory(home)
    if model.installed(home):
        if progress is not None:
            await progress(f"{model.id} is already in {directory}")
        return True

    archive = directory / model.asset
    if log is not None:
        from snowpea_core.audio.install import STAGE_DOWNLOAD, STAGE_EXTRACT

        await log.stage(STAGE_DOWNLOAD, f"downloading {model.url}")
    elif progress is not None:
        await progress(f"downloading {model.url}")
    if not await download_file(
        model.url, archive, progress=progress, fetch=fetch, sha256=model.sha256, log=log
    ):
        return False
    if log is not None:
        await log.stage(STAGE_EXTRACT, f"unpacking {archive.name}")
    elif progress is not None:
        await progress(f"unpacking {archive.name}")
    if not unpack(archive, directory):
        return False
    if not keep_archive:
        archive.unlink(missing_ok=True)

    for extra in model.extras:
        target = directory / extra
        if target.is_file():
            continue
        if progress is not None:
            await progress(f"downloading {extra}")
        if not await download_file(
            f"{SHERPA_RELEASE}/{extra}", target, progress=progress, fetch=fetch, log=log
        ):
            # The VAD only improves long-file decoding; its absence is worth a
            # line in the log, not a failed install.
            if progress is not None:
                await progress(f"{extra} could not be downloaded; continuing without it")

    missing = [name for name in model.needs if not (model.root(home) / name).is_file()]
    if missing:
        if progress is not None:
            await progress(f"{model.id} is missing {', '.join(missing)} after unpacking")
        return False
    (directory / STAMP_NAME).write_text(model.asset, encoding="utf-8")
    return True


__all__ = [
    "BY_LANGUAGE",
    "CHUNK",
    "DEFAULT_ORDER",
    "MODELS",
    "MODELS_DIRNAME",
    "PROGRESS_STEP",
    "SHERPA_DIRNAME",
    "SHERPA_RELEASE",
    "SILERO_VAD",
    "STAMP_NAME",
    "Progress",
    "SpeechModel",
    "download_file",
    "ensure_model",
    "installed_models",
    "model_for",
    "preferred",
    "unpack",
    "verify",
]
