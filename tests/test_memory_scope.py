"""M5 §1b: project memory, the scope question, and the human-readable mirror.

Three things are worth pinning here, and they are the three that are easy to
get quietly wrong:

* **Which namespace a session belongs to.**  Derived from the workdir's git
  root, so ``repo/`` and ``repo/core/`` share one project memory and a sibling
  checkout does not.
* **What happens when the model does not say where to save.**  It must ask a
  human, honour the answer, and write *nothing* on a cancel or a timeout —
  silence is not "save it globally".
* **That recall sees both scopes at once**, labelled, so the model can tell a
  project fact from a standing preference.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from _support import connect, make_daemon
from test_memory_recall import RecordingProvider, install, run
from test_session_loop import start_session

from snowpea_core.commands import memory_cmd
from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.memory import MemoryServices, mirror
from snowpea_core.memory.retrieval import namespaces_for, render_block
from snowpea_core.memory.scopes import (
    GLOBAL_NAMESPACE,
    project_namespace,
    project_root,
    scope_of,
)
from snowpea_core.memory.tools import (
    CANCELLED,
    NO_ANSWER,
    OPTION_GLOBAL,
    OPTION_PROJECT,
    memory_search,
    memory_write,
)
from snowpea_core.session.questions import QuestionQueue
from snowpea_core.session.session import Session
from snowpea_core.tools.registry import ToolContext

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# scaffolding
# ---------------------------------------------------------------------------


class _Hub:
    async def notify(self, method: str, params: dict[str, Any], exclude: Any = None) -> None:
        return None


class _Origin:
    """A client that answers ``question.request`` with a fixed label."""

    def __init__(self, label: str | None) -> None:
        self.label = label
        self.asked: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float = 0.0) -> Any:
        self.asked.append(params)
        if self.label is None:  # a surface that cannot ask: a decline
            return {"answers": [{"selected": [], "text": None}]}
        return {"answers": [{"selected": [self.label], "text": None}]}


class _Core:
    """The smallest Core-shaped thing ``memory_write`` reaches into."""

    def __init__(self, services: MemoryServices, *, timeout_sec: int = 600) -> None:
        self.memory = services
        self.settings = Settings.model_validate(
            {"questions": {"timeoutSec": timeout_sec}, "memory": {"enabled": True}}
        )
        self.questions = QuestionQueue(self.settings, _Hub())
        self.stopping = False


def git_repo(root: Path) -> Path:
    """A real (empty) git checkout at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


@pytest_asyncio.fixture
async def services(tmp_path: Path) -> Any:
    store = MemoryServices.open(Paths.create(tmp_path / "home"))
    try:
        yield store
    finally:
        await store.close()


def ctx_for(core: _Core, workdir: Path, **kwargs: Any) -> ToolContext:
    session = Session(id="s-1", workdir=workdir, **kwargs)
    return ToolContext(session=session, core=core, backend=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# (1) namespace derivation
# ---------------------------------------------------------------------------


async def test_project_root_is_the_git_root(tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    nested = root / "core" / "snowpea_core"
    nested.mkdir(parents=True)
    assert project_root(nested) == root.resolve()
    assert project_namespace(nested) == f"project:{root.resolve()}"
    # A sibling checkout is a different project, not the same one.
    other = git_repo(tmp_path / "other")
    assert project_namespace(other) != project_namespace(root)


async def test_a_plain_directory_is_its_own_project(tmp_path: Path) -> None:
    plain = tmp_path / "notes"
    plain.mkdir()
    assert project_root(plain) == plain.resolve()
    assert scope_of(project_namespace(plain)) == "project"


async def test_home_and_a_missing_directory_have_no_project(tmp_path: Path) -> None:
    assert project_root(Path.home()) is None
    assert project_namespace(tmp_path / "nope") == ""


async def test_session_derives_its_namespace_from_the_workdir(tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    (root / "sub").mkdir()
    session = Session(id="s-1", workdir=root / "sub")
    assert session.project_namespace == f"project:{root.resolve()}"
    assert session.project_root == str(root.resolve())
    # Recall reads project first, then global; no agent namespace here.
    assert namespaces_for(session) == [session.project_namespace, GLOBAL_NAMESPACE]
    session.memory_namespace = "agent:releaser"
    assert namespaces_for(session) == [
        session.project_namespace,
        GLOBAL_NAMESPACE,
        "agent:releaser",
    ]


# ---------------------------------------------------------------------------
# (2) memory_write with an explicit scope
# ---------------------------------------------------------------------------


async def test_explicit_scope_skips_the_question(services: MemoryServices, tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    origin = _Origin(OPTION_GLOBAL)
    core = _Core(services)
    ctx = ctx_for(core, root)
    ctx.session.origin_conn = origin

    project = await memory_write(ctx, {"text": "빌드는 uv run", "scope": "project"})
    glob = await memory_write(ctx, {"text": "이름은 whitevil", "scope": "global"})

    assert origin.asked == [], "an explicit scope must never ask"
    assert project.meta is not None and project.meta["scope"] == "project"
    assert glob.meta is not None and glob.meta["scope"] == "global"
    in_project = await services.store.list(namespace=f"project:{root.resolve()}")
    assert [row.text for row in in_project] == ["빌드는 uv run"]
    assert [row.text for row in await services.store.list()] == ["이름은 whitevil"]


async def test_an_unknown_scope_is_refused(services: MemoryServices, tmp_path: Path) -> None:
    result = await memory_write(
        ctx_for(_Core(services), git_repo(tmp_path / "repo")),
        {"text": "x", "scope": "team"},
    )
    assert result.ok is False
    assert "project" in (result.error or "")


# ---------------------------------------------------------------------------
# (3) memory_write without a scope: the question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected_scope"),
    [(OPTION_PROJECT.format(project="repo"), "project"), (OPTION_GLOBAL, "global")],
)
async def test_no_scope_asks_and_honours_the_answer(
    services: MemoryServices, tmp_path: Path, answer: str, expected_scope: str
) -> None:
    root = git_repo(tmp_path / "repo")
    origin = _Origin(answer)
    ctx = ctx_for(_Core(services), root)
    ctx.session.origin_conn = origin

    result = await memory_write(ctx, {"text": "배포 대상은 duho 서버다"})

    assert len(origin.asked) == 1
    question = origin.asked[0]["questions"][0]
    assert question["header"] == "Where to keep it"
    assert "repo" in question["question"]
    assert [option["label"] for option in question["options"]] == [
        OPTION_PROJECT.format(project="repo"),
        OPTION_GLOBAL,
        "Cancel",
    ]
    assert question["allowOther"] is False
    assert result.meta is not None and result.meta["scope"] == expected_scope


async def test_cancel_writes_nothing(services: MemoryServices, tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    ctx = ctx_for(_Core(services), root)
    ctx.session.origin_conn = _Origin("Cancel")

    result = await memory_write(ctx, {"text": "배포 대상은 duho 서버다"})

    assert result.output == CANCELLED
    assert result.meta == {"written": False}
    assert await services.store.list_many(namespaces=namespaces_for(ctx.session)) == []


async def test_a_declining_surface_writes_nothing(
    services: MemoryServices, tmp_path: Path
) -> None:
    """A headless client answers the s2c call with nothing; that is not assent."""
    root = git_repo(tmp_path / "repo")
    ctx = ctx_for(_Core(services), root)
    ctx.session.origin_conn = _Origin(None)

    result = await memory_write(ctx, {"text": "배포 대상은 duho 서버다"})

    assert result.output == CANCELLED
    assert await services.store.list_many(namespaces=namespaces_for(ctx.session)) == []


async def test_nobody_answering_writes_nothing(services: MemoryServices, tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    ctx = ctx_for(_Core(services, timeout_sec=1), root)
    ctx.session.origin_conn = None  # nothing to ask, and no client will respond

    result = await asyncio.wait_for(memory_write(ctx, {"text": "duho"}), timeout=10)

    assert result.output == NO_ANSWER
    assert await services.store.list_many(namespaces=namespaces_for(ctx.session)) == []


@pytest.mark.parametrize("kwargs", [{"unattended": True}, {"is_subagent": True}])
async def test_an_unwatched_session_defaults_to_project(
    services: MemoryServices, tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    root = git_repo(tmp_path / "repo")
    origin = _Origin(OPTION_GLOBAL)
    ctx = ctx_for(_Core(services), root, **kwargs)
    ctx.session.origin_conn = origin

    result = await memory_write(ctx, {"text": "빌드는 uv run"})

    assert origin.asked == [], "nobody is watching; asking would hang the turn"
    assert result.meta is not None and result.meta["scope"] == "project"


async def test_ask_scope_false_defaults_to_project(
    services: MemoryServices, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    origin = _Origin(OPTION_GLOBAL)
    core = _Core(services)
    services.settings.askScope = False
    ctx = ctx_for(core, root)
    ctx.session.origin_conn = origin

    result = await memory_write(ctx, {"text": "빌드는 uv run"})

    assert origin.asked == []
    assert result.meta is not None and result.meta["scope"] == "project"


async def test_a_session_with_no_project_writes_globally(
    services: MemoryServices, tmp_path: Path
) -> None:
    origin = _Origin(OPTION_GLOBAL)
    ctx = ctx_for(_Core(services), Path.home())
    ctx.session.origin_conn = origin

    result = await memory_write(ctx, {"text": "이름은 whitevil"})

    assert origin.asked == [], "there is no project to choose; global is the only answer"
    assert result.meta is not None and result.meta["scope"] == "global"


# ---------------------------------------------------------------------------
# (4) recall merges the scopes and labels them
# ---------------------------------------------------------------------------


async def test_recall_merges_and_labels_both_scopes(
    services: MemoryServices, tmp_path: Path
) -> None:
    """A hit the digest does not already show is rendered and labelled.

    The digest lists the newest few, so this pushes the interesting memory
    past that limit and asks for it by name.
    """
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    services.retrieval.settings.digestEntries = 1
    await services.store.write(
        "이 프로젝트 배포 대상은 duho 서버다", namespace=session.project_namespace
    )
    await services.store.write("최근 메모", namespace=session.project_namespace)
    elsewhere = project_namespace(git_repo(tmp_path / "elsewhere"))
    await services.store.write("남의 프로젝트 배포 대상", namespace=elsewhere)

    block = await services.retrieval.context_block(session, "배포 대상")

    assert "[project] 이 프로젝트 배포 대상은 duho 서버다" in block
    assert "남의 프로젝트" not in block, "another checkout's memories must not leak in"
    assert str(root.resolve()) in block, "the header says which project [project] means"


# ---------------------------------------------------------------------------
# (4b) the standing digest (M5 §1b)
# ---------------------------------------------------------------------------


async def test_a_fresh_session_already_knows_the_project(
    services: MemoryServices, tmp_path: Path
) -> None:
    """Nothing has been said yet, and the block is already full of context."""
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    for text in ("빌드는 uv run", "배포는 duho 서버", "테스트는 pytest -q"):
        await services.store.write(text, namespace=session.project_namespace)
    await services.profile.set("deploy_target", "duho 서버", namespace=GLOBAL_NAMESPACE)
    await services.store.write("짧은 답을 선호한다", namespace=GLOBAL_NAMESPACE)

    block = await services.retrieval.context_block(session, "")

    assert "## Project memory (repo)" in block
    for text in ("빌드는 uv run", "배포는 duho 서버", "테스트는 pytest -q"):
        assert text in block
    assert "## About the user" in block
    assert "- deploy_target: duho 서버" in block
    assert "## Global memory" in block
    assert "짧은 답을 선호한다" in block
    # The profile fact is listed once, under About the user, not twice.
    assert block.count("deploy_target") == 1
    # Every digest line is citable.
    assert block.count("[mem:m-") == 5


async def test_a_write_this_turn_is_in_the_next_turns_digest(
    services: MemoryServices, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    await services.store.write("빌드는 uv run", namespace=session.project_namespace)
    first = await services.retrieval.context_block(session, "")
    assert "배포는 duho" not in first

    await services.store.write("배포는 duho 서버", namespace=session.project_namespace)
    second = await services.retrieval.context_block(session, "")

    assert "배포는 duho 서버" in second
    # And a delete drops back out again.
    entries = await services.store.list(namespace=session.project_namespace)
    await services.store.delete(entries[0].id)
    assert "배포는 duho 서버" not in await services.retrieval.context_block(session, "")


async def test_the_digest_is_cached_until_the_store_changes(
    services: MemoryServices, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    await services.store.write("빌드는 uv run", namespace=session.project_namespace)

    first = await services.retrieval.digest(session)
    assert await services.retrieval.digest(session) is first, "rebuilt for no reason"

    await services.store.write("배포는 duho", namespace=session.project_namespace)
    assert await services.retrieval.digest(session) is not first


async def test_the_digest_trims_and_says_how_many_it_left_out(
    services: MemoryServices, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    for index in range(12):
        await services.store.write(f"메모 번호 {index:02d}", namespace=session.project_namespace)
    # Room for a handful of lines out of twelve memories.
    services.retrieval.settings.digestEntries = 10
    services.retrieval.settings.digestChars = 120

    block = await services.retrieval.context_block(session, "")

    shown = [line for line in block.splitlines() if line.startswith("- ") and "메모" in line]
    assert 0 < len(shown) < 10
    assert f"… and {12 - len(shown)} more — memory_search finds the rest" in block


async def test_an_entry_limit_below_the_total_is_counted_too(
    services: MemoryServices, tmp_path: Path
) -> None:
    """The "K more" line counts the whole namespace, not just what was trimmed."""
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    for index in range(9):
        await services.store.write(f"메모 {index}", namespace=session.project_namespace)
    services.retrieval.settings.digestEntries = 3

    block = await services.retrieval.context_block(session, "")

    shown = [line for line in block.splitlines() if line.startswith("- ") and "메모" in line]
    assert len(shown) == 3
    assert "… and 6 more" in block


async def test_no_memories_means_no_block(services: MemoryServices, tmp_path: Path) -> None:
    session = Session(id="s-1", workdir=git_repo(tmp_path / "repo"))
    assert await services.retrieval.context_block(session, "아무거나") == ""


async def test_empty_sections_are_omitted(services: MemoryServices, tmp_path: Path) -> None:
    session = Session(id="s-1", workdir=git_repo(tmp_path / "repo"))
    await services.store.write("짧은 답을 선호한다", namespace=GLOBAL_NAMESPACE)

    block = await services.retrieval.context_block(session, "")

    assert "## Global memory" in block
    assert "## Project memory" not in block
    assert "## About the user" not in block


async def test_a_recall_hit_the_digest_shows_is_not_repeated(
    services: MemoryServices, tmp_path: Path
) -> None:
    session = Session(id="s-1", workdir=git_repo(tmp_path / "repo"))
    await services.store.write("배포는 duho 서버", namespace=session.project_namespace)

    block = await services.retrieval.context_block(session, "배포")

    assert block.count("배포는 duho 서버") == 1
    assert "<memory id=" not in block


async def test_memory_search_tool_labels_its_hits(
    services: MemoryServices, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    ctx = ctx_for(_Core(services), root)
    await services.store.write("duho 서버에 배포한다", namespace=ctx.session.project_namespace)
    await services.store.write("duho 계정은 whitevil", namespace=GLOBAL_NAMESPACE)

    result = await memory_search(ctx, {"query": "duho"})

    assert "[project]" in result.output
    assert "[global]" in result.output


async def test_render_block_without_scopes_is_unchanged(services: MemoryServices) -> None:
    entry = await services.store.write("plain", namespace=GLOBAL_NAMESPACE)
    block = render_block([entry])
    assert block.splitlines()[-1] == f'<memory id="{entry.id}" tags="">[global] plain</memory>'


# ---------------------------------------------------------------------------
# (5) the human-readable mirror
# ---------------------------------------------------------------------------


async def test_project_memories_are_mirrored_into_the_checkout(
    services: MemoryServices, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    namespace = project_namespace(root)
    first = await services.store.write("빌드는 uv run", tags=["build"], namespace=namespace)
    await services.store.write("배포는 duho", namespace=namespace)

    path = root.resolve() / ".snowpea" / "memory.md"
    body = path.read_text(encoding="utf-8")
    lines = [line for line in body.splitlines() if line.startswith("- ")]
    assert lines == [
        f"- [{first.created_at[:10]}] 빌드는 uv run #build",
        f"- [{first.created_at[:10]}] 배포는 duho",
    ]
    assert body.startswith("# Snowpea memory — repo")

    # A delete rewrites the file from the store rather than cutting a line.
    assert await services.store.delete(first.id) is True
    remaining = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
    ]
    assert remaining == [f"- [{first.created_at[:10]}] 배포는 duho"]


async def test_global_memories_are_mirrored_into_snowpea_home(
    services: MemoryServices, tmp_path: Path
) -> None:
    await services.store.write("이름은 whitevil", namespace=GLOBAL_NAMESPACE)
    path = services.store.home / "memory.md"
    assert "이름은 whitevil" in path.read_text(encoding="utf-8")


async def test_the_nudge_does_not_duplicate_a_scoped_memory(
    services: MemoryServices, tmp_path: Path
) -> None:
    """A fact already filed under the project is not written globally again."""
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    fact = "배포 대상은 duho 서버다"
    await services.store.write(fact, namespace=session.project_namespace)

    assert await services.store.exists_any(fact, namespaces=namespaces_for(session)) is True
    assert await services.store.exists(fact, namespace=GLOBAL_NAMESPACE) is False


async def test_agent_memories_have_no_mirror(services: MemoryServices) -> None:
    await services.store.write("agent only", namespace="agent:releaser")
    assert mirror.mirror_path("agent:releaser", services.store.home) is None
    assert not (services.store.home / "memory.md").exists()


async def test_an_unwritable_mirror_never_costs_a_memory(
    services: MemoryServices, tmp_path: Path
) -> None:
    """A read-only checkout still remembers; the mirror is a convenience."""
    root = tmp_path / "ro"
    root.mkdir()
    (root / ".snowpea").write_text("not a directory", encoding="utf-8")
    namespace = project_namespace(root)

    entry = await services.store.write("여전히 기억된다", namespace=namespace)

    assert [row.id for row in await services.store.list(namespace=namespace)] == [entry.id]


# ---------------------------------------------------------------------------
# (6) /memory parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", ("list", "", "all")),
        ("list", ("list", "", "all")),
        ("list --project", ("list", "", "project")),
        ("--global list", ("list", "", "global")),
        ("search duho 서버", ("search", "duho 서버", "all")),
        ("search --global duho", ("search", "duho", "global")),
        ("forget m-abc123", ("forget", "m-abc123", "all")),
        ("duho", ("search", "duho", "all")),
    ],
)
async def test_memory_command_parsing(args: str, expected: tuple[str, str, str]) -> None:
    assert memory_cmd.parse(args) == expected


async def test_memory_command_namespaces(tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    session = Session(id="s-1", workdir=root)
    project = session.project_namespace
    assert memory_cmd.namespaces(session, "project") == [project]
    assert memory_cmd.namespaces(session, "global") == [GLOBAL_NAMESPACE]
    assert memory_cmd.namespaces(session, "all") == [project, GLOBAL_NAMESPACE]
    homeless = Session(id="s-2", workdir=Path.home())
    assert memory_cmd.namespaces(homeless, "project") == []


# ---------------------------------------------------------------------------
# (7) memory.list / memory.delete over the wire
# ---------------------------------------------------------------------------


async def test_memory_list_filters_by_scope(
    snowpea_home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    root = git_repo(tmp_path / "repo")
    daemon = await make_daemon(snowpea_home / "scopes")
    try:
        client = await connect(http, daemon)
        created = await client.ok("session.create", {"workdir": str(root)})
        session_id = str(created["sessionId"])
        assert daemon.core is not None
        store = daemon.core.memory.store
        project = await store.write("빌드는 uv run", namespace=project_namespace(root))
        glob = await store.write("이름은 whitevil", namespace=GLOBAL_NAMESPACE)

        every = await client.ok("memory.list", {"sessionId": session_id})
        assert {entry["id"] for entry in every["entries"]} == {project.id, glob.id}
        assert {entry["scope"] for entry in every["entries"]} == {"project", "global"}
        assert next(e for e in every["entries"] if e["id"] == project.id)["project"] == str(
            root.resolve()
        )

        only_project = await client.ok(
            "memory.list", {"sessionId": session_id, "scope": "project"}
        )
        assert [entry["id"] for entry in only_project["entries"]] == [project.id]

        only_global = await client.ok("memory.list", {"scope": "global"})
        assert [entry["id"] for entry in only_global["entries"]] == [glob.id]

        # A CLI in a checkout resolves the project without a session.
        by_path = await client.ok("memory.list", {"scope": "project", "project": str(root)})
        assert [entry["id"] for entry in by_path["entries"]] == [project.id]

        filtered = await client.ok(
            "memory.list", {"sessionId": session_id, "query": "whitevil"}
        )
        assert [entry["id"] for entry in filtered["entries"]] == [glob.id]

        assert await client.ok("memory.delete", {"id": project.id}) == {"ok": True}
        assert await store.list(namespace=project_namespace(root)) == []
        missing = await client.call("memory.delete", {"id": project.id})
        assert missing["error"]["data"]["code"] == "not_found"
    finally:
        await daemon.stop()


async def test_a_fresh_session_carries_the_digest_in_its_system_prompt(
    snowpea_home: Path, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """End to end: a project's standing facts are in the prompt of turn one.

    Nothing is searched for and no tool is called — the daemon is asked an
    unrelated question, and the project's memories are there anyway.
    """
    root = git_repo(tmp_path / "repo")
    daemon = await make_daemon(snowpea_home / "digest")
    provider = install(daemon, RecordingProvider())
    try:
        assert daemon.core is not None
        memory = daemon.core.memory
        namespace = project_namespace(root)
        for text in ("빌드는 uv run", "배포는 duho 서버", "테스트는 pytest -q"):
            await memory.store.write(text, namespace=namespace)
        await memory.profile.set("name", "whitevil", namespace=GLOBAL_NAMESPACE)

        client = await connect(http, daemon)
        session_id = await start_session(client, root)
        assert await run(client, session_id, "안녕하세요") == "complete"

        system = provider.system_prompts[0]
        assert "## Project memory (repo)" in system
        for text in ("빌드는 uv run", "배포는 duho 서버", "테스트는 pytest -q"):
            assert text in system
        assert "## About the user" in system
        assert "- name: whitevil" in system
    finally:
        await daemon.stop()
