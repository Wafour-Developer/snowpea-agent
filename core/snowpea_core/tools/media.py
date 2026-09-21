"""Media generation, forwarded to the snowpea-studio MCP server.

The four tools are always registered so ``tool.list`` describes the whole
surface, but they sit at ``state="inactive"`` until ``settings.media.mcp``
names a command or a url.  ``provider.configure("media", ...)`` flips them to
``active`` in place — no daemon restart — by writing the settings and calling
:func:`refresh_state`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from snowpea_core.tools import mcp_client
from snowpea_core.tools.registry import Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from snowpea_core.server.app_server import Core

log = logging.getLogger("snowpea.tools.media")

#: The MCP server these tools talk to.
SERVER_NAME = "snowpea-studio"

#: Tool name -> the MCP tool it forwards to.
#: ``text_to_speech`` is *not* registered here any more: it lives in
#: ``tools/audio_tools.py``, which runs a provider chain and only reaches the
#: studio server when one is configured.  The forward stays in this table
#: because that is how the audio code calls studio's ``generate_speech``
#: (CORE-multimodal).
FORWARDS: dict[str, str] = {
    "image_generate": "generate_image",
    "video_generate": "generate_video",
    "music_generate": "generate_music",
    "text_to_speech": "generate_speech",
}

#: The tools this module actually registers; the speech one is audio's now.
REGISTERED: tuple[str, ...] = ("image_generate", "video_generate", "music_generate")

INACTIVE = "tool_inactive"


def settings_block(core: Core) -> Any:
    return getattr(getattr(core.settings, "media", None), "mcp", None)


def configured(core: Core) -> bool:
    """True once a command or a url for the studio server is known."""
    block = settings_block(core)
    if block is None:
        return False
    check = getattr(block, "configured", None)
    if callable(check):
        return bool(check())
    return bool(getattr(block, "command", None) or getattr(block, "url", None))


def server_config(core: Core) -> mcp_client.McpServerConfig | None:
    """Build the MCP server entry from ``settings.media.mcp``."""
    block = settings_block(core)
    if block is None or not configured(core):
        return None
    env = dict(getattr(block, "env", None) or {})
    api_key = getattr(block, "api_key", None)
    if api_key:
        env.setdefault("SNOWPEA_STUDIO_API_KEY", str(api_key))
    return mcp_client.McpServerConfig(
        name=SERVER_NAME,
        command=getattr(block, "command", None),
        args=list(getattr(block, "args", None) or []),
        env=env,
        url=getattr(block, "url", None),
    )


def refresh_state(core: Core) -> str:
    """Set every media tool's state from the current settings; return it."""
    state = "active" if configured(core) else "inactive"
    for name in REGISTERED:
        core.tools.set_state(name, state)  # type: ignore[arg-type]
    return state


async def configure(core: Core, config: dict[str, Any]) -> str:
    """Apply a ``provider.configure("media", ...)`` payload and re-register.

    Accepts either the nested ``{"mcp": {...}}`` shape or the fields directly.
    """
    from snowpea_core.config.settings import MediaMcpSettings

    payload = config.get("mcp") if isinstance(config.get("mcp"), dict) else config
    block = MediaMcpSettings.model_validate(payload or {})
    core.settings.media.mcp = block
    try:
        core.settings.save(core.paths)
    except OSError:  # pragma: no cover - a read-only home must not fail the call
        log.warning("could not persist media settings", exc_info=True)
    entry = server_config(core)
    if entry is not None:
        mcp_client.MANAGER.register(entry)
    return refresh_state(core)


async def _forward(ctx: ToolContext, tool_name: str, args: dict[str, Any]) -> ToolResult:
    core = ctx.core
    if not configured(core):
        return ToolResult(
            ok=False,
            error=(
                f"{INACTIVE}: media generation needs the snowpea-studio MCP server. "
                "Set settings.media.mcp.command (or .url), or call "
                'provider.configure with vendor "media".'
            ),
        )
    entry = server_config(core)
    if entry is None:  # pragma: no cover - configured() already checked this
        return ToolResult(ok=False, error=f"{INACTIVE}: no snowpea-studio configuration")
    server = mcp_client.MANAGER.register(entry)
    try:
        rendered = await server.call(FORWARDS[tool_name], args)
    except mcp_client.McpError as exc:
        return ToolResult(ok=False, error=str(exc))
    except TimeoutError:
        return ToolResult(ok=False, error=f"{tool_name} timed out")
    except Exception as exc:  # noqa: BLE001 - the studio server can raise anything
        return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    rendered = mcp_client.as_rendered(rendered)
    meta = {"images": list(rendered.images)} if rendered.images else None
    return ToolResult(ok=True, output=rendered.text, meta=meta)


def _runner(tool_name: str) -> Any:
    async def run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return await _forward(ctx, tool_name, args)

    return run


_PROMPT = {"type": "string", "description": "What to generate."}


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="image_generate",
        category="media",
        description="Generate an image from a text prompt and return its asset id and url.",
        input_schema={
            "type": "object",
            "properties": {
                "prompt": _PROMPT,
                "orientation": {
                    "type": "string",
                    "description": "horizontal, vertical or square.",
                },
                "reference_image_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Asset ids to keep characters or style consistent.",
                },
            },
            "required": ["prompt"],
        },
        permission="network",
        run=_runner("image_generate"),
        state="inactive",
        source="media",
    ),
    Tool(
        name="video_generate",
        category="media",
        description="Generate a video clip from a prompt, optionally from reference images.",
        input_schema={
            "type": "object",
            "properties": {
                "prompt": _PROMPT,
                "duration": {"type": "number", "description": "Clip length in seconds."},
                "orientation": {"type": "string", "description": "horizontal or vertical."},
                "reference_image_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Asset ids used as visual references.",
                },
            },
            "required": ["prompt"],
        },
        permission="network",
        run=_runner("video_generate"),
        state="inactive",
        source="media",
    ),
    Tool(
        name="music_generate",
        category="media",
        description="Generate a music track from a style description and optional lyrics.",
        input_schema={
            "type": "object",
            "properties": {
                "prompt": _PROMPT,
                "lyrics": {"type": "string", "description": "Tagged lyrics, if any."},
                "duration": {"type": "number", "description": "Track length in seconds."},
            },
            "required": ["prompt"],
        },
        permission="network",
        run=_runner("music_generate"),
        state="inactive",
        source="media",
    ),
)


__all__ = [
    "FORWARDS",
    "INACTIVE",
    "REGISTERED",
    "SERVER_NAME",
    "TOOLS",
    "configure",
    "configured",
    "refresh_state",
    "server_config",
    "settings_block",
]
