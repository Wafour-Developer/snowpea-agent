"""M37 §2.3: status sources ranked osc > blocker > hook > rules, timings on a fake clock."""

from __future__ import annotations

from typing import Any

from snowpea_core.terminal.status import (
    BLOCKER_REPUBLISH,
    LAUNCH_SILENCE,
    HookReport,
    RuleVerdict,
    ScreenRules,
    StatusMachine,
)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class Rules:
    """Answers whatever ``verdict`` holds; counts the checks."""

    def __init__(self) -> None:
        self.verdict: RuleVerdict | None = None
        self.checks = 0

    def apply_rules(self, screen_text: str, title: str) -> RuleVerdict | None:
        self.checks += 1
        return self.verdict


def _machine(rules: Rules | None = None) -> tuple[StatusMachine, Clock, list]:
    clock = Clock()
    published: list = []
    machine = StatusMachine(rules=rules, clock=clock, on_change=published.append)
    return machine, clock, published


def _screen() -> tuple[str, str]:
    return "screen", "title"


def test_rules_object_satisfies_the_protocol() -> None:
    assert isinstance(Rules(), ScreenRules)


def test_precedence_osc_over_blocker_over_hook_over_rules() -> None:
    rules = Rules()
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    rules.verdict = RuleVerdict("working")
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("working", "rules")

    machine.apply_hook_report(HookReport("Stop", state="done"))
    assert (machine.state, machine.status.source) == ("done", "hook")

    rules.verdict = RuleVerdict("blocked", kind="permission", detail="rm -rf build")
    clock.t += 1
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.kind, machine.status.source) == (
        "blocked", "permission", "screen")
    assert machine.status.detail == "rm -rf build"

    machine.apply_osc("working")
    assert (machine.state, machine.status.source) == ("working", "osc")
    machine.apply_osc("clear")
    assert machine.status.source == "screen"  # the blocker is still on screen

    rules.verdict = RuleVerdict("working")
    clock.t += 1
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("done", "hook")


def test_launch_silence_then_rules_checked_every_400ms_while_output_flows() -> None:
    rules = Rules()
    rules.verdict = RuleVerdict("working")
    machine, clock, published = _machine(rules)
    machine.on_output()
    assert machine.poll(_screen) is None
    clock.t += LAUNCH_SILENCE - 0.1
    machine.poll(_screen)
    assert rules.checks == 0 and published == []
    clock.t += 0.1
    machine.poll(_screen)
    assert rules.checks == 1 and machine.state == "working"
    clock.t += 0.2
    machine.on_output()
    machine.poll(_screen)
    assert rules.checks == 1  # under 400 ms
    clock.t += 0.2
    machine.poll(_screen)
    assert rules.checks == 2


def test_working_to_idle_needs_three_checks_100ms_apart() -> None:
    rules = Rules()
    rules.verdict = RuleVerdict("working")
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.poll(_screen)
    rules.verdict = RuleVerdict("idle")
    for _ in range(2):
        clock.t += 0.4
        machine.on_output()  # output keeps flowing: no quiet shortcut
        machine.poll(_screen)
        assert machine.state == "working"
    clock.t += 0.1
    machine.on_output()
    machine.poll(_screen)
    assert machine.state == "idle"
    assert machine.status.turn == 1


def test_working_to_idle_after_700ms_quiet() -> None:
    rules = Rules()
    rules.verdict = RuleVerdict("working")
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.on_output()
    machine.poll(_screen)
    rules.verdict = None  # the working indicator is gone; nothing matches
    clock.t += 0.75
    machine.poll(_screen)  # one check after 700 ms of quiet is enough
    assert machine.state == "idle"


def test_visible_blocker_republished_every_800ms() -> None:
    rules = Rules()
    rules.verdict = RuleVerdict("blocked", kind="question")
    machine, clock, published = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.poll(_screen)
    assert len(published) == 1
    clock.t += BLOCKER_REPUBLISH / 2
    machine.poll(_screen)
    assert len(published) == 1
    clock.t += BLOCKER_REPUBLISH / 2
    machine.poll(_screen)
    assert len(published) == 2 and published[-1].state == "blocked"


def test_turn_stuck_and_seen() -> None:
    machine, _, _ = _machine()
    assert machine.status.seen and machine.status.turn == 0
    machine.apply_osc("working")
    machine.apply_osc("blocked", kind="permission")
    assert machine.status.turn == 1 and machine.status.stuck
    machine.apply_osc("working")
    assert not machine.status.stuck
    machine.apply_osc("done")
    assert machine.status.turn == 2 and not machine.status.seen
    machine.mark_seen()
    assert machine.status.seen
    machine.apply_osc("error")
    assert machine.status.stuck and machine.status.turn == 2


def test_hook_blocked_lapses_when_the_person_submits() -> None:
    machine, _, _ = _machine()
    machine.apply_hook_report(HookReport("Notification", state="blocked", kind="permission"))
    assert machine.state == "blocked" and machine.status.kind == "permission"
    machine.on_human_submit()
    assert machine.state == "idle"


def test_hook_report_without_state_changes_nothing() -> None:
    machine, _, published = _machine()
    assert machine.apply_hook_report(HookReport("SessionStart", session_id="abc")) is None
    assert published == []


def test_exit_wins_and_wire_shape() -> None:
    machine, _, _ = _machine()
    machine.apply_osc("working", progress=40, message="hi")
    machine.on_exit(2)
    assert machine.state == "error"
    wire = machine.status.wire()
    assert set(wire) <= {"state", "kind", "turn", "stuck", "seen", "source"}
    assert wire["source"] == "none"


def test_broken_rules_never_raise() -> None:
    class Broken:
        def apply_rules(self, screen_text: str, title: str) -> RuleVerdict | None:
            raise RuntimeError("bad regex")

    clock = Clock()
    machine = StatusMachine(rules=Broken(), clock=clock)
    clock.t += LAUNCH_SILENCE
    machine.poll(_screen)
    assert machine.state == "idle"


def test_engine_shaped_rule_sets_and_reports_are_understood() -> None:
    """T2's engine: ``RuleSet.evaluate`` and its verdict / hook-report field names."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class EngineVerdict:
        state: str
        kind: str | None
        rule_id: str
        priority: int
        visible_blocker: bool = False
        skip_update: bool = False
        detail: str | None = None

    class EngineRules:
        verdict: EngineVerdict | None = None

        def evaluate(self, screen: str, title: str = "", progress: str = "") -> Any:
            return self.verdict

    @dataclass(frozen=True)
    class EngineReport:
        cli: str
        event: str
        state: str | None
        kind: str | None
        session_id: str | None
        transcript_path: str | None
        received_at: float

    rules = EngineRules()
    clock = Clock()
    machine = StatusMachine(rules=rules, clock=clock)
    clock.t += LAUNCH_SILENCE
    rules.verdict = EngineVerdict("blocked", "permission", "claude.perm", 90, visible_blocker=True,
                                  detail="npm test")
    machine.poll(_screen)
    assert (machine.state, machine.status.source, machine.status.detail) == (
        "blocked", "screen", "npm test")
    machine.apply_hook_report(EngineReport("claude", "Stop", "done", None, None, None, 0.0))
    assert machine.status.source == "screen"  # the visible blocker still wins
    rules.verdict = EngineVerdict("unknown", None, "x", 1)
    clock.t += 1
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("blocked", "screen")  # unknown: no change
    rules.verdict = EngineVerdict("idle", None, "claude.prompt", 10)
    clock.t += 1
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("done", "hook")
    rules.verdict = EngineVerdict("working", None, "claude.busy", 50, skip_update=True)
    clock.t += 1
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("done", "hook")


def test_visible_idle_prompt_drops_a_stale_hook_working() -> None:
    rules = Rules()
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.apply_hook_report(HookReport("UserPromptSubmit", state="working"))
    rules.verdict = RuleVerdict("idle")
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("working", "hook")
    rules.verdict = RuleVerdict("idle", visible_idle=True)
    clock.t += 1
    machine.on_output()
    machine.poll(_screen)
    assert machine.state == "idle" and machine.status.source in ("rules", "none")


def test_stale_hook_working_yields_after_2s_of_idle_rules() -> None:
    """Claude fires no Stop on Esc: a hook ``working`` must not pin the state."""
    rules = Rules()
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.apply_hook_report(HookReport("UserPromptSubmit", state="working"))
    rules.verdict = RuleVerdict("idle")  # no visible prompt marker, just idle
    for _ in range(5):  # output keeps changing (a redraw), rules keep saying idle
        clock.t += 0.45
        machine.on_output()
        machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("working", "hook")  # idle for 1.8 s
    clock.t += 0.45
    machine.on_output()
    machine.poll(_screen)
    assert machine.state == "idle"  # idle rules for 2.25 s


def test_stale_hook_working_yields_after_2s_without_output() -> None:
    machine, clock, _ = _machine()  # no screen rules at all
    machine.apply_hook_report(HookReport("UserPromptSubmit", state="working"))
    clock.t += 1.9
    machine.poll(_screen)
    assert machine.state == "working"
    clock.t += 0.2
    machine.poll(_screen)
    assert machine.state == "idle"


def test_hook_working_holds_while_rules_say_working() -> None:
    rules = Rules()
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.apply_hook_report(HookReport("UserPromptSubmit", state="working"))
    rules.verdict = RuleVerdict("working")
    for _ in range(10):
        clock.t += 0.45
        machine.on_output()
        machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("working", "hook")


def test_hook_done_and_blocked_do_not_lapse_and_a_visible_blocker_wins() -> None:
    rules = Rules()
    machine, clock, _ = _machine(rules)
    clock.t += LAUNCH_SILENCE
    machine.apply_hook_report(HookReport("Stop", state="done"))
    rules.verdict = RuleVerdict("idle")
    for _ in range(10):
        clock.t += 0.45
        machine.on_output()
        machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("done", "hook")
    machine.apply_hook_report(HookReport("Notification", state="blocked", kind="permission"))
    clock.t += 5
    machine.poll(_screen)
    assert (machine.state, machine.status.source) == ("blocked", "hook")
    machine.apply_hook_report(HookReport("UserPromptSubmit", state="working"))
    rules.verdict = RuleVerdict("blocked", kind="question")
    clock.t += 0.45
    machine.on_output()
    machine.poll(_screen)
    assert (machine.state, machine.status.kind, machine.status.source) == (
        "blocked", "question", "screen")
