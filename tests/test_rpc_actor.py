"""``actor`` is a dispatcher-level param on EVERY method (1.54.0).

A browser stamps ``actor: "person"`` on whatever a click sends; no method may
refuse it as an unknown field.  The handlers are replaced by no-ops: nothing
here runs a real handler (``system.shutdown`` and friends included).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.config.paths import Paths
from snowpea_core.config.settings import Settings
from snowpea_core.server import errors
from snowpea_core.server.app_server import Core, build_dispatcher
from snowpea_core.server.errors import RpcError
from snowpea_core.server.protocol import METHODS
from snowpea_core.server.rpc import current_actor


def _conn(kind: str) -> Any:
    return SimpleNamespace(authenticated=True, closed=False, client_kind=kind,
                           remote_login=False, client_version="0.3.0", client_id=f"{kind}-1",
                           surface_id=f"{kind}-s")


@pytest.fixture
def dispatcher(tmp_path: Path) -> Any:
    core = Core(settings=Settings(), paths=Paths.create(tmp_path / "home"), token="t")
    built = build_dispatcher(core)
    seen: list[str | None] = []

    async def noop(_conn: Any, _params: Any, _core: Any) -> dict[str, Any]:
        seen.append(current_actor())
        return {}

    for name in list(built._handlers):
        built._handlers[name] = noop
    built.seen = seen
    return built


def _actor_errors(exc: RpcError) -> list[Any]:
    details = exc.data if isinstance(exc.data, list) else []
    return [e for e in details if isinstance(e, dict) and "actor" in [str(p) for p in
                                                                     e.get("loc", ())]]


C2S = sorted(name for name, method in METHODS.items() if method.direction == "c2s")


@pytest.mark.parametrize("kind", ["browser", "ide"])
@pytest.mark.parametrize("method", C2S)
async def test_actor_is_never_an_unknown_field(dispatcher: Any, method: str, kind: str) -> None:
    if method not in dispatcher._handlers:
        pytest.skip("not served by this daemon")
    try:
        await dispatcher._invoke(_conn(kind), {"method": method, "params": {"actor": "person"}})
    except RpcError as exc:
        assert not _actor_errors(exc), (method, exc.data)
        assert "actor" not in exc.message, (method, exc.message)


@pytest.mark.parametrize("method", C2S)
async def test_another_actor_value_is_invalid_params(dispatcher: Any, method: str) -> None:
    if method not in dispatcher._handlers:
        pytest.skip("not served by this daemon")
    with pytest.raises(RpcError) as caught:
        await dispatcher._invoke(_conn("browser"), {"method": method,
                                                    "params": {"actor": "robot"}})
    assert caught.value.code == errors.INVALID_PARAMS


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("session.create", {"workdir": "/tmp/x"}),
        ("session.setMode", {"sessionId": "s", "mode": "plan"}),
        ("session.setAgent", {"sessionId": "s", "agent": None}),
        ("job.schedule", {"spec": "every day", "task": "x"}),
        ("job.update", {"jobId": "j"}),
        ("question.respond", {"requestId": "q", "answers": []}),
    ],
)
async def test_named_methods_accept_the_marker_and_record_it(
    dispatcher: Any, method: str, params: dict[str, Any]
) -> None:
    await dispatcher._invoke(_conn("ide"), {"method": method,
                                            "params": {**params, "actor": "person"}})
    assert dispatcher.seen[-1] == "person"
    assert current_actor() is None  # reset after the call


async def test_the_reply_shapes_carry_actor(tmp_path: Path) -> None:
    """``approval.request`` and ``question.request`` replies: ``actor`` is read, never refused."""
    import asyncio

    from snowpea_core.permissions.approval_queue import ApprovalQueue
    from snowpea_core.server.protocol import QuestionItem, QuestionOption
    from snowpea_core.session.questions import QuestionQueue

    origin = _conn("browser")

    async def call(method: str, _params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        if method == "approval.request":
            return {"decision": "allow", "scope": "once", "actor": "person"}
        return {"answers": [{"selected": ["a"]}], "actor": "person"}

    origin.call = call
    session = SimpleNamespace(id="s-1", workdir=str(tmp_path), origin_conn=origin,
                              host_tools_from=None, owned_by=None)
    approvals = ApprovalQueue(Settings(), Paths.create(tmp_path / "home"))
    decision = await approvals.request(session, "shell", {"command": "npm test"},
                                       tags=("exec",))
    assert decision.allowed
    questions = QuestionQueue(Settings())
    answers = await asyncio.wait_for(questions.ask(
        session, [QuestionItem(header="h", question="q", options=[QuestionOption(label="a")])],
        person_only=True, kind="mode"), 5)
    assert answers[0].selected == ["a"] and answers[0].person
