"""The seven ``lsp_*`` tools (M13 contract §3).

Ported in spirit from opencode's LSP-backed tools; the wire shapes are LSP's
own, so what is here is the translation between an LSP answer and text the
model can act on — ``path:line:character`` rather than a ``file://`` URI, and
symbol kinds as words rather than as the numbers of LSP §3.17.

Everything except ``lsp_rename`` is tagged ``read``.  ``lsp_rename`` writes the
files the server's ``WorkspaceEdit`` names, so it is tagged ``write`` and plan
mode refuses it like any other write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from snowpea_core.lsp import diagnostic as diagnostics_mod
from snowpea_core.lsp.client import uri_to_path
from snowpea_core.lsp.manager import LspManager, manager_for
from snowpea_core.prompts import tool_descriptions as descriptions
from snowpea_core.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

log = logging.getLogger("snowpea.lsp.tools")

#: Tools this module registers, in ``tool.list`` order.
REGISTERED: tuple[str, ...] = (
    "lsp_diagnostics",
    "lsp_definition",
    "lsp_references",
    "lsp_symbols",
    "lsp_workspace_symbols",
    "lsp_hover",
    "lsp_rename",
)

#: Shown in ``tool.list`` when the tools are off.
INACTIVE_REASON = "lsp.enabled is false in settings.json"

#: LSP ``SymbolKind`` -> the word the model reads (LSP §3.17).
SYMBOL_KIND_NAMES: dict[int, str] = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum-member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type-parameter",
}

#: Locations listed before the "… N more" tail.
MAX_LOCATIONS = 100


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _manager(ctx: ToolContext) -> LspManager:
    return manager_for(ctx.core)


def _workdir(ctx: ToolContext) -> Path:
    return Path(ctx.session.workdir).resolve()


def _resolve(ctx: ToolContext, path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _workdir(ctx) / candidate


def _display(ctx: ToolContext, path: Path | str) -> str:
    """``path`` relative to the working directory when it is inside it."""
    resolved = Path(path)
    try:
        return str(resolved.relative_to(_workdir(ctx)))
    except ValueError:
        return str(resolved)


def _disabled() -> ToolResult:
    return ToolResult(ok=False, error=f"tool_inactive: {INACTIVE_REASON}")


def _position_args(args: dict[str, Any]) -> tuple[str, int, int, str | None]:
    """``(path, line, character, error)`` from a tool's arguments."""
    path = str(args.get("path", "")).strip()
    if not path:
        return "", 0, 0, "path is required"
    try:
        line = int(args.get("line", 0))
        character = int(args.get("character", 0))
    except (TypeError, ValueError):
        return path, 0, 0, "line and character must be integers"
    if line < 0 or character < 0:
        return path, 0, 0, "line and character are zero-based and cannot be negative"
    return path, line, character, None


def _location_line(ctx: ToolContext, location: dict[str, Any]) -> str | None:
    path = uri_to_path(str(location.get("uri", "")))
    if path is None:
        return None
    start = (location.get("range") or {}).get("start") or {}
    line = int(start.get("line", 0)) + 1
    character = int(start.get("character", 0)) + 1
    return f"{_display(ctx, path)}:{line}:{character}"


def _location_list(ctx: ToolContext, locations: list[dict[str, Any]]) -> str:
    seen: list[str] = []
    for location in locations:
        rendered = _location_line(ctx, location)
        if rendered is not None and rendered not in seen:
            seen.append(rendered)
    if len(seen) > MAX_LOCATIONS:
        extra = len(seen) - MAX_LOCATIONS
        return "\n".join([*seen[:MAX_LOCATIONS], f"… {extra} more"])
    return "\n".join(seen)


def _hover_text(hover: dict[str, Any]) -> str:
    """``MarkupContent | MarkedString | MarkedString[]`` flattened to text."""
    contents = hover.get("contents")
    if isinstance(contents, dict):
        return str(contents.get("value", "")).strip()
    if isinstance(contents, str):
        return contents.strip()
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("value", "")))
        return "\n\n".join(part for part in parts if part.strip()).strip()
    return ""


def _flatten_symbols(
    symbols: list[dict[str, Any]], depth: int = 0
) -> list[tuple[int, dict[str, Any]]]:
    """``DocumentSymbol`` trees and flat ``SymbolInformation`` lists, one shape."""
    out: list[tuple[int, dict[str, Any]]] = []
    for symbol in symbols:
        if not isinstance(symbol, dict):
            continue
        out.append((depth, symbol))
        children = symbol.get("children")
        if isinstance(children, list):
            out.extend(_flatten_symbols(children, depth + 1))
    return out


def _symbol_line(ctx: ToolContext, depth: int, symbol: dict[str, Any]) -> str:
    raw_kind = symbol.get("kind")
    kind = SYMBOL_KIND_NAMES.get(raw_kind, "symbol") if isinstance(raw_kind, int) else "symbol"
    name = str(symbol.get("name", "?"))
    location = symbol.get("location")
    if isinstance(location, dict):
        rendered = _location_line(ctx, location) or ""
    else:
        start = (symbol.get("selectionRange") or symbol.get("range") or {}).get("start") or {}
        rendered = str(int(start.get("line", 0)) + 1)
    detail = str(symbol.get("detail", "")).strip()
    suffix = f"  {detail}" if detail else ""
    return f"{'  ' * depth}{kind} {name}  {rendered}{suffix}".rstrip()


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


async def lsp_diagnostics(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    raw_path = str(args.get("path", "")).strip()
    if raw_path:
        file = _resolve(ctx, raw_path)
        if not file.exists():
            return ToolResult(ok=False, error=f"no such file: {raw_path}")
        await manager.touch_file(
            file,
            _workdir(ctx),
            wait_for_diagnostics=True,
            session_id=ctx.session.id,
        )
        found = manager.diagnostics(file)
    else:
        found = manager.diagnostics()
    blocks = [
        diagnostics_mod.report(_display(ctx, path), items)
        for path, items in sorted(found.items())
    ]
    body = "\n".join(block for block in blocks if block)
    if not body:
        return ToolResult(ok=True, output="No errors or warnings.", path=raw_path or None)
    return ToolResult(ok=True, output=body, path=raw_path or None)


async def lsp_definition(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    path, line, character, error = _position_args(args)
    if error:
        return ToolResult(ok=False, error=error)
    file = _resolve(ctx, path)
    if not file.exists():
        return ToolResult(ok=False, error=f"no such file: {path}")
    locations = await manager.definition(file, _workdir(ctx), line, character)
    body = _location_list(ctx, locations)
    return ToolResult(ok=True, output=body or "No definition found.", path=path)


async def lsp_references(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    path, line, character, error = _position_args(args)
    if error:
        return ToolResult(ok=False, error=error)
    file = _resolve(ctx, path)
    if not file.exists():
        return ToolResult(ok=False, error=f"no such file: {path}")
    locations = await manager.references(file, _workdir(ctx), line, character)
    body = _location_list(ctx, locations)
    return ToolResult(ok=True, output=body or "No references found.", path=path)


async def lsp_symbols(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    path = str(args.get("path", "")).strip()
    if not path:
        return ToolResult(ok=False, error="path is required")
    file = _resolve(ctx, path)
    if not file.exists():
        return ToolResult(ok=False, error=f"no such file: {path}")
    symbols = await manager.document_symbols(file, _workdir(ctx))
    lines = [_symbol_line(ctx, depth, symbol) for depth, symbol in _flatten_symbols(symbols)]
    return ToolResult(ok=True, output="\n".join(lines) or "No symbols found.", path=path)


async def lsp_workspace_symbols(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    query = str(args.get("query", "")).strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    symbols = await manager.workspace_symbols(query)
    lines = [_symbol_line(ctx, 0, symbol) for symbol in symbols]
    return ToolResult(ok=True, output="\n".join(lines) or "No symbols found.")


async def lsp_hover(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    path, line, character, error = _position_args(args)
    if error:
        return ToolResult(ok=False, error=error)
    file = _resolve(ctx, path)
    if not file.exists():
        return ToolResult(ok=False, error=f"no such file: {path}")
    hover = await manager.hover(file, _workdir(ctx), line, character)
    text = _hover_text(hover) if hover else ""
    return ToolResult(ok=True, output=text or "Nothing here.", path=path)


def apply_workspace_edit(edit: dict[str, Any]) -> dict[Path, str]:
    """Turn a ``WorkspaceEdit`` into ``{path: new content}``.

    Edits within a file are applied from the end backwards so earlier offsets
    stay valid, which is what the LSP spec requires of a client.  A file the
    edit names but that cannot be read is skipped rather than failing the whole
    rename.
    """
    per_file: dict[Path, list[dict[str, Any]]] = {}
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            path = uri_to_path(str(uri))
            if path is not None and isinstance(edits, list):
                per_file.setdefault(path, []).extend(
                    item for item in edits if isinstance(item, dict)
                )
    document_changes = edit.get("documentChanges")
    if isinstance(document_changes, list):
        for entry in document_changes:
            if not isinstance(entry, dict) or "edits" not in entry:
                continue
            uri = (entry.get("textDocument") or {}).get("uri")
            path = uri_to_path(str(uri))
            if path is not None and isinstance(entry["edits"], list):
                per_file.setdefault(path, []).extend(
                    item for item in entry["edits"] if isinstance(item, dict)
                )

    result: dict[Path, str] = {}
    for path, edits in per_file.items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("rename touched %s, which could not be read", path)
            continue
        lines = text.splitlines(keepends=True)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))

        def offset(position: dict[str, Any], _starts: list[int] = starts) -> int:
            line = min(max(0, int(position.get("line", 0))), len(_starts) - 1)
            return min(_starts[line] + int(position.get("character", 0)), _starts[-1])

        spans = sorted(
            (
                (
                    offset((item.get("range") or {}).get("start") or {}),
                    offset((item.get("range") or {}).get("end") or {}),
                    str(item.get("newText", "")),
                )
                for item in edits
            ),
            reverse=True,
        )
        updated = text
        for start, end, replacement in spans:
            updated = updated[:start] + replacement + updated[end:]
        result[path] = updated
    return result


async def lsp_rename(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    manager = _manager(ctx)
    if not manager.enabled:
        return _disabled()
    path, line, character, error = _position_args(args)
    if error:
        return ToolResult(ok=False, error=error)
    new_name = str(args.get("newName", "")).strip()
    if not new_name:
        return ToolResult(ok=False, error="newName is required")
    file = _resolve(ctx, path)
    if not file.exists():
        return ToolResult(ok=False, error=f"no such file: {path}")
    edit = await manager.rename(file, _workdir(ctx), line, character, new_name)
    if not edit:
        return ToolResult(
            ok=False,
            error=(
                "the language server would not rename this symbol; use lsp_references "
                "and edit_file instead"
            ),
        )
    updates = apply_workspace_edit(edit)
    if not updates:
        return ToolResult(ok=False, error="the rename produced no file changes")
    written: list[str] = []
    for target, content in sorted(updates.items()):
        try:
            await ctx.backend.write_file(str(target), content)
        except OSError as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        written.append(_display(ctx, target))
        await manager.touch_file(target, _workdir(ctx), session_id=ctx.session.id)
    body = "\n".join(f"- {name}" for name in written)
    return ToolResult(
        ok=True,
        output=f"renamed to {new_name} in {len(written)} file(s):\n{body}",
        path=path,
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

_POSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File the position is in."},
        "line": {"type": "integer", "description": "Zero-based line of the symbol."},
        "character": {
            "type": "integer",
            "description": "Zero-based column of the symbol on that line.",
        },
    },
    "required": ["path", "line", "character"],
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="lsp_diagnostics",
        category="lsp",
        description=descriptions.LSP_DIAGNOSTICS,
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to check; omit for every file seen so far.",
                }
            },
        },
        permission="read",
        run=lsp_diagnostics,
    ),
    Tool(
        name="lsp_definition",
        category="lsp",
        description=descriptions.LSP_DEFINITION,
        input_schema=_POSITION_SCHEMA,
        permission="read",
        run=lsp_definition,
    ),
    Tool(
        name="lsp_references",
        category="lsp",
        description=descriptions.LSP_REFERENCES,
        input_schema=_POSITION_SCHEMA,
        permission="read",
        run=lsp_references,
    ),
    Tool(
        name="lsp_symbols",
        category="lsp",
        description=descriptions.LSP_SYMBOLS,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File to outline."}},
            "required": ["path"],
        },
        permission="read",
        run=lsp_symbols,
    ),
    Tool(
        name="lsp_workspace_symbols",
        category="lsp",
        description=descriptions.LSP_WORKSPACE_SYMBOLS,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Symbol name, or part of one."}
            },
            "required": ["query"],
        },
        permission="read",
        run=lsp_workspace_symbols,
    ),
    Tool(
        name="lsp_hover",
        category="lsp",
        description=descriptions.LSP_HOVER,
        input_schema=_POSITION_SCHEMA,
        permission="read",
        run=lsp_hover,
    ),
    Tool(
        name="lsp_rename",
        category="lsp",
        description=descriptions.LSP_RENAME,
        input_schema={
            "type": "object",
            "properties": {
                **_POSITION_SCHEMA["properties"],
                "newName": {"type": "string", "description": "The new symbol name."},
            },
            "required": ["path", "line", "character", "newName"],
        },
        permission="write",
        run=lsp_rename,
    ),
)


def refresh_state(core: Any) -> str:
    """Flip every ``lsp_*`` tool active/inactive from ``lsp.enabled``.

    Called from ``wire_core`` and from the settings hot reload, so turning LSP
    off in settings.json takes the tools out of the model's list on the next
    turn rather than at the next restart.
    """
    enabled = bool(getattr(core.settings, "lsp", None) and core.settings.lsp.enabled)
    state = "active" if enabled else "inactive"
    for name in REGISTERED:
        tool = core.tools.set_state(name, state)
        if tool is not None:
            tool.reason = "" if enabled else INACTIVE_REASON
    return state


def register_lsp_tools(registry: ToolRegistry) -> ToolRegistry:
    """Register the seven ``lsp_*`` tools."""
    for tool in TOOLS:
        registry.register(tool)
    return registry


__all__ = [
    "INACTIVE_REASON",
    "MAX_LOCATIONS",
    "REGISTERED",
    "SYMBOL_KIND_NAMES",
    "TOOLS",
    "apply_workspace_edit",
    "lsp_definition",
    "lsp_diagnostics",
    "lsp_hover",
    "lsp_references",
    "lsp_rename",
    "lsp_symbols",
    "lsp_workspace_symbols",
    "refresh_state",
    "register_lsp_tools",
]
