"""Protocol 1.55.0, group 2: the core side of "the browser agent flounders".

a. ``tool.invoke`` carries ``model {canSee, contextWindow, localStyle}``.
b. Unknown / unavailable tools answer with instructions: host ``helpers``, an
   agent definition's ``unavailable`` map, the nearest names.
c. ``{{deferred_tools_note}}`` is the session's real deferred state; base.md and
   the pruned tool_search stub stop telling the model to search again.
d. Skill auto-inject matches whole words (Korean particles allowed) with caps.
e. Host tool manuals: ``descriptionShort``, ``sections``, ``tool_help``,
   ``meta.helpSection`` appended once per session.
f. ``tool.progress`` keeps a call alive and forwards ``state``/``note``.
g. ``llm.sample`` images and ``modelCategory: "vision"``; image views marked as
   a tab's snapshot are superseded like text snapshots.
h. ``context.autoCompactAt`` and ``compaction(.started).reason``.

No real model, browser or shell runs: hosts and providers are fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.agent import agent as agent_mod
from snowpea_core.agent import loop
from snowpea_core.agent.definition import parse_agent_text, render_agent_md
from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ChatMessage, ToolCall, ToolSpec
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import (
    CompactionEvent,
    CompactionStarted,
    ContextEvent,
    LlmSampleParams,
    ToolInvokeRequest,
    ToolProgress,
)
from snowpea_core.session import compaction, events
from snowpea_core.session.session import Session
from snowpea_core.skills.auto_inject import keyword_matches, usable
from snowpea_core.tools import deferred, host_tools, tool_hints, tool_search
from snowpea_core.tools.host_tools import HOST_TOOLS, model_facts
from snowpea_core.tools.registry import ToolContext, ToolResult

PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _Conn:
    def __init__(self, surface: str = "surface-b") -> None:
        self.surface_id = surface
        self.client_id = "browser-1"
        self.client_kind = "browser"
        self.closed = False
        self.on_close: list[Any] = []
        self.answers: list[dict[str, Any]] = []
        self.invoked: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
        self.invoked.append(params)
        return self.answers.pop(0) if self.answers else {"ok": True, "output": "done"}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        return None


@pytest.fixture
def core(tmp_path: Path) -> Core:
    from snowpea_core.tools.registry import register_builtin_tools

    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    register_builtin_tools(made.tools.bind(made.settings))
    return made


@pytest.fixture
def host(core: Core, tmp_path: Path) -> Iterator[SimpleNamespace]:
    conn = _Conn()
    HOST_TOOLS.register(
        core,
        conn,
        [
            {
                "name": "repl",
                "description": "LONG MANUAL " * 200,
                "descriptionShort": "Run page code.",
                "sections": {"clicking": "Use click(ref).", "waiting": "Use waitFor()."},
                "helpers": ["snapshot", "click"],
                "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}},
                "permission": "read",
            },
            {
                "name": "browser_navigate",
                "description": "Go to a URL.",
                "inputSchema": {"type": "object", "properties": {}},
                "permission": "read",
            },
        ],
    )
    (tmp_path / "proj").mkdir(exist_ok=True)
    session = Session(id="s-b", workdir=tmp_path / "proj", mode="auto")
    session.host_tools_from = conn.surface_id
    try:
        yield SimpleNamespace(conn=conn, session=session)
    finally:
        HOST_TOOLS.drop_connection(conn)
        HOST_TOOLS._gone.clear()


# -- a. tool.invoke model -------------------------------------------------------------


def test_model_facts_for_a_local_server() -> None:
    providers = SimpleNamespace(
        default_vendor=lambda: "llamacpp",
        is_local_style=lambda vendor: vendor == "llamacpp",
        vision_for=lambda vendor, model: None,  # unknown: a local server may see
    )
    session = SimpleNamespace(provider="llamacpp", model="qwen", context_window=32768)
    facts = model_facts(SimpleNamespace(providers=providers), session)
    assert facts == {"canSee": True, "contextWindow": 32768, "localStyle": True}
    blind = SimpleNamespace(
        default_vendor=lambda: "x", is_local_style=lambda v: False,
        vision_for=lambda v, m: False,
    )
    session = SimpleNamespace(provider="x", model="m", context_window=None)
    assert model_facts(SimpleNamespace(providers=blind), session) == {
        "canSee": False, "contextWindow": None, "localStyle": False,
    }


async def test_tool_invoke_carries_the_model(core: Core, host: Any) -> None:
    ctx = ToolContext(session=host.session, core=core, backend=None, call_id="c-1")  # type: ignore[arg-type]
    tool = core.tools.get("browser_navigate", host.session)
    assert tool is not None
    result = await tool.run(ctx, {})
    assert result.ok
    params = host.conn.invoked[-1]
    assert set(params["model"]) == {"canSee", "contextWindow", "localStyle"}
    ToolInvokeRequest(**params)


# -- b. answers that say where to look ----------------------------------------------------


class _Allow(PermissionPolicy):
    def decide(self, *_a: Any, **_k: Any) -> Any:
        return "allow"


async def _call(core: Core, session: Session, name: str) -> str:
    call = ToolCall(id=f"c-{name}", name=name, arguments={})
    await _run_one_call(core, session, LocalBackend(session.workdir), _Allow(), call, "t", False)
    return str(session.history.snapshot()[-1].content)


async def test_a_helper_called_as_a_tool_is_told_where_it_lives(core: Core, host: Any) -> None:
    text = await _call(core, host.session, "snapshot")
    assert text == "snapshot is not a tool: it is a helper inside repl; call it in that tool's code"


async def test_tool_search_for_a_helper_and_for_nothing(core: Core, host: Any) -> None:
    ctx = ToolContext(session=host.session, core=core, backend=None, call_id="c")  # type: ignore[arg-type]
    out = (await tool_search.tool_search(ctx, {"query": "select:click"})).output
    assert "click is not a tool: it is a helper inside repl" in out
    out = (await tool_search.tool_search(ctx, {"query": "select:browser_navigat"})).output
    assert out.startswith("No tool matched")
    assert "Nearest tools: browser_navigate" in out


def test_unavailable_frontmatter_block_and_inline_and_roundtrip() -> None:
    text = (
        "---\nname: browser\ndescription: drives pages\n"
        "unavailable:\n"
        '  mcp: "MCP tools are not available to this agent; call request_code_mode"\n'
        '  "git_*": no git here\n'
        "---\n\nYou browse.\n"
    )
    defn = parse_agent_text(text)
    assert defn.unavailable == {
        "mcp": "MCP tools are not available to this agent; call request_code_mode",
        "git_*": "no git here",
    }
    assert parse_agent_text(render_agent_md(defn)).unavailable == defn.unavailable
    inline = parse_agent_text('---\nname: a\nunavailable: {"mcp": "none here"}\n---\nx\n')
    assert inline.unavailable == {"mcp": "none here"}
    assert parse_agent_text("---\nname: b\n---\nx\n").unavailable == {}


def test_unavailable_matching_rules() -> None:
    session = SimpleNamespace(agent_unavailable={"mcp": "No MCP.", "git_*": "No git."})
    assert tool_hints.unavailable_note(session, "mcp__github__search") == "No MCP."
    assert tool_hints.unavailable_note(session, "git_commit") == "No git."
    assert tool_hints.unavailable_note(session, "mcpx") is None
    assert tool_hints.unavailable_note(session, query="mcp github") == "No MCP."
    assert tool_hints.unavailable_note(session, query="select:git_diff") == "No git."
    assert tool_hints.unavailable_note(SimpleNamespace(), "mcp__a") is None


async def test_unknown_tool_and_narrowed_refusal_carry_the_agent_hint(
    core: Core, tmp_path: Path
) -> None:
    session = Session(id="s-u", workdir=tmp_path, mode="auto")
    session.agent_unavailable = {"mcp": "MCP tools are not available to this agent."}
    text = await _call(core, session, "mcp__github__search")
    assert text.startswith("unknown tool: mcp__github__search")
    assert text.endswith("MCP tools are not available to this agent.")
    session.allowed_tools = {"read_file"}
    text = await _call(core, session, "shell")
    assert "shell is not available to this agent; its tools are:" in text
    session.agent_unavailable = {"shell": "Ask the coding agent to run commands."}
    text = await _call(core, session, "shell")
    assert text.endswith("Ask the coding agent to run commands.")


def test_nearest_names() -> None:
    names = ["browser_click", "browser_navigate", "read_file", "web_search"]
    assert tool_hints.nearest("click", names) == ["browser_click"]
    assert tool_hints.nearest("browser_navgate", names)[0] == "browser_navigate"
    assert tool_hints.nearest("zzz", names) == []
    assert len(tool_hints.nearest("browser", [f"browser_{i}" for i in range(9)])) == 5


async def test_narrowed_refusal_names_only_tools_the_session_can_call(
    core: Core, tmp_path: Path
) -> None:
    """Browser report: ``unknown tool: request_code_mode``.  The browser agent's
    list names host tools; with no host providing them (closed or older browser)
    the refusal must not advertise them, or the model calls them next."""
    session = Session(id="s-n", workdir=tmp_path, mode="auto")
    session.agent = "browser"
    session.allowed_tools = {"read_file", "repl", "request_code_mode"}
    text = await _call(core, session, "shell")
    assert "shell is not available to this agent; its tools are:" in text
    assert "read_file" in text
    assert "request_code_mode" not in text and "repl" not in text


# -- c. truthful deferral text --------------------------------------------------------------


def _spec(name: str, category: str = "git") -> ToolSpec:
    return ToolSpec(name=name, description=name, input_schema={}, category=category)


def test_deferred_note_is_empty_without_deferred_tools() -> None:
    assert deferred.deferred_note([]) == ""
    text = deferred.deferred_note([_spec("git_diff"), _spec("git_log")], fixed=True)
    assert "git: git_diff, git_log" in text and "not added to your tool list" in text
    assert "join your tool list" in deferred.deferred_note([_spec("git_diff")])


def test_the_agent_prompt_variable_renders_the_real_state(core: Core, tmp_path: Path) -> None:
    session = Session(id="s-p", workdir=tmp_path, mode="accept")
    session.system_prompt = "Brief.\n{{deferred_tools_note}}\nEnd."
    prompt = agent_mod.build_system_prompt(session, core.tools.specs(session), core=core)
    assert "{{deferred_tools_note}}" not in prompt
    assert "Some of your tools are named but not loaded" in prompt
    core.settings.tools.deferred = False  # nothing deferred: the note is empty
    session.prompt_plan = None
    agent_mod.invalidate_tools(session)
    prompt = agent_mod.build_system_prompt(session, core.tools.specs(session), core=core)
    assert "Brief.\n\nEnd." in prompt and "named but not loaded" not in prompt


def test_base_prompt_no_longer_orders_a_browser_search() -> None:
    base = (Path(agent_mod.__file__).parent.parent / "prompts" / "base.md").read_text("utf-8")
    assert "call tool_search with" not in base
    assert "with tool_search if not in your tool list" in base


def test_pruned_tool_search_stub_does_not_say_rerun() -> None:
    stub = compaction._tool_output_stub("tool_search", 900)
    assert "still yours" in stub and "re-run" not in stub
    assert "re-run" in compaction._tool_output_stub("read_file", 900)


# -- d. skill auto-inject -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keyword", "text", "hit"),
    [
        ("글", "글로벌 서비스 만들어줘", False),  # one CJK syllable never matches
        ("블로그", "블로그 글을 써줘", True),
        ("블로그", "블로그를 정리해줘", True),  # particle
        ("깃허브", "깃허브에서 이슈 찾아줘", True),
        ("메일", "메일로 보내줘", True),
        ("메일", "이메일 주소", False),  # inside a word
        ("구글", "구글링 해봐", False),  # not a particle
        ("Google", "search google for it", True),
        ("go", "google it", False),
        ("pull request", "open a Pull  Request now", True),
        ("gmail", "my gmail.", True),
        ("gmail", "gmailbox", False),
    ],
)
def test_keyword_rule(keyword: str, text: str, hit: bool) -> None:
    assert keyword_matches(keyword, text) is hit


def test_single_cjk_keyword_is_unusable() -> None:
    assert not usable("글") and usable("글쓰기") and usable("g")


def _skill(name: str, words: list[str], body: str) -> Any:
    doc = SimpleNamespace(name=name, auto_inject=words, body=body)
    return SimpleNamespace(doc=doc)


async def test_auto_inject_caps_and_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills = {
        f"s{i}": _skill(f"s{i}", ["deploy"], f"body {i}") for i in range(4)
    }
    core = SimpleNamespace(skills=SimpleNamespace(skills=skills))
    session = Session(id="s-i", workdir=tmp_path)
    first = await loop._auto_inject_skills(core, session, "please deploy it")  # type: ignore[arg-type]
    assert first == ["s0", "s1"]  # AUTO_INJECT_MAX_PER_TURN
    note = str(session.history.snapshot()[0].content)
    assert note.startswith("[skill s0 applies here (auto-injected: your message contains “deploy”)")
    second = await loop._auto_inject_skills(core, session, "deploy again")  # type: ignore[arg-type]
    assert second == ["s2", "s3"]  # left out by the cap, not lost
    monkeypatch.setattr(loop, "AUTO_INJECT_SESSION_BYTES", session.auto_injected_bytes)
    skills["late"] = _skill("late", ["deploy"], "x")
    assert await loop._auto_inject_skills(core, session, "deploy") == []  # type: ignore[arg-type]


# -- e. host tool manuals -------------------------------------------------------------------


def test_short_description_and_tool_help_in_the_session(core: Core, host: Any) -> None:
    repl = core.tools.get("repl", host.session)
    assert repl is not None and repl.description.startswith("Run page code.")
    assert "sections: clicking, waiting" in repl.description
    assert "LONG MANUAL" not in repl.description
    assert "tool_help" in HOST_TOOLS.names_for(host.session)
    assert "tool_help" in deferred.eager_names(host.session)


async def test_tool_help_answers(core: Core, host: Any) -> None:
    ctx = ToolContext(session=host.session, core=core, backend=None, call_id="c")  # type: ignore[arg-type]
    listed = await host_tools.tool_help(ctx, {"tool": "repl"})
    assert "- clicking: Use click(ref)." in listed.output
    one = await host_tools.tool_help(ctx, {"tool": "repl", "section": "waiting"})
    assert one.output.endswith("Use waitFor().")
    missing = await host_tools.tool_help(ctx, {"tool": "repl", "section": "x"})
    assert not missing.ok and "clicking, waiting" in (missing.error or "")
    plain = await host_tools.tool_help(ctx, {"tool": "browser_navigate"})
    assert plain.output == "Go to a URL."


async def test_help_section_is_appended_once_per_session(core: Core, host: Any) -> None:
    ctx = ToolContext(session=host.session, core=core, backend=None, call_id="c-h")  # type: ignore[arg-type]
    repl = core.tools.get("repl", host.session)
    failed = {"ok": False, "error": "no such ref", "meta": {"helpSection": "clicking"}}
    host.conn.answers = [dict(failed), dict(failed)]
    first = await repl.run(ctx, {"code": "click(9)"})  # type: ignore[union-attr]
    assert "[repl manual, section 'clicking']\nUse click(ref)." in (first.error or "")
    second = await repl.run(ctx, {"code": "click(9)"})  # type: ignore[union-attr]
    assert second.error == "no such ref"


def test_bad_sections_are_refused(core: Core) -> None:
    conn = _Conn("surface-bad")
    with pytest.raises(host_tools.HostToolError):
        HOST_TOOLS.register(core, conn, [{
            "name": "x", "description": "x", "permission": "read", "sections": {"": "a"},
        }])


# -- f. long host waits ---------------------------------------------------------------------


class _Slow(_Conn):
    def __init__(self, delay: float) -> None:
        super().__init__("surface-slow2")
        self.delay = delay

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None):
        await asyncio.sleep(self.delay)
        return {"ok": True, "output": "done"}


def _slow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delay: float) -> tuple[Any, ...]:
    monkeypatch.setattr(host_tools, "DEADLINE_POLL_SEC", 0.02)
    tools = host_tools.HostTools()
    conn = _Slow(delay)
    tools._hosts[conn.surface_id] = host_tools._Host(conn=conn, tools={"repl": object()})
    emitted: list[Any] = []

    async def emit(_sid: str, event: Any) -> None:
        emitted.append(event)

    core = SimpleNamespace(hub=SimpleNamespace(emit_event=emit), approvals=None, questions=None)
    session = Session(id="s-slow", workdir=tmp_path)
    ctx = ToolContext(session=session, core=core, backend=None, call_id="c-slow")  # type: ignore[arg-type]
    return tools, conn, core, ctx, emitted


async def test_waiting_state_is_forwarded_and_keeps_the_call_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, conn, core, ctx, emitted = _slow(monkeypatch, tmp_path, delay=0.6)

    async def waiting() -> None:
        await asyncio.sleep(0.05)
        await tools.progress(conn, "c-slow", "", state="waiting", note="waiting for the page")
        for _ in range(10):
            await asyncio.sleep(0.05)
            await tools.progress(conn, "c-slow", "")

    pinger = asyncio.ensure_future(waiting())
    result = await tools.invoke(core, conn.surface_id, "repl", 0.2, ctx, {})  # 0.2 s < 0.6 s
    await pinger
    assert result.ok
    (kind, payload), = emitted
    assert kind == "tool.progress"
    assert payload["state"] == "waiting" and payload["note"] == "waiting for the page"
    ToolProgress(kind="tool.progress", **payload)
    plain = events.tool_progress("c", "repl", chunk="x")[1]
    assert "state" not in plain and "note" not in plain


async def test_a_pending_question_stops_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, conn, core, ctx, _ = _slow(monkeypatch, tmp_path, delay=0.5)
    core.questions = SimpleNamespace(count=lambda session_id: 1)
    result = await tools.invoke(core, conn.surface_id, "repl", 0.1, ctx, {})
    assert result.ok and result.output == "done"


# -- g. llm.sample images, snapshot views -------------------------------------------------


def test_sample_messages_carry_the_images_after_the_page() -> None:
    from snowpea_core.server.llm_handlers import _messages

    params = LlmSampleParams(input="page text", task="find the button",
                             images=[{"mime": "image/png", "dataBase64": PNG}])
    _system, user = _messages(params)
    assert isinstance(user.content, list)
    kinds = [block["type"] for block in user.content]
    assert kinds == ["text", "image", "text"]
    assert user.content[1]["data"] == PNG and user.content[2]["text"] == "find the button"
    plain = _messages(LlmSampleParams(input="page"))[1]
    assert isinstance(plain.content, str)


class _Provider:
    vendor, model = "v", "m"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def stream(self, messages: list[ChatMessage], tools: Any, **_kw: Any):
        self.sent.append(messages)
        yield SimpleNamespace(kind="text_delta", text="ok", usage=None, error=None)


def _sample_core(vision: Any) -> Any:
    provider = _Provider()
    providers = SimpleNamespace(get=lambda *_a: provider, vision_for=lambda v, m: vision)
    return SimpleNamespace(settings=Settings(), providers=providers, hub=None), provider


async def test_llm_sample_vision_rules() -> None:
    from snowpea_core.server.llm_handlers import llm_sample_handler

    conn = _Conn("surface-sample")
    conn.client_id = "b"
    HOST_TOOLS._hosts[conn.surface_id] = host_tools._Host(conn=conn, tools={"t": object()})
    try:
        core, provider = _sample_core(vision=False)
        with pytest.raises(RpcError) as caught:
            await llm_sample_handler(conn, LlmSampleParams(input="x", modelCategory="vision"),
                                     core)
        assert caught.value.code == errors.UNAVAILABLE
        assert caught.value.data == {"reason": "no_vision_model"}
        image = [{"mime": "image/png", "dataBase64": PNG}]
        with pytest.raises(RpcError) as caught:
            await llm_sample_handler(conn, LlmSampleParams(input="x", images=image), core)
        assert caught.value.data["reason"] == "model_cannot_see"
        core, provider = _sample_core(vision=True)
        answer = await llm_sample_handler(conn, LlmSampleParams(input="x", images=image), core)
        assert answer.text == "ok"
        assert provider.sent[0][1].content[1]["type"] == "image"
        broken = [{"mime": "image/png", "dataBase64": "bm90IGFuIGltYWdl"}]
        with pytest.raises(RpcError) as caught:
            await llm_sample_handler(conn, LlmSampleParams(input="x", images=broken), core)
        assert caught.value.code == errors.INVALID_PARAMS
    finally:
        HOST_TOOLS._hosts.pop(conn.surface_id, None)


def _view(tab: str, n: int) -> ChatMessage:
    return ChatMessage(role="user", content=[
        {"type": "text", "text": f"(images from repl: v{n})"},
        {"type": "image", "text": "[image]", "name": f"v{n}.png", "mime": "image/png",
         "data": PNG, "snapshotTab": tab},
    ])


def test_image_views_are_superseded_in_batches() -> None:
    history = [_view("7", n) for n in range(4)]
    assert compaction.supersede_snapshots(history) is history  # 4 views: none stale yet
    history.append(_view("7", 4))
    history.append(_view("9", 0))
    out = compaction.supersede_snapshots(history)
    stubbed = [i for i, m in enumerate(out) if m.content[1]["type"] == "text"]
    assert stubbed == [0, 1, 2, 3]
    assert out[0].content[1]["text"] == "[superseded by a newer view of 7]"
    # Views 5..8 change nothing earlier: the request only grows until the 9th.
    for n in range(5, 8):
        history.append(_view("7", n))
        assert compaction.supersede_snapshots(history)[:4] == out[:4]
        assert compaction.supersede_snapshots(history)[4] == history[4]


def test_flush_marks_views_of_a_snapshot_result(core: Core, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.tools import view_image

    monkeypatch.setattr(view_image, "session_can_see_images", lambda *_a: True)
    session = Session(id="s-v", workdir=tmp_path)
    result = host_tools.result_from_answer("repl", {
        "ok": True, "output": "clicked; here is the page",
        "content": [{"type": "image", "data": PNG, "mediaType": "image/png"}],
        "meta": {"kind": "snapshot", "tab": 7},
    })
    session.pending_tool_images.append(("repl", result))
    view_image.flush_tool_image_messages(core, session)
    blocks = session.history.snapshot()[-1].content
    assert [b.get("snapshotTab") for b in blocks if b["type"] == "image"] == ["7"]


# -- h. autoCompactAt and compaction reasons ------------------------------------------------


def test_auto_compact_at_is_the_threshold_should_auto_compact_uses(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compaction, "_output_reserve", lambda *_a: 1000)
    session = Session(id="s-a", workdir=tmp_path)
    for n in range(40):
        session.history.append(ChatMessage(role="user", content=f"m{n}"))
    at = compaction.auto_compact_at(core, session, 10_000)
    assert at == 7_500  # 85 % of 10 000, less the 1 000 kept for the reply
    state = compaction.ContextState(used=at, window=10_000, estimated=True)
    assert compaction.should_auto_compact(core, session, state)
    state.used = at - 1
    assert not compaction.should_auto_compact(core, session, state)
    core.settings.context.autoCompact = False
    assert compaction.auto_compact_at(core, session, 10_000) is None
    assert compaction.auto_compact_at(core, session, None) is None


def test_event_shapes() -> None:
    _, ctx = events.context(10, 100, auto_compact_at=80)
    assert ctx["autoCompactAt"] == 80
    ContextEvent(kind="context", **ctx)
    assert events.context(10, 100)[1]["autoCompactAt"] is None
    _, started = events.compaction_started(5, auto=True, reason="overflow")
    assert started["reason"] == "overflow"
    CompactionStarted(kind="compaction.started", **started)
    _, done = events.compaction(5, 2, 10, auto=True, reason="overflow")
    assert done["reason"] == "overflow" and done["auto"] is True
    CompactionEvent(kind="compaction", **done)
    assert events.compaction(5, 2, 10)[1]["reason"] == "manual"
    assert events.compaction(5, 2, 10, auto=True)[1]["reason"] == "auto"


async def test_overflow_compaction_is_marked_overflow(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def emit(_sid: str, event: Any) -> None:
        seen.append(event)

    async def summarise(*_a: Any, **_k: Any) -> str:
        return "summary"

    monkeypatch.setattr(core.hub, "emit_event", emit)
    monkeypatch.setattr(compaction, "summarise", summarise)
    session = Session(id="s-o", workdir=tmp_path)
    for n in range(40):
        session.history.append(ChatMessage(role="user" if n % 2 else "assistant",
                                           content=f"message {n} " * 20))
    result = await compaction.compact_session(core, session, auto=True, reason="overflow")
    assert result.compacted
    reasons = [p.get("reason") for k, p in seen if k in ("compaction.started", "compaction")]
    assert reasons == ["overflow", "overflow"]
    context = [p for k, p in seen if k == "context"]
    assert context and "autoCompactAt" in context[-1]


def test_result_from_answer_keeps_text_and_snapshot_meta() -> None:
    result = host_tools.result_from_answer("repl", {
        "ok": True, "output": "did it", "meta": {"kind": "snapshot", "tab": "3"},
    })
    assert host_tools.snapshot_tab(result.meta) == "3"
    assert isinstance(result, ToolResult) and result.output == "did it"
