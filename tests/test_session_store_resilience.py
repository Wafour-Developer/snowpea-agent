from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.session.manager import EventHub, SessionManager
from snowpea_core.session.session import Session
from snowpea_core.session.store import Store

pytestmark = pytest.mark.asyncio


async def test_store_connect_sets_wal_and_busy_timeout(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    try:
        journal = store._query("PRAGMA journal_mode")  # noqa: SLF001
        timeout = store._query("PRAGMA busy_timeout")  # noqa: SLF001
        assert str(journal[0][0]).lower() == "wal"
        assert int(timeout[0][0]) == 5000
    finally:
        store.close()


async def test_append_event_retries_transient_operational_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "state.db")
    sleeps: list[float] = []

    class FlakyConnection:
        row_factory: Any = None

        def __init__(self) -> None:
            self.calls = 0
            self.commits = 0

        def execute(self, *_args: Any) -> None:
            self.calls += 1
            if self.calls < 3:
                raise sqlite3.OperationalError("database is locked")

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            return None

    fake = FlakyConnection()
    try:
        store._conn = fake  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr("snowpea_core.session.store.time.sleep", sleeps.append)

        await store.append_event("s-retry", 1, "message.delta", {"text": "hi"}, "now")

        assert fake.calls == 3
        assert fake.commits == 1
        assert sleeps == [0.05, 0.1]
    finally:
        store.close()


async def test_commit_failure_recreates_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "state.db")

    class CommitFails:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, *_args: Any) -> None:
            return None

        def commit(self) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            self.closed = True

    class Recovered:
        def __init__(self) -> None:
            self.executed = False
            self.committed = False

        def execute(self, *_args: Any) -> None:
            self.executed = True

        def commit(self) -> None:
            self.committed = True

        def close(self) -> None:
            return None

    failing = CommitFails()
    recovered = Recovered()
    try:
        store._conn = failing  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr(store, "_connect", lambda: recovered)
        monkeypatch.setattr("snowpea_core.session.store.time.sleep", lambda _delay: None)

        await store.append_event("s-commit", 1, "message.delta", {"text": "hi"}, "now")

        assert failing.closed is True
        assert store._conn is recovered  # noqa: SLF001
        assert recovered.executed is True
        assert recovered.committed is True
    finally:
        store.close()


async def test_final_append_failure_logs_the_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store(tmp_path / "state.db")

    class AlwaysLocked:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, *_args: Any) -> None:
            self.calls += 1
            raise sqlite3.OperationalError("database is locked")

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    fake = AlwaysLocked()
    try:
        store._conn = fake  # type: ignore[assignment]  # noqa: SLF001
        monkeypatch.setattr("snowpea_core.session.store.time.sleep", lambda _delay: None)

        with caplog.at_level(logging.ERROR, logger="snowpea.session.store"):
            with pytest.raises(sqlite3.OperationalError):
                await store.append_event("s-final", 1, "message.delta", {"text": "hi"}, "now")

        assert fake.calls == 5
        assert "s-final" in caplog.text
    finally:
        store.close()


async def test_emit_delivers_event_when_store_append_fails(tmp_path: Path) -> None:
    class BrokenStore:
        async def append_event(self, *_args: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

    class Subscriber:
        def __init__(self) -> None:
            self.messages: list[tuple[str, dict[str, Any]]] = []

        async def notify(self, method: str, params: dict[str, Any]) -> None:
            self.messages.append((method, params))

    sessions = SessionManager(settings=None)
    session = Session(id="s-live", workdir=tmp_path)
    sessions._sessions[session.id] = session  # noqa: SLF001
    hub = EventHub(store=BrokenStore(), sessions=sessions)  # type: ignore[arg-type]
    subscriber = Subscriber()
    hub.subscribe(subscriber, session.id)

    event = await hub.emit(session.id, "message.delta", {"text": "still delivered"})

    assert session.seq == 1
    assert event.seq == 1
    assert subscriber.messages == [
        (
            "session.event",
            {
                "sessionId": "s-live",
                "seq": 1,
                "kind": "message.delta",
                "payload": {"text": "still delivered"},
                "ts": event.ts,
            },
        )
    ]


async def test_emit_delivers_event_when_store_is_closed(tmp_path: Path) -> None:
    from snowpea_core.session.store import StoreClosed

    class ClosedStore:
        async def append_event(self, *_args: Any) -> None:
            raise StoreClosed("session store is closed")

    sessions = SessionManager(settings=None)
    session = Session(id="s-closed", workdir=tmp_path)
    sessions._sessions[session.id] = session  # noqa: SLF001
    hub = EventHub(store=ClosedStore(), sessions=sessions)  # type: ignore[arg-type]

    event = await hub.emit(session.id, "message.delta", {"text": "delivered on closed store"})

    assert session.seq == 1
    assert event.seq == 1

