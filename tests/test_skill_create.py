"""``/skill create`` and ``skill.create``/``skill.read``/``skill.write`` (skill authoring).

Modelled on ``test_generators.py``'s ``/agent create`` and ``/skill learn``
coverage: everything runs against a real in-process daemon with the
deterministic scripted fake provider, so the assertions describe what a
client actually sees on the wire.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from _support import RpcClient, connect, fake_provider, make_daemon

from snowpea_core.server.app_server import Daemon
from snowpea_core.skills.generate import extract_skill_document, force_frontmatter_name

FIXTURE = Path(__file__).parent / "fixtures" / "providers" / "fake" / "skill_create.json"
TIMEOUT = 15.0


@pytest_asyncio.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    with fake_provider(FIXTURE):
        instance = await make_daemon(tmp_path / "home")
        try:
            yield instance
        finally:
            await instance.stop()


def project(tmp_path: Path) -> Path:
    workdir = tmp_path / "project"
    workdir.mkdir(exist_ok=True)
    return workdir


async def start_session(client: RpcClient, workdir: Path, mode: str = "accept") -> str:
    result = await client.ok("session.create", {"workdir": str(workdir), "mode": mode})
    return str(result["sessionId"])


async def run(client: RpcClient, session_id: str, text: str) -> str:
    result = await client.ok("session.prompt", {"sessionId": session_id, "text": text})
    return await client.wait_turn(str(result["turnId"]), TIMEOUT)


def last_message(client: RpcClient) -> str:
    messages = client.of_kind("message.done")
    assert messages, f"no message.done; saw {client.kinds()}"
    return str(messages[-1]["payload"]["text"])


# ---------------------------------------------------------------------------
# pure function: forcing the frontmatter name
# ---------------------------------------------------------------------------


def test_force_frontmatter_name_overrides_a_mismatched_name() -> None:
    text = (
        "---\nname: whatever-the-model-picked\ndescription: does a thing, at length here\n"
        "---\nbody"
    )
    forced = force_frontmatter_name(text, "requested-name")
    assert forced.startswith("---\nname: requested-name\n")
    assert "description: does a thing" in forced
    assert forced.endswith("body")


def test_force_frontmatter_name_inserts_when_missing() -> None:
    text = "---\ndescription: does a thing, at length here\n---\nbody"
    forced = force_frontmatter_name(text, "new-name")
    assert forced.startswith("---\nname: new-name\ndescription:")


def test_extract_skill_document_strips_a_wrapping_fence() -> None:
    fenced = "```markdown\n---\nname: x\ndescription: does a thing, at length here\n---\nbody\n```"
    assert extract_skill_document(fenced) == (
        "---\nname: x\ndescription: does a thing, at length here\n---\nbody"
    )
    assert extract_skill_document("no fence here") == "no fence here"


# ---------------------------------------------------------------------------
# /skill create
# ---------------------------------------------------------------------------


async def test_skill_create_writes_a_valid_skill_md_and_reloads(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    reason = await run(
        client,
        session_id,
        '/skill create changelog-notes "a changelog summariser that writes one-line release notes"',
    )
    assert reason == "complete"

    path = workdir / ".snowpea" / "skills" / "changelog-notes" / "SKILL.md"
    assert path.is_file(), f"not written; reply was {last_message(client)}"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: changelog-notes\n")
    assert "## Procedure" in text
    assert "Created skill 'changelog-notes'" in last_message(client)
    assert str(path) in last_message(client)

    if getattr(daemon.core, "skills", None) is None:
        pytest.xfail("the US-017 skill loader is not wired into Core yet")

    result = await client.ok("command.list", {"sessionId": session_id})
    assert "changelog-notes" in {command["name"] for command in result["commands"]}

    await client.stop()


async def test_skill_create_refuses_to_overwrite_without_force(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    args = '"a changelog summariser that writes one-line release notes"'
    assert await run(client, session_id, f"/skill create changelog-notes {args}") == "complete"
    path = workdir / ".snowpea" / "skills" / "changelog-notes" / "SKILL.md"
    original = path.read_text(encoding="utf-8")

    assert await run(client, session_id, f"/skill create changelog-notes {args}") == "complete"
    assert "already exists" in last_message(client)
    assert "--force" in last_message(client)
    assert path.read_text(encoding="utf-8") == original

    forced = await run(client, session_id, f"/skill create changelog-notes {args} --force")
    assert forced == "complete"
    assert "Created skill 'changelog-notes'" in last_message(client)

    await client.stop()


async def test_skill_create_global_path(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    args = '"a changelog summariser that writes one-line release notes" --global'
    assert await run(client, session_id, f"/skill create changelog-notes {args}") == "complete"

    home_path = daemon.core.paths.home / "skills" / "changelog-notes" / "SKILL.md"
    assert home_path.is_file(), f"not written; reply was {last_message(client)}"
    assert not (workdir / ".snowpea" / "skills" / "changelog-notes").exists()

    await client.stop()


async def test_skill_create_plan_mode_reports_and_writes_nothing(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir, mode="plan")

    args = '"a changelog summariser that writes one-line release notes"'
    assert await run(client, session_id, f"/skill create changelog-notes {args}") == "complete"

    assert "Plan mode" in last_message(client)
    assert "would create" in last_message(client)
    assert not (workdir / ".snowpea" / "skills").exists()

    await client.stop()


async def test_skill_create_reports_bad_generated_frontmatter(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    session_id = await start_session(client, workdir)

    reason = await run(client, session_id, '/skill create bad-one "too short a description"')
    assert reason == "complete"
    assert "Could not create the skill" in last_message(client)
    assert not (workdir / ".snowpea" / "skills").exists()

    await client.stop()


# ---------------------------------------------------------------------------
# skill.create / skill.read / skill.write RPC
# ---------------------------------------------------------------------------

VALID_CONTENT = (
    "---\n"
    "name: placeholder\n"
    "description: A hand-written skill supplied verbatim through the RPC content path.\n"
    "---\n\n"
    "## Procedure\n1. Do the thing.\n"
)


async def test_skill_create_rpc_with_content_writes_directly_no_model_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)

    result = await client.ok(
        "skill.create",
        {"name": "hand-written", "content": VALID_CONTENT, "workdir": str(workdir)},
    )
    assert result["name"] == "hand-written"
    path = workdir / ".snowpea" / "skills" / "hand-written" / "SKILL.md"
    assert result["path"] == str(path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: hand-written\n")
    assert not client.of_kind("message.delta")

    await client.stop()


async def test_skill_create_rpc_with_description_starts_a_turn(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)
    await start_session(client, workdir)

    result = await client.ok(
        "skill.create",
        {
            "name": "changelog-notes",
            "description": "a changelog summariser that writes one-line release notes",
            "workdir": str(workdir),
        },
    )
    assert "turnId" in result and result["turnId"]
    assert await client.wait_turn(str(result["turnId"]), TIMEOUT) == "complete"

    path = workdir / ".snowpea" / "skills" / "changelog-notes" / "SKILL.md"
    assert path.is_file()

    await client.stop()


async def test_skill_read_and_write_rpc(
    daemon: Daemon, http: aiohttp.ClientSession, tmp_path: Path
) -> None:
    workdir = project(tmp_path)
    client = await connect(http, daemon, timeout=TIMEOUT)

    await client.ok(
        "skill.create",
        {"name": "hand-written", "content": VALID_CONTENT, "workdir": str(workdir)},
    )

    read = await client.ok("skill.read", {"name": "hand-written", "workdir": str(workdir)})
    assert read["scope"] == "project"
    assert read["content"].startswith("---\nname: hand-written\n")

    updated = read["content"].replace(
        "A hand-written skill supplied verbatim through the RPC content path.",
        "An edited description, saved back through skill.write.",
    )
    await client.ok(
        "skill.write",
        {"name": "hand-written", "content": updated, "workdir": str(workdir)},
    )

    reread = await client.ok("skill.read", {"name": "hand-written", "workdir": str(workdir)})
    assert "An edited description" in reread["content"]

    await client.stop()
