"""``/model`` — show or change the model this session talks to.

``/model`` lists the configured **model profiles** (``models.profiles``, global
merged with the project's, with ``models.default`` marked) and then what the
session's vendor actually serves (``GET /models`` on an OpenAI-compatible
endpoint), marking the current one.  ``/model <ref>`` pins this session:
``<ref>`` is a profile id, a ``vendor:model`` pair, a bare vendor, or a plain
model id from the vendor listing.  A bare number picks that row of the listing,
and ``/model inherit`` clears the pin.

The pin is persisted on the **session row**, so it survives a restart and a
``session.resume`` (it used to live only in memory plus
``settings.providers.<vendor>.model``, and was lost on restore).
``/model default <id>`` writes ``models.default`` instead — the setting every
new session starts from.

This is the escape hatch for the ``local`` preset, whose ``default_model`` is
the placeholder ``local-model``: a vLLM server answers it with
``HTTP 404: The model 'local-model' does not exist``.
"""

from __future__ import annotations

from typing import Any

from snowpea_core.commands.registry import Command, CommandContext
from snowpea_core.config.model_routing import ModelRoute, model_config_for, resolve_reference
from snowpea_core.providers.base import ProviderError
from snowpea_core.session import events

ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Profile id, vendor:model pair, model id, or its number in the "
                "listing. 'inherit' clears the pin, 'default <id>' sets "
                "models.default. Omit to list profiles and the vendor's models."
            ),
        }
    },
}

USAGE = (
    "Usage: /model [<profile-id>|<vendor:model>|<name>|<number>|inherit]"
    " | /model default <profile-id>"
)


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


def _profile_lines(ctx: CommandContext) -> list[str]:
    """The configured profiles, project merged over global, default marked."""
    settings = ctx.core.settings
    config = model_config_for(settings, ctx.session.workdir)
    if not config.profiles:
        return []
    active = config.project_default or config.default
    lines = ["Model profiles:"]
    for name in sorted(config.profiles):
        profile = config.profiles[name]
        mark = "*" if name == active else " "
        lines.append(f" {mark} {name}  ({profile.provider}:{profile.model})")
    return [*lines, ""]


async def _configured_models(ctx: CommandContext) -> list[tuple[str, str]]:
    """All selectable models, ordered exactly as the combined listing."""
    found: list[tuple[str, str]] = []
    for info in ctx.core.providers.list():
        if not info.configured:
            continue
        try:
            listing = await ctx.core.providers.model_listing(info.vendor)
        except ProviderError:
            continue
        found.extend((info.vendor, model) for model in listing.models)
    return found


async def _list(ctx: CommandContext, vendor: str) -> None:
    current = _current(ctx, vendor)
    profiles = _profile_lines(ctx)
    configured = [info for info in ctx.core.providers.list() if info.configured]
    lines = [*profiles]
    index = 0
    for info in configured:
        try:
            listing = await ctx.core.providers.model_listing(info.vendor)
        except ProviderError as exc:
            lines.append(f"Models for {info.vendor}: unavailable ({exc})")
            continue
        if not listing.models and listing.error:
            lines.append(f"{info.vendor}: could not list models ({listing.error})")
            continue
        lines.append(f"Models for {info.vendor} ({listing.detail}):")
        for name in listing.models:
            index += 1
            mark = "*" if info.vendor == vendor and name == current else " "
            lines.append(f" {mark} {index}. {info.vendor}:{name}")
        lines.append("")
    if index == 0:
        lines.append("No models were found for the configured providers.")
    lines.append("")
    lines.append(USAGE)
    await ctx.say("\n".join(lines))


async def _set_default(ctx: CommandContext, profile_id: str) -> None:
    """``/model default <id>`` — write ``models.default`` and adopt it live."""
    settings = ctx.core.settings
    if profile_id not in settings.models.profiles:
        known = ", ".join(sorted(settings.models.profiles)) or "none configured"
        await ctx.say(f"model: unknown profile {profile_id!r}; known profiles: {known}")
        return
    settings.models.default = profile_id
    settings.save(ctx.core.paths)
    ctx.core.mark_settings_saved()
    await ctx.say(f"default model profile is now {profile_id}")


async def cmd_model(ctx: CommandContext, args: str) -> None:
    """Show profiles and the vendor's models, or pin this session to one."""
    vendor = _vendor_of(ctx)
    wanted = args.strip()
    if not wanted:
        await _list(ctx, vendor)
        return

    head, _, tail = wanted.partition(" ")
    if head == "default":
        await _set_default(ctx, tail.strip())
        return

    if wanted.isdigit():
        available = await _configured_models(ctx)
        index = int(wanted)
        if not 1 <= index <= len(available):
            await ctx.say(f"there is no configured model number {index}.\n{USAGE}")
            return
        selected_vendor, selected_model = available[index - 1]
        wanted = f"{selected_vendor}:{selected_model}"

    # A profile id / vendor:model / bare vendor goes through the shared pin so
    # it persists and emits the same event as ``session.setModel``.  A plain
    # model id of the current vendor does not resolve as a reference, and is
    # pinned against the session's own vendor instead.
    config = model_config_for(ctx.core.settings, ctx.session.workdir)
    known = resolve_reference(ctx.core.settings, wanted, config=config).resolved()
    if wanted == "inherit" or known:
        try:
            route = await ctx.core.sessions.set_model(ctx.session, wanted)
        except ValueError as exc:
            await ctx.say(f"model: {exc}\n{USAGE}")
            return
    else:
        ctx.session.provider = vendor
        ctx.session.model = wanted
        if ctx.core.store is not None:
            await ctx.core.store.update_model(ctx.session.id, vendor, wanted)
        _persist(ctx, vendor, wanted)
        route = ModelRoute(vendor, wanted)
    await ctx.emit(events.model_changed(route.provider, route.model))
    await ctx.say(f"model: {route.provider or vendor}/{route.model or 'unset'}")


MODEL_COMMAND = Command(
    name="model",
    summary="Show or change the model: /model [<name>|<number>].",
    run=cmd_model,
    args_schema=ARGS_SCHEMA,
)
MODELS_COMMAND = Command(
    name="models",
    summary="Open the model list for every configured provider.",
    run=cmd_model,
    args_schema=ARGS_SCHEMA,
)

COMMANDS: tuple[Command, ...] = (MODEL_COMMAND, MODELS_COMMAND)


__all__ = ["ARGS_SCHEMA", "COMMANDS", "MODEL_COMMAND", "USAGE", "cmd_model"]
