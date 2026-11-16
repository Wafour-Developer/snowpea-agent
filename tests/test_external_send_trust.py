"""``external.send {trust}`` (1.54.0): continuing a ``host: none`` session in an
untrusted project carries the person's confirmation, with the errors of open/create."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from snowpea_core.external.base import ExternalError
from snowpea_core.external.manager import ExternalManager
from snowpea_core.server import errors
from snowpea_core.server.protocol import ExternalSendParams


class _Trust:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def trusted(self, key: str) -> bool:
        return key in self.keys

    def trust(self, key: str, *, by: str) -> None:
        del by
        self.keys.add(key)


def _manager() -> Any:
    manager = ExternalManager.__new__(ExternalManager)
    manager.trust = _Trust()  # type: ignore[attr-defined]
    return manager


PERSON = SimpleNamespace(authenticated=True, closed=False, client_kind="ide", remote_login=False)
BROWSER = SimpleNamespace(authenticated=True, closed=False, client_kind="browser",
                          remote_login=False)


def test_the_send_params_carry_trust() -> None:
    params = ExternalSendParams.model_validate(
        {"ref": {"cli": "claude", "id": "abc"}, "text": "hi", "trust": True})
    assert params.trust is True
    assert ExternalSendParams.model_validate(
        {"ref": {"cli": "claude", "id": "abc"}, "text": "hi"}).trust is False


@pytest.mark.parametrize(
    ("conn", "trust", "reason"),
    [
        (PERSON, False, "untrusted_project"),
        (BROWSER, True, "untrusted_project_person_only"),
        (BROWSER, False, "untrusted_project_person_only"),
    ],
)
def test_the_same_errors_as_open_and_create(conn: Any, trust: bool, reason: str) -> None:
    with pytest.raises(ExternalError) as caught:
        _manager()._require_trust("/srv/p", "/srv/p", conn, trust)
    assert caught.value.code == errors.EXTERNAL_UNAVAILABLE
    assert caught.value.details["reason"] == reason


async def test_send_hands_trust_to_the_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager()
    seen: dict[str, Any] = {}

    async def load(_cli: str, _sid: str) -> Any:
        return SimpleNamespace(), None

    async def send(_record: Any, _found: Any, _text: str, **kwargs: Any) -> dict[str, str]:
        seen.update(kwargs)
        return {"turnId": "xt-1"}

    monkeypatch.setattr(manager, "_load", load)
    monkeypatch.setattr(manager, "_send", send)
    assert await manager.send("claude", "abc", "hi", conn=PERSON, trust=True) == {"turnId": "xt-1"}
    assert seen["trust"] is True
