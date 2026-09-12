"""``/skill learn`` — turn the current session into a reusable skill (M6 §2).

The command replays this session's history to the provider, asks for a JSON
summary (``name``, ``description``, ``steps``, ``commands``) and writes it as
``<workdir>/.snowpea/skills/<name>/SKILL.md``.  Reloading the skill loader
afterwards is what makes ``/<name>`` a command.

``skill.learn`` is deliberately *not* an RPC method: the contract lists
``skill.search|install|list|reload`` and nothing else, so learning stays a
slash command.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from snowpea_core.agent.definition import (
    DefinitionError,
    complete_text,
    parse_generated_json,
    validate_name,
)
from snowpea_core.commands.agent_cmd import reload_loader, strip_quotes
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.prompts.loader import load
from snowpea_core.providers.base import ChatMessage
from snowpea_core.server import errors
from snowpea_core.session import events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.commands.skill")

SKILL_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["learn"],
            "description": "'learn [name]' writes a SKILL.md from this session.",
        },
        "name": {"type": "string", "description": "Name for the new skill (optional)."},
    },
}

USAGE = "Usage: /skill learn [name]"

#: How many of the most recent messages are summarised.
MAX_TRANSCRIPT_MESSAGES = 60
#: Per-message clip so one huge tool result cannot fill the prompt.
MAX_MESSAGE_CHARS = 800

LEARN_SYSTEM = load("workflows/skill-learn")


def _message_text(message: ChatMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        ).strip()
    return str(content or "")


def transcript(session: Session, limit: int = MAX_TRANSCRIPT_MESSAGES) -> str:
    """Readable ``role: text`` transcript, including the tool calls."""
    lines: list[str] = []
    for message in session.history.snapshot()[-limit:]:
        text = _message_text(message).strip()
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + " …"
        role = message.role
        if role == "tool":
            lines.append(f"tool_result[{message.name or '?'}]: {text}")
            continue
        if text:
            lines.append(f"{role}: {text}")
        for call in message.tool_calls or []:
            lines.append(f"{role} calls tool {call.name} with {call.arguments}")
    return "\n".join(lines)


def learn_messages(body: str, requested_name: str) -> list[ChatMessage]:
    hint = f'\n\nName the skill "{requested_name}".' if requested_name else ""
    return [
        ChatMessage(role="system", content=LEARN_SYSTEM),
        ChatMessage(
            role="user",
            content=f"Session transcript:\n\n{body}\n\nReply with the JSON object only.{hint}",
        ),
    ]


def render_skill_md(name: str, description: str, steps: list[str], commands: list[str]) -> str:
    """The SKILL.md text: frontmatter, numbered steps, example commands."""
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}" if description else "description: Learned from a session.",
        "---",
        "",
        f"# {name}",
        "",
    ]
    if steps:
        lines.append("## Steps")
        lines.append("")
        for index, step in enumerate(steps, start=1):
            lines.append(f"{index}. {step}")
        lines.append("")
    if commands:
        lines.append("## Example commands")
        lines.append("")
        lines.append("```bash")
        lines.extend(commands)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def write_skill(workdir: Path | str, name: str, text: str) -> Path:
    """Write ``<workdir>/.snowpea/skills/<name>/SKILL.md`` and return the path."""
    directory = Path(workdir) / ".snowpea" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


async def learn_skill(core: Core, session: Session, requested_name: str) -> Path:
    """Summarise ``session`` into a SKILL.md; raises :class:`DefinitionError`."""
    body = transcript(session)
    if not body.strip():
        raise DefinitionError("this session has no history to learn from yet")
    provider = core.providers.get(session.provider, session.model)
    text = await complete_text(provider, learn_messages(body, requested_name))
    data = parse_generated_json(text)
    name = validate_name(requested_name or str(data.get("name") or ""))
    document = render_skill_md(
        name,
        str(data.get("description") or "").strip(),
        _string_list(data.get("steps")),
        _string_list(data.get("commands")),
    )
    path = write_skill(session.workdir, name, document)
    await reload_loader(core)
    return path


async def cmd_skill(ctx: CommandContext, args: str) -> None:
    """``/skill learn [name]``."""
    action, _, rest = args.strip().partition(" ")
    action = action.lower()
    if not action:
        await ctx.say(USAGE)
        return
    if action != "learn":
        await ctx.say(
            f"/skill only handles 'learn' here; use the snowpea CLI for "
            f"search/install/list.\n{USAGE}"
        )
        return
    requested = strip_quotes(rest)
    try:
        path = await learn_skill(ctx.core, ctx.session, requested)
    except DefinitionError as exc:
        await ctx.emit(events.error(errors.INVALID_PARAMS, str(exc)))
        await ctx.say(f"Could not learn a skill: {exc}")
        return
    await ctx.say(f"Learned skill '{path.parent.name}' at {path}. Run it with /{path.parent.name}.")


COMMANDS: tuple[Command, ...] = (
    Command(
        name="skill",
        summary="Turn this session into a reusable skill: /skill learn [name].",
        run=cmd_skill,
        args_schema=SKILL_ARGS_SCHEMA,
    ),
)


__all__ = [
    "COMMANDS",
    "LEARN_SYSTEM",
    "SKILL_ARGS_SCHEMA",
    "USAGE",
    "cmd_skill",
    "learn_messages",
    "learn_skill",
    "render_skill_md",
    "transcript",
    "write_skill",
]
