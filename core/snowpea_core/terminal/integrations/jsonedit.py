"""Format-preserving edits of a strict JSON document.

A CLI's config (``~/.claude/settings.json``, ``hooks.json``) belongs to the
person: an install must change only the bytes it adds, and an uninstall must
give the same bytes back.  So instead of ``json.loads`` + ``json.dumps`` this
module parses the text into nodes that remember where they sit, and every edit
is one splice of the original text:

* :func:`insert_member` / :func:`append_item` add after the last entry, copying
  the separator, indentation and newline style found next to it (a compact
  one-line container stays compact);
* :func:`remove_member` / :func:`remove_item` cut an entry together with the
  comma and whitespace that join it to its neighbour, so a remove undoes an
  insert byte for byte; a container left empty collapses to ``{}`` / ``[]``.

Each edit re-parses its result and checks it equals the intended value
(:class:`JsonEditError` otherwise), the same safety net herdr's
``verify_updated`` gives.  Comments, trailing commas and duplicate keys are
refused: these files are strict JSON, and an edit of something we cannot
parse exactly is worse than none.

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0
(the edit rules of ``src/integration/claude_settings.rs``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

Path = tuple[str | int, ...]


class JsonEditError(ValueError):
    """The text is not strict JSON, or an edit would not give the intended value."""


@dataclass
class Node:
    start: int
    end: int
    kind: str  # "object" | "array" | "scalar"
    value: Any = None
    members: list[Member] = field(default_factory=list)  # object
    items: list[Node] = field(default_factory=list)  # array


@dataclass
class Member:
    key: str
    key_start: int
    value: Node

    @property
    def start(self) -> int:
        return self.key_start

    @property
    def end(self) -> int:
        return self.value.end


_WS = " \t\r\n"
_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def fail(self, what: str) -> JsonEditError:
        line = self.text.count("\n", 0, self.pos) + 1
        return JsonEditError(f"{what} at line {line}")

    def ws(self) -> None:
        text, pos = self.text, self.pos
        while pos < len(text) and text[pos] in _WS:
            pos += 1
        self.pos = pos

    def value(self, depth: int = 0) -> Node:
        if depth > 200:
            raise self.fail("nesting too deep")
        self.ws()
        if self.pos >= len(self.text):
            raise self.fail("unexpected end of document")
        ch = self.text[self.pos]
        if ch == "{":
            return self.obj(depth)
        if ch == "[":
            return self.arr(depth)
        if ch == '"':
            start = self.pos
            return Node(start, self.pos, "scalar", self.string())
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.text.startswith(word, self.pos):
                start = self.pos
                self.pos += len(word)
                return Node(start, self.pos, "scalar", val)
        match = _NUMBER.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return Node(match.start(), match.end(), "scalar", json.loads(match.group()))
        raise self.fail("unexpected character (comments and trailing commas are not allowed)")

    def string(self) -> str:
        match = _STRING.match(self.text, self.pos)
        if not match:
            raise self.fail("bad string")
        self.pos = match.end()
        return json.loads(match.group())

    def obj(self, depth: int) -> Node:
        node = Node(self.pos, -1, "object")
        self.pos += 1
        seen: set[str] = set()
        self.ws()
        if self.text.startswith("}", self.pos):
            self.pos += 1
            node.end = self.pos
            return node
        while True:
            self.ws()
            if not self.text.startswith('"', self.pos):
                raise self.fail("expected a key")
            key_start = self.pos
            key = self.string()
            if key in seen:
                raise self.fail(f"duplicate key {key!r}")
            seen.add(key)
            self.ws()
            if not self.text.startswith(":", self.pos):
                raise self.fail("expected ':'")
            self.pos += 1
            node.members.append(Member(key, key_start, self.value(depth + 1)))
            self.ws()
            if self.text.startswith(",", self.pos):
                self.pos += 1
                continue
            if self.text.startswith("}", self.pos):
                self.pos += 1
                node.end = self.pos
                return node
            raise self.fail("expected ',' or '}'")

    def arr(self, depth: int) -> Node:
        node = Node(self.pos, -1, "array")
        self.pos += 1
        self.ws()
        if self.text.startswith("]", self.pos):
            self.pos += 1
            node.end = self.pos
            return node
        while True:
            node.items.append(self.value(depth + 1))
            self.ws()
            if self.text.startswith(",", self.pos):
                self.pos += 1
                continue
            if self.text.startswith("]", self.pos):
                self.pos += 1
                node.end = self.pos
                return node
            raise self.fail("expected ',' or ']'")


def parse(text: str) -> Node:
    """Parse ``text`` (strict JSON, an optional BOM is kept out of the nodes)."""
    parser = _Parser(text)
    if text.startswith("﻿"):
        parser.pos = 1
    node = parser.value()
    parser.ws()
    if parser.pos != len(text):
        raise parser.fail("extra text after the document")
    return node


def loads(text: str) -> Any:
    """The value of ``text`` (strict, duplicate keys refused)."""
    return to_value(parse(text))


def to_value(node: Node) -> Any:
    if node.kind == "object":
        return {m.key: to_value(m.value) for m in node.members}
    if node.kind == "array":
        return [to_value(item) for item in node.items]
    return node.value


def find(root: Node, path: Path) -> Node | None:
    node: Node | None = root
    for step in path:
        if node is None:
            return None
        if isinstance(step, int):
            if node.kind != "array" or not 0 <= step < len(node.items):
                return None
            node = node.items[step]
        else:
            if node.kind != "object":
                return None
            node = next((m.value for m in node.members if m.key == step), None)
    return node


# -- style detection -----------------------------------------------------------


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _indent_unit(text: str) -> str:
    """The file's indentation step: a tab, or the smallest run of leading spaces."""
    smallest = 0
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if not stripped:
            continue
        lead = line[: len(line) - len(stripped)]
        if lead.startswith("\t"):
            return "\t"
        if lead and (smallest == 0 or len(lead) < smallest):
            smallest = len(lead)
    return " " * (smallest or 2)


def _line_indent(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    match = re.match(r"[ \t]*", text[line_start:pos])
    return match.group() if match else ""


def _kv_sep_anywhere(text: str, root: Node) -> str:
    """The ``:`` separator as the file spells it (``": "`` by default)."""
    stack = [root]
    while stack:
        node = stack.pop()
        for member in node.members:
            key = _STRING.match(text, member.key_start)
            sep = text[key.end() : member.value.start] if key else ""
            if sep.strip() == ":" and "\n" not in sep:
                return sep
        stack.extend(m.value for m in node.members)
        stack.extend(node.items)
    return ": "


def _render(value: Any, *, multiline: bool, base: str, unit: str, kv: str, item_sep: str,
            nl: str) -> str:
    if not multiline:
        return json.dumps(value, ensure_ascii=False, separators=(item_sep, kv))
    out = json.dumps(value, ensure_ascii=False, indent=unit, separators=(",", kv))
    return out.replace("\n", nl + base)


# -- edits ---------------------------------------------------------------------


def _container(root: Node, path: Path, kind: str) -> Node:
    node = find(root, path)
    if node is None or node.kind != kind:
        raise JsonEditError(f"{'/'.join(map(str, path)) or 'document'} is not an {kind}")
    return node


def _entries(node: Node) -> list[tuple[int, int]]:
    if node.kind == "object":
        return [(m.start, m.end) for m in node.members]
    return [(i.start, i.end) for i in node.items]


def _append(text: str, root: Node, node: Node, rendered_for: Any) -> str:
    """Splice a new last entry into ``node`` (object member text or array item)."""
    nl = _newline(text)
    unit = _indent_unit(text)
    kv = _kv_sep_anywhere(text, root)
    entries = _entries(node)
    if entries:
        if len(entries) >= 2:
            gap_from = text.index(",", entries[-2][1]) + 1
        else:
            gap_from = node.start + 1
        sep = text[gap_from : entries[-1][0]]
        multiline = "\n" in sep
        base = sep.rsplit("\n", 1)[-1] if multiline else ""
        item_sep = ", " if multiline else "," + sep
        body = rendered_for(multiline, base, unit, kv, item_sep, nl)
        insert = "," + sep + body
        pos = entries[-1][1]
        return text[:pos] + insert + text[pos:]
    # Empty container: expand it when the document is laid out over lines (or
    # the container is the whole document), keep it compact inside a one-liner.
    multiline = ("\n" in text) or node is root
    if multiline:
        outer = _line_indent(text, node.start)
        base = outer + unit
        body = rendered_for(True, base, unit, kv, ", ", nl)
        inner = nl + base + body + nl + outer
    else:
        inner = rendered_for(False, "", unit, kv, ", ", nl)
    return text[: node.start + 1] + inner + text[node.end - 1 :]


def _checked(text: str, expected: Any) -> str:
    try:
        value = loads(text)
    except JsonEditError as exc:  # pragma: no cover - would be a bug in the splices
        raise JsonEditError(f"edit produced invalid JSON: {exc}") from exc
    if value != expected:  # pragma: no cover - would be a bug in the splices
        raise JsonEditError("edit did not produce the intended value")
    return text


def insert_member(text: str, path: Path, key: str, value: Any) -> str:
    """Add ``key: value`` as the last member of the object at ``path``."""
    root = parse(text)
    node = _container(root, path, "object")
    if any(m.key == key for m in node.members):
        raise JsonEditError(f"{key!r} already exists")
    key_text = json.dumps(key, ensure_ascii=False)

    def render(multiline: bool, base: str, unit: str, kv: str, item_sep: str, nl: str) -> str:
        return key_text + kv + _render(value, multiline=multiline, base=base, unit=unit, kv=kv,
                                       item_sep=item_sep, nl=nl)

    expected = to_value(root)
    target = _walk(expected, path)
    target[key] = value
    return _checked(_append(text, root, node, render), expected)


def append_item(text: str, path: Path, value: Any) -> str:
    """Add ``value`` as the last item of the array at ``path``."""
    root = parse(text)
    node = _container(root, path, "array")

    def render(multiline: bool, base: str, unit: str, kv: str, item_sep: str, nl: str) -> str:
        return _render(value, multiline=multiline, base=base, unit=unit, kv=kv,
                       item_sep=item_sep, nl=nl)

    expected = to_value(root)
    _walk(expected, path).append(value)
    return _checked(_append(text, root, node, render), expected)


def _cut(text: str, node: Node, index: int) -> str:
    entries = _entries(node)
    start, end = entries[index]
    if len(entries) == 1:
        return text[: node.start + 1] + text[node.end - 1 :]
    if index == len(entries) - 1:
        return text[: entries[index - 1][1]] + text[end:]
    return text[:start] + text[entries[index + 1][0] :]


def remove_member(text: str, path: Path, key: str) -> str:
    """Remove member ``key`` of the object at ``path`` with its separator."""
    root = parse(text)
    node = _container(root, path, "object")
    index = next((i for i, m in enumerate(node.members) if m.key == key), None)
    if index is None:
        return text
    expected = to_value(root)
    del _walk(expected, path)[key]
    return _checked(_cut(text, node, index), expected)


def remove_item(text: str, path: Path, index: int) -> str:
    """Remove item ``index`` of the array at ``path`` with its separator."""
    root = parse(text)
    node = _container(root, path, "array")
    if not 0 <= index < len(node.items):
        raise JsonEditError(f"no item {index}")
    expected = to_value(root)
    del _walk(expected, path)[index]
    return _checked(_cut(text, node, index), expected)


def _walk(value: Any, path: Path) -> Any:
    for step in path:
        value = value[step]
    return value
