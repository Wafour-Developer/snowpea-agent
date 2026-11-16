"""Security review 8, part A, H8-1 / M8-4 / L8-1: a decision never uses bytes the
tamper guard did not verify, and the daemon never takes foreign content as its own.

Every test is deterministic: a write "by another program" is made at an exact
point between the guard's look and the use (or between the daemon's write and
the guard's record of it) by wrapping the seam, never by racing threads.  All
files live under pytest's ``tmp_path``; nothing runs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from snowpea_core.config.settings import Settings
from snowpea_core.security import tamper
from snowpea_core.security.tamper import FileGuard, SqliteGuard
from snowpea_core.trust import projects as trust
from snowpea_core.trust.projects import TrustStore

pytestmark = [pytest.mark.real_project_trust]


def _hooks(project: Path, command: str = "echo hi") -> None:
    path = project / ".snowpea" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}}),
        encoding="utf-8")


def _trust(store: TrustStore, project: Path) -> None:
    store.set(project, None, by="ide", digests=store.status(project)["digests"])


def _forged(store: TrustStore, project: Path) -> bytes:
    """The record as the daemon wrote it, plus a trust entry for ``project``."""
    data = json.loads(store.path.read_bytes())
    data["projects"][str(trust.project_root(project))] = {
        "scopes": ["hooks"], "by": "ide", "at": "2026-01-01T00:00:00Z",
        "files": {".snowpea/hooks.json": trust.digest_of(project / ".snowpea" / "hooks.json")}}
    return json.dumps(data).encode("utf-8")


def _after_look(monkeypatch: pytest.MonkeyPatch, path: Path, act: Any) -> list[int]:
    """Run ``act()`` once, right after the guard of ``path`` looked at the disk."""
    done: list[int] = []
    original = FileGuard._look

    def look(self: FileGuard) -> Any:
        result = original(self)
        if self.path == path and not done:
            done.append(1)
            act()
        return result

    monkeypatch.setattr(FileGuard, "_look", look)
    return done


@pytest.fixture
def store(tmp_path: Path) -> TrustStore:
    made = TrustStore(tmp_path / "home")
    made.tamper_listeners[:] = []
    return made


# -- the trust record -------------------------------------------------------------------


def test_a_record_written_between_the_look_and_the_use_is_not_read(
        store: TrustStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    _hooks(mine)
    _hooks(theirs, "echo planted")
    _trust(store, mine)
    told: list[tuple[str, str]] = []
    store.tamper_listeners[:] = [lambda where, happened, *_: told.append((where, happened))]
    store.guard()
    forged = _forged(store, theirs)
    done = _after_look(monkeypatch, store.path, lambda: store.path.write_bytes(forged))
    # The look found the daemon's copy; the forged bytes land before the use.
    assert not store.active(theirs, "hooks", ".snowpea/hooks.json")
    assert done == [1] and told == []
    # The next look sees them, puts the daemon's copy back and tells.
    assert not store.active(theirs, "hooks", ".snowpea/hooks.json")
    assert told == [(str(store.path), "changed")]
    assert theirs.name not in store.path.read_text(encoding="utf-8")
    assert store.active(mine, "hooks", ".snowpea/hooks.json")


def test_a_forged_record_landing_after_the_daemons_own_write_is_not_adopted(
        store: TrustStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_mutate``: the copy is the bytes the daemon wrote, not what is read back."""
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    _hooks(mine)
    _hooks(theirs, "echo planted")
    store.guard()
    real_write = trust.write_atomic
    planted: list[bytes] = []

    def write(path: Any, data: Any, **kw: Any) -> Any:
        info = real_write(path, data, **kw)
        if Path(path) == store.path and not planted:
            # Another program swaps the file right after the daemon's rename.
            forged = json.loads(data)
            forged["projects"][str(trust.project_root(theirs))] = {
                "scopes": ["hooks"], "files": {".snowpea/hooks.json": trust.digest_of(
                    theirs / ".snowpea" / "hooks.json")}}
            planted.append(json.dumps(forged).encode("utf-8"))
            Path(path).write_bytes(planted[0])
        return info

    monkeypatch.setattr(trust, "write_atomic", write)
    _trust(store, mine)
    assert planted
    monkeypatch.setattr(trust, "write_atomic", real_write)
    assert not store.active(theirs, "hooks", ".snowpea/hooks.json")
    assert store.active(mine, "hooks", ".snowpea/hooks.json")
    assert str(trust.project_root(theirs)) not in json.loads(store.path.read_bytes())["projects"]
    # A later change of the daemon's own starts from its copy, not the forged file.
    store.revoke(mine, by="ide")
    assert json.loads(store.path.read_bytes())["projects"] == {}


def test_the_marker_and_the_legacy_file_are_used_as_verified(
        store: TrustStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    _hooks(project)
    assert store.migrate([]) == []                # writes the marker
    _trust(store, project)
    store.guard()
    record = store.path

    def lose_both() -> None:
        record.unlink()
        store.marker.unlink()

    _after_look(monkeypatch, store.legacy, lose_both)
    assert store.active(project, "hooks", ".snowpea/hooks.json")
    assert store.problem() is None and store.migrated()
    monkeypatch.undo()
    assert record.exists() and store.marker.exists()
    # A legacy file planted after its look grants nothing.
    outsider = tmp_path / "x"

    def plant() -> None:
        store.legacy.parent.mkdir(parents=True, exist_ok=True)
        store.legacy.write_text(json.dumps({"projects": {str(outsider): {"trusted": True}}}),
                                encoding="utf-8")

    _after_look(monkeypatch, store.legacy, plant)
    assert store.scopes(outsider) == []
    monkeypatch.undo()
    assert store.scopes(outsider) == [] and not store.legacy.exists()


def test_without_a_daemon_a_linked_record_is_not_read(store: TrustStore,
                                                     tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _hooks(project)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"projects": {str(trust.project_root(project)): {
        "scopes": ["hooks"], "files": {".snowpea/hooks.json": trust.digest_of(
            project / ".snowpea" / "hooks.json")}}}}), encoding="utf-8")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.symlink_to(elsewhere)
    assert not store.active(project, "hooks", ".snowpea/hooks.json")
    # A daemon's write does not follow it either: the link is refused.
    with pytest.raises(OSError):
        _trust(store, project)
    assert "projects" in elsewhere.read_text(encoding="utf-8")


# -- the delegation store ----------------------------------------------------------------


def _engine(home: Path, clock: list[datetime] | None = None) -> Any:
    from snowpea_core.jarvis.delegation import DelegationEngine

    if clock is None:
        return DelegationEngine(home, Settings())
    return DelegationEngine(home, Settings(), now=lambda: clock[0])


def _caller() -> Any:
    from snowpea_core.jarvis.delegation import Caller

    return Caller(kind="connection", surface="ide")


def _foreign(path: Path, sql: str) -> None:
    other = sqlite3.connect(path)
    try:
        other.execute(sql)
        other.commit()
    finally:
        other.close()


def _after_check(monkeypatch: pytest.MonkeyPatch, act: Any) -> list[int]:
    done: list[int] = []
    original = SqliteGuard.check

    def check(self: SqliteGuard) -> bool:
        result = original(self)
        if not done:
            done.append(1)
            act()
        return result

    monkeypatch.setattr(SqliteGuard, "check", check)
    return done


def _after_write(monkeypatch: pytest.MonkeyPatch, act: Any) -> list[int]:
    """Run ``act()`` right after the daemon's own write reached the disk."""
    done: list[int] = []
    original = tamper.write_atomic

    def write(path: Any, data: Any, **kw: Any) -> Any:
        info = original(path, data, **kw)
        if not done:
            done.append(1)
            act()
        return info

    monkeypatch.setattr(tamper, "write_atomic", write)
    return done


def test_a_commit_between_the_check_and_the_query_is_not_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(tmp_path / "home")
    engine.guard()
    grant = engine.set_grant("global", None, "full", [], caller=_caller())[0]
    engine.revoke(grant.id, caller=_caller())
    done = _after_check(monkeypatch, lambda: _foreign(
        engine.db_path, "UPDATE grants SET revoked_at = NULL"))
    assert engine.effective().source == "default"
    assert done == [1]
    monkeypatch.undo()
    assert engine.effective().source == "default"
    assert [g.id for g in engine.list_grants()] == []
    engine.close()


@pytest.mark.parametrize("path", ["end_session", "sweep_expired", "pause_expiry", "takeover"])
def test_a_write_racing_the_daemons_own_write_is_never_its_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The writes that need no person (session end, expiry, a pause ending, a
    takeover) cannot be used to make a forged store the daemon's own."""
    clock = [datetime(2026, 11, 16, 12, 0, tzinfo=UTC)]
    engine = _engine(tmp_path / "home", clock)
    engine.guard()
    narrow = engine.set_grant("global", None, "staged", [], caller=_caller())[0]
    engine.set_grant("session", "s-1", "staged", [], caller=_caller(),
                     expires_at=(clock[0] + timedelta(minutes=5)).isoformat())
    engine.takeover("s-2", minutes=1)
    clock[0] += timedelta(minutes=10)
    forge = f"UPDATE grants SET level = 'full' WHERE id = '{narrow.id}'"
    done = _after_write(monkeypatch, lambda: _foreign(engine.db_path, forge))
    if path == "end_session":
        engine.end_session("s-1")
    elif path == "sweep_expired":
        engine.sweep_expired()
    elif path == "pause_expiry":
        assert engine.pause_state("s-2").paused is False
    else:
        engine.takeover("s-3")
    assert done == [1], "the daemon wrote"
    monkeypatch.undo()
    assert engine.effective().level == "staged"
    on_disk = sqlite3.connect(engine.db_path)
    try:
        level = on_disk.execute("SELECT level FROM grants WHERE id = ?",
                                (narrow.id,)).fetchone()[0]
    finally:
        on_disk.close()
    assert level == "staged"
    engine.close()


def test_a_journal_left_next_to_the_store_is_never_replayed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = _engine(home)
    grant = first.set_grant("global", None, "full", [], caller=_caller())[0]
    first.close()
    db_path = home / "jarvis" / "delegation.db"
    # A rollback journal made while the grant was live, kept by another program.
    saved = tmp_path / "saved-journal"
    other = sqlite3.connect(db_path, isolation_level=None)
    other.execute("PRAGMA journal_mode=DELETE")
    other.execute("PRAGMA cache_size=1")          # pages spill: the journal is complete
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE grants SET note = 'x'")  # the grants page goes into the journal
    for index in range(40):
        other.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (f"k{index}", "y" * 3000))
    journal = db_path.with_name(db_path.name + "-journal")
    saved.write_bytes(journal.read_bytes())
    other.execute("ROLLBACK")
    other.close()
    second = _engine(home)
    second.revoke(grant.id, caller=_caller())
    second.close()
    # Control: plain SQLite would replay it and bring the revoked grant back.
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "delegation.db").write_bytes(db_path.read_bytes())
    (copy / "delegation.db-journal").write_bytes(saved.read_bytes())
    plain = sqlite3.connect(copy / "delegation.db")
    try:
        assert plain.execute("SELECT revoked_at FROM grants").fetchone()[0] is None
    finally:
        plain.close()
    journal.write_bytes(saved.read_bytes())       # planted while the daemon was down
    daemon = _engine(home)
    daemon.guard()
    assert not journal.exists()
    assert daemon.effective().source == "default"
    daemon.close()
    # Planted while it runs: removed by the next look, still not read.
    journal.write_bytes(saved.read_bytes())
    daemon = _engine(home)
    daemon.guard()
    journal.write_bytes(saved.read_bytes())
    assert daemon.effective().source == "default" and not journal.exists()
    daemon.close()
    # Without a daemon (a CLI): removed before the store is opened.
    journal.write_bytes(saved.read_bytes())
    cli = _engine(home)
    assert cli.effective().source == "default" and not journal.exists()
    cli.close()


def test_the_store_is_compared_on_a_slow_cadence_even_when_the_stat_did_not_move(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(tmp_path / "home")
    engine.guard()
    engine.set_grant("global", None, "staged", [], caller=_caller())
    assert engine.effective().level == "staged"
    guard = engine._guard
    calls: list[int] = []
    real = tamper._read

    def counted(path: Path) -> Any:
        calls.append(1)
        return real(path)

    monkeypatch.setattr(tamper, "_read", counted)
    monkeypatch.setattr(tamper, "_fresh", lambda signature: False)
    guard.check()                                 # settles the stat
    calls.clear()
    guard.check()
    assert calls == []                            # stat unchanged: no read
    monkeypatch.setattr(guard, "_compared", guard._compared - tamper.CADENCE_S - 1)
    guard.check()
    assert calls == [1]                           # a minute on: the content is compared
    engine.close()


# -- L8-1: notices -------------------------------------------------------------------------


def test_repeated_changes_are_told_once_and_then_once_a_minute_with_a_count(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [1000.0]
    monkeypatch.setattr(tamper.time, "monotonic", lambda: moment[0])
    path = tmp_path / "home" / "record.json"
    path.parent.mkdir(parents=True)
    path.write_text("one", encoding="utf-8")
    guard = FileGuard(path, home=tmp_path / "home")
    told: list[tuple[str, dict[str, Any]]] = []
    guard.listeners.append(lambda where, happened, note: told.append((happened, note)))
    guard.arm()
    for text in ("two", "three", "four"):
        path.write_text(text, encoding="utf-8")
        assert guard.check() is True
        assert path.read_text(encoding="utf-8") == "one"
    assert told == [("changed", {"count": 1, "restored": True})]
    moment[0] += tamper.NOTICE_WINDOW_S
    guard.check()                                 # nothing new, but the summary is due
    assert told[-1] == ("changed", {"count": 2, "restored": True})
    lines = (tmp_path / "home" / "logs" / "security.jsonl").read_text().splitlines()
    assert [json.loads(line)["count"] for line in lines] == [1, 2]


def test_a_notice_does_not_claim_a_restore_that_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from snowpea_core.server import trust_handlers

    path = tmp_path / "home" / "trust" / "projects.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    guard = FileGuard(path, home=tmp_path / "home")
    told: list[dict[str, Any]] = []
    guard.listeners.append(lambda where, happened, note: told.append(note))
    guard.arm()

    def broken(*_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(tamper, "write_atomic", broken)
    path.write_text('{"projects": {}}', encoding="utf-8")
    assert guard.verified() == b"{}"              # the decision still uses the daemon's copy
    assert told == [{"count": 1, "restored": False}]
    trust_handlers._TAMPERED.clear()
    core = type("C", (), {})()
    store = TrustStore(tmp_path / "home2")
    trust_handlers._guard(core, store)
    store.tamper_listeners[0](str(path), "changed", {"count": 3, "restored": False})
    line = trust_handlers.tamper_problems()[-1]
    assert "put back" not in line.replace("putting the file back failed", "")
    assert "3 times" in line and "failed" in line
    trust_handlers._TAMPERED.clear()


def test_the_trust_store_keeps_no_disk_read_after_the_guard(tmp_path: Path) -> None:
    """Reading for a decision goes through the guard only: the module's own
    JSON reader is not used on the record, the marker or the legacy file."""
    import inspect

    source = inspect.getsource(TrustStore)
    for name in ("self.path", "self.marker", "self.legacy"):
        assert f"_read_json({name})" not in source
    assert ".exists()" not in inspect.getsource(TrustStore.read)


# -- L7-3, verified: nested projects, the innermost wins -------------------------------------


def test_of_two_nested_projects_agents_the_innermost_wins(tmp_path: Path) -> None:
    """``skills/loader.SkillLoader.agents_for`` (the ``_depth`` comparison): an agent
    both projects define is the inner one's; commands are pinned by round 7's
    ``test_an_outer_projects_entries_need_its_trust_inside_a_nested_project``."""
    from types import SimpleNamespace

    from snowpea_core.config.paths import Paths
    from snowpea_core.skills.loader import SkillLoader

    home = tmp_path / "home"
    trust.set_default_home(home)
    outer = tmp_path / "outer"
    inner = outer / "pkg" / "inner"
    for base, text in ((outer, "Outer."), (inner, "Inner.")):
        path = base / ".snowpea" / "agents" / "helper.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: helper\ndescription: d\n---\n\n{text}\n", encoding="utf-8")
        store = trust.store_for(home)
        store.set(base, ["agents"], by="ide", digests=store.status(base)["digests"])
    core = SimpleNamespace(paths=Paths.create(home), settings=Settings())
    loader = SkillLoader(core, workdirs=[outer, inner])
    loader.scan()
    chosen = [agent for agent in loader.agents_for(inner) if agent.project is not None]
    assert [(agent.name, Path(agent.project)) for agent in chosen] == [
        ("helper", trust.project_root(inner))]
    chosen = [agent for agent in loader.agents_for(outer) if agent.project is not None]
    assert [Path(agent.project) for agent in chosen] == [trust.project_root(outer)]
