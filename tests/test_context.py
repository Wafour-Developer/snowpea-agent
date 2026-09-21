"""CORE-context: context-window tracking, usage accounting and compaction.

Four layers, each tested where it actually lives:

* the window table and its overrides (:mod:`snowpea_core.providers.context_windows`),
* the prompt-size estimate and the provider-reported count that supersedes it,
* ``/compact`` and ``session.compact`` over a real daemon's WebSocket,
* auto-compaction firing — and not firing — at the configured threshold.

The local-server discovery test runs a threaded ``/v1/models`` endpoint rather
than patching httpx, so the socket, the real client and the real JSON parsing
are all in the path, matching ``tests/test_models.py``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, env_vars, make_daemon

from snowpea_core.config.settings import Settings
from snowpea_core.providers import context_windows
from snowpea_core.providers.base import ChatMessage, ToolCall
from snowpea_core.providers.presets import PRESETS
from snowpea_core.providers.registry import ProviderRegistry
from snowpea_core.server.app_server import Daemon
from snowpea_core.session import compaction
from snowpea_core.session.history import History, estimate_messages, estimate_tokens

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "context.json"
TIMEOUT = 20.0

#: Long enough that two turns of it pass half of a 2000-token window.
LONG_PROMPT = "please keep track of this requirement in detail. " * 70


@pytest.fixture(autouse=True)
def _clear_window_cache() -> Iterator[None]:
    """The discovered-window cache is process-global; no test inherits another's."""
    context_windows.cache_clear()
    yield
    context_windows.cache_clear()


# ---------------------------------------------------------------------------
# (a) the static window table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-4-5", 200_000),
        ("claude-opus-4-1", 200_000),
        ("gpt-4.1", 1_047_576),
        ("gpt-4o", 128_000),
        ("o4-mini", 200_000),
        ("gemini-2.5-pro", 1_048_576),
        ("gemini-1.5-pro", 2_097_152),
        ("deepseek-chat", 128_000),
        ("kimi-k2", 256_000),
        ("moonshot-v1-32k", 32_000),
        ("qwen3-max", 262_144),
        ("glm-4.6", 200_000),
        ("minimax-m2", 204_800),
        ("grok-4", 256_000),
        # OpenRouter ids carry a vendor prefix; the slug is what is looked up.
        ("anthropic/claude-sonnet-4.5", 200_000),
        ("openai/gpt-4.1", 1_047_576),
    ],
)
def test_static_window_table(model: str, expected: int) -> None:
    assert context_windows.static_window(model) == expected


def test_unknown_model_has_no_window() -> None:
    """An unknown id is None, never a guess — surfaces render it as '?'."""
    assert context_windows.static_window("some-model-nobody-has-heard-of") is None
    assert context_windows.static_window("") is None
    assert context_windows.static_window(None) is None


def test_every_preset_default_model_has_a_window_except_local() -> None:
    """Whatever the wizard picks by default must show a real number."""
    for vendor, preset in PRESETS.items():
        window = preset.context_window()
        if vendor == "local":
            # 'local-model' is a placeholder; only the server knows.
            assert window is None
        else:
            assert window is not None, f"{vendor}: {preset.default_model} has no window"


def test_settings_override_beats_the_table() -> None:
    settings = Settings.model_validate(
        {"providers": {"anthropic": {"context_window": 12_345, "api_key": "k"}}}
    )
    registry = ProviderRegistry(settings)
    assert registry.context_window("anthropic", "claude-sonnet-4-5") == 12_345
    # Without the override the table answers.
    assert ProviderRegistry(Settings()).context_window("anthropic", "claude-sonnet-4-5") == 200_000


def test_override_rejects_nonsense() -> None:
    """A zero, a negative or a non-number falls through to the table."""
    for bad in (0, -1, "nope", True, None):
        settings = Settings.model_validate({"providers": {"anthropic": {"context_window": bad}}})
        registry = ProviderRegistry(settings)
        assert registry.context_window("anthropic", "claude-sonnet-4-5") == 200_000


def test_unknown_vendor_has_no_window() -> None:
    assert ProviderRegistry(Settings()).context_window("not-a-vendor") is None


# ---------------------------------------------------------------------------
# (b) local discovery from /v1/models
# ---------------------------------------------------------------------------


class WindowServer:
    """A ``/v1/models`` endpoint that annotates its rows the way vLLM does."""

    def __init__(self, rows: list[dict[str, Any]], show: dict[str, Any] | None = None) -> None:
        self.rows = rows
        self.show = show
        self.paths: list[str] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        def respond(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                server.paths.append(self.path)
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                respond(self, {"object": "list", "data": server.rows})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
                server.paths.append(self.path)
                if self.path != "/api/show" or server.show is None:
                    self.send_error(404)
                    return
                length = int(self.headers.get("content-length", "0"))
                self.rfile.read(length)
                respond(self, server.show)

            def log_message(self, *args: Any) -> None:
                return

        return Handler


@pytest.fixture
def vllm_server() -> Iterator[WindowServer]:
    server = WindowServer([{"id": "Qwen/Qwen3-32B", "max_model_len": 40_960}])
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def ollama_server() -> Iterator[WindowServer]:
    """No window on the listing row; the answer is only in ``/api/show``."""
    server = WindowServer(
        [{"id": "llama3.1:8b"}],
        show={"model_info": {"general.architecture": "llama", "llama.context_length": 131_072}},
    )
    try:
        yield server
    finally:
        server.close()


@pytest.mark.asyncio
async def test_local_window_comes_from_the_models_listing(vllm_server: WindowServer) -> None:
    settings = Settings.model_validate(
        {"providers": {"local": {"base_url": vllm_server.base_url, "model": "Qwen/Qwen3-32B"}}}
    )
    registry = ProviderRegistry(settings)
    assert await registry.resolve_context_window("local") == 40_960
    assert vllm_server.paths == ["/v1/models"]


@pytest.mark.asyncio
async def test_local_window_is_cached(vllm_server: WindowServer) -> None:
    """One lookup per model per TTL: the HUD must not cost a round trip a turn."""
    settings = Settings.model_validate(
        {"providers": {"local": {"base_url": vllm_server.base_url, "model": "Qwen/Qwen3-32B"}}}
    )
    registry = ProviderRegistry(settings)
    assert await registry.resolve_context_window("local") == 40_960
    assert await registry.resolve_context_window("local") == 40_960
    assert vllm_server.paths == ["/v1/models"]
    # The synchronous lookup now answers from that cache too.
    assert registry.context_window("local") == 40_960


@pytest.mark.asyncio
async def test_local_window_falls_back_to_ollama_show(ollama_server: WindowServer) -> None:
    settings = Settings.model_validate(
        {"providers": {"local": {"base_url": ollama_server.base_url, "model": "llama3.1:8b"}}}
    )
    registry = ProviderRegistry(settings)
    assert await registry.resolve_context_window("local") == 131_072
    assert ollama_server.paths == ["/v1/models", "/api/show"]


@pytest.mark.asyncio
async def test_unreachable_local_server_yields_none() -> None:
    """A dead endpoint degrades the HUD to '?'; it must never raise."""
    settings = Settings.model_validate(
        {"providers": {"local": {"base_url": "http://127.0.0.1:1/v1", "model": "whatever"}}}
    )
    assert await ProviderRegistry(settings).resolve_context_window("local") is None


@pytest.mark.asyncio
async def test_local_override_skips_discovery(vllm_server: WindowServer) -> None:
    settings = Settings.model_validate(
        {
            "providers": {
                "local": {
                    "base_url": vllm_server.base_url,
                    "model": "Qwen/Qwen3-32B",
                    "context_window": 8192,
                }
            }
        }
    )
    assert await ProviderRegistry(settings).resolve_context_window("local") == 8192
    assert vllm_server.paths == []


def test_window_from_row_reads_the_usual_spellings() -> None:
    assert context_windows.window_from_row({"max_model_len": 4096}) == 4096
    assert context_windows.window_from_row({"context_length": "8192"}) == 8192
    assert context_windows.window_from_row({"meta": {"context_window": 2048}}) == 2048
    assert context_windows.window_from_row({"id": "x"}) is None


# ---------------------------------------------------------------------------
# (c) the estimate and the provider-reported count
# ---------------------------------------------------------------------------


def test_estimate_is_proportional_to_length() -> None:
    assert estimate_tokens("") == 0
    short = estimate_tokens("a" * 400)
    long = estimate_tokens("a" * 4000)
    assert short > 0
    assert 8 <= long / short <= 12


def test_estimate_counts_tool_calls_and_results() -> None:
    """A tool call's arguments and its result both cost tokens."""
    plain = [ChatMessage(role="assistant", content="ok")]
    with_call = [
        ChatMessage(
            role="assistant",
            content="ok",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "x" * 400})],
        ),
        ChatMessage(role="tool", content="y" * 400, tool_call_id="c1", name="read_file"),
    ]
    assert estimate_messages(with_call) > estimate_messages(plain) + 150


def test_history_estimate_grows_with_the_conversation() -> None:
    history = History()
    assert history.estimate_tokens() == 0
    history.append(ChatMessage(role="user", content="x" * 4000))
    first = history.estimate_tokens()
    history.append(ChatMessage(role="assistant", content="y" * 4000))
    assert history.estimate_tokens() > first >= 1000


def test_provider_reported_usage_supersedes_the_estimate() -> None:
    """Once the vendor says what it billed, that is the number surfaces show."""

    class Stub:
        context_used = 0
        context_estimated = True

    session = Stub()
    compaction.record_provider_usage(session, 4321)  # type: ignore[arg-type]
    assert session.context_used == 4321
    assert session.context_estimated is False
    # A zero or negative report is meaningless and is ignored.
    compaction.record_provider_usage(session, 0)  # type: ignore[arg-type]
    assert session.context_used == 4321


def test_split_index_never_orphans_a_tool_result() -> None:
    messages = [
        ChatMessage(role="user", content="do it"),
        ChatMessage(
            role="assistant", content="", tool_calls=[ToolCall(id="c1", name="shell", arguments={})]
        ),
        ChatMessage(role="tool", content="out", tool_call_id="c1", name="shell"),
        ChatMessage(role="assistant", content="done"),
    ]
    # Keeping the last two would start the tail on the tool result; the
    # boundary moves forward instead.
    assert compaction.split_index(messages, 2) == 3
    assert messages[compaction.split_index(messages, 2)].role == "assistant"


# ---------------------------------------------------------------------------
# (d) the wire: context events, /compact and session.compact
# ---------------------------------------------------------------------------


def _settings(**context: Any) -> dict[str, Any]:
    """Daemon settings with a small, known window for the ``fake`` vendor."""
    return {
        "providers": {"fake": {"context_window": 2000}},
        "context": {"autoCompactPercent": 50, "keepLastMessages": 2, **context},
    }


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with env_vars(SNOWPEA_PROVIDER=f"fake:{FIXTURE}"):
        instance = await make_daemon(tmp_path / "home", _settings(autoCompact=False))
        try:
            yield instance
        finally:
            await instance.stop()


@pytest_asyncio.fixture
async def auto_daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with env_vars(SNOWPEA_PROVIDER=f"fake:{FIXTURE}"):
        instance = await make_daemon(tmp_path / "auto-home", _settings())
        try:
            yield instance
        finally:
            await instance.stop()


async def start_session(client: RpcClient, workdir: Path) -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": "accept"})
    return str(result["sessionId"])


async def prompt(client: RpcClient, session_id: str, text: str) -> str:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    turn_id = str(result["turnId"])
    await client.wait_turn(turn_id, TIMEOUT)
    return turn_id


@pytest.mark.asyncio
async def test_context_event_follows_every_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        await prompt(client, session_id, "hello there")
        event = await client.wait(lambda e: e["kind"] == "context", TIMEOUT)
        payload = event["payload"]
        assert payload["used"] > 0
        assert payload["window"] == 2000
        assert payload["percent"] == pytest.approx(payload["used"] * 100 / 2000, abs=0.2)
        # The scripted provider reports usage, so the count is not an estimate.
        assert payload["estimated"] is False
        # The vendor reported is the one actually in use, not the session's
        # (unset) pin.  The scripted 'fake' vendor genuinely has no model id.
        assert payload["provider"] == "fake"
        # …and it arrives just before turn.done, so a consumer that stops
        # reading on turn.done (the headless CLI does) still sees it.
        kinds = client.kinds()
        assert kinds.index("context") < kinds.index("turn.done")
        assert kinds[kinds.index("context") + 1] == "turn.done"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_unknown_window_is_reported_as_null(
    tmp_path: Path, http: aiohttp.ClientSession
) -> None:
    """No window configured for the vendor: the event says null, not a guess."""
    with env_vars(SNOWPEA_PROVIDER=f"fake:{FIXTURE}"):
        instance = await make_daemon(tmp_path / "home-nowindow")
        try:
            client = await connect(http, instance, timeout=TIMEOUT)
            try:
                session_id = await start_session(client, tmp_path)
                await prompt(client, session_id, "hello there")
                event = await client.wait(lambda e: e["kind"] == "context", TIMEOUT)
                assert event["payload"]["window"] is None
                assert event["payload"]["percent"] is None
            finally:
                await client.stop()
        finally:
            await instance.stop()


@pytest.mark.asyncio
async def test_session_list_reports_context(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        await prompt(client, session_id, "hello there")
        await client.wait(lambda e: e["kind"] == "context", TIMEOUT)
        listing = await client.ok("session.list")
        row = next(r for r in listing["sessions"] if r["sessionId"] == session_id)
        assert row["contextUsed"] > 0
        assert row["contextWindow"] == 2000
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_compact_command_shrinks_the_history(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        for _ in range(3):
            await prompt(client, session_id, LONG_PROMPT)
        session = daemon.core.sessions.get(session_id)  # type: ignore[union-attr]
        assert session is not None
        before_messages = len(session.history)
        before_tokens = session.history.estimate_tokens()
        assert before_messages == 6

        turn_id = str(
            (await client.ok("session.prompt", {"sessionId": session_id, "text": "/compact"}))[
                "turnId"
            ]
        )
        assert await client.wait_turn(turn_id, TIMEOUT) == "complete"

        assert len(session.history) < before_messages
        assert session.history.estimate_tokens() < before_tokens
        # Summary first, then the messages kept verbatim.
        assert session.history.messages[0].role == "system"
        assert compaction.SUMMARY_HEADING in str(session.history.messages[0].content)

        events = client.of_kind("compaction")
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["before"] > payload["after"] > 0
        assert payload["summaryChars"] > 0
        assert payload["auto"] is False
        assert payload["kept"] == 2

        # The summary is also published as a system message…
        system_messages = [
            e for e in client.of_kind("message.done") if e["payload"].get("role") == "system"
        ]
        assert system_messages
        assert compaction.SUMMARY_HEADING in system_messages[-1]["payload"]["text"]
        # …and a fresh context reading follows it.
        kinds = client.kinds()
        assert kinds.index("context", kinds.index("compaction")) > kinds.index("compaction")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_compact_rpc_matches_the_command(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        for _ in range(3):
            await prompt(client, session_id, LONG_PROMPT)
        result = await client.ok(
            "session.compact",
            {"sessionId": session_id, "instructions": "keep the file paths"},
            timeout=TIMEOUT,
        )
        assert result["before"] > result["after"] > 0
        assert result["summaryChars"] > 0
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_compact_rpc_refuses_mid_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """Replacing the history under a running tool loop would strand a call."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        await client.ok("session.prompt", {"sessionId": session_id, "text": LONG_PROMPT})
        frame = await client.call("session.compact", {"sessionId": session_id})
        assert frame.get("error"), frame
        assert "in flight" in frame["error"]["message"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_compact_on_a_short_conversation_changes_nothing(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        turn_id = str(
            (await client.ok("session.prompt", {"sessionId": session_id, "text": "/compact"}))[
                "turnId"
            ]
        )
        assert await client.wait_turn(turn_id, TIMEOUT) == "complete"
        assert client.of_kind("compaction") == []
        assert "Nothing to compact" in client.of_kind("message.done")[-1]["payload"]["text"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_resume_replays_the_compaction_marker(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """A reconnecting client can draw the '— compacted (…) —' divider."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        for _ in range(3):
            await prompt(client, session_id, LONG_PROMPT)
        await client.ok("session.compact", {"sessionId": session_id}, timeout=TIMEOUT)
    finally:
        await client.stop()

    fresh = await connect(http, daemon, timeout=TIMEOUT)
    try:
        replay = await fresh.ok("session.resume", {"sessionId": session_id, "afterSeq": 0})
        kinds = [event["kind"] for event in replay["events"]]
        assert "compaction" in kinds
        assert "context" in kinds
        marker = next(e for e in replay["events"] if e["kind"] == "compaction")
        assert marker["payload"]["before"] > marker["payload"]["after"] > 0
    finally:
        await fresh.stop()


# ---------------------------------------------------------------------------
# (e) auto-compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_compaction_fires_at_the_threshold(
    auto_daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    client = await connect(http, auto_daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        for _ in range(4):
            await prompt(client, session_id, LONG_PROMPT)
        events = client.of_kind("compaction")
        assert events, f"no auto-compaction; saw {client.kinds()}"
        assert events[0]["payload"]["auto"] is True
        assert events[0]["payload"]["before"] > events[0]["payload"]["after"] > 0
        # It happened between turns, never inside one: no tool.call sits
        # between the compaction and the turn.done that precedes it.
        kinds = client.kinds()
        index = kinds.index("compaction")
        # The announcement (IDE-PROGRESS D3) sits immediately before the
        # completion, and the turn boundary immediately before that.
        assert kinds[index - 1] == "compaction.started"
        assert kinds[index - 2] in ("context", "turn.done", "turn.started")
        started = client.of_kind("compaction.started")
        assert started and started[0]["payload"]["reason"] == "auto"
        assert started[0]["payload"]["before"] == events[0]["payload"]["before"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_auto_compaction_can_be_disabled(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The ``daemon`` fixture sets ``context.autoCompact: false``."""
    client = await connect(http, daemon, timeout=TIMEOUT)
    try:
        session_id = await start_session(client, tmp_path)
        for _ in range(4):
            await prompt(client, session_id, LONG_PROMPT)
        assert client.of_kind("compaction") == []
        # The window is still tracked, and it really is over the threshold.
        last = client.of_kind("context")[-1]["payload"]
        assert last["percent"] is not None and last["percent"] >= 50
    finally:
        await client.stop()


def test_resolved_identity_names_what_is_actually_in_use() -> None:
    """A session that pinned nothing still runs against something."""

    class CoreStub:
        def __init__(self) -> None:
            self.providers = ProviderRegistry(
                Settings.model_validate({"providers": {"default": "anthropic"}})
            )

    class SessionStub:
        provider = None
        model = None

    vendor, model = compaction.resolved_identity(CoreStub(), SessionStub())  # type: ignore[arg-type]
    assert vendor == "anthropic"
    assert model == "claude-sonnet-5"


def test_should_auto_compact_respects_the_settings() -> None:
    """The predicate on its own: threshold, the off switch, an unknown window."""

    class Stub:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

    class SessionStub:
        def __init__(self, count: int) -> None:
            self.history = History(messages=[ChatMessage(role="user", content="x")] * count)

    core = Stub(Settings.model_validate({"context": {"autoCompactPercent": 80}}))
    session = SessionStub(10)
    over = compaction.ContextState(used=900, window=1000)
    under = compaction.ContextState(used=500, window=1000)
    unknown = compaction.ContextState(used=900, window=None)

    assert compaction.should_auto_compact(core, session, over) is True  # type: ignore[arg-type]
    assert compaction.should_auto_compact(core, session, under) is False  # type: ignore[arg-type]
    assert compaction.should_auto_compact(core, session, unknown) is False  # type: ignore[arg-type]

    off = Stub(Settings.model_validate({"context": {"autoCompact": False}}))
    assert compaction.should_auto_compact(off, session, over) is False  # type: ignore[arg-type]

    # A conversation no longer than what compaction would keep is left alone.
    assert compaction.should_auto_compact(core, SessionStub(2), over) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_known_family_served_larger_is_discovered_not_tabled() -> None:
    """``qwen`` is 131k in the static table; a vLLM node serving it at 256k
    must be believed, so the ``context`` event asks the server even though
    the table already has an answer (it only stops asking once cached)."""
    from types import SimpleNamespace

    from snowpea_core.session import compaction

    server = WindowServer([{"id": "qwen38-flash-next", "max_model_len": 262_144}])
    try:
        settings = Settings.model_validate(
            {"providers": {"local": {"base_url": server.base_url, "model": "qwen38-flash-next"}}}
        )
        registry = ProviderRegistry(settings)
        assert registry.context_window("local") == 131_072  # the table, before asking
        core = SimpleNamespace(providers=registry)
        session = SimpleNamespace(provider="local", model="qwen38-flash-next")
        assert compaction.window_is_cold(core, session) is True
        assert await compaction.resolve_window(core, session) == 262_144
        assert compaction.window_is_cold(core, session) is False
        assert registry.context_window("local") == 262_144
    finally:
        server.close()


@pytest.mark.asyncio
async def test_a_discovered_window_never_decays_to_the_table_default(
    vllm_server: WindowServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long session: the cache entry expires, the turn-ending event may not await.

    ``Qwen/Qwen3-32B`` is a family the static table knows (131k); this server
    serves it at 40 960. After the TTL the synchronous lookup used to fall back
    to the table, so the HUD jumped to 131k and auto-compaction used the wrong
    size. What the server said has to survive the TTL.
    """
    from snowpea_core.providers import context_windows

    settings = Settings.model_validate(
        {"providers": {"local": {"base_url": vllm_server.base_url, "model": "Qwen/Qwen3-32B"}}}
    )
    registry = ProviderRegistry(settings)
    assert await registry.resolve_context_window("local") == 40_960

    # Ten minutes later: every entry is stale.
    real = context_windows.time.monotonic
    monkeypatch.setattr(
        context_windows.time, "monotonic", lambda: real() + context_windows.CACHE_TTL_SEC + 1
    )
    assert registry.context_window("local") == 40_960  # not the table's 131 072

    # The server is unreachable when asked again: the known answer is kept, and
    # the failure is remembered only briefly so it is retried soon.
    async def unreachable(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(context_windows, "discover_window", unreachable)
    assert await registry.resolve_context_window("local") == 40_960
    assert registry.context_window("local") == 40_960
    assert context_windows.NEGATIVE_TTL_SEC < context_windows.CACHE_TTL_SEC


@pytest.mark.asyncio
async def test_a_model_never_seen_still_falls_back_to_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from snowpea_core.providers import context_windows

    async def unreachable(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(context_windows, "discover_window", unreachable)
    settings = Settings.model_validate(
        {"providers": {"local": {"base_url": "http://127.0.0.1:9/v1", "model": "Qwen/Qwen3-32B"}}}
    )
    registry = ProviderRegistry(settings)
    assert await registry.resolve_context_window("local") == 131_072
