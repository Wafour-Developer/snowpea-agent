"""Per-profile scoping for a multi-profile snowpea-browser (addendum 8).

Each browser profile is its own core client with a stable clientId, and every
session it opens carries that id as ``hostToolsFrom``. Profile B must never see
profile A's sessions, pending cards or "always allow" rules; IDE/CLI sessions
(no host) stay shared and unscoped.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
from test_host_tools import Daemon, daemon, new_session, open_client  # noqa: F401

from snowpea_core.config.settings import Settings
from snowpea_core.permissions.allowlist import Allowlist
from snowpea_core.permissions.approval_queue import ApprovalQueue
from snowpea_core.server.protocol import QuestionItem, QuestionOption
from snowpea_core.session.questions import QuestionQueue

A = "snowpea-browser-A"
B = "snowpea-browser-B"


class FakeHub:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any], exclude: Any = None) -> None:
        self.sent.append((method, params))


def _session(host: str | None, workdir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"s-{host or 'ide'}",
        workdir=workdir,
        host_tools_from=host,
        origin_conn=None,
        interrupt=None,
    )


# ---------------------------------------------------------------------------
# (c) always-allow rules belong to the profile that answered
# ---------------------------------------------------------------------------


async def test_an_always_answer_in_a_profile_is_that_profiles_rule(tmp_path: Path) -> None:
    allowlist = Allowlist(settings=Settings())
    hub = FakeHub()
    queue = ApprovalQueue(settings=Settings(), hub=hub, allowlist=allowlist)  # type: ignore[arg-type]
    session = _session(A, tmp_path)

    async def answer() -> None:
        while not queue.unattended():
            await asyncio.sleep(0.01)
        pending = queue.unattended()[0]
        await queue.respond(pending.requestId, "allow", "always")

    responder = asyncio.ensure_future(answer())
    decision = await queue.request(
        session, "repl", {"code": "1"}, unattended=True, timeout_sec=5  # type: ignore[arg-type]
    )
    await responder
    assert decision.allowed

    # (b) the unattended card names its profile
    pending = [p for m, p in hub.sent if m == "approval.pending"]
    assert pending and pending[0]["hostToolsFrom"] == A

    [item] = allowlist.list("global")
    assert item.host == A
    tool = SimpleNamespace(name="repl", permission="read")
    assert allowlist.matches(tool, {}, host=A) is True
    assert allowlist.matches(tool, {}, host=B) is False
    assert allowlist.matches(tool, {}, host=None) is False  # an IDE session


def test_ide_rules_stay_unscoped_and_list_remove_honour_the_profile(tmp_path: Path) -> None:
    allowlist = Allowlist(settings=Settings())
    ide_rule = allowlist.add("^git status$", "global")
    a_rule = allowlist.add("^repl$", "global", "tool:repl", host=A)
    project_rule = allowlist.add("^ls$", "project", workdir=tmp_path, host=A)

    assert allowlist.list("global")[0].host is None
    # A profile sees its own global rules and the shared project ones.
    ids = {item.id for item in allowlist.list(workdir=tmp_path, host=A, any_host=False)}
    assert ids == {a_rule, project_rule}
    ids_b = {item.id for item in allowlist.list(workdir=tmp_path, host=B, any_host=False)}
    assert ids_b == {project_rule}
    # An IDE session still matches its unscoped rule and project rules.
    shell = SimpleNamespace(name="shell", permission="exec")
    assert allowlist.matches(shell, {"command": "git status"}, workdir=tmp_path) is True
    assert allowlist.matches(shell, {"command": "git status"}, host=A) is False
    assert allowlist.matches(shell, {"command": "ls"}, workdir=tmp_path, host=B) is True

    # Profile B cannot remove profile A's rule; A can.
    assert allowlist.remove(a_rule, host=B, any_host=False) is False
    assert allowlist.remove(a_rule, host=A, any_host=False) is True
    assert [item.id for item in allowlist.list("global")] == [ide_rule]


# ---------------------------------------------------------------------------
# (b) question cards
# ---------------------------------------------------------------------------


async def test_a_question_card_names_its_profile(tmp_path: Path) -> None:
    hub = FakeHub()
    queue = QuestionQueue(settings=Settings(), hub=hub)  # type: ignore[arg-type]
    session = _session(A, tmp_path)
    item = QuestionItem(
        header="Pick", question="Which?", options=[QuestionOption(label="x", description="")]
    )
    await queue.ask(session, [item], timeout_sec=0)  # type: ignore[arg-type]
    pending = [p for m, p in hub.sent if m == "question.pending"]
    assert pending and pending[0]["hostToolsFrom"] == A
    changed = [p for m, p in hub.sent if m == "sessions.changed"]
    assert changed and all(p["hostToolsFrom"] == A for p in changed)


# ---------------------------------------------------------------------------
# (a) sessions carry their owner, and session.list filters on it
# ---------------------------------------------------------------------------


async def test_sessions_carry_their_profile_and_list_filters_on_it(
    http: aiohttp.ClientSession, daemon: Daemon, tmp_path: Path  # noqa: F811
) -> None:
    profile_a = await open_client(http, daemon, client_id=A)
    profile_b = await open_client(http, daemon, client_id=B)
    try:
        a_session = await new_session(profile_a, tmp_path / "a", hostToolsFrom=A)
        b_session = await new_session(profile_b, tmp_path / "b", hostToolsFrom=B)
        ide_session = await new_session(profile_a, tmp_path / "ide")

        rows = {r["sessionId"]: r for r in (await profile_a.ok("session.list", {}))["sessions"]}
        assert rows[a_session]["hostToolsFrom"] == A
        assert rows[b_session]["hostToolsFrom"] == B
        assert rows[ide_session]["hostToolsFrom"] is None

        only_a = await profile_a.ok("session.list", {"hostToolsFrom": A})
        assert [r["sessionId"] for r in only_a["sessions"]] == [a_session]
        with_shared = await profile_a.ok(
            "session.list", {"hostToolsFrom": A, "includeUnhosted": True}
        )
        assert {r["sessionId"] for r in with_shared["sessions"]} == {a_session, ide_session}

        await profile_a.ok("session.rename", {"sessionId": a_session, "title": "inbox"})
        await asyncio.sleep(0.05)
        renamed = [
            n["params"] for n in profile_b.notifications
            if n["method"] == "sessions.changed" and n["params"]["sessionId"] == a_session
        ]
        assert renamed and renamed[-1]["hostToolsFrom"] == A

        await profile_a.ok("session.notice", {"sessionId": a_session, "text": "hi"})
        await profile_a.ok("session.prompt", {"sessionId": a_session, "text": "hello"})
        await profile_a.wait(lambda e: e["kind"] == "turn.done", timeout=10)
        events = [
            n["params"] for n in profile_a.notifications
            if n["method"] == "session.event" and n["params"]["sessionId"] == a_session
        ]
        assert events and all(e["hostToolsFrom"] == A for e in events)
    finally:
        await profile_a.stop()
        await profile_b.stop()
