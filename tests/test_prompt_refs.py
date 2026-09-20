"""``@`` path references and bare image auto-attach in prompts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, fake_provider, make_daemon

from snowpea_core.agent.prompt_refs import (
    find_refs,
    prepare_prompt,
    resolve_path,
    resolve_prompt_refs,
)
from snowpea_core.attachments import pending
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.providers import content as content_parts
from snowpea_core.server.app_server import Core, Daemon
from snowpea_core.server.session_handlers import _accept_attachments
from snowpea_core.session.session import Session

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)



# ---------------------------------------------------------------------------
# grammar
# ---------------------------------------------------------------------------


def test_emails_and_escapes_are_not_refs() -> None:
    text = "mail me at user@host.com and use \\@literal"
    assert find_refs(text) == []


def test_quoted_path_with_spaces_and_range() -> None:
    refs = find_refs('see @"src/my file.py":3-5 here')
    assert len(refs) == 1
    assert refs[0].path == "src/my file.py"
    assert refs[0].line_start == 3
    assert refs[0].line_end == 5


def test_trailing_punctuation_is_not_part_of_path() -> None:
    refs = find_refs("look at @src/a.py.")
    assert refs[0].path == "src/a.py"
    assert find_refs("what is @shot.png?")[0].path == "shot.png"


def test_relative_range_ref() -> None:
    refs = find_refs("@src/a.py:10-40")
    assert refs[0].path == "src/a.py"
    assert refs[0].line_start == 10
    assert refs[0].line_end == 40


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


@pytest.fixture
def core(tmp_path: Path) -> Core:
    return Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")


@pytest.fixture
def session(tmp_path: Path) -> Session:
    workdir = tmp_path / "work"
    workdir.mkdir()
    return Session(id="s-ref", workdir=workdir, provider="openai", model="gpt-4o")


def test_text_file_is_inlined_with_line_numbers(
    core: Core, session: Session, tmp_path: Path
) -> None:
    workdir = Path(session.workdir)
    target = workdir / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    _, inline, _ = resolve_prompt_refs(core, session, "read @notes.txt")
    assert "[file: notes.txt]" in inline
    assert "1| alpha" in inline
    assert "2| beta" in inline


def test_truncation_note_names_read_file(core: Core, session: Session) -> None:
    workdir = Path(session.workdir)
    target = workdir / "big.txt"
    target.write_text("x" * 70_000, encoding="utf-8")
    _, inline, _ = resolve_prompt_refs(core, session, "@big.txt")
    assert "truncated" in inline
    assert "read_file" in inline


def test_image_becomes_attachment(core: Core, session: Session) -> None:
    workdir = Path(session.workdir)
    (workdir / "shot.png").write_bytes(PNG)
    attachments, inline, _ = resolve_prompt_refs(core, session, "what is @shot.png?")
    assert inline == ""
    assert len(attachments) == 1
    assert attachments[0].is_image


def test_directory_lists_entries(core: Core, session: Session) -> None:
    workdir = Path(session.workdir)
    (workdir / "src").mkdir()
    (workdir / "src" / "a.py").write_text("pass", encoding="utf-8")
    _, inline, _ = resolve_prompt_refs(core, session, "see @src")
    assert "[directory:" in inline
    assert "a.py" in inline


def test_unresolved_ref_is_left_alone(core: Core, session: Session) -> None:
    attachments, inline, _ = resolve_prompt_refs(core, session, "missing @nope.txt")
    assert attachments == []
    assert inline == ""


def test_bare_image_path_auto_attaches(core: Core, session: Session) -> None:
    workdir = Path(session.workdir)
    (workdir / "diagram.png").write_bytes(PNG)
    attachments, _, _ = resolve_prompt_refs(core, session, "check diagram.png please")
    assert len(attachments) == 1


def test_auto_attach_off_skips_bare_paths(core: Core, session: Session) -> None:
    core.settings.agent.autoAttachImages = False
    workdir = Path(session.workdir)
    (workdir / "diagram.png").write_bytes(PNG)
    attachments, _, _ = resolve_prompt_refs(core, session, "diagram.png")
    assert attachments == []


def test_at_ref_still_works_when_auto_attach_off(core: Core, session: Session) -> None:
    core.settings.agent.autoAttachImages = False
    workdir = Path(session.workdir)
    (workdir / "shot.png").write_bytes(PNG)
    attachments, _, _ = resolve_prompt_refs(core, session, "@shot.png")
    assert len(attachments) == 1


def test_absolute_path_outside_workdir(core: Core, session: Session, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("remote", encoding="utf-8")
    resolved = resolve_path(Path(session.workdir), str(outside))
    assert resolved == outside.resolve()


# ---------------------------------------------------------------------------
# integration: provider sees image
# ---------------------------------------------------------------------------


pytestmark_async = pytest.mark.asyncio


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    fixture = Path(__file__).parent / "fixtures" / "providers" / "fake" / "basic.json"
    with fake_provider(fixture):
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


def test_non_vision_model_gets_text_fallback(core: Core, session: Session) -> None:
    workdir = Path(session.workdir)
    (workdir / "shot.png").write_bytes(PNG)
    attachments, _, _ = resolve_prompt_refs(core, session, "@shot.png")
    assert attachments
    blocks = content_parts.history_blocks("@shot.png", attachments)
    from snowpea_core.providers.base import ChatMessage
    from snowpea_core.providers.normalize import messages_to_openai

    messages = messages_to_openai(
        [ChatMessage(role="user", content=blocks)],
        vision=False,
    )
    body = messages[0]["content"]
    assert isinstance(body, str)
    assert "cannot see images" in body or "[image attached" in body


@pytest.mark.asyncio
async def test_steered_prompt_resolves_at_ref(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "providers" / "fake" / "steer_queue.json"
    from snowpea_core.providers import fake as fake_provider_mod

    with fake_provider(fixture):
        captured: list[str] = []
        original = fake_provider_mod.FakeProvider.stream

        async def spy(self: Any, messages: list[Any], tools: list[Any], **kwargs: Any) -> Any:
            for message in messages:
                if message.role == "user":
                    captured.append(str(message.content))
            async for event in original(self, messages, tools, **kwargs):
                yield event

        monkeypatch.setattr(fake_provider_mod.FakeProvider, "stream", spy)
        workdir = tmp_path / "steer"
        workdir.mkdir()
        (workdir / "note.txt").write_text("steered-body", encoding="utf-8")
        async with aiohttp.ClientSession() as http:
            client = await connect(http, daemon)
            try:
                session_id = await _session(client, workdir)
                await client.ok("session.prompt", {"sessionId": session_id, "text": "start steer"})
                await client.wait(
                    lambda e: e["kind"] == "tool.call" and e["payload"].get("name") == "shell",
                    timeout=15.0,
                )
                await client.ok(
                    "session.prompt",
                    {"sessionId": session_id, "text": "follow @note.txt"},
                )
                await client.wait(lambda e: e["kind"] == "turn.done", timeout=15.0)
            finally:
                await client.stop()
        joined = "\n".join(captured)
        assert "steered-body" in joined or "[file:" in joined


def test_prepare_prompt_stashes_attachments(core: Core, session: Session) -> None:
    workdir = Path(session.workdir)
    (workdir / "pic.png").write_bytes(PNG)
    prepared = prepare_prompt(
        core, session, "see @pic.png", None, accept_wire=_accept_attachments
    )
    assert "@pic.png" in prepared.text
    assert pending.peek(session.id)


# ---------------------------------------------------------------------------
# corrective pass (D2–D7, D9)
# ---------------------------------------------------------------------------


def test_truncation_ends_on_a_line_boundary_and_names_the_next_line(
    core: Core, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = Path(session.workdir)
    target = workdir / "lines.txt"
    target.write_text("\n".join(f"line-{index}" for index in range(5000)), encoding="utf-8")
    _, inline, refs = resolve_prompt_refs(core, session, "@lines.txt")
    assert refs and refs[0]["truncated"] is True
    assert "truncated at line" in inline
    assert "offset=" in inline
    assert inline.rstrip()[-1].isdigit() or inline.rstrip().endswith("]")


def test_large_file_is_not_read(
    core: Core, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = Path(session.workdir)
    target = workdir / "big.iso"
    target.write_bytes(b"\x00" * 3_000_000)

    def boom(self: Path, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("read_bytes must not be called for huge non-image files")

    monkeypatch.setattr(Path, "read_bytes", boom)
    _, inline, refs = resolve_prompt_refs(core, session, "@big.iso")
    assert refs and refs[0]["kind"] == "binary"
    assert "too large to inline" in inline


def test_secret_files_are_refused(core: Core, session: Session, tmp_path: Path) -> None:
    workdir = Path(session.workdir)
    cases = [
        workdir / ".env",
        workdir / ".env.local",
        workdir / "deploy.pem",
        workdir / "server.key",
        workdir / "id_rsa",
        workdir / "id_ed25519",
        workdir / ".ssh" / "config",
    ]
    for path in cases:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret", encoding="utf-8")
        _, inline, refs = resolve_prompt_refs(core, session, f"@{path.relative_to(workdir)}")
        assert inline == ""
        assert refs and refs[0]["kind"] == "refused"


def test_gateway_scope_refuses_paths_outside_workdir(
    core: Core, session: Session, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("remote", encoding="utf-8")
    _, inline, refs = resolve_prompt_refs(
        core, session, f"@{outside}", scope="workdir"
    )
    assert inline == ""
    assert refs and refs[0]["kind"] == "refused"


@pytest.mark.parametrize(
    ("prompt", "relative"),
    [
        ("@a.py를", "a.py"),
        ("@a.py:3-5에서", "a.py"),
        ("shot.png를 봐줘", "shot.png"),
        ("docs/img/a.png 확인", "docs/img/a.png"),
        ("(see @a.py)", "a.py"),
        ('"my shot.png"', "my shot.png"),
    ],
)
def test_korean_particle_after_ref(
    core: Core, session: Session, prompt: str, relative: str
) -> None:
    workdir = Path(session.workdir)
    target = workdir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        target.write_bytes(PNG)
    else:
        target.write_text("ok", encoding="utf-8")
    attachments, inline, refs = resolve_prompt_refs(core, session, prompt)
    if target.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        assert attachments
        assert refs and refs[0]["kind"] == "image"
    else:
        assert "[file:" in inline or refs


@pytest.mark.asyncio
async def test_transcript_event_has_typed_text_only_and_refs(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.providers import fake as fake_provider_mod

    captured_events: list[dict[str, Any]] = []
    provider_bodies: list[str] = []
    original_emit = daemon.core.hub.emit_event  # type: ignore[union-attr]

    async def spy_emit(session_id: str, event: tuple[str, dict[str, Any]]) -> None:
        kind, payload = event
        if kind == "message.user":
            captured_events.append(payload)
        await original_emit(session_id, event)

    original_stream = fake_provider_mod.FakeProvider.stream

    async def spy_stream(self: Any, messages: list[Any], tools: list[Any], **kwargs: Any) -> Any:
        for message in messages:
            if message.role == "user":
                provider_bodies.append(str(message.content))
        async for event in original_stream(self, messages, tools, **kwargs):
            yield event

    monkeypatch.setattr(daemon.core.hub, "emit_event", spy_emit)  # type: ignore[union-attr]
    monkeypatch.setattr(fake_provider_mod.FakeProvider, "stream", spy_stream)
    workdir = tmp_path / "transcript"
    workdir.mkdir()
    (workdir / "note.txt").write_text("secret-body", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            await client.ok(
                "session.prompt",
                {"sessionId": session_id, "text": "read @note.txt please"},
            )
            await client.wait(lambda e: e["kind"] == "turn.done", timeout=15.0)
        finally:
            await client.stop()
    assert captured_events
    event = captured_events[0]
    assert event["text"] == "read @note.txt please"
    assert "secret-body" not in event["text"]
    assert any(ref.get("path") == "note.txt" for ref in event.get("refs", []))
    joined = "\n".join(provider_bodies)
    assert "secret-body" in joined or "[file:" in joined


@pytest.mark.asyncio
async def test_slash_command_text_is_not_expanded(
    daemon: Daemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, str]] = []

    def spy_start(core_obj: Any, session: Any, name: str, args: str, conn: Any) -> str:
        started.append((name, args))
        return "turn-cmd"

    monkeypatch.setattr(daemon.core.commands, "start", spy_start)  # type: ignore[union-attr]
    workdir = tmp_path / "slash"
    workdir.mkdir()
    notes = workdir / "notes.md"
    notes.write_text("file-body-should-not-expand", encoding="utf-8")
    async with aiohttp.ClientSession() as http:
        client = await connect(http, daemon)
        try:
            session_id = await _session(client, workdir)
            await client.ok(
                "session.prompt",
                {"sessionId": session_id, "text": "/skill create @notes.md"},
            )
        finally:
            await client.stop()
    assert started
    assert started[0][0] == "skill"
    assert started[0][1] == "create @notes.md"
    assert "file-body-should-not-expand" not in started[0][1]


def test_wire_attachment_and_ref_image_share_one_message(
    core: Core, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from snowpea_core.attachments.model import Attachment

    workdir = Path(session.workdir)
    ref_png = PNG + b"\x01"
    (workdir / "ref.png").write_bytes(ref_png)
    (workdir / "wire.png").write_bytes(PNG)
    wire = [
        type(
            "Wire",
            (),
            {
                "kind": "image",
                "name": "wire.png",
                "mimeType": "image/png",
                "path": None,
                "data": __import__("base64").b64encode(PNG).decode(),
                "text": None,
            },
        )()
    ]

    def accept_wire(
        _core: Core, session_id: str, attachments: Any
    ) -> tuple[list[Attachment], str]:
        return [Attachment.from_bytes("wire.png", PNG, "image/png")], ""

    prepared = prepare_prompt(
        core, session, "see @ref.png", wire, accept_wire=accept_wire
    )
    attachments = pending.take(session.id)
    blocks = content_parts.history_blocks(prepared.model_text, attachments)
    assert isinstance(blocks, list)
    assert sum(1 for block in blocks if block.get("type") == "image") == 2
    assert blocks[0].get("type") == "text"
    assert "@ref.png" in str(blocks[0].get("text", ""))
