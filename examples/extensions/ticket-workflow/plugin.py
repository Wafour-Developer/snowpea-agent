"""ticket-workflow: several capabilities working together.

* config validated by plugin.json's configSchema (project key, tracker URL);
* an async apply() that opens an HTTP client, closed by an async on_dispose;
* a network tool, ticket_get, that reads a ticket through that client;
* a prompt section with the commit-message rule for the configured project;
* a runtime-context provider naming the ticket of the current branch;
* a /ticket command that shows it with a link;
* a module of its own (tracker.py), imported relatively.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from snowpea_core.extensions import (
    Command,
    CommandContext,
    ContextProvider,
    PluginContext,
    PromptInputs,
    PromptSection,
    Tool,
    ToolContext,
    ToolResult,
)

from . import tracker


async def apply(ctx: PluginContext) -> None:
    project: str = ctx.config["project"]
    client = tracker.Tracker(ctx.config["baseUrl"], ctx.config.get("token", ""))
    # Registered first, so it runs last: after everything that uses the client is gone.
    ctx.on_dispose(client.close)

    async def ticket_get(tool_ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        ticket = str(args["id"]).strip().upper()
        if not ticket.startswith(f"{project}-"):
            return ToolResult(ok=False, error=f"ticket ids look like {project}-123")
        try:
            data = await client.get(ticket)
        except tracker.TrackerError as exc:
            return ToolResult(ok=False, error=str(exc))
        title = data.get("title", "")
        status = data.get("status", "unknown")
        return ToolResult(
            ok=True,
            output=f"{ticket}: {title} [{status}]\n{client.link(ticket)}",
            meta={"ticket": ticket, "status": status},
        )

    ctx.tools.register(
        Tool(
            name="ticket_get",
            category="tickets",
            description=f"Read a {project} ticket: title, status and link.",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string", "description": f"e.g. {project}-123"}},
                "required": ["id"],
            },
            permission="network",  # goes online: accept mode asks first, plan and auto run it
            run=ticket_get,
        ),
        timeout=30,
    )

    def commit_rule(inputs: PromptInputs) -> str:
        return f"Start every commit subject with the ticket id ({project}-123: ...)."

    ctx.prompt.section(PromptSection("ticket-workflow:commits", 660, commit_rule))

    def current_ticket(core: Any, session: Any) -> str:
        ticket = tracker.ticket_in_branch(Path(session.workdir), project)
        return f"Current ticket (from the branch name): {ticket}" if ticket else ""

    ctx.prompt.context(ContextProvider("ticket-workflow:current", 510, current_ticket))

    async def show(cmd_ctx: CommandContext, args: str) -> None:
        ticket = tracker.ticket_in_branch(Path(cmd_ctx.session.workdir), project)
        if not ticket:
            await cmd_ctx.say(f"This branch is not named after a {project} ticket.")
            return
        await cmd_ctx.say(f"{ticket}: {client.link(ticket)}")

    ctx.commands.register(Command(name="ticket", summary="show the branch's ticket", run=show))
