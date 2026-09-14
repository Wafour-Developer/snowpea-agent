"""``/skill learn|publish|sources`` (M6 §2, M6-M7 §1, CORE-registry-client §4b).

``/skill learn`` replays this session's history to the provider, asks for a
JSON summary (``name``, ``description``, ``steps``, ``commands``) and writes
it as ``<workdir>/.snowpea/skills/<name>/SKILL.md``.  Reloading the skill
loader afterwards is what makes ``/<name>`` a command.

``/skill publish <dir>`` and ``/skill sources`` forward to the same code the
CLI's ``snowpea skill publish``/``snowpea skill sources`` use
(:mod:`snowpea_core.skills.publish` and
:mod:`snowpea_core.skills.registry_client`), so a session can ship a skill to
the hosted registry, or see which hubs it federates, without dropping to a
shell.  All three stay slash commands rather than RPC methods: the contract's
``skill.*`` RPC surface is ``search|install|list|reload|remove`` and nothing
else.
"""

from __future__ import annotations

import logging
import shlex
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
from snowpea_core.skills.generate import (
    SkillCreateError,
    generate_skill_document,
    skill_root,
    write_skill_document,
)
from snowpea_core.skills.publish import PublishError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core
    from snowpea_core.session.session import Session

log = logging.getLogger("snowpea.commands.skill")

SKILL_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "learn", "publish", "sources", "reload", "list"],
            "description": (
                "'create <name> \"<brief>\" [--global]' generates a SKILL.md from a brief; "
                "'learn [name]' writes a SKILL.md from this session; "
                "'publish <dir>' uploads it to the registry; "
                "'sources' lists the hubs the registry federates; "
                "'reload' re-scans skills, agents, commands and plugin MCP servers after "
                "a SKILL.md was edited by hand; 'list' shows what is installed."
            ),
        },
        "name": {"type": "string", "description": "Name for the new skill (optional)."},
    },
}

USAGE = (
    'Usage: /skill create <name> "<what it should do>" [--global] [--force] | '
    "/skill learn [name] | /skill publish <dir> | /skill sources | /skill reload | /skill list"
)

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


def parse_create_args(args: str) -> tuple[str, str, bool, bool]:
    """``'<name> "<brief>" [--global] [--force]'`` -> ``(name, brief, global_, force)``."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    global_ = False
    force = False
    positional: list[str] = []
    for token in tokens:
        if token == "--global":
            global_ = True
        elif token == "--force":
            force = True
        else:
            positional.append(token)
    name = positional[0] if positional else ""
    description = " ".join(positional[1:]).strip()
    return name, description, global_, force


def _home_dir(core: Core) -> Path:
    paths = getattr(core, "paths", None)
    return Path(paths.home) if paths is not None else Path.home() / ".snowpea"


async def create_skill(ctx: CommandContext, args: str) -> None:
    """``/skill create <name> "<brief>" [--global] [--force]``."""
    raw_name, description, global_, force = parse_create_args(args)
    if not raw_name or not description:
        await ctx.say(USAGE)
        return
    try:
        name = validate_name(raw_name)
    except DefinitionError as exc:
        await ctx.say(f"Could not create the skill: {exc}")
        return
    directory = skill_root(
        name, workdir=ctx.session.workdir, home=_home_dir(ctx.core), global_=global_
    )
    path = directory / "SKILL.md"
    scope = "global" if global_ else "project"
    if ctx.session.mode == "plan":
        status = "would overwrite" if path.is_file() else "would create"
        await ctx.say(f"Plan mode: {status} {scope} skill '{name}' at {path}; nothing is written.")
        return
    if path.is_file() and not force:
        await ctx.say(f"'{path}' already exists; pass --force to overwrite it.")
        return
    provider = ctx.core.providers.get(ctx.session.provider, ctx.session.model)
    try:
        document = await generate_skill_document(provider, name, description)
        write_skill_document(directory, document, requested_name=name, force=force)
    except (PublishError, SkillCreateError) as exc:
        await ctx.say(f"Could not create the skill: {exc}")
        return
    await reload_loader(ctx.core)
    await ctx.say(f"Created skill '{name}' at {path}. Run it with /{name}.")


async def publish_skill(ctx: CommandContext, directory: str) -> None:
    """``/skill publish <dir>`` — the same zip-and-upload path as the CLI."""
    from snowpea_core.skills import publish as publish_mod
    from snowpea_core.skills import registry_client

    candidate = Path(directory).expanduser()
    if not candidate.is_absolute():
        candidate = Path(ctx.session.workdir) / candidate
    try:
        package = publish_mod.load_skill_dir(candidate)
    except publish_mod.PublishError as exc:
        await ctx.emit(events.error(errors.INVALID_PARAMS, str(exc)))
        await ctx.say(f"Could not publish: {exc}")
        return

    url = registry_client.resolve_url(settings=ctx.core.settings)
    token = registry_client.resolve_token(settings=ctx.core.settings)
    if not token:
        await ctx.say(
            f"Could not publish: no registry token configured for {url}. "
            "Set one with `snowpea setup tools`, SNOWPEA_REGISTRY_TOKEN, or "
            "`snowpea skill publish --token <t>` from a shell."
        )
        return

    client = registry_client.HttpRegistryClient(url)
    zip_bytes = publish_mod.build_zip(package)
    try:
        result = await client.publish(zip_bytes, filename=f"{package.name}.zip", token=token)
    except registry_client.RegistryError as exc:
        await ctx.emit(events.error(errors.INTERNAL, str(exc)))
        await ctx.say(f"Publish failed: {exc}")
        return

    created = "Published" if result.get("created") else "Updated"
    await ctx.say(
        f"{created} '{package.name}' to {url}. "
        f"Install it with `snowpea skill install registry:{package.name}`."
    )


async def sources_skill(ctx: CommandContext) -> None:
    """``/skill sources`` — every hub the registry federates, and its health."""
    from snowpea_core.skills import registry_client

    url = registry_client.resolve_url(settings=ctx.core.settings)
    client = registry_client.HttpRegistryClient(url)
    try:
        sources = await client.sources()
    except registry_client.RegistryError as exc:
        await ctx.emit(events.error(errors.INTERNAL, str(exc)))
        await ctx.say(f"Could not reach {url}: {exc}")
        return
    if not sources:
        await ctx.say(f"{url}: no sources reported")
        return
    lines = []
    for row in sources:
        sid = str(row.get("id", ""))
        label = str(row.get("label", ""))
        status = "enabled" if row.get("enabled") else f"disabled ({row.get('disabledReason')})"
        count = row.get("count")
        suffix = f" — {count} skills" if count is not None else ""
        lines.append(f"- {sid} ({label}): {status}{suffix}")
    await ctx.say("\n".join(lines))


async def reload_skills(ctx: CommandContext) -> None:
    """``/skill reload`` — re-scan after a hand edit, without reinstalling anything."""
    loader = getattr(ctx.core, "skills", None)
    reload_fn = getattr(loader, "reload", None) if loader is not None else None
    if not callable(reload_fn):
        await ctx.say("The skill loader is not wired; nothing to reload.")
        return
    try:
        report = await reload_fn()
    except Exception as exc:  # noqa: BLE001 - a broken SKILL.md must not fail the command
        await ctx.say(f"Reload failed: {exc}")
        return
    await ctx.say(
        "Reloaded: "
        f"{report.skills} skills, {report.agents} agents, {report.commands} commands, "
        f"{report.plugins} plugins, {report.hooks} hooks."
    )


async def list_skills(ctx: CommandContext) -> None:
    """``/skill list`` — installed skills, agents and commands by source."""
    loader = getattr(ctx.core, "skills", None)
    list_fn = getattr(loader, "list", None) if loader is not None else None
    if not callable(list_fn):
        await ctx.say("The skill loader is not wired; nothing installed.")
        return
    rows = list_fn()
    if not rows:
        await ctx.say("No skills installed. Try /skill create <name> \"<brief>\".")
        return
    lines = [f"- /{row.name} ({row.kind}, {row.source})" for row in rows]
    await ctx.say("\n".join(lines))


async def cmd_skill(ctx: CommandContext, args: str) -> None:
    """``/skill create|learn|publish|sources|reload|list``."""
    action, _, rest = args.strip().partition(" ")
    action = action.lower()
    if not action:
        await ctx.say(USAGE)
        return
    if action == "create":
        await create_skill(ctx, rest)
        return
    if action == "publish":
        directory = strip_quotes(rest)
        if not directory:
            await ctx.say(USAGE)
            return
        await publish_skill(ctx, directory)
        return
    if action == "sources":
        await sources_skill(ctx)
        return
    if action == "reload":
        await reload_skills(ctx)
        return
    if action == "list":
        await list_skills(ctx)
        return
    if action != "learn":
        await ctx.say(
            "/skill only handles 'create', 'learn', 'publish', 'sources', 'reload' and 'list' "
            f"here; use the snowpea CLI for search/install/rate.\n{USAGE}"
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
    "create_skill",
    "learn_messages",
    "learn_skill",
    "parse_create_args",
    "publish_skill",
    "render_skill_md",
    "sources_skill",
    "transcript",
    "write_skill",
]
