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
import logging
import os
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage
from snowpea_core.tools.renames import canonical, canonical_names

log = logging.getLogger("snowpea.agent.definition")

#: Frontmatter delimiter.
FENCE = "---"

#: Names that may become ``<name>.md`` and ``/<name>``.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Where definitions live, relative to a project or the snowpea home.
PROJECT_DIRS: tuple[tuple[str, str], ...] = (
    (".snowpea/agents", "project"),
    (".claude/agents", "claude-project"),
)
HOME_DIRS: tuple[tuple[str, str], ...] = (
    ("agents", "global"),
    (".claude/agents", "claude-global"),
)

#: Built-in definitions shipped with the package (M15 §C4).  Unlike the role
#: files, these carry frontmatter — a tool allowlist above all — so ``explore``
#: and ``reviewer`` are genuinely read-only rather than read-only by request.
BUILTIN_DIR = Path(__file__).resolve().parent / "definitions"

#: Default value of ``tools`` — every registered tool.
ALL_TOOLS = "*"

#: Output budget for one JSON-generating call.  A reasoning model spends its
#: thinking from the same budget: at 2048 a Muse Spark /ralph PRD came back
#: cut off mid-string (``"verify": ["test -f … && grep -qi qu``).
DEFAULT_MAX_TOKENS = 8192

#: Extra attempts `complete_text` makes when the model answers with nothing.
EMPTY_REPLY_RETRIES = 2


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
    #: Tool rounds one delegated turn of this agent may make before it has to
    #: stop and report (CORE-subagent-budget).  ``None`` inherits
    #: ``agents.toolRounds`` and then the subagent default.
    tool_rounds: int | None = None
    #: Alias / frontmatter key for tool-round budget.
    max_tool_rounds: int | None = None
    #: ``"on"`` | ``"off"`` | ``"inherit"``.  A reviewer that genuinely wants
    #: hidden reasoning says ``thinking: on``; everything else inherits, which
    #: for a delegated turn means off (CORE-reasoning-budget).
    thinking: str = "inherit"
    #: ``"lean"`` | ``"full"`` — what a delegated run of this agent starts
    #: with.  ``None`` follows ``agents.childContext``.  ``full`` keeps the
    #: skills index and the memory recall block a lean child goes without
    #: (snowpea-browser addendum 22: a ``browser`` agent needs its site skills).
    child_context: str | None = None
    #: ``"own"``: the definition's prompt replaces the core coding identity
    #: (base.md, execution, plan continuity, memory guidance), keeping only
    #: the core essentials (honesty, reply length, trust, language).  Applies
    #: when ``agents.ownIdentity`` (env ``SNOWPEA_AGENT_IDENTITY``) is on.
    identity: str | None = None
    #: Tool names or ``prefix_*`` patterns sent by name only, loaded with
    #: ``tool_search`` (frontmatter ``deferredTools``), even when the tool list
    #: or a host attached them.  Applies when ``tools.agentDeferred`` (env
    #: ``SNOWPEA_AGENT_DEFERRED``) is on, on a provider whose tool list stays
    #: fixed for the session (local-style servers).
    deferred_tools: list[str] = field(default_factory=list)
    #: What to tell the model when it reaches for a tool this agent does not
    #: have (frontmatter ``unavailable``, 1.55.0): ``{<namespace or pattern>:
    #: "<message>"}``, e.g. ``mcp: "MCP tools are not available to this agent;
    #: call request_code_mode …"``.  Used by unknown-tool and not-allowed
    #: answers and by a ``tool_search`` that matched nothing.
    unavailable: dict[str, str] = field(default_factory=dict)
    #: ``"plugin"``: the skills index lists only this definition's plugin's
    #: skills and the built-ins, under a softer header (frontmatter
    #: ``skillsIndex``).  ``None`` lists every visible skill.
    skills_index: str | None = None
    #: The agent an unnamed ``delegate_task`` from a session running this
    #: definition goes to (frontmatter ``defaultChild``).  A plugin agent with
    #: none delegates to its own plugin's agents, then to itself.
    default_child: str | None = None
    prompt: str = ""
    #: Where it was read from, when it came off disk.
    path: Path | None = None
    #: ``builtin`` | ``global`` | ``project`` | ``claude-global`` |
    #: ``claude-project`` | ``claude-plugin`` | ``codex-global`` |
    #: ``codex-plugin`` | ``plugin:<name>``.
    source: str = "project"
    _source_key: str | None = None

    def __post_init__(self) -> None:
        if self.max_tool_rounds is not None and self.tool_rounds is None:
            self.tool_rounds = self.max_tool_rounds
        elif self.tool_rounds is not None and self.max_tool_rounds is None:
            self.max_tool_rounds = self.tool_rounds

    def tool_list(self) -> list[str] | None:
        """``None`` when the definition allows every tool."""
        if isinstance(self.tools, str):
            return None if self.tools.strip() == ALL_TOOLS else [canonical(self.tools.strip())]
        return canonical_names(self.tools)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "tools": self.tools,
            "permission": self.permission,
            "max_turns": self.max_turns,
            "tool_rounds": self.tool_rounds,
            "max_tool_rounds": self.max_tool_rounds,
            "thinking": self.thinking,
            "child_context": self.child_context,
            "identity": self.identity,
            "deferred_tools": list(self.deferred_tools),
            "unavailable": dict(self.unavailable),
            "skills_index": self.skills_index,
            "default_child": self.default_child,
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


def _positive_int(value: Any) -> int | None:
    """A frontmatter number that has to be at least 1, or ``None``."""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def _child_context(value: Any) -> str | None:
    """``lean`` | ``full`` from frontmatter; anything else means "not set"."""
    text = str(value or "").strip().lower()
    return text if text in ("lean", "full") else None


def _choice(value: Any, allowed: tuple[str, ...]) -> str | None:
    """A frontmatter keyword out of ``allowed``; anything else means "not set"."""
    text = str(value or "").strip().lower()
    return text if text in allowed else None


def _names(value: Any) -> list[str]:
    """A frontmatter list of names (or one comma-separated string)."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


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


def _unquote(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if value[0] == '"':
            try:
                return str(json.loads(value))
            except ValueError:
                pass
        return value[1:-1]
    return value


def unavailable_map(text: str) -> dict[str, str]:
    """Frontmatter ``unavailable`` (1.55.0): an inline JSON object or an indented block.

    ::

        unavailable:
          mcp: "MCP tools are not available to this agent; call request_code_mode"
          "browser_*": "…"

    The shared front-matter reader keeps no nested maps, so the block is read
    here from the raw lines.  Anything malformed is skipped, never fatal.
    """
    lines = text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != FENCE:
        return {}
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == FENCE:
            break
        block.append(line)
    found: dict[str, str] = {}
    for index, line in enumerate(block):
        if not line.startswith("unavailable:"):
            continue
        inline = line[len("unavailable:") :].strip()
        if inline.startswith("{"):
            try:
                raw = json.loads(inline)
            except ValueError:
                return {}
            return {
                str(key): str(value)
                for key, value in (raw.items() if isinstance(raw, dict) else [])
                if str(key).strip() and isinstance(value, str) and value.strip()
            }
        for row in block[index + 1 :]:
            if not row.strip() or row.lstrip().startswith("#"):
                continue
            if not row[:1].isspace():
                break
            stripped = row.strip()
            if stripped.startswith('"'):
                end = stripped.find('"', 1)
                key, rest = stripped[1:end], stripped[end + 1 :]
            else:
                key, _, rest = stripped.partition(":")
                rest = ":" + rest
            rest = rest.strip()
            if not rest.startswith(":"):
                continue
            value = _unquote(rest[1:])
            if key.strip() and value:
                found[key.strip()] = value
        break
    return found


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
    rounds_val = defn.max_tool_rounds if defn.max_tool_rounds is not None else defn.tool_rounds
    if rounds_val is not None:
        key = getattr(defn, "_source_key", None) or "max_tool_rounds"
        fields.append((key, rounds_val))
    if defn.thinking != "inherit":
        fields.append(("thinking", defn.thinking))
    if defn.child_context is not None:
        fields.append(("childContext", defn.child_context))
    if defn.identity:
        fields.append(("identity", defn.identity))
    if defn.deferred_tools:
        fields.append(("deferredTools", defn.deferred_tools))
    if defn.skills_index:
        fields.append(("skillsIndex", defn.skills_index))
    if defn.default_child:
        fields.append(("defaultChild", defn.default_child))
    head = "\n".join(f"{key}: {_render_value(value)}" for key, value in fields)
    if defn.unavailable:
        rows = "\n".join(
            f"  {json.dumps(str(key), ensure_ascii=False)}: "
            f"{json.dumps(str(text), ensure_ascii=False)}"
            for key, text in defn.unavailable.items()
        )
        head = f"{head}\nunavailable:\n{rows}"
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
    raw_max_rounds = meta.get("max_tool_rounds")
    raw_tool_rounds = meta.get("tool_rounds")
    source_key = (
        "tool_rounds"
        if raw_tool_rounds is not None
        else ("max_tool_rounds" if raw_max_rounds is not None else None)
    )
    parsed_rounds = _positive_int(
        raw_max_rounds if raw_max_rounds is not None else raw_tool_rounds
    )
    return AgentDefinition(
        name=validate_name(raw_name),
        description=str(meta.get("description") or ""),
        model=str(meta.get("model") or "inherit"),
        tools=tools,
        permission=str(meta.get("permission") or "inherit"),
        max_turns=max_turns,
        tool_rounds=parsed_rounds,
        max_tool_rounds=parsed_rounds,
        thinking=str(meta.get("thinking") or "inherit"),
        child_context=_child_context(meta.get("childContext", meta.get("child_context"))),
        identity=_choice(meta.get("identity"), ("own", "core")),
        deferred_tools=_names(meta.get("deferredTools", meta.get("deferred_tools"))),
        unavailable=unavailable_map(text),
        skills_index=_choice(
            meta.get("skillsIndex", meta.get("skills_index")), ("plugin", "all", "none")
        ),
        default_child=str(meta.get("defaultChild") or meta.get("default_child") or "").strip()
        or None,
        prompt=body,
        path=path,
        source=source,
        _source_key=source_key,
    )


#: Codex ``model_reasoning_effort`` values that mean "do not think".
CODEX_NO_EFFORT = frozenset({"none", "minimal"})


def parse_codex_agent_toml(path: Path | str, *, source: str = "codex-global") -> AgentDefinition:
    """Read one Codex agent role (``~/.codex/agents/<name>.toml``).

    ``name``, ``description`` and ``model`` carry over as they are;
    ``developer_instructions`` is the prompt; ``model_reasoning_effort``
    becomes ``thinking`` (``none``/``minimal`` off, any other level on);
    ``sandbox_mode = "read-only"`` becomes ``permission: plan`` — a role may
    narrow its child's mode, never widen it, so the other sandbox modes
    inherit.  Everything else (``nickname_candidates``, ``mcp_servers``,
    ``approval_policy``, provider and tool settings) has no equivalent here
    and is ignored.
    """
    import tomllib

    file_path = Path(path)
    try:
        with file_path.open("rb") as handle:
            meta = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DefinitionError(f"cannot read Codex agent {file_path}: {exc}") from exc
    raw_name = str(meta.get("name") or file_path.stem).strip()
    effort = str(meta.get("model_reasoning_effort") or "").strip().lower()
    thinking = "inherit" if not effort else ("off" if effort in CODEX_NO_EFFORT else "on")
    sandbox = str(meta.get("sandbox_mode") or "").strip().lower()
    return AgentDefinition(
        name=validate_name(raw_name),
        description=str(meta.get("description") or ""),
        model=str(meta.get("model") or "inherit"),
        permission="plan" if sandbox == "read-only" else "inherit",
        thinking=thinking,
        prompt=str(meta.get("developer_instructions") or ""),
        path=file_path,
        source=source,
    )


# ---------------------------------------------------------------------------
# discovery (fallback when the skill loader is not available)
# ---------------------------------------------------------------------------


def definition_dirs(workdir: Path | str | None, home: Path | str | None) -> list[tuple[Path, str]]:
    """``(directory, source)`` pairs in precedence order (later wins).

    ``home`` is ``SNOWPEA_HOME``.  Nothing outside it and the project is
    walked: the label a ``.claude`` definition ends up with is decided by the
    root it was read from, not by guessing at the operating system home.
    """
    dirs: list[tuple[Path, str]] = []
    if home:
        dirs.extend((Path(home) / relative, source) for relative, source in HOME_DIRS)
    if workdir:
        for relative, source in PROJECT_DIRS:
            dirs.append((Path(workdir) / relative, source))
    return dirs


def builtin_agent_definitions() -> list[AgentDefinition]:
    """Built-in role prompts exposed as resolvable delegate agents.

    ``delegate_task(agent=...)`` already knows how to compose
    ``prompts/roles/<name>.md`` when an agent definition with that name exists.
    These synthetic definitions make those package roles visible through
    ``/agent list`` / ``agent.list`` without writing files into a user's project.
    """
    found: dict[str, AgentDefinition] = {}
    roles_dir = Path(__file__).resolve().parent.parent / "prompts" / "roles"
    if roles_dir.is_dir():
        for path in sorted(roles_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                name = validate_name(path.stem)
                text = path.read_text(encoding="utf-8")
            except (DefinitionError, OSError):
                continue
            meta, body = split_frontmatter(text)
            rounds = _positive_int(meta.get("max_tool_rounds") or meta.get("tool_rounds"))
            source_key = (
                "tool_rounds"
                if "tool_rounds" in meta
                else ("max_tool_rounds" if "max_tool_rounds" in meta else None)
            )
            found[name] = AgentDefinition(
                name=name,
                description=_role_description(body if meta else text, name),
                model="inherit",
                tools=ALL_TOOLS,
                permission="inherit",
                tool_rounds=rounds,
                max_tool_rounds=rounds,
                prompt="",
                path=path,
                source="builtin",
                _source_key=source_key,
            )
    if BUILTIN_DIR.is_dir():
        for path in sorted(BUILTIN_DIR.glob("*.md")):
            try:
                defn = parse_agent_md(path, source="builtin")
            except DefinitionError:  # pragma: no cover - a broken shipped file
                continue
            found[defn.name] = defn
    return sorted(found.values(), key=lambda entry: entry.name)


def _role_description(text: str, name: str) -> str:
    """A short, stable description for a built-in role file."""
    paragraph: list[str] = []
    fallback = f"Built-in {name} role."
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.lower().startswith("role:"):
            fallback = line.rstrip(".") + "."
            continue
        if line.startswith(("-", "*", "#")):
            if paragraph:
                break
            continue
        paragraph.append(line)
    return " ".join(paragraph) if paragraph else fallback


def discover_definitions(
    workdir: Path | str | None = None, home: Path | str | None = None
) -> list[AgentDefinition]:
    """Scan the definition directories; later roots win on a name clash."""
    found: dict[str, AgentDefinition] = {}
    project_sources = {source for _, source in PROJECT_DIRS}
    for directory, source in definition_dirs(workdir, home):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            project = bool(workdir) and source in project_sources
            before = _digest(path) if project else ""
            try:
                defn = parse_agent_md(path, source=source)
            except DefinitionError:
                continue
            # Security review 7, M7-7: the pin is compared with the digest of what
            # was parsed — the same before and after the parse, or not at all.
            if project and (before != _digest(path) or not _project_definition_active(
                    workdir, home, path, defn, before)):  # type: ignore[arg-type]
                log.info("agent %r of %s is not active: the project is not trusted for "
                         "agents (or the file changed since)", defn.name, workdir)
                continue
            found[defn.name] = defn
    return list(found.values())


def _digest(path: Path) -> str:
    from snowpea_core.trust.projects import digest_of

    return digest_of(path)


def _project_definition_active(workdir: Path | str, home: Path | str | None, path: Path,
                               defn: AgentDefinition, digest: str | None = None) -> bool:
    """Every project definition needs the project's ``agents`` trust, with the file
    as pinned (security review 6, L-1, as decided; review 7)."""
    try:
        from snowpea_core.trust.projects import agent_definition_active, store_for

        store = store_for(home) if home else None
        return agent_definition_active(workdir, path, defn.name, defn.permission, store,
                                       digest=digest)
    except Exception:  # noqa: BLE001 - trust fails closed
        return False


def write_definition(defn: AgentDefinition, workdir: Path | str) -> Path:
    """Write ``defn`` to ``<workdir>/.snowpea/agents/<name>.md`` and return it.

    snowpea writes this file itself, for the person who asked for the agent
    (``/agent create``): like a rule or a setting the person changes through
    snowpea, it is pinned as written when what was there before was trusted or
    nothing was there (:meth:`TrustStore.person_wrote`).  A definition with a
    ``permission`` of its own still waits for the person's trust.
    """
    directory = Path(workdir) / PROJECT_DIRS[0][0]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{defn.name}.md"
    before = _digest(path)
    path.write_text(render_agent_md(defn), encoding="utf-8")
    defn.path = path
    defn.source = "project"
    if defn.permission in (None, "", "inherit"):
        try:
            from snowpea_core.trust.projects import (
                builtin_agent_names,
                default_store,
                project_root,
            )

            if defn.name in builtin_agent_names():
                return path  # it would take a built-in agent's place: the person decides
            base = project_root(workdir)
            rel = os.path.relpath(os.path.realpath(path), base).replace(os.sep, "/")
            default_store().person_wrote(base, rel, before)
        except Exception:  # noqa: BLE001 - unpinned: it waits for the person's trust
            log.debug("could not pin the new agent definition", exc_info=True)
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
    provider: Any,
    messages: list[ChatMessage],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    response_format: dict[str, Any] | None = None,
) -> str:
    """Run one non-tool provider turn and return the concatenated text.

    An empty reply is asked for again, up to :data:`EMPTY_REPLY_RETRIES` times.
    A reasoning model now and then stops after its thinking with no answer at
    all (``finish_reason: stop``, zero content) — measured at one in three for
    the ``/team`` planner's prompt on a local Qwen. Every caller here parses the
    text as JSON, so that one flake used to fail a whole ``/team``, ``/ralph`` or
    agent generation outright.

    ``response_format`` is passed through only when given; the caller decides
    whether this provider takes it.
    """
    extra: dict[str, Any] = {"response_format": response_format} if response_format else {}
    text = ""
    for _attempt in range(1 + EMPTY_REPLY_RETRIES):
        chunks: list[str] = []
        async for event in provider.stream(messages, [], max_tokens=max_tokens, **extra):
            if event.kind == "text_delta" and event.text:
                chunks.append(event.text)
        text = "".join(chunks)
        if text.strip():
            break
    return text


#: Permissions a generated definition may carry: the default, or narrower.
GENERATED_PERMISSIONS: frozenset[str] = frozenset({"inherit", "plan", "accept"})


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
    # Security review 5, H1: a generated definition never grants more than the
    # project's default — the model writing it cannot pick a wider mode (a
    # person who wants one sets it in the file and approves it at agent.create).
    permission = str(data.get("permission") or "inherit").strip() or "inherit"
    if permission not in GENERATED_PERMISSIONS:
        permission = "inherit"
    return AgentDefinition(
        name=name,
        description=str(data.get("description") or description).strip(),
        model=str(data.get("model") or "inherit").strip() or "inherit",
        tools=tools,
        permission=permission,
        max_turns=max_turns,
        tool_rounds=_positive_int(data.get("tool_rounds")),
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
    "BUILTIN_DIR",
    "AgentDefinition",
    "DefinitionError",
    "GENERATOR_SYSTEM",
    "complete_text",
    "definition_dirs",
    "definition_from_payload",
    "discover_definitions",
    "generate_definition",
    "generator_messages",
    "builtin_agent_definitions",
    "parse_agent_md",
    "parse_agent_text",
    "parse_codex_agent_toml",
    "parse_generated_json",
    "render_agent_md",
    "slugify",
    "validate_name",
    "write_definition",
]
