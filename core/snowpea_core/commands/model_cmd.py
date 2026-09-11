"""``/model`` — show or change the model this session talks to.

``/model`` lists what the session's vendor actually serves (``GET /models`` on
an OpenAI-compatible endpoint), marking the current one.  ``/model <name>``
switches the session and remembers the choice in
``settings.providers.<vendor>.model`` so the next session starts there too.
A bare number picks that row of the listing.

This is the escape hatch for the ``local`` preset, whose ``default_model`` is
the placeholder ``local-model``: a vLLM server answers it with
``HTTP 404: The model 'local-model' does not exist``.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.providers.base import ProviderError

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Model id to switch to, or its number in the listing. "
                "Omit to list the vendor's models."
            ),
        }
    },
}

USAGE = "Usage: /model [<name>|<number>]   (no argument lists the vendor's models)"


def _vendor_of(ctx: CommandContext) -> str:
    return ctx.session.provider or ctx.core.providers.default_vendor()


def _current(ctx: CommandContext, vendor: str) -> str:
    return ctx.session.model or ctx.core.providers.model_for(vendor)


def _persist(ctx: CommandContext, vendor: str, name: str) -> None:
    """Remember the choice for the next session; a read-only home just skips."""
    try:
        ctx.core.providers.configure(vendor, {"model": name})
    except ProviderError:
        return
    ctx.core.providers.save()


async def _list(ctx: CommandContext, vendor: str) -> None:
    current = _current(ctx, vendor)
    try:
        available = await ctx.core.providers.list_models(vendor)
    except ProviderError as exc:
        await ctx.say(
            f"{vendor}: could not list models ({exc}).\n"
            f"Current model: {current or 'unset'}.\n{USAGE}"
        )
        return
    if not available:
        await ctx.say(
            f"{vendor} listed no models.\nCurrent model: {current or 'unset'}.\n{USAGE}"
        )
        return
    lines = [f"Models for {vendor}:"]
    for index, name in enumerate(available, 1):
        mark = "*" if name == current else " "
        lines.append(f" {mark} {index}. {name}")
    lines.append("")
    lines.append(USAGE)
    await ctx.say("\n".join(lines))


async def cmd_model(ctx: CommandContext, args: str) -> None:
    """Show the vendor's models, or switch this session to one."""
    vendor = _vendor_of(ctx)
    wanted = args.strip()
    if not wanted:
        await _list(ctx, vendor)
        return
    if wanted.isdigit():
        try:
            available = await ctx.core.providers.list_models(vendor)
        except ProviderError as exc:
            await ctx.say(f"{vendor}: could not list models ({exc}).\n{USAGE}")
            return
        index = int(wanted)
        if not 1 <= index <= len(available):
            await ctx.say(f"{vendor} has no model number {index}.\n{USAGE}")
            return
        wanted = available[index - 1]
    ctx.session.model = wanted
    _persist(ctx, vendor, wanted)
    await ctx.say(f"model: {wanted}")


MODEL_COMMAND = Command(
    name="model",
    summary="Show or change the model: /model [<name>|<number>].",
    run=cmd_model,
    args_schema=ARGS_SCHEMA,
)

COMMANDS: tuple[Command, ...] = (MODEL_COMMAND,)


__all__ = ["ARGS_SCHEMA", "COMMANDS", "MODEL_COMMAND", "USAGE", "cmd_model"]
