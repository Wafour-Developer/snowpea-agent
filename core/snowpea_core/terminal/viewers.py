"""Viewers of one terminal: attach modes, credit flow control, size arbitration (M37 §2.5).

* ``interactive`` viewers type and resize; ``observe`` viewers only watch.
* Each viewer may have at most :data:`CREDIT` bytes sent and not yet acked
  (``terminal.ack {bytes}``).  Output past that waits in the viewer's backlog;
  a backlog over :data:`MAX_BEHIND` is dropped and the viewer gets a repaint
  instead (``terminal.data {repaint: true}``) once it has credit again.
* The most recently active interactive viewer (attached, typed or resized) sets
  the size; with none attached the headless size applies.

Sending is done by the manager's ``send(conn, params)``: this module only
decides what goes to whom.
"""

from __future__ import annotations

import base64
import itertools
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CREDIT = 256 * 1024
MAX_BEHIND = 4 * 1024 * 1024
#: Largest single ``terminal.data`` payload (before base64).
MAX_CHUNK = 64 * 1024

_order = itertools.count(1)


@dataclass
class Viewer:
    conn: Any
    mode: str  # "interactive" | "observe"
    cols: int | None = None
    rows: int | None = None
    unacked: int = 0
    backlog: deque[bytes] = field(default_factory=deque)
    backlog_bytes: int = 0
    needs_repaint: bool = False
    #: Attaching: its repaint is not built yet, so live output (which the
    #: repaint will contain) is not sent to it.
    paused: bool = False
    active: int = field(default_factory=lambda: next(_order))

    @property
    def interactive(self) -> bool:
        return self.mode == "interactive"

    def touch(self) -> None:
        self.active = next(_order)


@dataclass(frozen=True)
class Delivery:
    """One ``terminal.data``: live bytes, or a repaint (``repaint=True``).

    ``seq`` is the stream offset just past this chunk (bytes so far), like
    ``terminal.attach``'s ``seq``: a chunk follows on when ``seq - len(data)``
    equals the previous ``seq``.
    """

    conn: Any
    data: bytes
    seq: int
    repaint: bool = False

    def params(self, terminal_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "terminalId": terminal_id,
            "seq": self.seq,
            "data": base64.b64encode(self.data).decode("ascii"),
        }
        if self.repaint:
            out["repaint"] = True
        return out


class ViewerSet:
    """The viewers of one terminal.  ``seq`` counts output bytes since the terminal started."""

    def __init__(self, repaint: Callable[[], bytes], *, credit: int = CREDIT,
                 max_behind: int = MAX_BEHIND, clock: Callable[[], float] = time.monotonic) -> None:
        self._viewers: dict[int, Viewer] = {}
        self._repaint = repaint
        self.credit = credit
        self.max_behind = max_behind
        self.seq = 0
        self._clock = clock

    # -- membership -----------------------------------------------------------
    def attach(self, conn: Any, mode: str, cols: int | None = None,
               rows: int | None = None) -> Viewer:
        viewer = self._viewers.get(id(conn))
        if viewer is None:
            viewer = Viewer(conn=conn, mode=mode, cols=cols, rows=rows)
            self._viewers[id(conn)] = viewer
        else:
            viewer.mode = mode
            viewer.backlog.clear()
            viewer.backlog_bytes = 0
            viewer.unacked = 0
            viewer.needs_repaint = False
            if cols and rows:
                viewer.cols, viewer.rows = cols, rows
        viewer.touch()
        return viewer

    def detach(self, conn: Any) -> bool:
        return self._viewers.pop(id(conn), None) is not None

    def get(self, conn: Any) -> Viewer | None:
        return self._viewers.get(id(conn))

    def __len__(self) -> int:
        return len(self._viewers)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(list(self._viewers.values()))

    @property
    def has_human(self) -> bool:
        """An interactive viewer is attached: its terminal answers the queries."""
        return any(v.interactive for v in self._viewers.values())

    # -- size -------------------------------------------------------------------
    def size_owner(self) -> Viewer | None:
        candidates = [v for v in self._viewers.values() if v.interactive and v.cols and v.rows]
        return max(candidates, key=lambda v: v.active) if candidates else None

    def resize(self, conn: Any, cols: int, rows: int) -> bool:
        """Record a viewer's size; True when that viewer is (now) the size owner."""
        viewer = self._viewers.get(id(conn))
        if viewer is None or not viewer.interactive:
            return False
        viewer.cols, viewer.rows = cols, rows
        viewer.touch()
        return True

    # -- data -------------------------------------------------------------------
    def broadcast(self, data: bytes) -> list[Delivery]:
        """Live output: what to send now (the rest waits for credit)."""
        start = self.seq
        self.seq += len(data)
        out: list[Delivery] = []
        for viewer in self._viewers.values():
            if viewer.needs_repaint or viewer.paused:
                continue
            if viewer.backlog or viewer.unacked >= self.credit:
                self._queue(viewer, data)
                continue
            room = self.credit - viewer.unacked
            now, later = data[:room], data[room:]
            viewer.unacked += len(now)
            out.extend(self._chunks(viewer.conn, now, start))
            if later:
                self._queue(viewer, later)
        return out

    def _queue(self, viewer: Viewer, data: bytes) -> None:
        viewer.backlog.append(data)
        viewer.backlog_bytes += len(data)
        if viewer.backlog_bytes > self.max_behind:
            viewer.backlog.clear()
            viewer.backlog_bytes = 0
            viewer.needs_repaint = True

    def ack(self, conn: Any, nbytes: int) -> list[Delivery]:
        viewer = self._viewers.get(id(conn))
        if viewer is None:
            return []
        viewer.unacked = max(0, viewer.unacked - max(0, int(nbytes)))
        return self._flush(viewer)

    def _flush(self, viewer: Viewer) -> list[Delivery]:
        out: list[Delivery] = []
        if viewer.needs_repaint:
            if viewer.unacked >= self.credit:
                return out
            data = self._repaint()
            viewer.needs_repaint = False
            viewer.unacked += len(data)
            out.append(Delivery(viewer.conn, data, self.seq, repaint=True))
            return out
        seq = self.seq - viewer.backlog_bytes
        while viewer.backlog and viewer.unacked < self.credit:
            chunk = viewer.backlog.popleft()
            room = self.credit - viewer.unacked
            now, later = chunk[:room], chunk[room:]
            viewer.backlog_bytes -= len(chunk)
            if later:
                viewer.backlog.appendleft(later)
                viewer.backlog_bytes += len(later)
            viewer.unacked += len(now)
            out.extend(self._chunks(viewer.conn, now, seq))
            seq += len(now)
        return out

    @staticmethod
    def _chunks(conn: Any, data: bytes, seq: int) -> list[Delivery]:
        out = []
        for offset in range(0, len(data), MAX_CHUNK):
            part = data[offset : offset + MAX_CHUNK]
            out.append(Delivery(conn, part, seq + offset + len(part)))
        return out
