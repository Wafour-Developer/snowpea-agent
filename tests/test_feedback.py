"""개선하기 — ``feedback.preview`` / ``feedback.send`` (protocol 1.59.0, capability
``improvementFeedback``).

The report is built from a session's stored events (the same mapping as
snowpea-browser's ``eventsToMessages``), masked with the browser's rules,
fitted to the server's limits and POSTed gzip to ``SNOWPEA_FEEDBACK_URL``.
Every upload here goes to a fake server on 127.0.0.1; nothing leaves the
machine.  Secret-looking strings below are made-up test data.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from test_host_tools import Daemon, daemon, open_client  # noqa: F401

from snowpea_core.feedback import known_secrets
from snowpea_core.feedback.redact import FIELD_CAP, REDACTED, redact_messages, redact_string
from snowpea_core.feedback.report import (
    MAX_MESSAGES,
    ReportError,
    check_attachments,
    events_to_messages,
    fit_report,
)
from snowpea_core.feedback.sender import EndpointError, feedback_endpoint, install_id
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.feedback_handlers import feedback_preview_handler, feedback_send_handler
from snowpea_core.server.person_gate import PERSON_METHODS
from snowpea_core.server.protocol import (
    CAPABILITIES,
    IMPLEMENTED_METHODS,
    PROTOCOL_VERSION,
    FeedbackPreviewParams,
    FeedbackSendParams,
)
from snowpea_core.session.store import Store

SID = "s_feedback"
TOKEN = "daemon-token-0123456789abcdef"
PROVIDER_KEY = "plainprovidervalue42"


def ev(seq: int, kind: str, payload: dict[str, Any], ts: str | None = None) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "ts": ts or f"2026-11-18T00:00:{seq:02d}.000Z",
    }


CHAT = [
    ev(1, "turn.started", {}),
    ev(
        2,
        "message.user",
        {
            "text": "fix the build",
            "attachments": [{"path": "/home/me/shot.png", "url": "file:///home/me/shot.png"}],
        },
    ),
    ev(3, "message.reasoning", {"text": "think "}),
    ev(4, "message.reasoning", {"text": "more"}),
    ev(5, "message.delta", {"text": "Look"}),
    ev(6, "tool.call", {"callId": "c1", "name": "shell", "args": {"command": "make"}}),
    ev(7, "tool.progress", {"callId": "c1", "chunk": "x"}),
    ev(
        8,
        "tool.result",
        {
            "callId": "c1",
            "name": "shell",
            "ok": False,
            "output": "boom",
            "error": "exit 2",
            "content": [{"type": "image", "mediaType": "image/png", "ref": "blob:1"}],
        },
        "2026-11-18T00:00:09.500Z",
    ),
    ev(9, "message.delta", {"text": "Done."}),
    ev(10, "message.done", {"role": "assistant", "text": "Done.", "messageKind": "plan"}),
    ev(11, "error", {"code": "internal", "message": "internal error (id abc123)"}),
    ev(12, "turn.done", {"reason": "interrupted"}),
    ev(13, "turn.done", {"reason": "complete"}),
    ev(14, "mode.changed", {"mode": "auto"}),
    ev(15, "loop.suspected", {"tool": "shell"}),
]


# ---------------------------------------------------------------- mapping


def test_events_map_to_browser_shaped_messages() -> None:
    messages = events_to_messages(CHAT)
    by_id = {m["id"]: m for m in messages}
    assert [m["id"] for m in messages] == [
        "m_2",
        "m_5",
        "m_6",
        "m_9",
        "m_11",
        "m_12",
        "m_14",
        "m_15",
    ]
    user = by_id["m_2"]
    assert user["role"] == "user" and user["text"] == "fix the build"
    # Names only: an absolute local path is its basename.
    assert user["meta"]["attachments"] == [{"path": "shot.png", "url": "shot.png"}]
    # Reasoning and streamed text flushed by the tool call: the delta's seq.
    assert by_id["m_5"] == {
        "id": "m_5",
        "seq": 5,
        "role": "assistant",
        "ts": CHAT[5]["ts"],
        "text": "Look",
        "reasoning": "think more",
    }
    tool = by_id["m_6"]["tool"]
    assert tool["name"] == "shell" and tool["callId"] == "c1" and tool["status"] == "error"
    assert tool["args"] == {"command": "make"}
    assert tool["result"] == {
        "output": "boom",
        "error": "exit 2",
        "content": [{"type": "image", "mediaType": "image/png"}],
    }
    assert tool["durationMs"] == 3500
    assert by_id["m_9"]["text"] == "Done." and by_id["m_9"]["meta"] == {"messageKind": "plan"}
    assert by_id["m_11"]["role"] == "event" and by_id["m_11"]["meta"]["errorId"] == "abc123"
    assert by_id["m_12"]["text"] == "turn ended: interrupted"
    assert by_id["m_14"]["meta"] == {"kind": "mode.changed", "mode": "auto"}
    assert by_id["m_15"]["meta"] == {"kind": "loop.suspected", "payload": {"tool": "shell"}}


def test_reasoning_only_and_orphan_results_and_unique_ids() -> None:
    messages = events_to_messages(
        [
            ev(1, "message.reasoning", {"text": "hmm"}),
            ev(2, "tool.result", {"callId": "zz", "name": "read", "ok": True, "output": "x"}),
            ev(3, "tool.call", {"callId": "c", "name": "a", "args": {}}),
            ev(4, "tool.call", {"callId": "c", "name": "a", "args": {}}),
        ]
    )
    assert messages[0]["meta"] == {"reasoningOnly": True} and messages[0]["reasoning"] == "hmm"
    assert messages[1]["tool"] == {
        "name": "read",
        "callId": "zz",
        "status": "ok",
        "result": {"output": "x"},
    }
    assert len(messages) == 3
    assert len({m["id"] for m in messages}) == len(messages)


# ---------------------------------------------------------------- redaction (the browser's cases)


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678"),
        ("curl -H 'X-Api-Key: zzzzyyyyxxxx1234'", "zzzzyyyyxxxx1234"),
        ("key sk-ant-api03-ABCDEFGHIJKLMNOPQRST", "sk-ant-api03-ABCDEFGHIJKLMNOPQRST"),
        ("AKIAABCDEFGHIJKLMNOP is the id", "AKIAABCDEFGHIJKLMNOP"),
        ("aws_secret_access_key = abcdEFGH1234ijklMNOP5678", "abcdEFGH1234ijklMNOP5678"),
        ("ghp_" + "a" * 36, "ghp_" + "a" * 36),
        ("AIza" + "B" * 35, "AIza" + "B" * 35),
        ("xoxb-1234567890-abcdef", "xoxb-1234567890-abcdef"),
        ("eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl", "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"),
        ("password=hunter2!", "hunter2!"),
        ('{"api_key": "s3cr3tvalue"}', "s3cr3tvalue"),
        ("https://x.test/cb?code=abc123&state=ok", "abc123"),
        ("https://s3.test/o?X-Amz-Signature=deadbeef", "deadbeef"),
        ("sid=abcdefghijkl; theme=dark", "abcdefghijkl"),
        ('{"set-cookie": "sid=1"}', "sid=1"),
        ("card 4111 1111 1111 1111 here", "4111 1111 1111 1111"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----", "MIIE"),
    ],
)
def test_redact_string_masks_like_the_browser(text: str, gone: str) -> None:
    out = redact_string(text)
    assert gone not in out
    assert REDACTED in out


@pytest.mark.parametrize(
    "text",
    [
        "password = os.environ['PW']",
        "token: ${TOKEN}",
        "the password is short",
        "order 1234 5678 9012 3456 7",  # not Luhn-valid
        "theme=dark; lang=ko",
    ],
)
def test_redact_string_leaves_references_and_prose(text: str) -> None:
    assert redact_string(text) == text


def test_daemon_token_and_provider_keys_are_masked_verbatim() -> None:
    out = redact_string(f"token in a log line: {TOKEN} and {PROVIDER_KEY}", [TOKEN, PROVIDER_KEY])
    assert TOKEN not in out and PROVIDER_KEY not in out


def test_long_strings_and_data_urls_are_cut() -> None:
    assert redact_string("a" * (FIELD_CAP + 10)).endswith("[truncated: 10 more characters]")
    url = "data:image/png;base64," + "A" * 3000
    assert redact_string(url) == f"data:image/png;[{len(url)} chars omitted]"
    small = "data:image/png;base64,AAAA"
    assert redact_string(small) == small


def test_secret_keys_tuples_cookie_jars_sensitive_and_vault() -> None:
    messages = [
        {
            "id": "m_1",
            "role": "tool",
            "tool": {
                "name": "fetch",
                "args": {
                    "headers": {"Authorization": "x", "cookie": ["a"]},
                    "raw": ["set-cookie", "sid=zz", "accept", "json"],
                    "pair": {"name": "Set-Cookie", "value": "sid=1"},
                    "c": {"name": "sid", "value": "v", "domain": "x.test"},
                    "none": None,
                },
                "result": {"output": "ok"},
            },
        },
        {
            "id": "m_2",
            "role": "tool",
            "tool": {"name": "read", "result": {"output": "pw", "meta": {"sensitive": True}}},
        },
        {
            "id": "m_3",
            "role": "tool",
            "tool": {
                "name": "repl",
                "args": {"code": "await vault.fill('x')"},
                "result": {"output": "pw"},
            },
        },
    ]
    out, stats = redact_messages(messages, [])
    args = out[0]["tool"]["args"]
    assert args["headers"] == {"Authorization": REDACTED, "cookie": REDACTED}
    assert args["raw"] == ["set-cookie", REDACTED, "accept", "json"]
    assert args["pair"]["value"] == REDACTED and args["c"]["value"] == REDACTED
    assert "none" not in args
    assert out[1]["tool"]["result"] == {
        "withheld": "sensitive result (vault, password or a protected field): not sent"
    }
    assert out[2]["tool"]["result"]["withheld"]
    assert stats.withheld == 2


def test_known_secrets_reads_settings_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_TEST_API_KEY", "envkeyvalue0123456789")
    settings = SimpleNamespace(
        model_dump=lambda: {"providers": {"x": {"api_key": PROVIDER_KEY, "model": "m"}}}
    )
    found = known_secrets(SimpleNamespace(token=TOKEN, settings=settings))
    assert TOKEN in found and PROVIDER_KEY in found and "envkeyvalue0123456789" in found
    assert "m" not in found


# ---------------------------------------------------------------- limits


def test_fit_report_drops_largest_results_first_and_keeps_flags() -> None:
    big = "x" * 200_000
    messages = [
        {
            "id": f"m_{i}",
            "role": "tool",
            "tool": {"name": "t", "args": {}, "result": {"output": big + str(i)}},
        }
        for i in range(3)
    ]
    report = {"comment": "", "flagged": [{"messageId": "m_0"}], "session": {"messages": messages}}
    raw, body, dropped = fit_report(report, max_compressed=10_000_000, max_raw=400_000)
    assert len(raw) <= 400_000 and dropped >= 1
    assert gzip.decompress(body) == raw
    assert any("dropped" in m["tool"]["result"] for m in report["session"]["messages"])


def test_fit_report_keeps_head_and_tail_of_a_very_long_chat() -> None:
    messages = [{"id": f"m_{i}", "role": "user", "text": "hi"} for i in range(MAX_MESSAGES + 5)]
    report = {
        "comment": "",
        "flagged": [{"messageId": "m_10"}, {"messageId": f"m_{MAX_MESSAGES // 2 + 2}"}],
        "session": {"messages": messages},
    }
    fit_report(report)
    kept = report["session"]["messages"]
    assert len(kept) == MAX_MESSAGES
    assert any(m["id"] == "m_gap" for m in kept)
    assert report["flagged"] == [{"messageId": "m_10"}]


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 10).decode()


def test_attachment_rules() -> None:
    check_attachments([{"name": "a.png", "mime": "image/png", "bytesBase64": PNG}])
    with pytest.raises(ReportError):
        check_attachments(
            [{"name": "a.png", "mime": "image/jpeg", "bytesBase64": PNG}]
        )  # wrong signature
    with pytest.raises(ReportError):
        check_attachments([{"name": "a.svg", "mime": "image/svg+xml", "bytesBase64": PNG}])
    with pytest.raises(ReportError):
        check_attachments(
            [{"name": "a.png", "mime": "image/png", "bytesBase64": "data:image/png;base64," + PNG}]
        )
    with pytest.raises(ReportError):
        check_attachments([{"name": "a.png", "mime": "image/png", "bytesBase64": PNG}] * 11)
    huge = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (5 * 1024 * 1024)).decode()
    with pytest.raises(ReportError):
        check_attachments([{"name": "a.png", "mime": "image/png", "bytesBase64": huge}])


def test_param_limits() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FeedbackPreviewParams(sessionId=SID, comment="x" * 20001)
    with pytest.raises(ValidationError):
        FeedbackPreviewParams(sessionId=SID, flagged=[{"messageId": "m_1", "note": "x" * 4001}])
    with pytest.raises(ValidationError):
        FeedbackPreviewParams(sessionId=SID, flagged=[{"messageId": "m_1"}] * 1001)
    with pytest.raises(ValidationError):
        FeedbackSendParams(sessionId=SID, surface="browser")


# ---------------------------------------------------------------- endpoint and install id


def test_endpoint_override_is_https_or_loopback_only() -> None:
    assert feedback_endpoint({}) == "https://browser.snowpea.ai/api/feedback"
    assert feedback_endpoint(
        {"SNOWPEA_FEEDBACK_URL": "http://127.0.0.1:9/api/feedback"}
    ).startswith("http://127.0.0.1")
    assert feedback_endpoint({"SNOWPEA_FEEDBACK_URL": "https://staging.test/api/feedback"})
    for bad in ("http://example.test/api", "ftp://127.0.0.1/x", "http://10.0.0.1/x"):
        with pytest.raises(EndpointError):
            feedback_endpoint({"SNOWPEA_FEEDBACK_URL": bad})


def test_install_id_is_kept_0600(tmp_path: Path) -> None:
    first = install_id(tmp_path)
    path = tmp_path / "feedback-install-id"
    assert path.read_text().strip() == first
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert install_id(tmp_path) == first
    path.write_text("not a uuid\n")
    again = install_id(tmp_path)
    assert again != first and len(again) == 36


# ------------------------------------------------- handlers against a fake core and server


class _Sessions:
    def get(self, session_id: str) -> Any:
        return None


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[Any]:
    store = Store(tmp_path / "store.db")
    await store.insert_session(
        SID, str(tmp_path), "accept", "fake", "fake-model", "tui", "2026-11-18T00:00:00Z"
    )
    for e in CHAT:
        await store.append_event(SID, e["seq"], e["kind"], e["payload"], e["ts"])
    await store.append_event(
        SID,
        20,
        "message.user",
        {"text": f"my key is {PROVIDER_KEY}, token {TOKEN}"},
        "2026-11-18T00:01:00Z",
    )
    settings = SimpleNamespace(
        model_dump=lambda: {"providers": {"fake": {"api_key": PROVIDER_KEY}}}
    )
    yield SimpleNamespace(
        sessions=_Sessions(),
        store=store,
        settings=settings,
        token=TOKEN,
        paths=SimpleNamespace(home=tmp_path / "home"),
    )
    store.close()


def person(kind: str | None = "tui", *, remote: bool = False) -> Any:
    return SimpleNamespace(
        authenticated=True,
        closed=False,
        remote_login=remote,
        client_kind=kind,
        client_id=None,
        surface_id=None,
        client_version=None,
    )


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answer: tuple[int, dict[str, Any], dict[str, str]] = (
            201,
            {"id": "a" * 24, "receivedAt": "2026-11-18T01:00:00Z"},
            {},
        )
        self.url = ""

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        self.requests.append({"headers": dict(request.headers), "body": body})
        status, payload, headers = self.answer
        return web.json_response(payload, status=status, headers=headers)


@pytest.fixture
async def server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeServer]:
    fake = FakeServer()
    app = web.Application()
    app.router.add_post("/api/feedback", fake.handle)
    # The body as it came over the wire (aiohttp would otherwise gunzip it).
    runner = web.AppRunner(app, auto_decompress=False)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    fake.url = f"http://127.0.0.1:{port}/api/feedback"
    monkeypatch.setenv("SNOWPEA_FEEDBACK_URL", fake.url)
    try:
        yield fake
    finally:
        await runner.cleanup()


async def test_preview_is_the_masked_report_and_sends_nothing(
    core: Any, server: FakeServer
) -> None:
    result = await feedback_preview_handler(
        person(),
        FeedbackPreviewParams(
            sessionId=SID, comment="  slow  ", flagged=[{"messageId": "m_6", "note": "wrong tool"}]
        ),
        core,
    )
    report = result.report
    assert server.requests == []
    assert result.endpoint == server.url
    assert report["schema"] == 1 and report["kind"] == "improvement"
    assert report["client"]["app"] == "snowpea-core" and report["client"]["surface"] == "tui"
    assert report["client"]["protocolVersion"] == PROTOCOL_VERSION
    assert (
        report["client"]["model"] == "fake-model" and report["client"]["permissionMode"] == "accept"
    )
    assert report["comment"] == "slow"
    assert report["flagged"] == [{"messageId": "m_6", "note": "wrong tool"}]
    text = json.dumps(report)
    assert PROVIDER_KEY not in text and TOKEN not in text
    assert result.stats.messages == len(report["session"]["messages"])
    assert result.stats.toolMessages == 1 and result.stats.redactions >= 1
    assert result.stats.rawBytes == len(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode()
    )


async def test_unknown_flag_missing_session_and_empty_session(
    core: Any, server: FakeServer
) -> None:
    with pytest.raises(RpcError) as exc:
        await feedback_preview_handler(
            person(), FeedbackPreviewParams(sessionId=SID, flagged=[{"messageId": "m_999"}]), core
        )
    assert exc.value.code == errors.INVALID_PARAMS and exc.value.data == {"field": "flagged"}
    with pytest.raises(RpcError) as exc:
        await feedback_preview_handler(person(), FeedbackPreviewParams(sessionId="nope"), core)
    assert exc.value.code == errors.NOT_FOUND
    await core.store.insert_session(
        "empty", "/w", "accept", None, None, "tui", "2026-11-18T00:00:00Z"
    )
    with pytest.raises(RpcError) as exc:
        await feedback_preview_handler(person(), FeedbackPreviewParams(sessionId="empty"), core)
    assert exc.value.code == errors.INVALID_PARAMS


async def test_send_posts_gzip_json_with_the_surface(core: Any, server: FakeServer) -> None:
    result = await feedback_send_handler(
        person(),
        FeedbackSendParams(
            sessionId=SID,
            surface="tui",
            surfaceVersion="0.2.91",
            attachments=[{"name": "a.png", "mime": "image/png", "bytesBase64": PNG}],
        ),
        core,
    )
    assert result.id == "a" * 24 and result.receivedAt == "2026-11-18T01:00:00Z"
    [request] = server.requests
    assert request["headers"]["Content-Encoding"] == "gzip"
    assert request["headers"]["Content-Type"].startswith("application/json")
    body = json.loads(gzip.decompress(request["body"]))
    assert body["client"]["surface"] == "tui" and body["client"]["surfaceVersion"] == "0.2.91"
    assert body["attachments"][0]["mime"] == "image/png"
    assert body["installId"] == (core.paths.home / "feedback-install-id").read_text().strip()
    assert PROVIDER_KEY not in json.dumps(body) and TOKEN not in json.dumps(body)


@pytest.mark.parametrize(
    ("status", "headers", "code", "retry"),
    [
        (400, {}, errors.FEEDBACK_REJECTED, None),
        (413, {}, errors.FEEDBACK_REJECTED, None),
        (415, {}, errors.FEEDBACK_REJECTED, None),
        (429, {"Retry-After": "120"}, errors.FEEDBACK_BUSY, 120),
        (503, {"Retry-After": "7"}, errors.FEEDBACK_BUSY, 7),
        (500, {}, errors.FEEDBACK_UNREACHABLE, None),
    ],
)
async def test_server_errors_map_and_are_not_retried(
    core: Any,
    server: FakeServer,
    status: int,
    headers: dict[str, str],
    code: str,
    retry: int | None,
) -> None:
    server.answer = (
        status,
        {"error": "invalid", "message": "session.messages[0].role: bad"},
        headers,
    )
    with pytest.raises(RpcError) as exc:
        await feedback_send_handler(
            person(), FeedbackSendParams(sessionId=SID, surface="tui"), core
        )
    assert exc.value.code == code
    assert exc.value.data["status"] == status
    assert exc.value.data.get("retryAfterSec") == retry
    assert exc.value.data["serverError"] == "invalid"
    assert exc.value.data["serverMessage"] == "session.messages[0].role: bad"
    assert len(server.requests) == 1  # one attempt, no retry


async def test_unreachable_server(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("SNOWPEA_FEEDBACK_URL", f"http://127.0.0.1:{port}/api/feedback")
    with pytest.raises(RpcError) as exc:
        await feedback_send_handler(
            person(), FeedbackSendParams(sessionId=SID, surface="tui"), core
        )
    assert exc.value.code == errors.FEEDBACK_UNREACHABLE and exc.value.data["status"] == 0


async def test_a_bad_endpoint_is_unavailable(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNOWPEA_FEEDBACK_URL", "http://example.test/api/feedback")
    with pytest.raises(RpcError) as exc:
        await feedback_preview_handler(person(), FeedbackPreviewParams(sessionId=SID), core)
    assert exc.value.code == errors.UNAVAILABLE


# ---------------------------------------------------------------- gate


@pytest.mark.parametrize(
    "conn",
    [
        person("browser"),
        person("connector"),
        person("jarvis"),
        None,  # Jarvis, a messenger, an agent's in-process call
        SimpleNamespace(authenticated=True, closed=True, remote_login=False, client_kind="tui"),
    ],
)
async def test_only_a_person_may_preview_or_send(core: Any, server: FakeServer, conn: Any) -> None:
    for call in (
        feedback_preview_handler(conn, FeedbackPreviewParams(sessionId=SID), core),
        feedback_send_handler(conn, FeedbackSendParams(sessionId=SID, surface="tui"), core),
    ):
        with pytest.raises(RpcError) as exc:
            await call
        assert exc.value.code == errors.JARVIS_PERSON_ONLY
    assert server.requests == []


async def test_a_remote_web_login_is_a_person_here(core: Any, server: FakeServer) -> None:
    result = await feedback_preview_handler(
        person("web", remote=True), FeedbackPreviewParams(sessionId=SID), core
    )
    assert result.report["client"]["surface"] == "web"


def test_contract_registration() -> None:
    assert PROTOCOL_VERSION == "1.67.0"
    assert "improvementFeedback" in CAPABILITIES


def test_hello_hides_feedback_until_the_server_takes_core_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from snowpea_core.feedback import sender
    from snowpea_core.server.transport_ws import _advertised_capabilities

    monkeypatch.delenv(sender.ENV_URL, raising=False)
    monkeypatch.setattr(sender, "SERVER_LIVE", False)
    assert "improvementFeedback" not in _advertised_capabilities()
    monkeypatch.setenv(sender.ENV_URL, "http://127.0.0.1:9/api/feedback")
    assert "improvementFeedback" in _advertised_capabilities()
    monkeypatch.delenv(sender.ENV_URL)
    monkeypatch.setattr(sender, "SERVER_LIVE", True)
    assert "improvementFeedback" in _advertised_capabilities()
    assert {"feedback.preview", "feedback.send"} <= IMPLEMENTED_METHODS
    assert PERSON_METHODS["feedback.preview"] is False and PERSON_METHODS["feedback.send"] is False
    for code in (errors.FEEDBACK_REJECTED, errors.FEEDBACK_BUSY, errors.FEEDBACK_UNREACHABLE):
        assert code in errors.ERROR_CODES


def test_never_a_tool() -> None:
    # No tool, Jarvis or agent code reaches the sender or the handlers.
    root = Path(__file__).resolve().parents[1] / "core" / "snowpea_core"
    for folder in ("tools", "jarvis", "agent", "gateway", "scheduler"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in (
                "feedback.send",
                "feedback.preview",
                "snowpea_core.feedback",
                "feedback_handlers",
            ):
                assert needle not in text, (path, needle)


async def test_over_the_wire_a_browser_is_refused_and_a_person_previews(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SNOWPEA_FEEDBACK_URL", "http://127.0.0.1:9/api/feedback")
    browser = await open_client(http, daemon, client_id="snowpea-browser-A")
    ide = await open_client(http, daemon, kind="ide")
    try:
        workdir = tmp_path / "w"
        workdir.mkdir()
        created = await ide.ok("session.create", {"workdir": str(workdir)})
        sid = created["sessionId"]
        await daemon.core.store.append_event(
            sid, 9000, "message.user", {"text": "hello"}, "2026-11-18T00:00:00Z"
        )
        frame = await browser.call("feedback.preview", {"sessionId": sid})
        assert frame["error"]["data"]["code"] == errors.JARVIS_PERSON_ONLY
        preview = await ide.ok("feedback.preview", {"sessionId": sid})
        assert preview["endpoint"] == "http://127.0.0.1:9/api/feedback"
        assert preview["report"]["client"]["surface"] == "ide"
        assert any(m["text"] == "hello" for m in preview["report"]["session"]["messages"])
    finally:
        await browser.stop()
        await ide.stop()
