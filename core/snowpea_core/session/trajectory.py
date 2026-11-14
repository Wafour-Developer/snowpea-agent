"""The Trajectory: a session's event log as turn-aware rows (M17 2b).

A pure projection, like dsh's ``ui-trajectory``: the rows are folded from the
same stored events the chat replays, so the two can never disagree.  Each row
points back at the event it came from (``seq``) and, for a call and its result,
at both (``seq``/``endSeq``).

Row kinds:

* ``turn`` — one per turn: what started it, how it ended;
* ``user`` — the user's message (``steered`` when typed mid-turn);
* ``message`` — one model call (a *step*): its text, reasoning length, timing
  and usage (from ``step.done``, 1.44); before 1.44, one per ``message.done``;
* ``tool`` — one tool call with its result;
* ``subagent`` — a delegation, with the child session to open;
* ``compacted`` — a compaction, before and after;
* ``context`` — something the daemon fed the model (a stop-chain nudge, the host note);
* ``system`` — errors, retries, model and mode changes.

The fold is incremental: :class:`TrajectoryBuilder` keeps its rows and the last
``seq`` it applied, so a later read only folds what was appended since.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Event kinds the fold reads; everything else (deltas above all) is skipped.
KINDS: frozenset[str] = frozenset(
    {
        "turn.started",
        "turn.done",
        "message.user",
        "message.done",
        "step.started",
        "step.done",
        "tool.call",
        "tool.result",
        "diff",
        "subagent.spawn",
        "subagent.done",
        "compaction",
        "hook.continue",
        "context.host",
        "error",
        "provider.retry",
        "model.changed",
        "mode.changed",
        "goal.changed",
        "session.forked",
    }
)

#: Characters of a tool result or a message kept in a row; the full text is in
#: the event the row points at.
PREVIEW_CHARS = 600


def _preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _first_line(text: Any, limit: int = 120) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return _preview(line, limit)


@dataclass
class Row:
    kind: str
    seq: int
    ts: str
    turnId: str | None = None
    step: int | None = None
    title: str = ""
    text: str = ""
    endSeq: int | None = None
    endTs: str | None = None
    ok: bool | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "seq": self.seq,
            "ts": self.ts,
            "turnId": self.turnId,
            "step": self.step,
            "title": self.title,
        }
        if self.text:
            out["text"] = self.text
        if self.endSeq is not None:
            out["endSeq"] = self.endSeq
            out["endTs"] = self.endTs
        if self.ok is not None:
            out["ok"] = self.ok
        if self.data:
            out["data"] = self.data
        return out


@dataclass
class TrajectoryBuilder:
    """Folds events into rows; keeps its place so a later read is incremental."""

    rows: list[Row] = field(default_factory=list)
    last_seq: int = 0
    _tools: dict[str, Row] = field(default_factory=dict)
    _steps: dict[tuple[str | None, int], Row] = field(default_factory=dict)
    _turns: dict[str, Row] = field(default_factory=dict)
    _agents: dict[str, Row] = field(default_factory=dict)
    #: Turns that report steps (1.44+): their ``message.done`` is not a row of its own.
    _stepped: set[str | None] = field(default_factory=set)

    def apply_all(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            self.apply(event)

    def apply(self, event: dict[str, Any]) -> None:
        seq = int(event.get("seq") or 0)
        if seq <= self.last_seq:
            return
        self.last_seq = seq
        kind = str(event.get("kind") or "")
        if kind not in KINDS:
            return
        payload = event.get("payload") or {}
        ts = str(event.get("ts") or "")
        turn = event.get("turnId") or payload.get("turnId")
        step = event.get("step")
        handler = getattr(self, "_" + kind.replace(".", "_"), None)
        if handler is not None:
            handler(seq, ts, turn, step, payload)

    def _add(self, row: Row) -> Row:
        self.rows.append(row)
        return row

    # -- turns -------------------------------------------------------------

    def _turn_started(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = Row(
            "turn",
            seq,
            ts,
            turnId=turn,
            title=_first_line(p.get("prompt")) or str(p.get("reason") or "turn"),
            data={"initiator": p.get("initiator"), "reason": p.get("reason")},
        )
        if turn:
            self._turns[str(turn)] = row
        self._add(row)

    def _turn_done(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = self._turns.get(str(turn)) if turn else None
        if row is None:
            return
        row.endSeq, row.endTs = seq, ts
        row.ok = p.get("reason") == "complete"
        row.data["outcome"] = p.get("reason")

    def _message_user(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        self._add(
            Row(
                "user",
                seq,
                ts,
                turnId=turn,
                step=step,
                title=_first_line(p.get("text")),
                text=_preview(p.get("text"), 4000),
                data={"steered": True} if p.get("steered") else {},
            )
        )

    # -- model calls -------------------------------------------------------

    def _step_started(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        number = int(p.get("step") or step or 0)
        self._stepped.add(turn)
        row = Row(
            "message",
            seq,
            ts,
            turnId=turn,
            step=number,
            title=f"{p.get('provider') or ''}:{p.get('model') or ''}".strip(":"),
        )
        self._steps[(turn, number)] = row
        self._add(row)

    def _step_done(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        number = int(p.get("step") or step or 0)
        row = self._steps.get((turn, number))
        if row is None:
            row = self._add(Row("message", seq, ts, turnId=turn, step=number))
        row.endSeq, row.endTs = seq, ts
        row.ok = p.get("outcome") not in ("error",)
        row.text = _preview(p.get("text"), 4000)
        row.data.update(
            {
                "outcome": p.get("outcome"),
                "ttftMs": p.get("ttftMs"),
                "decodeMs": p.get("decodeMs"),
                "totalMs": p.get("totalMs"),
                "inputTokens": p.get("inputTokens"),
                "cachedInputTokens": p.get("cachedInputTokens"),
                "outputTokens": p.get("outputTokens"),
                "reasoningChars": len(str(p.get("reasoning") or "")),
                "toolCalls": p.get("toolCalls"),
            }
        )
        if p.get("error"):
            row.data["error"] = p.get("error")

    def _message_done(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        if p.get("role") == "system":
            # A compaction summary or a system note.
            summary = Row("context", seq, ts, turnId=turn, step=step, title="summary")
            summary.text = _preview(p.get("text"))
            self._add(summary)
            return
        if turn in self._stepped:
            return  # step.done already holds this text
        self._add(
            Row(
                "message",
                seq,
                ts,
                turnId=turn,
                step=step,
                title=_first_line(p.get("text")),
                text=_preview(p.get("text"), 4000),
            )
        )

    # -- tools and delegation ----------------------------------------------

    def _tool_call(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        args = p.get("args") or {}
        hint = ""
        if isinstance(args, dict):
            for key in ("path", "command", "query", "url", "pattern", "name", "task"):
                if args.get(key):
                    hint = _first_line(args.get(key), 100)
                    break
        row = Row(
            "tool",
            seq,
            ts,
            turnId=turn,
            step=step,
            title=f"{p.get('name')}" + (f" {hint}" if hint else ""),
            data={"name": p.get("name"), "callId": p.get("callId")},
        )
        if p.get("callId"):
            self._tools[str(p["callId"])] = row
        self._add(row)

    def _tool_result(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = self._tools.get(str(p.get("callId") or ""))
        if row is None:
            return
        row.endSeq, row.endTs = seq, ts
        row.ok = bool(p.get("ok"))
        row.text = _preview(p.get("output") if p.get("ok") else (p.get("error") or p.get("output")))

    def _diff(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = self._tools.get(str(p.get("callId") or ""))
        if row is not None:
            row.data.setdefault("paths", []).append(p.get("path"))

    def _subagent_spawn(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = Row(
            "subagent",
            seq,
            ts,
            turnId=turn,
            step=step,
            title=f"{p.get('name') or 'agent'}: {_first_line(p.get('title') or p.get('task'))}",
            data={"agentId": p.get("agentId"), "childSessionId": p.get("sessionId")},
        )
        if p.get("agentId"):
            self._agents[str(p["agentId"])] = row
        self._add(row)

    def _subagent_done(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = self._agents.get(str(p.get("agentId") or ""))
        if row is None:
            return
        row.endSeq, row.endTs = seq, ts
        row.ok = bool(p.get("ok"))
        row.text = _preview(p.get("summary") or p.get("result"))
        row.data.update(
            {
                "reason": p.get("reason"),
                "rounds": p.get("rounds"),
                "childSessionId": p.get("sessionId") or row.data.get("childSessionId"),
            }
        )

    # -- the rest ----------------------------------------------------------

    def _compaction(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        self._add(
            Row(
                "compacted",
                seq,
                ts,
                turnId=turn,
                step=step,
                title=f"{p.get('before')} → {p.get('after')} tokens",
                data={k: p.get(k) for k in ("before", "after", "summaryChars", "auto", "kept")},
            )
        )

    def _hook_continue(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = Row("context", seq, ts, turnId=turn, step=step, title="kept going")
        row.text = _preview(p.get("reason"))
        self._add(row)

    def _context_host(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        title = f"host note ({p.get('chars')} chars)"
        self._add(Row("context", seq, ts, turnId=turn, step=step, title=title))

    def _system(self, seq: int, ts: str, turn: Any, step: Any, title: str, text: str = "") -> None:
        self._add(Row("system", seq, ts, turnId=turn, step=step, title=title, text=_preview(text)))

    def _error(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        self._system(seq, ts, turn, step, f"error {p.get('code')}", p.get("message") or "")

    def _provider_retry(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        self._system(
            seq, ts, turn, step, f"retry {p.get('attempt')}/{p.get('max')}", p.get("error") or ""
        )

    def _model_changed(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        self._system(seq, ts, turn, step, f"model {p.get('provider')}:{p.get('model')}")

    def _mode_changed(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        self._system(seq, ts, turn, step, f"mode {p.get('mode')}")

    def _goal_changed(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        goal = p.get("goal") or {}
        title = f"goal {goal.get('phase')}" if goal else "goal cleared"
        self._system(seq, ts, turn, step, title, goal.get("objective") or "")

    def _session_forked(self, seq: int, ts: str, turn: Any, step: Any, p: dict) -> None:
        row = Row("context", seq, ts, title="history above is inherited")
        row.data = {"fromSessionId": p.get("fromSessionId"), "atTurnId": p.get("atTurnId")}
        self._add(row)


def build(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows for ``events`` from scratch (tests and one-off reads)."""
    builder = TrajectoryBuilder()
    builder.apply_all(events)
    return [row.wire() for row in builder.rows]


__all__ = ["KINDS", "PREVIEW_CHARS", "Row", "TrajectoryBuilder", "build"]
