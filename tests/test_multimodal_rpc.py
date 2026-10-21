"""Attachments on ``session.prompt`` and the ``audio.*`` RPCs (CORE-multimodal)."""

from __future__ import annotations

import base64
import shutil
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import FIXTURES, connect, fake_provider, make_daemon

from snowpea_core.attachments import pending
from snowpea_core.server.app_server import Daemon

pytestmark = pytest.mark.asyncio

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = await make_daemon(tmp_path / "home")
    try:
        yield instance
    finally:
        await instance.stop()
        pending.clear()


async def _session(client: Any, workdir: Path) -> str:
    workdir.mkdir(parents=True, exist_ok=True)
    result = await client.ok("session.create", {"workdir": str(workdir)})
    return str(result["sessionId"])


# ---------------------------------------------------------------------------
# session.prompt attachments
# ---------------------------------------------------------------------------


async def test_inline_image_is_stored_and_stashed(daemon: Daemon, tmp_path: Path) -> None:
    """A pasted image lands in the store and waits for the turn to consume it."""
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            await client.ok(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "what is this?",
                    "attachments": [
                        {
                            "kind": "image",
                            "name": "shot.png",
                            "mimeType": "image/png",
                            "data": base64.b64encode(PNG).decode(),
                        }
                    ],
                },
            )
        finally:
            await client.stop()

    stored = [p for p in (daemon.paths.attachments_dir / session_id).iterdir() if p.is_file()]
    assert len(stored) == 1
    assert stored[0].suffix == ".png"
    assert stored[0].read_bytes() == PNG


async def test_a_file_path_is_referenced_not_copied(daemon: Daemon, tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello from a file", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            await client.ok(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "read it",
                    "attachments": [{"kind": "file", "path": str(source)}],
                },
            )
        finally:
            await client.stop()
    # The user's own file is never duplicated into the store: it is only
    # indexed, where it is, as this session's attachment (1.23.0).
    files = daemon.paths.attachments_dir / session_id / "files"
    assert [p.name for p in files.iterdir()] == ["index.json"]
    assert "notes.txt" in (files / "index.json").read_text(encoding="utf-8")
    from snowpea_core.attachments.store import AttachmentStore

    [row] = AttachmentStore(daemon.paths.attachments_dir).listing(session_id)
    assert row["byPath"] is True and row["path"] == str(source.resolve())


async def test_an_oversized_attachment_is_refused(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size cap answers with ``invalid_params``, not a dropped connection.

    The cap is lowered for the test rather than sending 20MB: the websocket
    transport refuses a frame over 4MB long before the attachment code sees it,
    so a genuinely huge inline attachment is a transport error, not this one.
    """
    monkeypatch.setattr("snowpea_core.attachments.model.MAX_BYTES", 1024)
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            frame = await client.call(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "big",
                    "attachments": [
                        {
                            "kind": "file",
                            "name": "huge.bin",
                            "data": base64.b64encode(b"x" * 2048).decode(),
                        }
                    ],
                },
            )
        finally:
            await client.stop()
    assert frame["error"]["data"]["code"] == "invalid_params"
    assert "limit" in frame["error"]["message"]


async def test_a_missing_path_is_refused(daemon: Daemon, tmp_path: Path) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            frame = await client.call(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "look",
                    "attachments": [{"kind": "image", "path": str(tmp_path / "gone.png")}],
                },
            )
        finally:
            await client.stop()
    assert frame["error"]["data"]["code"] == "invalid_params"


async def test_inline_text_is_folded_into_the_prompt(daemon: Daemon, tmp_path: Path) -> None:
    """``kind="text"`` is content, not a file: nothing is stored for it."""
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            await client.ok(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "explain",
                    "attachments": [
                        {"kind": "text", "name": "snippet", "text": "def f(): pass"}
                    ],
                },
            )
        finally:
            await client.stop()
    assert not (daemon.paths.attachments_dir / session_id).exists()
    assert pending.peek(session_id) == []


async def test_a_turn_carries_the_attachment_into_history(tmp_path: Path) -> None:
    """End to end: prompt with an image, and the stored turn holds content blocks."""
    with fake_provider(FIXTURES / "providers" / "fake" / "basic.json"):
        daemon = await make_daemon(tmp_path / "home")
        try:
            async with aiohttp.ClientSession() as http:
                client = await connect(http, daemon)
                try:
                    session_id = await _session(client, tmp_path / "work")
                    result = await client.ok(
                        "session.prompt",
                        {
                            "sessionId": session_id,
                            "text": "hello",
                            "attachments": [
                                {
                                    "kind": "image",
                                    "name": "shot.png",
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(PNG).decode(),
                                }
                            ],
                        },
                    )
                    assert await client.wait_turn(result["turnId"]) == "complete"
                    assert daemon.core is not None
                    session = daemon.core.sessions.get(session_id)
                    assert session is not None
                    user = session.history.snapshot()[0]
                finally:
                    await client.stop()
        finally:
            await daemon.stop()
            pending.clear()

    # The turn kept the typed text and a block pointing at the stored file.
    assert isinstance(user.content, list)
    assert user.content[0] == {"type": "text", "text": "hello"}
    image = user.content[1]
    assert image["type"] == "image"
    # The marker names the original, which the agent may use as a file (1.23.0).
    original = daemon.paths.attachments_dir / session_id / "files" / "shot.png"
    assert image["text"] == f'[attached: "shot.png" → {original}]'
    assert image["original"] == str(original)
    assert Path(image["path"]).read_bytes() == PNG
    # The stash is empty: the turn consumed it.
    assert pending.peek(session_id) == []


async def test_a_slash_command_does_not_keep_the_attachment(
    daemon: Daemon, tmp_path: Path
) -> None:
    """Nothing would consume the bytes, so they are dropped rather than queued."""
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            await client.ok(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "/help",
                    "attachments": [
                        {
                            "kind": "image",
                            "name": "shot.png",
                            "data": base64.b64encode(PNG).decode(),
                        }
                    ],
                },
            )
        finally:
            await client.stop()
    assert pending.peek(session_id) == []


# ---------------------------------------------------------------------------
# audio.*
# ---------------------------------------------------------------------------


async def test_audio_capabilities_reports_reasons(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            report = await client.ok("audio.capabilities", {})
        finally:
            await client.stop()
    assert set(report) >= {
        "stt",
        "tts",
        "ttsProvider",
        "record",
        "play",
        "autoSpeak",
        "sttProviders",
        "ttsProviders",
        "players",
        "recorders",
        "reasons",
    }
    # Whatever this machine has, every capability that is off has a reason.
    for key in ("stt", "tts", "record", "play"):
        value = report[key]
        off = value is None if key == "stt" else not value
        assert (key in report["reasons"]) == off, (key, report)


async def test_audio_transcribe_without_a_backend_says_why(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call("audio.transcribe", {"path": str(clip)})
        finally:
            await client.stop()
    error = frame["error"]
    assert error["data"]["code"] == "invalid_params"
    assert error["data"]["details"]["audio"] == "no_stt"


async def test_audio_transcribe_uses_the_configured_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-configured STT command is honoured end to end."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "my-stt"
    script.write_text('#!/bin/sh\nprintf "the transcript"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))

    home = tmp_path / "home"
    daemon = await make_daemon(
        home,
        {"audio": {"stt": {"provider": "command", "command": "my-stt {path}"}}},
    )
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                result = await client.ok("audio.transcribe", {"path": str(clip)})
                report = await client.ok("audio.capabilities", {})
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert result == {"text": "the transcript", "provider": "command"}
    assert report["stt"] == "command"
    assert "stt" not in report["reasons"]


async def test_audio_transcribe_needs_audio(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call("audio.transcribe", {})
        finally:
            await client.stop()
    assert frame["error"]["data"]["code"] == "invalid_params"


async def test_audio_speak_uses_a_local_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "espeak-ng"
    script.write_text(
        '#!/bin/sh\nout=""\nnext=0\nfor a in "$@"; do\n'
        '  if [ "$next" = "1" ]; then out="$a"; next=0; fi\n'
        '  if [ "$a" = "-w" ]; then next=1; fi\ndone\n'
        "printf 'RIFF....WAVE' > \"$out\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))

    # Pinned, because installed is no longer the same as chosen: nothing is
    # tried on the user's behalf any more.
    daemon = await make_daemon(
        tmp_path / "home", {"audio": {"tts": {"provider": "espeak-ng"}}}
    )
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                result = await client.ok("audio.speak", {"text": "snowpea ready"})
                report = await client.ok("audio.capabilities", {})
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert result["provider"] == "espeak-ng"
    assert result["mime"] == "audio/wav"
    assert result["played"] is False
    assert Path(result["path"]).read_bytes().startswith(b"RIFF")
    assert report["tts"] is True
    assert report["ttsProvider"] == "espeak-ng"


async def test_audio_speak_can_preview_an_unpinned_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "espeak-ng"
    script.write_text(
        '#!/bin/sh\nout=""\nnext=0\nfor a in "$@"; do\n'
        '  if [ "$next" = "1" ]; then out="$a"; next=0; fi\n'
        '  if [ "$a" = "-w" ]; then next=1; fi\ndone\n'
        "printf 'RIFF....WAVE' > \"$out\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))

    daemon = await make_daemon(tmp_path / "home")
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                result = await client.ok(
                    "audio.speak",
                    {"text": "preview", "provider": "espeak-ng"},
                )
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert result["provider"] == "espeak-ng"
    assert Path(result["path"]).read_bytes().startswith(b"RIFF")


async def test_audio_speak_accepts_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "espeak-ng"
    script.write_text(
        '#!/bin/sh\nout=""\nnext=0\nfor a in "$@"; do\n'
        '  if [ "$next" = "1" ]; then out="$a"; next=0; fi\n'
        '  if [ "$a" = "-w" ]; then next=1; fi\ndone\n'
        "printf 'RIFF....WAVE' > \"$out\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))

    daemon = await make_daemon(
        tmp_path / "home", {"audio": {"tts": {"provider": "espeak-ng"}}}
    )
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                result = await client.ok(
                    "audio.speak",
                    {"text": "preview", "language": "ko"},
                )
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert result["provider"] == "espeak-ng"


async def test_audio_speak_without_a_backend_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    daemon = await make_daemon(tmp_path / "home")
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                frame = await client.call("audio.speak", {"text": "hello"})
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert frame["error"]["data"]["details"]["audio"] == "no_tts"


async def test_audio_tts_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "espeak-ng"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    daemon = await make_daemon(tmp_path / "home", {"audio": {"tts": {"enabled": False}}})
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                report = await client.ok("audio.capabilities", {})
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert report["tts"] is False
    assert "switched off" in report["reasons"]["tts"]


async def test_record_stop_without_start(daemon: Daemon) -> None:
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            frame = await client.call("audio.record.stop", {"sessionId": "nope"})
        finally:
            await client.stop()
    assert frame["error"]["data"]["details"]["audio"] == "not_recording"


async def test_record_start_without_a_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    daemon = await make_daemon(tmp_path / "home")
    try:
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                frame = await client.call("audio.record.start", {})
            finally:
                await client.stop()
    finally:
        await daemon.stop()
    assert frame["error"]["data"]["details"]["audio"] == "no_recorder"


@pytest.mark.skipif(shutil.which("rec") is None, reason="sox is not installed")
async def test_record_round_trip(daemon: Daemon) -> None:  # pragma: no cover - needs a mic
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            started = await client.ok("audio.record.start", {"sessionId": "s1"})
            assert started["recording"] is True
            stopped = await client.ok("audio.record.stop", {"sessionId": "s1"})
            assert stopped["recording"] is False
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# autoSpeak
# ---------------------------------------------------------------------------


def _voice_scripts(bin_dir: Path, played: Path) -> None:
    """A fake espeak-ng that writes a wav, and a fake player that records it."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    speak = bin_dir / "espeak-ng"
    speak.write_text(
        '#!/bin/sh\nout=""\nnext=0\nfor a in "$@"; do\n'
        '  if [ "$next" = "1" ]; then out="$a"; next=0; fi\n'
        '  if [ "$a" = "-w" ]; then next=1; fi\ndone\n'
        "printf 'RIFF....WAVE' > \"$out\"\n"
    )
    speak.chmod(speak.stat().st_mode | stat.S_IXUSR)
    player = bin_dir / "mpv"
    player.write_text(f'#!/bin/sh\necho "$@" >> "{played}"\nexit 0\n')
    player.chmod(player.stat().st_mode | stat.S_IXUSR)


async def test_auto_speak_speaks_the_reply_and_emits_audio_spoken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    played = tmp_path / "played.txt"
    _voice_scripts(tmp_path / "bin", played)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    with fake_provider(FIXTURES / "providers" / "fake" / "basic.json"):
        daemon = await make_daemon(
            tmp_path / "home", {"audio": {"tts": {"provider": "espeak-ng", "autoSpeak": True}}}
        )
        try:
            async with aiohttp.ClientSession() as http:
                client = await connect(http, daemon)
                try:
                    session_id = await _session(client, tmp_path / "work")
                    result = await client.ok(
                        "session.prompt", {"sessionId": session_id, "text": "hello"}
                    )
                    assert await client.wait_turn(result["turnId"]) == "complete"
                    spoken = client.of_kind("audio.spoken")
                finally:
                    await client.stop()
        finally:
            await daemon.stop()

    assert len(spoken) == 1
    payload = spoken[0]["payload"]
    assert payload["provider"] == "espeak-ng"
    assert payload["played"] is True
    assert Path(payload["path"]).read_bytes().startswith(b"RIFF")
    assert str(payload["path"]) in played.read_text()
    # It lands before turn.done, so a surface can show it with the reply.
    kinds = client.kinds()
    assert kinds.index("audio.spoken") < kinds.index("turn.done")


async def test_auto_speak_is_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _voice_scripts(tmp_path / "bin", tmp_path / "played.txt")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with fake_provider(FIXTURES / "providers" / "fake" / "basic.json"):
        daemon = await make_daemon(tmp_path / "home")
        try:
            async with aiohttp.ClientSession() as http:
                client = await connect(http, daemon)
                try:
                    session_id = await _session(client, tmp_path / "work")
                    result = await client.ok(
                        "session.prompt", {"sessionId": session_id, "text": "hello"}
                    )
                    assert await client.wait_turn(result["turnId"]) == "complete"
                    assert client.of_kind("audio.spoken") == []
                finally:
                    await client.stop()
        finally:
            await daemon.stop()


async def test_a_broken_voice_does_not_fail_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """autoSpeak with nothing installed: the turn still completes, silently."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    with fake_provider(FIXTURES / "providers" / "fake" / "basic.json"):
        daemon = await make_daemon(
            tmp_path / "home", {"audio": {"tts": {"autoSpeak": True}}}
        )
        try:
            async with aiohttp.ClientSession() as http:
                client = await connect(http, daemon)
                try:
                    session_id = await _session(client, tmp_path / "work")
                    result = await client.ok(
                        "session.prompt", {"sessionId": session_id, "text": "hello"}
                    )
                    assert await client.wait_turn(result["turnId"]) == "complete"
                    assert client.of_kind("audio.spoken") == []
                    assert client.of_kind("error") == []
                finally:
                    await client.stop()
        finally:
            await daemon.stop()


# ---------------------------------------------------------------------------
# protocol
# ---------------------------------------------------------------------------


async def test_audio_methods_are_implemented(daemon: Daemon) -> None:
    """Nothing under audio.* may answer ``not_implemented``."""
    from snowpea_core.server.protocol import IMPLEMENTED_METHODS, METHODS, PROTOCOL_VERSION

    audio = {name for name in METHODS if name.startswith("audio.")}
    assert audio <= IMPLEMENTED_METHODS
    assert len(audio) == 7
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            hello = await client.ok(
                "system.hello",
                {
                    "token": daemon.token,
                    "clientVersion": "test",
                    "protocolVersion": PROTOCOL_VERSION,
                },
            )
        finally:
            await client.stop()
    assert "audio" in hello["capabilities"]



async def test_session_attachments_lists_the_originals_by_turn(tmp_path: Path) -> None:
    """The user's files as attached: names, paths, the turn that brought them,
    on session.attachments and on every tool.invoke's ``attachments``."""
    with fake_provider(FIXTURES / "providers" / "fake" / "basic.json"):
        daemon = await make_daemon(tmp_path / "home")
        try:
            async with aiohttp.ClientSession() as http:
                client = await connect(http, daemon)
                try:
                    session_id = await _session(client, tmp_path / "work")
                    data = b"%PDF-1.4 a contract"
                    result = await client.ok(
                        "session.prompt",
                        {
                            "sessionId": session_id,
                            "text": "hello",
                            "attachments": [
                                {
                                    "kind": "file",
                                    "name": "../계약서.pdf",
                                    "data": base64.b64encode(data).decode(),
                                }
                            ],
                        },
                    )
                    await client.wait_turn(result["turnId"])
                    listed = await client.ok("session.attachments", {"sessionId": session_id})
                finally:
                    await client.stop()
            from snowpea_core.tools.host_tools import root_attachments

            session = daemon.core.sessions.get(session_id)
            assert root_attachments(daemon.core, session) == listed["attachments"]
        finally:
            await daemon.stop()
            pending.clear()
    [row] = listed["attachments"]
    assert row["name"] == "계약서.pdf" and row["mime"] == "application/pdf"
    assert row["messageId"] == result["turnId"] and row["size"] == len(data)
    assert row["byPath"] is False
    assert Path(row["path"]).read_bytes() == data


async def test_another_sessions_attachments_are_asked_for(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from snowpea_core.config.paths import Paths
    from snowpea_core.tools.config_guard import permission_for_read

    paths = Paths.create(tmp_path / "home")
    core = SimpleNamespace(paths=paths, sessions=SimpleNamespace(get=lambda _id: None))
    own = paths.attachments_dir / "s-mine" / "files" / "a.png"
    theirs = paths.attachments_dir / "s-theirs" / "files" / "b.png"
    session = SimpleNamespace(id="s-mine", parent_session_id=None, workdir=tmp_path, backend=None)
    assert permission_for_read({"path": str(own)}, session, core) == "read"
    assert permission_for_read({"path": str(theirs)}, session, core) == "secret"
    child = SimpleNamespace(
        id="s-child", parent_session_id="s-mine", workdir=tmp_path, backend=None, kind="subagent"
    )
    core.sessions = SimpleNamespace(get={"s-mine": session}.get)
    assert permission_for_read({"path": str(own)}, child, core) == "read"


async def test_the_session_cap_and_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.attachments import store as store_mod
    from snowpea_core.attachments.model import AttachmentError

    store = store_mod.AttachmentStore(tmp_path)
    first = store.keep_original("s-1", "a.txt", b"one", "text/plain")
    again = store.keep_original("s-1", "a.txt", b"one", "text/plain")
    other = store.keep_original("s-1", "a.txt", b"two", "text/plain")
    assert first == again and other.name == "a (2).txt"
    monkeypatch.setattr(store_mod, "SESSION_MAX_BYTES", 8)
    with pytest.raises(AttachmentError) as caught:
        store.keep_original("s-1", "big.bin", b"x" * 10, "application/octet-stream")
    assert caught.value.code == "attachment_too_large"
    assert store.purge("s-1") >= 0 and store.listing("s-1") == []


async def test_a_prompt_bigger_than_the_old_32mb_frame_goes_through(
    daemon: Daemon, tmp_path: Path
) -> None:
    """Two 14 MB files as base64 (~37 MB) used to drop the socket."""
    blobs = [base64.b64encode(bytes([n]) * (14 * 1024 * 1024)).decode() for n in (1, 2)]
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, tmp_path / "work")
            await client.ok(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "text": "two big files",
                    "attachments": [
                        {"kind": "file", "name": f"big{n}.bin", "data": blobs[n - 1]}
                        for n in (1, 2)
                    ],
                },
                timeout=60,
            )
            listed = await client.ok("session.attachments", {"sessionId": session_id})
        finally:
            await client.stop()
    assert [row["name"] for row in listed["attachments"]] == ["big1.bin", "big2.bin"]
