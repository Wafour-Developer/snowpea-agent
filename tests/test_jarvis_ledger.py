"""Jarvis mode (M19 WP2): the append-only ledger, its rotation and its pages."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from snowpea_core.jarvis.classify import ClassifyRequest, classify
from snowpea_core.jarvis.ledger import Ledger
from snowpea_core.server.protocol import JarvisGrant


def _decide(ledger: Ledger, n: int, **extra: object) -> str:
    entry = ledger.record_decision(
        kind="approval",
        decided_by=str(extra.pop("decided_by", "jarvis")),  # type: ignore[arg-type]
        summary=f"decision {n}",
        session_id=str(extra.pop("session_id", "s-1")),
        decision="allow",
        **extra,  # type: ignore[arg-type]
    )
    return entry.id


def test_records_are_appended_and_read_newest_first(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "jarvis" / "ledger.jsonl")
    ids = [_decide(ledger, n) for n in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5
    entries, cursor = ledger.decisions(limit=10)
    assert [e.id for e in entries] == list(reversed(ids))
    assert cursor is None
    assert entries[0].summary == "decision 4" and entries[0].decidedBy == "jarvis"
    mode = stat.S_IMODE(os.stat(ledger.path).st_mode)
    assert mode == 0o600


def test_paging_with_a_cursor_visits_every_entry_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ids = [_decide(ledger, n) for n in range(23)]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        entries, cursor = ledger.decisions(before=cursor, limit=5)
        seen.extend(e.id for e in entries)
        pages += 1
        if cursor is None:
            break
    assert pages == 5
    assert seen == list(reversed(ids))


def test_filters_by_session_and_decider(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    _decide(ledger, 1, session_id="a", decided_by="auto")
    _decide(ledger, 2, session_id="b", decided_by="jarvis")
    _decide(ledger, 3, session_id="a", decided_by="person")
    by_session, _ = ledger.decisions(session_id="a")
    assert [e.summary for e in by_session] == ["decision 3", "decision 1"]
    by_auto, _ = ledger.decisions(decided_by="auto")
    assert [e.summary for e in by_auto] == ["decision 1"]
    both, _ = ledger.decisions(session_id="a", decided_by="person")
    assert [e.summary for e in both] == ["decision 3"]


def test_rotation_keeps_a_bounded_set_and_pages_across_files(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", max_bytes=2048, keep=3)
    ids = [_decide(ledger, n, reason="x" * 200) for n in range(60)]
    files = ledger.files()
    assert [f.name for f in files] == ["ledger.jsonl", "ledger.1.jsonl", "ledger.2.jsonl",
                                        "ledger.3.jsonl"]
    assert not (tmp_path / "ledger.4.jsonl").exists()
    assert all(f.stat().st_size <= 2048 for f in files)
    seen: list[str] = []
    cursor: str | None = None
    while True:
        entries, cursor = ledger.decisions(before=cursor, limit=7)
        seen.extend(e.id for e in entries)
        if cursor is None:
            break
    # The newest entries survive, in order; the oldest rotated away.
    assert seen == list(reversed(ids))[: len(seen)]
    assert 0 < len(seen) < len(ids)


def test_ids_keep_growing_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    first = Ledger(path)
    last = _decide(first, 1)
    # A clock that went backwards must not reorder the ledger.
    lines = path.read_text().splitlines()
    record = json.loads(lines[-1])
    record["id"] = f"jl-{time.time_ns() + 3_600 * 10**9:019d}"  # an hour ahead
    path.write_text(json.dumps(record) + "\n")
    second = Ledger(path)
    newer = _decide(second, 2)
    assert newer > record["id"] > last


def test_broken_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    _decide(ledger, 1)
    with path.open("a") as handle:
        handle.write('{"type": "decision", "id": "jl-1"\n')  # torn write
        handle.write("not json\n")
        handle.write('{"type": "decision", "id": "jl-0000000000000000002", "kind": "nope"}\n')
    _decide(ledger, 2)
    entries, _ = ledger.decisions()
    assert [e.summary for e in entries] == ["decision 2", "decision 1"]


def test_a_bad_cursor_is_refused(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with pytest.raises(ValueError):
        ledger.decisions(before="yesterday")


def test_grants_and_pauses_are_recorded_but_not_decisions(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    grant = JarvisGrant(id="jg-1", scope="global", level="full", grantedBy="ide",
                        grantedAt="2026-11-15T00:00:00.000Z")
    ledger.record_grant("set", grant, by="ide")
    ledger.record_pause("takeover", "s-1", by="ide")
    _decide(ledger, 1)
    entries, _ = ledger.decisions()
    assert [e.summary for e in entries] == ["decision 1"]
    kinds = [r["type"] for r in ledger.entries()]
    assert kinds == ["decision", "pause", "grant"]
    grants, _ = ledger.page(types=("grant",))
    assert grants[0]["grant"]["id"] == "jg-1" and grants[0]["by"] == "ide"


def test_a_verdict_fills_the_audit_fields(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    verdict = classify(ClassifyRequest(tool="shell", args={"command": "git push && rm -rf /"},
                                       tags=("exec",), workdir="/srv/p", home="/srv/h"))
    entry = ledger.record_decision(kind="escalate", decided_by="auto", summary="to the person",
                                   session_id="s", verdict=verdict, importance="high",
                                   tool="shell")
    assert entry.classification == "guarded" and entry.guardedClass == "deploy"
    assert entry.rule == "exec:git-push"
    raw = next(ledger.entries())
    assert raw["audit"]["guarded"] == ["deploy", "outside_destructive"]


def test_summaries_are_clipped_and_listeners_hear_every_record(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    heard: list[dict[str, object]] = []
    ledger.listeners.append(heard.append)
    ledger.listeners.append(lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
    entry = ledger.record_decision(kind="prompt", decided_by="jarvis", summary="y" * 500)
    assert len(entry.summary) <= 160
    assert heard and heard[0]["id"] == entry.id


def test_unknown_record_types_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Ledger(tmp_path / "ledger.jsonl").append({"type": "gossip"})
