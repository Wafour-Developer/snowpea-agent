"""``/vision`` — the model that looks at images for a model that cannot.

``models.categories.vision`` (1.33.0) names a model whose description of each
picture joins the stored message as text, for a session whose own model has no
vision (see :mod:`snowpea_core.agent.image_describe`).  ``/vision`` shows it,
lists the models that could take the job, sets it and clears it.

``/vision`` lists; ``/vision <profile>|<vendor:model>|<number>`` sets;
``/vision off`` clears.  Setting writes the global settings file, so it holds
for every session and survives a restart.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.commands.model_cmd import _configured_models
from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.config.model_routing import category_route, model_config_for, resolve_reference

USAGE = "Usage: /vision [<profile-id>|<vendor:model>|<number>|off]"

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Profile id, vendor:model pair or its number in the listing. 'off' "
                "clears the vision model. Omit to show the current one and the choices."
            ),
        }
    },
}


def _current_ref(ctx: CommandContext) -> str:
    categories = getattr(ctx.core.settings.models, "categories", None) or {}
    return str(categories.get("vision") or "").strip()


def _describe_current(ctx: CommandContext) -> str:
    ref = _current_ref(ctx)
    if not ref:
        return "vision model: not set (images reach a text-only model as a short note)"
    route = category_route(ctx.core.settings, "vision", workdir=ctx.session.workdir)
    if not route.resolved():
        return f"vision model: {ref} (does not resolve to a configured model)"
    return f"vision model: {route.provider}:{route.model}"


async def _list(ctx: CommandContext) -> None:
    available = await _configured_models(ctx)
    ref = _current_ref(ctx)
    lines = [_describe_current(ctx), ""]
    for index, (vendor, model) in enumerate(available, start=1):
        mark = "*" if ref and ref == f"{vendor}:{model}" else " "
        lines.append(f" {mark} {index}. {vendor}:{model}")
    if not available:
        lines.append("No models were found for the configured providers.")
    lines += ["", USAGE]
    await ctx.say("\n".join(lines))


async def cmd_vision(ctx: CommandContext, args: str) -> None:
    """Show, set or clear the model that describes images."""
    wanted = args.strip()
    if not wanted:
        await _list(ctx)
        return
    categories = ctx.core.settings.models.categories
    if wanted in ("off", "none", "clear", "inherit"):
        categories.pop("vision", None)
        ctx.core.settings.save(ctx.core.paths)
        ctx.core.mark_settings_saved()
        await ctx.say("vision model cleared; images reach a text-only model as a short note")
        return
    if wanted.isdigit():
        available = await _configured_models(ctx)
        index = int(wanted)
        if not 1 <= index <= len(available):
            await ctx.say(f"there is no configured model number {index}.\n{USAGE}")
            return
        vendor, model = available[index - 1]
        wanted = f"{vendor}:{model}"
    config = model_config_for(ctx.core.settings, ctx.session.workdir)
    if not resolve_reference(ctx.core.settings, wanted, config=config).resolved():
        await ctx.say(f"vision: {wanted!r} is not a profile or a vendor:model\n{USAGE}")
        return
    vendor = wanted.partition(":")[0].strip()
    profiles = model_config_for(ctx.core.settings, ctx.session.workdir).profiles
    if wanted not in profiles and vendor not in ctx.core.providers.known_vendors():
        await ctx.say(f"vision: unknown provider {vendor!r}\n{USAGE}")
        return
    categories["vision"] = wanted
    ctx.core.settings.save(ctx.core.paths)
    ctx.core.mark_settings_saved()
    await ctx.say(_describe_current(ctx))


VISION_COMMAND = Command(
    name="vision",
    summary="Show or set the model that describes images: /vision [<name>|<number>|off].",
    run=cmd_vision,
    args_schema=ARGS_SCHEMA,
)

COMMANDS: tuple[Command, ...] = (VISION_COMMAND,)

__all__ = ["ARGS_SCHEMA", "COMMANDS", "USAGE", "VISION_COMMAND", "cmd_vision"]
