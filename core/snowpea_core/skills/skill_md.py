"""Front-matter parsing for ``SKILL.md``, ``agents/*.md`` and ``commands/*.md``.

The Claude Code / agentskills.io format is a YAML block delimited by ``---``
lines at the very top of the file, followed by the body:

.. code-block:: text

    ---
    name: hello
    description: Say hello.
    argument-hint: "[name]"
    allowed-tools: [read_file, glob]
    ---
    Greet $ARGUMENTS warmly.

PyYAML is not a dependency of the daemon, so this module reads the small subset
the format actually uses: scalars, quoted scalars, booleans, inline ``[a, b]``
lists and ``- item`` block lists.  Anything it does not understand is kept as a
string, which is what every consumer here wants anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The delimiter that opens and closes the front matter.
FENCE = "---"

TRUE_WORDS = {"true", "yes", "on"}
FALSE_WORDS = {"false", "no", "off"}


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """``(frontmatter, body)``; an empty dict when the file has no front matter."""
    lines = text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != FENCE:
        return {}, text
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == FENCE:
            block = lines[start + 1 : index]
            body = "\n".join(lines[index + 1 :])
            return parse_block(block), body.lstrip("\n")
    return {}, text


def parse_block(lines: list[str]) -> dict[str, Any]:
    """Parse the YAML subset described in the module docstring."""
    data: dict[str, Any] = {}
    key: str | None = None
    pending: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") or stripped == "-":
            if key is not None:
                pending.append(_scalar(stripped[1:].strip()))
                data[key] = list(pending)
            continue
        head, sep, tail = line.partition(":")
        if not sep or head.strip() != head.lstrip():
            # A continuation line of a folded scalar; append to the last key.
            if key is not None and isinstance(data.get(key), str):
                data[key] = f"{data[key]} {stripped}".strip()
            continue
        key = head.strip()
        pending = []
        value = tail.strip()
        data[key] = _scalar(value) if value else ""
    return data


def _scalar(value: str) -> Any:
    """One YAML scalar: quoted string, inline list, bool, int or plain string."""
    text = value.strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_items(inner)]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in TRUE_WORDS:
        return True
    if lowered in FALSE_WORDS:
        return False
    if lowered in ("null", "~"):
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _split_items(inner: str) -> list[str]:
    """Split ``a, "b, c", d`` on the commas that are not inside quotes."""
    items: list[str] = []
    buffer: list[str] = []
    quote = ""
    for char in inner:
        if quote:
            if char == quote:
                quote = ""
            buffer.append(char)
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
            continue
        if char == ",":
            items.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    if buffer:
        items.append("".join(buffer))
    return [item.strip() for item in items if item.strip()]


def as_list(value: Any) -> list[str]:
    """Normalise ``tools: "*"`` / ``tools: [a, b]`` / ``tools: a, b`` to a list."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in TRUE_WORDS


@dataclass
class SkillDoc:
    """One parsed ``SKILL.md`` (or Claude Code ``commands/*.md``)."""

    name: str
    description: str = ""
    body: str = ""
    argument_hint: str = ""
    user_invocable: bool = True
    #: ``None`` means "every tool"; a list restricts the turn the command starts.
    allowed_tools: list[str] | None = None
    path: Path | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)

    def render(self, arguments: str = "") -> str:
        """The body with ``$ARGUMENTS`` substituted (appended when absent)."""
        args = arguments.strip()
        if "$ARGUMENTS" in self.body:
            return self.body.replace("$ARGUMENTS", args)
        if not args:
            return self.body
        return f"{self.body}\n\nArguments: {args}"


def parse_skill_md(text: str, *, default_name: str, path: Path | None = None) -> SkillDoc:
    """Parse a skill or command markdown file into a :class:`SkillDoc`."""
    front, body = split_frontmatter(text)
    raw_tools = front.get("allowed-tools", front.get("allowedTools"))
    tools = as_list(raw_tools)
    return SkillDoc(
        name=str(front.get("name") or default_name).strip() or default_name,
        description=str(front.get("description") or "").strip(),
        body=body.strip(),
        argument_hint=str(front.get("argument-hint") or front.get("argumentHint") or "").strip(),
        user_invocable=as_bool(front.get("user-invocable", front.get("userInvocable")), True),
        allowed_tools=tools or None if raw_tools not in (None, "", "*") else None,
        path=path,
        frontmatter=front,
    )


def load_skill_md(path: Path, *, default_name: str | None = None) -> SkillDoc | None:
    """Read and parse ``path``; ``None`` when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fallback = default_name or path.stem
    return parse_skill_md(text, default_name=fallback, path=path)


__all__ = [
    "FENCE",
    "SkillDoc",
    "as_bool",
    "as_list",
    "load_skill_md",
    "parse_block",
    "parse_skill_md",
    "split_frontmatter",
]
