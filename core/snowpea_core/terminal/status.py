"""Terminal status: one state machine per terminal, fed by four sources (M37 §2.3).

States are ``idle | working | done | blocked | error``; ``blocked`` carries a
``kind`` (``permission | question | auth``).  Sources, highest first:

1. ``osc``    — OSC 7501 records the program itself emits (:mod:`.osc`);
2. ``screen`` — a blocker the screen rules see on screen (a :class:`RuleVerdict`
   with ``state == "blocked"``);
3. ``hook``   — a CLI hook report (``terminal.report``, :class:`HookReport`);
4. ``rules``  — every other screen-rule verdict (working / idle / done / error).

The interface the rule engine and the hook reports plug into is
:class:`ScreenRules` (``apply_rules(screen_text, title) -> RuleVerdict | None``),
:class:`RuleVerdict`, :class:`HookReport` and :meth:`StatusMachine.apply_hook_report`.
The rule engine itself lives in ``terminal/rules/`` (T2); this module only
times when it is asked and ranks what it answers.

Every method takes an optional ``now`` (seconds on the machine's clock) so the
debounce timings are testable with a fake clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

State = Literal["idle", "working", "done", "blocked", "error"]
Kind = Literal["permission", "question", "auth"]
Source = Literal["osc", "screen", "hook", "rules", "none"]

STATES: frozenset[str] = frozenset({"idle", "working", "done", "blocked", "error"})
KINDS: frozenset[str] = frozenset({"permission", "question", "auth"})

#: Screen-rule timings (§2.3).
RULE_CHECK_INTERVAL = 0.4  #: re-check while output flows
IDLE_CONFIRM_CHECKS = 3  #: working -> idle needs this many matching checks …
IDLE_CONFIRM_SPACING = 0.1  #: … this far apart,
IDLE_QUIET = 0.7  #: … or this much output quiet
LAUNCH_SILENCE = 3.0  #: nothing from the rules is published this soon after launch
BLOCKER_REPUBLISH = 0.8  #: a visible blocker is published again this often


# ---------------------------------------------------------------------------
# the plug-in interface (T2: screen rules + hook reports)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleVerdict:
    """What the screen rules see.  ``state == "blocked"`` is a visible blocker.

    ``rule`` names the matching rule (for logs); ``detail`` is text the rule
    extracted, e.g. the command a permission prompt asks about (Jarvis answers
    a prompt only when the rule extracted it, §2.11).
    """

    state: str
    kind: str | None = None
    rule: str | None = None
    detail: str | None = None
    priority: int = 0


@runtime_checkable
class ScreenRules(Protocol):
    """One CLI's screen rules (``terminal/rules/<cli>.toml`` compiled by T2's engine).

    ``screen_text`` is :meth:`Screen.text` (plain text, rows joined with ``\\n``,
    bottom row last); ``title`` is the window title (OSC 0/2).  Return ``None``
    when no rule matches.
    """

    def apply_rules(self, screen_text: str, title: str) -> RuleVerdict | None: ...


#: ``rules_for(rules_id) -> ScreenRules | None``; T2's engine provides it.
RulesLoader = Callable[[str], "ScreenRules | None"]


@dataclass(frozen=True)
class HookReport:
    """One ``terminal.report`` from a CLI hook (T2's hook script / report socket).

    ``state`` is already mapped by the integration (``UserPromptSubmit`` →
    working, ``Stop`` → done, ``Notification`` → blocked); ``None`` means the
    event carries no state (``SessionStart``: only the session id).
    """

    event: str
    state: str | None = None
    kind: str | None = None
    session_id: str | None = None
    transcript_path: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# published status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalStatus:
    state: str = "idle"
    kind: str | None = None
    turn: int = 0
    stuck: bool = False
    seen: bool = True
    source: str = "none"
    #: From the claim that won: OSC 7501 ``progress``/``msg``, or a rule's detail.
    progress: int | None = None
    message: str | None = None
    detail: str | None = None

    def wire(self) -> dict[str, Any]:
        """The wire ``TerminalStatus`` (§2.13); progress, message and detail stay in-process."""
        out: dict[str, Any] = {
            "state": self.state,
            "turn": self.turn,
            "stuck": self.stuck,
            "seen": self.seen,
            "source": self.source,
        }
        if self.kind:
            out["kind"] = self.kind
        return out


@dataclass(frozen=True)
class _Claim:
    state: str
    kind: str | None = None
    progress: int | None = None
    message: str | None = None
    detail: str | None = None


@dataclass
class _Rules:
    claim: _Claim | None = None
    last_check: float = float("-inf")
    pending: _Claim | None = None
    pending_none: bool = False
    pending_count: int = 0


@dataclass
class StatusMachine:
    """Ranks the sources, applies the screen-rule timings and keeps turn/stuck/seen.

    ``on_change(status)`` runs whenever the published status changes (and every
    :data:`BLOCKER_REPUBLISH` seconds while a visible blocker holds).
    """

    rules: ScreenRules | None = None
    clock: Callable[[], float] = time.monotonic
    launched_at: float | None = None
    on_change: Callable[[TerminalStatus], None] | None = None

    _osc: _Claim | None = field(default=None, init=False)
    _blocker: _Claim | None = field(default=None, init=False)
    _hook: _Claim | None = field(default=None, init=False)
    _exit: _Claim | None = field(default=None, init=False)
    _rules: _Rules = field(default_factory=_Rules, init=False)
    _status: TerminalStatus = field(default_factory=TerminalStatus, init=False)
    _last_publish: float = field(default=float("-inf"), init=False)
    _last_output: float = field(default=float("-inf"), init=False)
    _dirty: bool = field(default=False, init=False)
    #: The program spoke OSC 7501 at least once: it reports its own status.
    reports_status: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.launched_at is None:
            self.launched_at = self.clock()

    # -- read --------------------------------------------------------------
    @property
    def status(self) -> TerminalStatus:
        return self._status

    @property
    def state(self) -> str:
        return self._status.state

    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else now

    # -- sources -------------------------------------------------------------
    def apply_osc(self, state: str, *, kind: str | None = None, progress: int | None = None,
                  message: str | None = None, now: float | None = None) -> TerminalStatus | None:
        """A root OSC 7501 record; ``state == "clear"`` drops the program's claim."""
        self.reports_status = True
        if state == "clear":
            self._osc = None
        elif state in STATES:
            self._osc = _Claim(state, _kind(state, kind), _progress(progress), message or None)
        else:
            return None
        return self._publish(self._now(now))

    def apply_rule_verdict(self, verdict: RuleVerdict | None, *,
                           now: float | None = None) -> TerminalStatus | None:
        """Feed one rule check's result (the timings in :meth:`poll` decide when)."""
        now = self._now(now)
        rules = self._rules
        rules.last_check = now
        if verdict is not None and verdict.state not in STATES:
            verdict = None
        if verdict is not None and verdict.state == "blocked":
            self._blocker = _Claim("blocked", _kind("blocked", verdict.kind), detail=verdict.detail)
            rules.pending, rules.pending_none, rules.pending_count = None, False, 0
            return self._publish(now)
        self._blocker = None
        claim = None if verdict is None else _Claim(
            verdict.state, _kind(verdict.state, verdict.kind), detail=verdict.detail
        )
        current = rules.claim
        if claim is not None and claim.state == "working":
            rules.claim = claim
            rules.pending, rules.pending_none, rules.pending_count = None, False, 0
        elif current is not None and current.state == "working":
            # Leaving working needs confirmation: 3 matching checks 100 ms
            # apart, or 700 ms without output.
            same = (claim is None and rules.pending_none) or (
                claim is not None and rules.pending == claim
            )
            rules.pending_count = rules.pending_count + 1 if same else 1
            rules.pending, rules.pending_none = claim, claim is None
            if rules.pending_count >= IDLE_CONFIRM_CHECKS or now - self._last_output >= IDLE_QUIET:
                rules.claim = claim
                rules.pending, rules.pending_none, rules.pending_count = None, False, 0
        else:
            rules.claim = claim
            rules.pending, rules.pending_none, rules.pending_count = None, False, 0
        return self._publish(now)

    def apply_hook_report(
        self, report: HookReport, *, now: float | None = None
    ) -> TerminalStatus | None:
        """A hook's state; a report without one (``SessionStart``) changes nothing here."""
        if report.state is None:
            return None
        if report.state == "clear":
            self._hook = None
        elif report.state in STATES:
            self._hook = _Claim(report.state, _kind(report.state, report.kind),
                                message=report.message or None)
        else:
            return None
        return self._publish(self._now(now))

    def on_output(self, now: float | None = None) -> None:
        self._last_output = self._now(now)
        self._dirty = True

    def on_human_submit(self, now: float | None = None) -> TerminalStatus | None:
        """The person pressed Enter: a hook's ``blocked`` is answered, so it lapses.

        (Claude's Notification hook says blocked, but nothing says unblocked
        until the turn ends; the screen rules see the rest.)
        """
        if self._hook is not None and self._hook.state == "blocked":
            self._hook = None
            return self._publish(self._now(now))
        return None

    def on_exit(self, code: int | None, now: float | None = None) -> TerminalStatus | None:
        state = "done" if code in (0, None) else "error"
        self._exit = _Claim(state)
        return self._publish(self._now(now))

    def mark_seen(self, now: float | None = None) -> TerminalStatus | None:
        if self._status.seen:
            return None
        self._status = replace(self._status, seen=True)
        self._last_publish = self._now(now)
        if self.on_change is not None:
            self.on_change(self._status)
        return self._status

    # -- timing --------------------------------------------------------------
    def check_due(self, now: float | None = None) -> bool:
        """True when the screen rules should be asked now."""
        if self.rules is None or self._exit is not None:
            return False
        now = self._now(now)
        assert self.launched_at is not None
        if now - self.launched_at < LAUNCH_SILENCE:
            return False
        rules = self._rules
        since = now - rules.last_check + 1e-6  # float slack for 100 ms steps
        if self._dirty and since >= RULE_CHECK_INTERVAL:
            return True
        if rules.pending_count and since >= IDLE_CONFIRM_SPACING:
            return True
        if rules.last_check == float("-inf"):
            return True  # the first check after the launch silence
        working = rules.claim is not None and rules.claim.state == "working"
        quiet_since = self._last_output + IDLE_QUIET
        # One more check once output has been quiet for 700 ms.
        return working and now >= quiet_since and rules.last_check < quiet_since

    def poll(self, screen: Callable[[], tuple[str, str]],
             now: float | None = None) -> TerminalStatus | None:
        """Ask the rules when due; republish a visible blocker.  ``screen() -> (text, title)``.

        Returns the status when something was published, else ``None``.
        """
        now = self._now(now)
        published: TerminalStatus | None = None
        if self.check_due(now):
            assert self.rules is not None
            self._dirty = False
            text, title = screen()
            try:
                verdict = self.rules.apply_rules(text, title)
            except Exception:  # noqa: BLE001 - a broken rule must not stop the terminal
                verdict = None
            published = self.apply_rule_verdict(verdict, now=now)
        if (published is None and self._status.source == "screen"
                and now - self._last_publish >= BLOCKER_REPUBLISH):
            self._last_publish = now
            if self.on_change is not None:
                self.on_change(self._status)
            published = self._status
        return published

    # -- ranking ------------------------------------------------------------
    def _winner(self) -> tuple[_Claim | None, str]:
        for claim, source in ((self._exit, "none"), (self._osc, "osc"), (self._blocker, "screen"),
                              (self._hook, "hook"), (self._rules.claim, "rules")):
            if claim is not None:
                return claim, source
        return None, "none"

    def _publish(self, now: float) -> TerminalStatus | None:
        claim, source = self._winner()
        if claim is None:
            claim = _Claim("idle")
        old = self._status
        if (claim.state, claim.kind, source, claim.progress, claim.message, claim.detail) == (
            old.state, old.kind, old.source, old.progress, old.message, old.detail
        ):
            return None
        turn, stuck, seen = old.turn, old.stuck, old.seen
        if claim.state != old.state:
            if old.state == "working":
                turn += 1  # the root leaves working, for any reason
                seen = False
            if claim.state in ("blocked", "error"):
                stuck = True
            elif claim.state in ("working", "idle"):
                stuck = False
            if claim.state == "done":
                seen = False
        self._status = TerminalStatus(
            state=claim.state, kind=claim.kind, turn=turn, stuck=stuck, seen=seen, source=source,
            progress=claim.progress, message=claim.message, detail=claim.detail,
        )
        self._last_publish = now
        if self.on_change is not None:
            self.on_change(self._status)
        return self._status


def _kind(state: str, kind: str | None) -> str | None:
    return kind if state == "blocked" and kind in KINDS else None


def _progress(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, min(100, int(value)))
