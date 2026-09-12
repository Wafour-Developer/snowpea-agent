"""``settings_get`` and ``settings_set``.

"Show me my web search settings" used to mean the model reached for
``read_file`` — and sometimes ``write_file`` — on ``settings.json``.  These two
tools give it the honest path instead: a read-only view of the configuration,
and a write that carries the ``config`` tag, so changing a setting is always
something the user agrees to first.

Secrets are masked on the way out (``api_key``, ``token``, …), so a settings
dump can never leak a key into the transcript.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from snowpea_core.config import hot_reload
from snowpea_core.config.patch import coerce, deep_merge, mask_secrets, nest, pluck
from snowpea_core.config.project import ProjectSettings
from snowpea_core.config.settings import Settings
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

log = logging.getLogger("snowpea.tools.settings")

#: Scopes both tools accept.
SCOPES = ("global", "project")


def _scope(args: dict[str, Any]) -> str:
    scope = str(args.get("scope", "global") or "global").strip().lower()
    return scope if scope in SCOPES else "global"


def _document(ctx: ToolContext, scope: str) -> dict[str, Any]:
    if scope == "project":
        project = ProjectSettings.load(ctx.session.workdir)
        return dict(project.model_dump(mode="json"))
    settings = getattr(ctx.core, "settings", None) or Settings()
    return dict(settings.model_dump(mode="json"))


def _render(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True)


async def settings_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Read the settings document, or one dotted key out of it."""
    scope = _scope(args)
    document = mask_secrets(_document(ctx, scope))
    key = str(args.get("key", "") or "").strip()
    if not key:
        return ToolResult(ok=True, output=_render(document), meta={"scope": scope})
    try:
        value = pluck(document, key)
    except KeyError:
        return ToolResult(ok=False, error=f"no such setting: {key} (scope {scope})")
    return ToolResult(
        ok=True, output=f"{key} = {_render(value)}", meta={"scope": scope, "key": key}
    )


async def settings_set(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Write one dotted key, validating the whole document before it persists."""
    key = str(args.get("key", "") or "").strip()
    if not key:
        return ToolResult(ok=False, error="key is required, e.g. search.provider")
    if "value" not in args:
        return ToolResult(ok=False, error="value is required")
    scope = _scope(args)
    value = coerce(args.get("value"))
    try:
        patch = nest(key, value)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    if scope == "project":
        current = ProjectSettings.load(ctx.session.workdir)
        merged = deep_merge(current.model_dump(mode="json"), patch)
        try:
            updated_project = ProjectSettings.model_validate(merged)
        except ValidationError as exc:
            return ToolResult(ok=False, error=f"invalid settings: {exc}")
        updated_project.save(ctx.session.workdir)
        return ToolResult(
            ok=True,
            output=f"set {key} in the project settings",
            meta={"scope": scope, "key": key},
        )

    settings = getattr(ctx.core, "settings", None) or Settings()
    before = settings.model_dump(mode="json")
    merged = deep_merge(before, patch)
    try:
        updated = Settings.model_validate(merged)
    except ValidationError as exc:
        return ToolResult(ok=False, error=f"invalid settings: {exc}")
    updated.save(ctx.core.paths)
    adopt = getattr(ctx.core, "adopt_settings", None)
    if callable(adopt):
        await adopt(updated, hot_reload.changed_keys(before, merged))
    return ToolResult(
        ok=True, output=f"set {key} in the global settings", meta={"scope": scope, "key": key}
    )


_SCOPE_SCHEMA = {
    "type": "string",
    "enum": list(SCOPES),
    "description": "global ($SNOWPEA_HOME/settings.json) or project (<workdir>/.snowpea).",
}


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="settings_get",
        category="settings",
        description=(
            "Read snowpea's own configuration (secrets masked). Use this to show the "
            "user their settings instead of opening settings.json."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Dotted path, e.g. search.provider. Omit for everything.",
                },
                "scope": _SCOPE_SCHEMA,
            },
        },
        permission="read",
        run=settings_get,
    ),
    Tool(
        name="settings_set",
        category="settings",
        description=(
            "Change one snowpea setting by dotted key. Only call this when the user "
            "asked for the change; it always needs their approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Dotted path, e.g. search.provider."},
                "value": {"description": "New value (JSON scalars, objects and lists)."},
                "scope": _SCOPE_SCHEMA,
            },
            "required": ["key", "value"],
        },
        permission="config",
        run=settings_set,
    ),
)


__all__ = ["SCOPES", "TOOLS", "settings_get", "settings_set"]
