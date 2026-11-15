"""The Jarvis ledger: an append-only record of everything decided for a session.

``$SNOWPEA_HOME/jarvis/ledger.jsonl`` holds one JSON object per line, of three
types:

``decision``
    A :class:`~snowpea_core.server.protocol.JarvisDecision`: an approval or
    question answered by Jarvis, the person or the engine alone (``auto``), a
    stop resumed, an escalation, an external session opened unrestricted.
    These are what ``jarvis.ledger`` pages through.
``grant``
    A delegation grant set, replaced, revoked or expired, with the grant as it
    then stood and who did it.
``pause``
    The person stepping in (typing, a takeover) or handing back.

Lines are only ever appended.  When the file passes ``max_bytes`` it is
rotated (``ledger.jsonl`` -> ``ledger.1.jsonl`` -> … -> ``ledger.<keep>.jsonl``,
the oldest dropped), and the reader pages across every file, newest first.
Entry ids grow strictly (``jl-`` + nanoseconds), so an id is also the cursor
``nextBefore`` hands back.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

from snowpea_core.config.paths import utc_now
from snowpea_core.server.protocol import ExternalRef, JarvisDecision, JarvisGrant

log = logging.getLogger("snowpea.jarvis.ledger")

EntryType = Literal["decision", "grant", "pause"]
DecisionKind = Literal[
    "approval", "question", "continue", "prompt", "interrupt", "escalate", "unrestricted"
]
DecidedBy = Literal["jarvis", "person", "auto"]

#: Size at which ``ledger.jsonl`` is rotated.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
#: Rotated files kept besides the live one.
DEFAULT_KEEP = 5
#: Longest ``summary`` stored (it has to fit an OS notification).
SUMMARY_MAX = 160

_ID_PREFIX = "jl-"
_ID_DIGITS = 19

Listener = Callable[[dict[str, Any]], None]


def _id_number(entry_id: str) -> int | None:
    if not entry_id.startswith(_ID_PREFIX):
        return None
    digits = entry_id[len(_ID_PREFIX) :]
    return int(digits) if digits.isdigit() else None


class Ledger:
    """Append-only JSONL ledger with rotation and a paged, filtered reader."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.keep = max(1, int(keep))
        self._lock = threading.Lock()
        self._last = 0
        self._primed = False
        #: Called with every record after it is written (the server turns
        #: ``decision`` records into ``jarvis.decision`` notifications).
        self.listeners: list[Listener] = []

    # -- files -------------------------------------------------------------
    def files(self) -> list[Path]:
        """The live file, then the rotated ones, newest first (only those that exist)."""
        found = [self.path] + [self._rotated(n) for n in range(1, self.keep + 1)]
        return [path for path in found if path.exists()]

    def _rotated(self, number: int) -> Path:
        return self.path.with_name(f"{self.path.stem}.{number}{self.path.suffix}")

    def _rotate(self) -> None:
        oldest = self._rotated(self.keep)
        with contextlib.suppress(FileNotFoundError):
            oldest.unlink()
        for number in range(self.keep - 1, 0, -1):
            source = self._rotated(number)
            if source.exists():
                source.replace(self._rotated(number + 1))
        if self.path.exists():
            self.path.replace(self._rotated(1))

    def _prime(self) -> None:
        """Pick up the newest id on disk so ids keep growing across restarts."""
        if self._primed:
            return
        self._primed = True
        for path in self.files():
            for record in _read_reversed(path):
                number = _id_number(str(record.get("id", "")))
                if number is not None:
                    self._last = max(self._last, number)
                    return

    def _next_id(self) -> str:
        number = max(time.time_ns(), self._last + 1)
        self._last = number
        return f"{_ID_PREFIX}{number:0{_ID_DIGITS}d}"

    # -- writing -----------------------------------------------------------
    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Write one record (``type`` required); returns it with ``id`` and ``ts``."""
        if record.get("type") not in ("decision", "grant", "pause"):
            raise ValueError(f"unknown ledger record type {record.get('type')!r}")
        with self._lock:
            self._prime()
            stored = dict(record)
            stored["id"] = self._next_id()
            stored.setdefault("ts", utc_now())
            line = json.dumps(stored, ensure_ascii=False, separators=(",", ":")) + "\n"
            data = line.encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(self.path.parent, 0o700)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data) > self.max_bytes:
                self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        for listener in list(self.listeners):
            try:
                listener(stored)
            except Exception:  # noqa: BLE001 - a listener never breaks the record
                log.warning("ledger listener failed", exc_info=True)
        return stored

    def record_decision(
        self,
        *,
        kind: DecisionKind,
        decided_by: DecidedBy,
        summary: str,
        session_id: str | None = None,
        external: ExternalRef | dict[str, str] | None = None,
        project: str | None = None,
        request_id: str | None = None,
        tool: str | None = None,
        decision: str | None = None,
        classification: str | None = None,
        guarded_class: str | None = None,
        rule: str | None = None,
        grant_id: str | None = None,
        reason: str = "",
        importance: str = "low",
        verdict: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> JarvisDecision:
        """Append one decision and return it as the wire model.

        ``verdict`` (a :class:`~snowpea_core.jarvis.classify.Verdict`) fills
        ``classification``, ``guardedClass`` and ``rule`` when they are not
        given.  ``extra`` is stored alongside for the audit trail (all guarded
        classes, every matching rule) but not sent on the wire.
        """
        if verdict is not None:
            classification = classification or verdict.classification
            guarded_class = guarded_class or verdict.guarded_class
            rule = rule or verdict.rule
            extra = {"guarded": list(verdict.guarded), "rules": list(verdict.rules),
                     **(extra or {})}
        ref = external.model_dump() if isinstance(external, ExternalRef) else external
        entry = JarvisDecision(
            id="pending",
            ts=utc_now(),
            sessionId=session_id,
            external=ref,  # type: ignore[arg-type]
            project=project,
            kind=kind,
            importance=importance,  # type: ignore[arg-type]
            requestId=request_id,
            tool=tool,
            summary=_clip(summary),
            decision=decision,
            decidedBy=decided_by,
            classification=classification,  # type: ignore[arg-type]
            guardedClass=guarded_class,  # type: ignore[arg-type]
            rule=rule,
            grantId=grant_id,
            reason=reason,
        )
        record = {"type": "decision", **entry.model_dump(mode="json", exclude={"id"})}
        if extra:
            record["audit"] = extra
        stored = self.append(record)
        return entry.model_copy(update={"id": stored["id"], "ts": stored["ts"]})

    def record_grant(self, action: str, grant: JarvisGrant, *, by: str) -> dict[str, Any]:
        """A grant ``set``, ``replaced``, ``revoked`` or ``expired``."""
        return self.append(
            {"type": "grant", "action": action, "by": by, "grant": grant.model_dump(mode="json")}
        )

    def record_pause(
        self,
        action: str,
        target: str,
        *,
        by: str,
        until: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """The person stepped in (``typing``, ``takeover``) or authority returned."""
        return self.append(
            {"type": "pause", "action": action, "target": target, "by": by, "until": until,
             "project": project}
        )

    # -- reading -----------------------------------------------------------
    def entries(self, types: tuple[str, ...] | None = None) -> Iterator[dict[str, Any]]:
        """Every readable record, newest first."""
        for path in self.files():
            for record in _read_reversed(path):
                if types is None or record.get("type") in types:
                    yield record

    def page(
        self,
        *,
        session_id: str | None = None,
        decided_by: str | None = None,
        before: str | None = None,
        limit: int = 50,
        types: tuple[str, ...] = ("decision",),
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of records, newest first, and the cursor for the next page.

        ``before`` is a cursor from a previous page (an entry id): only older
        entries are returned.  An unreadable cursor raises ``ValueError``.
        """
        limit = max(1, min(int(limit), 1000))
        bound: int | None = None
        if before:
            bound = _id_number(before)
            if bound is None:
                raise ValueError(f"not a ledger cursor: {before!r}")
        found: list[dict[str, Any]] = []
        more = False
        for record in self.entries(types):
            number = _id_number(str(record.get("id", "")))
            if number is None:
                continue
            if bound is not None and number >= bound:
                continue
            if session_id is not None and record.get("sessionId") != session_id:
                continue
            if decided_by is not None and record.get("decidedBy") != decided_by:
                continue
            if len(found) == limit:
                more = True
                break
            found.append(record)
        next_before = str(found[-1]["id"]) if more and found else None
        return found, next_before

    def decisions(
        self,
        *,
        session_id: str | None = None,
        decided_by: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> tuple[list[JarvisDecision], str | None]:
        """:meth:`page` of decisions as wire models (records that fail validation are skipped)."""
        records, next_before = self.page(
            session_id=session_id, decided_by=decided_by, before=before, limit=limit
        )
        entries: list[JarvisDecision] = []
        for record in records:
            fields = {k: v for k, v in record.items() if k not in ("type", "audit")}
            try:
                entries.append(JarvisDecision.model_validate(fields))
            except Exception:  # noqa: BLE001 - one bad line never hides the rest
                log.debug("skipping unreadable ledger entry %s", record.get("id"))
        return entries, next_before


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"


def _read_reversed(path: Path) -> Iterator[dict[str, Any]]:
    """The JSON records of one file, last line first; broken lines are skipped."""
    try:
        data = path.read_bytes()
    except OSError:
        return
    for raw in reversed(data.splitlines()):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            yield record


__all__ = [
    "DEFAULT_KEEP",
    "DEFAULT_MAX_BYTES",
    "DecidedBy",
    "DecisionKind",
    "EntryType",
    "Ledger",
]
