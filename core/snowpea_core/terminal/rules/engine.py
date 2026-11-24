"""Screen rules: what a CLI's screen says about its state (design m37 §2.3).

Each CLI has a TOML manifest beside this file (``claude.toml``, ``codex.toml``,
``cursor.toml``, ``agy.toml``).  A manifest is a list of ``[[rules]]``; every
rule names a ``state`` (``idle`` / ``working`` / ``blocked`` / ``unknown``), a
``priority`` and a ``region`` of the screen, and a gate of matchers:

* ``contains`` — every needle is a substring of the region (case-insensitive);
* ``regex`` — every regex is found in the region;
* ``line_regex`` — every regex is found in at least one line of the region;
* ``all`` / ``any`` / ``not`` — nested gates (every / at least one / none).

Every rule is evaluated; the matching rule with the highest priority wins (the
earlier one on a tie).  Regions: ``whole_recent`` (the whole screen text),
``bottom_lines(N)``, ``bottom_non_empty_lines(N)``, ``top_non_empty_lines(N)``,
``osc_title``, ``osc_progress`` and herdr's prompt-structure regions
(``after_last_horizontal_rule``, ``prompt_box_body``, ``above_prompt_box``,
``last_non_empty_above_prompt_box``, ``after_last_prompt_marker``,
``before_current_prompt_marker``, ``whole_recent_without_current_prompt_marker``,
``current_prompt_block_marker``, ``after_current_prompt_block_marker``).

snowpea additions to herdr's grammar: ``kind`` (``permission`` / ``question`` /
``auth``) on a blocked rule, and ``extract`` — a regex with a ``detail`` group
whose last match in the region (or in ``extract_region``) becomes
:attr:`RuleVerdict.detail` (the command text of a permission prompt, for
Jarvis's delegation check; it can include the CLI's one-line description, so
treat it as text to classify, never as a command to run).

The engine only classifies one screen.  Timing (re-check cadence, the
working → idle hold, the startup grace) belongs to the status machine.

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0
(``src/detect/manifest.rs``: grammar, limits, matching semantics and regions).
"""

from __future__ import annotations

import functools
import re
import textwrap
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

PROFILES: tuple[str, ...] = ("claude", "codex", "cursor", "agy")
STATES = frozenset({"idle", "working", "blocked", "unknown"})
KINDS = frozenset({"permission", "question", "auth"})

MAX_RULES = 128
MAX_GATE_DEPTH = 8
MAX_TOTAL_GATES = 512
MAX_MATCHERS_PER_GATE = 32
MAX_TOTAL_MATCHERS = 1024
MAX_MATCHER_CHARS = 512
MAX_DETAIL_CHARS = 4000
_MAX_TOP_COUNT = 65535

_HEADER_KEYS = frozenset({"id", "version", "upstream_version", "min_engine_version",
                          "updated_at", "aliases", "rules"})
_GATE_KEYS = frozenset({"contains", "regex", "line_regex", "all", "any", "not"})
_RULE_KEYS = _GATE_KEYS | {"id", "state", "priority", "region", "visible_idle",
                           "visible_blocker", "visible_working", "skip_state_update",
                           "kind", "extract", "extract_region"}
_FIXED_REGIONS = frozenset({
    "whole_recent", "after_last_prompt_marker", "before_current_prompt_marker",
    "whole_recent_without_current_prompt_marker", "current_prompt_block_marker",
    "after_current_prompt_block_marker", "prompt_box_body", "above_prompt_box",
    "last_non_empty_above_prompt_box", "after_last_horizontal_rule", "osc_title",
    "osc_progress",
})
_COUNTED = re.compile(r"^(bottom_lines|bottom_non_empty_lines|top_non_empty_lines)\(([0-9]+)\)$")


class RuleError(ValueError):
    """A manifest that does not follow the grammar or limits."""


@dataclass(frozen=True)
class RuleVerdict:
    """The winning rule for one screen."""

    state: str  # idle | working | blocked | unknown
    kind: str | None
    rule_id: str
    priority: int
    visible_blocker: bool = False
    visible_idle: bool = False
    visible_working: bool = False
    skip_update: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class _Gate:
    contains: tuple[str, ...]
    regex: tuple[re.Pattern[str], ...]
    line_regex: tuple[re.Pattern[str], ...]
    all: tuple[_Gate, ...]
    any: tuple[_Gate, ...]
    not_: tuple[_Gate, ...]

    def matches(self, text: str, lower: str, lines: list[str]) -> bool:
        if not all(needle in lower for needle in self.contains):
            return False
        if not all(rx.search(text) for rx in self.regex):
            return False
        if not all(any(rx.search(line) for line in lines) for rx in self.line_regex):
            return False
        if not all(g.matches(text, lower, lines) for g in self.all):
            return False
        if self.any and not any(g.matches(text, lower, lines) for g in self.any):
            return False
        return not any(g.matches(text, lower, lines) for g in self.not_)


@dataclass(frozen=True)
class _Rule:
    id: str
    state: str
    priority: int
    region: str
    gate: _Gate
    kind: str | None
    extract: re.Pattern[str] | None
    extract_region: str | None
    visible_idle: bool
    visible_blocker: bool
    visible_working: bool
    skip_state_update: bool


class _Counter:
    def __init__(self) -> None:
        self.gates = 0
        self.matchers = 0


def _compile_regex(pattern: Any, where: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise RuleError(f"{where}: a regex must be a non-empty string")
    if len(pattern) > MAX_MATCHER_CHARS:
        raise RuleError(f"{where}: regex longer than {MAX_MATCHER_CHARS} characters")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RuleError(f"{where}: bad regex {pattern!r}: {exc}") from exc


def _strings(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RuleError(f"{where}: expected a list of strings")
    return value


def _gate(raw: Any, where: str, depth: int, counter: _Counter) -> _Gate:
    if not isinstance(raw, dict):
        raise RuleError(f"{where}: a gate must be a table")
    if depth > MAX_GATE_DEPTH:
        raise RuleError(f"{where}: gates nested deeper than {MAX_GATE_DEPTH}")
    unknown = set(raw) - _GATE_KEYS
    if unknown:
        raise RuleError(f"{where}: unknown field(s) {sorted(unknown)}")
    counter.gates += 1
    if counter.gates > MAX_TOTAL_GATES:
        raise RuleError(f"{where}: more than {MAX_TOTAL_GATES} gates")
    contains = _strings(raw.get("contains"), where)
    regex = _strings(raw.get("regex"), where)
    line_regex = _strings(raw.get("line_regex"), where)
    count = len(contains) + len(regex) + len(line_regex)
    if count > MAX_MATCHERS_PER_GATE:
        raise RuleError(f"{where}: more than {MAX_MATCHERS_PER_GATE} matchers in one gate")
    counter.matchers += count
    if counter.matchers > MAX_TOTAL_MATCHERS:
        raise RuleError(f"{where}: more than {MAX_TOTAL_MATCHERS} matchers")
    for needle in contains:
        if not needle or len(needle) > MAX_MATCHER_CHARS:
            raise RuleError(f"{where}: empty or too long `contains` needle")

    def nested(key: str) -> tuple[_Gate, ...]:
        items = raw.get(key) or []
        if not isinstance(items, list):
            raise RuleError(f"{where}.{key}: expected a list of gates")
        return tuple(_gate(item, f"{where}.{key}[{i}]", depth + 1, counter)
                     for i, item in enumerate(items))

    gate = _Gate(
        contains=tuple(n.lower() for n in contains),
        regex=tuple(_compile_regex(p, where) for p in regex),
        line_regex=tuple(_compile_regex(p, where) for p in line_regex),
        all=nested("all"), any=nested("any"), not_=nested("not"))
    if not (gate.contains or gate.regex or gate.line_regex or gate.all or gate.any
            or gate.not_):
        raise RuleError(f"{where}: a gate needs at least one matcher")
    return gate


def _check_region(region: Any, where: str) -> str:
    if not isinstance(region, str):
        raise RuleError(f"{where}: region must be a string")
    region = region.strip()
    if region in _FIXED_REGIONS:
        return region
    match = _COUNTED.match(region)
    if match:
        count = match.group(2)
        if match.group(1) == "top_non_empty_lines" and (
                count.startswith("0") or int(count) > _MAX_TOP_COUNT):
            raise RuleError(f"{where}: bad line count in {region!r}")
        return region
    raise RuleError(f"{where}: unknown region {region!r}")


@dataclass(frozen=True)
class RuleSet:
    """A compiled manifest."""

    id: str
    version: str
    aliases: tuple[str, ...]
    rules: tuple[_Rule, ...]

    def evaluate(self, screen: str, title: str = "", progress: str = "") -> RuleVerdict | None:
        """The verdict of the highest-priority matching rule, or ``None`` when none matches.

        ``screen`` is the visible screen text, one row per line, each right-
        stripped and trailing blank rows dropped (``Screen.text()``); ``title``
        and ``progress`` are the latest OSC 0/2 title and OSC 9 payload.
        A caller treats ``None`` for a known CLI as idle (herdr's fallback).
        """
        screen = screen.replace("\r\n", "\n")
        best: _Rule | None = None
        best_text = ""
        cache: dict[str, tuple[str, str, list[str]]] = {}
        for rule in self.rules:
            entry = cache.get(rule.region)
            if entry is None:
                text = region_text(screen, rule.region, title, progress)
                entry = (text, text.lower(), _lines(text))
                cache[rule.region] = entry
            text, lower, lines = entry
            if not rule.gate.matches(text, lower, lines):
                continue
            if best is None or rule.priority > best.priority:
                best, best_text = rule, text
        if best is None:
            return None
        return RuleVerdict(
            state=best.state,
            kind=best.kind if best.state == "blocked" else None,
            rule_id=best.id,
            priority=best.priority,
            visible_blocker=best.visible_blocker and best.state == "blocked",
            visible_idle=best.visible_idle and best.state == "idle",
            visible_working=best.visible_working and best.state == "working",
            skip_update=best.skip_state_update,
            detail=_extract(best, best_text if best.extract_region is None else region_text(
                screen, best.extract_region, title, progress)),
        )


def _extract(rule: _Rule, text: str) -> str | None:
    if rule.extract is None:
        return None
    found = None
    for found in rule.extract.finditer(text):  # noqa: B007 - the last match wins
        pass
    if found is None:
        return None
    detail = found.group("detail")
    if not detail:
        return None
    lines = [line.rstrip() for line in detail.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out = textwrap.dedent("\n".join(lines)).strip()
    return out[:MAX_DETAIL_CHARS] or None


def parse_manifest(text: str, source: str = "<manifest>") -> RuleSet:
    """Compile a manifest's TOML text; :class:`RuleError` when it breaks the grammar."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"{source}: {exc}") from exc
    unknown = set(raw) - _HEADER_KEYS
    if unknown:
        raise RuleError(f"{source}: unknown field(s) {sorted(unknown)}")
    manifest_id = raw.get("id")
    if not isinstance(manifest_id, str) or not manifest_id:
        raise RuleError(f"{source}: `id` is required")
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleError(f"{source}: no [[rules]]")
    if len(raw_rules) > MAX_RULES:
        raise RuleError(f"{source}: more than {MAX_RULES} rules")
    counter = _Counter()
    rules: list[_Rule] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_rules):
        where = f"{source} rule {index}"
        if not isinstance(item, dict):
            raise RuleError(f"{where}: must be a table")
        unknown = set(item) - _RULE_KEYS
        if unknown:
            raise RuleError(f"{where}: unknown field(s) {sorted(unknown)}")
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen:
            raise RuleError(f"{where}: missing or duplicate id")
        seen.add(rule_id)
        where = f"{source} rule {rule_id!r}"
        state = item.get("state")
        if state not in STATES:
            raise RuleError(f"{where}: state must be one of {sorted(STATES)}")
        priority = item.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise RuleError(f"{where}: priority must be an integer")
        kind = item.get("kind")
        if kind is not None and (kind not in KINDS or state != "blocked"):
            raise RuleError(f"{where}: kind is one of {sorted(KINDS)} on a blocked rule")
        skip = bool(item.get("skip_state_update", False))
        if skip and any(item.get(k) for k in ("visible_idle", "visible_blocker",
                                               "visible_working")):
            raise RuleError(f"{where}: a skip rule cannot claim visible state")
        extract = item.get("extract")
        gate_raw = {k: v for k, v in item.items() if k in _GATE_KEYS}
        rules.append(_Rule(
            id=rule_id, state=state, priority=priority,
            region=_check_region(item.get("region", "whole_recent"), where),
            gate=_gate(gate_raw, where, 1, counter),
            kind=kind,
            extract=_compile_regex(extract, f"{where}.extract") if extract is not None else None,
            extract_region=(_check_region(item["extract_region"], where)
                            if item.get("extract_region") is not None else None),
            visible_idle=bool(item.get("visible_idle", False)),
            visible_blocker=bool(item.get("visible_blocker", False)),
            visible_working=bool(item.get("visible_working", False)),
            skip_state_update=skip,
        ))
        if extract is not None and "detail" not in rules[-1].extract.groupindex:  # type: ignore[union-attr]
            raise RuleError(f"{where}: extract needs a (?P<detail>…) group")
    aliases = raw.get("aliases") or []
    return RuleSet(id=manifest_id, version=str(raw.get("version") or ""),
                   aliases=tuple(_strings(aliases, f"{source} aliases")), rules=tuple(rules))


@functools.cache
def load_rules(profile: str) -> RuleSet:
    """The bundled rules of ``profile`` (``claude`` / ``codex`` / ``cursor`` / ``agy``)."""
    if profile not in PROFILES:
        raise RuleError(f"no screen rules for {profile!r}")
    text = resources.files("snowpea_core.terminal.rules").joinpath(
        f"{profile}.toml").read_text(encoding="utf-8")
    return parse_manifest(text, f"{profile}.toml")


def load_rules_file(path: Path) -> RuleSet:
    """Compile a manifest from a file (a person's override, a test fixture)."""
    return parse_manifest(Path(path).read_text(encoding="utf-8"), str(path))


# -- regions (herdr's src/detect/manifest.rs) ------------------------------------------


def _lines(content: str) -> list[str]:
    """``str.lines()`` as Rust has it: split on ``\\n``, no empty last line."""
    if not content:
        return []
    parts = content.split("\n")
    if content.endswith("\n"):
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def _line_start(content: str, lines: list[str], index: int) -> int:
    return min(sum(len(line) + 1 for line in lines[: min(index, len(lines))]), len(content))


def _from_line(content: str, lines: list[str], index: int) -> str:
    return content[_line_start(content, lines, index):]


def _is_rule(line: str) -> bool:
    trimmed = line.strip()
    if not trimmed:
        return False
    count = len(trimmed) - len(trimmed.lstrip("─"))
    if count == 0:
        return False
    suffix = trimmed[count:].lstrip()
    return not suffix or count >= 3


def _prompt_line(line: str) -> bool:
    return line == "›" or line.startswith("› ")


def _block_marker(line: str) -> bool:
    return line.startswith(("•", "■", "✗", "✓"))


def _current_prompt_index(lines: list[str]) -> int | None:
    index = next((i for i in range(len(lines) - 1, -1, -1) if _prompt_line(lines[i])), None)
    if index is None or any(_block_marker(line) for line in lines[index + 1:]):
        return None
    return index


def _prompt_box_top(lines: list[str]) -> int | None:
    seen = 0
    for index in range(len(lines) - 1, -1, -1):
        if _is_rule(lines[index]):
            seen += 1
            if seen == 2:
                return index
    return None


def region_text(content: str, region: str, title: str = "", progress: str = "") -> str:
    """The part of ``content`` a rule's ``region`` looks at."""
    if region == "osc_title":
        return title
    if region == "osc_progress":
        return progress
    lines = _lines(content)
    if region == "whole_recent":
        return content
    if region == "after_last_prompt_marker":
        index = next((i for i in range(len(lines) - 1, -1, -1) if _prompt_line(lines[i])), None)
        return content if index is None else _from_line(content, lines, index + 1)
    if region == "before_current_prompt_marker":
        index = _current_prompt_index(lines)
        if index is None:
            return content
        return content[: _line_start(content, lines, index)]
    if region == "whole_recent_without_current_prompt_marker":
        return "" if _current_prompt_index(lines) is not None else content
    if region in ("current_prompt_block_marker", "after_current_prompt_block_marker"):
        prompt = _current_prompt_index(lines)
        if prompt is None:
            return ""
        block = next((i for i in range(prompt - 1, -1, -1) if _block_marker(lines[i])), None)
        if block is None:
            return ""
        if region == "current_prompt_block_marker":
            return lines[block]
        return _from_line(content, lines, block)
    if region == "prompt_box_body":
        top = _prompt_box_top(lines)
        if top is None:
            return ""
        end = next((i for i in range(top + 1, len(lines)) if _is_rule(lines[i])), len(lines))
        return content[_line_start(content, lines, top + 1): _line_start(content, lines, end)]
    if region in ("above_prompt_box", "last_non_empty_above_prompt_box"):
        top = _prompt_box_top(lines)
        above = content if top is None else content[: _line_start(content, lines, top)]
        if region == "above_prompt_box":
            return above
        return next((line for line in reversed(_lines(above)) if line.strip()), "")
    if region == "after_last_horizontal_rule":
        offset = last_end = 0
        for line in _lines(content):
            offset += len(line) + 1
            if _is_rule(line):
                last_end = min(offset, len(content))
        return content[last_end:]
    match = _COUNTED.match(region)
    if match:
        name, count = match.group(1), int(match.group(2))
        if name == "bottom_lines":
            return _from_line(content, lines, max(len(lines) - count, 0))
        non_empty = [i for i, line in enumerate(lines) if line.strip()]
        if name == "bottom_non_empty_lines":
            chosen = non_empty[-count:] if count else []
            return _from_line(content, lines, chosen[0]) if chosen else ""
        chosen = non_empty[:count]
        if not chosen:
            return ""
        return content[: _line_start(content, lines, chosen[-1] + 1)]
    return ""
