"""M37 §2.3: screen rules (TOML manifests ported from herdr) and their engine."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from snowpea_core.terminal.rules import engine

FIXTURES = Path(__file__).parent / "fixtures" / "m37_screens"


def _load(path: Path) -> tuple[dict[str, str], str]:
    head, _, screen = path.read_text(encoding="utf-8").partition("---\n")
    meta = {}
    for line in head.splitlines():
        key, _, value = line.lstrip("# ").partition(":")
        meta[key.strip()] = value.strip()
    # Screen.text(): rows right-stripped, trailing blank rows dropped.
    rows = [row.rstrip() for row in screen.split("\n")]
    while rows and not rows[-1]:
        rows.pop()
    return meta, "\n".join(rows)


CASES = sorted(FIXTURES.glob("*.txt"))


def test_fixtures_exist_for_every_cli():
    assert {p.name.split("__")[0] for p in CASES} == set(engine.PROFILES)


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_screen_fixture(path: Path):
    meta, screen = _load(path)
    rules = engine.load_rules(path.name.split("__")[0])
    verdict = rules.evaluate(screen, title=meta.get("title", ""))
    if meta["state"] == "none":
        assert verdict is None, verdict
        return
    assert verdict is not None
    assert (verdict.state, verdict.rule_id) == (meta["state"], meta["rule"])
    assert verdict.kind == (meta.get("kind") or None)
    if verdict.state == "blocked":
        assert verdict.visible_blocker
    if "detail" in meta:
        assert verdict.detail == meta["detail"].replace("|", "\n")
    assert verdict.skip_update == (meta.get("skip") == "true")


def test_every_bundled_manifest_compiles():
    for profile in engine.PROFILES:
        rules = engine.load_rules(profile)
        assert rules.id == profile and rules.rules


def test_priority_and_tie_order():
    manifest = """
id = "t"
[[rules]]
id = "low"
state = "working"
priority = 1
contains = ["x"]
[[rules]]
id = "first"
state = "blocked"
priority = 5
contains = ["x"]
[[rules]]
id = "second"
state = "idle"
priority = 5
contains = ["x"]
"""
    verdict = engine.parse_manifest(manifest).evaluate("X marks")
    assert verdict.rule_id == "first"


def test_gate_semantics():
    manifest = """
id = "t"
[[rules]]
id = "r"
state = "blocked"
kind = "permission"
priority = 1
contains = ["proceed"]
line_regex = ['^exact line$']
any = [{ contains = ["yes"] }, { contains = ["ok"] }]
not = [{ contains = ["nope"] }]
"""
    rules = engine.parse_manifest(manifest)
    assert rules.evaluate("Proceed?\nexact line\nYes").state == "blocked"
    assert rules.evaluate("Proceed?\nexact line here\nYes") is None
    assert rules.evaluate("Proceed?\nexact line\nmaybe") is None
    assert rules.evaluate("Proceed?\nexact line\nok nope") is None


@pytest.mark.parametrize(("region", "expected"), [
    ("whole_recent", "a\n──────\nb\n──────\nc\n\nd"),
    ("bottom_lines(2)", "\nd"),
    ("bottom_non_empty_lines(2)", "c\n\nd"),
    ("top_non_empty_lines(2)", "a\n──────\n"),
    ("after_last_horizontal_rule", "c\n\nd"),
    ("prompt_box_body", "b\n"),
    ("above_prompt_box", "a\n"),
    ("last_non_empty_above_prompt_box", "a"),
    ("osc_title", "T"),
    ("osc_progress", "4;0"),
])
def test_regions(region, expected):
    content = "a\n──────\nb\n──────\nc\n\nd"
    assert engine.region_text(content, region, title="T", progress="4;0") == expected


def test_codex_prompt_regions():
    content = "› old prompt\n• answer\nWorking (3s • esc to interrupt)\n›\n  footer"
    assert engine.region_text(content, "before_current_prompt_marker") == (
        "› old prompt\n• answer\nWorking (3s • esc to interrupt)\n")
    assert engine.region_text(content, "after_last_prompt_marker") == "  footer"
    assert engine.region_text(content, "whole_recent_without_current_prompt_marker") == ""
    assert engine.region_text(content, "current_prompt_block_marker") == "• answer"


@pytest.mark.parametrize("bad", [
    'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\nbogus = 1\ncontains = ["x"]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "sleeping"\ncontains = ["x"]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\nregion = "middle"\ncontains = ["x"]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\nregex = ["("]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\nkind = "permission"\ncontains = ["x"]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "blocked"\nextract = "x"\ncontains = ["x"]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "unknown"\nskip_state_update = true\n'
    'visible_idle = true\ncontains = ["x"]\n',
    'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\nregion = "top_non_empty_lines(0)"\n'
    'contains = ["x"]\n',
])
def test_validation_rejects(bad):
    with pytest.raises(engine.RuleError):
        engine.parse_manifest(bad)


def test_limits():
    deep = "{ contains = ['x'] }"
    for _ in range(engine.MAX_GATE_DEPTH + 1):
        deep = "{ all = [" + deep + "] }"
    manifest = f'id = "t"\n[[rules]]\nid = "r"\nstate = "working"\nall = [{deep}]\n'
    with pytest.raises(engine.RuleError, match="deeper"):
        engine.parse_manifest(manifest)
    many = "".join(f'[[rules]]\nid = "r{i}"\nstate = "working"\ncontains = ["x"]\n'
                   for i in range(engine.MAX_RULES + 1))
    with pytest.raises(engine.RuleError, match="more than"):
        engine.parse_manifest('id = "t"\n' + many)


def test_rules_stay_fast_on_hostile_screens():
    """Python's re backtracks where Rust's regex does not: bound the worst cases we know."""
    screens = [
        "\n".join(["• x (1s)" + " y" * 30] * 60),
        "\n".join(["─" * 200] * 60),
        "\n".join([" " * 150 + "a"] * 60),
        "\n".join(["✻ " + "a" * 150 + "…"] * 60),
        "› " + "\n".join(["Working (1s • esc to interrupt) · " + "q" * 100] * 60),
        "enter to confirm · esc to cancel\n" + "\n".join(["   " + "─" * 100] * 60),
    ]
    for profile in engine.PROFILES:
        rules = engine.load_rules(profile)
        for screen in screens:
            started = time.perf_counter()
            rules.evaluate(screen, title="⠋ x" * 20, progress="4;3;")
            assert time.perf_counter() - started < 0.5, (profile, screen[:40])
