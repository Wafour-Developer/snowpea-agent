"""Voice I/O: player, recorder, transcription, speech and the capability report."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from snowpea_core.audio import (
    MISSING_ENGINE_REASON,
    NO_ENGINE_REASON,
    AudioConfig,
    audio_dir,
    capabilities,
    stt_providers,
)
from snowpea_core.audio import tts as tts_mod
from snowpea_core.audio.player import (
    AudioError,
    available_players,
    can_play,
    find_player,
    play,
)
from snowpea_core.audio.recorder import Recorder, available_recorders, find_recorder
from snowpea_core.audio.stt import (
    CommandSTT,
    LocalWhisperSTT,
    OpenAISTT,
    Transcript,
    build_provider,
    resolve_provider,
)
from snowpea_core.audio.tts import (
    CommandTTS,
    EdgeTTS,
    EspeakTTS,
    OpenAITTS,
    PiperTTS,
    SayTTS,
    Speech,
    materialise,
    parse_result,
    synthesize,
)


def write_script(directory: Path, name: str, body: str) -> Path:
    """Drop an executable shell script into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def only_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty PATH so only the fakes this test installs are discoverable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fakes are POSIX shell scripts")


# ---------------------------------------------------------------------------
# player
# ---------------------------------------------------------------------------


def test_no_player_is_reported_not_raised(only_path: Path) -> None:
    assert find_player() is None
    assert available_players() == []
    assert not can_play()


async def test_play_without_a_player_raises_no_player(only_path: Path, tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(AudioError) as excinfo:
        await play(audio)
    assert excinfo.value.code == "no_player"


async def test_play_runs_the_first_available_player(only_path: Path, tmp_path: Path) -> None:
    marker = tmp_path / "played.txt"
    write_script(only_path, "mpv", f'echo "$@" > "{marker}"\nexit 0\n')
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    assert available_players() == ["mpv"]
    assert await play(audio) == "mpv"
    assert str(audio) in marker.read_text()


async def test_play_surfaces_a_failing_player(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "mpv", 'echo "broken speaker" >&2\nexit 3\n')
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(AudioError) as excinfo:
        await play(audio)
    assert excinfo.value.code == "playback_failed"
    assert "broken speaker" in str(excinfo.value)


async def test_play_rejects_a_missing_file(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "mpv", "exit 0\n")
    with pytest.raises(AudioError) as excinfo:
        await play(tmp_path / "nope.wav")
    assert excinfo.value.code == "playback_failed"


def test_preferred_player_wins_and_falls_back(only_path: Path) -> None:
    write_script(only_path, "mpv", "exit 0\n")
    write_script(only_path, "ffplay", "exit 0\n")
    # ffplay comes first in PLAYERS, but the preference overrides the order.
    assert find_player().name == "ffplay"  # type: ignore[union-attr]
    assert find_player("mpv").name == "mpv"  # type: ignore[union-attr]
    # An unknown preference falls back rather than failing.
    assert find_player("nosuchplayer").name == "ffplay"  # type: ignore[union-attr]


def test_a_preferred_player_may_be_any_command(only_path: Path) -> None:
    script = write_script(only_path, "my-player", "exit 0\n")
    player = find_player(str(script))
    assert player is not None
    assert player.argv(Path("/tmp/a.wav"))[-1] == "/tmp/a.wav"


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------

# The fake blocks on stdin rather than sleeping: PATH holds nothing but the
# fakes themselves, so the script cannot call out to `sleep`.
RECORDER_SCRIPT = """
out=""
for a in "$@"; do out="$a"; done
printf 'RIFF....WAVEfake' > "$out"
trap 'exit 0' INT
read line
exit 0
"""

#: A recorder that floods stderr; draining it is what keeps ``stop`` from hanging.
NOISY_RECORDER_SCRIPT = """
out=""
for a in "$@"; do out="$a"; done
printf 'RIFF....WAVEfake' > "$out"
trap 'exit 0' INT
i=0
while [ $i -lt 4000 ]; do
  echo "frame $i dropped, buffer underrun, please stand by" >&2
  i=$((i + 1))
done
read line
exit 0
"""


def test_no_recorder_is_reported(only_path: Path) -> None:
    assert find_recorder() is None
    assert available_recorders() == []


async def test_recorder_without_a_backend(only_path: Path, tmp_path: Path) -> None:
    with pytest.raises(AudioError) as excinfo:
        await Recorder(tmp_path).start()
    assert excinfo.value.code == "no_recorder"


async def test_record_start_and_stop(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "rec", RECORDER_SCRIPT)
    assert available_recorders() == ["sox"]

    recorder = Recorder(tmp_path / "audio")
    path = await recorder.start()
    assert recorder.recording
    assert path.parent == tmp_path / "audio"
    await asyncio.sleep(0.1)

    finished = await recorder.stop()
    assert finished == path
    assert finished.read_bytes().startswith(b"RIFF")
    assert not recorder.recording


async def test_two_recordings_cannot_overlap(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "rec", RECORDER_SCRIPT)
    recorder = Recorder(tmp_path)
    await recorder.start()
    try:
        with pytest.raises(AudioError) as excinfo:
            await recorder.start()
        assert excinfo.value.code == "record_failed"
    finally:
        await recorder.cancel()


async def test_stop_without_start(tmp_path: Path) -> None:
    with pytest.raises(AudioError) as excinfo:
        await Recorder(tmp_path).stop()
    assert excinfo.value.code == "not_recording"


async def test_cancel_removes_the_file(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "rec", RECORDER_SCRIPT)
    recorder = Recorder(tmp_path)
    path = await recorder.start()
    await recorder.cancel()
    assert not path.exists()
    assert not recorder.recording


async def test_a_silent_recorder_is_an_error(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "rec", "exit 0\n")
    recorder = Recorder(tmp_path)
    await recorder.start()
    with pytest.raises(AudioError) as excinfo:
        await recorder.stop()
    assert excinfo.value.code == "record_failed"


async def test_stop_drains_a_chatty_recorder(only_path: Path, tmp_path: Path) -> None:
    """A tool that fills its stderr pipe must still be stoppable."""
    write_script(only_path, "rec", NOISY_RECORDER_SCRIPT)
    recorder = Recorder(tmp_path)
    await recorder.start()
    await asyncio.sleep(0.2)
    finished = await asyncio.wait_for(recorder.stop(), timeout=20)
    assert finished.read_bytes().startswith(b"RIFF")


def test_ffmpeg_argv_names_a_capture_device(only_path: Path) -> None:
    write_script(only_path, "ffmpeg", "exit 0\n")
    backend = find_recorder()
    assert backend is not None and backend.name == "ffmpeg"
    argv = backend.argv(Path("/tmp/a.wav"))
    assert "-i" in argv
    assert argv[-1] == "/tmp/a.wav"


def test_ffmpeg_without_audio_devices_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "snowpea_core.audio.recorder._ffmpeg_has_audio_device",
        lambda: False,
    )
    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg" if name == "ffmpeg" else None)
    assert find_recorder() is None
    assert available_recorders() == []


def test_ffmpeg_probe_ignores_error_lines_without_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Screen-only ffmpeg builds list video devices but no microphone."""
    from snowpea_core.audio.recorder import _ffmpeg_has_audio_device

    stderr = """\
[AVFoundation indev @ 0x7f998c804ec0] AVFoundation video devices:
[AVFoundation indev @ 0x7f998c804ec0] [0] Capture screen 0
[AVFoundation indev @ 0x7f998c804ec0] [1] Capture screen 1
[AVFoundation indev @ 0x7f998c804ec0] AVFoundation audio devices:
[in#0 @ 0x7f998c8048c0] Error opening input: Input/output error
Error opening input file .
"""
    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", stderr)

    monkeypatch.setattr("snowpea_core.audio.recorder.subprocess.run", fake_run)
    assert _ffmpeg_has_audio_device() is False


# ---------------------------------------------------------------------------
# stt: openai
# ---------------------------------------------------------------------------


def openai_stt(handler: object, **kwargs: object) -> OpenAISTT:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
            base_url="https://api.openai.com/v1",
        )

    return OpenAISTT("sk-test", client_factory=factory, **kwargs)  # type: ignore[arg-type]


async def test_openai_transcribes(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  hello there  "})

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....WAVE")
    result = await openai_stt(handler).transcribe(audio, "audio/wav")

    assert result == Transcript(text="hello there", provider="openai")
    assert seen["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test"
    assert b"whisper-1" in bytes(seen["body"])  # type: ignore[arg-type]
    assert b"clip.wav" in bytes(seen["body"])  # type: ignore[arg-type]


async def test_openai_honours_the_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"gpt-4o-transcribe" in request.content
        return httpx.Response(200, json={"text": "ok"})

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    provider = openai_stt(handler, model="gpt-4o-transcribe")
    assert (await provider.transcribe(audio)).text == "ok"


async def test_openai_reports_an_http_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(AudioError) as excinfo:
        await openai_stt(handler).transcribe(audio)
    assert excinfo.value.code == "transcribe_failed"
    assert "401" in str(excinfo.value)


async def test_openai_accepts_a_plain_text_body(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain transcript\n")

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    assert (await openai_stt(handler).transcribe(audio)).text == "plain transcript"


async def test_openai_without_a_key_is_unavailable(tmp_path: Path) -> None:
    provider = OpenAISTT(None)
    assert not provider.available()
    with pytest.raises(AudioError) as excinfo:
        await provider.transcribe(tmp_path / "a.wav")
    assert excinfo.value.code == "no_stt"


# ---------------------------------------------------------------------------
# stt: local whisper and command
# ---------------------------------------------------------------------------


async def test_local_whisper_reads_the_written_transcript(
    only_path: Path, tmp_path: Path
) -> None:
    write_script(
        only_path,
        "whisper",
        """
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  if [ "$a" = "--output_dir" ]; then next=1; fi
done
printf 'transcribed locally\n' > "$out/clip.txt"
exit 0
""",
    )
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    provider = LocalWhisperSTT()
    assert provider.available()
    result = await provider.transcribe(audio)
    assert result == Transcript(text="transcribed locally", provider="local-whisper")


async def test_local_whisper_surfaces_a_failure(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "whisper", 'echo "model missing" >&2\nexit 2\n')
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(AudioError) as excinfo:
        await LocalWhisperSTT().transcribe(audio)
    assert excinfo.value.code == "transcribe_failed"
    assert "model missing" in str(excinfo.value)


async def test_local_whisper_without_a_cli(only_path: Path, tmp_path: Path) -> None:
    provider = LocalWhisperSTT()
    assert not provider.available()
    with pytest.raises(AudioError) as excinfo:
        await provider.transcribe(tmp_path / "a.wav")
    assert excinfo.value.code == "no_stt"


def test_command_argv_substitutes_the_path() -> None:
    provider = CommandSTT("my-stt --in {path} --lang ko")
    assert provider.argv(Path("/tmp/a.wav")) == ["my-stt", "--in", "/tmp/a.wav", "--lang", "ko"]
    # Without a placeholder the path is appended.
    assert CommandSTT("my-stt").argv(Path("/tmp/a.wav")) == ["my-stt", "/tmp/a.wav"]


async def test_command_stt_reads_stdout(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "my-stt", 'printf "from the command\n"\nexit 0\n')
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    provider = CommandSTT("my-stt {path}")
    assert provider.available()
    assert (await provider.transcribe(audio)).text == "from the command"


async def test_command_stt_needs_a_template(tmp_path: Path) -> None:
    provider = CommandSTT(None)
    assert not provider.available()
    with pytest.raises(AudioError) as excinfo:
        await provider.transcribe(tmp_path / "a.wav")
    assert excinfo.value.code == "no_stt"


# ---------------------------------------------------------------------------
# stt: resolution
# ---------------------------------------------------------------------------


def test_resolving_a_pinned_engine_never_falls_back(only_path: Path) -> None:
    """Two states: an engine is pinned and usable, or it is not there.

    The chain is gone. A user who pinned whisper and does not have it gets
    "whisper is not installed", not a quiet switch to somebody's API.
    """
    assert resolve_provider("") is None
    assert resolve_provider("local-whisper") is None

    write_script(only_path, "my-stt", "exit 0\n")
    chosen = resolve_provider("command", command="my-stt {path}")
    assert chosen is not None and chosen.name == "command"
    # A key in the settings does not rescue a pinned local engine.
    assert resolve_provider("local-whisper", api_key="sk-x") is None

    write_script(only_path, "whisper", "exit 0\n")
    chosen = resolve_provider("local-whisper", api_key="sk-x")
    assert chosen is not None and chosen.name == "local-whisper"


def test_the_recommendation_order_is_local_first(only_path: Path) -> None:
    """It no longer resolves anything; it is what the wizard suggests."""
    from snowpea_core.audio import stt as stt_mod

    order = list(stt_mod.RECOMMENDED_ORDER)
    assert order[0] == "sherpa-onnx-sensevoice"
    assert order.index("local-whisper") < order.index("openai")


def test_stt_providers_lists_everything_usable(only_path: Path) -> None:
    write_script(only_path, "whisper", "exit 0\n")
    write_script(only_path, "my-stt", "exit 0\n")
    config = AudioConfig(openai_api_key="sk-x", stt_command="my-stt {path}")
    assert stt_providers(config) == ["local-whisper", "openai", "command"]


def test_stt_providers_is_empty_on_a_bare_machine(only_path: Path) -> None:
    assert stt_providers(AudioConfig()) == []


def test_resolve_a_named_provider_only_when_it_works(only_path: Path) -> None:
    assert resolve_provider("openai") is None
    assert resolve_provider("openai", api_key="sk-x") is not None
    assert resolve_provider("local-whisper") is None
    assert resolve_provider("nonsense") is None


def test_build_provider_rejects_an_unknown_name() -> None:
    with pytest.raises(AudioError):
        build_provider("telepathy")


# ---------------------------------------------------------------------------
# tts
# ---------------------------------------------------------------------------


def test_parse_result_finds_the_audio_in_every_shape(tmp_path: Path) -> None:
    assert parse_result({"url": "https://e.g/a.mp3"}) == ("url", "https://e.g/a.mp3")
    assert parse_result(json.dumps({"asset": {"audio_url": "https://e.g/b.wav"}})) == (
        "url",
        "https://e.g/b.wav",
    )
    assert parse_result("Generated: https://e.g/c.mp3 (3s)") == ("url", "https://e.g/c.mp3")
    local = tmp_path / "d.wav"
    local.write_bytes(b"RIFF")
    assert parse_result({"path": str(local)}) == ("path", str(local))
    assert parse_result(str(local)) == ("path", str(local))
    payload = base64.b64encode(b"x" * 100).decode()
    assert parse_result({"audio_base64": payload}) == ("base64", payload)


def test_parse_result_without_audio() -> None:
    with pytest.raises(AudioError) as excinfo:
        parse_result({"status": "queued"})
    assert excinfo.value.code == "synthesis_failed"
    with pytest.raises(AudioError):
        parse_result("the server said no")


async def test_materialise_downloads_a_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ID3-audio")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    speech = await materialise(
        "url", "https://e.g/a.mp3", tmp_path / "out", stem="s1", client_factory=factory
    )
    assert speech.mime == "audio/mpeg"
    assert speech.path == tmp_path / "out" / "s1.mp3"
    assert speech.path.read_bytes() == b"ID3-audio"


async def test_materialise_reports_a_failed_download(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(AudioError) as excinfo:
        await materialise(
            "url", "https://e.g/a.mp3", tmp_path / "out", stem="s1", client_factory=factory
        )
    assert excinfo.value.code == "synthesis_failed"


async def test_materialise_copies_a_path_and_decodes_base64(tmp_path: Path) -> None:
    source = tmp_path / "src.wav"
    source.write_bytes(b"RIFF....WAVE")
    copied = await materialise("path", str(source), tmp_path / "out", stem="s2")
    assert copied.mime == "audio/wav"
    assert copied.path.read_bytes() == b"RIFF....WAVE"

    payload = base64.b64encode(b"ID3").decode()
    decoded = await materialise(
        "base64", f"data:audio/mpeg;base64,{payload}", tmp_path / "out", stem="s3"
    )
    assert decoded.path.read_bytes() == b"ID3"
    assert decoded.mime == "audio/mpeg"


async def test_materialise_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AudioError) as excinfo:
        await materialise("path", str(tmp_path / "gone.wav"), tmp_path, stem="s")
    assert excinfo.value.code == "synthesis_failed"


async def test_studio_backend_calls_the_media_tool(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    source = tmp_path / "spoken.wav"
    source.write_bytes(b"RIFF....WAVE")

    async def caller(name: str, args: dict[str, object]) -> object:
        calls.append((name, args))
        return {"path": str(source)}

    speech = await synthesize(
        "hello", caller=caller, out_dir=tmp_path / "out", voice="ko-1", language="ko", stem="s"
    )
    assert calls == [("text_to_speech", {"text": "hello", "voice": "ko-1", "language": "ko"})]
    assert isinstance(speech, Speech)
    assert speech.voice == "ko-1"
    assert speech.to_payload() == {
        "path": str(tmp_path / "out" / "s.wav"),
        "mime": "audio/wav",
        "voice": "ko-1",
        "provider": "studio",
    }


async def test_synthesize_rejects_empty_text(tmp_path: Path) -> None:
    async def caller(name: str, args: dict[str, object]) -> object:  # pragma: no cover
        raise AssertionError("must not be called")

    with pytest.raises(AudioError) as excinfo:
        await synthesize("   ", caller=caller, out_dir=tmp_path)
    assert excinfo.value.code == "synthesis_failed"


async def test_synthesize_wraps_a_backend_failure(tmp_path: Path) -> None:
    async def caller(name: str, args: dict[str, object]) -> object:
        raise RuntimeError("studio is down")

    with pytest.raises(AudioError) as excinfo:
        await synthesize("hi", caller=caller, out_dir=tmp_path)
    assert excinfo.value.code == "synthesis_failed"
    assert "studio is down" in str(excinfo.value)


async def test_synthesize_without_any_backend(only_path: Path, tmp_path: Path) -> None:
    with pytest.raises(AudioError) as excinfo:
        await synthesize("hi", out_dir=tmp_path)
    assert excinfo.value.code == "no_tts"
    assert "edge-tts" in str(excinfo.value)


# -- openai speech


async def test_openai_speech_writes_an_mp3(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"ID3-mp3-bytes")

    provider = OpenAITTS(
        "sk-test",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.openai.com/v1"
        ),
    )
    assert provider.available()
    speech = await provider.synthesize("hello", out_dir=tmp_path, voice="nova", stem="s")

    assert speech.path == tmp_path / "s.mp3"
    assert speech.path.read_bytes() == b"ID3-mp3-bytes"
    assert speech.mime == "audio/mpeg"
    assert speech.provider == "openai"
    assert seen["url"] == "https://api.openai.com/v1/audio/speech"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {
        "model": "tts-1",
        "input": "hello",
        "voice": "nova",
        "response_format": "mp3",
    }


async def test_openai_speech_reports_an_http_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    provider = OpenAITTS(
        "sk-test",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.openai.com/v1"
        ),
    )
    with pytest.raises(AudioError) as excinfo:
        await provider.synthesize("hi", out_dir=tmp_path)
    assert excinfo.value.code == "synthesis_failed"
    assert "429" in str(excinfo.value)


async def test_openai_speech_without_a_key(tmp_path: Path) -> None:
    provider = OpenAITTS(None)
    assert not provider.available()
    with pytest.raises(AudioError) as excinfo:
        await provider.synthesize("hi", out_dir=tmp_path)
    assert excinfo.value.code == "no_tts"


# -- local CLIs

WRITER = """
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  if [ "$a" = "{flag}" ]; then next=1; fi
done
printf '{magic}' > "$out"
exit 0
"""


async def test_edge_tts_writes_an_mp3(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "edge-tts", WRITER.format(flag="--write-media", magic="ID3"))
    provider = EdgeTTS()
    assert provider.available()
    speech = await provider.synthesize("hi", out_dir=tmp_path, voice="ko-KR-SunHiNeural", stem="s")
    assert speech.path == tmp_path / "s.mp3"
    assert speech.mime == "audio/mpeg"
    assert speech.provider == "edge-tts"


async def test_piper_takes_text_on_stdin(only_path: Path, tmp_path: Path) -> None:
    write_script(
        only_path,
        "piper",
        """
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  if [ "$a" = "--output_file" ]; then next=1; fi
done
read spoken
printf "RIFF$spoken" > "$out"
exit 0
""",
    )
    speech = await PiperTTS().synthesize("hello piper", out_dir=tmp_path, stem="s")
    assert speech.path.read_bytes() == b"RIFFhello piper"
    assert speech.mime == "audio/wav"


async def test_espeak_and_say_argv(only_path: Path, tmp_path: Path) -> None:
    assert EspeakTTS().argv("hi", tmp_path / "a.wav", "en") == [
        "espeak-ng",
        "-w",
        str(tmp_path / "a.wav"),
        "-v",
        "en",
        "hi",
    ]
    argv = SayTTS().argv("hi", tmp_path / "a.wav", None)
    assert argv[:2] == ["say", "-o"]
    assert argv[-1] == "hi"


async def test_a_cli_that_writes_nothing_is_an_error(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "espeak-ng", 'echo "no voice data" >&2\nexit 0\n')
    with pytest.raises(AudioError) as excinfo:
        await EspeakTTS().synthesize("hi", out_dir=tmp_path, stem="s")
    assert excinfo.value.code == "synthesis_failed"
    assert "no voice data" in str(excinfo.value)


async def test_a_failing_cli_is_reported(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "espeak-ng", 'echo "boom" >&2\nexit 4\n')
    with pytest.raises(AudioError) as excinfo:
        await EspeakTTS().synthesize("hi", out_dir=tmp_path, stem="s")
    assert excinfo.value.code == "synthesis_failed"
    assert "exited 4" in str(excinfo.value)


async def test_command_tts_template(only_path: Path, tmp_path: Path) -> None:
    write_script(only_path, "my-tts", 'printf "RIFF$1" > "$2"\nexit 0\n')
    provider = CommandTTS("my-tts {text} {out}")
    assert provider.available()
    speech = await provider.synthesize("spoken", out_dir=tmp_path, stem="s")
    assert speech.path.read_bytes() == b"RIFFspoken"
    assert speech.provider == "command"


def test_command_tts_needs_a_template() -> None:
    assert not CommandTTS(None).available()


# -- resolution


def test_a_pinned_voice_is_the_only_voice(only_path: Path) -> None:
    """No chain: pinning an engine you do not have is not a reason to use another."""
    assert tts_mod.resolve_provider("") is None
    assert tts_mod.resolve_provider("espeak-ng") is None

    write_script(only_path, "espeak-ng", "exit 0\n")
    chosen = tts_mod.resolve_provider("espeak-ng")
    assert chosen is not None and chosen.name == "espeak-ng"

    # Another engine being installed changes nothing about the pinned one.
    write_script(only_path, "edge-tts", "exit 0\n")
    chosen = tts_mod.resolve_provider("espeak-ng")
    assert chosen is not None and chosen.name == "espeak-ng"
    # And a key does not make a missing local engine resolve.
    assert tts_mod.resolve_provider("piper", api_key="sk-x") is None


def test_the_media_tool_keeps_its_chain(only_path: Path) -> None:
    """``text_to_speech`` uses what the machine has; voice output does not."""
    assert tts_mod.resolve_any() is None

    write_script(only_path, "espeak-ng", "exit 0\n")
    chosen = tts_mod.resolve_any()
    assert chosen is not None and chosen.name == "espeak-ng"

    write_script(only_path, "edge-tts", "exit 0\n")
    chosen = tts_mod.resolve_any()
    assert chosen is not None and chosen.name == "edge-tts", "recommendation order"

    # Studio is last, so it is reached only when it is all there is — which is
    # what keeps the media tool working on a machine set up for it.
    assert tts_mod.RECOMMENDED_ORDER[-1] == "studio"
    chosen = tts_mod.resolve_any(studio_configured=True)
    assert chosen is not None and chosen.name == "edge-tts"


def test_build_tts_provider_rejects_an_unknown_name() -> None:
    with pytest.raises(AudioError):
        tts_mod.build_provider("telepathy")


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capabilities_on_a_bare_machine(only_path: Path) -> None:
    report = capabilities(AudioConfig())
    assert report["stt"] is None
    assert report["tts"] is False
    assert report["ttsProvider"] is None
    assert report["record"] is False
    assert report["play"] is False
    assert report["autoSpeak"] is False
    assert set(report["reasons"]) == {"stt", "tts", "record", "play"}
    # Nothing is pinned, and that is the reason — not a list of things that
    # were tried and failed, which is what the chain could only ever report.
    assert report["reasons"]["stt"] == NO_ENGINE_REASON
    assert report["reasons"]["tts"] == NO_ENGINE_REASON
    assert report["sttPinned"] is False and report["ttsPinned"] is False
    assert report["sttProviders"] == []
    assert report["ttsProviders"] == []


def test_capabilities_says_which_engine_is_missing(only_path: Path) -> None:
    report = capabilities(AudioConfig(stt_provider="local-whisper", tts_provider="piper"))
    assert report["sttPinned"] is True and report["ttsPinned"] is True
    assert report["reasons"]["stt"] == MISSING_ENGINE_REASON.format(engine="local-whisper")
    assert report["reasons"]["tts"] == MISSING_ENGINE_REASON.format(engine="piper")


def test_capabilities_with_everything(only_path: Path) -> None:
    write_script(only_path, "mpv", "exit 0\n")
    write_script(only_path, "rec", "exit 0\n")
    write_script(only_path, "whisper", "exit 0\n")
    write_script(only_path, "piper", "exit 0\n")
    config = AudioConfig(
        openai_api_key="sk-x",
        auto_speak=True,
        voice="nova",
        studio_configured=True,
        # Both directions pinned: installed is not the same as chosen.
        stt_provider="local-whisper",
        tts_provider="piper",
    )
    report = capabilities(config)
    assert report["stt"] == "local-whisper"
    assert report["tts"] is True
    assert report["ttsProvider"] == "piper"
    assert report["voice"] == "nova"
    assert report["autoSpeak"] is True
    assert report["record"] is True
    assert report["play"] is True
    assert report["reasons"] == {}
    assert report["players"] == ["mpv"]
    assert report["recorders"] == ["sox"]
    assert report["sttProviders"] == ["local-whisper", "openai"]
    assert report["ttsProviders"] == ["piper", "openai", "studio"]


def test_an_installed_engine_still_has_to_be_pinned(only_path: Path) -> None:
    """Installing and choosing are separate steps, and so are their states."""
    write_script(only_path, "espeak-ng", "exit 0\n")

    report = capabilities(AudioConfig())
    assert report["tts"] is False, "on disk is not the same as chosen"
    assert report["reasons"]["tts"] == NO_ENGINE_REASON
    # Detection still sees it, which is what the wizard lists.
    assert "espeak-ng" in report["ttsProviders"]

    pinned = capabilities(AudioConfig(tts_provider="espeak-ng"))
    assert pinned["tts"] is True
    assert pinned["ttsProvider"] == "espeak-ng"
    assert pinned["ttsEffective"] == "espeak-ng"
    assert pinned["ttsPinned"] is True
    assert "tts" not in pinned["reasons"]


def test_capabilities_explains_a_disabled_tts(only_path: Path) -> None:
    report = capabilities(AudioConfig(tts_enabled=False, studio_configured=True))
    assert report["tts"] is False
    assert "switched off" in report["reasons"]["tts"]


def test_capabilities_names_an_unusable_provider(only_path: Path) -> None:
    report = capabilities(AudioConfig(stt_provider="local-whisper", tts_provider="piper"))
    assert report["stt"] is None
    assert "local-whisper" in report["reasons"]["stt"]
    assert "piper" in report["reasons"]["tts"]


def test_audio_dir_is_scoped_by_session(tmp_path: Path) -> None:
    assert audio_dir(tmp_path) == tmp_path / "audio"
    assert audio_dir(tmp_path, "sess-1") == tmp_path / "audio" / "sess-1"
