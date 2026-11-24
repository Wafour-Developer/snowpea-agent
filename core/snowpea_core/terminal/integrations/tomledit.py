"""Line-level edit of Codex's ``config.toml``: turn ``[features] hooks`` on, and back.

Codex only runs ``hooks.json`` when ``features.hooks = true``.  The file is the
person's, so the edit is one line: no TOML round trip, no reformatting.  What
was done is returned as a small record (kept in ``integrations/state.json``)
so uninstall can undo exactly that — herdr leaves the flag on; we do not.

Only the top-level ``[features]`` table and a top-level ``features.hooks``
dotted key are looked at (nested tables such as ``[profiles.x.features]`` are
not ours to change).

Portions derived from herdr (https://github.com/herdrdev/herdr), Apache-2.0
(``build_codex_config_with_hooks`` in ``src/integration/config_edit.rs``).
"""

from __future__ import annotations

import re
import tomllib
from typing import Any

ON_LINE = "hooks = true"
_HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_ARRAY_HEADER = re.compile(r"^\s*\[\[")
_HOOKS_KEY = re.compile(r"^\s*hooks\s*=")
_DOTTED_KEY = re.compile(r"^\s*features\s*\.\s*hooks\s*=")
_FEATURES_INLINE = re.compile(r"^\s*features\s*=")


class TomlEditError(ValueError):
    pass


def hooks_enabled(text: str) -> bool:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"config.toml does not parse: {exc}") from exc
    features = data.get("features")
    return isinstance(features, dict) and features.get("hooks") is True


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _features_span(lines: list[str]) -> tuple[int, int] | None:
    """``(header index, end index)`` of the top-level ``[features]`` table."""
    start = None
    for index, line in enumerate(lines):
        array = _ARRAY_HEADER.match(line)
        header = None if array else _HEADER.match(line)
        if not array and header is None:
            continue
        if start is not None:
            return start, index
        if header is not None and header.group(1).replace(" ", "") == "features":
            start = index
    return (start, len(lines)) if start is not None else None


def enable_hooks(text: str) -> tuple[str, dict[str, Any] | None]:
    """Return ``(new text, change record)``; the record is ``None`` when nothing changed."""
    if hooks_enabled(text):
        return text, None
    lines = _lines(text)
    nl = _nl(text)
    top_end = next((i for i, line in enumerate(lines)
                    if _HEADER.match(line) or _ARRAY_HEADER.match(line)), len(lines))
    for index in range(top_end):
        if _FEATURES_INLINE.match(lines[index]) or _DOTTED_KEY.match(lines[index]):
            # ``features = { … }`` or ``features.hooks = false`` at the top level.
            if _DOTTED_KEY.match(lines[index]):
                original = lines[index]
                ending = original[len(original.rstrip("\r\n")):] or nl
                lines[index] = "features.hooks = true" + ending
                out = "".join(lines)
                _verify(out)
                return out, {"op": "replace", "line": index, "original": original,
                             "written": lines[index]}
            raise TomlEditError(
                "config.toml sets `features` inline; add `hooks = true` to it by hand")
    span = _features_span(lines)
    if span is not None:
        header, end = span
        for index in range(header + 1, end):
            if _HOOKS_KEY.match(lines[index]):
                original = lines[index]
                ending = original[len(original.rstrip("\r\n")):] or nl
                lines[index] = ON_LINE + ending
                out = "".join(lines)
                _verify(out)
                return out, {"op": "replace", "line": index, "original": original,
                             "written": lines[index]}
        if not lines[header].endswith(("\n", "\r")):
            lines[header] += nl
        lines.insert(header + 1, ON_LINE + nl)
        out = "".join(lines)
        _verify(out)
        return out, {"op": "insert", "line": header + 1, "written": ON_LINE + nl}
    prefix = ""
    if text and not text.endswith("\n"):
        prefix = nl
    if text.strip():
        prefix += nl
    block = f"{prefix}[features]{nl}{ON_LINE}{nl}"
    out = text + block
    _verify(out)
    return out, {"op": "append", "written": block}


def _verify(text: str) -> None:
    if not hooks_enabled(text):  # pragma: no cover - would be a bug in the edit
        raise TomlEditError("edit did not turn features.hooks on")


def undo(text: str, change: dict[str, Any]) -> str:
    """Undo ``change`` (from :func:`enable_hooks`) if the line is still as we left it."""
    op = change.get("op")
    written = str(change.get("written") or "")
    if op == "append":
        if text.endswith(written):
            return text[: len(text) - len(written)]
        # Someone added to the table since: drop only our line.
        op = "insert"
    lines = _lines(text)
    span = _features_span(lines)
    if op == "insert":
        if span is None:
            return text
        header, end = span
        for index in range(header + 1, end):
            if lines[index].rstrip("\r\n") == ON_LINE:
                del lines[index]
                return "".join(lines)
        return text
    if op == "replace":
        original = str(change.get("original") or "")
        for index in range(len(lines)):
            if lines[index] == written:
                lines[index] = original
                return "".join(lines)
        return text
    return text
