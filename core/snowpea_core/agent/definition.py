"""Agent definitions and the generator behind ``/agent create`` (M6 contract §2).

An agent definition is a markdown file with YAML-ish frontmatter::

    ---
    name: release-notes
    description: Writes release notes from a changelog.
    model: inherit
    tools: "*"
    permission: inherit
    max_turns: 20
    ---
    You write crisp release notes...

The body is the agent's system prompt.  :func:`parse_agent_md` and
:func:`render_agent_md` are inverses of each other, and
:func:`generate_definition` asks a chat provider to invent one from a
one-line brief.

The frontmatter parser is deliberately hand-rolled: the project has no YAML
dependency, and the contract only ever asks for scalars, inline lists and
block lists.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage

#: Frontmatter delimiter.
FENCE = "---"

#: Names that may become ``<name>.md`` and ``/<name>``.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Where definitions live, relative to a project or the snowpea home.
PROJECT_DIRS: tuple[str, ...] = (".snowpea/agents", ".claude/agents")
HOME_DIR = "agents"

#: Default value of ``tools`` — every registered tool.
ALL_TOOLS = "*"

DEFAULT_MAX_TOKENS = 2048


class DefinitionError(ValueError):
    """A definition could not be parsed, generated or validated."""


# ---------------------------------------------------------------------------
# the dataclass
# ---------------------------------------------------------------------------


@dataclass
class AgentDefinition:
    """One ``agents/<name>.md`` file."""

    name: str
    description: str = ""
    model: str = "inherit"
    tools: list[str] | str = ALL_TOOLS
    permission: str = "inherit"
    max_turns: int | None = None
    prompt: str = ""
    #: Where it was read from, when it came off disk.
    path: Path | None = None
    #: ``builtin`` | ``global`` | ``project`` | ``plugin:<name>``.
    source: str = "project"

    def tool_list(self) -> list[str] | None:
        """``None`` when the definition allows every tool."""
        if isinstance(self.tools, str):
            return None if self.tools.strip() == ALL_TOOLS else [self.tools.strip()]
        return list(self.tools)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "permission": self.permission,
            "max_turns": self.max_turns,
            "prompt": self.prompt,
            "source": self.source,
            "path": str(self.path) if self.path else None,
        }


# ---------------------------------------------------------------------------
# slugs
# ---------------------------------------------------------------------------


def slugify(value: str, *, fallback: str = "") -> str:
    """Turn free text into a filename-safe, command-safe slug.

    Non-ASCII text (the brief may well be Korean) has no useful
    transliteration here, so it is dropped; an empty result is the caller's
    signal to fall back or fail.
    """
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text or fallback


def validate_name(name: str) -> str:
    """Return a valid slug for ``name`` or raise :class:`DefinitionError`."""
    candidate = name.strip()
    if not SLUG_RE.match(candidate):
        candidate = slugify(candidate)
    if not candidate or not SLUG_RE.match(candidate):
        raise DefinitionError(f"agent name is not usable as a file name or command: {name!r}")
    return candidate


# ---------------------------------------------------------------------------
# frontmatter
# ---------------------------------------------------------------------------


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_unquote(part) for part in inner.split(",") if part.strip()]
    text = _unquote(raw)
    if text in ("true", "false"):
        return text == "true"
    if text == "null" or text == "~":
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """``("---\\nname: x\\n---\\nbody")`` -> ``({"name": "x"}, "body")``."""
    stripped = text.lstrip("﻿")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != FENCE:
        return {}, stripped.strip()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == FENCE:
            end = index
            break
    if end is None:
        return {}, stripped.strip()
    meta = _parse_block(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _parse_block(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    key: str | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key is not None:
            item = _unquote(line.lstrip()[2:])
            existing = meta.get(key)
            if isinstance(existing, list):
                existing.append(item)
            else:
                meta[key] = [item]
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip()
        if not key:
            continue
        if not value.strip():
            meta[key] = []
        else:
            meta[key] = _parse_scalar(value)
    return meta


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(item)) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text == "" or re.search(r"[:#\[\]{}\"'*&!|>%@`,]|^\s|\s$", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def render_agent_md(defn: AgentDefinition) -> str:
    """Serialise a definition back to ``agents/<name>.md`` text."""
    fields: list[tuple[str, Any]] = [
        ("name", defn.name),
        ("description", defn.description),
        ("model", defn.model),
        ("tools", defn.tools),
        ("permission", defn.permission),
    ]
    if defn.max_turns is not None:
        fields.append(("max_turns", defn.max_turns))
    head = "\n".join(f"{key}: {_render_value(value)}" for key, value in fields)
    return f"{FENCE}\n{head}\n{FENCE}\n\n{defn.prompt.strip()}\n"


def parse_agent_md(path: Path | str, *, source: str = "project") -> AgentDefinition:
    """Read ``path`` into an :class:`AgentDefinition`."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DefinitionError(f"cannot read agent definition {file_path}: {exc}") from exc
    return parse_agent_text(text, path=file_path, source=source)


def parse_agent_text(
    text: str, *, path: Path | None = None, source: str = "project"
) -> AgentDefinition:
    """Parse definition text; ``path`` only supplies the fallback name."""
    meta, body = split_frontmatter(text)
    raw_name = str(meta.get("name") or (path.stem if path else ""))
    if not raw_name:
        raise DefinitionError(f"agent definition has no name: {path or '<text>'}")
    tools_value = meta.get("tools", ALL_TOOLS)
    tools: list[str] | str
    if isinstance(tools_value, list):
        tools = [str(item) for item in tools_value]
    else:
        tools = str(tools_value or ALL_TOOLS)
    max_turns_raw = meta.get("max_turns")
    max_turns: int | None
    try:
        max_turns = int(max_turns_raw) if max_turns_raw not in (None, "") else None
    except (TypeError, ValueError):
        max_turns = None
    return AgentDefinition(
        name=validate_name(raw_name),
        description=str(meta.get("description") or ""),
        model=str(meta.get("model") or "inherit"),
        tools=tools,
        permission=str(meta.get("permission") or "inherit"),
        max_turns=max_turns,
        prompt=body,
        path=path,
        source=source,
    )


# ---------------------------------------------------------------------------
# discovery (fallback when the skill loader is not available)
# ---------------------------------------------------------------------------


def definition_dirs(workdir: Path | str | None, home: Path | str | None) -> list[tuple[Path, str]]:
    """``(directory, source)`` pairs in precedence order (later wins)."""
    dirs: list[tuple[Path, str]] = []
    if home:
        dirs.append((Path(home) / HOME_DIR, "global"))
    if workdir:
        for relative in PROJECT_DIRS:
            dirs.append((Path(workdir) / relative, "project"))
    return dirs


def discover_definitions(
    workdir: Path | str | None = None, home: Path | str | None = None
) -> list[AgentDefinition]:
    """Scan the definition directories; later roots win on a name clash."""
    found: dict[str, AgentDefinition] = {}
    for directory, source in definition_dirs(workdir, home):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                defn = parse_agent_md(path, source=source)
            except DefinitionError:
                continue
            found[defn.name] = defn
    return list(found.values())


def write_definition(defn: AgentDefinition, workdir: Path | str) -> Path:
    """Write ``defn`` to ``<workdir>/.snowpea/agents/<name>.md`` and return it."""
    directory = Path(workdir) / PROJECT_DIRS[0]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{defn.name}.md"
    path.write_text(render_agent_md(defn), encoding="utf-8")
    defn.path = path
    defn.source = "project"
    return path


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------

GENERATOR_SYSTEM = load("workflows/agent-generate")


def generator_messages(description: str) -> list[ChatMessage]:
    """The two messages :func:`generate_definition` sends."""
    return [
        ChatMessage(role="system", content=GENERATOR_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                "Create an agent definition for this brief. "
                "Reply with the JSON object only.\n\n"
                f"Brief: {description}"
            ),
        ),
    ]


def _json_candidates(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` run in ``text``, outermost first."""
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    for block in fenced:
        yield block.strip()
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : index + 1]


def parse_generated_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Tolerant on purpose: real models wrap the object in prose or a fenced
    block, and the fake provider streams it in eight-character chunks.
    """
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise DefinitionError(
        "the model did not return a JSON object; got: " + (text.strip()[:200] or "an empty reply")
    )


async def complete_text(
    provider: Any, messages: list[ChatMessage], *, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    """Run one non-tool provider turn and return the concatenated text."""
    chunks: list[str] = []
    async for event in provider.stream(messages, [], max_tokens=max_tokens):
        if event.kind == "text_delta" and event.text:
            chunks.append(event.text)
    return "".join(chunks)


def definition_from_payload(data: dict[str, Any], description: str) -> AgentDefinition:
    """Validate a generator payload into an :class:`AgentDefinition`."""
    raw_name = str(data.get("name") or "").strip()
    if not raw_name:
        raise DefinitionError("the model's JSON has no 'name' field")
    name = validate_name(raw_name)
    tools_value = data.get("tools", ALL_TOOLS)
    tools: list[str] | str
    if isinstance(tools_value, list):
        tools = [str(item) for item in tools_value if str(item).strip()] or ALL_TOOLS
    elif isinstance(tools_value, str) and tools_value.strip():
        tools = tools_value.strip()
    else:
        tools = ALL_TOOLS
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        prompt = f"You are {name}. {description.strip()}"
    max_turns_raw = data.get("max_turns")
    try:
        max_turns = int(max_turns_raw) if max_turns_raw not in (None, "") else None
    except (TypeError, ValueError):
        max_turns = None
    return AgentDefinition(
        name=name,
        description=str(data.get("description") or description).strip(),
        model=str(data.get("model") or "inherit").strip() or "inherit",
        tools=tools,
        permission=str(data.get("permission") or "inherit").strip() or "inherit",
        max_turns=max_turns,
        prompt=prompt,
    )


async def generate_definition(provider: Any, description: str) -> AgentDefinition:
    """Ask ``provider`` for an agent definition matching ``description``."""
    brief = description.strip()
    if not brief:
        raise DefinitionError('describe the agent you want: /agent create "<description>"')
    text = await complete_text(provider, generator_messages(brief))
    return definition_from_payload(parse_generated_json(text), brief)


__all__ = [
    "ALL_TOOLS",
    "AgentDefinition",
    "DefinitionError",
    "GENERATOR_SYSTEM",
    "complete_text",
    "definition_dirs",
    "definition_from_payload",
    "discover_definitions",
    "generate_definition",
    "generator_messages",
    "parse_agent_md",
    "parse_agent_text",
    "parse_generated_json",
    "render_agent_md",
    "slugify",
    "validate_name",
    "write_definition",
]
