"""m26: an edit a person approved on a card applies exactly what the card showed.

* ``ToolContext.exact`` turns off the fuzzy fallback of ``patch``: zero exact
  matches fail, several fail without ``replace_all``, ``new_string`` goes in
  verbatim (no ``\\r``/``\\t`` unescaping, no reindent).
* ``write_file`` with ``exact`` writes ``content`` byte for byte.
* The loop sets ``exact`` for every call allowed on an approval card, and not
  for an auto-mode call or a session-cache answer (no card for that call).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.agent.loop import _run_one_call
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.exec.local import LocalBackend
from snowpea_core.permissions.approval_queue import Decision
from snowpea_core.permissions.policy import PermissionPolicy
from snowpea_core.providers.base import ToolCall
from snowpea_core.server.app_server import Core
from snowpea_core.session.session import Session
from snowpea_core.tools import file_state, fs
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    file_state.REGISTRY.clear()
    yield
    file_state.REGISTRY.clear()


async def _ctx(tmp_path: Path, text: str, *, exact: bool) -> ToolContext:
    (tmp_path / "a.txt").write_bytes(text.encode("utf-8"))
    core = type("Core", (), {"settings": Settings()})()
    session = Session(id="s-x", workdir=tmp_path)
    ctx = ToolContext(
        session=session, core=core, backend=LocalBackend(tmp_path), exact=exact  # type: ignore[arg-type]
    )
    assert (await fs.read_file(ctx, {"path": "a.txt"})).ok is True
    return ctx


def _on_disk(tmp_path: Path) -> str:
    return (tmp_path / "a.txt").read_bytes().decode("utf-8")


# A block_anchor match: same first and last line, a different middle line.
HIDDEN = "def f():\n    allow_remote = false\n    return 1\n"
SHOWN = "def f():\n    debug = true\n    return 1\n"


async def test_an_approved_edit_with_a_fuzzy_only_match_fails(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, HIDDEN, exact=True)
    result = await fs.patch(ctx, {"path": "a.txt", "old_string": SHOWN, "new_string": "x\n"})
    assert result.ok is False
    assert "not found exactly" in (result.error or "")
    assert _on_disk(tmp_path) == HIDDEN


async def test_an_approved_whitespace_drift_also_fails(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, "  hello\n  world\n", exact=True)
    result = await fs.patch(
        ctx, {"path": "a.txt", "old_string": "hello\nworld", "new_string": "bye"}
    )
    assert result.ok is False
    assert _on_disk(tmp_path) == "  hello\n  world\n"


async def test_an_approved_exact_match_succeeds(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, HIDDEN, exact=True)
    result = await fs.patch(
        ctx,
        {"path": "a.txt", "old_string": "remote = false", "new_string": "remote = true"},
    )
    assert result.ok is True
    assert result.output == "replaced 1 occurrence(s) in a.txt"
    assert _on_disk(tmp_path) == HIDDEN.replace("false", "true")


async def test_several_matches_fail_without_replace_all(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, "x = 1\nx = 1\n", exact=True)
    result = await fs.patch(ctx, {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 2"})
    assert result.ok is False and "appears 2 times" in (result.error or "")
    assert _on_disk(tmp_path) == "x = 1\nx = 1\n"
    result = await fs.patch(
        ctx, {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}
    )
    assert result.ok is True
    assert _on_disk(tmp_path) == "x = 2\nx = 2\n"


async def test_backslash_r_in_new_string_is_not_unescaped(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, 'sep = "a"\n', exact=True)
    new = 'sep = "\\r\\t"'  # the literal characters backslash-r, backslash-t
    result = await fs.patch(ctx, {"path": "a.txt", "old_string": 'sep = "a"', "new_string": new})
    assert result.ok is True
    assert _on_disk(tmp_path) == 'sep = "\\r\\t"\n'
    assert "\r" not in _on_disk(tmp_path) and "\t" not in _on_disk(tmp_path)


async def test_no_reindent_on_the_exact_path(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, "if a:\n        call()\n", exact=True)
    result = await fs.patch(
        ctx, {"path": "a.txt", "old_string": "call()", "new_string": "one()\n  two()"}
    )
    assert result.ok is True
    assert _on_disk(tmp_path) == "if a:\n        one()\n  two()\n"


async def test_a_batch_of_approved_edits_is_exact_too(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, HIDDEN, exact=True)
    result = await fs.patch(ctx, {"path": "a.txt", "edits": [
        {"old_string": "return 1", "new_string": "return 2"},
        {"old_string": SHOWN.replace("1", "2"), "new_string": "gone\n"},
    ]})
    assert result.ok is False and "not found exactly" in (result.error or "")
    assert _on_disk(tmp_path) == HIDDEN.replace("return 1", "return 2")


async def test_an_unapproved_auto_edit_keeps_fuzzy_matching(tmp_path: Path) -> None:
    ctx = await _ctx(tmp_path, "  hello\n  world\n", exact=False)
    result = await fs.patch(
        ctx, {"path": "a.txt", "old_string": "hello\nworld", "new_string": "bye"}
    )
    assert result.ok is True and "matched approximately" in result.output
    assert _on_disk(tmp_path) == "  bye\n"


async def test_the_hidden_line_case_is_fuzzy_without_approval(tmp_path: Path) -> None:
    # The control for the first test: fuzzy matching would replace the hidden line.
    ctx = await _ctx(tmp_path, HIDDEN, exact=False)
    result = await fs.patch(ctx, {"path": "a.txt", "old_string": SHOWN, "new_string": "x\n"})
    assert result.ok is True and "block_anchor" in result.output
    assert _on_disk(tmp_path) == "x\n"


@pytest.mark.parametrize("exact", [True, False])
async def test_write_file_content_is_verbatim(tmp_path: Path, exact: bool) -> None:
    ctx = await _ctx(tmp_path, "old\n", exact=exact)
    content = "a\r\nb\rc\nliteral \\r \\n \\t \\\\ é\n"
    result = await fs.write_file(ctx, {"path": "a.txt", "content": content})
    assert result.ok is True
    assert (tmp_path / "a.txt").read_bytes() == content.encode("utf-8")


async def test_the_card_preview_is_exact(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(HIDDEN, encoding="utf-8")
    backend = LocalBackend(tmp_path)
    fuzzy = {"path": "a.txt", "old_string": SHOWN, "new_string": "x\n"}
    assert await fs.preview_diff(backend, "patch", fuzzy) is None
    exact = {"path": "a.txt", "old_string": "return 1", "new_string": "return 2"}
    assert "+    return 2" in (await fs.preview_diff(backend, "patch", exact) or "")


# -- the loop sets the flag from the approval decision ---------------------------------


class _Ask(PermissionPolicy):
    def decide(self, *_a: Any, **_k: Any) -> Any:
        return "ask"


class _Allow(PermissionPolicy):
    def decide(self, *_a: Any, **_k: Any) -> Any:
        return "allow"


@pytest.fixture
def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    made = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")

    made.seen = []  # type: ignore[attr-defined]

    async def emit(_sid: str, event: Any) -> None:
        made.seen.append(event)  # type: ignore[attr-defined]

    monkeypatch.setattr(made.hub, "emit_event", emit)
    return made


def _spy(core: Any) -> list[bool]:
    seen: list[bool] = []

    async def run(ctx: ToolContext, _args: dict[str, Any]) -> ToolResult:
        seen.append(ctx.exact)
        return ToolResult(ok=True, output="ok")

    core.tools.register(Tool(name="spy", category="test", description="spy",
                             input_schema={}, permission="read", run=run))
    return seen


def _answer(core: Any, monkeypatch: pytest.MonkeyPatch, by: str) -> None:
    async def request(*_a: Any, **_k: Any) -> Decision:
        return Decision("allow", "once", by)

    monkeypatch.setattr(core.approvals, "request", request)


async def _run(core: Any, tmp_path: Path, policy: PermissionPolicy, call: ToolCall,
               mode: str = "ask") -> None:
    (tmp_path / "proj").mkdir(exist_ok=True)
    session = Session(id="s-l", workdir=tmp_path / "proj", mode=mode)
    await asyncio.wait_for(_run_one_call(
        core, session, LocalBackend(session.workdir), policy, call, "t", False), 10)


@pytest.mark.parametrize("by,exact", [
    ("user", True), ("person", True), ("jarvis", True),
    ("cache", False), ("script-grant", False),
])
async def test_an_asked_call_runs_exact_unless_no_card_was_shown(
    core: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by: str, exact: bool
) -> None:
    seen = _spy(core)
    _answer(core, monkeypatch, by)
    await _run(core, tmp_path, _Ask(), ToolCall(id="c-1", name="spy", arguments={}))
    assert seen == [exact]


async def test_an_auto_call_is_not_exact(core: Any, tmp_path: Path) -> None:
    seen = _spy(core)
    await _run(core, tmp_path, _Allow(), ToolCall(id="c-2", name="spy", arguments={}),
               mode="auto")
    assert seen == [False]


@pytest.mark.parametrize("by,applied", [("user", False), ("cache", True)])
async def test_an_approved_fuzzy_patch_is_refused_end_to_end(
    core: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by: str, applied: bool
) -> None:
    _answer(core, monkeypatch, by)
    core.tools.register(next(tool for tool in fs.TOOLS if tool.name == "patch"))
    (tmp_path / "proj").mkdir()
    target = tmp_path / "proj" / "a.txt"
    target.write_text(HIDDEN, encoding="utf-8")
    await _run(core, tmp_path, _Ask(), ToolCall(
        id="c-3", name="patch",
        arguments={"path": "a.txt", "old_string": SHOWN, "new_string": "x\n"}))
    (result,) = [payload for kind, payload in core.seen if kind == "tool.result"]
    assert result["ok"] is applied, result.get("error")
    assert applied or "not found exactly" in result["error"]
    assert target.read_text(encoding="utf-8") == ("x\n" if applied else HIDDEN)
