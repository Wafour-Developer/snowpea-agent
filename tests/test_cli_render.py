"""Headless CLI exit-code and warning behaviour."""

from __future__ import annotations

from snowpea_core.cli.render import TurnTracker


def test_completed_turn_with_soft_approval_denial_exits_zero() -> None:
    tracker = TurnTracker(strict_approvals=False)
    tracker.event(
        {
            "kind": "error",
            "payload": {"code": "approval_denied", "message": "shell was not approved"},
        }
    )
    tracker.event({"kind": "turn.done", "payload": {"reason": "complete"}})
    assert tracker.exit_code() == 0
    assert tracker.warnings


def test_strict_approvals_still_exit_four() -> None:
    tracker = TurnTracker(strict_approvals=True)
    tracker.event(
        {
            "kind": "error",
            "payload": {"code": "approval_denied", "message": "shell was not approved"},
        }
    )
    tracker.event({"kind": "turn.done", "payload": {"reason": "complete"}})
    assert tracker.exit_code() == 4
