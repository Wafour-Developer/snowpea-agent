"""개선하기 against the feedback server's own rules (M32).

A fake server on 127.0.0.1 checks every upload the way snowpea-browser's
``website/server/src/upload.mjs`` and ``validate.mjs`` do (schema 1,
``client.app`` ``snowpea-core`` with a surface) and answers like it: 201 with
an id, or the server's ``{"error", "message"}`` with its status.  The tests
show the report core builds passes those rules, and that a refusal, a server
error and a server that never answers each reach the client as an error —
never as a sent report.  Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from test_feedback import PNG, SID, core, person  # noqa: F401
from test_host_tools import Daemon, daemon, open_client  # noqa: F401

from snowpea_core.feedback import sender
from snowpea_core.feedback.sender import UploadError, post_feedback
from snowpea_core.server import errors
from snowpea_core.server.errors import RpcError
from snowpea_core.server.feedback_handlers import feedback_send_handler
from snowpea_core.server.protocol import FeedbackSendParams

# ------------------------------------------------- the server's rules (validate.mjs)

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})$")
APPS = ("snowpea-browser", "snowpea-core")
SURFACES = ("desktop", "tui", "web", "ide", "cli", "messenger", "other")
ROLES = ("user", "assistant", "tool", "system", "event")
MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")


class Invalid(Exception):
    pass


def _string(v: Any, path: str, lo: int = 0, hi: int = 10**9) -> None:
    if not isinstance(v, str):
        raise Invalid(f"{path}: must be a string")
    if not lo <= len(v) <= hi:
        raise Invalid(f"{path}: length out of range")


def _opt(obj: dict[str, Any], key: str, path: str, hi: int = 10**9) -> None:
    if key in obj:
        _string(obj[key], f"{path}.{key}", 0, hi)


def _iso(v: Any, path: str) -> None:
    _string(v, path, 0, 40)
    if not ISO.match(v):
        raise Invalid(f"{path}: must be an ISO 8601 date-time")


def _timestamp(v: Any, path: str) -> None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if v < 0:
            raise Invalid(f"{path}: must be epoch milliseconds or an ISO date-time")
    else:
        _iso(v, path)


def validate_report(r: Any) -> None:
    """Python twin of ``validateReport`` in snowpea-browser website/server/src/validate.mjs."""
    if not isinstance(r, dict):
        raise Invalid("body: must be an object")
    if r.get("schema") != 1:
        raise Invalid("schema: must be 1")
    if r.get("kind") != "improvement":
        raise Invalid('kind: must be "improvement"')
    _string(r.get("installId"), "installId")
    if not UUID.match(r["installId"]):
        raise Invalid("installId: must be a UUID")
    _iso(r.get("createdAt"), "createdAt")
    _string(r.get("comment"), "comment", 0, 20_000)

    c = r.get("client")
    if not isinstance(c, dict):
        raise Invalid("client: must be an object")
    if c.get("app") not in APPS:
        raise Invalid("client.app: must be one of " + ", ".join(APPS))
    if c["app"] == "snowpea-core":
        if c.get("surface") not in SURFACES:
            raise Invalid("client.surface: must be one of " + ", ".join(SURFACES))
    elif "surface" in c and c["surface"] != "browser":
        raise Invalid('client.surface: must be "browser" for snowpea-browser')
    _opt(c, "surfaceVersion", "client", 64)
    for key in ("version", "os", "arch", "coreVersion"):
        _string(c.get(key), f"client.{key}", 1, 64)
    pv = c.get("protocolVersion")
    if not (isinstance(pv, str) and 1 <= len(pv) <= 64) and not (
        isinstance(pv, int) and not isinstance(pv, bool)
    ):
        raise Invalid("client.protocolVersion: must be a string or an integer")
    _opt(c, "model", "client", 200)
    _opt(c, "permissionMode", "client", 64)
    _opt(c, "locale", "client", 35)

    s = r.get("session")
    if not isinstance(s, dict):
        raise Invalid("session: must be an object")
    _string(s.get("id"), "session.id", 1, 200)
    _opt(s, "title", "session", 1000)
    if "createdAt" in s:
        _timestamp(s["createdAt"], "session.createdAt")
    messages = s.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 20_000:
        raise Invalid("session.messages: must have 1..20000 items")
    ids: set[str] = set()
    for i, m in enumerate(messages):
        path = f"session.messages[{i}]"
        if not isinstance(m, dict):
            raise Invalid(f"{path}: must be an object")
        _string(m.get("id"), f"{path}.id", 1, 200)
        if m["id"] in ("__proto__", "constructor", "prototype") or m["id"] in ids:
            raise Invalid(f"{path}.id: reserved or duplicate")
        ids.add(m["id"])
        if "seq" in m and (not isinstance(m["seq"], int) or isinstance(m["seq"], bool)):
            raise Invalid(f"{path}.seq: must be an integer")
        if m.get("role") not in ROLES:
            raise Invalid(f"{path}.role: must be one of " + ", ".join(ROLES))
        if "ts" in m:
            _timestamp(m["ts"], f"{path}.ts")
        _opt(m, "text", path)
        _opt(m, "reasoning", path)
        if "tool" in m:
            t = m["tool"]
            if not isinstance(t, dict):
                raise Invalid(f"{path}.tool: must be an object")
            _string(t.get("name"), f"{path}.tool.name", 1, 200)
            _opt(t, "callId", f"{path}.tool", 200)
            _opt(t, "status", f"{path}.tool", 64)
            if "durationMs" in t and not (
                isinstance(t["durationMs"], (int, float)) and t["durationMs"] >= 0
            ):
                raise Invalid(f"{path}.tool.durationMs: must be a number >= 0")
        if "meta" in m and not isinstance(m["meta"], dict):
            raise Invalid(f"{path}.meta: must be an object")

    flagged = r.get("flagged")
    if not isinstance(flagged, list) or len(flagged) > 1000:
        raise Invalid("flagged: must be an array of at most 1000")
    for i, f in enumerate(flagged):
        if not isinstance(f, dict):
            raise Invalid(f"flagged[{i}]: must be an object")
        _string(f.get("messageId"), f"flagged[{i}].messageId", 1, 200)
        if f["messageId"] not in ids:
            raise Invalid(f"flagged[{i}].messageId: is not the id of a message")
        _opt(f, "note", f"flagged[{i}]", 4000)
    for i, a in enumerate(r.get("attachments", [])):
        _string(a.get("name"), f"attachments[{i}].name", 1, 255)
        if a.get("mime") not in MIMES:
            raise Invalid(f"attachments[{i}].mime: not allowed")
        _string(a.get("bytesBase64"), f"attachments[{i}].bytesBase64", 1)


class SchemaServer:
    """Answers like upload.mjs: 415 / 400 / 201, or a forced answer, or never."""

    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.force: tuple[int, str, str] | None = None  # status, content type, body
        self.hang = False
        self.url = ""

    async def handle(self, request: web.Request) -> web.StreamResponse:
        raw_body = await request.read()
        if self.hang:
            await asyncio.sleep(3600)
        if self.force is not None:
            status, ctype, text = self.force
            return web.Response(status=status, text=text, content_type=ctype)
        ctype = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
        encoding = request.headers.get("Content-Encoding", "").strip().lower()
        if ctype != "application/json" or encoding != "gzip":
            return self.error(415, "unsupported_media_type", "Send gzip JSON")
        try:
            report = json.loads(gzip.decompress(raw_body).decode("utf-8"))
        except (OSError, ValueError):
            return self.error(400, "invalid", "Body is not valid gzip JSON")
        try:
            validate_report(report)
        except Invalid as exc:
            return self.error(400, "invalid", str(exc))
        self.reports.append(report)
        return web.json_response(
            {"id": f"{len(self.reports):024x}", "receivedAt": "2026-11-20T00:00:00.000Z"},
            status=201,
        )

    @staticmethod
    def error(status: int, code: str, message: str) -> web.Response:
        return web.json_response({"error": code, "message": message}, status=status)


@pytest.fixture
async def schema_server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SchemaServer]:
    fake = SchemaServer()
    app = web.Application()
    app.router.add_post("/api/feedback", fake.handle)
    runner = web.AppRunner(app, auto_decompress=False, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    fake.url = f"http://127.0.0.1:{port}/api/feedback"
    monkeypatch.setenv(sender.ENV_URL, fake.url)
    try:
        yield fake
    finally:
        fake.hang = False
        await runner.cleanup()


# ------------------------------------------------------------------ the tests


@pytest.mark.parametrize("surface", ["tui", "ide", "web", "desktop", "cli"])
async def test_the_report_passes_the_servers_rules(
    core: Any,  # noqa: F811
    schema_server: SchemaServer,
    surface: str,
) -> None:
    result = await feedback_send_handler(
        person(surface),
        FeedbackSendParams(
            sessionId=SID,
            surface=surface,
            surfaceVersion="0.2.101",
            comment="slow tool",
            flagged=[{"messageId": "m_6", "note": "wrong tool"}],
            attachments=[{"name": "a.png", "mime": "image/png", "bytesBase64": PNG}],
        ),
        core,
    )
    [report] = schema_server.reports
    assert result.id == f"{1:024x}"
    assert report["client"]["app"] == "snowpea-core" and report["client"]["surface"] == surface
    assert report["flagged"] == [{"messageId": "m_6", "note": "wrong tool"}]


@pytest.mark.parametrize(
    ("status", "ctype", "text", "code"),
    [
        (
            400,
            "application/json",
            '{"error":"invalid","message":"client.app: bad"}',
            errors.FEEDBACK_REJECTED,
        ),
        (413, "text/html", "<html>413 Request Entity Too Large</html>", errors.FEEDBACK_REJECTED),
        (
            500,
            "application/json",
            '{"error":"internal","message":"Internal error"}',
            errors.FEEDBACK_UNREACHABLE,
        ),
        (502, "text/html", "<html>Bad Gateway</html>", errors.FEEDBACK_UNREACHABLE),
        (200, "application/json", "{}", errors.FEEDBACK_UNREACHABLE),
    ],
)
async def test_a_server_error_is_an_error_not_a_sent_report(
    core: Any,  # noqa: F811
    schema_server: SchemaServer,
    status: int,
    ctype: str,
    text: str,
    code: str,
) -> None:
    schema_server.force = (status, ctype, text)
    with pytest.raises(RpcError) as exc:
        await feedback_send_handler(
            person(), FeedbackSendParams(sessionId=SID, surface="tui"), core
        )
    assert exc.value.code == code and exc.value.data["status"] == status
    assert "not sent" in str(exc.value)


async def test_a_server_that_never_answers_times_out(
    core: Any,  # noqa: F811
    schema_server: SchemaServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_server.hang = True
    with pytest.raises(UploadError) as raw:
        await post_feedback(schema_server.url, gzip.compress(b"{}"), user_agent="t", idle_sec=0.3)
    assert raw.value.kind == "unreachable" and raw.value.status == 0
    # Through the handler: the same idle limit, mapped to feedback_unreachable.
    monkeypatch.setattr(sender, "post_feedback", functools.partial(post_feedback, idle_sec=0.3))
    with pytest.raises(RpcError) as exc:
        await feedback_send_handler(
            person(), FeedbackSendParams(sessionId=SID, surface="tui"), core
        )
    assert exc.value.code == errors.FEEDBACK_UNREACHABLE and exc.value.data["status"] == 0
    assert schema_server.reports == []


async def test_over_the_wire_the_client_gets_the_id_or_the_error(
    http: aiohttp.ClientSession,
    daemon: Daemon,  # noqa: F811
    schema_server: SchemaServer,
    tmp_path: Path,
) -> None:
    ide = await open_client(http, daemon, kind="ide")
    try:
        workdir = tmp_path / "w"
        workdir.mkdir()
        sid = (await ide.ok("session.create", {"workdir": str(workdir)}))["sessionId"]
        await daemon.core.store.append_event(
            sid, 9000, "message.user", {"text": "hello"}, "2026-11-20T00:00:00.000Z"
        )
        sent = await ide.ok("feedback.send", {"sessionId": sid, "surface": "desktop"})
        assert sent["id"] == f"{1:024x}" and sent["receivedAt"]
        assert schema_server.reports[0]["client"]["surface"] == "desktop"

        schema_server.force = (
            400,
            "application/json",
            '{"error":"invalid","message":"session.messages[0].ts: bad"}',
        )
        frame = await ide.call("feedback.send", {"sessionId": sid, "surface": "desktop"})
        assert "result" not in frame
        data = frame["error"]["data"]
        assert data["code"] == errors.FEEDBACK_REJECTED
        assert data["details"]["status"] == 400
        assert data["details"]["serverMessage"] == "session.messages[0].ts: bad"
        assert len(schema_server.reports) == 1
    finally:
        await ide.stop()
